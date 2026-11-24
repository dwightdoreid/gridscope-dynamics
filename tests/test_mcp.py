"""
MCP server (gridscope/mcp_server.py), exercised through a real MCP client connected in-process:
tool listing, case open / edit / save round trip through the workspace folder, every steady-state
study, error messages a model can act on, and (with Dynawo) the dynamic studies.

Run with:  python -m pytest -q tests/test_mcp.py
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp import Client                          # noqa: E402

from gridscope import case as C                 # noqa: E402
from gridscope import custom_models as CM       # noqa: E402
from gridscope import mcp_server as M           # noqa: E402

needs_dynawo = pytest.mark.skipif(CM.dynawo_home() is None, reason='Dynawo not configured')


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Each test: no open cases, and a private workspace folder."""
    monkeypatch.setattr(C, 'WORKSPACE_DIR', tmp_path / 'workspace')
    M._CASES.clear()
    yield
    M._CASES.clear()


def session(steps):
    """Run steps(call) against the server; call(tool, **args) -> (is_error, parsed JSON or text)."""
    async def main():
        async with Client(M.SERVER) as c:
            async def call(_tool, _progress=None, **args):
                kw = {}
                if _progress is not None:
                    async def on_progress(p, total, msg):
                        _progress.append((p, total, msg))
                    kw['progress_callback'] = on_progress
                r = await c.call_tool(_tool, args, **kw)
                text = r.content[0].text
                return r.is_error, (text if r.is_error or text[:1] not in '{[' else json.loads(text))
            return await steps(call, c)
    return asyncio.run(main())


def ok(res):
    err, out = res
    assert not err, out
    return out


def test_tools_are_listed_with_schemas():
    async def steps(call, c):
        return (await c.list_tools()).tools
    tools = {t.name: t for t in session(steps)}
    for name in ('list_cases', 'open_case', 'get_case', 'edit_case', 'save_case', 'run_load_flow',
                 'run_contingency_n1', 'run_dc_opf', 'run_fault_study', 'run_dynamic_simulation',
                 'critical_clearing_time', 'screen_dynamic_contingencies'):
        assert name in tools and tools[name].description
    # the edit operations are a typed union the model can read from the schema
    schema = json.dumps(tools['edit_case'].input_schema)
    for op in ('update', 'add_bus', 'add_branch', 'add_generator', 'delete', 'set_case'):
        assert f'"{op}"' in schema
    assert tools['run_load_flow'].annotations.read_only_hint


def test_open_edit_save_round_trip():
    async def steps(call, c):
        o = ok(await call('open_case', source='IEEE 14-Bus', name='Study A'))
        cid = o['case_id']
        assert o['summary']['buses'] == 14 and o['validation']['ok']
        e = ok(await call('edit_case', case_id=cid, operations=[
            {'op': 'update', 'kind': 'bus', 'id': 14, 'fields': {'Pd': 40}},
            {'op': 'add_bus', 'fields': {'id': 101, 'baseKV': 13.8, 'Pd': 5, 'Qd': 1}},
            {'op': 'add_branch', 'from_bus': 14, 'to_bus': 101, 'fields': {'id': 'L14-101', 'x': 0.2, 'rate': 30}},
            {'op': 'add_generator', 'bus': 101, 'fields': {'id': 'G101', 'Pg': 3, 'Pmax': 10}},
            {'op': 'update', 'kind': 'generator', 'id': 'G101', 'fields': {'dyn': {'H': 4.0}}},
            {'op': 'set_case', 'fields': {'dynamicSettings': {'angle_limit': 150}}}]))
        assert len(e['applied']) == 6, e
        data = ok(await call('get_case', case_id=cid, tables=['buses', 'dynamics'], ids=['14', '101', 'G101']))
        rows = {r[0]: dict(zip(data['buses']['columns'], r)) for r in data['buses']['rows']}
        assert rows[14]['Pd'] == 40 and rows[101]['baseKV'] == 13.8 and rows[101]['type'] == 'PV'
        assert data['dynamics']['generators'][0]['H'] == 4.0
        assert data['dynamics']['dynamicSettings']['angle_limit'] == 150
        s = ok(await call('save_case', case_id=cid))
        assert s['file'] == 'Study_A.json'
        # the app's Case picker reads the same folder
        assert 'Study_A.json' in C.workspace_cases()
        again = ok(await call('open_case', source='Study_A.json'))
        assert again['summary']['buses'] == 15 and again['saved_as'] == 'Study_A.json'
        # another case may not silently replace the file
        other = ok(await call('open_case', source='IEEE 9-Bus', name='Study A'))
        err, msg = await call('save_case', case_id=other['case_id'])
        assert err and 'already exists' in msg
        ok(await call('save_case', case_id=other['case_id'], overwrite=True))
        return C.load_file(C.WORKSPACE_DIR / 'Study_A.json')
    saved = session(steps)
    assert len(saved['buses']) == 9 and not any('_k' in b for b in saved['buses'])


