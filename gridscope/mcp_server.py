"""
GridScope Dynamics MCP server: the app's power-system engine for LLM clients (Claude Code, Claude
Desktop, any MCP client) over the Model Context Protocol.

Run with:   ./run_mcp.sh            (stdio; this is what MCP clients launch)
            python -m gridscope.mcp_server

What a client can do
--------------------
* open cases (built-in, from the workspace folder, blank, or pasted GridScope JSON), read and edit them,
  check them with the app's model validation, and save them to the workspace folder;
* run every study the app runs: AC / DC load flow, N-1 contingency, DC-OPF, short-circuit, and with
  Dynawo installed, time-domain simulations, critical clearing time and dynamic N-1 screening.

Cases saved here go to <project>/workspace/ and appear in the app's Case picker ("workspace / ..."),
so a person can open what an LLM built or studied, and the other way round.

The server holds open cases in memory, keyed by a short case_id. Edits are applied all-or-nothing and
make the case's earlier results out of date. Every engine call runs on ONE worker thread, as in the app
(pypowsybl's native library must not be called from short-lived threads).
"""
from __future__ import annotations

import asyncio
import copy
import functools
import inspect
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Any, Literal, Union

import numpy as np
import pandas as pd
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from gridscope import case as C
from gridscope import edit as E

VMIN, VMAX = 0.95, 1.05          # pu, GridScope's voltage band
DYN_DEFAULTS = dict(load_alpha=2.0, load_beta=2.0, angle_limit=180.0, solver='SIM')
KINDS = {'bus': 'buses', 'branch': 'branches', 'generator': 'generators'}

INSTRUCTIONS = """\
GridScope Dynamics: steady-state and dynamic power-system studies on GridScope JSON cases.

Workflow: list_cases -> open_case (returns a case_id) -> get_case / edit_case -> run_* studies ->
save_case (writes to the workspace folder, which the GridScope Dynamics app lists in its Case picker).

Units: per-unit data on case.baseMVA as in MATPOWER. Bus Pd/Qd, generator Pg/Qg/Pmax/... and branch
rate are in MW / MVAr / MVA; branch r, x, b in pu; bus baseKV in kV; Vm in pu, Va in degrees.
Bus types: Slack, PV, PQ (exactly one Slack per island). Loads and shunts are bus fields (Pd, Qd, Gs, Bs).
Branch ids are strings; bus ids are usually integers. A branch with tap != 1 or different end kV is a
transformer.

Dynamic studies need Dynawo (check dynawo_status). Generators without dynamic data get typical machine
data scaled to their rating; list_dynamic_models shows what a generator's "dyn" field may select.
Results are engineering estimates from open-source engines, not a certified tool.
"""

SERVER = MCPServer(name='gridscope-dynamics', title='GridScope Dynamics', instructions=INSTRUCTIONS,
                   version='1.0')

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
COMPUTE = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
EDITS = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)


def _compact(result):
    """Dict results go out as compact JSON (the SDK default is indented, which costs the client tokens)."""
    if isinstance(result, dict):
        return CallToolResult(content=[TextContent(type='text', text=json.dumps(
            result, separators=(',', ':'), ensure_ascii=False, default=str))])
    return result


def _tool(annotations: ToolAnnotations):
    """Register a tool. Any exception reaches the client as a ToolError carrying its message, so the
    model can see what was wrong (bad id, validation errors, a solver failure) and correct its call:
    the SDK would otherwise replace the text of an unexpected exception with a generic one."""
    def deco(fn):
        def fail(e):
            if isinstance(e, ToolError):
                return e
            return ToolError(str(e) if isinstance(e, (ValueError, KeyError)) else f'{type(e).__name__}: {e}')

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(*a, **k):
                try:
                    return _compact(await fn(*a, **k))
                except Exception as e:
                    raise fail(e) from e
        else:
            @functools.wraps(fn)
            def wrapper(*a, **k):
                try:
                    return _compact(fn(*a, **k))
                except Exception as e:
                    raise fail(e) from e
        SERVER.tool(annotations=annotations)(wrapper)
        return fn                     # the plain function stays callable from Python (and the tests)
    return deco


# ------------------------------------------------------------------ engine thread

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix='pypowsybl')


