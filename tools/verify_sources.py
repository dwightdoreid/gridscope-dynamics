"""
Verify the non-synchronous generator models and the load models in dynawo_par/source_library.json and
record the outcome ("verified": true/false + details); the GUI offers verified models by default.

Generator models: on IEEE 9 (WSCC machine data) the generator at bus 3 and on IEEE 14 the generator at
bus 2 use the model; all others stay synchronous machines. Load models: every load uses the model.
A model is verified when, with the simplified fixed-step solver,
  1. a 5 s flat run stays at equilibrium: |dP| < 0.5 MW, |dV| < 1e-3 pu
  2. a 100 ms bolted bus fault cleared by a line trip, a line trip and a load step run to t = 6 s.

Usage:  python tools/verify_sources.py [model ...]
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gridscope import case as C          # noqa: E402
from gridscope import dynamics as D      # noqa: E402

LIB = ROOT / 'dynawo_par' / 'source_library.json'
HOST_BUS = {'IEEE 9-Bus': '3', 'IEEE 14-Bus': '2'}


def _case(name, source=None, load=None):
    c = C.load_builtin(name)
    bench, _ = D.benchmark_machine_data(c)
    for g in c['generators']:
        g['dyn'] = dict(bench.get(g['id'], {}))
        if source and str(g['bus']) == HOST_BUS[name]:
            g['dyn']['source'] = source
    if load:
        c['dynamicSettings'] = dict(c.get('dynamicSettings') or {}, loadModel=load)
    return c


def check(source=None, load=None):
    problems = []
    for name in HOST_BUS:
        c = _case(name, source, load)
        try:
            flat = D.simulate(c, [], stop_time=5, solver='SIM')
        except Exception as e:  # noqa: BLE001
            return [f'{name}: {e}']
        if not flat.ok or flat.metrics.get('end_time', 0) < 5 - 1e-6:
            problems.append(f'{name}: flat run failed ({(flat.status_text or flat.reason)[:200]})')
            continue
        dp = float((flat.pgen.iloc[-1] - flat.pgen.iloc[0]).abs().max())
        dv = float((flat.voltages.iloc[-1] - flat.voltages.iloc[0]).abs().max())
        if not (dp < 0.5 and dv < 1e-3):
            problems.append(f'{name}: not at equilibrium without a disturbance (dP {dp:.2f} MW, dV {dv:.4f} pu)')
            continue
        br = next(b for b in c['branches'] if b.get('equipmentType') == 'line')
        load_bus = next(str(b['id']) for b in c['buses'] if float(b['Pd']) > 0)
        for label, evs in (('fault + line trip', [D.Event('Bus fault', str(br['from']), 1.0, duration=0.1),
                                                  D.Event('Trip branch', br['id'], 1.1)]),
                           ('line trip', [D.Event('Trip branch', br['id'], 1.0)]),
                           ('load step', [D.Event('Load step (ΔP)', load_bus, 1.0, value=0.1)])):
            r = D.simulate(c, evs, stop_time=6, solver='SIM')
            if not r.ok or r.metrics.get('end_time', 0) < 6 - 1e-6:
                problems.append(f'{name}: {label} stopped at t = {r.metrics.get("end_time", 0):.2f} s '
                                f'({(r.status_text or r.reason)[:160]})')
    return problems


def main(only):
    lib = json.loads(LIB.read_text())
    for grp in ('sources', 'loads'):
        for model, info in lib[grp].items():
            if only and model not in only:
                continue
            problems = check(source=model) if grp == 'sources' else check(load=model)
            info['verified'] = not problems
            info['verification'] = problems or ['flat start + fault/trip/load-step on IEEE 9 and IEEE 14']
            print(f'{"OK  " if not problems else "FAIL"} {model:58s} {problems[0][:140] if problems else ""}',
                  flush=True)
            LIB.write_text(json.dumps(lib, indent=1))
    for grp in ('sources', 'loads'):
        n = sum(v.get('verified', False) for v in lib[grp].values())
        print(f'{grp}: {n} of {len(lib[grp])} verified')


if __name__ == '__main__':
    main(set(sys.argv[1:]))
