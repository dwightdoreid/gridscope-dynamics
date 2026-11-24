"""
Check the dynamic-simulation set-up on this machine and run a few reference cases.

Usage:  python tools/diagnose.py [path/to/case.json]

Prints the Python / pypowsybl / Dynawo versions, the compiler tool chain, the state of the compiled
GridScope models, and the outcome (with Dynawo's own error messages) of:
  * IEEE 9-bus without protection,
  * IEEE 9-bus with distance protection on one line (fault + line trip),
  * the given case file, if any, with its own settings and saved events.
Send the output when reporting a problem.
"""
import json
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pypowsybl                                  # noqa: E402

from gridscope import case as C                   # noqa: E402
from gridscope import custom_models as CM         # noqa: E402
from gridscope import dynamics as D               # noqa: E402


def run(label, case, events, stop=3.0):
    t = time.time()
    try:
        r = D.simulate(case, events, stop_time=stop, solver=(case.get('dynamicSettings') or {}).get('solver', 'SIM'))
    except Exception as e:  # noqa: BLE001
        print(f'  {label}: EXCEPTION {type(e).__name__}: {e}')
        return
    print(f'  {label}: {r.status} in {time.time() - t:.0f} s — {r.reason}')
    if r.status_text:
        print(f'      Dynawo: {r.status_text[:1500]}')
    for n in r.notes:
        print(f'      note: {n}')
    if len(r.protection):
        print('      protection: ' + '; '.join(f'{x.time:.2f} s {x.element} {x.protection}' for x in r.protection.itertuples()))


def main():
    print('Python', platform.python_version(), platform.platform())
    print('pypowsybl', pypowsybl.__version__)
    home = CM.dynawo_home()
    print('Dynawo', home, '(found)' if home and (home / 'dynawo.sh').exists() else '(NOT FOUND)')
    tc = CM.toolchain_status()
    print('Tool chain:', 'ok' if tc['ok'] else '; '.join(tc['issues']))
    print('Model cache:', CM.CACHE)
    for n in CM.FIXED_MODELS:
        state = 'compiled' if CM.is_compiled(n) else ('FAILED: ' + CM.previous_failure(n)[:300]
                                                      if CM.previous_failure(n) else 'not compiled')
        print(f'  {n}: {state}')

    print('Runs:')
    c = C.load_builtin('IEEE 9-Bus')
    run('IEEE 9, no protection, fault + trip', c, [D.Event('Bus fault', '7', 1.0, duration=0.1),
                                                   D.Event('Trip branch', 'L6-7', 1.1)])
    c = C.load_builtin('IEEE 9-Bus')
    c['dynamicSettings'] = dict(protection={'lines': {'L4-5': {'distance': True}}})
    run('IEEE 9, distance on L4-5, fault + trip', c, [D.Event('Bus fault', '7', 1.0, duration=0.1),
                                                      D.Event('Trip branch', 'L6-7', 1.1)])
    if len(sys.argv) > 1:
        case = C.load_json_text(Path(sys.argv[1]).read_text())
        ds = case.get('dynamicSettings') or {}
        events = []
        for e in ds.get('events') or []:
            if e.get('On', True) and e.get('Type') and e.get('Element') is not None:
                dur, val = e.get('Duration (s)'), e.get('Value')
                events.append(D.Event(e['Type'], str(e['Element']), float(e.get('Start (s)') or 1.0),
                                      duration=0.1 if dur is None else float(dur),
                                      value=0.0 if val is None else float(val), side=e.get('Side') or ''))
        print('  case protection settings:', json.dumps(ds.get('protection') or {})[:600])
        run(f'{case["name"]} ({len(events)} events)', case, events, float(ds.get('stopTime', 10.0)))


if __name__ == '__main__':
    main()
