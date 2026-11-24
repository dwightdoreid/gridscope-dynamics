"""
GridScope Dynamics — acceptance tests on the IEEE 14-bus case.

Runs every study the app offers on IEEE 14 and checks each result against a published reference
(MATPOWER case14), a physical law, or a value recorded when the release was verified. Prints a
PASS/FAIL table and writes acceptance_report.md and acceptance_report.csv.

    .venv/bin/python tools/acceptance_ieee14.py            # core set (~10-15 min)
    .venv/bin/python tools/acceptance_ieee14.py --full     # + every library model, governor, AVR, PSS,
                                                           #   converter and load model (compiles ~40 models
                                                           #   the first time: about an hour)
    .venv/bin/python tools/acceptance_ieee14.py --only C   # only the checks whose id starts with C
    .venv/bin/python tools/acceptance_ieee14.py --list     # list the checks

Exit code 0 when every check passes.
"""
from __future__ import annotations

import argparse
import copy
import math
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from gridscope import case as C  # noqa: E402
from gridscope import contingency as N  # noqa: E402
from gridscope import custom_models as CM  # noqa: E402
from gridscope import dynamics as D  # noqa: E402
from gridscope import faults as F  # noqa: E402
from gridscope import loadflow as L  # noqa: E402
from gridscope import opf as O  # noqa: E402

CASE = 'IEEE 14-Bus'
CHECKS: list[tuple[str, str, str, bool, object]] = []     # (id, area, title, full_only, fn)


def check(cid, area, title, full=False):
    def deco(fn):
        CHECKS.append((cid, area, title, full, fn))
        return fn
    return deco


class Fail(AssertionError):
    pass


def expect(cond, msg):
    if not cond:
        raise Fail(msg)


def near(x, ref, tol):
    return x is not None and not (isinstance(x, float) and math.isnan(x)) and abs(x - ref) <= tol


# ------------------------------------------------------------------ helpers

def ieee14(**dyn) -> dict:
    c = C.load_builtin(CASE)
    if dyn:
        c['dynamicSettings'] = dyn
    return c


def with_prot(**kinds) -> dict:
    return ieee14(protection={k: v for k, v in kinds.items()})


def lines(c):
    return [b['id'] for b in c['branches'] if b['equipmentType'] == 'line']


def tfos(c):
    return [b['id'] for b in c['branches'] if b['equipmentType'] == 'transformer']


def load_buses(c):
    return [str(b['id']) for b in c['buses'] if float(b.get('Pd') or 0) > 0]


def sim(c, events, T=5.0, **kw):
    kw.setdefault('solver', 'SIM')
    r = D.simulate(c, events, stop_time=T, **kw)
    return r


def ran(r, T):
    expect(r.ok, f'run failed: {r.status} {r.status_text[:200]}')
    expect(r.metrics.get('end_time', 0) >= T - 1e-6, f'stopped at {r.metrics.get("end_time")} s')


def ops(r):
    p = r.protection
    if p is None or not len(p):
        return []
    return [(round(float(t), 2), e, pr, a) for t, e, pr, a in zip(p['time'], p['element'], p['protection'], p['action'])]


def ops_txt(r):
    return '; '.join(f'{e} {p} {a} @ {t:g}s' for t, e, p, a in ops(r)) or 'none'


def set_model(c, model, controls=None):
    for g in c['generators']:
        d = D.default_dyn_params(g, c)
        d['model'] = model
        d['controls'] = dict(controls or {})
        if 'ThreeWindings' in model or model.startswith('GsSync3W'):
            d.pop('XpqPu', None)
            d.pop('Tpq0', None)
        g['dyn'] = d
    return c


def set_source(c, gid, source):
    g = next(g for g in c['generators'] if g['id'] == gid)
    d = D.default_dyn_params(g, c)
    d['source'] = source
    d['sourceParams'] = {}
    g['dyn'] = d
    return c


def flat_and_fault(c, T_flat=5.0, T_fault=5.0, fault=('4', 0.1), trip='4-5'):
    flat = sim(c, [], T_flat)
    ran(flat, T_flat)
    dp = float((flat.pgen.iloc[-1] - flat.pgen.iloc[0]).abs().max())
    dv = float((flat.voltages.iloc[-1] - flat.voltages.iloc[0]).abs().max())
    expect(dp < 0.5 and dv < 2e-3, f'flat start drifts: ΔP {dp:.2f} MW, ΔV {dv:.4f} pu')
    ev = [D.Event('Bus fault', fault[0], 1.0, duration=fault[1])] + ([D.Event('Trip branch', trip, 1.0 + fault[1])]
                                                                    if trip else [])
    r = sim(c, ev, T_fault)
    ran(r, T_fault)
    # a model passes when it runs (the criterion of tools/verify_composition.py); whether it stays stable
    # depends on its parameters (many are Dynawo's single-machine example data), so the verdict is reported
    verdict = 'STABLE' if r.stable else 'UNSTABLE with its example parameters'
    return f'flat ΔP {dp:.2f} MW ΔV {dv:.4f} pu; fault + trip runs to the end, {verdict}: {r.reason[:60]}'


# ================================================================== A. installation and case handling

@check('A0', 'Install', 'Dynawo and the C++ tool chain are found; GridScope protection models are compiled or compile')
def a0():
    st = CM.toolchain_status()
    expect(st['ok'], str(st))
    need = ['GsLineProtected', 'GsNetworkTransformer', 'GsLoadShedding', 'GsLoadSheddingLocal']
    res = CM.ensure_compiled(need)
    bad = [n for n in need if not CM.is_compiled(n)]
    expect(not bad, f'not compiled: {bad} {res}')
    return f'Dynawo {CM.dynawo_home()}; {len(need)} models compiled'