async def _engine(fn, *args, **kwargs):
    """Run an engine call on the single pypowsybl worker thread."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EXECUTOR, lambda: fn(*args, **kwargs))


def _progress_bridge(ctx: Context | None, total: float | None, describe):
    """A thread-safe progress(*args) callback for the engine that reports MCP progress notifications."""
    if ctx is None:
        return None
    loop = asyncio.get_running_loop()

    def cb(*args):
        done, message = describe(*args)
        asyncio.run_coroutine_threadsafe(ctx.report_progress(done, total, message), loop)
    return cb


# ------------------------------------------------------------------ open cases

class _Open:
    def __init__(self, case: dict, source: str):
        self.case = case
        self.source = source
        self.saved_as: str | None = None
        self.changed = False
        self.dyn = None                  # last DynResult (for get_dynamic_curves)
        self.dyn_events: list = []


_CASES: dict[str, _Open] = {}


def _slug(s: str) -> str:
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', str(s)).strip('_') or 'network'


def _new_id(name: str) -> str:
    base = _slug(name).lower()[:40]
    cid, k = base, 2
    while cid in _CASES:
        cid, k = f'{base}-{k}', k + 1
    return cid


def _id_hint(c: dict, limit: int = 40) -> str:
    """The element ids of a case, for error messages (so a model can correct a wrong id at once)."""
    def ids(rows):
        s = [str(r['id']) for r in rows]
        return ', '.join(s[:limit]) + (f' ... ({len(s)} in all)' if len(s) > limit else '')
    return (f'[bus ids: {ids(c["buses"])}] [branch ids: {ids(c["branches"])}] '
            f'[generator ids: {ids(c["generators"])}]')


def _get(case_id: str) -> _Open:
    if case_id not in _CASES:
        known = ', '.join(_CASES) or 'none'
        raise ValueError(f'Unknown case_id {case_id!r}. Open cases: {known} (open_case opens another)')
    return _CASES[case_id]


# ------------------------------------------------------------------ result formatting

def _clean(v):
    """JSON-safe scalar: NaN/inf -> None, numpy -> Python, floats rounded to 5 significant-ish digits."""
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        v = float(v)
        if not math.isfinite(v):
            return None
        return round(v, 5) if abs(v) < 1e6 else round(v)
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (pd.Timestamp,)):
        return str(v)
    return v


def _table(df: pd.DataFrame | None, max_rows: int | None = None) -> dict:
    """Compact table: {columns, rows, total_rows[, truncated]}."""
    if df is None or not len(df):
        return dict(columns=list(df.columns) if df is not None else [], rows=[], total_rows=0)
    n = len(df)
    part = df if not max_rows or n <= max_rows else df.head(max_rows)
    out = dict(columns=[str(c) for c in part.columns],
               rows=[[_clean(v) for v in row] for row in part.itertuples(index=False, name=None)],
               total_rows=n)
    if len(part) < n:
        out['truncated'] = f'showing the first {len(part)} of {n} rows; raise max_rows to see more'
    return out


def _validation(case: dict) -> dict:
    v = C.validate(case)
    return dict(ok=not v['errors'], errors=[e['message'] for e in v['errors']],
                warnings=[w['message'] for w in v['warnings']])


def _overview(cid: str) -> dict:
    o = _CASES[cid]
    s = C.summary(o.case)
    v = _validation(o.case)
    return dict(case_id=cid, name=o.case['name'], source=o.source, saved_as=o.saved_as,
                unsaved_changes=o.changed,
                summary={k.replace(' ', '_').lower(): _clean(x) for k, x in s.items()},
                validation=dict(ok=v['ok'], errors=len(v['errors']), warnings=len(v['warnings']),
                                first_issues=(v['errors'] + v['warnings'])[:5]))


# ------------------------------------------------------------------ cases

@_tool(READ_ONLY)
def list_cases() -> dict:
    """List the built-in cases, the GridScope JSON files in the shared workspace folder (the app's
    Case picker shows them too) and the cases open in this session."""
    return dict(
        built_in=list(C.BUILT_IN_CASES),
        workspace_folder=str(C.WORKSPACE_DIR),
        workspace=[dict(file=n, modified=pd.Timestamp.fromtimestamp(p.stat().st_mtime).isoformat(timespec='seconds'))
                   for n, p in C.workspace_cases().items()],
        open=[_overview(cid) for cid in _CASES])


@_tool(EDITS)
def open_case(source: Annotated[str, Field(description=(
                  'A built-in case name (e.g. "IEEE 14-Bus"), a workspace file name (e.g. "my_grid.json"), '
                  '"new" for a blank network with one slack bus, or "json" to import json_text'))],
              json_text: Annotated[str | None, Field(description='GridScope JSON when source is "json"')] = None,
              name: Annotated[str | None, Field(description='Rename the opened case')] = None) -> dict:
    """Open a case in this session and return its case_id, summary and validation status.
    Opening the same source twice gives two independent copies."""
    if source in C.BUILT_IN_CASES:
        case = C.load_builtin(source)
    elif source == 'new':
        case = C.blank_case(name or 'New Network')
    elif source == 'json':
        if not json_text:
            raise ValueError('source "json" needs json_text')
        case = C.load_json_text(json_text)
        source = 'pasted JSON'
    else:
        files = C.workspace_cases()
        fname = source if source.endswith('.json') else source + '.json'
        if fname not in files:
            raise ValueError(f'No built-in case or workspace file called {source!r}. '
                             f'Built-in: {", ".join(C.BUILT_IN_CASES)}. Workspace: {", ".join(files) or "empty"}')
        case = C.load_file(files[fname])
        source = f'workspace/{fname}'
    if name:
        case['name'] = name
    cid = _new_id(case['name'])
    _CASES[cid] = _Open(case, source)
    if source.startswith('workspace/'):
        _CASES[cid].saved_as = source[len('workspace/'):]
    return _overview(cid)


@_tool(READ_ONLY)
def get_case(case_id: str,
             tables: Annotated[list[Literal['buses', 'branches', 'generators', 'dynamics']], Field(
                 description='Which element tables to return. "dynamics" = per-generator dynamic model data '
                             'plus the case dynamicSettings and protection settings')] = ['buses', 'branches', 'generators'],  # noqa: B006
             ids: Annotated[list[str] | None, Field(description='Only these element ids (any table)')] = None,
             max_rows: int = 300) -> dict:
    """Return a case's data: header fields and the requested element tables (GridScope JSON fields)."""
    o = _get(case_id)
    c = o.case
    keep = None if ids is None else {str(i) for i in ids}
    out = dict(_overview(case_id), baseMVA=c['baseMVA'])
    for t in tables:
        if t == 'dynamics':
            rows = [dict(id=g['id'], bus=g['bus'], **(g.get('dyn') or {'model': '(default: typical data)'}))
                    for g in c['generators'] if keep is None or str(g['id']) in keep]
            out['dynamics'] = dict(generators=_clean(rows),
                                   dynamicSettings=_clean({**DYN_DEFAULTS, **(c.get('dynamicSettings') or {})}))
            continue
        recs = [{k: v for k, v in r.items() if k not in ('_k', 'dyn')} for r in c[t]
                if keep is None or str(r['id']) in keep]
        cols = list(dict.fromkeys(k for r in recs for k in r))
        out[t] = _table(pd.DataFrame(recs, columns=cols), max_rows)
    return out


