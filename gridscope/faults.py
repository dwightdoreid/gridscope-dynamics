"""
Short-circuit analysis — a numpy port of GridScope's Phase-2 fault engine
(same defaults, same zero-sequence winding rules, same IEC / ANSI formulas),
so results are directly comparable with the original web app.

Sequence networks are built from the case data (pu on the system base);
Zbus = inv(Ybus) per sequence; faults: 3ph, SLG, LL, LLG with fault impedance.

Difference from GridScope: source contributions are scaled by the actual
positive-sequence fault current (incl. c factor and fault impedance), so the
per-source shares add up to the fault current. GridScope scales them by the
bolted 1.0 pu current.
"""
from __future__ import annotations

import copy
import math

import numpy as np
import pandas as pd

from .case import _num

FAULT_LABELS = {'3ph': '3Φ', 'slg': '1Φ-G', 'll': 'Φ-Φ', 'llg': '2Φ-G'}
A = np.exp(2j * np.pi / 3)


# ---------------------------------------------------------- input data

def ensure_fault_data(case: dict, mark_assumed=True) -> dict:
    """Fill missing sequence / grounding data with GridScope's defaults (in place)."""
    for br in case.get('branches', []):
        assumed = False
        if not br.get('equipmentType'):
            t = abs(_num(br.get('tap'), 1) - 1) > 1e-6 or abs(_num(br.get('shift'), 0)) > 1e-6 \
                or str(br.get('id', '')).upper().startswith('T')
            br['equipmentType'] = 'transformer' if t else 'line'
            assumed = True
        xf = br['equipmentType'] == 'transformer'
        for k, v in (('fromConnection', 'wye-grounded' if xf else 'line'),
                     ('toConnection', 'wye-grounded' if xf else 'line')):
            if not br.get(k):
                br[k] = v
                assumed = True
        if br.get('vectorGroup') is None:
            br['vectorGroup'] = 'YNyn0' if xf else ''
            assumed = True
        for k, v in (('r2', _num(br.get('r'), 0)), ('x2', _num(br.get('x'), 0)),
                     ('r0', 3 * _num(br.get('r'), 0)), ('x0', 3 * _num(br.get('x'), 0)),
                     ('fromRn', 0.0), ('fromXn', 0.0), ('toRn', 0.0), ('toXn', 0.0)):
            if not math.isfinite(_num(br.get(k), float('nan'))):
                br[k] = v
                assumed = True
        if not br.get('z0mode'):
            br['z0mode'] = 'auto'
            assumed = True
        if mark_assumed and assumed:
            br['seqAssumed'] = True
    kv = {str(b['id']): _num(b.get('baseKV'), 1) for b in case.get('buses', [])}
    for g in case.get('generators', []):
        assumed = False
        defaults = dict(sourceType='synchronous-generator', ratedMVA=_num(g.get('Pmax'), 0) or case['baseMVA'],
                        r1=0.002, x1=0.20, r2=0.002, x2=0.20, r0=0.001, x0=0.05, rn=0.0, xn=0.0,
                        steadyFactor=0.70, ratedKV=kv.get(str(g.get('bus')), 1), cosPhiRated=0.8,
                        minCaseInclude='auto')
        for k, v in defaults.items():
            cur = g.get(k)
            if cur in (None, '') or (isinstance(v, (int, float)) and not math.isfinite(_num(cur, float('nan')))):
                g[k] = v
                assumed = True
        if not math.isfinite(_num(g.get('r1Min'), float('nan'))):
            g['r1Min'] = g['r1']
        if not math.isfinite(_num(g.get('x1Min'), float('nan'))):
            g['x1Min'] = g['x1']
        if mark_assumed and assumed:
            g['seqAssumed'] = True
    return case


def _grounded(conn) -> bool:
    return str(conn or '').lower() in ('wye-grounded', 'grounded-wye', 'yn', 'wye_grounded')


def zero_sequence_disposition(br: dict) -> dict:
    mode = br.get('z0mode', 'auto')
    if mode == 'connected':
        return dict(transfer=True, fromGround=False, toGround=False, label='Forced connected')
    if mode == 'blocked':
        return dict(transfer=False, fromGround=False, toGround=False, label='Forced blocked')
    if br.get('equipmentType') != 'transformer':
        return dict(transfer=True, fromGround=False, toGround=False, label='Line path')
    fg, tg = _grounded(br.get('fromConnection')), _grounded(br.get('toConnection'))
    label = ('Grounded-wye transfer' if fg and tg else 'From-side grounded path' if fg
             else 'To-side grounded path' if tg else 'Blocked by winding connections')
    return dict(transfer=fg and tg, fromGround=fg and not tg, toGround=tg and not fg, label=label)


