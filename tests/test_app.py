"""
Headless end-to-end test of the Streamlit app: every view renders and every study runs from the UI.
Needs Dynawo configured (for the Dynamic Stability part).

Run with:  python -m pytest -q tests/test_app.py
"""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / 'app.py')
VIEWS = ['network', 'buses', 'branches', 'generators', 'results', 'contingency', 'opf', 'faults', 'plots', 'dynamics']


def _no_errors(at):
    assert not at.exception, [str(e.value)[:300] for e in at.exception]


def _click(at, label):
    [b for b in at.button if b.label == label][0].click().run()
    _no_errors(at)


@pytest.fixture(scope='module')
def at():
    a = AppTest.from_file(APP, default_timeout=300).run()
    _no_errors(a)
    return a


def test_every_view_renders(at):
    for v in VIEWS:
        at.session_state['view'] = v
        at.run()
        _no_errors(at)


def test_diagram_style_switch(at):
    at.session_state['view'] = 'network'
    at.run()
    style = [r for r in at.radio if r.key == 'diag_style'][0]
    style.set_value('Standard').run()
    _no_errors(at)
    assert any('bus bar' in m.value for m in at.markdown)       # legend switches to the standard symbols
    [r for r in at.radio if r.key == 'diag_style'][0].set_value('Schematic').run()
    _no_errors(at)


def test_diagram_edit_mode_and_property_panel(at):
    at.session_state['view'] = 'network'
    at.run()
    [t for t in at.toggle if t.key == 'diag_edit'][0].set_value(True).run()
    _no_errors(at)
    at.session_state['inspect'] = ['bus', '9']
    at.run()
    load_p = [n for n in at.number_input if n.label == 'Load P (MW)'][0]
    assert load_p.value == 29.5
    load_p.set_value(40.0)
    [b for b in at.button if b.label == 'Apply'][0].click().run()
    _no_errors(at)
    assert next(b for b in at.session_state['case']['buses'] if b['id'] == 9)['Pd'] == 40.0
    assert at.session_state['sld_undo']                         # the panel edit can be undone
    at.session_state['case'] = at.session_state['sld_undo'][-1]  # put the case back for the later tests
    [t for t in at.toggle if t.key == 'diag_edit'][0].set_value(False).run()
    _no_errors(at)


def test_steady_state_studies_from_ui(at):
    at.session_state['view'] = 'network'
    at.run()
    _click(at, '▶ Run AC Load Flow')
    assert at.session_state['view'] == 'network'          # stays put: results show on the diagram
    assert at.session_state['results']['ac']['value']['converged']
    at.session_state['view'] = 'contingency'
    at.run()
    _click(at, 'Run DC Load Flow')
    _click(at, 'Run N-1 Contingency')
    assert 'n1' in at.session_state['results']
    at.session_state['view'] = 'opf'
    at.run()
    _click(at, 'Run DC-OPF')
    assert at.session_state['results']['opf']['value']['ok']
    at.session_state['view'] = 'faults'
    at.run()
    _click(at, 'Run Fault Study')
    assert at.session_state['results']['fault']['value']['max_Ik'] > 0


def test_dynamic_studies_from_ui(at):
    _click(at, 'Load')                    # selected built-in case (IEEE 14-Bus by default)
    at.session_state['view'] = 'dynamics'
    at.run()
    _no_errors(at)
    _click(at, '▶ Run simulation')
    r = at.session_state['results']['dyn_sim']['value']
    assert r.ok and r.stable is not None
    _click(at, '⏱ Find critical clearing time')
    cct = at.session_state['results']['cct']['value']
    assert cct['runs']
    at.session_state['view'] = 'network'
    at.run()
    _no_errors(at)


def test_editing_a_table_marks_results_stale(at):
    at.session_state['view'] = 'network'
    at.run()
    _click(at, '▶ Run AC Load Flow')
    v0 = at.session_state['case_version']
    case = dict(at.session_state['case'])
    case['buses'] = [dict(b) for b in case['buses']]
    case['buses'][-1]['Pd'] = case['buses'][-1]['Pd'] + 5
    at.session_state['case'] = case
    at.session_state['case_version'] = v0 + 1
    at.session_state['view'] = 'results'
    at.run()
    _no_errors(at)
    assert any('model has changed' in w.value for w in at.warning)