@_tool(READ_ONLY)
def validate_case(case_id: str) -> dict:
    """Run the app's model validation (GridScope's rules plus the checks dynamic studies need)."""
    return _validation(_get(case_id).case)


# ---- edits

class UpdateOp(BaseModel):
    """Change fields of an existing element. Nested dicts (e.g. a generator's "dyn") are merged."""
    op: Literal['update']
    kind: Literal['bus', 'branch', 'generator']
    id: str | int
    fields: dict[str, Any]


class AddBusOp(BaseModel):
    """Add a bus. id defaults to the next free integer; x/y (diagram position) default next to the network."""
    op: Literal['add_bus']
    fields: dict[str, Any] = Field(default_factory=dict,
                                   description='e.g. {"name", "type", "baseKV", "Pd", "Qd", "Vm", "id", "x", "y"}')


class AddBranchOp(BaseModel):
    """Add a line or transformer between two existing buses. id defaults to "<from>-<to>"."""
    op: Literal['add_branch']
    from_bus: str | int
    to_bus: str | int
    transformer: bool = False
    fields: dict[str, Any] = Field(default_factory=dict, description='e.g. {"r", "x", "b", "rate", "tap", "id"}')


class AddGeneratorOp(BaseModel):
    """Add a generator at a bus (a PQ bus becomes PV). id defaults to "G<bus>"."""
    op: Literal['add_generator']
    bus: str | int
    fields: dict[str, Any] = Field(default_factory=dict,
                                   description='e.g. {"Pg", "Pmax", "Pmin", "Qmax", "Qmin", "Vg", "ratedMVA", "id"}')


class DeleteOp(BaseModel):
    """Delete an element. Deleting a bus also deletes its branches and generators."""
    op: Literal['delete']
    kind: Literal['bus', 'branch', 'generator']
    id: str | int


class CaseFieldsOp(BaseModel):
    """Set case-level fields: name, baseMVA, dynamicSettings (merged: load_alpha, load_beta,
    angle_limit, f_nom, solver 'SIM'|'IDA', protection ...)."""
    op: Literal['set_case']
    fields: dict[str, Any]


EditOp = Annotated[Union[UpdateOp, AddBusOp, AddBranchOp, AddGeneratorOp, DeleteOp, CaseFieldsOp],
                   Field(discriminator='op')]

PROTECTED = {'bus': {'id'}, 'branch': {'id', 'from', 'to'}, 'generator': {'id', 'bus'}}


def _find(c, kind, eid):
    return next((r for r in c[KINDS[kind]] if str(r['id']) == str(eid)), None)


def _merge(dst: dict, src: dict):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        elif v is None:
            dst.pop(k, None)
        else:
            dst[k] = v


