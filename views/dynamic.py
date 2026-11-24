"""Dynamic Stability view: machine data, time-domain simulation, CCT search, dynamic N-1 screening."""
from __future__ import annotations

import copy

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from gridscope import dynamics as D
from gridscope.case import _num
from views import common as U
from views import protection as P

SETTINGS_DEFAULT = dict(load_alpha=2.0, load_beta=2.0, angle_limit=180.0, f_nom=50.0, solver='SIM')


def _settings(c):
    s = dict(SETTINGS_DEFAULT)
    s.update(c.get('dynamicSettings') or {})
    return s


def _save_settings(c, **kw):
    s = _settings(c)
    if any(s.get(k) != v for k, v in kw.items()):
        s.update(kw)
        U.touch_case({**U.case(), 'dynamicSettings': s}, 'Dynamic settings changed')
    for k, v in kw.items():          # written even when equal to the default, so the saved file is explicit
        U.remember(k, v)


def view_dynamics():
    c = U.case()
    ok, where = U.dynawo_status()
    with st.container(border=True):
        h1, h2 = st.columns([3, 2])
        with h1:
            U.card_header('Dynamic & Transient Stability (Dynawo)', 'time-domain RMS simulation')
        with h2:
            st.markdown(f'<div style="text-align:right" class="{"valOk" if ok else "valErr"}">'
                        f'{"● Dynawo: " + where if ok else "● " + where}</div>', unsafe_allow_html=True)
        U.notice('Each in-service generator is a Dynawo synchronous machine with your choice of governor, AVR and '
                 'PSS, or a converter-interfaced plant (solar PV, wind, battery storage, grid-forming converter); loads '
                 'use the Dynawo load model chosen under <b>Machine &amp; load models</b>. The simulation starts '
                 'from the AC load-flow solution of this case. Rotor angles are shown relative to the inertia-weighted '
                 'centre of angle of the synchronous machines; a case is <b>unstable</b> when the angle separation '
                 'between any two machines that are still connected exceeds the limit (loss of synchronism) or the '
                 'simulation cannot continue.')
    if not ok:
        st.error('Dynawo is not configured. Create `~/.itools/config.yml` containing\n\n'
                 '```yaml\ndynawo:\n  homeDir: /path/to/the/folder/with/dynawo.sh\n  debug: false\n```\n'
                 'then restart the app.')
        return
    if U.validation()['errors']:
        st.error('Fix the model validation errors (sidebar) before running dynamic studies.')
        return
    t_mach, t_prot, t_sim, t_cct, t_scr = st.tabs(['⚙ Machine & load models', '🛡 Protection',
                                                   '▶ Time-domain simulation', '⏱ Critical clearing time',
                                                   '⛒ Dynamic N-1 screening'])
    with t_mach:
        _machine_tab(c)
    with t_prot:
        P.protection_tab(c)
    with t_sim:
        _simulation_tab(c)
    with t_cct:
        _cct_tab(c)
    with t_scr:
        _screening_tab(c)


# ------------------------------------------------------------- machines

def _machine_tab(c):
    s = _settings(c)
    with st.container(border=True):
        U.card_header('Study settings')
        g = st.columns(5)
        a = g[0].number_input('Load exponent α (P ∝ U^α)', 0.0, 3.0, float(s['load_alpha']), 0.5,
                              help='2 = constant impedance (classical transient-stability assumption), '
                                   '1 = constant current, 0 = constant power. Exponents below 2 can make the solver '
                                   'fail for bolted faults at a load bus.')
        b = g[1].number_input('Load exponent β (Q ∝ U^β)', 0.0, 3.0, float(s['load_beta']), 0.5)
        lim = g[2].number_input('Loss-of-synchronism angle limit (°)', 90.0, 360.0, float(s['angle_limit']), 10.0)
        f = g[3].selectbox('System frequency', [50.0, 60.0], index=0 if s['f_nom'] == 50 else 1,
                           format_func=lambda x: f'{x:.0f} Hz', help='Used to show rotor speed in Hz.')
        solver = g[4].selectbox('Solver', list(D.SOLVERS), index=list(D.SOLVERS).index(s['solver']),
                                format_func=lambda k: D.SOLVERS[k],
                                help='The fixed-step solver is robust through faults with every verified control '
                                     'model; IDA is variable-step and can fail at faults with the IEEE controllers. '
                                     'On IEEE 9 they agree within ~1 % on critical clearing time.')
        _save_settings(c, load_alpha=a, load_beta=b, angle_limit=lim, f_nom=f, solver=solver)

    c = U.case()
    _machine_table(c)
    _controls_editor(U.case())
    _load_models_editor(U.case())


def _machine_table(c):
    gens = [g for g in c['generators'] if _num(g.get('status'), 1)]
    bench, desc = D.benchmark_machine_data(c)
    with st.container(border=True):
        h1, h2, h3 = st.columns([3, 1.3, 1.3])
        with h1:
            U.card_header('Generator data', 'own MVA base; H and reactances apply to synchronous machines')
        if h2.button('Reset to typical values', width='stretch',
                     help='Clears machine data and controls for every generator.'):
            new = copy.deepcopy(c)
            for g in new['generators']:
                g.pop('dyn', None)
            U.touch_case(new, 'Machine data reset to typical values')
            st.rerun()
        if h3.button('Load benchmark data', width='stretch', disabled=not bench,
                     help=desc or 'Benchmark machine data are available for the IEEE 9-bus (WSCC) and 39-bus '
                                  '(New England) systems.'):
            new = copy.deepcopy(c)
            for g in new['generators']:
                if g['id'] in bench:
                    keep = {k: v for k, v in (g.get('dyn') or {}).items()
                            if k in ('controls', 'source', 'sourceParams')}
                    g['dyn'] = {**dict(bench[g['id']]), **keep}
            U.touch_case(new, 'Benchmark machine data loaded')
            st.rerun()
        if bench:
            st.caption(f'Benchmark available: {desc}')
        U.notice('Missing values use typical data (H = 5.4 s, X<sub>d</sub> = 2.22, X′<sub>d</sub> = 0.384, '
                 'X″<sub>d</sub> = 0.264 pu…) on a rating of <code>ratedMVA</code>, else Pmax/0.9. Reactances are on '
                 'the machine\'s own base. The machine type (round rotor / salient pole) and the governor, AVR and '
                 'PSS (or a converter model) are chosen in <b>Generator models</b> below. Saved in the network JSON as <code>generator.dyn</code>.')
        rows = []
        for g in gens:
            d = D.default_dyn_params(g, c)
            if D.is_sync(d):
                info = D.CONTROL_LIB['models'][d['model']]
                machine, ctl = info['machine'], _controls_summary(info)
            else:
                src = D.SOURCE_LIB['sources'][d['source']]
                machine, ctl = src['kind'], src['label']
            rows.append(dict(id=g['id'], bus=str(g['bus']), Pg=_num(g['Pg'], 0), machine=machine,
                             controls=ctl, **{k: d.get(k) for k in D.DYN_COLUMNS}))
        df = pd.DataFrame(rows)
        num = lambda label, fmt='%.4g', **kw: st.column_config.NumberColumn(label, format=fmt, **kw)   # noqa: E731
        cfg = {'id': st.column_config.TextColumn('Generator', disabled=True),
               'bus': st.column_config.TextColumn('Bus', disabled=True), 'Pg': num('PG MW', disabled=True),
               'machine': st.column_config.TextColumn('Technology', disabled=True, width='medium'),
               'controls': st.column_config.TextColumn('Model / controls', disabled=True, width='large'),
               'SNom': num('Rating MVA', min_value=1.0), 'H': num('H s', min_value=0.1),
               'XdPu': num('Xd'), 'XpdPu': num("X'd"), 'XppdPu': num("X''d"), 'XqPu': num('Xq'),
               'Tpd0': num("T'd0 s"), 'Tppd0': num("T''d0 s")}
        edited = st.data_editor(df, column_config=cfg, hide_index=True, width='stretch', num_rows='fixed',
                                key=f'ed_dyn_{st.session_state.case_version}', height=min(460, 38 + 35 * len(df)))
        if not edited.equals(df):
            new = copy.deepcopy(c)
            by_id = {g['id']: g for g in new['generators']}
            for _, row in edited.iterrows():
                g = by_id[row['id']]
                dyn = dict(g.get('dyn') or {})
                old = df[df.id == row['id']].iloc[0]
                for k in D.DYN_COLUMNS:
                    if row[k] != old[k]:
                        dyn[k] = float(row[k])
                g['dyn'] = dyn
            bad = [r['id'] for _, r in edited.iterrows() if not (r['XppdPu'] < r['XpdPu'] < r['XdPu'])]
            if bad:
                st.warning('Expected X″d < X′d < Xd for: ' + ', '.join(bad))
            U.touch_case(new, 'Machine data edited')
            st.rerun()


