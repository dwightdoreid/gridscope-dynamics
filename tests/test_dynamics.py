"""
Dynamic-stability engine checks (need Dynawo configured in ~/.itools/config.yml).

Run with:  python -m pytest -q tests/test_dynamics.py
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gridscope import case as C       # noqa: E402
from gridscope import dynamics as D   # noqa: E402


def _wscc():
    c = C.load_builtin('IEEE 9-Bus')
    data, desc = D.benchmark_machine_data(c)
    assert desc
    for g in c['generators']:
        g['dyn'] = data[g['id']]
    return c


@pytest.mark.parametrize('name', ['IEEE 9-Bus', 'IEEE 14-Bus', 'IEEE 14-Bus (mod)', 'IEEE 39-Bus'])
def test_flat_run_stays_in_steady_state(name):
    """No disturbance: the converted network + load flow + generated machine data must be an equilibrium."""
    r = D.simulate(C.load_builtin(name), [], stop_time=5)
    assert r.ok and r.stable
    assert (r.voltages.iloc[-1] - r.voltages.iloc[0]).abs().max() < 1e-4
    assert (r.pgen.iloc[-1] - r.pgen.iloc[0]).abs().max() < 0.05      # MW


def test_initial_generator_output_matches_load_flow():
    r = D.simulate(C.load_builtin('IEEE 9-Bus'), [], stop_time=1)
    p0 = r.pgen.iloc[0]
    assert p0['G2'] == pytest.approx(163.0, abs=1e-3)
    assert p0['G3'] == pytest.approx(85.0, abs=1e-3)


def test_long_fault_loses_synchronism_short_fault_does_not():
    c = _wscc()
    ok = D.simulate(c, [D.Event('Bus fault', '7', 1.0, duration=0.1), D.Event('Trip branch', 'L7-8', 1.1)], 5)
    bad = D.simulate(c, [D.Event('Bus fault', '7', 1.0, duration=0.5), D.Event('Trip branch', 'L7-8', 1.5)], 5)
    assert ok.stable and ok.metrics['max_angle_sep'] < 180
    assert not bad.stable and bad.metrics['max_angle_sep'] > 180


def test_cct_bracket_is_confirmed_by_independent_runs():
    c = _wscc()
    res = D.critical_clearing_time(c, '7', 'L7-8', tol=0.01)
    lo, hi = res['low'], res['high']
    assert lo is not None and hi is not None and 0 < hi - lo <= 0.0101
    for tc, expect in ((lo, True), (hi, False)):
        r = D.simulate(c, [D.Event('Bus fault', '7', 1.0, duration=tc), D.Event('Trip branch', 'L7-8', 1.0 + tc)],
                       5 + tc)
        assert r.stable is expect


def test_runs_do_not_contaminate_each_other():
    """Dynawo writes its final state into the network it ran on; every run must start fresh."""
    c = _wscc()
    p = D.prepare(c)
    evs = [D.Event('Bus fault', '4', 1.0, duration=0.1), D.Event('Trip branch', 'L4-5', 1.1)]
    first = D.simulate(c, evs, 4, prepared=p)
    D.simulate(c, [D.Event('Trip branch', 'T1', 1.0)], 3, prepared=p)        # islands G1
    again = D.simulate(c, evs, 4, prepared=p)
    assert again.metrics['max_angle_sep'] == pytest.approx(first.metrics['max_angle_sep'], rel=1e-9)


def test_islanded_generator_is_excluded():
    r = D.simulate(_wscc(), [D.Event('Trip branch', 'T1', 1.0)], 4)
    assert r.islanded_buses == ['1'] and r.lost_gens == ['G1']
    assert r.stable


def test_screening_covers_every_branch():
    c = _wscc()
    ids = [b['id'] for b in c['branches']]
    df = D.screen_contingencies(c, ids, clearing_time=0.1)
    assert sorted(df['branch']) == sorted(ids)
    assert df['max_angle_sep'].notna().all()


# ---------------------------------------------------------------- controls library

def _with_model(model, controls=None, name='IEEE 9-Bus'):
    c = C.load_builtin(name)
    bench, _ = D.benchmark_machine_data(c)
    for g in c['generators']:
        d = dict(bench.get(g['id'], {}))
        d['model'] = model
        d['controls'] = dict(controls or {})
        if 'ThreeWindings' in model:
            d.pop('XpqPu', None)
            d.pop('Tpq0', None)
        g['dyn'] = d
    return c


def test_library_offers_only_verified_compiled_models():
    models = D.available_models()
    assert len(models) >= 10
    assert all(v.get('verified') for v in models.values())
    every = D.available_models(include_unverified=True)
    assert len(every) > len(models)


@pytest.mark.parametrize('model', ['GeneratorSynchronousFourWindingsTGov1SexsPss2a',
                                   'GeneratorSynchronousFourWindingsPmConstSt6cPss6c',
                                   'GeneratorSynchronousThreeWindingsGoverNordicVRNordic'])
def test_ieee_control_models_hold_equilibrium_and_ride_through_faults(model):
    c = _with_model(model)
    flat = D.simulate(c, [], stop_time=5)
    assert flat.ok and (flat.pgen.iloc[-1] - flat.pgen.iloc[0]).abs().max() < 0.5
    # trip L9-4, not L4-5: with L4-5 out bus 5 is radial and has no load-flow solution (it settles at 0.77 pu with
    # constant-impedance load, which the post-disturbance voltage check rightly calls unstable)
    r = D.simulate(c, [D.Event('Bus fault', '4', 1.0, duration=0.1), D.Event('Trip branch', 'L9-4', 1.1)], 6)
    assert r.ok and r.metrics['end_time'] >= 6 - 1e-6 and r.stable, r.reason


def test_parameter_overrides_and_bindings_reach_dynawo():
    import re
    c = _with_model('GeneratorSynchronousFourWindingsTGov1SexsPss2a', {'governor_R': '0.04'})
    p = D.prepare(c)
    txt = (p.work_dir / 'models.par').read_text()
    assert re.findall(r'name="governor_R" value="([^"]+)"', txt) == ['0.04'] * 3
    snom = sorted(float(v) for v in re.findall(r'name="pss_SNom" value="([^"]+)"', txt))
    assert snom == pytest.approx(sorted([247.5, 192.0, 128.0]))            # PSS rating = machine rating


def test_governor_droop_sets_steady_frequency_deviation():
    """Halving the TGOV1 droop R roughly halves the steady-state frequency drop after a load step."""
    m = 'GeneratorSynchronousFourWindingsTGov1SexsPss2a'
    df = {}
    for R in ('0.05', '0.025'):
        r = D.simulate(_with_model(m, {'governor_R': R}), [D.Event('Load step (ΔP)', '5', 1.0, value=0.3)], 40)
        df[R] = 50 - r.speed.iloc[-1].mean() * 50
    assert df['0.05'] > 0 and 0.35 < df['0.025'] / df['0.05'] < 0.65


def test_pss_adds_damping():
    swing = {}
    for m in ('GeneratorSynchronousFourWindingsVRKundur', 'GeneratorSynchronousFourWindingsVRKundurPssKundur'):
        r = D.simulate(_with_model(m), [D.Event('Bus fault', '4', 1.0, duration=0.1),
                                       D.Event('Trip branch', 'L4-5', 1.1)], 10)
        s = r.angles.max(axis=1) - r.angles.min(axis=1)
        late = s[s.index > 6]
        swing[m] = late.max() - late.min()
    no_pss, with_pss = swing.values()
    assert with_pss < 0.6 * no_pss
