"""Protection settings for dynamic studies: generators, lines, transformers and loads."""
from __future__ import annotations

import copy

import pandas as pd
import streamlit as st

from gridscope import dynamics as D
from gridscope import protection as PR
from gridscope.case import _num, is_transformer
from views import common as U

KIND_TITLE = {'generators': 'Generators', 'lines': 'Lines', 'transformers': 'Transformers', 'loads': 'Loads'}
KIND_NOTE = {
    'generators': 'Dynawo\'s speed (over/under frequency), under-voltage (UVA), over-voltage (OVA) and loss-of-'
                  'synchronism protections, combined into one relay that trips the machine. Synchronous machines '
                  'only; the relay is compiled into the machine model (about a minute, once per control combination).',
    'lines': 'Dynawo\'s two-zone quadrilateral distance relay at each end (reaches in % of the line impedance; the '
             'zone delay is followed by the breaker time) plus a definite-time overcurrent relay. A trip at either '
             'end opens the line. Any line protection makes Dynawo model the whole network in Modelica (lines, '
             'transformers, buses; shunts become constant-impedance loads), so runs are slower; cases with '
             'phase-shifting transformers are not supported.',
    'transformers': 'Dynawo\'s current limit automaton: opens the transformer when its current stays above the '
                    'pick-up for the delay. Pick-ups are in % of the rating (Rate MVA), or of the pre-disturbance '
                    'loading when the transformer has no rating.',
    'loads': 'Under-frequency load shedding (Dynawo UFLS, three steps, measuring the system frequency) and under-'
             'voltage load shedding at the bus. A protected load uses the α/β voltage-dependent model.',
}


def _elements(c, kind):
    """[(id, description)] of the in-service elements of one kind."""
    if kind == 'generators':
        out = []
        for g in c['generators']:
            if not _num(g.get('status'), 1):
                continue
            d = D.default_dyn_params(g, c)
            tech = 'synchronous' if D.is_sync(d) else D.SOURCE_LIB['sources'][d['source']]['kind'].lower()
            out.append((str(g['id']), f'bus {g["bus"]}, {tech}'))
        return out
    if kind in ('lines', 'transformers'):
        want = 'line' if kind == 'lines' else 'transformer'
        out = []
        for b in c['branches']:
            if not _num(b.get('status'), 1):
                continue
            if ('transformer' if is_transformer(b, c) else 'line') == want:
                rate = _num(b.get('rate'), 0)
                out.append((str(b['id']), f'{b["from"]}–{b["to"]}' + (f', {rate:g} MVA' if rate else ', no rating')))
        return out
    return [(str(b['id']), f'{_num(b.get("Pd"), 0):g} MW') for b in c['buses']
            if abs(_num(b.get('Pd'), 0)) + abs(_num(b.get('Qd'), 0)) > 0]


def _editor(c, kind):
    cur = PR.settings(c)[kind]
    cols = PR.COLUMNS[kind]
    els = _elements(c, kind)
    if not els:
        st.caption(f'No {KIND_TITLE[kind].lower()} in this case.')
        return
    rows = []
    for eid, desc in els:
        cfg = cur.get(eid, dict(PR.DEFAULTS[kind]))
        rows.append({'Element': eid, 'Description': desc, **{k: cfg[k] for k in cols}})
    df = pd.DataFrame(rows)
    cfg_cols = {'Element': st.column_config.TextColumn(disabled=True, width='small'),
                'Description': st.column_config.TextColumn(disabled=True)}
    for k, (label, help_) in cols.items():
        if isinstance(PR.DEFAULTS[kind][k], bool):
            cfg_cols[k] = st.column_config.CheckboxColumn(label, help=help_, width='small')
        else:
            cfg_cols[k] = st.column_config.NumberColumn(label, help=help_, format='%g', min_value=0.0)
    ver = st.session_state.case_version
    b1, b2, b3 = st.columns([1.2, 1.2, 3])
    sw = PR.SWITCHES[kind]
    if b1.button('Turn all on', key=f'prot_all_{kind}', width='stretch',
                 help='Switch every protection function on for every element (current settings kept)'):
        _save(c, kind, {eid: {**cur.get(eid, PR.DEFAULTS[kind]), **{s: True for s in sw}} for eid, _ in els})
    if b2.button('Turn all off', key=f'prot_none_{kind}', width='stretch'):
        _save(c, kind, {eid: {**cur.get(eid, PR.DEFAULTS[kind]), **{s: False for s in sw}} for eid, _ in els})
    ed = st.data_editor(df, hide_index=True, width='stretch', num_rows='fixed', key=f'ed_prot_{kind}_{ver}',
                        column_config=cfg_cols, height=min(440, 38 + 35 * len(df)))
    if not ed.equals(df):
        new = {}
        for _, r in ed.iterrows():
            cfg = {k: (bool(r[k]) if isinstance(PR.DEFAULTS[kind][k], bool) else
                       float(r[k]) if pd.notna(r[k]) else PR.DEFAULTS[kind][k]) for k in cols}
            if cfg != PR.DEFAULTS[kind]:
                new[str(r['Element'])] = cfg
        _save(c, kind, new)
    n_on = sum(PR.active(cur.get(eid, {}), kind) for eid, _ in els if eid in cur)
    b3.caption(f'{n_on} of {len(els)} {KIND_TITLE[kind].lower()} with protection in service.')


