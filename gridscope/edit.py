"""Pure network edits, shared by the diagram editor (views/editor.py) and the MCP server.

apply_action(case, action) returns (new case or None if unchanged, status message, selection) and never
modifies its input, so it is unit-tested without a browser.
"""
from __future__ import annotations

import copy
import math

from gridscope import case as C


def _ids(case):
    return {str(b['id']): b['id'] for b in case['buses']}


def _next_bus_id(case):
    nums = [int(C._num(b['id'], 0)) for b in case['buses'] if str(b['id']).lstrip('-').isdigit()]
    return max(nums + [0]) + 1


def _unique(existing, base):
    if base not in existing:
        return base
    k = 2
    while f'{base}#{k}' in existing:
        k += 1
    return f'{base}#{k}'


def apply_action(case: dict, act: dict):
    """Apply one editor action. Returns (new case or None if unchanged, status message, selection)."""
    t = act.get('type')
    c = copy.deepcopy(case)
    ids = _ids(c)
    sel = None

    if t == 'select':
        kind, eid = act.get('kind'), act.get('id')
        return None, None, [kind, str(eid)] if kind else None

    if t == 'move':
        b = next((b for b in c['buses'] if str(b['id']) == str(act['id'])), None)
        x, y = float(act['x']), float(act['y'])
        if b is None or not (math.isfinite(x) and math.isfinite(y)):
            return None, None, None
        x, y = _tidy(x), _tidy(y)
        if C._num(b.get('x'), None) == x and C._num(b.get('y'), None) == y:
            return None, None, ['bus', str(b['id'])]
        b['x'], b['y'] = x, y
        return c, f'Moved bus {b["id"]}', ['bus', str(b['id'])]

    if t == 'add_bus':
        x, y = _tidy(float(act['x'])), _tidy(float(act['y']))
        new_id = _next_bus_id(c)
        near = min(c['buses'], key=lambda b: math.hypot(C._num(b.get('x'), 0) - x, C._num(b.get('y'), 0) - y),
                   default=None)
        kv = near['baseKV'] if near is not None else 138
        bus = dict(C.BUS_DEFAULTS, id=new_id, name=f'Bus {new_id}', baseKV=kv, x=x, y=y,
                   type='PQ' if c['buses'] else 'Slack')
        c['buses'].append(bus)
        return c, f'Added bus {new_id} ({kv} kV)', ['bus', str(new_id)]

    if t == 'add_branch':
        a, b = str(act.get('from')), str(act.get('to'))
        if a not in ids or b not in ids or a == b:
            return None, 'A branch needs two different existing buses.', None
        trafo = act.get('kind') == 'transformer'
        bid = _unique({str(br['id']) for br in c['branches']}, f'{a}-{b}')
        br = dict(C.BRANCH_DEFAULTS, id=bid, **{'from': ids[a]}, to=ids[b],
                  equipmentType='transformer' if trafo else 'line')
        if not trafo:
            br.update(r=0.01, x=0.1, b=0.02)
        c['branches'].append(br)
        kva, kvb = (C._num(x['baseKV'], 0) for x in c['buses'] if str(x['id']) in (a, b))
        msg = f'Added {"transformer" if trafo else "line"} {bid}'
        if not trafo and abs(kva - kvb) > 1e-6:
            msg += ' — note: the two buses have different base kV (a transformer may be intended)'
        return c, msg, ['branch', bid]

    if t == 'add_device':
        kind, bus_id = act.get('kind'), str(act.get('bus'))
        if bus_id not in ids:
            return None, None, None
        bus = next(b for b in c['buses'] if str(b['id']) == bus_id)
        if kind == 'gen':
            gid = _unique({str(g['id']) for g in c['generators']}, f'G{bus_id}')
            c['generators'].append(dict(C.GEN_DEFAULTS, id=gid, bus=ids[bus_id], Pg=50.0, Pmax=100.0,
                                        Qmax=50.0, Qmin=-50.0, Vg=C._num(bus.get('Vm'), 1.0)))
            msg = f'Added generator {gid} at bus {bus_id}'
            if bus['type'] == 'PQ':
                bus['type'] = 'PV'
                msg += ' (bus set to PV)'
            return c, msg, ['gen', gid]
        if kind == 'load':
            if C._num(bus.get('Pd'), 0) or C._num(bus.get('Qd'), 0):
                return None, f'Bus {bus_id} already has a load — edit it in the Selected Element panel.', \
                    ['bus', bus_id]
            bus['Pd'], bus['Qd'] = 10.0, 3.0
            return c, f'Added a 10 MW / 3 MVAr load at bus {bus_id}', ['bus', bus_id]
        if kind == 'shunt':
            if C._num(bus.get('Bs'), 0) or C._num(bus.get('Gs'), 0):
                return None, f'Bus {bus_id} already has a shunt — edit it in the Selected Element panel.', \
                    ['bus', bus_id]
            bus['Bs'] = 10.0
            return c, f'Added a 10 MVAr shunt capacitor at bus {bus_id}', ['bus', bus_id]
        return None, None, None

    if t == 'delete':
        kind, eid = act.get('kind'), str(act.get('id'))
        if kind == 'bus' and eid in ids:
            nb = len(c['branches'])
            ng = len(c['generators'])
            c['buses'] = [b for b in c['buses'] if str(b['id']) != eid]
            c['branches'] = [br for br in c['branches'] if eid not in (str(br['from']), str(br['to']))]
            c['generators'] = [g for g in c['generators'] if str(g['bus']) != eid]
            extra = [f'{n} {w}' for n, w in ((nb - len(c['branches']), 'branch(es)'),
                                            (ng - len(c['generators']), 'generator(s)')) if n]
            return c, f'Deleted bus {eid}' + (f' with {" and ".join(extra)}' if extra else ''), None
        if kind == 'branch':
            keep = [br for br in c['branches'] if str(br['id']) != eid]
            if len(keep) == len(c['branches']):
                return None, None, None
            c['branches'] = keep
            return c, f'Deleted branch {eid}', None
        if kind == 'gen':
            keep = [g for g in c['generators'] if str(g['id']) != eid]
            if len(keep) == len(c['generators']):
                return None, None, None
            c['generators'] = keep
            return c, f'Deleted generator {eid}', None
    return None, None, sel


def _tidy(v):
    v = round(v, 1)
    return int(v) if float(v).is_integer() else v
