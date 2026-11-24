"""Shared UI plumbing: the pypowsybl worker thread, case/result state, theme helpers."""
from __future__ import annotations

import copy
import math
import queue
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from gridscope import case as C

# GridScope palette
BG, PANEL, PANEL2, LINE = '#0b1020', '#121a2e', '#17223b', '#283754'
TEXT, MUTED, ACCENT, ACCENT2 = '#eef4ff', '#9fb0ca', '#4ea1ff', '#2dd4bf'
WARN, BAD, GOOD, SLATE = '#f59e0b', '#fb7185', '#34d399', '#94a3b8'
SERIES = ['#4ea1ff', '#f59e0b', '#2dd4bf', '#fb7185', '#a78bfa', '#facc15', '#34d399', '#f472b6',
          '#60a5fa', '#fb923c', '#22d3ee', '#c084fc']


# ------------------------------------------------------- pypowsybl thread
# pypowsybl's native library can segfault when called from Streamlit's short-lived
# script threads, so every pypowsybl call goes through ONE long-lived worker thread.

@st.cache_resource
def _executor():
    return ThreadPoolExecutor(max_workers=1, thread_name_prefix='pypowsybl')


def on_pp_thread(fn, *args, **kwargs):
    return _executor().submit(fn, *args, **kwargs).result()


def on_pp_thread_with_progress(fn, on_update, *args, **kwargs):
    """Run fn(..., progress=callback) on the worker thread; on_update(*msg) runs here, in the
    script thread, for every progress message (Streamlit elements can only be updated from here)."""
    q: queue.Queue = queue.Queue()
    fut = _executor().submit(fn, *args, progress=lambda *a: q.put(a), **kwargs)
    while True:
        try:
            on_update(*q.get(timeout=0.1))
        except queue.Empty:
            if fut.done():
                break
    while not q.empty():
        on_update(*q.get())
    return fut.result()


# --------------------------------------------------------------- state

def init_state():
    ss = st.session_state
    if 'case' not in ss:
        set_case(C.load_builtin('IEEE 14-Bus'))
    ss.setdefault('view', 'network')
    ss.setdefault('enforce_q', True)
    ss.setdefault('status', 'Ready')


def set_case(case: dict):
    ss = st.session_state
    ss.case = C.normalise(case)
    ss.case_version = ss.get('case_version', 0) + 1
    ss.case_load = ss.get('case_load', 0) + 1      # a different network: the diagram editor re-fits, undo clears
    ss.sld_undo = []
    ss.inspect = None
    ss.results = {}
    ss.status = f'Loaded {ss.case["name"]}'
    for k in [k for k in ss.keys() if str(k).startswith('ed_')]:
        del ss[k]


def case() -> dict:
    return st.session_state.case


def touch_case(new_case: dict, msg='Model edited'):
    """Replace the case after an edit: results computed on the old version become stale."""
    ss = st.session_state
    ss.case = new_case
    ss.case_version += 1
    ss.status = msg


def remember(key: str, value):
    """Store a study setup (events, stop time, ...) in case['dynamicSettings'] so it is saved with the
    network. Quiet: the case version does not change, so results are not marked out of date."""
    ds = st.session_state.case.setdefault('dynamicSettings', {})
    if ds.get(key) != value:
        ds[key] = value


def recalled(key: str, default=None):
    return (st.session_state.case.get('dynamicSettings') or {}).get(key, default)


def put_result(key, value):
    st.session_state.results[key] = dict(value=value, version=st.session_state.case_version, at=time.time())


def get_result(key):
    r = st.session_state.results.get(key)
    if r is None:
        return None, False
    return r['value'], r['version'] != st.session_state.case_version


def stale_note(stale: bool):
    if stale:
        st.warning('The model has changed since these results were computed — re-run to update them.', icon='⚠️')


def validation():
    return C.validate(case())


# --------------------------------------------------------------- widgets

def card_header(title: str, badge: str | None = None):
    b = f'<span class="gs-badge">{badge}</span>' if badge else ''
    st.markdown(f'<div class="gs-head"><h3>{title}</h3>{b}</div>', unsafe_allow_html=True)


def notice(text: str):
    st.markdown(f'<div class="gs-notice">{text}</div>', unsafe_allow_html=True)


def metric_cards(items: list[tuple[str, str, str | None]]):
    """items: (label, value, css class among ok/bad/warn/None)"""
    cols = st.columns(len(items))
    for col, (label, value, cls) in zip(cols, items):
        col.markdown(f'<div class="gs-metric"><div class="label">{label}</div>'
                     f'<div class="value {cls or ""}">{value}</div></div>', unsafe_allow_html=True)