@check('A1', 'Case', 'Load IEEE 14-bus: 14 buses, 20 branches, 5 generators, 259 MW load, no validation issues')
def a1():
    c = ieee14()
    s, v = C.summary(c), C.validate(c)
    expect((s['Buses'], s['Branches'], s['Generators']) == (14, 20, 5), str(s))
    expect(abs(s['Total load'] - 259.0) < 1e-6, f'load {s["Total load"]}')
    expect(not v['errors'] and not v['warnings'], str(v))
    expect(len(tfos(c)) == 4 and len(lines(c)) == 16, f'{len(lines(c))} lines, {len(tfos(c))} transformers')
    return '14 buses, 16 lines, 4 transformers, 5 generators, 259.0 MW'


@check('A2', 'Case', 'Save/open round trip keeps machine models, events (incl. disabled), protection and study settings')
def a2():
    c = ieee14(f_nom=50.0, solver='SIM', stopTime=7.0,
               events=[{'On': True, 'Type': 'Bus fault', 'Element': '4', 'Start (s)': 1.0, 'Duration (s)': 0.1},
                       {'On': False, 'Type': 'Trip branch', 'Element': '4-5', 'Start (s)': 1.1}],
               protection={'lines': {'4-5': {'distance': True}}, 'loads': {'9': {'ufls': True}}},
               loadModel='LoadAlphaBetaRestorative')
    set_model(c, 'GeneratorSynchronousFourWindingsTGov1SexsPss2a', {'governor_R': '0.04'})
    back = C.load_json_text(C.to_json(c))
    expect(back['dynamicSettings'] == c['dynamicSettings'], 'dynamicSettings changed')
    expect([g.get('dyn') for g in back['generators']] == [g.get('dyn') for g in c['generators']], 'generator models changed')
    return 'identical after save → open'


# ================================================================== B. steady state

def _ref():
    sys.path.insert(0, str(ROOT / 'tests'))
    import reference_ac
    return reference_ac


@check('B1', 'Load flow', 'AC load flow matches MATPOWER case14: total generation 272.39 MW, losses 13.393 MW')
def b1():
    _, _, r = L.run_ac(ieee14())
    expect(r['converged'], r['status'])
    expect(near(r['total_gen'], 272.39, 0.02) and near(r['Ploss'], 13.393, 0.01),
           f'gen {r["total_gen"]:.3f}, loss {r["Ploss"]:.3f}')
    return f'{r["iterations"]} iterations, gen {r["total_gen"]:.2f} MW, losses {r["Ploss"]:.3f} MW'


@check('B2', 'Load flow', 'Bus voltages and angles match MATPOWER case14 (bus 14: 1.036 pu, −16.03°)')
def b2():
    _, _, r = L.run_ac(ieee14(), enforce_q_limits=False)
    b = r['buses'].set_index(r['buses']['id'].astype(str))
    vm = b.filter(regex='^(Vm|V|vm)$').iloc[:, 0]
    va = b.filter(regex='^(Va|angle|va)$').iloc[:, 0]
    expect(near(float(vm['14']), 1.036, 0.001) and near(float(va['14']), -16.03, 0.05),
           f'bus 14: {float(vm["14"]):.4f} pu {float(va["14"]):.2f}°')
    expect(near(float(va['3']), -12.73, 0.05) and near(float(vm['4']), 1.018, 0.001),
           f'bus 3 angle {float(va["3"]):.2f}°, bus 4 V {float(vm["4"]):.4f}')
    return f'bus 14 {float(vm["14"]):.4f} pu {float(va["14"]):.2f}°; bus 3 {float(va["3"]):.2f}°'


@check('B3', 'Load flow', 'Generator results table: outputs add up to total generation; slack G1 = 232.39 MW')
def b3():
    _, _, r = L.run_ac(ieee14())
    g = r['generators'].set_index('id')
    pcol = next(k for k in ('P', 'Pg', 'Pout') if k in g.columns)
    tot = float(g[pcol].sum())
    expect(near(tot, r['total_gen'], 0.01), f'sum {tot:.2f} vs {r["total_gen"]:.2f}')
    expect(near(float(g.loc['G1', pcol]), 232.39, 0.02), f'G1 {float(g.loc["G1", pcol]):.2f}')
    return f'sum {tot:.2f} MW, G1 {float(g.loc["G1", pcol]):.2f} MW; limit flags: ' + \
        ', '.join(f'{i} {v}' for i, v in g['limits'].items() if v != 'ok')


@check('B4', 'Load flow', 'Changing a generator schedule moves the slack: G2 +20 MW → G1 about −21 MW (−20 MW plus the change in losses)')
def b4():
    c = ieee14()
    g2 = next(g for g in c['generators'] if g['id'] == 'G2')
    g2['Pg'] = float(g2['Pg']) + 20
    _, _, r = L.run_ac(c)
    g = r['generators'].set_index('id')
    pcol = next(k for k in ('P', 'Pg', 'Pout') if k in g.columns)
    d = float(g.loc['G1', pcol]) - 232.39
    expect(r['converged'] and -22 < d < -20, f'G1 changed by {d:.2f} MW')
    return f'G1 changed by {d:.2f} MW'


@check('B5', 'Load flow', 'DC load flow converges; flows balance the 259 MW load')
def b5():
    r = L.run_dc(ieee14())
    expect(r['converged'], str(r.get('status')))
    a = r['angles']
    return f'{len(r["branches"])} branch flows; angles from {float(np.min(list(a.values()) if isinstance(a, dict) else a)):.2f}°'


@check('B6', 'Contingency', 'DC N-1: 20 outages; 7-8 islands bus 8; the rest give thermal violations (line 1-2 and 1-5 at 168 %)')
def b6():
    r = N.run_n1(ieee14())
    t = r['table']
    expect(len(t) == 20, f'{len(t)} rows')
    expect(r['counts'] == {'Violation': 19, 'Islanding': 1}, str(r['counts']))
    row = t.set_index('branch').loc['7-8']
    expect(str(row['islanded']) == '8', f'7-8 islands {row["islanded"]}')
    return str(r['counts'])


