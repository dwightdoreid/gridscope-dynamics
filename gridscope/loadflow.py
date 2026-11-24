"""AC / DC load flow on the converted network, with GridScope-style result tables."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pypowsybl.loadflow as lf

from .case import _num
from .convert import build_network


def lf_parameters(net_map, enforce_q_limits=True, dc=False, flat_start=True):
    prov = {
        'slackBusSelectionMode': 'NAME',
        'slackBusesIds': net_map.slack_vl,
        'slackBusPMaxMismatch': '0.0001',
        'maxNewtonRaphsonIterations': '30',
        'newtonRaphsonStoppingCriteriaType': 'UNIFORM_CRITERIA',
        'newtonRaphsonConvEpsPerEq': '1e-8',
        'reactiveLimitsMaxPqPvSwitch': '0',      # like GridScope: never switch back PQ -> PV
        'plausibleActivePowerLimit': '100000',
    }
    return lf.Parameters(
        distributed_slack=True,
        balance_type=lf.BalanceType.PROPORTIONAL_TO_GENERATION_P_MAX,
        use_reactive_limits=enforce_q_limits,
        transformer_voltage_control_on=False,
        twt_split_shunt_admittance=True,      # B split half per side, like a MATPOWER branch
        phase_shifter_regulation_on=False,
        shunt_compensator_voltage_control_on=False,
        voltage_init_mode=lf.VoltageInitMode.UNIFORM_VALUES if flat_start else None,
        dc_use_transformer_ratio=True,
        provider_parameters=prov,
    )


def run_ac(case: dict, enforce_q_limits=True, network=None, net_map=None):
    """Solve and return (network, map, result dict). Results use GridScope's table layout."""
    if network is None:
        network, net_map = build_network(case)
    res = lf.run_ac(network, lf_parameters(net_map, enforce_q_limits))
    comp = res[0]
    converged = comp.status.name == 'CONVERGED'
    log = [f'OpenLoadFlow AC Newton–Raphson (slack: bus {next((b["id"] for b in case["buses"] if b["type"] == "Slack"), "?")})',
           f'Status: {comp.status.name} — {comp.status_text}',
           f'Iterations: {comp.iteration_count}']
    for s in comp.slack_bus_results:
        log.append(f'Slack mismatch after distribution: {s.active_power_mismatch:.3e} MW')
    log += net_map.notes
    out = dict(converged=converged, iterations=comp.iteration_count, status=comp.status.name, log=log)
    if converged:
        out.update(_tables(case, network, net_map))
        out['q_switches'] = _q_switches(case, network, net_map, enforce_q_limits)
        for sw in out['q_switches']:
            log.append(f'Bus {sw["bus"]}: PV→PQ switch, generator(s) held at {sw["limit"]} = {sw["value"]:.2f} MVAr')
        log.append(f'Converged in {comp.iteration_count} iterations.')
    else:
        log.append('Did not converge — results are NOT reliable and should not be used.')
    return network, net_map, out