def _controls_summary(info):
    parts = [f'Gov: {info["governor"].split(" (")[0]}', f'AVR: {info["avr"].split(" (")[0]}',
             f'PSS: {info["pss"].split(" (")[0]}']
    if info.get('limiter', 'None') != 'None':
        parts.append(f'Lim: {info["limiter"]}')
    return ' · '.join(parts)


def _param_editor(rows, key, source_note=''):
    """Editable parameter table; returns {key: (value, type)} of changed cells (validated)."""
    df = pd.DataFrame([dict(Parameter=r['name'], Value='auto' if r['bind'] else ('' if r['value'] in (None, 'None') else r['value']),
                            Default='auto (= ' + r['bind'] + ')' if r['bind'] else
                            (f"{float(r['value']):.4g} (= {r['default']} pu on the plant rating)" if r.get('scaled_to_rating')
                             else ('—' if r['default'] in (None, 'None') else r['default'])),
                            Type=r['type'].lower(), Description=r.get('doc', ''), key=r['key'],
                            locked=bool(r['bind'])) for r in rows])
    if source_note:
        st.caption(f'Defaults from: {source_note}')
    missing = [r['name'] for r in rows if not r['bind'] and r['value'] in (None, '', 'None')]
    if missing:
        st.warning('Dynawo has no reference value for: ' + ', '.join(missing) + ' — enter them before simulating.')
    ed = st.data_editor(df, hide_index=True, width='stretch', num_rows='fixed', key=key,
                        height=min(520, 38 + 35 * len(df)),
                        column_order=['Parameter', 'Value', 'Default', 'Type', 'Description'],
                        column_config={
                            'Parameter': st.column_config.TextColumn(disabled=True),
                            'Value': st.column_config.TextColumn(
                                help='Edit to override the default. "auto" values are set from the generator data '
                                     '(rating, nominal power, limits) and cannot be edited.'),
                            'Default': st.column_config.TextColumn(disabled=True),
                            'Type': st.column_config.TextColumn(disabled=True, width='small'),
                            'Description': st.column_config.TextColumn(disabled=True, width='large')})
    changed, errs = {}, []
    for (_, a), (_, b) in zip(df.iterrows(), ed.iterrows()):
        if str(a['Value']) != str(b['Value']) and not a['locked']:
            v = str(b['Value']).strip()
            try:
                D._fmt(a['Type'].upper(), v)
            except (TypeError, ValueError):
                errs.append(f'{a["Parameter"]}: "{v}" is not a valid {a["Type"]}')
                continue
            changed[a['key']] = v
    for e in errs:
        st.error(e)
    return changed


def _apply_overrides(c, gid, field, changed, defaults, msg):
    new = copy.deepcopy(c)
    gg = next(x for x in new['generators'] if x['id'] == gid)
    dyn = dict(gg.get('dyn') or {})
    ov = dict(dyn.get(field) or {})
    for k, v in changed.items():
        if v == str(defaults.get(k)):
            ov.pop(k, None)
        else:
            ov[k] = v
    dyn[field] = ov
    gg['dyn'] = dyn
    U.touch_case(new, msg)
    st.rerun()


def _set_dyn(c, gid, msg, **kw):
    new = copy.deepcopy(c)
    gg = next(x for x in new['generators'] if x['id'] == gid)
    dyn = dict(gg.get('dyn') or {})
    for k, v in kw.items():
        if v is None:
            dyn.pop(k, None)
        else:
            dyn[k] = v
    gg['dyn'] = dyn
    U.touch_case(new, msg)
    st.rerun()