def _apply(c: dict, op) -> tuple[dict, str]:
    if isinstance(op, UpdateOp):
        el = _find(c, op.kind, op.id)
        if el is None:
            raise ValueError(f'No {op.kind} with id {op.id!r}. {_id_hint(c)}')
        bad = PROTECTED[op.kind] & set(op.fields)
        if bad:
            raise ValueError(f'{", ".join(sorted(bad))} of a {op.kind} cannot be changed: delete and re-add it')
        _merge(el, op.fields)
        return c, f'Updated {op.kind} {op.id}: {", ".join(op.fields)}'
    if isinstance(op, CaseFieldsOp):
        bad = set(op.fields) - {'name', 'baseMVA', 'dynamicSettings'}
        if bad:
            raise ValueError(f'set_case can change name, baseMVA and dynamicSettings, not {", ".join(sorted(bad))}')
        f = dict(op.fields)
        if 'dynamicSettings' in f:
            ds = dict(c.get('dynamicSettings') or {})
            _merge(ds, f.pop('dynamicSettings') or {})
            c['dynamicSettings'] = ds
        c.update(f)
        return c, f'Case fields set: {", ".join(op.fields)}'
    if isinstance(op, AddBusOp):
        f = dict(op.fields)
        xs = [C._num(b.get('x'), 0) for b in c['buses']]
        ys = [C._num(b.get('y'), 0) for b in c['buses']]
        x = f.pop('x', (max(xs) + 120) if xs else 400)
        y = f.pop('y', (sum(ys) / len(ys)) if ys else 300)
        new, msg, sel = E.apply_action(c, dict(type='add_bus', x=x, y=y))
        bus = new['buses'][-1]
        if 'id' in f:
            nid = f.pop('id')
            if _find(c, 'bus', nid) is not None:
                raise ValueError(f'Bus id {nid!r} already exists')
            bus['id'] = nid
            bus['name'] = f'Bus {nid}'
        _merge(bus, f)
        return new, f'Added bus {bus["id"]} ({bus["type"]}, {bus["baseKV"]} kV)'
    if isinstance(op, AddBranchOp):
        new, msg, sel = E.apply_action(c, {'type': 'add_branch', 'from': str(op.from_bus), 'to': str(op.to_bus),
                                           'kind': 'transformer' if op.transformer else 'line'})
        if new is None:
            raise ValueError(f'Cannot add a branch {op.from_bus} -> {op.to_bus}: {msg}')
        br = new['branches'][-1]
        f = dict(op.fields)
        if 'id' in f:
            nid = str(f.pop('id'))
            if _find(c, 'branch', nid) is not None:
                raise ValueError(f'Branch id {nid!r} already exists')
            br['id'] = nid
        _merge(br, f)
        return new, msg.replace(sel[1], str(br['id']), 1) if sel else msg
    if isinstance(op, AddGeneratorOp):
        new, msg, sel = E.apply_action(c, dict(type='add_device', kind='gen', bus=str(op.bus)))
        if new is None:
            raise ValueError(f'No bus {op.bus!r}')
        g = new['generators'][-1]
        f = dict(op.fields)
        if 'id' in f:
            nid = str(f.pop('id'))
            if _find(c, 'generator', nid) is not None:
                raise ValueError(f'Generator id {nid!r} already exists')
            g['id'] = nid
        _merge(g, f)
        return new, msg.replace(sel[1], str(g['id']), 1) if sel else msg
    if isinstance(op, DeleteOp):
        new, msg, _ = E.apply_action(c, dict(type='delete', kind='gen' if op.kind == 'generator' else op.kind,
                                             id=str(op.id)))
        if new is None:
            raise ValueError(f'No {op.kind} with id {op.id!r}. {_id_hint(c)}')
        return new, msg
    raise ValueError(f'Unknown operation {op!r}')


@_tool(EDITS)
def edit_case(case_id: str, operations: list[EditOp]) -> dict:
    """Apply edits to an open case, in order, all-or-nothing: if any operation fails, the case is left
    unchanged and the error names the operation. Returns what each did and the validation after.
    Ops: update, add_bus, add_branch, add_generator, delete, set_case."""
    o = _get(case_id)
    c = copy.deepcopy(o.case)
    done = []
    for k, op in enumerate(operations, 1):
        try:
            c, msg = _apply(c, op)
        except Exception as e:
            raise ValueError(f'Operation {k} ({op.op}) failed, no change made: {e}') from e
        done.append(msg)
    o.case = C.normalise(c)
    o.changed = True
    o.dyn = None
    return dict(case_id=case_id, applied=done, validation=_validation(o.case))


@_tool(EDITS)
def save_case(case_id: str,
              file_name: Annotated[str | None, Field(description='Defaults to the case name; ".json" is added')] = None,
              overwrite: bool = False) -> dict:
    """Save an open case as GridScope JSON in the shared workspace folder, where the GridScope Dynamics
    app lists it in its Case picker ("workspace / <file>"). Refuses to replace an existing file unless
    overwrite is true (re-saving a case to the file it was opened from or last saved to is allowed)."""
    o = _get(case_id)
    fname = _slug(file_name or o.case['name'])
    if not fname.endswith('.json'):
        fname += '.json'
    path = C.WORKSPACE_DIR / fname
    if path.exists() and not overwrite and o.saved_as != fname:
        raise ValueError(f'{fname} already exists in the workspace: pass overwrite=true or another file_name')
    C.WORKSPACE_DIR.mkdir(exist_ok=True)
    clean = copy.deepcopy(o.case)
    for k in KINDS.values():
        for r in clean.get(k, []):
            r.pop('_k', None)
    path.write_text(C.to_json(clean))
    o.saved_as, o.changed = fname, False
    return dict(saved=str(path), file=fname, app_hint=f'In the app: Case picker -> "workspace / {fname}" -> Load')


@_tool(READ_ONLY)
def export_case_json(case_id: str) -> str:
    """Return the case as GridScope JSON text (the format the GridScope web app and this app open)."""
    c = copy.deepcopy(_get(case_id).case)
    for k in KINDS.values():
        for r in c.get(k, []):
            r.pop('_k', None)
    return C.to_json(c)