def _save(c, kind, values):
    new = copy.deepcopy(c)
    st_ = dict(new.get('dynamicSettings') or {})
    prot = dict(st_.get('protection') or {})
    prot[kind] = {k: v for k, v in values.items()
                  if v != PR.DEFAULTS[kind] or PR.active(v, kind)}
    st_['protection'] = prot
    new['dynamicSettings'] = st_
    U.touch_case(new, f'{KIND_TITLE[kind]} protection settings changed')
    st.rerun()


def _compile_status(c):
    from gridscope import custom_models as CM
    s = PR.settings(c)
    need = []
    if any(PR.active(v, 'lines') for v in s['lines'].values()):
        need += ['GsLineProtected', 'GsNetworkTransformer']
    if any(PR.active(v, 'loads') for v in s['loads'].values()):
        need += ['GsLoadShedding', 'GsLoadSheddingLocal']
    for g in c['generators']:
        cfg = s['generators'].get(str(g['id']))
        if cfg and PR.active(cfg, 'generators') and _num(g.get('status'), 1):
            d = D.default_dyn_params(g, c)
            if D.is_sync(d):
                try:
                    need.append(CM.with_protection(d['model']))
                except ValueError as e:
                    st.warning(f'Generator {g["id"]}: {e}')
    need = sorted(set(need))
    todo = [n for n in need if not CM.is_compiled(n)]
    if not need:
        return
    if not todo:
        st.caption('Protection models: ✔ compiled (' + ', '.join(f'`{n}`' for n in need) + ').')
        return
    tc = CM.toolchain_status()
    if not tc['ok']:
        st.error('These protection models must be compiled first, but: ' + ' '.join(tc['issues']))
        return
    st.info('Protection models to compile (once, about a minute each; also done automatically before the first '
            'run): ' + ', '.join(f'`{n}`' for n in todo), icon='🛠')
    if st.button('Compile now', key='prot_compile'):
        with st.status('Compiling protection models with Dynawo…') as stat:
            try:
                res = CM.compile_models(todo)
            except RuntimeError as e:
                res = {'': str(e)}
            bad = {k: v for k, v in res.items() if v}
            if bad:
                stat.update(label='Compilation failed', state='error')
                for v in bad.values():
                    st.error(v)
            else:
                stat.update(label='Compiled', state='complete')
                st.rerun()


def protection_tab(c):
    with st.container(border=True):
        U.card_header('Protection', 'relays and automata modelled in the dynamic simulation')
        U.notice('Switch protection functions on per element and set their thresholds and delays. They act in '
                 'every dynamic study (simulation, critical clearing time, N-1 screening); what operated is listed '
                 'with the results and in the Dynawo timeline. Tripped machines and islanded buses are left out of '
                 'the stability verdict.')
        for msg in PR.validate(c):
            st.warning(msg)
        _compile_status(c)
        tabs = st.tabs([KIND_TITLE[k] for k in PR.DEFAULTS])
        for tab, kind in zip(tabs, PR.DEFAULTS):
            with tab:
                st.caption(KIND_NOTE[kind])
                _editor(c, kind)


def operations_table(r):
    ops = getattr(r, 'protection', None)
    if ops is None or not len(ops):
        return
    with st.container(border=True):
        U.card_header('Protection operations')
        st.dataframe(ops[['time', 'element', 'protection', 'action']].rename(columns=dict(
            time='Time s', element='Element', protection='Protection', action='Action')),
            hide_index=True, width='stretch', height=min(320, 38 + 35 * len(ops)))