def _controls_editor(c):
    gens = [g for g in c['generators'] if _num(g.get('status'), 1)]
    if not gens:
        return
    with st.container(border=True):
        U.card_header('Generator models: technology, controls and parameters')
        U.notice('Each generator is either a <b>synchronous machine</b> with any governor × AVR × PSS from Dynawo\'s '
                 'library, or a converter-interfaced plant: <b>solar PV</b>, <b>type-4 wind</b>, <b>battery storage</b> '
                 '(WECC generic models), a <b>grid-forming converter</b>, a <b>simplified generator</b> or a <b>grid '
                 'equivalent</b>. Default parameters come from Dynawo\'s own examples and regression tests; edit any '
                 'value per generator. Parameters marked <i>auto</i> follow the generator data (rating, P/Q limits). '
                 'Synchronous-machine control combinations that Dynawo does not ship pre-assembled are compiled once '
                 'with Dynawo\'s tool chain (about a minute each) and cached.')
        ver = st.session_state.case_version     # widgets re-read the case after every model change
        top = st.columns([1.2, 1.4, 1.4])
        gid = top[0].selectbox('Generator', [g['id'] for g in gens], key='ctl_gen',
                               format_func=lambda x: f'{x} (bus {next(g["bus"] for g in gens if g["id"] == x)})')
        g = next(x for x in gens if x['id'] == gid)
        d = D.default_dyn_params(g, c)
        kind = D.SYNC if D.is_sync(d) else D.SOURCE_LIB['sources'][d['source']]['kind']
        new_kind = top[1].selectbox('Technology', D.SOURCE_KINDS, index=D.SOURCE_KINDS.index(kind),
                                    key=f'ctl_kind_{gid}_{ver}')
        experimental = top[2].toggle('Show unverified models', key='ctl_exp',
                                     help='Models / controller classes that failed GridScope\'s verification with '
                                          'Dynawo 1.7 (flat start, fault, line trip, load step on IEEE 9 and 14). '
                                          'Shown with the reason; use for testing only.')
        if new_kind != kind:
            if new_kind == D.SYNC:
                _set_dyn(c, gid, f'{gid}: synchronous machine', source=None, sourceParams=None)
            choices = D.source_models(new_kind, include_unverified=experimental) or D.source_models(new_kind, True)
            _set_dyn(c, gid, f'{gid}: technology set to {new_kind}', source=next(iter(choices)), sourceParams={})
        if kind == D.SYNC:
            _sync_editor(c, g, d, experimental, ver)
        else:
            _source_editor(c, g, d, kind, experimental, ver)
        b1, b2, _ = st.columns([1.3, 1.3, 3])
        n_over = len(d.get('controls') or {}) + len(d.get('sourceParams') or {})
        if b1.button(f'Reset {gid} parameters to defaults', disabled=not n_over, width='stretch',
                     help=f'{n_over} parameter(s) differ from the defaults.'):
            _set_dyn(c, gid, f'{gid}: parameters reset', controls={}, sourceParams={})
        if b2.button(f'Copy {gid} model to all generators', width='stretch',
                     help='Same technology, model and parameter overrides for every in-service generator '
                          '(machine data unchanged).'):
            new = copy.deepcopy(c)
            for gg in new['generators']:
                if _num(gg.get('status'), 1) and gg['id'] != gid:
                    dyn = dict(gg.get('dyn') or {})
                    dyn.update(model=d['model'], controls=dict(d.get('controls') or {}),
                               source=d['source'], sourceParams=dict(d.get('sourceParams') or {}))
                    gg['dyn'] = dyn
            U.touch_case(new, f'Model of {gid} copied to all generators')
            st.rerun()


def _sync_editor(c, g, d, experimental, ver):
    from gridscope import custom_models as CM
    gid, cur = g['id'], d['model']
    info = D.CONTROL_LIB['models'][cur]
    sel = st.columns(4)
    machines = list(D.MACHINES)
    machine = sel[0].selectbox('Machine', machines, index=machines.index(info['machine']), key=f'ctl_m_{gid}_{ver}')
    picks = {}
    for col, role, title in ((sel[1], 'governor', 'Governor'), (sel[2], 'avr', 'Voltage regulator (AVR)'),
                             (sel[3], 'pss', 'Power system stabilizer (PSS)')):
        labels = [CM.NONE_LABEL[role]] + list(CM.composable(role, include_unverified=experimental))
        have = info[role]
        if have not in labels:
            labels.append(have)
        picks[role] = col.selectbox(title, labels, index=labels.index(have), key=f'ctl_{role}_{gid}_{ver}',
                                    help=_class_help(role, CM))
    if picks['pss'] != CM.NONE_LABEL['pss'] and picks['avr'] == CM.NONE_LABEL['avr']:
        st.error('A PSS acts through the voltage regulator: choose an AVR or set the PSS to None.')
        return
    key = (machine, picks['governor'], picks['avr'], picks['pss'])
    if key != (info['machine'], info['governor'], info['avr'], info['pss']):
        try:
            chosen, _ = CM.resolve(*key)
        except ValueError as e:
            st.error(str(e))
            return
        if chosen != cur:
            keys = {r['key'] for r in D.control_parameters(chosen)}
            ctl = {k: v for k, v in (d.get('controls') or {}).items() if k in keys}
            _set_dyn(c, gid, f'{gid}: controls set to {chosen}', model=chosen, controls=ctl)
    custom = info.get('custom')
    if not custom:
        badge = '✔ verified' if info.get('verified', True) else '⚠ unverified'
        st.caption(f'Dynawo pre-assembled model `{cur}` — {badge}. {info.get("description", "")}'
                   + (f' Limiter: {info["limiter"]}.' if info.get('limiter', 'None') != 'None' else ''))
        if not info.get('verified', True):
            st.warning('This combination failed verification: ' + '; '.join(info.get('verification', [])[:2]))
    else:
        compiled = CM.is_compiled(cur)
        st.caption(f'Composed model `{cur}` — ' + ('✔ compiled (cached)' if compiled else
                                                   'not compiled yet: compiled automatically before the first run'))
        bad = [D.component(cls)['label'] + ': ' + CM.class_status(cls)['details'][0][:160]
               for cls in info['units'].values() if CM.class_status(cls) and not CM.class_status(cls)['verified']]
        if bad:
            st.warning('Unverified controller class(es) — ' + ' | '.join(bad))
        if not compiled:
            tc = CM.toolchain_status()
            if not tc['ok']:
                st.error('This combination must be compiled, but: ' + ' '.join(tc['issues']))
            elif st.button('Compile now (~1 min)', key=f'compile_{cur}'):
                with st.status(f'Compiling {cur} with Dynawo…', expanded=False) as stat:
                    try:
                        res = CM.compile_models([cur])
                    except RuntimeError as e:
                        res = {cur: str(e)}
                    if res.get(cur):
                        stat.update(label='Compilation failed', state='error')
                        st.error(res[cur])
                    else:
                        stat.update(label=f'{cur} compiled', state='complete')
                        st.rerun()
    rows = D.control_parameters(cur, d.get('controls'))
    if not rows:
        st.info('No governor / AVR / PSS: constant mechanical power and constant field voltage.')
        return
    roles = list(dict.fromkeys(r['role'] for r in rows))
    tabs = st.tabs([f'{D.ROLE_LABEL.get(rl, rl)} — {next(r["component"] for r in rows if r["role"] == rl)}'
                    for rl in roles])
    changed = {}
    for tab, rl in zip(tabs, roles):
        with tab:
            rr = [r for r in rows if r['role'] == rl]
            changed.update(_param_editor(rr, f'ed_ctl_{ver}_{gid}_{rl}', rr[0]['source']))
    if changed:
        _apply_overrides(c, gid, 'controls', changed, {r['key']: r['default'] for r in rows},
                         f'{gid}: control parameters edited')


def _class_help(role, CM):
    n_all = len(CM.composable(role))
    n_ok = len(CM.composable(role, include_unverified=False))
    return (f'{n_all} Dynawo {D.ROLE_LABEL[role].lower()} models can be combined freely; {n_ok} are verified. '
            'Combinations Dynawo does not ship are compiled once on first use.')