@_tool(EDITS)
def close_case(case_id: str) -> dict:
    """Forget an open case (unsaved changes are lost)."""
    o = _CASES.pop(case_id, None)
    if o is None:
        raise ValueError(f'Unknown case_id {case_id!r}')
    return dict(closed=case_id, had_unsaved_changes=o.changed)


# ------------------------------------------------------------------ steady-state studies

def _require_valid(c):
    v = C.validate(c)
    if v['errors']:
        raise ValueError('The case has validation errors; fix them first (validate_case): '
                         + '; '.join(e['message'] for e in v['errors'][:8]))


@_tool(COMPUTE)
async def run_load_flow(case_id: str,
                        method: Literal['ac', 'dc'] = 'ac',
                        enforce_q_limits: Annotated[bool, Field(description='AC only: PV->PQ switching at Q limits')] = True,
                        detail: Annotated[Literal['summary', 'full'], Field(description=(
                            '"summary": totals, violations and extremes; "full": also every bus, branch and '
                            'generator row'))] = 'summary',
                        max_rows: int = 300) -> dict:
    """Solve a load flow with PowSyBl OpenLoadFlow (AC Newton-Raphson configured like GridScope, or DC).
    Reports convergence, losses, voltage violations (outside 0.95-1.05 pu), overloaded branches (>100 % of
    rate) and generators at their limits."""
    from gridscope import loadflow as LF
    c = _get(case_id).case
    _require_valid(c)
    if method == 'dc':
        r = await _engine(LF.run_dc, c)
        br = r['branches'].sort_values('loading', ascending=False)
        out = dict(method='dc', converged=r['converged'], status=r['status'],
                   overloaded=_table(br[br['loading'] > 100], max_rows),
                   most_loaded=_table(br.head(10)))
        if detail == 'full':
            out.update(branches=_table(r['branches'], max_rows),
                       bus_angles_deg=_clean(r['angles']))
        return out
    _, _, r = await _engine(LF.run_ac, c, enforce_q_limits)
    out = dict(method='ac', converged=r['converged'], status=r['status'], iterations=r['iterations'], log=r['log'])
    if not r['converged']:
        return out
    b, br, g = r['buses'], r['branches'], r['generators']
    out.update(
        totals=_clean(dict(generation_MW=r['total_gen'], load_MW=r['total_load'], losses_MW=r['Ploss'],
                           losses_MVAr=r['Qloss'])),
        voltage=_clean(dict(min_pu=b['Vm'].min(), min_bus=b.loc[b['Vm'].idxmin(), 'id'],
                            max_pu=b['Vm'].max(), max_bus=b.loc[b['Vm'].idxmax(), 'id'])),
        voltage_violations=_table(b[(b['Vm'] < VMIN) | (b['Vm'] > VMAX)][['id', 'name', 'type', 'Vm', 'Va']], max_rows),
        overloaded_branches=_table(br[br['loading'] > 100].sort_values('loading', ascending=False), max_rows),
        most_loaded_branches=_table(br.sort_values('loading', ascending=False).head(5)[
            ['id', 'from', 'to', 'type', 'Pf', 'Qf', 'rate', 'loading']]),
        generators_at_limits=_table(g[~g['limits'].isin(['ok', ''])][['id', 'bus', 'P', 'Q', 'limits']], max_rows),
        q_switches=_clean(r['q_switches']))
    if detail == 'full':
        out.update(buses=_table(b, max_rows), branches=_table(br, max_rows), generators=_table(g, max_rows))
    return out


@_tool(COMPUTE)
async def run_contingency_n1(case_id: str,
                             method: Annotated[Literal['dc', 'ac'], Field(description=(
                                 '"dc": thermal screening only (fast); "ac": thermal and voltage (0.95-1.05 pu)'))] = 'dc',
                             enforce_q_limits: bool = True,
                             max_rows: Annotated[int, Field(description='Rows of the outage table, worst first')] = 50) -> dict:
    """N-1 branch-outage screening with PowSyBl security analysis: every in-service branch taken out in
    turn, with islanding detection. Rows are sorted worst first (non-convergence, islanding, violations)."""
    from gridscope import contingency as N1
    c = _get(case_id).case
    _require_valid(c)
    r = await _engine(N1.run_n1, c, method, enforce_q_limits)
    return dict(method=r['method'], counts=_clean(r['counts']), outages=_table(r['table'], max_rows))


@_tool(COMPUTE)
async def run_dc_opf(case_id: str, max_rows: int = 300) -> dict:
    """DC optimal power flow (GridScope's formulation: quadratic generator costs costC2/costC1/costC0,
    branch limits = rate), solved with cvxpy/Clarabel. Returns dispatch, cost, LMPs and binding branches.
    Generators without cost data get GridScope's defaults (listed in "defaulted")."""
    from gridscope import opf
    c = _get(case_id).case
    _require_valid(c)
    r = await _engine(opf.run_dc_opf, c)
    out = {k: _clean(v) for k, v in r.items() if not isinstance(v, pd.DataFrame)}
    if r.get('ok'):
        br = r['branches']
        out.update(generators=_table(r['generators'], max_rows), buses=_table(r['buses'], max_rows),
                   binding_branches=_table(br[br['binding'] == 'Yes'], max_rows))
    return out