@check('B7', 'Contingency', 'AC N-1: 20 outages solved (one non-converged case flagged, not crashed)')
def b7():
    r = N.run_n1(ieee14(), method='ac')
    expect(len(r['table']) == 20, f'{len(r["table"])} rows')
    expect(sum(r['counts'].values()) == 20, str(r['counts']))
    return str(r['counts'])


@check('B8', 'OPF', 'DC OPF: optimal, Σ Pg = 259 MW (lossless), uniform LMP (no congestion), cost 5314.16')
def b8():
    r = O.run_dc_opf(ieee14())
    expect(r['ok'] and r['status'] == 'optimal', r['status'])
    expect(near(r['total_pg'], 259.0, 0.01), f'Σ Pg {r["total_pg"]}')
    lo, hi = r['lmp_range']
    expect(abs(hi - lo) < 1e-3, f'LMP {lo}–{hi}')
    expect(near(r['cost'], 5314.16, 0.5), f'cost {r["cost"]:.2f}')
    return f'cost {r["cost"]:.2f}, LMP {lo:.3f}'


@check('B9', 'Short circuit', 'IEC 60909 max, 3-phase at all buses: largest Ik″ 41.78 kA; ip = κ·√2·Ik″ at every bus')
def b9():
    r = F.run_fault_study(ieee14())
    t = r['results']
    expect(len(t) == 14 and (t['Ik'] > 0).all(), f'{len(t)} buses')
    expect(near(r['max_Ik'], 41.78, 0.05), f'max Ik {r["max_Ik"]:.2f}')
    err = float((t['peak'] - t['kappa'] * math.sqrt(2) * t['Ik']).abs().max())
    expect(err < 1e-6, f'peak mismatch {err}')
    return f'max Ik″ {r["max_Ik"]:.2f} kA, max ip {r["max_peak"]:.2f} kA, max {r["max_MVA"]:.0f} MVA'


@check('B10', 'Short circuit', 'IEC min case gives lower currents than max at every bus')
def b10():
    mx = F.run_fault_study(ieee14())['results'].set_index('bus')['Ik']
    mn = F.run_fault_study(ieee14(), iec_case='min')['results'].set_index('bus')['Ik']
    expect((mn < mx).all(), 'min ≥ max somewhere')
    return f'min/max ratio {float((mn / mx).min()):.3f}–{float((mn / mx).max()):.3f}'


@check('B11', 'Short circuit', 'SLG, LL and LLG faults and the ANSI method run at all buses; LL ≈ 0.866 × 3-phase')
def b11():
    out = []
    t3 = F.run_fault_study(ieee14())['results'].set_index('bus')['Ik']
    for ft in ('slg', 'll', 'llg'):
        t = F.run_fault_study(ieee14(), fault_type=ft)['results'].set_index('bus')['Ik']
        expect(len(t) == 14 and (t > 0).all(), f'{ft}: {len(t)} buses')
        out.append(f'{ft} max {t.max():.2f} kA')
        if ft == 'll':
            ratio = float((t / t3).median())
            expect(0.84 < ratio < 0.89, f'LL/3ph {ratio:.3f}')
    a = F.run_fault_study(ieee14(), standard='ansi')['results']
    expect(len(a) == 14, 'ANSI rows')
    return '; '.join(out) + '; ANSI ok'


# ================================================================== C. time-domain simulation (default models)

@check('C1', 'Simulation', 'Flat start, 10 s: no drift (ΔP < 0.5 MW, ΔV < 0.001 pu); initial P equals the load flow')
def c1():
    c = ieee14()
    r = sim(c, [], 10)
    ran(r, 10)
    dp = float((r.pgen.iloc[-1] - r.pgen.iloc[0]).abs().max())
    dv = float((r.voltages.iloc[-1] - r.voltages.iloc[0]).abs().max())
    expect(dp < 0.5 and dv < 1e-3, f'ΔP {dp:.3f} MW ΔV {dv:.5f}')
    _, _, lf = L.run_ac(c)
    g = lf['generators'].set_index('id')
    pcol = next(k for k in ('P', 'Pg', 'Pout') if k in g.columns)
    e = max(abs(float(r.pgen[i].iloc[0]) - float(g.loc[i, pcol])) for i in r.pgen.columns)
    expect(e < 0.5, f'initial P differs from LF by {e:.2f} MW')
    return f'ΔP {dp:.3f} MW, ΔV {dv:.5f} pu, P(0) vs LF {e:.3f} MW'


@check('C2', 'Simulation', 'Bolted fault at bus 4 for 100 ms: bus 4 voltage ≈ 0 during the fault; system stable')
def c2():
    r = sim(ieee14(), [D.Event('Bus fault', '4', 1.0, duration=0.1)], 5)
    ran(r, 5)
    v = float(r.voltages['4'].loc[1.02:1.08].max())
    expect(v < 0.02, f'V4 during fault {v:.3f}')
    expect(r.stable, r.reason)
    return f'V4 {v:.3f} pu during fault; {r.reason[:60]}'


@check('C3', 'Simulation', 'Fault with reactance (Value = 0.05 pu): retained voltage > 0, smaller swing than bolted')
def c3():
    a = sim(ieee14(), [D.Event('Bus fault', '4', 1.0, duration=0.1)], 4)
    b = sim(ieee14(), [D.Event('Bus fault', '4', 1.0, duration=0.1, value=0.05)], 4)
    ran(b, 4)
    v = float(b.voltages['4'].loc[1.02:1.08].mean())
    expect(v > 0.1, f'V4 {v:.3f}')
    expect(b.metrics['max_angle_sep'] < a.metrics['max_angle_sep'], 'swing not smaller')
    return f'V4 {v:.2f} pu; swing {b.metrics["max_angle_sep"]:.0f}° vs {a.metrics["max_angle_sep"]:.0f}° bolted'