def _source_editor(c, g, d, kind, experimental, ver):
    gid, cur = g['id'], d['source']
    models = D.source_models(kind, include_unverified=experimental)
    if cur not in models:
        models = {**models, cur: D.SOURCE_LIB['sources'][cur]}
    names = list(models)
    pick = st.selectbox('Model', names, index=names.index(cur), key=f'src_{gid}_{ver}',
                        format_func=lambda k: models[k]['label'] + ('' if models[k].get('verified', True) else '  ⚠'))
    if pick != cur:
        _set_dyn(c, gid, f'{gid}: model set to {pick}', source=pick, sourceParams={})
    lib = D.SOURCE_LIB['sources'][cur]
    badge = '✔ verified' if lib.get('verified', True) else '⚠ unverified'
    st.caption(f'Dynawo model `{cur}` — {badge}. Rating {d["SNom"]:g} MVA (Rating column above); initial P, Q and '
               'voltage come from the load flow.')
    if not lib.get('verified', True):
        st.warning('Verification: ' + '; '.join(lib.get('verification', [])[:2]))
    rows = D.source_parameters(cur, d.get('sourceParams'), snom=d['SNom'])
    changed = _param_editor(rows, f'ed_src_{ver}_{gid}_{cur}', lib.get('source', ''))
    if changed:
        _apply_overrides(c, gid, 'sourceParams', changed, {r['key']: r['default'] for r in rows},
                         f'{gid}: model parameters edited')


def _load_models_editor(c):
    s = _settings(c)
    with st.container(border=True):
        U.card_header('Load models')
        U.notice('Dynawo load model used for every load, with optional per-bus overrides. Constant-power style '
                 'models (constant P/Q, ZIP with a power share, electronic load) make the solver fail for bolted '
                 'faults at or near the load: use them for trips and load steps, or with impedance faults.')
        exp = st.toggle('Show unverified load models', key='load_exp')
        loads = {k: v for k, v in D.SOURCE_LIB['loads'].items() if exp or v.get('verified', True)}
        cur = s.get('loadModel') or D.DEFAULT_LOAD_MODEL
        if cur not in loads:
            loads[cur] = D.SOURCE_LIB['loads'][cur]
        names = list(loads)
        lm = st.selectbox('Default load model', names, index=names.index(cur),
                          format_func=lambda k: f'{k} — {loads[k]["label"]}', key=f'loadmodel_{st.session_state.case_version}')
        if lm != cur:
            _save_settings(c, loadModel=lm)
            st.rerun()
        info = loads[cur]
        if not info.get('verified', True):
            st.warning('Verification: ' + '; '.join(info.get('verification', [])[:1]))
        if cur == 'LoadAlphaBeta':
            st.caption('Exponents α and β are set in Study settings above.')
        else:
            rows = D.source_parameters(cur, (s.get('loadParams') or {}).get(cur), load=True)
            if rows:
                changed = _param_editor(rows, f'ed_load_{st.session_state.case_version}_{cur}', info.get('source', ''))
                if changed:
                    lp = {k: dict(v) for k, v in (s.get('loadParams') or {}).items()}
                    mine = lp.setdefault(cur, {})
                    defaults = {r['key']: r['default'] for r in rows}
                    for k, v in changed.items():
                        if v == str(defaults[k]):
                            mine.pop(k, None)
                        else:
                            mine[k] = v
                    _save_settings(c, loadParams=lp)
                    st.rerun()
        with st.expander('Per-bus load model overrides'):
            per = dict(s.get('loadModels') or {})
            buses = [b for b in c['buses'] if abs(_num(b.get('Pd'), 0)) + abs(_num(b.get('Qd'), 0)) > 0]
            df = pd.DataFrame([dict(Bus=str(b['id']), Pd=_num(b['Pd'], 0), Qd=_num(b['Qd'], 0),
                                    Model=per.get(str(b['id']), '(default)')) for b in buses])
            ed = st.data_editor(df, hide_index=True, width='stretch', num_rows='fixed',
                                key=f'ed_loadper_{st.session_state.case_version}',
                                column_config={'Bus': st.column_config.TextColumn(disabled=True),
                                               'Pd': st.column_config.NumberColumn('Pd MW', disabled=True),
                                               'Qd': st.column_config.NumberColumn('Qd Mvar', disabled=True),
                                               'Model': st.column_config.SelectboxColumn(
                                                   options=['(default)'] + list(D.SOURCE_LIB['loads']))})
            if not ed.equals(df):
                per = {r['Bus']: r['Model'] for _, r in ed.iterrows() if r['Model'] not in ('(default)', None)}
                _save_settings(c, loadModels=per)
                st.rerun()


# ------------------------------------------------------------ simulation

def _element_options(c):
    ids = [str(b['id']) for b in c['buses']] + [str(b['id']) for b in c['branches']] + [str(g['id']) for g in c['generators']]
    return list(dict.fromkeys(ids))


def _default_events(c):
    br = next((b for b in c['branches'] if _num(b.get('status'), 1) and b.get('equipmentType') == 'line'), None)
    if br is None:
        return pd.DataFrame(columns=EVENT_COLUMNS)
    return pd.DataFrame([
        {'On': True, 'Type': 'Bus fault', 'Element': str(br['from']), 'Start (s)': 1.0, 'Duration (s)': 0.1,
         'Value': None, 'Side': ''},
        {'On': True, 'Type': 'Trip branch', 'Element': str(br['id']), 'Start (s)': 1.1, 'Duration (s)': None,
         'Value': None, 'Side': ''}])


EVENT_COLUMNS = ['On', 'Type', 'Element', 'Start (s)', 'Duration (s)', 'At (%)', 'Value', 'Side']


def _event_columns(df):
    df = df.copy()
    for col in EVENT_COLUMNS:
        if col not in df.columns:
            df[col] = True if col == 'On' else ('' if col == 'Side' else None)
    for col in ('Start (s)', 'Duration (s)', 'At (%)', 'Value'):
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df['Element'] = df['Element'].map(lambda v: None if v is None or (isinstance(v, float) and np.isnan(v)) else str(v))
    return df[EVENT_COLUMNS]


def _events_records(df) -> list[dict]:
    """Event table as JSON-friendly records (saved with the network)."""
    out = []
    for r in df.to_dict('records'):
        if not r.get('Type') and (r.get('Element') is None or r.get('Element') != r.get('Element')):
            continue
        out.append({k: (None if isinstance(v, float) and np.isnan(v) else
                        bool(v) if k == 'On' else v.item() if hasattr(v, 'item') else v)
                    for k, v in r.items() if k in EVENT_COLUMNS})
    return out


def _with_on(df):
    """Event table with the 'On' (enabled) column first; rows without a value count as enabled."""
    df = df.copy()
    if 'On' not in df.columns:
        df.insert(0, 'On', True)
    df['On'] = df['On'].map(lambda v: False if v is False or v == 0 else True).astype(bool)
    return df[['On'] + [c for c in df.columns if c != 'On']]


def _enabled(r) -> bool:
    v = r.get('On', True)
    return not (v is False or (not pd.isna(v) and not bool(v)))