@_tool(COMPUTE)
async def run_fault_study(case_id: str,
                          fault_type: Literal['3ph', 'slg', 'll', 'llg'] = '3ph',
                          fault_bus: Annotated[str, Field(description='A bus id, or "all" for every bus')] = 'all',
                          standard: Literal['iec', 'ansi'] = 'iec',
                          iec_case: Literal['max', 'min'] = 'max',
                          Rf: Annotated[float, Field(description='Fault resistance, ohm')] = 0.0,
                          Xf: Annotated[float, Field(description='Fault reactance, ohm')] = 0.0,
                          hz: float = 50.0,
                          breaker_ka: Annotated[float, Field(description='Breaker rating used for the duty check')] = 40.0,
                          include_contributions: bool = False,
                          max_rows: int = 300) -> dict:
    """Short-circuit study (port of GridScope's engine: IEC 60909 with K_G/K_T corrections, or ANSI-style),
    3-phase, single-line-to-ground, line-to-line or double-line-to-ground, at one bus or all buses.
    Missing sequence data are assumed (count in "assumed")."""
    from gridscope import faults as F
    c = _get(case_id).case
    _require_valid(c)
    if fault_bus != 'all' and _find(c, 'bus', fault_bus) is None:
        raise ValueError(f'No bus {fault_bus!r}. {_id_hint(c)}')
    r = await _engine(F.run_fault_study, c, standard=standard, iec_case=iec_case, fault_type=fault_type,
                      fault_bus=fault_bus if fault_bus == 'all' else next(b['id'] for b in c['buses']
                                                                         if str(b['id']) == str(fault_bus)),
                      Rf=Rf, Xf=Xf, hz=hz, breaker_ka=breaker_ka)
    out = dict(fault_type=F.FAULT_LABELS[fault_type], standard=standard, iec_case=iec_case,
               max_Ik_kA=_clean(r['max_Ik']), max_peak_kA=_clean(r['max_peak']), max_MVA=_clean(r['max_MVA']),
               assumed=_clean(r['assumed']), notes=r['notes'], results=_table(r['results'], max_rows))
    if include_contributions:
        out['contributions'] = _table(r['contributions'], max_rows)
        out['correction_factors'] = _table(r['corrections'], max_rows)
    return out


# ------------------------------------------------------------------ dynamics

def _dyn_settings(c: dict) -> dict:
    return {**DYN_DEFAULTS, **(c.get('dynamicSettings') or {})}


@_tool(READ_ONLY)
def dynawo_status() -> dict:
    """Whether Dynawo (needed by the dynamic studies) is installed and configured, and whether a C/C++
    toolchain is there to compile governor/AVR/PSS combinations Dynawo doesn't ship."""
    from gridscope import custom_models as CM
    home = CM.dynawo_home()
    return dict(dynawo_found=home is not None, dynawo_home=str(home) if home else None,
                config=str(Path.home() / '.itools' / 'config.yml'),
                toolchain=_clean(CM.toolchain_status(home)) if home else None)


@_tool(READ_ONLY)
def list_dynamic_models(include_unverified: bool = False) -> dict:
    """What a generator's "dyn" field can select, and the event types for run_dynamic_simulation.
    Synchronous machines: dyn.model = one of synchronous_models (pre-assembled), or any
    machine x governor x AVR x PSS combination (compiled on first use). Converter/simplified sources:
    dyn.source = a source_models key. Machine data fields (H, XdPu, ...) can be set in dyn too."""
    from gridscope import custom_models as CM
    from gridscope import dynamics as D
    sync = {k: dict(machine=v['machine'], governor=v['governor'], avr=v['avr'], pss=v['pss'])
            for k, v in D.available_models(include_unverified=include_unverified).items()}
    return dict(
        event_types={
            'Bus fault': 'element = bus id; duration (s); value = fault reactance pu (0 = bolted)',
            'Line fault': 'element = branch id; location = % from the "from" end; duration; side "NONE" '
                          'leaves clearing to protection',
            'Trip branch': 'element = branch id; side "" (both ends), "ONE" or "TWO"',
            'Trip generator': 'element = generator id',
            'Load step (ΔP)': 'element = bus id; value = step pu on 100 MVA',
            'Load step (ΔQ)': 'element = bus id; value = step pu on 100 MVA',
            'Generator ΔP setpoint': 'element = generator id; value = pu on 100 MVA',
            'AVR setpoint step (ΔU)': 'element = generator id; value = pu'},
        synchronous_models=sync,
        composable={role: list(CM.composable(role, include_unverified=include_unverified))
                    for role in ('governor', 'avr', 'pss')},
        machines=list(D.MACHINES),
        source_models={k: dict(label=v['label'], kind=v['kind'])
                       for k, v in D.source_models(include_unverified=include_unverified).items()},
        source_parameters_hint='dyn.sourceParams overrides a source model\'s parameters by name',
        default_machine_data_example=_clean({k: v for k, v in D.default_dyn_params(
            dict(C.GEN_DEFAULTS, id='G', bus=1, Pmax=250.0, Pg=150.0), C.blank_case()).items()}))


