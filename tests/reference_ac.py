"""
Independent reference AC load flow: a direct numpy port of GridScope's own
ybus() + Newton-Raphson (no reactive-limit enforcement). Used only by tests
to cross-check the IIDM conversion + OpenLoadFlow results.
"""
import numpy as np


def ybus(case):
    buses = case['buses']
    idx = {str(b['id']): i for i, b in enumerate(buses)}
    n = len(buses)
    Y = np.zeros((n, n), complex)
    for br in case['branches']:
        if not float(br.get('status', 1)):
            continue
        i, j = idx[str(br['from'])], idx[str(br['to'])]
        y = 1 / complex(float(br['r']), float(br['x']))
        bc = 1j * float(br['b']) / 2
        a = (float(br.get('tap') or 1)) * np.exp(1j * np.deg2rad(float(br.get('shift') or 0)))
        Y[i, i] += (y + bc) / (a * np.conj(a))
        Y[j, j] += y + bc
        Y[i, j] -= y / np.conj(a)
        Y[j, i] -= y / a
    for i, b in enumerate(buses):
        Y[i, i] += complex(float(b['Gs']), float(b['Bs'])) / case['baseMVA']
    return Y, idx


def solve(case, tol=1e-10, max_it=30):
    Y, idx = ybus(case)
    buses = case['buses']
    n = len(buses)
    Vm = np.array([float(b['Vm']) for b in buses])
    Va = np.zeros(n)
    P = np.zeros(n)
    Q = np.zeros(n)
    for g in case['generators']:
        if not float(g.get('status', 1)):
            continue
        i = idx[str(g['bus'])]
        P[i] += float(g['Pg'])
        Q[i] += float(g['Qg'])
        if buses[i]['type'] in ('PV', 'Slack'):
            Vm[i] = float(g.get('Vg') or Vm[i])
    P -= np.array([float(b['Pd']) for b in buses])
    Q -= np.array([float(b['Qd']) for b in buses])
    P /= case['baseMVA']
    Q /= case['baseMVA']
    types = [b['type'] for b in buses]
    pv = [i for i, t in enumerate(types) if t == 'PV']
    pq = [i for i, t in enumerate(types) if t == 'PQ']
    ang = pv + pq
    converged = False
    for _ in range(max_it):
        V = Vm * np.exp(1j * Va)
        S = V * np.conj(Y @ V)
        mis = np.r_[P[ang] - S.real[ang], Q[pq] - S.imag[pq]]
        if np.max(np.abs(mis)) < tol:
            converged = True
            break
        # numerical Jacobian is fine for a test reference
        x0 = np.r_[Va[ang], Vm[pq]]
        J = np.zeros((len(mis), len(x0)))
        h = 1e-7
        for k in range(len(x0)):
            Va2, Vm2 = Va.copy(), Vm.copy()
            if k < len(ang):
                Va2[ang[k]] += h
            else:
                Vm2[pq[k - len(ang)]] += h
            V2 = Vm2 * np.exp(1j * Va2)
            S2 = V2 * np.conj(Y @ V2)
            J[:, k] = (np.r_[S2.real[ang], S2.imag[pq]] - np.r_[S.real[ang], S.imag[pq]]) / h
        dx = np.linalg.solve(J, mis)
        Va[ang] += dx[:len(ang)]
        Vm[pq] += dx[len(ang):]
    if not converged:
        raise RuntimeError('reference load flow did not converge')
    return Vm, np.rad2deg(Va)
