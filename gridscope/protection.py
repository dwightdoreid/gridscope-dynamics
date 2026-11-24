"""
Protection models for dynamic simulations: settings, Dynawo parameter sets and what operated.

Settings live in case["dynamicSettings"]["protection"], per element, in engineering units:

  generators  {gen id: {...}}    over/under speed, under/over voltage, loss of synchronism
              -> Dynawo SpeedProtection, UVA, OVA, LossOfSynchronismProtection combined in
                 GsGeneratorProtection, compiled into the machine model (synchronous machines)
  lines       {branch id: {...}} distance (two zones, both ends) + definite-time overcurrent
              -> GsLineProtected: Dynawo Line + DistanceProtectionLineTwoZones at each end
  transformers{branch id: {...}} definite-time overcurrent / overload
              -> Dynawo CurrentLimitAutomaton (pypowsybl OverloadManagementSystem)
  loads       {bus id: {...}}    under-frequency (3 steps) and under-voltage load shedding
              -> GsLoadShedding (GsLoadSheddingLocal with line protection): alpha-beta load + Dynawo UFLS3Steps + UVLS

Only the elements with at least one function switched on get a protection model.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

GEN = dict(speed=False, f_min=47.5, f_max=52.0, t_speed=1.0,
           uv=False, u_min=0.7, t_uv=1.5,
           ov=False, u_max=1.2, t_ov=1.0,
           los=False, theta_max=150.0, t_los=0.0)
LINE = dict(distance=False, z1=80.0, z2=120.0, r_reach=100.0, t_z1=0.0, t_z2=0.3, t_cb=0.06,
            oc=False, i_pickup=150.0, t_oc=1.0)
TRANSFORMER = dict(oc=False, i_pickup=150.0, t_oc=2.0)
LOAD = dict(ufls=False, f1=49.0, s1=10.0, f2=48.7, s2=10.0, f3=48.4, s3=10.0, t_ufls=0.1,
            uvls=False, u_min=0.85, s_uv=20.0, t_uvls=1.0)
DEFAULTS = dict(generators=GEN, lines=LINE, transformers=TRANSFORMER, loads=LOAD)
SWITCHES = dict(generators=('speed', 'uv', 'ov', 'los'), lines=('distance', 'oc'), transformers=('oc',),
                loads=('ufls', 'uvls'))

# column labels for the editor (key -> label, help)
COLUMNS = {
    'generators': {
        'speed': ('Speed', 'Over/under speed (frequency) protection'),
        'f_min': ('f min Hz', 'Under-speed trip threshold'), 'f_max': ('f max Hz', 'Over-speed trip threshold'),
        't_speed': ('t speed s', 'Over/under speed time delay'),
        'uv': ('UV', 'Under-voltage protection'), 'u_min': ('U min pu', 'Under-voltage threshold'),
        't_uv': ('t UV s', 'Under-voltage time delay'),
        'ov': ('OV', 'Over-voltage protection'), 'u_max': ('U max pu', 'Over-voltage threshold'),
        't_ov': ('t OV s', 'Over-voltage time delay'),
        'los': ('Pole slip', 'Loss-of-synchronism protection: trips when the internal angle exceeds the limit'),
        'theta_max': ('δ max °', 'Internal (load) angle limit'), 't_los': ('t slip s', 'Loss-of-synchronism delay')},
    'lines': {
        'distance': ('Distance', 'Two-zone distance protection at both ends'),
        'z1': ('Zone 1 %', 'Zone 1 reach, % of the line impedance'), 't_z1': ('t Z1 s', 'Zone 1 delay'),
        'z2': ('Zone 2 %', 'Zone 2 reach, % of the line impedance'), 't_z2': ('t Z2 s', 'Zone 2 delay'),
        'r_reach': ('R reach %', 'Resistive reach of each zone, % of its reactive reach'),
        't_cb': ('Breaker s', 'Circuit-breaker opening time added to the zone delays'),
        'oc': ('Overcurrent', 'Definite-time overcurrent'), 'i_pickup': ('I pick-up %', 'Pick-up, % of the rating'),
        't_oc': ('t OC s', 'Overcurrent delay')},
    'transformers': {
        'oc': ('Overcurrent', 'Definite-time overcurrent / overload (Dynawo current limit automaton)'),
        'i_pickup': ('I pick-up %', 'Pick-up, % of the rating'), 't_oc': ('t OC s', 'Overcurrent delay')},
    'loads': {
        'ufls': ('UFLS', 'Under-frequency load shedding, 3 steps'),
        'f1': ('f1 Hz', 'Step 1 threshold'), 's1': ('Step 1 %', 'Share of the load shed by step 1'),
        'f2': ('f2 Hz', 'Step 2 threshold'), 's2': ('Step 2 %', 'Share shed by step 2'),
        'f3': ('f3 Hz', 'Step 3 threshold'), 's3': ('Step 3 %', 'Share shed by step 3'),
        't_ufls': ('t UFLS s', 'Delay of each step (relay + breaker)'),
        'uvls': ('UVLS', 'Under-voltage load shedding'), 'u_min': ('U min pu', 'UVLS threshold'),
        's_uv': ('UVLS %', 'Share of the load shed by UVLS'), 't_uvls': ('t UVLS s', 'UVLS delay')},
}
GEN_CAUSE = {1: 'over/under speed', 2: 'under-voltage', 3: 'over-voltage', 4: 'loss of synchronism'}
LINE_CAUSE = {1: 'distance (end 1)', 2: 'distance (end 2)', 3: 'overcurrent'}


def settings(case: dict) -> dict:
    """All protection settings of the case, defaults filled in: {kind: {element id: cfg}}."""
    raw = ((case.get('dynamicSettings') or {}).get('protection') or {})
    return {kind: {str(k): {**DEFAULTS[kind], **(v or {})} for k, v in (raw.get(kind) or {}).items()}
            for kind in DEFAULTS}


def active(cfg: dict, kind: str) -> bool:
    return any(bool(cfg.get(s)) for s in SWITCHES[kind])


def f_nom(case: dict) -> float:
    return float((case.get('dynamicSettings') or {}).get('f_nom', 50.0))


def _par(t, name, v):
    if t == 'BOOL':
        v = 'true' if v else 'false'
    elif t == 'INT':
        v = str(int(v))
    else:
        v = repr(float(v))
    return f'    <par type="{t}" name="{name}" value="{v}"/>'


def gen_pars(cfg: dict, fn: float, prefix='protection') -> list[str]:
    p = prefix + '_'
    return [_par('BOOL', p + 'SpeedOn', cfg['speed']), _par('DOUBLE', p + 'OmegaMinPu', cfg['f_min'] / fn),
            _par('DOUBLE', p + 'OmegaMaxPu', cfg['f_max'] / fn), _par('DOUBLE', p + 'tSpeed', cfg['t_speed']),
            _par('BOOL', p + 'UVOn', cfg['uv']), _par('DOUBLE', p + 'UMinPu', cfg['u_min']),
            _par('DOUBLE', p + 'tUV', cfg['t_uv']),
            _par('BOOL', p + 'OVOn', cfg['ov']), _par('DOUBLE', p + 'UMaxPu', cfg['u_max']),
            _par('DOUBLE', p + 'tOV', cfg['t_ov']),
            _par('BOOL', p + 'LOSOn', cfg['los']), _par('DOUBLE', p + 'ThetaMax', math.radians(cfg['theta_max'])),
            _par('DOUBLE', p + 'tLOS', cfg['t_los'])]


def line_par_set(set_id: str, cfg: dict, r_pu: float, x_pu: float, g_pu: float, b_pu: float,
                 rating_mva: float) -> str:
    """Parameter set of GsLineProtected. Impedances in pu on 100 MVA and the line's nominal voltage;
    g_pu / b_pu are the half shunt admittances."""
    zx = abs(x_pu) if abs(x_pu) > 1e-9 else math.hypot(r_pu, x_pu)
    # a line split for a line fault: reaches stay based on the whole line, and each section keeps only the
    # relay at the real line end (the inner end at the fault point is fictitious)
    zx = cfg.get('_zx_pu') or zx
    ends = cfg.get('_ends', (1, 2))
    z1x, z2x = zx * cfg['z1'] / 100, zx * cfg['z2'] / 100
    k = cfg['r_reach'] / 100
    imax = (cfg['i_pickup'] / 100) * (rating_mva / 100.0) if rating_mva > 0 else 1e6
    lines = [f'  <set id="{set_id}">',
             _par('DOUBLE', 'line_RPu', r_pu), _par('DOUBLE', 'line_XPu', x_pu),
             _par('DOUBLE', 'line_GPu', g_pu), _par('DOUBLE', 'line_BPu', b_pu),
             _par('BOOL', 'line_DistanceOn', cfg['distance']),
             _par('BOOL', 'line_Distance1On', 1 in ends), _par('BOOL', 'line_Distance2On', 2 in ends),
             _par('DOUBLE', 'line_GEnd1Pu', 1e-4 if cfg.get('_inner') == 1 else 0.0),
             _par('DOUBLE', 'line_GEnd2Pu', 1e-4 if cfg.get('_inner') == 2 else 0.0),
             _par('DOUBLE', 'line_Z1RPu', max(z1x * k, 1e-6)), _par('DOUBLE', 'line_Z1XPu', z1x),
             _par('DOUBLE', 'line_tZ1', cfg['t_z1']),
             _par('DOUBLE', 'line_Z2RPu', max(z2x * k, 1e-6)), _par('DOUBLE', 'line_Z2XPu', z2x),
             _par('DOUBLE', 'line_tZ2', cfg['t_z2']), _par('DOUBLE', 'line_CircuitBreakerTime', cfg['t_cb']),
             _par('BOOL', 'line_OCOn', cfg['oc'] and rating_mva > 0), _par('DOUBLE', 'line_IMaxPu', imax),
             _par('DOUBLE', 'line_tOC', cfg['t_oc']), '  </set>']
    return '\n'.join(lines)


def oms_par_set(set_id: str, cfg: dict, i_rated_a: float) -> str:
    """Dynawo CurrentLimitAutomaton: opens the transformer when its current (A) stays above IMax."""
    return '\n'.join([f'  <set id="{set_id}">',
                      _par('DOUBLE', 'currentLimitAutomaton_IMax', cfg['i_pickup'] / 100 * i_rated_a),
                      _par('INT', 'currentLimitAutomaton_OrderToEmit', 1),
                      _par('BOOL', 'currentLimitAutomaton_Running', True),
                      _par('DOUBLE', 'currentLimitAutomaton_tLagBeforeActing', cfg['t_oc']), '  </set>'])


def load_par_set(set_id: str, cfg: dict, alpha: float, beta: float, fn: float) -> str:
    lines = [f'  <set id="{set_id}">',
             _par('DOUBLE', 'load_alpha', alpha), _par('DOUBLE', 'load_beta', beta),
             _par('BOOL', 'load_UFLSOn', cfg['ufls'])]
    for i in (1, 2, 3):
        lines += [_par('DOUBLE', f'load_omegaThreshold{i}Pu', cfg[f'f{i}'] / fn),
                  _par('DOUBLE', f'load_UFLSStep{i}', cfg[f's{i}'] / 100)]
    lines += [_par('DOUBLE', 'load_tUFLS', cfg['t_ufls']), _par('BOOL', 'load_UVLSOn', cfg['uvls']),
              _par('DOUBLE', 'load_UMinPu', cfg['u_min']), _par('DOUBLE', 'load_UVLSStep', cfg['s_uv'] / 100),
              _par('DOUBLE', 'load_tUVLS', cfg['t_uvls'])]
    for k, o in (('P0Pu', 'p_pu'), ('Q0Pu', 'q_pu'), ('U0Pu', 'v_pu'), ('UPhase0', 'angle_pu')):
        lines.append(f'    <reference name="load_{k}" origData="IIDM" origName="{o}" type="DOUBLE"/>')
    lines.append('  </set>')
    return '\n'.join(lines)


def validate(case: dict) -> list[str]:
    """Settings that cannot work."""
    out, fn = [], f_nom(case)
    s = settings(case)
    what = {'generators': 'Generator', 'lines': 'Line', 'transformers': 'Transformer', 'loads': 'Load at bus'}
    for kind, items in s.items():
        for eid, c in items.items():
            if not active(c, kind):
                continue
            for k, v in c.items():
                if k.startswith('_') or isinstance(v, bool) or not isinstance(v, (int, float)):
                    continue
                label = COLUMNS.get(kind, {}).get(k, (k,))[0]
                if (k.startswith('t_') or k in ('s1', 's2', 's3', 's_uv')) and v < 0:
                    out.append(f'{what[kind]} {eid}: {label} must not be negative.')
                elif not (k.startswith('t_') or k in ('s1', 's2', 's3', 's_uv')) and v <= 0:
                    out.append(f'{what[kind]} {eid}: {label} must be positive.')
    for gid, c in s['generators'].items():
        if c['speed'] and not (c['f_min'] < fn < c['f_max']):
            out.append(f'Generator {gid}: speed protection thresholds must bracket {fn:g} Hz.')
        if c['uv'] and c['ov'] and c['u_min'] >= c['u_max']:
            out.append(f'Generator {gid}: U min must be below U max.')
    for bid, c in s['lines'].items():
        if c['distance'] and not (0 < c['z1'] <= c['z2']):
            out.append(f'Line {bid}: zone 1 reach must be positive and not larger than zone 2.')
    for bus, c in s['loads'].items():
        if c['ufls']:
            if c['s1'] + c['s2'] + c['s3'] > 100:
                out.append(f'Load at bus {bus}: UFLS steps shed more than 100 %.')
            if not (c['f1'] >= c['f2'] >= c['f3']):
                out.append(f'Load at bus {bus}: UFLS thresholds must decrease from step 1 to step 3.')
            if c['f1'] >= fn:
                out.append(f'Load at bus {bus}: UFLS step 1 must be below {fn:g} Hz.')
    return out


def operations(curves: pd.DataFrame, gen_ids: dict, line_ids: dict, tfo_ids: dict, load_ids: dict) -> pd.DataFrame:
    """What the protections did, from the recorded protection variables.

    gen_ids / line_ids / tfo_ids / load_ids: {IIDM id: case id} of the protected elements."""
    rows = []

    def first_change(col):
        s = curves[col]
        nz = s[s.abs() > 0.5]
        return (float(nz.index[0]), int(round(nz.iloc[0]))) if len(nz) else None

    for iid, cid in gen_ids.items():
        col = f'{iid}_protection_tripCause'
        if col in curves and (fc := first_change(col)):
            rows.append(dict(time=fc[0], element=f'Generator {cid}', kind='generator', id=cid,
                             protection=GEN_CAUSE.get(fc[1], 'protection'), action='tripped'))
    for iid, cid in line_ids.items():
        col = f'{iid}_line_tripCause'
        if col in curves and (fc := first_change(col)):
            rows.append(dict(time=fc[0], element=f'Line {cid}', kind='branch', id=cid,
                             protection=LINE_CAUSE.get(fc[1], 'protection'), action='opened'))
    for iid, cid in tfo_ids.items():
        col = f'{iid}_transformer_tripCause'        # Modelica transformer (network modelled in Modelica)
        if col in curves and (fc := first_change(col)):
            rows.append(dict(time=fc[0], element=f'Transformer {cid}', kind='branch', id=cid,
                             protection='overcurrent', action='opened'))
            continue
        col = f'OC_{iid}_currentLimitAutomaton_order_value'
        if col in curves:
            s = curves[col]
            hit = s[s == 1]
            if len(hit):
                rows.append(dict(time=float(hit.index[0]), element=f'Transformer {cid}', kind='branch', id=cid,
                                 protection='overcurrent', action='opened'))
    for iid, cid in load_ids.items():
        col = f'{iid}_load_shed'
        if col in curves:
            s = curves[col].clip(lower=0).round(3)
            prev = 0.0
            for t, v in s.items():
                if v > prev + 0.005:
                    # report each settled shedding step once (the shed share is smoothed over ~10 ms)
                    nxt = s[s.index >= t]
                    level = float(nxt.iloc[min(len(nxt) - 1, np.searchsorted(nxt.index, t + 0.05))])
                    if level > prev + 0.005:
                        rows.append(dict(time=float(t), element=f'Load at bus {cid}', kind='load', id=cid,
                                         protection='load shedding', action=f'{100 * level:.0f} % shed'))
                        prev = level
    df = pd.DataFrame(rows, columns=['time', 'element', 'kind', 'id', 'protection', 'action'])
    return df.sort_values('time').reset_index(drop=True)