def test_controls_editor_composes_and_applies(at):
    _click(at, 'Load')
    at.session_state['view'] = 'dynamics'
    at.run()
    gov = [s for s in at.selectbox if s.label == 'Governor'][0]
    assert {'TGOV1 (steam)', 'GAST (gas turbine)', 'HYGOV (hydro)'} <= set(gov.options)
    gov.set_value('TGOV1 (steam)').run()
    _no_errors(at)
    [s for s in at.selectbox if s.label == 'Voltage regulator (AVR)'][0].set_value('SEXS (simplified exciter)').run()
    [s for s in at.selectbox if s.label == 'Power system stabilizer (PSS)'][0].set_value('PSS2A (IEEE)').run()
    _no_errors(at)
    g1 = at.session_state['case']['generators'][0]
    assert g1['dyn']['model'] == 'GeneratorSynchronousFourWindingsTGov1SexsPss2a'     # shipped by Dynawo
    _click(at, [b.label for b in at.button if b.label.startswith('Copy ') and 'to all generators' in b.label][0])
    models = {g['dyn']['model'] for g in at.session_state['case']['generators']}
    assert models == {'GeneratorSynchronousFourWindingsTGov1SexsPss2a'}
    _click(at, '▶ Run simulation')
    r = at.session_state['results']['dyn_sim']['value']
    assert r.ok and len(r.efd.columns) > 0


def test_technology_switch_to_battery_storage(at):
    at.session_state['case_pick'] = 'IEEE 9-Bus'
    at.run()
    _click(at, 'Load')
    at.session_state['view'] = 'dynamics'
    at.run()
    [s for s in at.selectbox if s.label == 'Generator'][0].set_value('G3').run()
    [s for s in at.selectbox if s.label == 'Technology'][0].set_value('Battery storage').run()
    _no_errors(at)
    g3 = next(g for g in at.session_state['case']['generators'] if g['id'] == 'G3')
    assert g3['dyn']['source'] == 'BESScbWeccCurrentSource'
    assert any(s.label == 'Model' for s in at.selectbox)
    _click(at, '▶ Run simulation')
    r = at.session_state['results']['dyn_sim']['value']
    assert r.ok and 'G3' in r.pgen.columns and 'G3' not in r.angles.columns


def test_load_model_selection(at):
    _click(at, 'Load')
    at.session_state['view'] = 'dynamics'
    at.run()
    sel = [s for s in at.selectbox if s.label == 'Default load model'][0]
    sel.set_value('LoadAlphaBetaRestorative').run()
    _no_errors(at)
    assert at.session_state['case']['dynamicSettings']['loadModel'] == 'LoadAlphaBetaRestorative'


def test_changing_controls_keeps_machine_data(at):
    """Benchmark inertia / reactances must survive a change of governor/AVR/PSS (they belong to the machine)."""
    at.session_state['case_pick'] = 'IEEE 9-Bus'
    at.run()
    _click(at, 'Load')
    at.session_state['view'] = 'dynamics'
    at.run()
    _click(at, 'Load benchmark data')
    h_before = {g['id']: g['dyn']['H'] for g in at.session_state['case']['generators']}
    sel = [s for s in at.selectbox if s.label == 'Generator'][0]
    sel.set_value('G2').run()
    [s for s in at.selectbox if s.label == 'Governor'][0].set_value('TGOV1 (steam)').run()
    _no_errors(at)
    g2 = next(g for g in at.session_state['case']['generators'] if g['id'] == 'G2')
    assert g2['dyn']['model'] == 'GsSync4W_TGov1_VRProportional_None'       # composed by GridScope
    assert g2['dyn']['H'] == h_before['G2'] == 3.333


def test_benchmark_rotor_type_survives_rerenders(at):
    """Stale widget state must not overwrite the case: WSCC G1 is a salient-pole (3-winding) machine."""
    at.session_state['case_pick'] = 'IEEE 9-Bus'
    at.run()
    _click(at, 'Load')
    at.session_state['view'] = 'dynamics'
    at.run()
    [s for s in at.selectbox if s.label == 'Generator'][0].set_value('G1').run()
    _click(at, 'Reset to typical values')          # G1 -> round rotor (default)
    _click(at, 'Load benchmark data')              # G1 -> salient pole
    at.run()
    _no_errors(at)
    g1 = next(g for g in at.session_state['case']['generators'] if g['id'] == 'G1')
    assert g1['dyn']['model'] == 'GeneratorSynchronousThreeWindingsProportionalRegulations'
    assert [s for s in at.selectbox if s.label == 'Machine'][0].value == 'Salient pole (3 windings)'


def test_disabled_events_are_kept_but_not_run():
    import pandas as pd
    from views import dynamic as V
    df = pd.DataFrame([
        {'On': True, 'Type': 'Bus fault', 'Element': '7', 'Start (s)': 1.0, 'Duration (s)': 0.1, 'Value': None, 'Side': ''},
        {'On': False, 'Type': 'Trip branch', 'Element': 'L6-7', 'Start (s)': 1.1, 'Duration (s)': None, 'Value': None,
         'Side': ''}])
    assert [e.type for e in V._events_from_df(df)] == ['Bus fault']
    assert len(V._events_from_df(df, include_disabled=True)) == 2
    old = df.drop(columns=['On'])                    # tables saved before the column existed
    assert V._with_on(old)['On'].tolist() == [True, True] and len(V._events_from_df(old)) == 2


