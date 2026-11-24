"""
Compile and verify every composable governor / AVR / PSS class in a representative combination, and
record the outcome per class in dynawo_par/composition_verification.json (shown in the GUI).

  governor G  ->  machine + G + SEXS            (hydro governors on a salient-pole machine)
  AVR A       ->  round rotor + TGOV1 + A
  PSS P       ->  round rotor + TGOV1 + SEXS + P

Each composed model is checked like the pre-assembled ones (tools/verify_controller_library.py):
flat start at equilibrium, then fault + trip, trip and load step running to the end, on IEEE 9 and 14.

Usage:  python tools/verify_composition.py [--jobs 4] [--only Gast,St1a] [--resume]
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

from gridscope import custom_models as CM      # noqa: E402
from verify_controller_library import check    # noqa: E402

OUT = ROOT / 'dynawo_par' / 'composition_verification.json'
HYDRO = ('HyGov', 'GovHydro4', 'IEEEG2')
REF_GOV = 'Dynawo.Electrical.Controls.Machines.Governors.Standard.Steam.TGov1'
REF_AVR = 'Dynawo.Electrical.Controls.Machines.VoltageRegulators.Standard.SEXS'


def plan(only=None):
    out = []
    for role in ('governor', 'avr', 'pss'):
        for label, cls in CM.composable(role).items():
            short = cls.split('.')[-1]
            if only and short not in only:
                continue
            machine = 'Salient pole (3 windings)' if short in HYDRO else 'Round rotor (4 windings)'
            gov, avr, pss = {'governor': (cls, REF_AVR, None), 'avr': (REF_GOV, cls, None),
                             'pss': (REF_GOV, REF_AVR, cls)}[role]
            name = CM.model_id(machine, gov, avr, pss)
            CM.register_entry(name, CM.describe(machine, gov, avr, pss))
            out.append((cls, name))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--jobs', type=int, default=4)
    ap.add_argument('--only', default='')
    ap.add_argument('--resume', action='store_true', help='skip classes already recorded')
    a = ap.parse_args()
    only = set(filter(None, a.only.split(',')))
    todo = plan(only)
    if a.resume and OUT.exists():
        done = json.loads(OUT.read_text())
        todo = [(c, n) for c, n in todo if c not in done]
    names = sorted({n for _, n in todo})
    CM._shadow(CM.dynawo_home())
    t = time.time()
    chunks = [names[k::a.jobs] for k in range(a.jobs)]
    errors = {}
    with ThreadPoolExecutor(a.jobs) as ex:
        for res in ex.map(lambda ch: CM.compile_models(ch) if ch else {}, chunks):
            errors.update({k: v for k, v in res.items() if v})
    print(f'compiled {len(names)} models in {time.time() - t:.0f} s, {len(errors)} failed', flush=True)
    CM.activate()
    result = json.loads(OUT.read_text()) if OUT.exists() else {}
    for cls, name in todo:
        if name in errors:
            problems = [errors[name][:400]]
        else:
            problems = check(name)
        result[cls] = dict(model=name, verified=not problems,
                           details=problems or ['flat start + fault/trip/load-step on IEEE 9 and IEEE 14'])
        print(('OK   ' if not problems else 'FAIL ') + f'{cls.split(".")[-1]:12s} {name:40s} '
              + ('' if not problems else problems[0][:150]), flush=True)
        OUT.write_text(json.dumps(result, indent=1))
    print(sum(v['verified'] for v in result.values()), 'of', len(result), 'classes verified')


if __name__ == '__main__':
    main()
