"""Diagram editor: the edits the browser component asks for (views/editor.apply_action), and a
network built from scratch with them that solves."""
import json
from pathlib import Path

import pytest

from gridscope import case as C
from gridscope import loadflow as LF
from views.diagram import build_scene
from views.editor import apply_action

CASES = Path(__file__).resolve().parent.parent / 'cases'


@pytest.fixture
def ieee14():
    return C.normalise(json.loads((CASES / 'IEEE_14-Bus.json').read_text()))


def test_select_does_not_change_the_case(ieee14):
    new, msg, sel = apply_action(ieee14, {'type': 'select', 'kind': 'branch', 'id': '2-3'})
    assert new is None and sel == ['branch', '2-3']
    assert apply_action(ieee14, {'type': 'select', 'kind': None, 'id': None})[2] is None


def test_move_bus(ieee14):
    new, msg, sel = apply_action(ieee14, {'type': 'move', 'id': '4', 'x': 512.04, 'y': 300})
    b = next(b for b in new['buses'] if b['id'] == 4)
    assert (b['x'], b['y']) == (512, 300) and sel == ['bus', '4'] and 'Moved bus 4' in msg
    assert next(b for b in ieee14['buses'] if b['id'] == 4)['x'] != 512      # input untouched
    # no-op move
    assert apply_action(new, {'type': 'move', 'id': '4', 'x': 512, 'y': 300})[0] is None


def test_add_bus_takes_next_id_and_nearest_voltage(ieee14):
    b8 = next(b for b in ieee14['buses'] if b['id'] == 8)
    new, msg, sel = apply_action(ieee14, {'type': 'add_bus', 'x': b8['x'] + 5, 'y': b8['y'] + 5})
    bus = new['buses'][-1]
    assert bus['id'] == 15 and bus['type'] == 'PQ' and bus['baseKV'] == b8['baseKV'] and sel == ['bus', '15']


def test_first_bus_of_an_empty_network_is_slack():
    empty = C.normalise({'name': 'x', 'buses': [], 'branches': [], 'generators': []})
    new, _, _ = apply_action(empty, {'type': 'add_bus', 'x': 0, 'y': 0})
    assert new['buses'][0]['type'] == 'Slack' and new['buses'][0]['id'] == 1


def test_add_branches_with_unique_ids(ieee14):
    new, msg, sel = apply_action(ieee14, {'type': 'add_branch', 'from': '2', 'to': '3', 'kind': 'line'})
    br = new['branches'][-1]
    assert br['id'] == '2-3#2' and br['from'] == 2 and br['to'] == 3 and br['equipmentType'] == 'line'
    new, msg, _ = apply_action(new, {'type': 'add_branch', 'from': '2', 'to': '3', 'kind': 'transformer'})
    assert new['branches'][-1]['id'] == '2-3#3' and new['branches'][-1]['equipmentType'] == 'transformer'
    # a line between different voltage levels is allowed but flagged
    assert 'different base kV' in apply_action(ieee14, {'type': 'add_branch', 'from': '4', 'to': '7',
                                                        'kind': 'line'})[1]
    assert apply_action(ieee14, {'type': 'add_branch', 'from': '4', 'to': '4', 'kind': 'line'})[0] is None


def test_add_devices(ieee14):
    new, msg, sel = apply_action(ieee14, {'type': 'add_device', 'kind': 'gen', 'bus': '4'})
    g = new['generators'][-1]
    assert g['id'] == 'G4' and g['bus'] == 4 and sel == ['gen', 'G4']
    assert next(b for b in new['buses'] if b['id'] == 4)['type'] == 'PV' and 'PV' in msg
    new, _, _ = apply_action(new, {'type': 'add_device', 'kind': 'gen', 'bus': '4'})
    assert new['generators'][-1]['id'] == 'G4#2'
    # bus 4 already has a load; bus 1 has none
    assert apply_action(ieee14, {'type': 'add_device', 'kind': 'load', 'bus': '4'})[0] is None
    new, _, _ = apply_action(ieee14, {'type': 'add_device', 'kind': 'load', 'bus': '1'})
    assert next(b for b in new['buses'] if b['id'] == 1)['Pd'] == 10
    new, _, _ = apply_action(ieee14, {'type': 'add_device', 'kind': 'shunt', 'bus': '1'})
    assert next(b for b in new['buses'] if b['id'] == 1)['Bs'] == 10


def test_delete(ieee14):
    new, msg, sel = apply_action(ieee14, {'type': 'delete', 'kind': 'bus', 'id': '2'})
    assert all(b['id'] != 2 for b in new['buses']) and sel is None
    assert all(2 not in (br['from'], br['to']) for br in new['branches'])
    assert all(g['bus'] != 2 for g in new['generators'])
    assert 'branch(es)' in msg and 'generator(s)' in msg
    new, _, _ = apply_action(ieee14, {'type': 'delete', 'kind': 'branch', 'id': '2-3'})
    assert len(new['branches']) == len(ieee14['branches']) - 1
    new, _, _ = apply_action(ieee14, {'type': 'delete', 'kind': 'gen', 'id': 'G2'})
    assert len(new['generators']) == len(ieee14['generators']) - 1


def test_build_a_network_from_scratch_and_solve():
    c = C.blank_case()                                       # one Slack bus at (400, 300)
    steps = [{'type': 'add_device', 'kind': 'gen', 'bus': '1'},
             {'type': 'add_bus', 'x': 600, 'y': 300},
             {'type': 'add_bus', 'x': 500, 'y': 450},
             {'type': 'add_branch', 'from': '1', 'to': '2', 'kind': 'line'},
             {'type': 'add_branch', 'from': '2', 'to': '3', 'kind': 'line'},
             {'type': 'add_branch', 'from': '1', 'to': '3', 'kind': 'transformer'},
             {'type': 'add_device', 'kind': 'load', 'bus': '3'},
             {'type': 'add_device', 'kind': 'shunt', 'bus': '2'},
             {'type': 'move', 'id': '3', 'x': 520, 'y': 460}]
    for s in steps:
        new, msg, _ = apply_action(c, s)
        assert new is not None, (s, msg)
        c = new
    assert not C.validate(c)['errors']
    _, _, res = LF.run_ac(c)
    assert res['converged']
    for style in ('Schematic', 'Standard'):
        sc = build_scene(c, res, style=style).to_json()
        assert set(sc['bus_pos']) == {'1', '2', '3'}
        json.dumps(sc)                                       # what the browser component receives