def generator_correction_factor(g, bus, cmax):
    un = _num(bus.get('baseKV') if bus else 1, 1)
    ug = _num(g.get('ratedKV'), 0) or un
    cos = min(max(_num(g.get('cosPhiRated'), 0.8), 0), 1) or 0.8
    sin = math.sqrt(max(0.0, 1 - cos * cos))
    return (un / ug) * (cmax / (1 + abs(_num(g.get('x1'), 0)) * sin))


def transformer_correction_factor(br, base_mva, cmax):
    rated = _num(br.get('rate'), 0) or base_mva or 100
    x_own = abs(_num(br.get('x'), 0)) * (rated / (base_mva or 100))
    return 0.95 * (cmax / (1 + 0.6 * x_own))


def apply_case_configuration(case: dict, iec_case: str) -> dict:
    """IEC minimum case: drop motor/converter sources (unless overridden), use r1Min/x1Min."""
    if iec_case != 'min':
        return case
    c = copy.deepcopy(case)
    for g in c['generators']:
        inc = g.get('minCaseInclude', 'auto')
        included = True if inc == 'include' else False if inc == 'exclude' \
            else g.get('sourceType') not in ('motor', 'converter')
        if not included:
            g['status'] = 0
            continue
        r1b, x1b = _num(g.get('r1'), 0), _num(g.get('x1'), 0)
        if math.isfinite(_num(g.get('r1Min'), float('nan'))) and math.isfinite(_num(g.get('x1Min'), float('nan'))):
            r1, x1 = _num(g['r1Min'], r1b), _num(g['x1Min'], x1b)
            rr = r1 / r1b if r1b else 1
            rx = x1 / x1b if x1b else 1
            g.update(r1=r1, x1=x1, r2=_num(g.get('r2'), 0) * rr, x2=_num(g.get('x2'), 0) * rx)
    return c


def build_sequence_y(case, seq, r_factor=1.0, corr=None):
    buses = case['buses']
    n = len(buses)
    idx = {str(b['id']): i for i, b in enumerate(buses)}
    Y = np.zeros((n, n), complex)
    apply_corr = bool(corr and corr.get('enabled') and seq != 0)
    cmax = corr.get('cmax', 1.1) if corr else 1.1

    def series(i, j, z, tap=1.0):
        if abs(z) < 1e-12:
            return
        y = 1 / z
        Y[i, i] += y / tap ** 2
        Y[j, j] += y
        Y[i, j] -= y / tap
        Y[j, i] -= y / tap

    def shunt(i, z):
        if abs(z) > 1e-12:
            Y[i, i] += 1 / z

    for br in case['branches']:
        if not _num(br.get('status'), 1):
            continue
        i, j = idx.get(str(br['from'])), idx.get(str(br['to']))
        if i is None or j is None:
            continue
        key_r, key_x = {1: ('r', 'x'), 2: ('r2', 'x2'), 0: ('r0', 'x0')}[seq]
        rr, xx = _num(br.get(key_r), 0), _num(br.get(key_x), 0)
        if br.get('equipmentType') != 'transformer':
            rr *= r_factor      # conductor temperature correction: lines only
        z = complex(rr, xx)
        if apply_corr and br.get('equipmentType') == 'transformer':
            z *= transformer_correction_factor(br, case['baseMVA'], cmax)
        tap = _num(br.get('tap'), 1) or 1.0
        if seq != 0:
            series(i, j, z, tap)
            continue
        d = zero_sequence_disposition(br)
        if d['transfer']:
            zn = complex(3 * (_num(br.get('fromRn'), 0) + _num(br.get('toRn'), 0)),
                         3 * (_num(br.get('fromXn'), 0) + _num(br.get('toXn'), 0)))
            series(i, j, z + zn, tap)
        if d['fromGround']:
            shunt(i, z + complex(3 * _num(br.get('fromRn'), 0), 3 * _num(br.get('fromXn'), 0)))
        if d['toGround']:
            shunt(j, z + complex(3 * _num(br.get('toRn'), 0), 3 * _num(br.get('toXn'), 0)))

    for g in case['generators']:
        if not _num(g.get('status'), 1):
            continue
        i = idx.get(str(g['bus']))
        if i is None:
            continue
        if seq == 1:
            z = complex(_num(g.get('r1'), 0), _num(g.get('x1'), 0.2))
        elif seq == 2:
            z = complex(_num(g.get('r2'), 0), _num(g.get('x2'), 0.2))
        else:
            z = complex(_num(g.get('r0'), 0) + 3 * _num(g.get('rn'), 0),
                        _num(g.get('x0'), 0.05) + 3 * _num(g.get('xn'), 0))
        if apply_corr:
            bus = next((b for b in buses if str(b['id']) == str(g['bus'])), None)
            z *= generator_correction_factor(g, bus, cmax)
        shunt(i, z)
    return Y, idx