class EventIn(BaseModel):
    type: Literal['Bus fault', 'Line fault', 'Trip branch', 'Trip generator', 'Load step (ΔP)', 'Load step (ΔQ)',
                  'Generator ΔP setpoint', 'AVR setpoint step (ΔU)']
    element: str | int = Field(description='Bus id, branch id or generator id (see list_dynamic_models)')
    start: float = Field(description='Event time, s')
    duration: float = Field(0.1, description='Fault duration, s (faults only)')
    value: float = Field(0.0, description='Step size, or fault reactance for faults')
    side: Literal['', 'ONE', 'TWO', 'NONE'] = ''
    location: float = Field(50.0, description='Line fault position, % from the "from" end')


QUANTITIES = ('angles', 'speed', 'voltages', 'pgen', 'qgen', 'efd', 'pm')
Quantity = Literal['angles', 'speed', 'voltages', 'pgen', 'qgen', 'efd', 'pm']
QUANTITY_UNITS = dict(angles='rotor angle vs centre of inertia, deg', speed='pu', voltages='bus voltage, pu',
                      pgen='MW', qgen='MVAr', efd='field voltage, pu', pm='mechanical power, pu of PNomTurb')


def _curves(r, quantities, elements, t_start, t_end, dt) -> dict:
    out = {}
    keep = None if not elements else {str(e) for e in elements}
    for q in quantities:
        df = getattr(r, q)
        if df is None or not len(df):
            continue
        df = df[[c for c in df.columns if keep is None or str(c) in keep]]
        df = df[(df.index >= (t_start or 0)) & (df.index <= (t_end if t_end is not None else df.index.max()))]
        if not df.shape[1] or not len(df):
            continue
        grid = np.arange(df.index.min(), df.index.max() + 1e-9, dt) if dt else df.index.values
        # sample with the value at or just before each grid time (keeps fault steps sharp)
        s = df[~df.index.duplicated(keep='last')].reindex(grid, method='ffill')
        s.index.name = 't'
        out[q] = dict(unit=QUANTITY_UNITS[q], **_table(s.reset_index()))
    return out


def _dyn_report(r, case, events) -> dict:
    return dict(
        status=r.status, status_text=r.status_text, stable=r.stable, verdict=r.reason,
        metrics=_clean(r.metrics), islanded_buses=r.islanded_buses, lost_generators=r.lost_gens,
        protection_operations=_table(r.protection, 100),
        timeline=_table(r.timeline, 100), notes=r.notes,
        events=[e.__dict__ for e in events])


@_tool(COMPUTE)
async def run_dynamic_simulation(
        case_id: str,
        events: list[EventIn],
        stop_time: Annotated[float, Field(description='Simulation end, s')] = 10.0,
        curves: Annotated[list[Quantity], Field(description='Time series to return (sampled every sample_dt)')] = ['angles'],  # noqa: B006
        curve_elements: Annotated[list[str] | None, Field(description='Only these generator / bus ids in the curves')] = None,
        sample_dt: Annotated[float, Field(description='Curve sampling interval, s')] = 0.1,
        solver: Literal['SIM', 'IDA'] | None = None, angle_limit: float | None = None,
        load_alpha: float | None = None, load_beta: float | None = None) -> dict:
    """Time-domain (transient stability) simulation with Dynawo. The network is initialised from an AC load
    flow; events disturb it (faults, trips, load / set-point steps). Returns the stability verdict, metrics
    (max rotor-angle separation, TSI, min post-fault voltage, frequency extremes, damping), protection
    operations, islanding, and sampled curves. Settings default to the case's dynamicSettings.
    The full result stays available to get_dynamic_curves until the case is edited."""
    from gridscope import dynamics as D
    o = _get(case_id)
    c = o.case
    _require_valid(c)
    s = _dyn_settings(c)
    evs = [D.Event(type=e.type, element=str(e.element), start=e.start, duration=e.duration, value=e.value,
                   side=e.side, location=e.location) for e in events]
    problems = D.validate_events(evs, c, None, stop_time)
    if problems:
        raise ValueError('Invalid events: ' + '; '.join(problems) + ' ' + _id_hint(c))
    r = await _engine(D.simulate, c, evs, stop_time, load_alpha if load_alpha is not None else s['load_alpha'],
                      load_beta if load_beta is not None else s['load_beta'],
                      angle_limit if angle_limit is not None else s['angle_limit'],
                      solver=solver or s['solver'])
    o.dyn, o.dyn_events = r, evs
    out = _dyn_report(r, c, evs)
    out['curves'] = _curves(r, curves, curve_elements, None, None, sample_dt)
    return out


