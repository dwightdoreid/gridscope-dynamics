"""Protection models in dynamic simulations (IEEE 9-bus, WSCC machine data, 60 Hz)."""
import pytest

from gridscope import case as C
from gridscope import custom_models as CM
from gridscope import dynamics as D
from gridscope import protection as PR

needs_compiler = pytest.mark.skipif(
    not (CM.toolchain_status()['ok'] or all(CM.is_compiled(n) for n in ('GsLineProtected', 'GsLoadShedding'))),
    reason='protection models need a C++ tool chain to compile')


def _wscc(protection):
    c = C.load_builtin('IEEE 9-Bus')
    bench, _ = D.benchmark_machine_data(c)
    for g in c['generators']:
        g['dyn'] = dict(bench[g['id']])
    c['dynamicSettings'] = dict(f_nom=60.0, protection=protection)
    return c


def test_settings_defaults_and_validation():
    c = _wscc({'generators': {'G1': {'speed': True, 'f_min': 61.0}},
               'loads': {'5': {'ufls': True, 's1': 60.0, 's2': 30.0, 's3': 20.0}}})
    s = PR.settings(c)
    assert s['generators']['G1']['f_max'] == PR.GEN['f_max'] and PR.active(s['generators']['G1'], 'generators')
    msgs = ' '.join(PR.validate(c))
    assert 'bracket 60 Hz' in msgs and 'more than 100 %' in msgs


def test_no_protection_keeps_the_network_model():
    p = D.prepare(_wscc({}))
    assert not p.line_models and not any(p.protected.values())


@needs_compiler
def test_distance_protection_clears_a_bus_fault_in_zone_2():
    c = _wscc({'lines': {'L6-7': {'distance': True}, 'L7-8': {'distance': True}}})
    r = D.simulate(c, [D.Event('Bus fault', '7', 1.0, duration=0.5)], stop_time=3, solver='SIM')
    assert r.ok
    ops = r.protection.set_index('id')
    assert set(ops.index) == {'L6-7', 'L7-8'}
    # remote ends see the busbar fault at the full line length: zone 2 (0.3 s) + breaker (0.06 s)
    assert ops['time'].max() == pytest.approx(1.36, abs=0.02)
    assert ops.loc['L7-8', 'protection'].startswith('distance')


@needs_compiler
def test_line_overcurrent_uses_the_rating():
    c = _wscc({'lines': {'L7-8': {'oc': True, 'i_pickup': 8.0, 't_oc': 0.5}}})
    next(b for b in c['branches'] if b['id'] == 'L7-8')['rate'] = 250.0     # pick-up 20 MVA < ~26 MVA flow
    r = D.simulate(c, [], stop_time=2, solver='SIM')
    ops = r.protection
    assert r.ok and list(ops['id']) == ['L7-8'] and ops['protection'].iloc[0] == 'overcurrent'
    assert ops['time'].iloc[0] == pytest.approx(0.501, abs=0.02)


@needs_compiler
def test_generator_under_voltage_trip():
    c = _wscc({'generators': {'G3': {'uv': True, 'u_min': 0.7, 't_uv': 1.5}}})
    r = D.simulate(c, [D.Event('Bus fault', '9', 1.0, duration=2.0)], stop_time=4, solver='SIM')
    ops = r.protection
    assert list(ops['id']) == ['G3'] and ops['protection'].iloc[0] == 'under-voltage'
    assert ops['time'].iloc[0] == pytest.approx(2.5, abs=0.05) and 'G3' in r.lost_gens


@needs_compiler
def test_pole_slip_protection_trips_the_unstable_machine():
    c = _wscc({'generators': {g: {'los': True} for g in ('G1', 'G2', 'G3')}})
    r = D.simulate(c, [D.Event('Bus fault', '7', 1.0, duration=0.4)], stop_time=5, solver='SIM')
    assert list(r.protection['id']) == ['G3'] and r.protection['protection'].iloc[0] == 'loss of synchronism'
    assert r.stable          # the rest of the system holds once the slipping machine is off


def test_transformer_overcurrent_opens_and_islands_the_generator():
    c = _wscc({'transformers': {'T2': {'oc': True, 'i_pickup': 50.0, 't_oc': 1.0}}})
    r = D.simulate(c, [], stop_time=3, solver='SIM')
    ops = r.protection
    assert list(ops['id']) == ['T2'] and ops['time'].iloc[0] == pytest.approx(1.0, abs=0.02)
    assert 'G2' in r.lost_gens and '2' in r.islanded_buses
    assert not any('rating' in n for n in r.notes)          # 50 % of T2's 250 MVA rating < 163 MW


def test_overcurrent_without_rating_falls_back_to_the_loading():
    c = _wscc({'transformers': {'T2': {'oc': True, 'i_pickup': 150.0, 't_oc': 1.0}}})
    next(b for b in c['branches'] if b['id'] == 'T2')['rate'] = 0
    r = D.simulate(c, [], stop_time=2, solver='SIM')
    assert r.ok and r.protection.empty
    assert any('Transformer T2: no rating' in n and 'pre-disturbance loading' in n for n in r.notes)