def _events_from_df(df, include_disabled=False) -> list[D.Event]:
    out = []
    for _, r in df.dropna(how='all', subset=[c for c in df.columns if c != 'On']).iterrows():
        if not r.get('Type') or pd.isna(r.get('Element')):
            continue
        if not include_disabled and not _enabled(r):
            continue
        out.append(D.Event(type=r['Type'], element=str(r['Element']), start=float(_num(r['Start (s)'], 0)),
                           duration=float(_num(r.get('Duration (s)'), 0.1)), value=float(_num(r.get('Value'), 0)),
                           side=r.get('Side') if r.get('Side') in ('ONE', 'TWO', 'NONE') else '',
                           location=float(_num(r.get('At (%)'), 50.0))))
    return out


def _simulation_tab(c):
    s = _settings(c)
    with st.container(border=True):
        U.card_header('Events')
        st.caption('**On**: untick to keep an event in the list but leave it out of the run. '
                   '**Value**: step size for load / generator steps (pu on 100 MVA) and AVR steps (pu), or the fault '
                   'reactance for a bus or line fault (blank = bolted). **Duration**: fault duration. **Line fault**: '
                   'a fault along a line at **At (%)** of its length from its "from" end; the line opens when the fault '
                   'clears, or pick Side **NONE** to leave the clearing to the line protection. **Side**: open only '
                   'one end of a branch. Elements: bus numbers, branch ids, generator ids (load steps use the bus number).')
        key = f'dyn_ev_df_{st.session_state.case_version}'
        if key not in st.session_state:
            saved = U.recalled('events')
            last = pd.DataFrame(saved) if saved else st.session_state.get('dyn_ev_last')
            # keep the user's events across edits of the same network, not when another case is loaded
            if last is None or not len(last) and not saved or \
                    D.validate_events(_events_from_df(last, include_disabled=True), c, None, 1e9):
                last = _default_events(c)
            st.session_state[key] = _with_on(_event_columns(last))
        ev_df = st.data_editor(st.session_state[key], num_rows='dynamic', hide_index=True, width='stretch',
                               key=f'ed_dynev_{st.session_state.case_version}', column_config={
                                   'On': st.column_config.CheckboxColumn('On', default=True, width='small',
                                                                         help='Include this event in the run'),
                                   'Type': st.column_config.SelectboxColumn(options=D.EVENT_TYPES, required=True, width='medium'),
                                   'Element': st.column_config.SelectboxColumn(options=_element_options(c), required=True),
                                   'Start (s)': st.column_config.NumberColumn(min_value=0.0, step=0.01, format='%.3f'),
                                   'Duration (s)': st.column_config.NumberColumn(min_value=0.0, step=0.01, format='%.3f'),
                                   'Value': st.column_config.NumberColumn(step=0.01, format='%.4f'),
                                   'At (%)': st.column_config.NumberColumn(
                                       min_value=0.0, max_value=100.0, step=1.0, format='%.1f',
                                       help='Line fault: distance of the fault from the line\'s "from" end, % of '
                                            'its length (blank = 50 %)'),
                                   'Side': st.column_config.SelectboxColumn(options=['', 'ONE', 'TWO', 'NONE'])})
        st.session_state.dyn_ev_last = ev_df
        U.remember('events', _events_records(ev_df))
        g = st.columns([1, 1, 2])
        stop = g[0].number_input('Stop time (s)', 1.0, 120.0, float(U.recalled('stopTime', 10.0)), 1.0,
                                 key=f'dyn_stop_{st.session_state.case_version}')
        U.remember('stopTime', float(stop))
        events = _events_from_df(ev_df)
        n_off = len(_events_from_df(ev_df, include_disabled=True)) - len(events)
        problems = D.validate_events(events, c, None, stop)
        run = g[1].button('▶ Run simulation', type='primary', width='stretch', disabled=bool(problems))
        g[2].caption(f'{len(events)} event(s) enabled' + (f', {n_off} disabled' if n_off else '')
                     + ('' if events else ' — a run without events checks the steady state.'))
        for p in problems:
            st.warning(p)
    if run:
        with st.spinner('Running Dynawo…'):
            r = U.on_pp_thread(D.simulate, c, events, stop, s['load_alpha'], s['load_beta'], s['angle_limit'],
                               solver=s['solver'])
        U.put_result('dyn_sim', r)
        st.session_state.dyn_events_used = events
        st.session_state.status = ('Dynamic simulation: ' + ('not simulated' if r.stable is None
                                                             else 'stable' if r.stable else 'UNSTABLE'))
    r, stale = U.get_result('dyn_sim')
    if r is None:
        U.notice('Define the events and press <b>Run simulation</b>. Tip: after a run, the Network Diagram can replay '
                 'bus voltages over time (overlay “Dynamic simulation”).')
        return
    U.stale_note(stale)
    _verdict(r)
    _metrics(r, s)
    if len(r.angles) or len(r.pgen):
        evts = st.session_state.get('dyn_events_used', [])
        _dynamic_charts(r, evts, s, key='sim')
        with st.container(border=True):
            h1, h2 = st.columns([5, 1])
            with h1:
                U.card_header('Simulation timeline')
            with h2:
                U.csv_button(r.curves.reset_index(), 'dynamic_curves.csv', 'Curves CSV')
            tl = r.timeline
            if len(tl) and st.checkbox('Hide PMIN / PMAX governor messages', True, key='hide_pmin'):
                tl = tl[~tl['message'].str.startswith(('PMIN', 'PMAX'))]
            st.dataframe(tl, hide_index=True, width='stretch', height=260)


def _verdict(r):
    if r.stable is None:
        st.warning(f'**NOT SIMULATED** — {r.reason}', icon='⚠️')
    elif r.stable:
        st.success(f'**STABLE** — {r.reason}', icon='✅')
    else:
        st.error(f'**UNSTABLE** — {r.reason}', icon='⛔')
        if not r.ok and r.status_text and r.status_text not in r.reason:
            st.caption(f'Dynawo: {r.status_text[:1500]}')
    for n in getattr(r, 'notes', []) or []:
        st.info(n, icon='ℹ️')
    P.operations_table(r)


def _metrics(r, s):
    m = r.metrics or {}
    if not m:
        return
    f0 = s['f_nom']
    U.metric_cards([
        ('Max rotor-angle separation', U.fmt(m.get('max_angle_sep'), 1, '°'),
         'bad' if m.get('max_angle_sep', 0) > s['angle_limit'] else 'ok'),
        ('Transient stability index', U.fmt(m.get('TSI'), 0, ' %'), 'ok' if (m.get('TSI') or -1) > 0 else 'bad'),
        ('Min post-disturbance V', (U.fmt(m.get('min_post_voltage'), 3, ' pu') +
                                    (f' @ {m.get("min_post_voltage_bus")}' if m.get('min_post_voltage_bus') else '')),
         'warn' if (m.get('min_post_voltage') or 1) < 0.9 else None),
        ('Frequency range', f'{m["f_min"] / 50 * f0:.3f} – {m["f_max"] / 50 * f0:.3f} Hz'
         if np.isfinite(m.get('f_min', np.nan)) else '—', None),
        ('Damping ratio (swing)', U.fmt(m.get('damping'), 3), 'warn' if (m.get('damping') or 1) < 0.05 else None),
    ])
    st.caption('TSI = 100·(360 − δmax)/(360 + δmax): positive = stable. Damping ratio from the logarithmic decrement '
               'of the first two post-disturbance peaks of the angle separation (— if the swing is too small/short).')