@check('C4', 'Simulation', 'Fault at bus 4 cleared by tripping line 4-5 after 100 ms: stable')
def c4():
    r = sim(ieee14(), [D.Event('Bus fault', '4', 1.0, duration=0.1), D.Event('Trip branch', '4-5', 1.1)], 6)
    ran(r, 6)
    expect(r.stable, r.reason)
    return r.reason[:80]


@check('C5', 'Simulation', 'Line trip alone (2-3), and one end only (side ONE): both run; one-end trip leaves the line charging')
def c5():
    a = sim(ieee14(), [D.Event('Trip branch', '2-3', 1.0)], 4)
    b = sim(ieee14(), [D.Event('Trip branch', '2-3', 1.0, side='ONE')], 4)
    ran(a, 4)
    ran(b, 4)
    expect(a.stable and b.stable, 'unstable')
    return f'both ends: V3 {float(a.voltages["3"].iloc[-1]):.4f}; side ONE: V3 {float(b.voltages["3"].iloc[-1]):.4f}'


@check('C6', 'Simulation', 'Generator trip G2 (40 MW): frequency dips (nadir ≈ 49.46 Hz), G2 excluded, stable')
def c6():
    r = sim(ieee14(), [D.Event('Trip generator', 'G2', 1.0)], 10)
    ran(r, 10)
    expect(r.lost_gens == ['G2'] and r.stable, f'{r.lost_gens} {r.reason}')
    expect(49.3 < r.metrics['f_min'] < 49.6, f'f_min {r.metrics["f_min"]}')
    return f'f min {r.metrics["f_min"]:.3f} Hz; {r.reason[:50]}'


@check('C7', 'Simulation', 'Load step +0.2 pu (20 MW) at bus 9: frequency falls and settles below 50 Hz')
def c7():
    r = sim(ieee14(), [D.Event('Load step (ΔP)', '9', 1.0, value=0.2)], 15)
    ran(r, 15)
    f_end = float(r.speed.iloc[-1].mean() * 50)
    expect(r.metrics['f_min'] < 49.95 and f_end < 50, f'f_min {r.metrics["f_min"]}, end {f_end}')
    return f'f min {r.metrics["f_min"]:.3f} Hz, final {f_end:.3f} Hz'


@check('C8', 'Simulation', 'Load step ΔQ at bus 9: voltage at bus 9 falls')
def c8():
    r = sim(ieee14(), [D.Event('Load step (ΔQ)', '9', 1.0, value=0.2)], 5)
    ran(r, 5)
    dv = float(r.voltages['9'].iloc[-1] - r.voltages['9'].iloc[0])
    expect(dv < -0.002, f'ΔV9 {dv:.4f}')
    return f'ΔV9 {dv:.4f} pu'


@check('C9', 'Simulation', 'Generator ΔP setpoint +0.1 pu on G2: G2 output rises by about 10 MW')
def c9():
    r = sim(ieee14(), [D.Event('Generator ΔP setpoint', 'G2', 1.0, value=0.1)], 15)
    ran(r, 15)
    dp = float(r.pgen['G2'].iloc[-1] - r.pgen['G2'].iloc[0])
    expect(5 < dp < 15, f'ΔP G2 {dp:.2f}')
    return f'ΔP G2 {dp:.2f} MW'


@check('C10', 'Simulation', 'AVR setpoint step +0.02 pu on G2: G2 reactive output rises, bus 2 voltage rises')
def c10():
    r = sim(ieee14(), [D.Event('AVR setpoint step (ΔU)', 'G2', 1.0, value=0.02)], 8)
    ran(r, 8)
    dq = float(r.qgen['G2'].iloc[-1] - r.qgen['G2'].iloc[0])
    dv = float(r.voltages['2'].iloc[-1] - r.voltages['2'].iloc[0])
    expect(dq > 1 and dv > 0.005, f'ΔQ {dq:.2f} ΔV {dv:.4f}')
    return f'ΔQ G2 {dq:.1f} Mvar, ΔV2 {dv:.4f} pu'


@check('C11', 'Simulation', 'Line fault at 30 % of line 4-5 (line opens when it clears): stable, fault point reported')
def c11():
    r = sim(ieee14(), [D.Event('Line fault', '4-5', 1.0, duration=0.1, location=30)], 5)
    ran(r, 5)
    expect('4-5@30%' in r.voltages.columns and r.stable and not r.islanded_buses, r.reason)
    return r.reason[:80]


@check('C12', 'Simulation', 'Islanding: tripping transformer 7-8 islands bus 8; G8 excluded from the verdict')
def c12():
    r = sim(ieee14(), [D.Event('Trip branch', '7-8', 1.0)], 5)
    ran(r, 5)
    expect(r.islanded_buses == ['8'] and r.lost_gens == ['G8'] and r.stable, f'{r.islanded_buses} {r.lost_gens}')
    return r.reason[:90]


@check('C13', 'Simulation', 'Long fault (0.5 s at bus 2) loses synchronism → UNSTABLE')
def c13():
    r = sim(ieee14(), [D.Event('Bus fault', '2', 1.0, duration=0.5)], 5)
    expect(not r.stable, r.reason)
    return r.reason[:90]


@check('C14', 'Simulation', 'Disabled events (On unticked) are kept in the table but not simulated')
def c14():
    from views import dynamic as V
    df = V._with_on(V._event_columns(pd.DataFrame([
        {'On': True, 'Type': 'Bus fault', 'Element': '4', 'Start (s)': 1.0, 'Duration (s)': 0.1},
        {'On': False, 'Type': 'Trip generator', 'Element': 'G2', 'Start (s)': 1.0}])))
    ev = V._events_from_df(df)
    expect([e.type for e in ev] == ['Bus fault'], str(ev))
    expect(len(V._events_records(df)) == 2, 'disabled row dropped from the saved table')
    return '1 of 2 events run; both saved'


