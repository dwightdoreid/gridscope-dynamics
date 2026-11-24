"""
GridScope case model: load / save / normalise / validate.

A case is a plain dict in the GridScope JSON format:
    {"name", "baseMVA", "buses": [...], "branches": [...], "generators": [...]}
so files exported by the original GridScope web app open unchanged, and
cases saved here open in GridScope.

Optional dynamic data added by this app (ignored by the original GridScope):
    generator["dyn"] = {"model", "H", "Xd", "Xpd", ...}   (see dynamics.py)
    case["dynamicSettings"] = {...}
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

CASES_DIR = Path(__file__).resolve().parent.parent / 'cases'

BUILT_IN_CASES = {
    'IEEE 9-Bus': 'IEEE_9-Bus.json',
    'IEEE 14-Bus': 'IEEE_14-Bus.json',
    'IEEE 14-Bus (mod)': 'IEEE_14-Bus_mod.json',
    'IEEE 39-Bus': 'IEEE_39-Bus.json',
    'IEEE 118-Bus (mod)': 'IEEE_118-Bus_mod.json',
}

BUS_TYPES = ('Slack', 'PV', 'PQ')

BUS_DEFAULTS = dict(type='PQ', baseKV=138.0, Pd=0.0, Qd=0.0, Gs=0.0, Bs=0.0, Vm=1.0, Va=0.0)
BRANCH_DEFAULTS = dict(r=0.0, x=0.1, b=0.0, tap=1.0, shift=0.0, rate=100.0, status=1)
GEN_DEFAULTS = dict(Pg=0.0, Qg=0.0, Qmax=9999.0, Qmin=-9999.0, Vg=1.0, Pmax=9999.0, Pmin=0.0, status=1)
# cost coefficients are NOT filled on load: like GridScope, DC-OPF applies its defaults at run time and says so


# ------------------------------------------------------------------- I/O

def load_builtin(name: str) -> dict:
    return load_file(CASES_DIR / BUILT_IN_CASES[name])


def load_file(path) -> dict:
    return normalise(json.loads(Path(path).read_text()))


def load_json_text(text: str) -> dict:
    return normalise(json.loads(text))


def to_json(case: dict) -> str:
    return json.dumps(case, indent=1)


def blank_case(name='New Network') -> dict:
    return normalise({'name': name, 'baseMVA': 100, 'buses': [
        {'id': 1, 'name': 'Bus 1', 'type': 'Slack', 'baseKV': 138, 'Pd': 0, 'Qd': 0, 'Gs': 0, 'Bs': 0,
         'Vm': 1.0, 'Va': 0, 'x': 400, 'y': 300}], 'branches': [], 'generators': []})


def _num(v, default):
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def normalise(case: dict) -> dict:
    """Fill defaults so every solver can rely on the fields being present."""
    c = copy.deepcopy(case)
    c.setdefault('name', 'Unnamed case')
    c['baseMVA'] = _num(c.get('baseMVA'), 100.0)
    c.setdefault('buses', [])
    c.setdefault('branches', [])
    c.setdefault('generators', [])
    kv = {}
    for i, b in enumerate(c['buses']):
        for k, v in BUS_DEFAULTS.items():
            if b.get(k) in (None, ''):
                b[k] = v
        b.setdefault('name', f'Bus {b.get("id")}')
        b.setdefault('x', 100 + 60 * (i % 12))
        b.setdefault('y', 100 + 80 * (i // 12))
        kv[str(b['id'])] = _num(b.get('baseKV'), 0)
    for br in c['branches']:
        for k, v in BRANCH_DEFAULTS.items():
            if br.get(k) in (None, ''):
                br[k] = v
        if not _num(br.get('tap'), 0):
            br['tap'] = 1.0  # MATPOWER convention: tap 0 means 1.0
        if 'equipmentType' not in br or br['equipmentType'] not in ('line', 'transformer'):
            br['equipmentType'] = infer_equipment_type(br, kv)
    for g in c['generators']:
        for k, v in GEN_DEFAULTS.items():
            if g.get(k) in (None, ''):
                g[k] = v
    return c


def infer_equipment_type(br: dict, kv: dict) -> str:
    kf, kt = kv.get(str(br.get('from'))), kv.get(str(br.get('to')))
    if abs(_num(br.get('tap'), 1) - 1) > 1e-9 or abs(_num(br.get('shift'), 0)) > 1e-9:
        return 'transformer'
    if kf and kt and abs(kf - kt) > 1e-6:
        return 'transformer'
    return 'line'


def is_transformer(br: dict, case: dict | None = None) -> bool:
    if br.get('equipmentType') == 'transformer':
        return True
    if case is not None:
        kv = {str(b['id']): _num(b['baseKV'], 0) for b in case['buses']}
        return infer_equipment_type(br, kv) == 'transformer'
    return False


# ------------------------------------------------------------- summary

def summary(case: dict) -> dict:
    return {
        'Base MVA': case['baseMVA'],
        'Buses': len(case['buses']),
        'Branches': len(case['branches']),
        'Generators': len(case['generators']),
        'Total load': sum(_num(b['Pd'], 0) for b in case['buses']),
    }


# ---------------------------------------------------------- validation
# Same rules as GridScope's validateModel(), plus checks the dynamic study needs.

def validate(case: dict) -> dict:
    errors, warnings = [], []

    def err(scope, id_, field, msg):
        errors.append(dict(scope=scope, id=id_, field=field, message=msg))

    def warn(scope, id_, field, msg):
        warnings.append(dict(scope=scope, id=id_, field=field, message=msg))

    buses = case.get('buses') or []
    if not buses:
        err('model', None, None, 'Model has no buses.')
        return dict(errors=errors, warnings=warnings)
    if not _num(case.get('baseMVA'), 0) > 0:
        err('model', None, 'baseMVA', 'System base MVA must be a positive number.')

    seen = set()
    for b in buses:
        bid = b.get('id')
        if bid in (None, ''):
            err('bus', bid, 'id', 'A bus is missing an ID.')
        else:
            if str(bid) in seen:
                err('bus', bid, 'id', f'Duplicate bus ID "{bid}" is used by more than one bus.')
            seen.add(str(bid))
        if b.get('type') not in BUS_TYPES:
            err('bus', bid, 'type', f'Bus {bid}: unknown type "{b.get("type")}" (must be Slack, PV, or PQ).')
        for f in ('baseKV', 'Pd', 'Qd', 'Gs', 'Bs', 'Vm', 'Va'):
            v = b.get(f)
            if v not in (None, '') and not math.isfinite(_num(v, float('nan'))):
                err('bus', bid, f, f'Bus {bid}: field "{f}" is not a valid number.')
        if _num(b.get('Vm'), 1) <= 0:
            err('bus', bid, 'Vm', f'Bus {bid}: voltage setpoint must be greater than 0 pu.')
        if _num(b.get('baseKV'), 1) <= 0:
            err('bus', bid, 'baseKV', f'Bus {bid}: base kV must be greater than 0.')
        if _num(b.get('Pd'), 0) < 0:
            warn('bus', bid, 'Pd', f'Bus {bid}: negative real load — confirm this represents embedded generation.')

    slack = [b for b in buses if b.get('type') == 'Slack']
    if not slack:
        err('model', None, 'type', 'No Slack bus defined — exactly one is required to solve.')
    if len(slack) > 1:
        err('model', None, 'type', f'{len(slack)} Slack buses defined '
                                   f'({", ".join(str(b["id"]) for b in slack)}) — exactly one is required.')

    seen_br = set()
    for br in case.get('branches') or []:
        bid = br.get('id')
        if bid in seen_br:
            warn('branch', bid, 'id', f'Duplicate branch ID "{bid}".')
        seen_br.add(bid)
        f_ok, t_ok = str(br.get('from')) in seen, str(br.get('to')) in seen
        if not f_ok:
            err('branch', bid, 'from', f'Branch {bid}: "from" bus {br.get("from")} does not exist.')
        if not t_ok:
            err('branch', bid, 'to', f'Branch {bid}: "to" bus {br.get("to")} does not exist.')
        if f_ok and t_ok and str(br.get('from')) == str(br.get('to')):
            err('branch', bid, 'to', f'Branch {bid}: "from" and "to" are the same bus ({br.get("from")}).')
        for f in ('r', 'x', 'b', 'tap', 'shift', 'rate'):
            v = br.get(f)
            if v not in (None, '') and not math.isfinite(_num(v, float('nan'))):
                err('branch', bid, f, f'Branch {bid}: field "{f}" is not a valid number.')
        if _num(br.get('status'), 1):
            if abs(_num(br.get('r'), 0)) < 1e-12 and abs(_num(br.get('x'), 0)) < 1e-12:
                err('branch', bid, 'x', f'Branch {bid}: R and X are both ~0 — zero-impedance branches aren\'t '
                                        'supported (use a small X, e.g. 0.0001, for a closed tie).')
            if not _num(br.get('tap'), 1) > 0:
                err('branch', bid, 'tap', f'Branch {bid}: tap ratio must be greater than 0.')
        if not _num(br.get('rate'), 0) > 0:
            warn('branch', bid, 'rate', f'Branch {bid}: no thermal rating set — % loading can\'t be evaluated.')

    for g in case.get('generators') or []:
        gid = g.get('id')
        if str(g.get('bus')) not in seen:
            err('gen', gid, 'bus', f'Generator {gid}: bus {g.get("bus")} does not exist.')
        for f in ('Pg', 'Qg', 'Qmax', 'Qmin', 'Vg', 'Pmax', 'Pmin'):
            v = g.get(f)
            if v not in (None, '') and not math.isfinite(_num(v, float('nan'))):
                err('gen', gid, f, f'Generator {gid}: field "{f}" is not a valid number.')
        if _num(g.get('Qmax'), 0) < _num(g.get('Qmin'), 0):
            warn('gen', gid, 'Qmax', f'Generator {gid}: Qmax is less than Qmin.')

    # topology: every bus reachable from the slack over closed branches
    if len(slack) == 1 and len(seen) == len(buses):
        adj = {str(b['id']): [] for b in buses}
        for br in case.get('branches') or []:
            f, t = str(br.get('from')), str(br.get('to'))
            if _num(br.get('status'), 1) and f in adj and t in adj:
                adj[f].append(t)
                adj[t].append(f)
        start = str(slack[0]['id'])
        reach, stack = {start}, [start]
        while stack:
            u = stack.pop()
            for v in adj[u]:
                if v not in reach:
                    reach.add(v)
                    stack.append(v)
        unreachable = [str(b['id']) for b in buses if str(b['id']) not in reach]
        if unreachable:
            err('model', None, 'topology', f'{len(unreachable)} bus(es) are electrically islanded — no '
                                           f'closed-status path to the Slack bus: {", ".join(unreachable)}.')
    return dict(errors=errors, warnings=warnings)