def _vlines(fig, events, rows):
    for e in events:
        for row in rows:
            fig.add_vline(x=e.start, line_dash='dot', line_color=U.MUTED, line_width=1, row=row, col='all')
            if e.type in ('Bus fault', 'Line fault'):
                fig.add_vrect(x0=e.start, x1=e.start + e.duration, fillcolor=U.BAD, opacity=0.08, line_width=0,
                              row=row, col='all')


SHOW_ALL_UP_TO = 10     # larger systems start with the most disturbed units only
SHOW_FIRST = 8


def _ranked_gens(r) -> list:
    """Generators, most disturbed first: lost units, then machines by rotor-angle swing, then converters by
    active-power swing."""
    lost = [g for g in r.lost_gens]
    sync = sorted(r.angles.columns, key=lambda g: -float(r.angles[g].max() - r.angles[g].min()))
    conv = sorted([g for g in r.pgen.columns if g not in r.angles.columns],
                  key=lambda g: -float(r.pgen[g].max() - r.pgen[g].min()))
    return list(dict.fromkeys(lost + sync + conv))


def _ranked_buses(r) -> list:
    return list(r.voltages.min().sort_values().index)


def _picker(label, options, ranked, key, labels=None, first=SHOW_FIRST):
    """Multiselect with All / Most disturbed / Clear. The choice is kept while the options stay the same."""
    k = f'{key}_pick'
    sig = f'{key}_opts'
    if st.session_state.get(sig) != tuple(options):          # new result: start from the default
        st.session_state[sig] = tuple(options)
        st.session_state[k] = list(options) if len(options) <= SHOW_ALL_UP_TO else ranked[:first]
    else:
        st.session_state[k] = [o for o in st.session_state.get(k, []) if o in options]

    def _set(v):
        st.session_state[k] = v

    g = st.columns([6, 1, 1.4, 1], vertical_alignment='bottom')
    g[0].multiselect(label, options, key=k, format_func=lambda o: (labels or {}).get(o, o),
                     placeholder='None shown: pick to add', help=f'{len(options)} in this run. Clicking a name in '
                     'the chart legend also hides or shows it.')
    g[1].button('All', key=f'{key}_all', on_click=_set, args=(list(options),), width='stretch')
    g[2].button(f'Top {first}', key=f'{key}_top', on_click=_set, args=(ranked[:first],), width='stretch',
                help='The most disturbed: ' + ('lost units, largest rotor-angle and power swings'
                                               if 'enerator' in label else 'lowest voltages'))
    g[3].button('Clear', key=f'{key}_none', on_click=_set, args=([],), width='stretch')
    return st.session_state[k]


def _dynamic_charts(r, events, s, key):
    f0 = s['f_nom']
    gens_all = list(dict.fromkeys(list(r.angles.columns) + list(r.pgen.columns)))
    color = {g: U.SERIES[i % len(U.SERIES)] for i, g in enumerate(gens_all)}   # fixed per unit, whatever is shown
    lost = set(r.lost_gens)
    with st.container(border=True):
        U.card_header('Machine response')
        chosen = set(_picker('Generators to plot', gens_all, _ranked_gens(r), f'gens_{key}',
                             labels={g: g + (' (lost)' if g in lost else '') +
                                     ('' if g in r.angles.columns else ' (converter)') for g in gens_all}))
        gens = [g for g in gens_all if g in chosen]
        _machine_chart(r, events, f0, gens, color, lost, key)
    if len(r.voltages):
        with st.container(border=True):
            U.card_header('Bus voltages')
            buses = list(r.voltages.columns)
            pick = _picker('Buses to plot', buses, _ranked_buses(r), f'vb_{key}', first=6)
            fig = go.Figure()
            for i, b in enumerate(pick):
                fig.add_trace(go.Scatter(x=r.voltages.index, y=r.voltages[b], name=f'Bus {b}', mode='lines',
                                         line=dict(width=1.8, color=U.SERIES[buses.index(b) % len(U.SERIES)])))
            for e in events:
                fig.add_vline(x=e.start, line_dash='dot', line_color=U.MUTED, line_width=1)
            fig.add_hline(y=0.9, line_dash='dot', line_color=U.WARN, line_width=1)
            U.plotly_layout(fig, 300)
            fig.update_layout(hovermode='x unified')
            fig.update_xaxes(title='Time (s)')
            fig.update_yaxes(title='pu')
            st.plotly_chart(fig, key=f'volt_{key}', config=dict(displaylogo=False))


def _machine_chart(r, events, f0, gens, color, lost, key):
    has_ctl = len(getattr(r, 'efd', [])) and len(getattr(r, 'pm', []))
    rows = 3 if has_ctl else 2
    titles = ['Rotor angle vs centre of angle (°)', 'Rotor speed (Hz)', 'Active power (MW)', 'Reactive power (Mvar)']
    if has_ctl:
        titles += ['Field voltage Efd (pu) — AVR output', 'Mechanical power Pm (pu of PNom) — governor output']
    fig = make_subplots(rows=rows, cols=2, shared_xaxes=True, vertical_spacing=0.09 if has_ctl else 0.12,
                        horizontal_spacing=0.07, subplot_titles=titles)
    for g in gens:
        dash = 'dot' if g in lost else 'solid'
        kw = dict(mode='lines', legendgroup=g, line=dict(color=color[g], width=1.8, dash=dash))
        name = g + (' (lost)' if g in lost else '')
        if g in r.angles:
            fig.add_trace(go.Scatter(x=r.angles.index, y=r.angles[g], name=name, **kw), 1, 1)
            if g in r.speed:
                fig.add_trace(go.Scatter(x=r.speed.index, y=r.speed[g] * f0, name=g, showlegend=False, **kw), 1, 2)
        if g in r.pgen:
            fig.add_trace(go.Scatter(x=r.pgen.index, y=r.pgen[g], name=name + ('' if g in r.angles else ' (converter)'),
                                     showlegend=g not in r.angles, **kw), 2, 1)
        if g in r.qgen:
            fig.add_trace(go.Scatter(x=r.qgen.index, y=r.qgen[g], name=g, showlegend=False, **kw), 2, 2)
        if has_ctl and g in r.efd:
            fig.add_trace(go.Scatter(x=r.efd.index, y=r.efd[g], name=g, showlegend=False, **kw), 3, 1)
            fig.add_trace(go.Scatter(x=r.pm.index, y=r.pm[g], name=g, showlegend=False, **kw), 3, 2)
    _vlines(fig, events, list(range(1, rows + 1)))
    U.plotly_layout(fig, 820 if has_ctl else 560)
    fig.update_layout(hovermode='x unified')
    fig.update_xaxes(title_text='Time (s)', row=rows)
    for ann in fig.layout.annotations:
        ann.font = dict(color=U.TEXT, size=12)
    if not gens:
        st.caption('No generator selected.')
        return
    st.plotly_chart(fig, key=f'mach_{key}', config=dict(displaylogo=False))