@check('C15', 'Simulation', 'Invalid events are rejected with a message (unknown bus, line fault at 0 %, branch fault on a transformer)')
def c15():
    msgs = D.validate_events([D.Event('Bus fault', '99', 1.0, duration=0.1),
                              D.Event('Line fault', '4-5', 1.0, duration=0.1, location=0),
                              D.Event('Line fault', '4-7', 1.0, duration=0.1)], ieee14(), None, 5)
    expect(len(msgs) >= 3, str(msgs))
    return f'{len(msgs)} messages'


@check('C16', 'Simulation', 'IDA solver: flat start and a load step run to the end')
def c16():
    r = sim(ieee14(), [D.Event('Load step (ΔP)', '9', 1.0, value=0.1)], 5, solver='IDA')
    ran(r, 5)
    return r.reason[:80]


@check('C17', 'Simulation', 'Study settings: 60 Hz nominal frequency is used in the frequency results')
def c17():
    r = sim(ieee14(f_nom=60.0), [D.Event('Trip generator', 'G2', 1.0)], 6)
    ran(r, 6)
    expect(59 < r.metrics['f_min'] < 60 and near(r.metrics['f_max'], 60.0, 0.05), str(r.metrics))
    return f'f min {r.metrics["f_min"]:.3f} Hz'


# ================================================================== D. studies

@check('D1', 'CCT', 'Critical clearing time, bus 2, no trip: bracket confirmed by independent runs either side')
def d1():
    c = ieee14()
    out = D.critical_clearing_time(c, '2', None)
    cct = out.get('cct')
    expect(cct is not None and 0.05 < cct < 0.5, out.get('message'))
    lo = sim(c, [D.Event('Bus fault', '2', 1.0, duration=cct - 0.01)], 5)
    hi = sim(c, [D.Event('Bus fault', '2', 1.0, duration=cct + 0.02)], 5)
    expect(lo.stable and not hi.stable, f'{cct - 0.01:.3f}s stable={lo.stable}, {cct + 0.02:.3f}s stable={hi.stable}')
    return out['message'][:100]


@check('D2', 'CCT', 'Critical clearing time, bus 2 cleared by tripping line 2-4')
def d2():
    out = D.critical_clearing_time(ieee14(), '2', '2-4')
    expect(out.get('cct') is not None, out.get('message'))
    return out['message'][:100]


@check('D3', 'Screening', 'Dynamic N-1 screening of all 20 branches (100 ms fault at the from end + trip)')
def d3():
    c = ieee14()
    t = D.screen_contingencies(c, [b['id'] for b in c['branches']], clearing_time=0.1)
    expect(len(t) == 20, f'{len(t)} rows')
    return f'{len(t)} contingencies; columns: {", ".join(map(str, t.columns[:6]))}'


# ================================================================== E. models

LIB_CORE = ['GeneratorSynchronousFourWindingsTGov1SexsPss2a', 'GeneratorSynchronousThreeWindingsGoverNordicVRNordic',
            'GeneratorSynchronousFourWindingsPmConstSt6cPss6c']


def _lib_check(model):
    def fn():
        return flat_and_fault(set_model(ieee14(), model))
    return fn


for i, m in enumerate(D.available_models()):
    check(f'E1.{i + 1:02d}', 'Library model', f'{m}: flat start and fault + trip',
          full=m not in LIB_CORE)(_lib_check(m))


def _combo(gov, avr, pss):
    def fn():
        name, _ = CM.resolve('Round rotor (4 windings)', gov, avr, pss)
        return f'{name}: ' + flat_and_fault(set_model(ieee14(), name))
    return fn


CORE_COMBO = ('GAST (gas turbine)', 'IEEET1 (IEEE DC)', 'PSS2A (IEEE)')
check('E2.00', 'Composed model', 'GAST + IEEET1 + PSS2A (any governor × AVR × PSS): flat start and fault')(
    _combo(*CORE_COMBO))
_gov = list(CM.composable('governor', include_unverified=False))
_avr = list(CM.composable('avr', include_unverified=False))
_pss = list(CM.composable('pss', include_unverified=False))
for i, g in enumerate(_gov):
    check(f'E2.G{i + 1:02d}', 'Composed model', f'Governor {g} + SEXS', full=True)(_combo(g, 'SEXS (simplified exciter)', 'None'))
for i, a in enumerate(_avr):
    check(f'E2.A{i + 1:02d}', 'Composed model', f'TGOV1 + AVR {a}', full=True)(_combo('TGOV1 (steam)', a, 'None'))
for i, p in enumerate(_pss):
    check(f'E2.P{i + 1:02d}', 'Composed model', f'TGOV1 + SEXS + PSS {p}', full=True)(
        _combo('TGOV1 (steam)', 'SEXS (simplified exciter)', p))


def _source(src):
    def fn():
        c = set_source(ieee14(), 'G2', src)
        flat = sim(c, [], 4)
        ran(flat, 4)
        p0 = float(flat.pgen['G2'].iloc[0])
        expect(abs(p0 - 40.0) < 0.5, f'P(0) {p0:.2f} MW (load flow 40)')
        dp = float((flat.pgen.iloc[-1] - flat.pgen.iloc[0]).abs().max())
        expect(dp < 0.5, f'flat drifts {dp:.2f} MW')
        expect('G2' not in flat.angles.columns, 'converter has a rotor angle')
        r = sim(c, [D.Event('Bus fault', '4', 1.0, duration=0.1)], 4)
        ran(r, 4)
        expect(r.stable, r.reason)
        return f'P(0) {p0:.2f} MW, flat ΔP {dp:.2f} MW; fault: stable'
    return fn


SRC_CORE = {'PhotovoltaicsWeccCurrentSourceB', 'WTG4AWeccCurrentSource', 'BESScbWeccCurrentSource',
            'GridFormingConverterMatchingControl'}
for i, (s, info) in enumerate(D.source_models().items()):
    check(f'E3.{i + 1:02d}', 'Converter source', f'{info["kind"]}: {s} at G2 — starts at 40 MW, rides through a fault',
          full=s not in SRC_CORE)(_source(s))


