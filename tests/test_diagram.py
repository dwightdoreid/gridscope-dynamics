"""Single-line diagram: both drawing styles build for every bundled case, and every bus and branch
stays hoverable / clickable (the inspector relies on the customdata)."""
import json
from pathlib import Path

import pytest

from gridscope import case as C
from gridscope import loadflow as LF
from views.diagram import STYLES, build_figure

CASES = sorted((Path(__file__).resolve().parent.parent / 'cases').glob('*.json'))


def _load(path):
    return C.normalise(json.loads(path.read_text()))


def _clickable(fig, kind):
    return {cd[1] for tr in fig.data if tr.customdata is not None for cd in tr.customdata if cd[0] == kind}


@pytest.mark.parametrize('style', STYLES)
@pytest.mark.parametrize('path', CASES, ids=[p.stem for p in CASES])
def test_every_bus_and_branch_is_clickable(path, style):
    c = _load(path)
    fig = build_figure(c, style=style)
    assert _clickable(fig, 'bus') == {str(b['id']) for b in c['buses']}
    assert _clickable(fig, 'branch') == {str(br['id']) for br in c['branches']}


def test_standard_style_draws_symbols():
    c = _load(Path(__file__).resolve().parent.parent / 'cases' / 'IEEE_14-Bus.json')
    fig = build_figure(c, style='Standard')
    devices = [tr for tr in fig.data if tr.name == 'devices'][0]
    tips = [cd[2] for cd in devices.customdata]
    assert sum('generator' in t for t in tips) == len(c['generators'])
    assert sum(t.startswith('<b>Load') for t in tips) == sum(1 for b in c['buses'] if b['Pd'] or b['Qd'])
    assert sum(t.startswith('<b>Shunt') for t in tips) == sum(1 for b in c['buses'] if b['Bs'] or b['Gs'])
    # transformer tooltips carry the tap, lines don't
    branches = [tr for tr in fig.data if tr.name == 'branches'][0]
    n_trafo = sum(C.is_transformer(br, c) for br in c['branches'])
    assert sum('Tap' in cd[2] for cd in branches.customdata) == n_trafo == 4      # 4-7, 4-9, 5-6, 7-8


@pytest.mark.parametrize('style', STYLES)
def test_load_flow_and_dynamic_overlays(style):
    c = _load(Path(__file__).resolve().parent.parent / 'cases' / 'IEEE_9-Bus.json')
    _, _, res = LF.run_ac(c)
    assert res['converged']
    fig = build_figure(c, res, style=style)
    texts = [t for tr in fig.data if tr.mode == 'text' for t in tr.text]
    assert any(' pu' in t for t in texts)                       # solved voltages shown
    overlay = dict(voltages={str(b['id']): 0.97 for b in c['buses']}, label='V at t = 0.10 s')
    open_br = (c['branches'][0]['id'],)
    fig = build_figure(c, res, overlay, open_br, style=style)
    tips = [cd[2] for tr in fig.data if tr.customdata is not None for cd in tr.customdata]
    assert any('V at t = 0.10 s' in t for t in tips)
    assert any('<i>open</i>' in t for t in tips)
