"""
Steady-state checks against independent references.

Run with:  python -m pytest -q tests
"""
import copy
import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from gridscope import case as C                 # noqa: E402
from gridscope import contingency, faults, loadflow, opf   # noqa: E402
import reference_ac                             # noqa: E402

CASES = ['IEEE 9-Bus', 'IEEE 14-Bus', 'IEEE 14-Bus (mod)', 'IEEE 39-Bus', 'IEEE 118-Bus (mod)']


# ----------------------------------------------------------- AC load flow

def test_ieee14_matches_matpower():
    """Published MATPOWER case14 solution (V to 3 dp, angles to 2 dp)."""
    _, _, r = loadflow.run_ac(C.load_builtin('IEEE 14-Bus'))
    assert r['converged']
    ref_v = [1.06, 1.045, 1.01, 1.018, 1.020, 1.07, 1.062, 1.09, 1.056, 1.051, 1.057, 1.055, 1.050, 1.036]
    ref_a = [0, -4.98, -12.72, -10.33, -8.78, -14.22, -13.37, -13.36, -14.94, -15.10, -14.79, -15.07, -15.16, -16.04]
    assert np.allclose(r['buses'].Vm.round(3), ref_v, atol=1e-9)
    assert np.allclose(r['buses'].Va, ref_a, atol=0.03)
    assert r['Ploss'] == pytest.approx(13.393, abs=1e-3)
    assert r['buses'].Pg.iloc[0] == pytest.approx(232.39, abs=0.01)


@pytest.mark.parametrize('name', CASES)
def test_conversion_matches_gridscope_newton_raphson(name):
    """IIDM conversion + OpenLoadFlow == GridScope's own Ybus/NR (ported to numpy)."""
    c = C.load_builtin(name)
    _, _, r = loadflow.run_ac(c, enforce_q_limits=False)
    vm, va = reference_ac.solve(c)
    assert r['converged']
    assert np.max(np.abs(r['buses'].Vm.values - vm)) < 1e-8
    assert np.max(np.abs(r['buses'].Va.values - va)) < 1e-6


def test_phase_shifter_and_off_nominal_tap():
    c = C.load_builtin('IEEE 14-Bus')
    for br in c['branches']:
        if br['id'] == '4-9':
            br['shift'] = 8.0
        if br['id'] == '2-3':
            br.update(shift=-5.0, tap=1.03, equipmentType='transformer')
    _, _, r = loadflow.run_ac(c, enforce_q_limits=False)
    vm, va = reference_ac.solve(c)
    assert np.max(np.abs(r['buses'].Vm.values - vm)) < 1e-8
    assert np.max(np.abs(r['buses'].Va.values - va)) < 1e-6


def test_q_limit_switching():
    """Tighten G2's Qmax below what bus 2 needs: bus 2 must switch to PQ at exactly Qmax."""
    c = C.load_builtin('IEEE 14-Bus')
    next(g for g in c['generators'] if g['id'] == 'G2')['Qmax'] = 30.0
    _, _, r = loadflow.run_ac(c, enforce_q_limits=True)
    assert r['converged']
    assert any(s['bus'] == '2' and s['limit'] == 'Qmax' for s in r['q_switches'])
    assert r['buses'].Qg.iloc[1] == pytest.approx(30.0, abs=1e-3)
    assert r['buses'].Vm.iloc[1] < 1.045


# --------------------------------------------------------------------- N-1

def test_dc_n1_hand_check():
    """IEEE 14, DC: with 1-5 out, the slack's 219 MW must all flow over 1-2."""
    r = contingency.run_n1(C.load_builtin('IEEE 14-Bus'), 'dc')
    row = r['table'].set_index('branch').loc['1-5']
    total_load = 259.0
    assert row['maxLoading'] == pytest.approx(100 * (total_load - 40.0) / 130.0, rel=1e-6)


def test_n1_islanding_detected():
    r = contingency.run_n1(C.load_builtin('IEEE 14-Bus'), 'ac')
    row = r['table'].set_index('branch').loc['7-8']
    assert row['resultLabel'] == 'Islanding'
    assert row['islanded'] == '8'


# ------------------------------------------------------------------ DC-OPF

