"""
DC optimal power flow — same formulation as GridScope's DC-OPF:

    min  sum_g  c2*Pg^2 + c1*Pg + c0                      ($/h)
    s.t. nodal balance (MW) at every bus, lossless B-theta network
         (tap ratio and phase shift included, as in GridScope's DC load flow)
         Pmin <= Pg <= Pmax
         |flow| <= rate   for branches with rate > 0
         theta_slack = 0

Solved as a convex QP with cvxpy (Clarabel). LMPs are the duals of the
nodal balance constraints, in $/MWh.
"""
from __future__ import annotations

import cvxpy as cp
import numpy as np
import pandas as pd

from .case import _num

DEFAULT_COST = dict(costC2=0.01, costC1=20.0, costC0=0.0)


def run_dc_opf(case: dict) -> dict:
    base = float(case['baseMVA'])
    buses = case['buses']
    idx = {str(b['id']): i for i, b in enumerate(buses)}
    n = len(buses)
    slack = next(i for i, b in enumerate(buses) if b['type'] == 'Slack')

    gens = [g for g in case['generators'] if _num(g.get('status'), 1) and str(g['bus']) in idx]
    brs = [br for br in case['branches'] if _num(br.get('status'), 1)
           and str(br['from']) in idx and str(br['to']) in idx]
    defaulted = []
    c2, c1, c0 = [], [], []
    for g in gens:
        vals = []
        for k in ('costC2', 'costC1', 'costC0'):
            v = g.get(k)
            if v in (None, ''):
                v = DEFAULT_COST[k]
                defaulted.append(g['id'])
            vals.append(_num(v, DEFAULT_COST[k]))
        c2.append(vals[0]); c1.append(vals[1]); c0.append(vals[2])
    c2, c1, c0 = map(np.array, (c2, c1, c0))
    pmin = np.array([_num(g['Pmin'], 0) for g in gens])
    pmax = np.array([_num(g['Pmax'], 1e4) for g in gens])

    # branch-bus incidence and susceptances (MW per rad)
    nb = len(brs)
    A = np.zeros((nb, n))
    bsus = np.zeros(nb)
    shift = np.zeros(nb)
    rate = np.zeros(nb)
    for k, br in enumerate(brs):
        i, j = idx[str(br['from'])], idx[str(br['to'])]
        A[k, i], A[k, j] = 1, -1
        x = _num(br['x'], 0)
        tap = _num(br.get('tap'), 1) or 1.0
        bsus[k] = base / (x * tap) if abs(x) > 1e-12 else 0.0
        shift[k] = np.deg2rad(_num(br.get('shift'), 0))
        rate[k] = _num(br.get('rate'), 0)

    Cg = np.zeros((n, len(gens)))
    for k, g in enumerate(gens):
        Cg[idx[str(g['bus'])], k] = 1
    pd_ = np.array([_num(b['Pd'], 0) for b in buses])

    Pg = cp.Variable(len(gens))
    th = cp.Variable(n)
    flow = cp.multiply(bsus, A @ th - shift)                     # MW, from -> to
    balance = Cg @ Pg - pd_ == A.T @ flow
    cons = [balance, th[slack] == 0, Pg >= pmin, Pg <= pmax]
    lim = np.where(rate > 0)[0]
    upper = lower = None
    if len(lim):
        upper = flow[lim] <= rate[lim]
        lower = flow[lim] >= -rate[lim]
        cons += [upper, lower]
    objective = cp.Minimize(cp.sum(cp.multiply(c2, cp.square(Pg))) + c1 @ Pg + c0.sum())
    prob = cp.Problem(objective, cons)
    try:
        prob.solve(solver=cp.CLARABEL)
    except cp.SolverError:
        prob.solve(solver=cp.OSQP, eps_abs=1e-9, eps_rel=1e-9, max_iter=200000)

    status = prob.status
    ok = status in (cp.OPTIMAL, cp.OPTIMAL_INACCURATE)
    out = dict(status=status, ok=ok, defaulted=sorted(set(defaulted)))
    if not ok:
        out['message'] = ('Infeasible: total Pmax cannot cover the load, or branch limits make the '
                          'dispatch impossible.' if status == cp.INFEASIBLE else f'Solver status: {status}')
        return out

    pg = Pg.value
    lmp = balance.dual_value       # $/MWh at each bus
    if np.nanmean(lmp) < 0:        # cvxpy dual sign depends on how the equality is written
        lmp = -lmp
    fl = bsus * (A @ th.value - shift)
    tol = 1e-4
    gen_rows = []
    for k, g in enumerate(gens):
        cost = c2[k] * pg[k] ** 2 + c1[k] * pg[k] + c0[k]
        at = 'Pmax' if pg[k] >= pmax[k] - tol * max(1, pmax[k]) else \
             'Pmin' if pg[k] <= pmin[k] + tol * max(1, abs(pmin[k])) else ''
        gen_rows.append(dict(id=g['id'], bus=g['bus'], Pg=pg[k], Pmin=pmin[k], Pmax=pmax[k],
                             cost=cost, limitStatus=at, marginal=2 * c2[k] * pg[k] + c1[k]))
    bus_rows = [dict(id=b['id'], name=b.get('name', b['id']), lmp=lmp[i],
                     theta=np.rad2deg(th.value[i]), Pd=pd_[i]) for i, b in enumerate(buses)]
    br_rows = []
    for k, br in enumerate(brs):
        loading = 100 * abs(fl[k]) / rate[k] if rate[k] else 0.0
        br_rows.append(dict(id=br['id'], **{'from': br['from']}, to=br['to'], flow=fl[k], rate=rate[k],
                            loading=loading, binding='Yes' if rate[k] and loading > 99.99 else ''))
    out.update(
        cost=float(prob.value), total_pg=float(pg.sum()),
        lmp_range=(float(np.min(lmp)), float(np.max(lmp))),
        generators=pd.DataFrame(gen_rows), buses=pd.DataFrame(bus_rows), branches=pd.DataFrame(br_rows))
    return out