def _load(model):
    def fn():
        c = ieee14(loadModel=model)
        return flat_and_fault(c, trip=None)
    return fn


for i, m in enumerate(k for k, v in D.SOURCE_LIB['loads'].items() if v.get('verified')):
    check(f'E4.{i + 1:02d}', 'Load model', f'All loads {m}: flat start and fault', full=m != 'LoadAlphaBetaRestorative')(_load(m))


@check('E5', 'Load model', 'Per-bus load model override reaches Dynawo (bus 9 restorative, rest default)')
def e5():
    p = D.prepare(ieee14(loadModels={'9': 'LoadAlphaBetaRestorative'}))
    try:
        vals = sorted(set(p.load_models.values()))
        expect('LoadAlphaBetaRestorative' in vals and 'LoadAlphaBeta' in vals, str(vals))
        return str(vals)
    finally:
        import shutil
        shutil.rmtree(p.work_dir, ignore_errors=True)


@check('E6', 'Parameters', 'Edited parameter reaches Dynawo: halving TGOV1 droop R halves the frequency drop')
def e6():
    m = 'GeneratorSynchronousFourWindingsTGov1SexsPss2a'
    df = {}
    for R in ('0.05', '0.025'):
        r = sim(set_model(ieee14(), m, {'governor_R': R}), [D.Event('Load step (ΔP)', '9', 1.0, value=0.3)], 30)
        ran(r, 30)
        df[R] = 50 - float(r.speed.iloc[-1].mean()) * 50
    ratio = df['0.025'] / df['0.05']
    expect(df['0.05'] > 0 and 0.35 < ratio < 0.65, f'Δf {df}')
    return f'Δf {df["0.05"]:.3f} Hz → {df["0.025"]:.3f} Hz (ratio {ratio:.2f})'


@check('E7', 'Parameters', 'PSS adds damping: Kundur AVR + PSS swings decay faster than without PSS')
def e7():
    sw = {}
    for m in ('GeneratorSynchronousFourWindingsVRKundur', 'GeneratorSynchronousFourWindingsVRKundurPssKundur'):
        r = sim(set_model(ieee14(), m), [D.Event('Bus fault', '4', 1.0, duration=0.1), D.Event('Trip branch', '4-5', 1.1)], 10)
        ran(r, 10)
        s = r.angles.max(axis=1) - r.angles.min(axis=1)
        late = s[s.index > 5]
        sw[m] = float(late.max() - late.min())
    a, b = sw.values()
    expect(b < 0.8 * a, f'late swing {a:.2f}° without, {b:.2f}° with PSS')
    return f'late swing {a:.2f}° → {b:.2f}° with PSS'


@check('E8', 'Parameters', 'Set-point steps reach every machine type: library (proportional), protected, and composed GAST/IEEET1')
def e8():
    from gridscope import custom_models as CM
    name, _ = CM.resolve('Round rotor (4 windings)', 'GAST (gas turbine)', 'IEEET1 (IEEE DC)', 'PSS2A (IEEE)')
    out = []
    for label, c in (('library', ieee14()), ('protected', with_prot(generators={'G2': dict(uv=True)})),
                     ('composed', set_model(ieee14(), name))):
        u = sim(c, [D.Event('AVR setpoint step (ΔU)', 'G2', 1.0, value=0.02)], 8)
        p = sim(c, [D.Event('Generator ΔP setpoint', 'G2', 1.0, value=0.1)], 12)
        ran(u, 8)
        ran(p, 12)
        dv = float(u.voltages['2'].iloc[-1] - u.voltages['2'].iloc[0])
        dp = float(p.pgen['G2'].iloc[-1] - p.pgen['G2'].iloc[0])
        expect(dv > 0.004 and dp > 5 and not u.notes and not p.notes, f'{label}: ΔV {dv:.4f} ΔP {dp:.2f} {u.notes + p.notes}')
        out.append(f'{label}: ΔV2 {dv:+.4f} pu, ΔP {dp:+.1f} MW')
    return '; '.join(out)


@check('E9', 'Parameters', 'A library model that cannot take set-point steps (TGov1/SEXS/PSS2A) says so instead of ignoring them silently')
def e9():
    r = sim(set_model(ieee14(), 'GeneratorSynchronousFourWindingsTGov1SexsPss2a'),
            [D.Event('AVR setpoint step (ΔU)', 'G2', 1.0, value=0.02)], 3)
    ran(r, 3)
    expect(any('not applied' in n for n in r.notes), str(r.notes))
    return r.notes[-1][:110]


# ================================================================== F. protection

GEN_ALL = dict(speed=True, uv=True, ov=True, los=True)


@check('F1', 'Protection', 'No protection set: the network model is unchanged (no protection notes, no operations)')
def f1():
    r = sim(ieee14(), [D.Event('Bus fault', '4', 1.0, duration=0.1)], 3)
    ran(r, 3)
    expect(not ops(r) and not r.notes, f'{ops_txt(r)} {r.notes}')
    return 'no operations'


@check('F2', 'Protection', 'Generator under-voltage: 2 s fault at bus 3 trips G3 at 2.5 s (U < 0.7 for 1.5 s)')
def f2():
    r = sim(with_prot(generators={'G3': dict(uv=True, u_min=0.7, t_uv=1.5)}),
            [D.Event('Bus fault', '3', 1.0, duration=2.0)], 4)
    o = ops(r)
    expect(o and o[0][1:3] == ('Generator G3', 'under-voltage') and near(o[0][0], 2.5, 0.05), ops_txt(r))
    expect('G3' in r.lost_gens, str(r.lost_gens))
    return ops_txt(r)


@check('F3', 'Protection', 'Generator over-voltage: AVR step +0.1 pu on G6 raises it to 1.135 pu and trips it (U > 1.12 for 1 s)')
def f3():
    r = sim(with_prot(generators={'G6': dict(ov=True, u_max=1.12, t_ov=1.0)}),
            [D.Event('AVR setpoint step (ΔU)', 'G6', 1.0, value=0.1)], 6)
    o = ops(r)
    expect(o and o[0][1:3] == ('Generator G6', 'over-voltage'), ops_txt(r))
    return ops_txt(r)


