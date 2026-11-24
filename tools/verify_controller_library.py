"""
Verify every machine/control combination in dynawo_par/controller_library.json with the local Dynawo
and record the outcome in the library ("verified": true/false + details). The GUI offers verified
combinations by default.

A combination is verified when, on IEEE 9 (WSCC machine data) AND IEEE 14 (typical data), with every
generator using it and the simplified fixed-step solver:
  1. a 5 s flat run (no disturbance) stays at equilibrium: |dP| < 0.5 MW, |dV| < 1e-3 pu
  2. a 100 ms bolted bus fault cleared by a line trip, a line trip alone and a load step each run to the
     end of the simulation.

Usage:  python tools/verify_controller_library.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gridscope import case as C          # noqa: E402
from gridscope import dynamics as D      # noqa: E402

LIB = ROOT / 'dynawo_par' / 'controller_library.json'


def _case(name, model):
    c = C.load_builtin(name)
    bench, _ = D.benchmark_machine_data(c)
    for g in c['generators']:
        d = dict(bench.get(g['id'], {}))
        d['model'] = model
        if D._winding(model) == 'three':
            d.pop('XpqPu', None)
            d.pop('Tpq0', None)
        g['dyn'] = d
    return c


def _line(c):
    return next(b for b in c['branches'] if b.get('equipmentType') == 'line')


def check(model):
    problems = []
    for name in ('IEEE 9-Bus', 'IEEE 14-Bus'):
        c = _case(name, model)
        flat = D.simulate(c, [], stop_time=5, solver='SIM')
        if not flat.ok or flat.metrics.get('end_time', 0) < 5 - 1e-6:
            problems.append(f'{name}: flat run failed ({flat.status_text or flat.reason})')
            continue
        dp = float((flat.pgen.iloc[-1] - flat.pgen.iloc[0]).abs().max())
        dv = float((flat.voltages.iloc[-1] - flat.voltages.iloc[0]).abs().max())
        if not (dp < 0.5 and dv < 1e-3):
            problems.append(f'{name}: not at equilibrium without a disturbance (dP {dp:.2f} MW, dV {dv:.4f} pu)')
            continue
        br = _line(c)
        load_bus = next(str(b['id']) for b in c['buses'] if float(b['Pd']) > 0)
        for label, evs in (('fault + line trip', [D.Event('Bus fault', str(br['from']), 1.0, duration=0.1),
                                                  D.Event('Trip branch', br['id'], 1.1)]),
                           ('line trip', [D.Event('Trip branch', br['id'], 1.0)]),
                           ('load step', [D.Event('Load step (ΔP)', load_bus, 1.0, value=0.1)])):
            r = D.simulate(c, evs, stop_time=6, solver='SIM')
            if not r.ok or r.metrics.get('end_time', 0) < 6 - 1e-6:
                problems.append(f'{name}: {label} stopped at t = {r.metrics.get("end_time", 0):.2f} s '
                                f'({r.status_text or r.reason})')
    return problems


def main():
    lib = json.loads(LIB.read_text())
    for k, (model, info) in enumerate(lib['models'].items(), 1):
        problems = check(model)
        info['verified'] = not problems
        info['verification'] = problems or ['flat start + fault/trip/load-step on IEEE 9 and IEEE 14']
        print(f'{k:2d}/{len(lib["models"])} {"OK  " if not problems else "FAIL"} {model}'
              + ('' if not problems else f'  — {problems[0][:110]}'), flush=True)
    LIB.write_text(json.dumps(lib, indent=1))
    n = sum(v['verified'] for v in lib['models'].values())
    print(f'{n} of {len(lib["models"])} combinations verified')


if __name__ == '__main__':
    main()