def test_rated_branches_use_their_rating():
    c = C.load_builtin('IEEE 14-Bus')
    tfo = [b['id'] for b in c['branches'] if b['equipmentType'] == 'transformer']
    c['dynamicSettings'] = dict(protection={'transformers': {t: {'oc': True} for t in tfo}})
    p = D.prepare(c)
    assert len(p.protected['transformers']) == len(tfo) and not any('rating' in n for n in p.notes)
    par = (p.work_dir / 'models.par').read_text()
    rate = next(b['rate'] for b in c['branches'] if b['id'] == tfo[0])
    iid = p.net_map.branch[tfo[0]]
    vl = p.network.get_2_windings_transformers().loc[iid, 'voltage_level1_id']
    imax = 1.5 * rate * 1e3 / (3 ** 0.5 * p.network.get_voltage_levels().loc[vl, 'nominal_v'])
    block = par.split(f'<set id="OMS_{iid}">')[1].split('</set>')[0]
    assert float(block.split('currentLimitAutomaton_IMax" value="')[1].split('"')[0]) == pytest.approx(imax)


@needs_compiler
def test_ufls_sheds_in_steps_after_losing_a_generator():
    c = _wscc({'loads': {b: {'ufls': True, 'f1': 59.5, 'f2': 59.3, 'f3': 59.1} for b in ('5', '6', '8')}})
    r = D.simulate(c, [D.Event('Trip generator', 'G2', 1.0)], stop_time=8, solver='SIM')
    ops = r.protection
    assert r.ok and set(ops['id']) == {'5', '6', '8'}
    assert list(ops[ops['id'] == '5']['action']) == ['10 % shed', '20 % shed', '30 % shed']


@needs_compiler
def test_line_protection_with_shunts_ieee14():
    c = C.load_builtin('IEEE 14-Bus')
    c['dynamicSettings'] = dict(f_nom=60.0, protection={'lines': {'2-3': {'distance': True}}})
    r = D.simulate(c, [], stop_time=2, solver='SIM')
    assert r.ok and any('constant-impedance loads' in n for n in r.notes)
    assert float((r.voltages.iloc[-1] - r.voltages.iloc[0]).abs().max()) < 1e-3     # still a flat start


@needs_compiler
@pytest.mark.parametrize('events', [
    [D.Event('Bus fault', '7', 1.0, duration=0.1), D.Event('Trip branch', 'L6-7', 1.1)],
    [D.Event('Trip branch', 'T3', 1.0)],
    [D.Event('Trip branch', 'L8-9', 1.0, side='ONE')],
], ids=['fault+line trip', 'transformer trip', 'one-end trip'])
def test_switching_events_with_line_protection_match_the_network_model(events):
    """Line protection switches the network to Modelica; with no relay operating, results must not change."""
    out = {}
    for label, prot in (('network', {}), ('modelica', {'lines': {'L4-5': {'distance': True}}})):
        c = C.load_builtin('IEEE 9-Bus')
        c['dynamicSettings'] = dict(protection=prot)
        out[label] = D.simulate(c, events, stop_time=3, solver='SIM')
        assert out[label].ok, out[label].status_text
    a, b = out['network'], out['modelica']
    # an islanded bus is de-energised by the network model but stays energised by its own generator in
    # Modelica: compare the rest
    keep = [x for x in a.voltages.columns if x not in a.islanded_buses]
    assert float((a.voltages[keep].iloc[-1] - b.voltages[keep].iloc[-1]).abs().max()) < 1e-3
    assert a.metrics['max_angle_sep'] == pytest.approx(b.metrics['max_angle_sep'], abs=0.5)


@needs_compiler
def test_transformer_overcurrent_with_line_protection():
    c = _wscc({'lines': {'L4-5': {'distance': True}},
               'transformers': {'T2': {'oc': True, 'i_pickup': 50.0, 't_oc': 1.0}}})
    r = D.simulate(c, [], stop_time=3, solver='SIM')
    assert r.ok and list(r.protection['id']) == ['T2'] and 'G2' in r.lost_gens


@needs_compiler
def test_ufls_with_line_protection_measures_frequency_locally():
    """In an all-Modelica network pypowsybl gives loads no system frequency: GsLoadSheddingLocal is used."""
    loads = {b: {'ufls': True, 'f1': 59.5, 'f2': 59.3, 'f3': 59.1} for b in ('5', '6', '8')}
    out = {}
    for label, lines in (('network', {}), ('modelica', {'L4-5': {'distance': True}})):
        c = _wscc({'loads': loads, 'lines': lines})
        out[label] = D.simulate(c, [D.Event('Trip generator', 'G2', 1.0)], stop_time=8, solver='SIM')
        assert out[label].ok, out[label].status_text
    a, b = out['network'].protection, out['modelica'].protection
    assert list(a['action']) and sorted(a['action']) == sorted(b['action'])
    assert abs(a['time'].min() - b['time'].min()) < 0.5