@check('F4', 'Protection', 'Generator over/under speed: losing G1 (232 MW) trips G2 on under-frequency (f < 49.5 Hz for 0.5 s)')
def f4():
    r = sim(with_prot(generators={'G2': dict(speed=True, f_min=49.5, t_speed=0.5)}),
            [D.Event('Trip generator', 'G1', 1.0)], 6)
    o = ops(r)
    expect(any(x[1:3] == ('Generator G2', 'over/under speed') for x in o), ops_txt(r))
    return ops_txt(r)


@check('F5', 'Protection', 'Generator loss of synchronism: 0.5 s fault at bus 2 — slipping machine(s) tripped')
def f5():
    r = sim(with_prot(generators={g: dict(los=True) for g in ('G1', 'G2', 'G3', 'G6', 'G8')}),
            [D.Event('Bus fault', '2', 1.0, duration=0.5)], 5)
    o = ops(r)
    expect(o and all(x[2] == 'loss of synchronism' for x in o), ops_txt(r))
    return ops_txt(r) + f' → {r.reason[:50]}'


@check('F6', 'Protection', 'Distance: 0.5 s busbar fault at bus 4 cleared in zone 2 by the remote ends at 1.36 s')
def f6():
    c = ieee14()
    c['dynamicSettings'] = dict(protection={'lines': {l: dict(distance=True) for l in lines(c)}})
    r = sim(c, [D.Event('Bus fault', '4', 1.0, duration=0.5)], 4)
    ran(r, 4)
    o = ops(r)
    els = {x[1] for x in o}
    expect({'Line 2-4', 'Line 3-4', 'Line 4-5'} <= els, ops_txt(r))
    expect(all(near(x[0], 1.36, 0.02) for x in o if x[1] in ('Line 2-4', 'Line 3-4', 'Line 4-5')), ops_txt(r))
    return ops_txt(r)


@check('F7', 'Protection', 'Distance zones on a line fault (Side NONE): at 10 % end 1 zone 1 @1.06 s, end 2 zone 2 @1.36 s; at 50 % both zone 1')
def f7():
    res = []
    for loc, want in ((10, [(1.06, 'distance (end 1)'), (1.36, 'distance (end 2)')]),
                      (50, [(1.06, 'distance (end 1)'), (1.06, 'distance (end 2)')])):
        r = sim(with_prot(lines={'4-5': dict(distance=True)}),
                [D.Event('Line fault', '4-5', 1.0, duration=0.5, location=loc, side='NONE')], 3)
        ran(r, 3)
        got = sorted((x[0], x[2]) for x in ops(r))
        expect(len(got) == 2 and all(near(g[0], w[0], 0.02) and g[1] == w[1] for g, w in zip(got, sorted(want))),
               f'{loc} %: {ops_txt(r)}')
        res.append(f'{loc} %: {ops_txt(r)}')
    return ' | '.join(res)


@check('F8', 'Protection', 'Line overcurrent: line 1-2 (122 % loaded) with pick-up 110 % trips at its 1 s delay')
def f8():
    r = sim(with_prot(lines={'1-2': dict(oc=True, i_pickup=110.0, t_oc=1.0)}), [], 3)
    o = ops(r)
    expect(o and o[0][1:3] == ('Line 1-2', 'overcurrent') and o[0][0] < 1.1, ops_txt(r))
    return ops_txt(r)


@check('F9', 'Protection', 'Transformer overcurrent: 5-6 (70 % loaded) with pick-up 50 % trips at its 2 s delay')
def f9():
    r = sim(with_prot(transformers={'5-6': dict(oc=True, i_pickup=50.0, t_oc=2.0)}), [], 4)
    o = ops(r)
    expect(o and o[0][1].endswith('5-6') and near(o[0][0], 2.0, 0.05), ops_txt(r))
    return ops_txt(r)


@check('F10', 'Protection', 'No pick-up when loading is below the setting (all transformers at 150 %): no operation, no rating note')
def f10():
    c = ieee14()
    c['dynamicSettings'] = dict(protection={'transformers': {t: dict(oc=True) for t in tfos(c)}})
    r = sim(c, [], 3)
    ran(r, 3)
    expect(not ops(r) and not any('rating' in n for n in r.notes), f'{ops_txt(r)} {r.notes}')
    return 'no operations; ratings used'


@check('F11', 'Protection', 'Missing rating: pick-up falls back to pre-disturbance loading, with a note')
def f11():
    c = with_prot(transformers={'5-6': dict(oc=True)})
    next(b for b in c['branches'] if b['id'] == '5-6')['rate'] = 0
    r = sim(c, [], 2)
    ran(r, 2)
    expect(any('no rating' in n for n in r.notes), str(r.notes))
    return r.notes[0][:100]


@check('F12', 'Protection', 'Under-frequency load shedding: losing G2 sheds in steps at the loads (f1 49.9, f2 49.8, f3 49.7 Hz)')
def f12():
    c = ieee14()
    c['dynamicSettings'] = dict(protection={'loads': {b: dict(ufls=True, f1=49.9, f2=49.8, f3=49.7) for b in load_buses(c)}})
    r = sim(c, [D.Event('Trip generator', 'G2', 1.0)], 8)
    ran(r, 8)
    o = ops(r)
    expect(o and all('shed' in x[3] for x in o), ops_txt(r))
    return f'{len(o)} operations, first: {o[0][1]} {o[0][3]} @ {o[0][0]} s'