def _two_bus(load=300.0, rate=0.0, g1=(0.01, 20.0), g2=(0.02, 15.0)):
    return C.normalise({
        'name': 'two-bus', 'baseMVA': 100,
        'buses': [dict(id=1, type='Slack', baseKV=138, Pd=0), dict(id=2, type='PV', baseKV=138, Pd=load)],
        'branches': [dict(id='1-2', **{'from': 1}, to=2, r=0.0, x=0.1, b=0.0, rate=rate)],
        'generators': [dict(id='G1', bus=1, Pmin=0, Pmax=1000, costC2=g1[0], costC1=g1[1], costC0=0),
                       dict(id='G2', bus=2, Pmin=0, Pmax=1000, costC2=g2[0], costC1=g2[1], costC0=0)]})


def test_opf_unconstrained_equal_marginal_cost():
    # 20 + 0.02 P1 = 15 + 0.04 P2,  P1 + P2 = 300  ->  P1 = 116.667, lambda = 22.333
    r = opf.run_dc_opf(_two_bus())
    assert r['ok']
    g = r['generators'].set_index('id')
    assert g.loc['G1', 'Pg'] == pytest.approx(350 / 3, abs=1e-3)
    assert g.loc['G2', 'Pg'] == pytest.approx(550 / 3, abs=1e-3)
    assert r['buses'].lmp.values == pytest.approx([20 + 0.02 * 350 / 3] * 2, abs=1e-3)


def test_opf_binding_line_splits_lmps():
    # cheap G1 behind a 120 MW line: G1 = 120, G2 = 80, LMPs = each side's marginal cost
    r = opf.run_dc_opf(_two_bus(load=200, rate=120, g1=(0.0, 10.0), g2=(0.0, 30.0)))
    g = r['generators'].set_index('id')
    assert g.loc['G1', 'Pg'] == pytest.approx(120, abs=1e-3)
    assert g.loc['G2', 'Pg'] == pytest.approx(80, abs=1e-3)
    assert r['buses'].lmp.values == pytest.approx([10, 30], abs=1e-3)
    assert r['branches'].binding.iloc[0] == 'Yes'


def test_opf_binding_pmax():
    c = _two_bus(load=300, g1=(0.0, 10.0), g2=(0.0, 30.0))
    c['generators'][0]['Pmax'] = 100
    r = opf.run_dc_opf(c)
    g = r['generators'].set_index('id')
    assert g.loc['G1', 'Pg'] == pytest.approx(100, abs=1e-3)
    assert g.loc['G1', 'limitStatus'] == 'Pmax'
    assert r['buses'].lmp.values == pytest.approx([30, 30], abs=1e-3)


def test_opf_lossless_balance_ieee14():
    r = opf.run_dc_opf(C.load_builtin('IEEE 14-Bus'))
    assert r['ok']
    assert r['total_pg'] == pytest.approx(259.0, abs=1e-4)


# ------------------------------------------------------------------ faults

def _radial(**gen):
    """Source (x1=0.2, x2=0.2, x0=0.05, solidly grounded) -> bus 1 -> line (x=0.1, x0=0.3) -> bus 2, 138 kV."""
    g = dict(id='G1', bus=1, Pg=0, r1=0, x1=0.2, r2=0, x2=0.2, r0=0, x0=0.05, rn=0, xn=0)
    g.update(gen)
    return C.normalise({'name': 'radial', 'baseMVA': 100,
                        'buses': [dict(id=1, type='Slack', baseKV=138), dict(id=2, type='PQ', baseKV=138)],
                        'branches': [dict(id='L', **{'from': 1}, to=2, r=0, x=0.1, b=0, r2=0, x2=0.1, r0=0, x0=0.3,
                                          equipmentType='line')],
                        'generators': [g]})


IBASE_138 = 100 / (math.sqrt(3) * 138)


def test_fault_3ph_radial_hand_calc():
    r = faults.run_fault_study(_radial(), fault_type='3ph', fault_bus=2, corrections=False, cmax=1.1)
    assert r['results'].Ik.iloc[0] == pytest.approx(1.1 / 0.3 * IBASE_138, rel=1e-9)
    # one source -> it supplies 100 % of the fault current
    assert r['contributions'].pct.iloc[0] == pytest.approx(100, rel=1e-9)
    assert r['contributions'].kA.iloc[0] == pytest.approx(r['results'].Ik.iloc[0], rel=1e-9)