def csv_button(df: pd.DataFrame, name: str, label='Export CSV', key=None):
    st.download_button(label, df.to_csv(index=False).encode(), file_name=name, mime='text/csv',
                       key=key or f'csv_{name}', type='secondary')


def plotly_layout(fig: go.Figure, height=320, title=None, legend=True):
    fig.update_layout(
        template='plotly_dark', height=height, paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color=MUTED, size=12), margin=dict(l=10, r=10, t=36 if title else 10, b=10),
        title=dict(text=title, font=dict(color=TEXT, size=14), x=0, xanchor='left') if title else None,
        showlegend=legend, legend=dict(orientation='h', y=1.02, yanchor='bottom', x=1, xanchor='right',
                                       bgcolor='rgba(0,0,0,0)'),
        hoverlabel=dict(bgcolor=PANEL2, bordercolor=LINE, font_color=TEXT))
    fig.update_xaxes(gridcolor='#1c2840', zerolinecolor=LINE, linecolor=LINE)
    fig.update_yaxes(gridcolor='#1c2840', zerolinecolor=LINE, linecolor=LINE)
    return fig


def fmt(v, nd=2, unit=''):
    try:
        if v is None or pd.isna(v):
            return '—'
        return f'{v:,.{nd}f}{unit}'
    except (TypeError, ValueError):
        return str(v)


# ----------------------------------------------------- editable tables

def edit_records(records: list[dict], columns: list[str], column_config: dict, key: str, defaults: dict,
                 new_id=None, height=520):
    """data_editor over a list of dicts. Hidden fields are preserved; returns the new list or None if
    nothing changed."""
    # row keys must be the same on every rerun: if the editor's input data changes between reruns,
    # Streamlit discards the user's pending edit (records of a freshly opened file carry no key yet)
    for i, r in enumerate(records):
        r.setdefault('_k', f'row{i}')
    df = pd.DataFrame([{**{c: r.get(c) for c in columns}, '_k': r['_k']} for r in records],
                      columns=columns + ['_k'])
    cfg = dict(column_config)
    cfg['_k'] = None
    edited = st.data_editor(df, column_config=cfg, num_rows='dynamic', hide_index=True, width='stretch',
                            height=height, key=key)
    by_k = {r['_k']: r for r in records}
    out = []
    for i, row in edited.iterrows():
        k = row.get('_k')
        base = copy.deepcopy(by_k[k]) if isinstance(k, str) and k in by_k else \
            {**copy.deepcopy(defaults), '_k': uuid.uuid4().hex}
        for c in columns:
            v = row[c]
            v = v.item() if hasattr(v, 'item') else v
            if _blank(v):
                if c not in base and c in defaults:      # new row: fill from the defaults
                    base[c] = defaults[c]
                continue                                 # blank cell: keep what the record had
            if not _same(v, base.get(c)):
                base[c] = _like(v, base.get(c))
        if new_id and base.get('id') in (None, ''):
            base['id'] = new_id(out, base)
        out.append(base)
    strip = lambda rs: [{k: v for k, v in r.items() if k != '_k'} for r in rs]   # noqa: E731
    return out if strip(out) != strip(records) else None


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or (isinstance(v, str) and not v.strip())


def _same(a, b) -> bool:
    """Editor value equals stored value, ignoring int/float/str representation (1 == 1.0 == '1')."""
    if _blank(a) and _blank(b):
        return True
    try:
        return abs(float(a) - float(b)) <= 1e-12 * max(1.0, abs(float(b)))
    except (TypeError, ValueError):
        return str(a) == str(b)


def _like(v, old):
    """Keep the stored type where possible (a bus number stays a number)."""
    if isinstance(old, (int, float)) and not isinstance(old, bool) and isinstance(v, str):
        try:
            f = float(v)
            return int(f) if isinstance(old, int) and f.is_integer() else f
        except ValueError:
            return v
    return v


def clean_case_for_export(c: dict) -> dict:
    out = copy.deepcopy(c)
    for k in ('buses', 'branches', 'generators'):
        for r in out.get(k, []):
            r.pop('_k', None)
    return out


def slug(s: str) -> str:
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', s).strip('_') or 'network'


def dynawo_status():
    cfg = Path.home() / '.itools' / 'config.yml'
    if not cfg.exists():
        return False, f'No Dynawo config at {cfg}'
    m = re.search(r'homeDir:\s*(\S+)', cfg.read_text())
    if not m:
        return False, 'homeDir missing in ~/.itools/config.yml'
    home = Path(m.group(1)).expanduser()
    if not (home / 'dynawo.sh').exists():
        return False, f'dynawo.sh not found in {home}'
    return True, str(home)
