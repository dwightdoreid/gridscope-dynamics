"""
GridScope case (per-unit, MATPOWER-style) -> pypowsybl IIDM network (SI units).

Conventions
-----------
* One voltage level + one bus per GridScope bus (bus-breaker topology).
* Buses joined by transformers share a substation (IIDM requires the two
  sides of a 2-winding transformer to be in the same substation).
* Line:         Z[ohm] = Z[pu] * kV^2 / baseMVA,  B split half at each end.
* Transformer:  MATPOWER model = ideal ratio tap:1 at the FROM side, series
                impedance on the TO side.  In IIDM the ratio rho = ratedU2/ratedU1
                is also at side 1 with the impedance on side 2, so
                ratedU1 = kV_from * tap, ratedU2 = kV_to, Z on the kV_to base.
                A non-zero phase shift becomes a one-step phase tap changer.
* Shunt Gs/Bs are MW/MVAr at 1 pu  ->  G,B[S] = value / kV^2.
* PV/Slack bus generators regulate voltage at Vg (GridScope uses the
  generator setpoint).  Only Slack-bus generators take part in slack
  balancing, so the slack picks up the whole mismatch, as in GridScope.
* Out-of-service branches and generators are not created.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd
import pypowsybl.network as pn

from .case import _num, is_transformer


def _safe(s) -> str:
    return re.sub(r'[^A-Za-z0-9_.\-]', '_', str(s))


@dataclass
class NetworkMap:
    """Links GridScope ids to IIDM ids (and back)."""
    bus_vl: dict = field(default_factory=dict)        # case bus id -> voltage level id
    bus_iidm: dict = field(default_factory=dict)      # case bus id -> IIDM bus id
    branch: dict = field(default_factory=dict)        # case branch id -> IIDM id
    branch_kind: dict = field(default_factory=dict)   # case branch id -> 'line' | 'transformer'
    gen: dict = field(default_factory=dict)           # case gen id -> IIDM generator id
    load: dict = field(default_factory=dict)          # case bus id -> IIDM load id
    slack_vl: str = ''
    slack_gens: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def branch_rev(self):
        return {v: k for k, v in self.branch.items()}

    @property
    def gen_rev(self):
        return {v: k for k, v in self.gen.items()}

    @property
    def bus_rev(self):
        return {v: k for k, v in self.bus_iidm.items()}


def _substation_groups(case) -> dict:
    """Union-find over transformer branches -> substation id per bus."""
    parent = {str(b['id']): str(b['id']) for b in case['buses']}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    kv = {str(b['id']): _num(b['baseKV'], 0) for b in case['buses']}
    for br in case['branches']:
        f, t = str(br['from']), str(br['to'])
        if f in parent and t in parent and (is_transformer(br, case) or abs(kv[f] - kv[t]) > 1e-6):
            parent[find(f)] = find(t)
    roots = {}
    return {b: 'S' + _safe(roots.setdefault(find(b), find(b))) for b in parent}


def build_network(case: dict, slack_participation_only: bool = True):
    """Return (pypowsybl Network, NetworkMap)."""
    base = float(case['baseMVA'])
    m = NetworkMap()
    net = pn.create_empty(_safe(case.get('name', 'case')) or 'case')

    kv = {str(b['id']): _num(b['baseKV'], 1.0) for b in case['buses']}
    sub_of = _substation_groups(case)

    net.create_substations(id=sorted(set(sub_of.values())))
    vl_rows, bus_rows = [], []
    for b in case['buses']:
        bid = str(b['id'])
        vl, bus = f'VL{_safe(bid)}', f'B{_safe(bid)}'
        m.bus_vl[bid], m.bus_iidm[bid] = vl, bus
        vl_rows.append(dict(id=vl, substation_id=sub_of[bid], topology_kind='BUS_BREAKER',
                            nominal_v=kv[bid], name=str(b.get('name', bid))))
        bus_rows.append(dict(id=bus, voltage_level_id=vl, name=str(b.get('name', bid))))
        if b.get('type') == 'Slack':
            m.slack_vl = vl
    net.create_voltage_levels(pd.DataFrame(vl_rows).set_index('id'))
    net.create_buses(pd.DataFrame(bus_rows).set_index('id'))

    # ---------------------------------------------------------- branches
    lines, twts, ptcs, ptc_steps, used = [], [], [], [], set()

    def unique(prefix, raw):
        i, cand = 1, f'{prefix}{_safe(raw)}'
        while cand in used:
            i += 1
            cand = f'{prefix}{_safe(raw)}_{i}'
        used.add(cand)
        return cand

    for br in case['branches']:
        if not _num(br.get('status'), 1):
            continue
        f, t = str(br['from']), str(br['to'])
        if f not in kv or t not in kv:
            continue
        r, x, bsh = _num(br['r'], 0), _num(br['x'], 0), _num(br['b'], 0)
        tap, shift = _num(br.get('tap'), 1) or 1.0, _num(br.get('shift'), 0)
        transformer = is_transformer(br, case) or abs(kv[f] - kv[t]) > 1e-6
        if not transformer:
            zb = kv[f] ** 2 / base
            iid = unique('L', br['id'])
            lines.append(dict(id=iid, voltage_level1_id=m.bus_vl[f], bus1_id=m.bus_iidm[f],
                              voltage_level2_id=m.bus_vl[t], bus2_id=m.bus_iidm[t],
                              r=r * zb, x=x * zb, g1=0.0, g2=0.0, b1=bsh / 2 / zb, b2=bsh / 2 / zb))
            m.branch_kind[br['id']] = 'line'
        else:
            zb2 = kv[t] ** 2 / base
            iid = unique('T', br['id'])
            twts.append(dict(id=iid, voltage_level1_id=m.bus_vl[f], bus1_id=m.bus_iidm[f],
                             voltage_level2_id=m.bus_vl[t], bus2_id=m.bus_iidm[t],
                             rated_u1=kv[f] * tap, rated_u2=kv[t],
                             r=r * zb2, x=x * zb2, g=0.0, b=bsh / zb2))
            if abs(shift) > 1e-9:
                ptcs.append(dict(id=iid, target_deadband=0.0, regulation_mode='CURRENT_LIMITER',
                                 low_tap=0, tap=0, regulating=False))
                ptc_steps.append(dict(id=iid, b=0.0, g=0.0, r=0.0, x=0.0, rho=1.0, alpha=-shift))
            m.branch_kind[br['id']] = 'transformer'
        m.branch[br['id']] = iid
    if lines:
        net.create_lines(pd.DataFrame(lines).set_index('id'))
    if twts:
        net.create_2_windings_transformers(pd.DataFrame(twts).set_index('id'))
    if ptcs:
        net.create_phase_tap_changers(pd.DataFrame(ptcs).set_index('id'),
                                      pd.DataFrame(ptc_steps).set_index('id'))

    # ------------------------------------------------------ loads, shunts
    loads, shunts, shunt_models = [], [], []
    for b in case['buses']:
        bid = str(b['id'])
        pd_, qd = _num(b['Pd'], 0), _num(b['Qd'], 0)
        if abs(pd_) > 1e-12 or abs(qd) > 1e-12:
            lid = f'LD{_safe(bid)}'
            loads.append(dict(id=lid, voltage_level_id=m.bus_vl[bid], bus_id=m.bus_iidm[bid], p0=pd_, q0=qd))
            m.load[bid] = lid
        gs, bs = _num(b['Gs'], 0), _num(b['Bs'], 0)
        if abs(gs) > 1e-12 or abs(bs) > 1e-12:
            sid = f'SH{_safe(bid)}'
            shunts.append(dict(id=sid, voltage_level_id=m.bus_vl[bid], bus_id=m.bus_iidm[bid],
                               model_type='LINEAR', section_count=1))
            shunt_models.append(dict(id=sid, g_per_section=gs / kv[bid] ** 2,
                                     b_per_section=bs / kv[bid] ** 2, max_section_count=1))
    if loads:
        net.create_loads(pd.DataFrame(loads).set_index('id'))
    if shunts:
        net.create_shunt_compensators(pd.DataFrame(shunts).set_index('id'),
                                      pd.DataFrame(shunt_models).set_index('id'))

    # --------------------------------------------------------- generators
    bus_type = {str(b['id']): b.get('type', 'PQ') for b in case['buses']}
    bus_vm = {str(b['id']): _num(b.get('Vm'), 1.0) for b in case['buses']}
    gens, limits, has_gen = [], [], set()
    for g in case['generators']:
        if not _num(g.get('status'), 1):
            continue
        bid = str(g['bus'])
        if bid not in kv:
            continue
        gid = unique('', g['id']) if str(g['id']) else unique('G', bid)
        regulating = bus_type[bid] in ('PV', 'Slack')
        pmin, pmax = _num(g['Pmin'], 0), _num(g['Pmax'], 9999)
        if bus_type[bid] == 'Slack':
            # GridScope does not cap the slack generator, so neither does the load flow
            pmin, pmax = min(pmin, -99999.0), max(pmax, 99999.0)
            m.slack_gens.append(gid)
        gens.append(dict(id=gid, voltage_level_id=m.bus_vl[bid], bus_id=m.bus_iidm[bid],
                         target_p=_num(g['Pg'], 0), target_q=_num(g['Qg'], 0),
                         target_v=(_num(g.get('Vg'), 0) or bus_vm[bid]) * kv[bid],
                         voltage_regulator_on=regulating, min_p=pmin, max_p=max(pmax, pmin),
                         energy_source='OTHER',
                         rated_s=_num(g.get('ratedMVA'), 0) or float('nan')))
        qmin, qmax = _num(g['Qmin'], -9999), _num(g['Qmax'], 9999)
        if qmax < qmin:
            qmin, qmax = qmax, qmin
        if bus_type[bid] == 'Slack':
            qmin, qmax = -99999.0, 99999.0   # GridScope never limits the slack's Q
        limits.append(dict(id=gid, min_q=qmin, max_q=qmax))
        m.gen[g['id']] = gid
        has_gen.add(bid)
    for bid, t in bus_type.items():
        if t in ('PV', 'Slack') and bid not in has_gen:
            gid = unique('VSRC_', bid)
            gens.append(dict(id=gid, voltage_level_id=m.bus_vl[bid], bus_id=m.bus_iidm[bid],
                             target_p=0.0, target_q=0.0, target_v=bus_vm[bid] * kv[bid],
                             voltage_regulator_on=True, min_p=-99999.0 if t == 'Slack' else 0.0,
                             max_p=99999.0 if t == 'Slack' else 0.0, energy_source='OTHER',
                             rated_s=float('nan')))
            limits.append(dict(id=gid, min_q=-9999.0, max_q=9999.0))
            if t == 'Slack':
                m.slack_gens.append(gid)
            m.notes.append(f'{t} bus {bid} has no generator: a voltage source "{gid}" was added '
                           'so the load flow can hold its voltage.')
    if gens:
        gdf = pd.DataFrame(gens).set_index('id')
        if gdf['rated_s'].isna().all():
            gdf = gdf.drop(columns='rated_s')
        else:
            gdf['rated_s'] = gdf['rated_s'].fillna(0.0)
            gdf.loc[gdf['rated_s'] <= 0, 'rated_s'] = float('nan')
        net.create_generators(gdf)
        net.create_minmax_reactive_limits(pd.DataFrame(limits).set_index('id'))
        if slack_participation_only:
            ids = gdf.index.tolist()
            net.create_extensions('activePowerControl', id=ids, droop=[4.0] * len(ids),
                                  participate=[i in m.slack_gens for i in ids])
    # Dynawo refuses networks with an empty sourceFormat, which create_empty() leaves blank
    xml = net.save_to_string('XIIDM')
    xml = xml.replace('sourceFormat=""', 'sourceFormat="GridScope"', 1)
    net = pn.load_from_string('case.xiidm', xml)
    return net, m
