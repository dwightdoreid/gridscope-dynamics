"""Single-line diagram: both drawing styles build for every bundled case, and every bus and branch
stays hoverable / clickable (the inspector relies on the customdata)."""
import json
from pathlib import Path

import pytest

from gridscope import case as C
from gridscope import loadflow as LF
from views.diagram import STYLES, build_figure, build_scene

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


@pytest.mark.parametrize('style', STYLES)
def test_power_flow_data_for_the_arrows(style):
    c = _load(Path(__file__).resolve().parent.parent / 'cases' / 'IEEE_9-Bus.json')
    _, _, res = LF.run_ac(c)
    flows = {f['id']: f for f in build_scene(c, res, style=style).flows}
    assert set(flows) == {str(br['id']) for br in c['branches']}         # every in-service branch
    br_res = res['branches'].set_index('id')
    for bid, f in flows.items():
        r = br_res.loc[bid]
        assert f['P'] == pytest.approx([r.Pf, r.Pt]) and f['Q'] == pytest.approx([r.Qf, r.Qt])
        frm, to = next((str(b['from']), str(b['to'])) for b in c['branches'] if str(b['id']) == bid)
        assert f['ends'] == [frm, to] or tuple(f['ends']) == (frm, to)
        # the path runs from the 'from' bus to the 'to' bus
        bus = {str(b['id']): (b['x'], b['y']) for b in c['buses']}
        start, end = f['pts'][0], f['pts'][-1]
        d = lambda p, q: ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5   # noqa: E731
        assert d(start, bus[frm]) < d(start, bus[to]) and d(end, bus[to]) < d(end, bus[frm])
    # L5-6 exports its charging MVAr at both ends: both Q values negative (leaving the line)
    assert flows['L5-6']['Q'][0] < 0 and flows['L5-6']['Q'][1] < 0


def test_no_flow_arrows_without_a_current_load_flow_or_during_dynamic_replay():
    c = _load(Path(__file__).resolve().parent.parent / 'cases' / 'IEEE_9-Bus.json')
    _, _, res = LF.run_ac(c)
    assert build_scene(c, None, style='Standard').flows == []
    overlay = dict(voltages={str(b['id']): 1.0 for b in c['buses']}, label='t')
    assert build_scene(c, res, overlay, style='Standard').flows == []
    # an open branch carries no arrows
    c['branches'][0]['status'] = 0
    _, _, res = LF.run_ac(c)
    assert str(c['branches'][0]['id']) not in {f['id'] for f in build_scene(c, res, style='Standard').flows}