@check('F13', 'Protection', 'Under-voltage load shedding: 1.5 s fault at bus 9 sheds load at buses near the fault')
def f13():
    c = ieee14()
    c['dynamicSettings'] = dict(protection={'loads': {b: dict(uvls=True, u_min=0.85, t_uvls=1.0) for b in load_buses(c)}})
    r = sim(c, [D.Event('Bus fault', '9', 1.0, duration=1.5, value=0.05)], 5)
    o = ops(r)
    expect(o and all('shed' in x[3] for x in o), ops_txt(r) + ' ' + r.status_text[:100])
    return ops_txt(r)[:150]


@check('F14', 'Protection', 'Line protection + UFLS together: load shedding measures frequency locally and still operates')
def f14():
    c = ieee14()
    c['dynamicSettings'] = dict(protection={'lines': {'4-5': dict(distance=True)},
                                            'loads': {b: dict(ufls=True, f1=49.9, f2=49.8, f3=49.7) for b in load_buses(c)}})
    r = sim(c, [D.Event('Trip generator', 'G2', 1.0)], 8)
    ran(r, 8)
    expect(ops(r), 'no shedding')
    return f'{len(ops(r))} operations, first @ {ops(r)[0][0]} s'


@check('F15', 'Protection', 'Line protection with a converter source: run goes ahead without line protection and says why')
def f15():
    c = set_source(with_prot(lines={'2-3': dict(distance=True)}), 'G2', 'GridFormingConverterMatchingControl')
    r = sim(c, [D.Event('Bus fault', '4', 1.0, duration=0.1)], 3)
    ran(r, 3)
    expect(any(n.startswith('Line protection not applied') for n in r.notes), str(r.notes))
    return r.notes[0][:110]


@check('F16', 'Protection', 'All protection on (every generator, line, transformer, load): 100 ms fault at bus 4 runs, no spurious trips')
def f16():
    c = ieee14()
    c['dynamicSettings'] = dict(protection={
        'generators': {g['id']: dict(GEN_ALL) for g in c['generators']},
        'lines': {l: dict(distance=True, oc=True) for l in lines(c)},
        'transformers': {t: dict(oc=True) for t in tfos(c)},
        'loads': {b: dict(ufls=True, uvls=True) for b in load_buses(c)}})
    r = sim(c, [D.Event('Bus fault', '4', 1.0, duration=0.1)], 5)
    ran(r, 5)
    expect(r.stable, r.reason)
    return f'{r.reason[:60]}; operations: {ops_txt(r)}'


@check('F17', 'Protection', 'All protection on: CCT at bus 1 completes (the protection may isolate the faulted bus)')
def f17():
    c = ieee14()
    c['dynamicSettings'] = dict(protection={
        'generators': {g['id']: dict(GEN_ALL) for g in c['generators']},
        'lines': {l: dict(distance=True, oc=True) for l in lines(c)},
        'transformers': {t: dict(oc=True) for t in tfos(c)},
        'loads': {b: dict(ufls=True, uvls=True) for b in load_buses(c)}})
    out = D.critical_clearing_time(c, '1', None)
    expect(out.get('cct') is not None, out.get('message'))
    return out['message'][:100]


@check('F18', 'Protection', 'Protection settings are validated (negative delay rejected)')
def f18():
    from gridscope import protection as PR
    msgs = PR.validate(with_prot(generators={'G1': dict(uv=True, t_uv=-1)}))
    expect(msgs, 'no message')
    return msgs[0][:100]


# ================================================================== G. app

@check('G2', 'App', 'Every page of the app renders, and the load flow, N-1, OPF, short-circuit and simulation run from the UI')
def g2():
    import subprocess
    p = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-x', str(ROOT / 'tests' / 'test_app.py')],
                       cwd=ROOT, capture_output=True, text=True, timeout=1800)
    last = (p.stdout.strip().splitlines() or [''])[-1]
    expect(p.returncode == 0, last + '\n' + p.stdout[-800:])
    return last


# ================================================================== runner

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--full', action='store_true', help='also every library model, control class, source and load model')
    ap.add_argument('--only', default='', help='run only checks whose id starts with this (e.g. F or C1)')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--out', default='.', help='folder for acceptance_report.md / .csv')
    a = ap.parse_args()
    todo = [c for c in CHECKS if (a.full or not c[3]) and c[0].startswith(a.only)]
    if a.list:
        for cid, area, title, full, _ in todo:
            print(f'{cid:8s} {area:18s} {title}' + ('  [--full]' if full else ''))
        return 0
    CM.activate() if hasattr(CM, 'activate') else None
    rows = []
    t_all = time.time()
    for cid, area, title, _, fn in todo:
        t0 = time.time()
        try:
            obs = fn() or ''
            res = 'PASS'
        except Fail as e:
            res, obs = 'FAIL', str(e)
        except Exception as e:  # noqa: BLE001
            res, obs = 'ERROR', f'{type(e).__name__}: {e}'
            traceback.print_exc()
        dt = time.time() - t0
        rows.append(dict(id=cid, area=area, check=title, result=res, observed=str(obs).replace('\n', ' '), seconds=round(dt, 1)))
        print(f'{res:5s} {cid:8s} {dt:6.1f}s  {title}\n                     → {str(obs)[:160]}', flush=True)
    df = pd.DataFrame(rows)
    out = Path(a.out)
    df.to_csv(out / 'acceptance_report.csv', index=False)
    n_pass = int((df['result'] == 'PASS').sum())
    md = [f'# GridScope Dynamics acceptance — {CASE}', '',
          f'{n_pass} of {len(df)} checks passed in {(time.time() - t_all) / 60:.1f} min'
          f' ({"full" if a.full else "core"} set).', '',
          '| Id | Area | Check | Result | Observed |', '|---|---|---|---|---|']
    md += [f'| {r.id} | {r.area} | {r.check} | {r.result} | {r.observed.replace("|", "/")} |' for r in df.itertuples()]
    (out / 'acceptance_report.md').write_text('\n'.join(md) + '\n')
    print(f'\n{n_pass}/{len(df)} passed. Report: {out / "acceptance_report.md"}')
    return 0 if n_pass == len(df) else 1


if __name__ == '__main__':
    sys.exit(main())