@_tool(READ_ONLY)
def get_dynamic_curves(case_id: str,
                       quantities: list[Quantity],
                       elements: Annotated[list[str] | None, Field(description='Generator / bus ids; all if omitted')] = None,
                       t_start: float | None = None, t_end: float | None = None,
                       sample_dt: Annotated[float | None, Field(description='s; omit for every solver step')] = 0.05) -> dict:
    """More time series from the last run_dynamic_simulation of this case, without re-running it."""
    o = _get(case_id)
    if o.dyn is None:
        raise ValueError('No dynamic result for this case: run run_dynamic_simulation first '
                         '(results are cleared when the case is edited)')
    return dict(curves=_curves(o.dyn, quantities, elements, t_start, t_end, sample_dt),
                available={q: [str(x) for x in getattr(o.dyn, q).columns] for q in QUANTITIES
                           if len(getattr(o.dyn, q))})


@_tool(COMPUTE)
async def critical_clearing_time(
        case_id: str,
        fault_bus: Annotated[str, Field(description='Bus with the bolted 3-phase fault')],
        trip_branch: Annotated[str | None, Field(description='Branch tripped when the fault clears (optional)')] = None,
        t_low: float = 0.02, t_high: float = 0.5,
        tol: Annotated[float, Field(description='Bisection tolerance, s')] = 0.01,
        post_time: Annotated[float, Field(description='Simulated time after clearing, s')] = 4.0,
        solver: Literal['SIM', 'IDA'] | None = None,
        ctx: Context | None = None) -> dict:
    """Critical clearing time of a bolted 3-phase bus fault, by bisection on the clearing time
    (each step is a Dynawo run). Reports progress after each run."""
    from gridscope import dynamics as D
    c = _get(case_id).case
    _require_valid(c)
    if _find(c, 'bus', fault_bus) is None:
        raise ValueError(f'No bus {fault_bus!r}. {_id_hint(c)}')
    if trip_branch and _find(c, 'branch', trip_branch) is None:
        raise ValueError(f'No branch {trip_branch!r}. {_id_hint(c)}')
    s = _dyn_settings(c)
    runs_max = 2 + math.ceil(math.log2(max((t_high - t_low) / tol, 1)))
    prog = _progress_bridge(ctx, runs_max, lambda k, tc, ok: (k, f'run {k}: cleared at {tc * 1000:.0f} ms -> '
                                                                  f'{"stable" if ok else "unstable"}'))
    r = await _engine(D.critical_clearing_time, c, str(fault_bus), trip_branch, t_low=t_low, t_high=t_high,
                      tol=tol, post_time=post_time, load_alpha=s['load_alpha'], load_beta=s['load_beta'],
                      angle_limit=s['angle_limit'], progress=prog, solver=solver or s['solver'])
    return dict(cct_s=_clean(r['cct']), stable_up_to_s=_clean(r['low']), unstable_from_s=_clean(r['high']),
                message=r['message'], runs=_clean(r['runs']))


@_tool(COMPUTE)
async def screen_dynamic_contingencies(
        case_id: str,
        branch_ids: Annotated[list[str] | None, Field(description='Branches to screen; all in service if omitted')] = None,
        clearing_time: Annotated[float, Field(description='Fault clearing time, s')] = 0.1,
        fault_end: Literal['from', 'to'] = 'from',
        post_time: float = 5.0,
        solver: Literal['SIM', 'IDA'] | None = None,
        max_rows: int = 100,
        ctx: Context | None = None) -> dict:
    """Dynamic N-1: for each branch, a bolted 3-phase fault at one end cleared after clearing_time by
    tripping the branch (one Dynawo run per branch; reports progress). Sorted unstable first, then by TSI."""
    from gridscope import dynamics as D
    c = _get(case_id).case
    _require_valid(c)
    ids = [str(b['id']) for b in c['branches'] if C._num(b.get('status'), 1)] if branch_ids is None else \
        [str(b) for b in branch_ids]
    known = {str(b['id']) for b in c['branches']}
    missing = [b for b in ids if b not in known]
    if missing:
        raise ValueError(f'Unknown branch id(s): {", ".join(missing)}. {_id_hint(c)}')
    by_str = {str(b['id']): b['id'] for b in c['branches']}
    s = _dyn_settings(c)
    prog = _progress_bridge(ctx, len(ids), lambda k, n, bid, ok: (k, f'{k}/{n}: {bid} {"stable" if ok else "UNSTABLE"}'))
    df = await _engine(D.screen_contingencies, c, [by_str[b] for b in ids], clearing_time=clearing_time,
                       fault_end=fault_end, post_time=post_time, load_alpha=s['load_alpha'],
                       load_beta=s['load_beta'], angle_limit=s['angle_limit'], progress=prog,
                       solver=solver or s['solver'])
    counts = df['verdict'].value_counts().to_dict() if len(df) else {}
    return dict(counts=_clean(counts), results=_table(df, max_rows))


# ------------------------------------------------------------------ resources

@SERVER.resource('gridscope://case/{case_id}', name='case', title='Open case (GridScope JSON)',
                 mime_type='application/json')
def case_resource(case_id: str) -> str:
    """An open case as GridScope JSON."""
    return export_case_json(case_id)


def main():
    SERVER.run('stdio')


if __name__ == '__main__':
    main()