def test_edits_are_all_or_nothing_and_errors_name_valid_ids():
    async def steps(call, c):
        cid = ok(await call('open_case', source='IEEE 9-Bus'))['case_id']
        err, msg = await call('edit_case', case_id=cid, operations=[
            {'op': 'update', 'kind': 'bus', 'id': 5, 'fields': {'Pd': 999}},
            {'op': 'delete', 'kind': 'branch', 'id': 'nope'}])
        assert err and 'Operation 2 (delete)' in msg and 'L5-6' in msg      # lists the real branch ids
        data = ok(await call('get_case', case_id=cid, tables=['buses'], ids=['5']))
        assert data['buses']['rows'][0][data['buses']['columns'].index('Pd')] != 999   # first op rolled back
        err, msg = await call('edit_case', case_id=cid, operations=[
            {'op': 'update', 'kind': 'branch', 'id': 'L5-6', 'fields': {'from': 1}}])
        assert err and 'cannot be changed' in msg
        err, msg = await call('get_case', case_id='missing')
        assert err and 'open_case' in msg
    session(steps)


def test_steady_state_studies():
    async def steps(call, c):
        cid = ok(await call('open_case', source='IEEE 14-Bus'))['case_id']
        ac = ok(await call('run_load_flow', case_id=cid))
        assert ac['converged'] and ac['totals']['load_MW'] == pytest.approx(259.0)
        assert 0 < ac['totals']['losses_MW'] < 20
        assert 'buses' not in ac                                    # summary unless detail='full'
        full = ok(await call('run_load_flow', case_id=cid, detail='full', max_rows=5))
        assert full['buses']['total_rows'] == 14 and len(full['buses']['rows']) == 5 and full['buses']['truncated']
        dc = ok(await call('run_load_flow', case_id=cid, method='dc'))
        assert dc['converged'] and dc['most_loaded']['rows']
        n1 = ok(await call('run_contingency_n1', case_id=cid))
        assert sum(n1['counts'].values()) == 20
        opf = ok(await call('run_dc_opf', case_id=cid))
        assert opf['ok'] and opf['total_pg'] == pytest.approx(259.0, rel=1e-3)
        sc = ok(await call('run_fault_study', case_id=cid, fault_bus='4', fault_type='slg'))
        assert sc['results']['total_rows'] == 1 and sc['max_Ik_kA'] > 0
        err, msg = await call('run_fault_study', case_id=cid, fault_bus='99')
        assert err and 'No bus' in msg
    session(steps)


def test_invalid_case_is_refused_before_solving():
    async def steps(call, c):
        cid = ok(await call('open_case', source='new'))['case_id']
        ok(await call('edit_case', case_id=cid, operations=[
            {'op': 'update', 'kind': 'bus', 'id': 1, 'fields': {'type': 'PQ'}}]))   # no slack any more
        v = ok(await call('validate_case', case_id=cid))
        assert not v['ok'] and v['errors']
        err, msg = await call('run_load_flow', case_id=cid)
        assert err and 'validation errors' in msg
    session(steps)


@needs_dynawo
def test_dynamic_simulation_curves_and_studies():
    async def steps(call, c):
        cid = ok(await call('open_case', source='IEEE 9-Bus'))['case_id']
        r = ok(await call('run_dynamic_simulation', case_id=cid, stop_time=4, sample_dt=0.5, events=[
            {'type': 'Bus fault', 'element': '5', 'start': 1.0, 'duration': 0.08},
            {'type': 'Trip branch', 'element': 'L5-6', 'start': 1.08}]))
        assert r['status'] == 'SUCCESS' and r['stable'] is True
        assert r['metrics']['max_angle_sep'] > 0 and r['curves']['angles']['rows']
        cur = ok(await call('get_dynamic_curves', case_id=cid, quantities=['voltages'], elements=['5'],
                            t_start=0.95, t_end=1.05, sample_dt=0.05))
        v = [row[1] for row in cur['curves']['voltages']['rows']]
        assert v[0] > 0.9 and min(v) < 0.1                         # the fault is visible
        err, msg = await call('run_dynamic_simulation', case_id=cid, events=[
            {'type': 'Trip generator', 'element': 'G9', 'start': 1.0}])
        assert err and 'G1' in msg
        progress = []
        s = ok(await call('screen_dynamic_contingencies', progress, case_id=cid, branch_ids=['L5-6', 'L8-9'],
                          post_time=2))
        assert s['results']['total_rows'] == 2 and len(progress) == 2
        # an edit makes the stored result out of date
        ok(await call('edit_case', case_id=cid, operations=[{'op': 'update', 'kind': 'bus', 'id': 5, 'fields': {'Pd': 120}}]))
        err, msg = await call('get_dynamic_curves', case_id=cid, quantities=['angles'])
        assert err and 'run_dynamic_simulation first' in msg
    session(steps)


def test_curves_resample_non_monotonic_solver_output():
    """IDA (variable step) can return time stamps out of order and repeated; sampling must still work."""
    import pandas as pd

    class R:
        voltages = pd.DataFrame({'5': [1.0, 0.2, 0.9, 0.95, 0.97]}, index=[0.0, 1.0, 1.1, 1.05, 1.1])
    out = M._curves(R, ['voltages'], None, None, None, 0.05)['voltages']
    t = [row[0] for row in out['rows']]
    assert t == sorted(t) and out['rows'][-1][1] == 0.97
