"""Free composition of synchronous-machine controls, converter-interfaced sources and load models."""
import re

import pytest

from gridscope import case as C
from gridscope import custom_models as CM
from gridscope import dynamics as D


def _wscc(source_at=None, source=None):
    c = C.load_builtin('IEEE 9-Bus')
    bench, _ = D.benchmark_machine_data(c)
    for g in c['generators']:
        g['dyn'] = dict(bench[g['id']])
        if source and str(g['bus']) == source_at:
            g['dyn']['source'] = source
    return c


# ------------------------------------------------------------------ catalogue / XML

def test_every_composable_class_is_wired_to_the_machine():
    for role in ('governor', 'avr', 'pss'):
        classes = CM.composable(role)
        assert len(classes) >= {'governor': 10, 'avr': 30, 'pss': 9}[role]
        for cls in classes.values():
            comp = D.component(cls)
            gen_vars = {g for _, g in comp['connect']}
            if role == 'governor':
                assert 'PmPu.value' in gen_vars and 'omegaPu.value' in gen_vars
            elif role == 'avr':
                assert 'efdPu.value' in gen_vars and 'UStatorPu.value' in gen_vars
            else:
                assert comp['pssOut']


def test_composed_model_definition():
    name, custom = CM.resolve('Round rotor (4 windings)', 'GAST (gas turbine)', 'ST1A (IEEE)', 'PSS1A (speed input)')
    assert custom and name == 'GsSync4W_Gast_St1a_Pss1aOmega'
    xml = CM.model_xml(name)
    assert 'GeneratorSynchronousExt4W_INIT' in xml
    assert 'VoltageRegulators.Standard.St15c_INIT' in xml                      # ST1A initialisation
    assert '<dyn:connect id1="voltageRegulator" var1="UPssPu" id2="powerSystemStabilizer" var2="VPssPu"/>' in xml
    assert '<dyn:connect id1="governor" var1="PmPu" id2="generator" var2="PmPu.value"/>' in xml
    assert len(re.findall(r'initConnect id1="voltageRegulator"', xml)) == 3       # Efd0, Us0, Ir0
    # a shipped combination is used as is
    assert CM.resolve('Round rotor (4 windings)', 'TGOV1 (steam)', 'SEXS (simplified exciter)', 'PSS2A (IEEE)') == \
        ('GeneratorSynchronousFourWindingsTGov1SexsPss2a', False)


def test_composed_model_name_round_trips():
    D.CONTROL_LIB['models'].pop('GsSync3W_HyGov_Dc1a_None', None)
    e = CM.entry_from_name('GsSync3W_HyGov_Dc1a_None')
    assert e['machine'].startswith('Salient') and e['pss'] == 'None' and e['custom']
    assert D.known_model('GsSync3W_HyGov_Dc1a_None')
    assert not D.known_model('GsSync4W_Nope_Dc1a_None')


def test_pss_without_avr_is_rejected():
    with pytest.raises(ValueError):
        CM.resolve('Round rotor (4 windings)', 'TGOV1 (steam)', 'None (constant field voltage)', 'PSS2A (IEEE)')


@pytest.mark.skipif(not (CM.is_compiled('GsSync4W_Gast_St1a_Pss1aOmega') or CM.toolchain_status()['ok']),
                    reason='needs the composed model compiled or a C++ tool chain')
def test_composed_model_runs_flat_and_rides_through_a_fault():
    name, _ = CM.resolve('Round rotor (4 windings)', 'GAST (gas turbine)', 'ST1A (IEEE)', 'PSS1A (speed input)')
    c = _wscc()
    for g in c['generators']:
        g['dyn']['model'] = name
    flat = D.simulate(c, [], stop_time=3, solver='SIM')
    assert flat.ok
    assert float((flat.pgen.iloc[-1] - flat.pgen.iloc[0]).abs().max()) < 0.5
    r = D.simulate(c, [D.Event('Bus fault', '7', 1.0, duration=0.08), D.Event('Trip branch', 'L6-7', 1.08)],
                   stop_time=5, solver='SIM')
    assert r.ok and r.stable and len(r.efd.columns) == 3


# ------------------------------------------------------------------ converter-interfaced sources

@pytest.mark.parametrize('model', ['BESScbWeccCurrentSource', 'PhotovoltaicsWeccCurrentSourceB',
                                   'WTG4AWeccCurrentSource', 'GridFormingConverterMatchingControl'])
def test_converter_models_start_from_the_load_flow_and_ride_through(model):
    c = _wscc('3', model)
    flat = D.simulate(c, [], stop_time=3, solver='SIM')
    assert flat.ok
    g3 = next(g for g in c['generators'] if str(g['bus']) == '3')['id']
    assert abs(flat.pgen[g3].iloc[0] - 85.0) < 0.5               # P from the load flow
    assert float((flat.pgen.iloc[-1] - flat.pgen.iloc[0]).abs().max()) < 0.5
    assert g3 not in flat.angles.columns                          # no rotor angle for a converter
    r = D.simulate(c, [D.Event('Bus fault', '8', 1.0, duration=0.1)], stop_time=4, solver='SIM')
    assert r.ok and r.stable


def test_verified_source_library_covers_pv_wind_and_storage():
    kinds = {v['kind'] for v in D.source_models().values()}
    assert {'Solar PV', 'Wind (type 4)', 'Battery storage', 'Grid-forming converter'} <= kinds


def test_missing_parameter_is_reported_not_crashed():
    c = _wscc('3', 'BESScbWeccCurrentSource')
    g3 = next(g for g in c['generators'] if str(g['bus']) == '3')
    g3['dyn']['sourceParams'] = {'BESScb_Kqv': ''}
    r = D.simulate(c, [], stop_time=1, solver='SIM')
    assert not r.ok and 'Kqv' in r.status_text


# ------------------------------------------------------------------ loads

def test_load_model_and_per_bus_override_reach_dynawo():
    c = _wscc()
    c['dynamicSettings'] = dict(loadModel='LoadAlphaBetaRestorative', loadModels={'5': 'LoadAlphaBeta'})
    p = D.prepare(c)
    try:
        assert sorted(p.load_models.values()) == ['LoadAlphaBeta', 'LoadAlphaBetaRestorative',
                                                 'LoadAlphaBetaRestorative']
        par = (p.work_dir / 'models.par').read_text()
        assert 'name="load_tFilter"' in par and 'id="LOAD_LoadAlphaBeta"' in par
        r = D.simulate(c, [D.Event('Bus fault', '7', 1.0, duration=0.08)], stop_time=4, solver='SIM', prepared=p)
        assert r.ok and r.stable
    finally:
        import shutil
        shutil.rmtree(p.work_dir, ignore_errors=True)