def _tables(case, network, m):
    base = case['baseMVA']
    vl = network.get_voltage_levels()['nominal_v']
    buses = network.get_bus_breaker_view_buses()
    gens = network.get_generators()
    loads = network.get_loads()
    shunts = network.get_shunt_compensators()

    bus_rows = []
    for b in case['buses']:
        bid = str(b['id'])
        iidm = m.bus_iidm[bid]
        v_kv, ang = buses.loc[iidm, 'v_mag'], buses.loc[iidm, 'v_angle']
        vm = v_kv / vl[m.bus_vl[bid]]
        g_here = gens[gens['bus_breaker_bus_id'] == iidm] if 'bus_breaker_bus_id' in gens else gens[gens['bus_id'].str.startswith(m.bus_vl[bid])]
        pg, qg = -g_here['p'].sum(), -g_here['q'].sum()
        pd_, qd = _num(b['Pd'], 0), _num(b['Qd'], 0)
        bus_rows.append(dict(id=b['id'], name=b.get('name', bid), type=b['type'], Vm=vm, Va=ang,
                             Pg=pg, Qg=qg, Pd=pd_, Qd=qd, Pinj=pg - pd_, Qinj=qg - qd))
    bus_df = pd.DataFrame(bus_rows)

    lines = network.get_lines()[['p1', 'q1', 'p2', 'q2', 'i1', 'i2']]
    twts = network.get_2_windings_transformers()[['p1', 'q1', 'p2', 'q2', 'i1', 'i2']]
    flows = pd.concat([lines, twts])
    br_rows = []
    for br in case['branches']:
        iid = m.branch.get(br['id'])
        row = dict(id=br['id'], from_=br['from'], to=br['to'], type=br.get('equipmentType', 'line'),
                   rate=_num(br.get('rate'), 0), status=int(_num(br.get('status'), 1)))
        if iid is None or iid not in flows.index:
            row.update(Pf=0.0, Qf=0.0, Pt=0.0, Qt=0.0, loss=0.0, qloss=0.0, loading=0.0)
        else:
            f = flows.loc[iid]
            pf, qf, pt, qt = (float(np.nan_to_num(f[k])) for k in ('p1', 'q1', 'p2', 'q2'))
            rate = row['rate']
            loading = 100 * max(math.hypot(pf, qf), math.hypot(pt, qt)) / rate if rate else 0.0
            row.update(Pf=pf, Qf=qf, Pt=pt, Qt=qt, loss=pf + pt, qloss=qf + qt, loading=loading)
        br_rows.append(row)
    br_df = pd.DataFrame(br_rows).rename(columns={'from_': 'from'})
    vm_by_bus = dict(zip(bus_df['id'].astype(str), bus_df['Vm']))
    type_by_bus = {str(b['id']): b['type'] for b in case['buses']}
    gen_rows = []
    for g in case['generators']:
        gid = m.gen.get(g['id'])
        on = int(_num(g.get('status'), 1)) and gid is not None and gid in gens.index
        p = -float(np.nan_to_num(gens.loc[gid, 'p'])) if on else 0.0
        q = -float(np.nan_to_num(gens.loc[gid, 'q'])) if on else 0.0
        pmin, pmax = _num(g.get('Pmin'), 0), _num(g.get('Pmax'), 0)
        qmin, qmax = _num(g.get('Qmin'), -9999), _num(g.get('Qmax'), 9999)
        rated = _num(g.get('ratedMVA'), 0)
        bus = str(g['bus'])
        flags = []
        if on:
            if pmax and p > pmax + 1e-3:
                flags.append('P > Pmax')
            if p < pmin - 1e-3:
                flags.append('P < Pmin')
            if q > qmax + 1e-3:
                flags.append('Q > Qmax')       # only the slack, whose Q is never limited
            elif q >= qmax - 1e-3:
                flags.append('at Qmax')
            elif q < qmin - 1e-3:
                flags.append('Q < Qmin')
            elif q <= qmin + 1e-3:
                flags.append('at Qmin')
        gen_rows.append(dict(id=g['id'], bus=g['bus'], busType=type_by_bus.get(bus, ''), status='on' if on else 'off',
                             Psched=_num(g.get('Pg'), 0) if on else 0.0, P=p, dP=p - (_num(g.get('Pg'), 0) if on else 0.0),
                             Q=q, S=math.hypot(p, q), Pmin=pmin, Pmax=pmax, Qmin=qmin, Qmax=qmax,
                             Vset=_num(g.get('Vg'), 1.0), V=vm_by_bus.get(bus, float('nan')),
                             loading=100 * math.hypot(p, q) / rated if rated and on else float('nan'),
                             limits=', '.join(flags) or ('ok' if on else '')))
    gen_df = pd.DataFrame(gen_rows)
    return dict(buses=bus_df, branches=br_df, generators=gen_df,
                Ploss=float(br_df['loss'].sum()), Qloss=float(br_df['qloss'].sum()),
                total_gen=float(bus_df['Pg'].sum()), total_load=float(bus_df['Pd'].sum()))


def _q_switches(case, network, m, enforce):
    if not enforce:
        return []
    gens = network.get_generators()
    out = []
    bus_type = {str(b['id']): b['type'] for b in case['buses']}
    by_bus = {}
    for g in case['generators']:
        gid = m.gen.get(g['id'])
        if gid is not None and bus_type.get(str(g['bus'])) == 'PV':
            by_bus.setdefault(str(g['bus']), []).append((g, gid))
    for bid, items in by_bus.items():
        q = -sum(gens.loc[gid, 'q'] for _, gid in items)
        qmax = sum(_num(g['Qmax'], 9999) for g, _ in items)
        qmin = sum(_num(g['Qmin'], -9999) for g, _ in items)
        if q >= qmax - 1e-3:
            out.append(dict(bus=bid, limit='Qmax', value=q))
        elif q <= qmin + 1e-3:
            out.append(dict(bus=bid, limit='Qmin', value=q))
    return out


def run_dc(case: dict, network=None, net_map=None, outage: str | None = None):
    """DC load flow (lossless, |V| = 1). Returns branch MW flows keyed by case branch id."""
    if network is None:
        network, net_map = build_network(case)
    res = lf.run_dc(network, lf_parameters(net_map, dc=True))
    comp = res[0]
    lines = network.get_lines()[['p1']]
    twts = network.get_2_windings_transformers()[['p1']]
    flows = pd.concat([lines, twts])['p1']
    rows = []
    for br in case['branches']:
        iid = net_map.branch.get(br['id'])
        p = float(np.nan_to_num(flows.get(iid, 0.0))) if iid else 0.0
        rate = _num(br.get('rate'), 0)
        rows.append(dict(id=br['id'], **{'from': br['from']}, to=br['to'], Pf=p, rate=rate,
                         loading=100 * abs(p) / rate if rate else 0.0))
    buses = network.get_bus_breaker_view_buses()
    ang = {str(b['id']): float(buses.loc[net_map.bus_iidm[str(b['id'])], 'v_angle']) for b in case['buses']}
    return dict(converged=comp.status.name == 'CONVERGED', status=comp.status.name,
                branches=pd.DataFrame(rows), angles=ang)