def _inv(Y, label):
    try:
        if np.linalg.cond(Y) > 1e14:
            raise np.linalg.LinAlgError
        return np.linalg.inv(Y)
    except np.linalg.LinAlgError:
        raise ValueError(f'{label} sequence network is singular. Check source impedances, '
                         'grounding paths, and islands.') from None


def _asym_rms(xr, cycles):
    if xr <= 0:
        return 1.0
    return math.sqrt(1 + 2 * math.exp(-4 * math.pi * max(0.0, cycles) / xr))


# -------------------------------------------------------------- study

def run_fault_study(case: dict, *, standard='iec', iec_case='max', corrections=True, fault_type='3ph',
                    fault_bus='all', Rf=0.0, Xf=0.0, prefault='standard', cmax=1.10, cmin=0.95,
                    theta_end=80.0, break_time=0.10, thermal_time=1.0, mom_cycles=0.5, int_cycles=5.0,
                    hz=50.0, breaker_ka=40.0, loadflow_vm: dict | None = None) -> dict:
    model = ensure_fault_data(copy.deepcopy(case), mark_assumed=False)
    r_factor = (1 + 0.004 * (theta_end - 20)) if (standard == 'iec' and iec_case == 'min') else 1.0
    cfactor = cmin if iec_case == 'min' else cmax
    corr_on = standard == 'iec' and corrections
    corr = dict(enabled=True, cmax=cmax) if corr_on else None
    eff = apply_case_configuration(model, iec_case) if standard == 'iec' else model

    Y1, idx = build_sequence_y(eff, 1, r_factor, corr)
    Y2, _ = build_sequence_y(eff, 2, r_factor, corr)
    Y0, _ = build_sequence_y(eff, 0, r_factor)
    Z1, Z2 = _inv(Y1, 'Positive'), _inv(Y2, 'Negative')
    notes = []
    try:
        Z0 = _inv(Y0, 'Zero')
    except ValueError:
        # Buses with no zero-sequence path to ground (e.g. behind a delta winding):
        # a ground fault there draws ~no current. Model that with a negligible
        # admittance to ground instead of failing like GridScope does.
        Z0 = np.linalg.inv(Y0 + 1e-9 * np.eye(len(Y0)))
        floating = [b['id'] for k, b in enumerate(eff['buses']) if abs(Z0[k, k]) > 1e6]
        notes.append('No zero-sequence path to ground at bus(es) ' + ', '.join(map(str, floating)) +
                     ': ground-fault current there is ~0 (ungrounded / delta-fed).')
    sf = [_num(g.get('steadyFactor'), 0.7) for g in eff['generators'] if _num(g.get('status'), 1)]
    steady_factor = sum(sf) / len(sf) if sf else 0.7
    base = eff['baseMVA']

    targets = eff['buses'] if fault_bus == 'all' else [b for b in eff['buses'] if str(b['id']) == str(fault_bus)]
    rows, contrib = [], []
    for bus in targets:
        i = idx[str(bus['id'])]
        z1, z2, z0 = Z1[i, i], Z2[i, i], Z0[i, i]
        kv = _num(bus['baseKV'], 1)
        base_i = base / (math.sqrt(3) * kv)
        zf = complex(Rf, Xf) / (kv * kv / base)
        if prefault == 'loadflow' and loadflow_vm:
            v = loadflow_vm.get(str(bus['id']), 1.0)
        elif prefault == 'standard':
            v = cfactor if standard == 'iec' else 1.0
        else:
            v = 1.0
        i0 = i1 = i2 = 0j
        if fault_type == '3ph':
            i1 = v / (z1 + zf)
            phase = abs(i1)
        elif fault_type == 'slg':
            i1 = i2 = i0 = v / (z1 + z2 + z0 + 3 * zf)
            phase = abs(3 * i1)
        elif fault_type == 'll':
            i1 = v / (z1 + z2 + zf)
            i2 = -i1
            phase = max(abs(x) for x in _abc(i0, i1, i2))
        else:
            den = z2 + z0 + 3 * zf
            i1 = v / (z1 + z2 * (z0 + 3 * zf) / den)
            i2 = -i1 * (z0 + 3 * zf) / den
            i0 = -i1 * z2 / den
            phase = max(abs(x) for x in _abc(i0, i1, i2))
        ik = phase * base_i
        R, X = abs(z1.real), abs(z1.imag)
        xr = X / R if R > 1e-9 else 999.0
        kappa = 1.02 + 0.98 * math.exp(-3 * (R / X if X > 1e-9 else 0))
        if standard == 'iec':
            peak = kappa * math.sqrt(2) * ik
            tau = xr / (2 * math.pi * hz)
            decay = math.exp(-break_time / max(tau, 1e-6))
            ib = ik * math.sqrt(0.5 + 0.5 * decay * decay)
            steady = ik * steady_factor
            ith = ik * math.sqrt(1 + min(1, 0.2 * xr) / max(thermal_time, 0.01))
        else:
            peak = ik * _asym_rms(xr, mom_cycles)
            ib = ik * _asym_rms(xr, int_cycles)
            steady = ik
            ith = ik
        duty = 100 * ib / breaker_ka if breaker_ka > 0 else 0.0
        rows.append(dict(bus=bus['id'], name=bus.get('name', bus['id']), baseKV=kv,
                         faultLabel=FAULT_LABELS[fault_type], Ik=ik, peak=peak, Ib=ib, Ith=ith, steady=steady,
                         MVA=math.sqrt(3) * kv * ik, xr=xr, kappa=kappa, dutyPct=duty,
                         Z1mag=abs(z1), Z2mag=abs(z2), Z0mag=abs(z0)))
        # positive-sequence source contributions: I_g = y_g * Z1[g, f] * I1
        i1_ka = abs(i1) * base_i
        for g in eff['generators']:
            if not _num(g.get('status'), 1):
                continue
            gi = idx.get(str(g['bus']))
            if gi is None:
                continue
            zg = complex(_num(g.get('r1'), 0), _num(g.get('x1'), 0.2))
            if corr_on:
                gb = next((b for b in eff['buses'] if str(b['id']) == str(g['bus'])), None)
                zg *= generator_correction_factor(g, gb, cmax)
            share = Z1[gi, i] / zg
            contrib.append(dict(faultBus=bus.get('name', bus['id']), source=g['id'], sourceBus=g['bus'],
                                kA=abs(share) * i1_ka, pct=100 * abs(share)))

    cf = []
    if corr_on:
        for g in eff['generators']:
            if _num(g.get('status'), 1):
                bus = next((b for b in eff['buses'] if str(b['id']) == str(g['bus'])), None)
                cf.append(dict(equipment=g['id'], type='Generator (K_G)', where=str(g['bus']),
                               factor=generator_correction_factor(g, bus, cmax)))
        for br in eff['branches']:
            if _num(br.get('status'), 1) and br.get('equipmentType') == 'transformer':
                cf.append(dict(equipment=br['id'], type='Transformer (K_T)', where=f'{br["from"]} → {br["to"]}',
                               factor=transformer_correction_factor(br, base, cmax)))
    res = pd.DataFrame(rows)
    return dict(results=res, contributions=pd.DataFrame(contrib), corrections=pd.DataFrame(cf),
                notes=notes, max_Ik=float(res['Ik'].max()), max_peak=float(res['peak'].max()), max_MVA=float(res['MVA'].max()),
                assumed=_assumed_count(case))


def _abc(i0, i1, i2):
    return (i0 + i1 + i2, i0 + A * A * i1 + A * i2, i0 + A * i1 + A * A * i2)


def _assumed_count(case):
    c = ensure_fault_data(copy.deepcopy(case), mark_assumed=True)
    nb = sum(1 for b in c['branches'] if b.get('seqAssumed'))
    ng = sum(1 for g in c['generators'] if g.get('seqAssumed'))
    return dict(branches=nb, generators=ng)