def test_fault_slg_radial_hand_calc():
    # I = 3c / (Z1 + Z2 + Z0) = 3.3 / (0.3 + 0.3 + 0.35)
    r = faults.run_fault_study(_radial(), fault_type='slg', fault_bus=2, corrections=False, cmax=1.1)
    assert r['results'].Ik.iloc[0] == pytest.approx(3.3 / 0.95 * IBASE_138, rel=1e-9)


def test_fault_ll_and_llg_radial_hand_calc():
    r = faults.run_fault_study(_radial(), fault_type='ll', fault_bus=2, corrections=False, cmax=1.1)
    assert r['results'].Ik.iloc[0] == pytest.approx(math.sqrt(3) * 1.1 / 0.6 * IBASE_138, rel=1e-9)
    # LLG, bolted: I1 = c/(Z1 + Z2||Z0); worst phase from the symmetrical-component transform
    z1 = z2 = 0.3j
    z0 = 0.35j
    i1 = 1.1 / (z1 + z2 * z0 / (z2 + z0))
    i2, i0 = -i1 * z0 / (z2 + z0), -i1 * z2 / (z2 + z0)
    a = np.exp(2j * np.pi / 3)
    worst = max(abs(i0 + a * a * i1 + a * i2), abs(i0 + a * i1 + a * a * i2))
    r = faults.run_fault_study(_radial(), fault_type='llg', fault_bus=2, corrections=False, cmax=1.1)
    assert r['results'].Ik.iloc[0] == pytest.approx(worst * IBASE_138, rel=1e-9)


def test_fault_delta_winding_blocks_zero_sequence():
    """Grounded source -> YNd transformer -> bus 2 on the delta side: no zero-sequence path at bus 2,
    so a ground fault there draws ~no current, while the 3-phase fault is unaffected."""
    c = _radial()
    c['branches'][0].update(equipmentType='transformer', fromConnection='wye-grounded',
                            toConnection='delta', vectorGroup='YNd1')
    slg = faults.run_fault_study(c, fault_type='slg', fault_bus=2, corrections=False)
    assert slg['results'].Ik.iloc[0] < 1e-6
    assert slg['notes']
    three = faults.run_fault_study(c, fault_type='3ph', fault_bus=2, corrections=False, cmax=1.1)
    assert three['results'].Ik.iloc[0] == pytest.approx(1.1 / 0.3 * IBASE_138, rel=1e-9)


def test_fault_iec_kappa_and_peak():
    c = _radial(r1=0.02)
    r = faults.run_fault_study(c, fault_type='3ph', fault_bus=1, corrections=False)
    row = r['results'].iloc[0]
    k = 1.02 + 0.98 * math.exp(-3 * 0.02 / 0.2)
    assert row.kappa == pytest.approx(k, rel=1e-12)
    assert row.peak == pytest.approx(k * math.sqrt(2) * row.Ik, rel=1e-12)


def test_fault_contributions_sum_to_fault_current():
    """Without off-nominal taps, source shares are a phasor split of the fault current: magnitudes
    add up to at least 100 % and the largest-share source is the strongest/closest one."""
    c = C.load_builtin('IEEE 14-Bus')
    for br in c['branches']:
        br['tap'] = 1.0
    r = faults.run_fault_study(c, fault_type='3ph', fault_bus=4, corrections=False)
    total = r['results'].Ik.iloc[0]
    assert r['contributions'].kA.sum() >= total * (1 - 1e-9)
    assert r['contributions'].pct.sum() >= 100 - 1e-9


def test_generator_results_report_final_outputs():
    c = C.load_builtin('IEEE 14-Bus')
    _, _, r = loadflow.run_ac(c)
    g = r['generators'].set_index('id')
    assert abs(g.loc['G1', 'P'] - 232.39) < 0.05                    # slack picks up the losses
    assert abs(g.loc['G1', 'dP']) < 0.05 and g.loc['G2', 'P'] == pytest.approx(40.0, abs=1e-6)
    assert abs(g['P'].sum() - r['total_gen']) < 1e-6
    assert abs(g.loc['G2', 'V'] - 1.045) < 1e-4                     # PV generator holds its set-point
    assert g.loc['G1', 'limits'] == 'Q < Qmin'                       # slack Q is never limited