# ----------------------------------------------------------------- CCT

def _cct_tab(c):
    s = _settings(c)
    with st.container(border=True):
        U.card_header('Critical clearing time search', 'bisection on fault duration')
        U.notice('A bolted three-phase fault is applied at the chosen bus at t = 1 s and cleared after the trial '
                 'clearing time — optionally by tripping a branch at the same instant (the usual “fault on a line, '
                 'line tripped” case). Each trial is a full Dynawo simulation from the load-flow state; the search '
                 'halves the stable/unstable bracket until it is narrower than the tolerance.')
        g = st.columns(6)
        bus_ids = [str(b['id']) for b in c['buses']]
        line_buses = [str(br['from']) for br in c['branches'] if br.get('equipmentType') == 'line' and _num(br.get('status'), 1)]
        cs = dict(U.recalled('cct') or {})
        ver = st.session_state.case_version
        default = bus_ids.index(line_buses[0]) if line_buses else 0
        if cs.get('bus') in bus_ids:
            default = bus_ids.index(cs['bus'])
        bus = g[0].selectbox('Fault bus', bus_ids, index=default, key=f'cct_bus_{ver}')
        adj = [br['id'] for br in c['branches'] if _num(br.get('status'), 1) and bus in (str(br['from']), str(br['to']))]
        trips = ['(fault removed only)'] + adj
        trip = g[1].selectbox('Cleared by tripping', trips, key=f'cct_trip_{ver}_{bus}',
                              index=trips.index(cs['trip']) if cs.get('trip') in trips else 0)
        lo = g[2].number_input('Search from (ms)', 10, 2000, int(cs.get('lo', 20)), 10, key=f'cct_lo_{ver}')
        hi = g[3].number_input('Search to (ms)', 20, 5000, int(cs.get('hi', 500)), 10, key=f'cct_hi_{ver}')
        tol = g[4].number_input('Tolerance (ms)', 1, 100, int(cs.get('tol', 10)), 1, key=f'cct_tol_{ver}')
        post = g[5].number_input('Simulate after clearing (s)', 1.0, 30.0, float(cs.get('post', 4.0)), 0.5,
                                 key=f'cct_post_{ver}')
        U.remember('cct', dict(bus=bus, trip=trip, lo=int(lo), hi=int(hi), tol=int(tol), post=float(post)))
        run = st.button('⏱ Find critical clearing time', type='primary', disabled=hi <= lo)
    if run:
        trip_id = None if trip.startswith('(') else trip
        log = st.empty()
        bar = st.progress(0.0, text='Starting…')
        lines = []
        est = 2 + int(np.ceil(np.log2(max(2, (hi - lo) / tol))))

        def update(k, tc, stable):
            lines.append(f'trial {k}: clearing {tc * 1000:.0f} ms → {"stable" if stable else "UNSTABLE"}')
            bar.progress(min(1.0, k / est), text=lines[-1])
            log.code('\n'.join(lines), language=None)

        try:
            res = U.on_pp_thread_with_progress(D.critical_clearing_time, update, c, bus, trip_id, 1.0, lo / 1000,
                                               hi / 1000, tol / 1000, post, s['load_alpha'], s['load_beta'],
                                               s['angle_limit'], solver=s['solver'])
        except (RuntimeError, ValueError) as e:
            bar.empty()
            st.error(str(e))
            return
        bar.empty()
        log.empty()
        detail = {}
        for label, tc in (('stable', res.get('low')), ('unstable', res.get('high'))):
            if tc:
                ev = [D.Event('Bus fault', bus, 1.0, duration=tc)] + ([D.Event('Trip branch', trip_id, 1.0 + tc)] if trip_id else [])
                detail[label] = (tc, U.on_pp_thread(D.simulate, c, ev, 1.0 + tc + post, s['load_alpha'], s['load_beta'],
                                                    s['angle_limit'], solver=s['solver']), ev)
        res['detail'] = detail
        res['setup'] = dict(bus=bus, trip=trip_id)
        res.pop('network_map', None)
        U.put_result('cct', res)
    res, stale = U.get_result('cct')
    if res is None:
        return
    U.stale_note(stale)
    su = res['setup']
    where = f'fault at bus {su["bus"]}' + (f', cleared by tripping {su["trip"]}' if su['trip'] else ', fault removed')
    if res['cct'] is not None:
        U.metric_cards([('Critical clearing time', f'{res["low"] * 1000:.0f} ms', 'ok'),
                        ('First unstable trial', f'{res["high"] * 1000:.0f} ms', 'bad'),
                        ('Bracket', f'±{(res["high"] - res["low"]) * 500:.0f} ms', None),
                        ('Simulations', str(len(res['runs'])), None)])
        st.caption(f'{where}. ' + res['message'])
    else:
        st.info(f'{where}: {res["message"]}')
    runs = pd.DataFrame(res['runs'])
    a1, a2 = st.columns([1, 2])
    with a1, st.container(border=True):
        U.card_header('Trials')
        limit = _settings(c)['angle_limit']
        cap = 2 * limit                      # runaway (pole-slipping) trials are drawn at the top of the axis
        runs = runs.sort_values('clearing_time')
        y = runs.max_angle_sep.clip(upper=cap)
        fig = go.Figure(go.Scatter(x=runs.clearing_time * 1000, y=y, mode='markers',
                                   marker=dict(size=11, color=[U.GOOD if s_ else U.BAD for s_ in runs.stable],
                                               symbol=['circle' if v <= cap else 'triangle-up' for v in runs.max_angle_sep],
                                               line=dict(color=U.TEXT, width=1)),
                                   customdata=runs.max_angle_sep,
                                   hovertemplate='%{x:.0f} ms → %{customdata:.0f}°<extra></extra>'))
        fig.add_hline(y=limit, line_dash='dot', line_color=U.BAD, line_width=1,
                      annotation_text=f'{limit:.0f}° limit', annotation_font_color=U.BAD)
        if res.get('low') and res.get('high'):
            fig.add_vrect(x0=res['low'] * 1000, x1=res['high'] * 1000, fillcolor=U.WARN, opacity=0.15, line_width=0)
        U.plotly_layout(fig, 300, legend=False)
        fig.update_xaxes(title='clearing time (ms)')
        fig.update_yaxes(title='max angle separation (°)', range=[0, cap * 1.05])
        st.plotly_chart(fig, config=dict(displaylogo=False))
    with a2, st.container(border=True):
        U.card_header('Rotor angles either side of the CCT')
        st.caption('The verdict uses the angle-separation limit set under Machine & load models (180° by default, a '
                   'common conservative criterion). A trial just past the limit may still swing back; well past it, '
                   'machines slip poles and the angles run away.')
        det = res.get('detail', {})
        if det:
            lim = _settings(c)['angle_limit']
            fig = make_subplots(rows=1, cols=len(det), shared_yaxes=False, subplot_titles=[
                f'{"Within" if k == "stable" else "Beyond"} the {lim:.0f}° limit: cleared in {v[0] * 1000:.0f} ms '
                f'(max Δδ {v[1].metrics.get("max_angle_sep", float("nan")):.0f}°)' for k, v in det.items()])
            for col, (k, (tc, rr, ev)) in enumerate(det.items(), 1):
                for i, gname in enumerate(rr.angles.columns):
                    fig.add_trace(go.Scatter(x=rr.angles.index, y=rr.angles[gname], name=gname, mode='lines',
                                             legendgroup=gname, showlegend=col == 1,
                                             line=dict(color=U.SERIES[i % len(U.SERIES)], width=1.6)), 1, col)
                fig.add_vrect(x0=1.0, x1=1.0 + tc, fillcolor=U.BAD, opacity=0.1, line_width=0, row=1, col=col)
            U.plotly_layout(fig, 300)
            fig.update_xaxes(title='Time (s)')
            fig.update_yaxes(title='° vs COI', col=1)
            for ann in fig.layout.annotations:
                ann.font = dict(color=U.TEXT, size=12)
            st.plotly_chart(fig, config=dict(displaylogo=False))