@needs_compiler
def test_line_protection_is_skipped_with_converter_models():
    c = C.load_builtin('IEEE 14-Bus')
    c['generators'][1]['dyn'] = {'source': 'GridFormingConverterMatchingControl'}
    c['dynamicSettings'] = dict(protection={'lines': {'2-3': {'distance': True}}})
    r = D.simulate(c, [D.Event('Bus fault', '4', 1.0, duration=0.1)], stop_time=2, solver='SIM')
    assert r.ok, r.status_text
    assert any(n.startswith('Line protection not applied') and 'G2' in n for n in r.notes)


@needs_compiler
def test_metrics_when_protection_islands_the_only_recorded_bus():
    """CCT runs record only the faulted bus; distance relays can isolate it (used to raise in idxmin)."""
    c = C.load_builtin('IEEE 14-Bus')
    lines = {b['id']: {'distance': True} for b in c['branches'] if b['equipmentType'] == 'line'}
    c['dynamicSettings'] = dict(protection={'lines': lines})
    r = D.simulate(c, [D.Event('Bus fault', '1', 1.0, duration=0.4)], stop_time=3, record_buses=['1'], solver='SIM')
    assert '1' in r.islanded_buses and r.metrics['min_post_voltage_bus'] == ''


def test_line_fault_near_an_end_matches_a_bus_fault():
    c = C.load_builtin('IEEE 14-Bus')
    a = D.simulate(c, [D.Event('Line fault', '4-5', 1.0, duration=0.1, location=0.5)], stop_time=3, solver='SIM')
    b = D.simulate(c, [D.Event('Bus fault', '4', 1.0, duration=0.1), D.Event('Trip branch', '4-5', 1.1)],
                   stop_time=3, solver='SIM')
    assert a.ok and b.ok
    assert a.metrics['max_angle_sep'] == pytest.approx(b.metrics['max_angle_sep'], abs=2.0)
    assert '4-5@0.5%' in a.voltages.columns and not a.islanded_buses


def test_line_fault_validation():
    c = C.load_builtin('IEEE 14-Bus')
    msgs = ' '.join(D.validate_events([D.Event('Line fault', '4-5', 1.0, location=0.0),
                                       D.Event('Line fault', '4-7', 1.0),
                                       D.Event('Line fault', '2-3', 1.0, location=20),
                                       D.Event('Line fault', '2-3', 1.5, location=30)], c, None, 5))
    assert 'between 0 and 100' in msgs and 'an in-service line' in msgs and 'same location' in msgs


@needs_compiler
@pytest.mark.parametrize('location, first, second', [(10, 'distance (end 1)', 'distance (end 2)'),
                                                     (90, 'distance (end 2)', 'distance (end 1)')])
def test_line_fault_cleared_by_distance_zones(location, first, second):
    """Near end trips in zone 1 (instantaneous), far end in zone 2 (0.3 s); the fault is left to the relays."""
    c = C.load_builtin('IEEE 14-Bus')
    c['dynamicSettings'] = dict(protection={'lines': {'4-5': {'distance': True}}})
    r = D.simulate(c, [D.Event('Line fault', '4-5', 1.0, duration=0.5, location=location, side='NONE')],
                   stop_time=3, solver='SIM')
    assert r.ok, r.status_text
    ops = r.protection
    assert list(ops['element']) == ['Line 4-5', 'Line 4-5'] and list(ops['protection']) == [first, second]
    assert ops['time'].iloc[0] == pytest.approx(1.06, abs=0.02) and ops['time'].iloc[1] == pytest.approx(1.36, abs=0.02)


@needs_compiler
@pytest.mark.parametrize('event', [D.Event('AVR setpoint step (ΔU)', 'G2', 1.0, value=0.02),
                                   D.Event('Generator ΔP setpoint', 'G2', 1.0, value=0.1)])
def test_setpoint_steps_reach_a_protected_machine(event):
    """A protected machine is a composed model, which pypowsybl's set-point events skip: GsSetpoints applies them."""
    plain = C.load_builtin('IEEE 14-Bus')
    prot = C.load_builtin('IEEE 14-Bus')
    prot['dynamicSettings'] = dict(protection={'generators': {'G2': {'uv': True}}})
    a, b = (D.simulate(c, [event], stop_time=8, solver='SIM') for c in (plain, prot))
    assert a.ok and b.ok and not b.notes
    for frame in ('pgen', 'qgen'):
        da = getattr(a, frame)['G2'].iloc[-1] - getattr(a, frame)['G2'].iloc[0]
        db = getattr(b, frame)['G2'].iloc[-1] - getattr(b, frame)['G2'].iloc[0]
        assert abs(da) > 1 or frame == 'pgen' and event.type.startswith('AVR')
        assert db == pytest.approx(da, abs=0.2)