def test_edit_comparison_ignores_representation():
    from views import common as U
    assert U._same(1, '1') and U._same(1.0, 1) and U._same(None, float('nan'))
    assert not U._same(90, 71.64) and not U._same('G1', 'G2')
    assert U._like('2', 1) == 2 and isinstance(U._like('2', 1), int) and U._like('x', 1) == 'x'


def test_protection_tab_turns_functions_on(at):
    at.session_state['case_pick'] = 'IEEE 9-Bus'
    at.run()
    _click(at, 'Load')
    at.session_state['view'] = 'dynamics'
    at.run()
    _no_errors(at)
    at.button(key='prot_all_transformers').click().run()
    _no_errors(at)
    prot = at.session_state['case']['dynamicSettings']['protection']['transformers']
    assert set(prot) == {'T1', 'T2', 'T3'} and all(v['oc'] for v in prot.values())
    at.button(key='prot_none_transformers').click().run()
    assert not at.session_state['case']['dynamicSettings']['protection']['transformers']


def test_dynamic_setup_is_saved_with_the_network(at):
    import json
    from gridscope import case as C
    from views import common as U
    at.session_state['case_pick'] = 'IEEE 9-Bus'
    at.run()
    _click(at, 'Load')
    at.session_state['view'] = 'dynamics'
    at.run()
    at.button(key='prot_all_transformers').click().run()
    ds = at.session_state['case']['dynamicSettings']
    assert ds['events'] and ds['events'][0]['Type'] == 'Bus fault' and ds['stopTime'] == 10.0
    assert {'cct', 'screening', 'protection', 'solver', 'f_nom'} <= set(ds)
    # what Save writes, read back as Open does
    saved = json.dumps(U.clean_case_for_export(at.session_state['case']))
    back = C.load_json_text(saved)['dynamicSettings']
    assert back['events'] == ds['events'] and back['protection']['transformers']['T2']['oc']
    # a saved event list (one event switched off) is what the editor shows after opening the file
    case = C.load_json_text(saved)
    case['dynamicSettings']['events'][1]['On'] = False
    case['dynamicSettings']['stopTime'] = 7.0
    at.session_state['case'] = case
    at.session_state['case_version'] += 1
    at.run()
    _no_errors(at)
    ds2 = at.session_state['case']['dynamicSettings']
    assert [e['On'] for e in ds2['events']] == [True, False] and ds2['stopTime'] == 7.0
    assert [n.value for n in at.number_input if n.label == 'Stop time (s)'] == [7.0]


def test_plot_selection_on_a_large_system():
    """IEEE 118 has 54 generators: the machine plots start with the 8 most disturbed and can be changed."""
    at = AppTest.from_file(APP, default_timeout=600).run()
    at.selectbox(key='case_pick').set_value('IEEE 118-Bus (mod)').run()
    _click(at, 'Load')
    at.session_state['view'] = 'dynamics'
    at.run()
    _click(at, '▶ Run simulation')
    r = at.session_state['results']['dyn_sim']['value']
    assert r.ok
    gens = list(dict.fromkeys(list(r.angles.columns) + list(r.pgen.columns)))
    assert len(gens) > 10
    ms = at.multiselect(key='gens_sim_pick')
    assert len(ms.value) == 8 and set(ms.value) <= set(gens)
    biggest = max(r.angles.columns, key=lambda g: float(r.angles[g].max() - r.angles[g].min()))
    assert biggest in ms.value

    def plotted():
        import json
        fig = json.loads(at.get('plotly_chart')[0].proto.spec)
        return {t['legendgroup'] for t in fig['data'] if t.get('legendgroup')}

    assert plotted() == set(ms.value)
    _click(at, 'All')
    assert len(at.multiselect(key='gens_sim_pick').value) == len(gens)
    [b for b in at.button if b.key == 'gens_sim_none'][0].click().run()
    _no_errors(at)
    assert at.multiselect(key='gens_sim_pick').value == []
    at.multiselect(key='gens_sim_pick').select(gens[0]).run()
    _no_errors(at)
    assert at.multiselect(key='gens_sim_pick').value == [gens[0]] and plotted() == {gens[0]}
    at.run()                                           # the choice survives a rerun
    assert at.multiselect(key='gens_sim_pick').value == [gens[0]]
    assert len(at.multiselect(key='vb_sim_pick').value) == 6