# ----------------------------------------------------------- screening

def _screening_tab(c):
    s = _settings(c)
    inservice = [br['id'] for br in c['branches'] if _num(br.get('status'), 1)]
    with st.container(border=True):
        U.card_header('Dynamic N-1 contingency screening', 'fault + branch trip, one simulation per branch')
        U.notice('For each selected branch: a bolted three-phase fault at one end at t = 1 s, cleared after the '
                 'clearing time by tripping that branch, then simulated for the post-fault period. Contingencies that '
                 'island part of the network are flagged; machines that end up disconnected are left out of the '
                 'rotor-angle check. Complements the static N-1 screen (thermal/voltage) with transient stability.')
        ss_ = dict(U.recalled('screening') or {})
        ver = st.session_state.case_version
        keep = [b for b in (ss_.get('branches') or []) if b in inservice]
        sel = st.multiselect('Branches', inservice, default=keep if 'branches' in ss_ else inservice,
                             key=f'scr_sel_{ver}')
        g = st.columns(4)
        tc = g[0].number_input('Clearing time (ms)', 20, 1000, int(ss_.get('tc', 100)), 10, key=f'scr_tc_{ver}')
        end = g[1].selectbox('Fault location', ['from', 'to'], index=1 if ss_.get('end') == 'to' else 0,
                             format_func=lambda x: f'{x}-bus end', key=f'scr_end_{ver}')
        post = g[2].number_input('Simulate after clearing (s)', 1.0, 30.0, float(ss_.get('post', 5.0)), 0.5,
                                 key=f'scr_post_{ver}')
        U.remember('screening', dict(branches=list(sel), tc=int(tc), end=end, post=float(post)))
        run = g[3].button('Run dynamic N-1', type='primary', width='stretch', disabled=not sel)
    if run:
        bar = st.progress(0.0, text='Starting…')

        def update(k, n, bid, stable):
            bar.progress(k / n, text=f'{k}/{n}: {bid} → {"stable" if stable else "UNSTABLE"}')

        try:
            df = U.on_pp_thread_with_progress(D.screen_contingencies, update, c, sel, tc / 1000, end, 1.0, post,
                                              s['load_alpha'], s['load_beta'], s['angle_limit'], solver=s['solver'])
        except (RuntimeError, ValueError) as e:
            bar.empty()
            st.error(str(e))
            return
        bar.empty()
        U.put_result('screen', dict(table=df, tc=tc / 1000, end=end, post=post))
    res, stale = U.get_result('screen')
    if res is None:
        return
    U.stale_note(stale)
    df = res['table']
    n_unst = int((df.verdict == 'Unstable').sum())
    n_skip = int((df.verdict == 'Not simulated').sum())
    n_isl = int((df.islanded.astype(str) != '').sum()) if 'islanded' in df else 0
    U.metric_cards([('Contingencies', str(len(df)), None), ('Stable', str(int((df.verdict == 'Stable').sum())), 'ok'),
                    ('Unstable', str(n_unst), 'bad' if n_unst else 'ok'), ('Islanding', str(n_isl), 'warn' if n_isl else None),
                    ('Lowest TSI', U.fmt(df.TSI.min(), 0, ' %'), None)])
    if n_skip:
        U.notice(f'{n_skip} contingenc{"y" if n_skip == 1 else "ies"} not simulated: tripping the branch disconnects '
                 'converter-interfaced generation, which Dynawo cannot simulate. Assess them as a loss of that '
                 'generation (see the Detail column).')
    with st.container(border=True):
        h1, h2 = st.columns([5, 1])
        with h1:
            U.card_header('Results', f'cleared in {res["tc"] * 1000:.0f} ms, fault at {res["end"]}-bus end')
        with h2:
            U.csv_button(df, 'dynamic_n1.csv')
        show = df[['branch', 'fault_bus', 'verdict', 'max_angle_sep', 'TSI', 'min_post_voltage', 'min_v_bus', 'f_min',
                   'damping', 'islanded', 'detail']].rename(columns=dict(
            branch='Branch', fault_bus='Fault bus', verdict='Verdict', max_angle_sep='Max Δδ °', TSI='TSI %',
            min_post_voltage='Min V pu', min_v_bus='at bus', f_min='f min Hz', damping='Damping ζ',
            islanded='Islanded buses', detail='Detail'))
        show['f min Hz'] = show['f min Hz'] / 50.0 * s['f_nom']      # engine reports on a 50 Hz base
        st.dataframe(show.style.format(precision=3, subset=['Min V pu', 'f min Hz', 'Damping ζ'])
                     .format(precision=1, subset=['Max Δδ °', 'TSI %'])
                     .map(lambda v: f'color:{U.GOOD if v == "Stable" else U.BAD};font-weight:700', subset=['Verdict']),
                     hide_index=True, width='stretch', height=min(520, 38 + 35 * len(show)))
    with st.container(border=True):
        U.card_header('Inspect a contingency')
        pick = st.selectbox('Contingency', list(df.branch), key='scr_pick')
        row = df[df.branch == pick].iloc[0]
        cache = st.session_state.setdefault('scr_cache', {})
        ck = (st.session_state.case_version, pick, res['tc'], res['end'], res['post'])
        if ck not in cache:
            ev = [D.Event('Bus fault', str(row.fault_bus), 1.0, duration=res['tc']),
                  D.Event('Trip branch', pick, 1.0 + res['tc'])]
            with st.spinner(f'Simulating {pick}…'):
                cache[ck] = (U.on_pp_thread(D.simulate, c, ev, 1.0 + res['tc'] + res['post'], s['load_alpha'],
                                            s['load_beta'], s['angle_limit'], solver=s['solver']), ev)
        rr, ev = cache[ck]
        _verdict(rr)
        _dynamic_charts(rr, ev, s, key=f'scr_{pick}')
