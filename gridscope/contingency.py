"""N-1 branch-outage screening (DC: thermal only; AC: thermal + voltage), via
pypowsybl security analysis on OpenLoadFlow. Same summary columns as GridScope."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pypowsybl.security as sa

from .case import _num
from .convert import build_network
from .loadflow import lf_parameters

VMIN, VMAX = 0.95, 1.05   # pu — GridScope's voltage band


def run_n1(case: dict, method: str = 'dc', enforce_q_limits: bool = True,
           vmin: float = VMIN, vmax: float = VMAX):
    network, m = build_network(case)
    branch_ids = list(m.branch.values())
    rev = m.branch_rev
    rates = {br['id']: _num(br.get('rate'), 0) for br in case['branches']}

    analysis = sa.create_analysis()
    analysis.add_single_element_contingencies(branch_ids)
    analysis.add_monitored_elements(branch_ids=branch_ids, voltage_level_ids=list(m.bus_vl.values()))
    params = lf_parameters(m, enforce_q_limits=enforce_q_limits)
    res = analysis.run_dc(network, parameters=params) if method == 'dc' \
        else analysis.run_ac(network, parameters=params)

    br_res = res.branch_results.reset_index()
    bus_res = res.bus_results.reset_index() if method == 'ac' else None
    nominal = network.get_voltage_levels()['nominal_v']
    vl_to_bus = {v: k for k, v in m.bus_vl.items()}

    # islanding: which buses lose their path to the slack when a branch is out
    adjacency = _adjacency(case)
    slack = next(str(b['id']) for b in case['buses'] if b['type'] == 'Slack')

    rows = []
    for iid in branch_ids:
        cid = rev[iid]
        pc = res.post_contingency_results.get(iid)
        status = pc.status.name if pc is not None else 'NO_RESULT'
        islanded = _islanded(adjacency, slack, cid)
        row = dict(branch=cid, status=status, islanded=', '.join(islanded))
        if status != 'CONVERGED':
            label = 'Islanding' if islanded else 'Did not converge'
            rows.append(dict(row, resultLabel=label, maxLoading=float('nan'),
                             thermalCount=0, voltageCount=0, worst=f'Solver status: {status}'))
            continue
        flows = br_res[br_res['contingency_id'] == iid]
        thermal, max_load = [], 0.0
        for _, f in flows.iterrows():
            bid = rev.get(f['branch_id'])
            if bid is None or bid == cid:
                continue
            rate = rates.get(bid, 0)
            if not rate:
                continue
            if method == 'dc':
                s = abs(np.nan_to_num(f['p1']))
            else:
                s = max(math.hypot(np.nan_to_num(f['p1']), np.nan_to_num(f['q1'])),
                        math.hypot(np.nan_to_num(f['p2']), np.nan_to_num(f['q2'])))
            loading = 100 * s / rate
            max_load = max(max_load, loading)
            if loading > 100:
                thermal.append((loading, f'{bid} {loading:.0f}%'))
        volt = []
        if bus_res is not None:
            bb = bus_res[bus_res['contingency_id'] == iid]
            for _, b in bb.iterrows():
                vpu = b['v_mag'] / nominal[b['voltage_level_id']]
                if not np.isfinite(vpu):
                    continue
                if vpu < vmin or vpu > vmax:
                    volt.append((abs(vpu - 1), f'Bus {vl_to_bus[b["voltage_level_id"]]} {vpu:.3f} pu'))
        worst = [t for _, t in sorted(thermal, reverse=True)[:3]] + [t for _, t in sorted(volt, reverse=True)[:3]]
        if islanded:
            label = 'Islanding'
        elif thermal or volt:
            label = 'Violation'
        else:
            label = 'Secure'
        rows.append(dict(row, resultLabel=label, maxLoading=max_load, thermalCount=len(thermal),
                         voltageCount=len(volt), worst='; '.join(worst)))
    df = pd.DataFrame(rows)
    order = {'Did not converge': 0, 'Islanding': 1, 'Violation': 2, 'Secure': 3}
    df = df.sort_values(by=['resultLabel', 'maxLoading'], key=lambda s: s.map(order) if s.name == 'resultLabel' else -s.fillna(1e9),
                        ascending=[True, True]).reset_index(drop=True)
    return dict(method=method, table=df,
                counts=df['resultLabel'].value_counts().to_dict())


def _adjacency(case):
    adj = {}
    for br in case['branches']:
        if _num(br.get('status'), 1):
            adj.setdefault(br['id'], (str(br['from']), str(br['to'])))
    return adj


def _islanded(adj, slack, removed):
    nbrs = {}
    for bid, (f, t) in adj.items():
        if bid == removed:
            continue
        nbrs.setdefault(f, []).append(t)
        nbrs.setdefault(t, []).append(f)
    seen, stack = {slack}, [slack]
    while stack:
        u = stack.pop()
        for v in nbrs.get(u, []):
            if v not in seen:
                seen.add(v)
                stack.append(v)
    buses = {x for ft in adj.values() for x in ft}
    return sorted(buses - seen, key=lambda x: (len(x), x))
