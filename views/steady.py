"""GridScope steady-state views: network, data tables, results, N-1, OPF, faults, charts."""
from __future__ import annotations

import copy

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from gridscope import case as C
from gridscope import contingency, faults, loadflow, opf
from views import common as U
from views.diagram import build_figure


# =============================================================== load flow

def run_ac_load_flow():
    v = U.validation()
    if v['errors']:
        U.put_result('ac', dict(converged=False, status='VALIDATION',
                                log=['AC load flow'] + [f'ERROR: {e["message"]}' for e in v['errors']]))
        st.session_state.status = f'{len(v["errors"])} validation error(s) — fix before solving'
        return False
    c = U.case()
    _, _, res = U.on_pp_thread(loadflow.run_ac, c, st.session_state.enforce_q)
    res['log'] = [f'WARNING: {w["message"]}' for w in v['warnings']] + res['log']
    U.put_result('ac', res)
    st.session_state.status = f'Converged — {res["iterations"]} iterations' if res['converged'] else 'Not converged'
    return res['converged']


# ================================================================= network

def view_network():
    c = U.case()
    ac, ac_stale = U.get_result('ac')
    dyn_res, _ = U.get_result('dyn_sim')
    left, right = st.columns([2, 1], gap='medium')
    with left:
        with st.container(border=True):
            h1, h2 = st.columns([3, 2])
            with h1:
                U.card_header('Single-Line Diagram')
            overlay, open_br = None, ()
            with h2:
                modes = ['Load flow'] + (['Dynamic simulation'] if dyn_res is not None and len(dyn_res.voltages) else [])
                mode = st.radio('Overlay', modes, horizontal=True, label_visibility='collapsed', key='diag_mode')
            if mode == 'Dynamic simulation':
                t = dyn_res.voltages.index.values
                ts = st.slider('Time (s)', float(t[0]), float(t[-1]), float(min(t[-1], (dyn_res.metrics or {}).get('first_event', 0) + 0.05)),
                               step=0.01, key='diag_t')
                k = int(np.searchsorted(t, ts))
                k = min(k, len(t) - 1)
                overlay = dict(voltages={str(b): float(v) for b, v in dyn_res.voltages.iloc[k].items()},
                               label=f'V at t = {t[k]:.2f} s')
                open_br = tuple(e.element for e in st.session_state.get('dyn_events_used', [])
                                if (e.type == 'Trip branch' and e.start <= t[k]) or
                                (e.type == 'Line fault' and e.side != 'NONE' and e.start + e.duration <= t[k]))
            st.markdown(
                '<div class="gs-legend"><span><i class="dot" style="background:#fb7185"></i>Slack</span>'
                '<span><i class="dot" style="background:#4ea1ff"></i>PV</span>'
                '<span><i class="dot" style="background:#94a3b8"></i>PQ</span>'
                '<span>━ line &nbsp; ╍ transformer &nbsp; ┈ open</span>'
                + ('<span>colour = bus voltage: <b style="color:#fb7185">0.90</b> → <b style="color:#34d399">1.00</b>'
                   ' → <b style="color:#a78bfa">1.10</b> pu</span>' if overlay else
                   '<span>branch colour/width = loading</span>') + '</div>', unsafe_allow_html=True)
            fig = build_figure(c, None if ac_stale else ac, overlay, open_br)
            ev = st.plotly_chart(fig, key='sld', on_select='rerun', selection_mode='points',
                                 config=dict(displaylogo=False, scrollZoom=True,
                                             modeBarButtonsToRemove=['select2d', 'lasso2d']))
            pts = (ev or {}).get('selection', {}).get('points', []) if ev else []
            if pts and pts[0].get('customdata'):
                st.session_state.inspect = pts[0]['customdata'][:2]
    with right:
        with st.container(border=True):
            U.card_header('Selected Element')
            _inspector(c, None if ac_stale else ac)
        with st.container(border=True):
            U.card_header('AC Solver Options')
            st.selectbox('Generator reactive-limit enforcement', [True, False], key='enforce_q',
                         format_func=lambda x: 'Enforce Qmax/Qmin (auto PV→PQ switching)' if x
                         else 'Off (PV buses always hold voltage setpoint)')
            U.notice('When on, a PV bus whose generator(s) would need to exceed their combined Qmax/Qmin to '
                     'hold the voltage setpoint is switched to PQ with Q fixed at the limit. A bus is never '
                     'switched back to PV within the same solve. Solver: OpenLoadFlow (PowSyBl), Newton–Raphson, '
                     'flat start, all mismatch taken by the Slack generator.')
        with st.container(border=True):
            U.card_header('Solver Log')
            log = '\n'.join(ac['log']) if ac else 'Load a case and run AC load flow.'
            st.code(log, language=None, height=220, wrap_lines=True)


def _inspector(c, ac):
    sel = st.session_state.get('inspect')
    if not sel:
        U.notice('Click a bus or branch to inspect it.')
        return
    kind, eid = sel
    if kind == 'bus':
        b = next((b for b in c['buses'] if str(b['id']) == eid), None)
        if b is None:
            return
        rows = [('Nominal voltage', f'{b["baseKV"]} kV'), ('Load', f'{b["Pd"]} MW / {b["Qd"]} MVAr'),
                ('Shunt', f'{b["Gs"]} MW / {b["Bs"]} MVAr')]
        gens = [g['id'] for g in c['generators'] if str(g['bus']) == eid]
        if gens:
            rows.append(('Generators', ', '.join(gens)))
        if ac and ac.get('converged'):
            r = ac['buses'][ac['buses'].id.astype(str) == eid].iloc[0]
            rows += [('Solved voltage', f'{r.Vm:.5f} pu ∠ {r.Va:.3f}°'),
                     ('Net injection', f'{r.Pinj:.2f} MW / {r.Qinj:.2f} MVAr')]
        title = f'{b.get("name", eid)} <span class="gs-badge">{b["type"]}</span>'
    else:
        br = next((x for x in c['branches'] if str(x['id']) == eid), None)
        if br is None:
            return
        rows = [('Type', br.get('equipmentType', 'line')), ('Impedance', f'{br["r"]} + j{br["x"]} pu'),
                ('Charging B', f'{br["b"]} pu'), ('Tap / shift', f'{br["tap"]} / {br["shift"]}°'),
                ('Rating', f'{br["rate"]} MVA'), ('Status', 'in service' if C._num(br['status'], 1) else 'open')]
        if ac and ac.get('converged'):
            r = ac['branches'][ac['branches'].id.astype(str) == eid].iloc[0]
            rows += [('From flow', f'{r.Pf:.2f} MW / {r.Qf:.2f} MVAr'), ('To flow', f'{r.Pt:.2f} MW / {r.Qt:.2f} MVAr'),
                     ('Losses', f'{r.loss:.3f} MW'), ('Loading', f'{r.loading:.1f}%')]
        title = f'{eid} <span class="gs-badge">{br["from"]} → {br["to"]}</span>'
    html = f'<h4 style="margin:0 0 8px">{title}</h4>' + ''.join(
        f'<div class="gs-kv"><span>{k}</span><b>{v}</b></div>' for k, v in rows)
    st.markdown(html, unsafe_allow_html=True)


# ============================================================ data tables

def _issue_list(scope):
    v = U.validation()
    items = [i for i in v['errors'] + v['warnings'] if i['scope'] == scope]
    if items:
        errs = {id(i) for i in v['errors']}
        st.markdown(''.join(f'<div class="gs-issue {"err" if id(i) in errs else "warn"}">'
                            f'{i["message"]}</div>' for i in items[:12]), unsafe_allow_html=True)


def view_buses():
    c = U.case()
    with st.container(border=True):
        U.card_header('Bus Data', 'add rows at the bottom · select rows + Delete to remove')
        num = lambda label, fmt='%.4g': st.column_config.NumberColumn(label, format=fmt)   # noqa: E731
        cols = ['name', 'id', 'type', 'baseKV', 'Pd', 'Qd', 'Gs', 'Bs', 'Vm', 'Va', 'x', 'y']
        cfg = {'name': st.column_config.TextColumn('Name'), 'id': st.column_config.TextColumn('No.'),
               'type': st.column_config.SelectboxColumn('Type', options=list(C.BUS_TYPES), required=True),
               'baseKV': num('Base kV'), 'Pd': num('PD MW'), 'Qd': num('QD MVAr'), 'Gs': num('Gsh MW'),
               'Bs': num('Bsh MVAr'), 'Vm': num('V set pu'), 'Va': num('Angle °'), 'x': num('x'), 'y': num('y')}
        recs = copy.deepcopy(c['buses'])
        for r in recs:
            r['id'] = str(r['id'])
        new = U.edit_records(recs, cols, cfg, f'ed_bus_{st.session_state.case_version}',
                             dict(C.BUS_DEFAULTS, x=450, y=300),
                             new_id=lambda out, r: str(max([int(C._num(b['id'], 0)) for b in out] + [0]) + 1))
        if new is not None:
            for b in new:
                b['id'] = _as_id(b['id'])
                if b.get('name') in (None, ''):
                    b['name'] = f'Bus {b["id"]}'
            U.touch_case({**c, 'buses': new}, 'Bus data edited')
            st.rerun()
        _issue_list('bus')


def view_branches():
    c = U.case()
    with st.container(border=True):
        U.card_header('Branch / Transformer Data')
        num = lambda label, fmt='%.5g': st.column_config.NumberColumn(label, format=fmt)   # noqa: E731
        cols = ['id', 'equipmentType', 'from', 'to', 'r', 'x', 'b', 'tap', 'shift', 'rate', 'status']
        cfg = {'id': st.column_config.TextColumn('ID'),
               'equipmentType': st.column_config.SelectboxColumn('Type', options=['line', 'transformer']),
               'from': st.column_config.TextColumn('From'), 'to': st.column_config.TextColumn('To'),
               'r': num('R pu'), 'x': num('X pu'), 'b': num('B pu'), 'tap': num('Tap'), 'shift': num('Shift °'),
               'rate': num('Rate MVA'), 'status': st.column_config.SelectboxColumn('Status', options=[1, 0])}
        recs = copy.deepcopy(c['branches'])
        for r in recs:
            r['from'], r['to'] = str(r['from']), str(r['to'])
        new = U.edit_records(recs, cols, cfg, f'ed_br_{st.session_state.case_version}', dict(C.BRANCH_DEFAULTS),
                             new_id=lambda out, r: f'{r.get("from")}-{r.get("to")}')
        if new is not None:
            U.touch_case({**c, 'branches': _restore_bus_refs(c, new)}, 'Branch data edited')
            st.rerun()
        _issue_list('branch')


def view_generators():
    c = U.case()
    with st.container(border=True):
        U.card_header('Generator Data')
        num = lambda label, fmt='%.4g': st.column_config.NumberColumn(label, format=fmt)   # noqa: E731
        cols = ['id', 'bus', 'Pg', 'Qg', 'Qmax', 'Qmin', 'Vg', 'Pmax', 'Pmin', 'status', 'costC2', 'costC1', 'costC0']
        cfg = {'id': st.column_config.TextColumn('ID'), 'bus': st.column_config.TextColumn('Bus'),
               'Pg': num('PG MW'), 'Qg': num('QG MVAr'), 'Qmax': num('Qmax'), 'Qmin': num('Qmin'), 'Vg': num('V set pu'),
               'Pmax': num('Pmax'), 'Pmin': num('Pmin'),
               'status': st.column_config.SelectboxColumn('Status', options=[1, 0]),
               'costC2': num('Cost c2 $/MW²h'), 'costC1': num('Cost c1 $/MWh'), 'costC0': num('Cost c0 $/h')}
        recs = copy.deepcopy(c['generators'])
        for r in recs:
            r['bus'] = str(r['bus'])
        new = U.edit_records(recs, cols, cfg, f'ed_gen_{st.session_state.case_version}', dict(C.GEN_DEFAULTS),
                             new_id=lambda out, r: f'G{r.get("bus")}')
        if new is not None:
            U.touch_case({**c, 'generators': _restore_bus_refs(c, new, ('bus',))}, 'Generator data edited')
            st.rerun()
        _issue_list('gen')


def _as_id(v):
    """GridScope uses numeric bus ids; keep integers as integers so saved files stay identical."""
    sv = str(v).strip()
    return int(sv) if sv.lstrip('-').isdigit() else sv


def _restore_bus_refs(c, recs, keys=('from', 'to')):
    """Bus references are edited as text; store them back with the same type as the bus ids."""
    ids = {str(b['id']): b['id'] for b in c['buses']}
    for r in recs:
        for k in keys:
            r[k] = ids.get(str(r.get(k)), r.get(k))
    return recs


# ================================================================ results

def view_results():
    ac, stale = U.get_result('ac')
    if ac is None:
        U.notice('Run AC load flow (toolbar) to populate the results.')
        return
    U.stale_note(stale)
    if not ac.get('converged'):
        st.error('The last AC load flow did not converge — results are not reliable. See the Solver Log.')
        return
    if ac.get('q_switches'):
        st.info('Reactive-limit enforcement switched ' + ', '.join(
            f'bus {s["bus"]} → PQ at {s["limit"]} ({s["value"]:.1f} MVAr)' for s in ac['q_switches']) + '.')
    U.metric_cards([('Convergence', 'Converged', 'ok'), ('Iterations', str(ac['iterations']), None),
                    ('Real Losses', f'{ac["Ploss"]:.3f} MW', None), ('Reactive Losses', f'{ac["Qloss"]:.3f} MVAr', None)])
    with st.container(border=True):
        h1, h2 = st.columns([5, 1])
        with h1:
            U.card_header('Bus Results')
        with h2:
            U.csv_button(ac['buses'], 'bus_results.csv')
        df = ac['buses'].rename(columns=dict(name='Bus', type='Type', Vm='V pu', Va='Angle °', Pg='PG MW', Qg='QG MVAr',
                                             Pd='PD MW', Qd='QD MVAr', Pinj='P Injection MW', Qinj='Q Injection MVAr'))
        st.dataframe(df.drop(columns=['id']).style.format(precision=4, subset=['V pu']).format(precision=2, subset=[
            'Angle °', 'PG MW', 'QG MVAr', 'PD MW', 'QD MVAr', 'P Injection MW', 'Q Injection MVAr']).map(
            lambda v: f'color:{U.BAD}' if v < 0.95 or v > 1.05 else '', subset=['V pu']),
            hide_index=True, width='stretch', height=min(560, 38 + 35 * len(df)))
    if 'generators' in ac and len(ac['generators']):
        with st.container(border=True):
            h1, h2 = st.columns([5, 1])
            with h1:
                U.card_header('Generator Results', 'final outputs at the load-flow solution')
            with h2:
                U.csv_button(ac['generators'], 'generator_results.csv')
            gdf = ac['generators'].rename(columns=dict(
                id='Generator', bus='Bus', busType='Bus Type', status='Status', Psched='P Scheduled MW', P='P MW',
                dP='ΔP MW', Q='Q MVAr', S='S MVA', Pmin='Pmin MW', Pmax='Pmax MW', Qmin='Qmin MVAr',
                Qmax='Qmax MVAr', Vset='V Setpoint pu', V='V pu', loading='Loading % of rating', limits='Limits'))
            num = [c for c in gdf.columns if c not in ('Generator', 'Bus', 'Bus Type', 'Status', 'Limits')]
            st.dataframe(gdf.style.format(precision=2, subset=num, na_rep='—')
                         .format(precision=4, subset=['V Setpoint pu', 'V pu'])
                         .map(lambda v: f'color:{U.WARN};font-weight:700' if v not in ('ok', '') else '',
                              subset=['Limits'])
                         .map(lambda v: f'color:{U.BAD};font-weight:700' if v == v and v > 100 else '',
                              subset=['Loading % of rating']),
                         hide_index=True, width='stretch', height=min(460, 38 + 35 * len(gdf)))
            tot_p, tot_q = gdf.loc[gdf['Status'] == 'on', 'P MW'].sum(), gdf.loc[gdf['Status'] == 'on', 'Q MVAr'].sum()
            st.caption(f'Total generation {tot_p:.2f} MW / {tot_q:.2f} MVAr. ΔP = final − scheduled: the slack '
                       'generator picks up losses and any imbalance; PV generators hold their voltage set-point '
                       'unless a Q limit is reached.')
    with st.container(border=True):
        h1, h2 = st.columns([5, 1])
        with h1:
            U.card_header('Branch Flows')
        with h2:
            U.csv_button(ac['branches'], 'branch_flows.csv')
        df = ac['branches'][['id', 'from', 'to', 'Pf', 'Qf', 'Pt', 'Qt', 'loss', 'loading']].rename(columns=dict(
            id='Branch', **{'from': 'From'}, to='To', Pf='P From MW', Qf='Q From MVAr', Pt='P To MW', Qt='Q To MVAr',
            loss='Loss MW', loading='Loading %'))
        st.dataframe(df.style.format(precision=2).map(
            lambda v: f'color:{U.BAD};font-weight:700' if v > 100 else f'color:{U.WARN}' if v > 80 else '',
            subset=['Loading %']), hide_index=True, width='stretch', height=min(560, 38 + 35 * len(df)))


# ============================================================ contingency

def view_contingency():
    c = U.case()
    with st.container(border=True):
        U.card_header('DC Load Flow & N-1 Contingency Screening')
        a, b, d = st.columns([1, 2, 1])
        run_dc = a.button('Run DC Load Flow', width='stretch')
        method = b.selectbox('N-1 method', ['dc', 'ac'], label_visibility='collapsed', key='n1_method',
                             format_func=lambda m: 'N-1 method: DC (fast, thermal only)' if m == 'dc'
                             else 'N-1 method: AC (full, voltage-aware)')
        run_n1 = d.button('Run N-1 Contingency', type='primary', width='stretch')
        U.notice('DC load flow is a fast linearized approximation (angles and MW flows only — no voltage magnitude or '
                 'reactive power). N-1 screening re-solves the network once per in-service branch outage (PowSyBl '
                 'security analysis on OpenLoadFlow). Choose DC for a quick thermal-only sweep, or AC to also catch '
                 'bus voltages outside 0.95–1.05 pu. For a <b>dynamic</b> N-1 (fault + trip, rotor-angle stability), '
                 'use <b>Dynamic Stability → Dynamic N-1 screening</b>.')
    errs = U.validation()['errors']
    if (run_dc or run_n1) and errs:
        st.error(f"Can't run: {len(errs)} model validation error(s). See the sidebar.")
    elif run_dc:
        with st.spinner('Solving DC load flow…'):
            U.put_result('dc', U.on_pp_thread(loadflow.run_dc, c))
    elif run_n1:
        with st.spinner(f'Running {method.upper()} N-1 on {len(c["branches"])} outages…'):
            U.put_result('n1', U.on_pp_thread(contingency.run_n1, c, method, st.session_state.enforce_q))
    dc, dc_stale = U.get_result('dc')
    with st.container(border=True):
        U.card_header('DC Load Flow — Branch Flows')
        if dc is None:
            U.notice('Run DC load flow to populate this table.')
        else:
            U.stale_note(dc_stale)
            df = dc['branches'].rename(columns=dict(id='Branch', **{'from': 'From'}, to='To', Pf='P Flow MW',
                                                    rate='Rating MVA', loading='Loading %'))
            st.dataframe(df.style.format(precision=2).map(
                lambda v: f'color:{U.BAD};font-weight:700' if v > 100 else f'color:{U.WARN}' if v > 80 else '',
                subset=['Loading %']), hide_index=True, width='stretch', height=min(420, 38 + 35 * len(df)))
    n1, n1_stale = U.get_result('n1')
    with st.container(border=True):
        U.card_header('N-1 Contingency Summary', f'method: {n1["method"].upper()}' if n1 else None)
        if n1 is None:
            U.notice('Run N-1 contingency screening to populate this table.')
            return
        U.stale_note(n1_stale)
        cnt = n1['counts']
        U.metric_cards([('Secure', str(cnt.get('Secure', 0)), 'ok'), ('Violations', str(cnt.get('Violation', 0)), 'warn'),
                        ('Islanding', str(cnt.get('Islanding', 0)), 'warn'),
                        ('Did not converge', str(cnt.get('Did not converge', 0)), 'bad')])
        df = n1['table'][['branch', 'resultLabel', 'maxLoading', 'thermalCount', 'voltageCount', 'worst']].rename(
            columns=dict(branch='Outaged Branch', resultLabel='Result', maxLoading='Max Post-Contingency Loading %',
                         thermalCount='# Thermal', voltageCount='# Voltage', worst='Worst Violations'))
        colors = {'Secure': U.GOOD, 'Violation': U.WARN, 'Islanding': U.WARN, 'Did not converge': U.BAD}
        st.dataframe(df.style.format(precision=1, subset=['Max Post-Contingency Loading %']).map(
            lambda v: f'color:{colors.get(v, "")};font-weight:700', subset=['Result']),
            hide_index=True, width='stretch', height=min(560, 38 + 35 * len(df)))
        U.csv_button(n1['table'], f'n1_{n1["method"]}.csv')


# =================================================================== OPF

def view_opf():
    c = U.case()
    with st.container(border=True):
        h1, h2 = st.columns([5, 1])
        with h1:
            U.card_header('DC Optimal Power Flow')
        run = h2.button('Run DC-OPF', type='primary', width='stretch')
        U.notice('Minimizes total generator cost (quadratic cost curves, set per generator in Generator Data) subject to '
                 'the lossless linearized DC network: nodal power balance, branch thermal limits, generator P limits. '
                 'No reactive power, voltage magnitude or losses (DC-OPF, not AC-OPF). Missing cost coefficients '
                 'default to c2 = 0.01 $/MW²h, c1 = 20 $/MWh, c0 = 0. LMPs are the duals of the nodal balance '
                 'constraints. Solved as a convex QP with cvxpy (Clarabel).')
    if run:
        if U.validation()['errors']:
            st.error("Can't run: model validation errors (see the sidebar).")
        else:
            with st.spinner('Solving DC-OPF…'):
                U.put_result('opf', opf.run_dc_opf(c))
    r, stale = U.get_result('opf')
    if r is None:
        U.metric_cards([('Solve Status', '—', None), ('Total Cost', '—', None), ('Total Generation', '—', None),
                        ('LMP Range', '—', None)])
        return
    U.stale_note(stale)
    if not r['ok']:
        U.metric_cards([('Solve Status', r['status'], 'bad'), ('Total Cost', '—', None), ('Total Generation', '—', None),
                        ('LMP Range', '—', None)])
        st.error(r.get('message', ''))
        return
    lo, hi = r['lmp_range']
    U.metric_cards([('Solve Status', 'Optimal', 'ok'), ('Total Cost', f'${r["cost"]:,.2f}/h', None),
                    ('Total Generation', f'{r["total_pg"]:.2f} MW', None),
                    ('LMP Range', f'{lo:.2f} – {hi:.2f} $/MWh', 'warn' if hi - lo > 0.01 else None)])
    if r['defaulted']:
        st.caption('Default cost coefficients used for: ' + ', '.join(map(str, r['defaulted'])))
    g1, g2 = st.columns(2)
    with g1, st.container(border=True):
        U.card_header('Generator Dispatch')
        df = r['generators'][['id', 'bus', 'Pg', 'Pmin', 'Pmax', 'cost', 'marginal', 'limitStatus']].rename(columns=dict(
            id='Generator', bus='Bus', Pg='Dispatch MW', cost='Cost $/h', marginal='Marginal $/MWh', limitStatus='Limit'))
        st.dataframe(df.style.format(precision=2), hide_index=True, width='stretch')
    with g2, st.container(border=True):
        U.card_header('Bus Locational Marginal Prices')
        fig = go.Figure(go.Bar(x=r['buses']['name'].astype(str), y=r['buses']['lmp'], marker_color=U.ACCENT,
                               hovertemplate='%{x}: %{y:.3f} $/MWh<extra></extra>'))
        U.plotly_layout(fig, 240, legend=False)
        fig.update_yaxes(title='$/MWh')
        st.plotly_chart(fig, config=dict(displaylogo=False))
    with st.container(border=True):
        U.card_header('Bus LMPs and Branch Flows')
        a, b = st.columns(2)
        a.dataframe(r['buses'][['name', 'lmp', 'theta', 'Pd']].rename(columns=dict(
            name='Bus', lmp='LMP $/MWh', theta='Angle °', Pd='Load MW')).style.format(precision=3),
            hide_index=True, width='stretch', height=min(420, 38 + 35 * len(r['buses'])))
        b.dataframe(r['branches'].rename(columns=dict(id='Branch', **{'from': 'From'}, to='To', flow='Flow MW',
                                                      rate='Rate MVA', loading='Loading %', binding='Binding')).style
                    .format(precision=2), hide_index=True, width='stretch', height=min(420, 38 + 35 * len(r['branches'])))


# ================================================================ faults

def view_faults():
    c = U.case()
    with st.container(border=True):
        h1, h2, h3 = st.columns([4, 1.4, 1.2])
        with h1:
            U.card_header('IEC 60909 / ANSI Short-Circuit Analysis')
        init = h2.button('Initialize Missing Fault Data', width='stretch')
        run = h3.button('Run Fault Study', type='primary', width='stretch')
        U.notice('Same engine as GridScope Phase 2 (ported and cross-checked against its JavaScript to 1e-13): '
                 'sequence networks from the case data, transformer winding connections set the zero-sequence '
                 'topology, IEC max/min cases with K_G / K_T, ANSI momentary/interrupting duties. Values initialized '
                 'as defaults remain preliminary until replaced with equipment data. Buses with no zero-sequence '
                 'path to ground (behind a delta winding) report ~0 ground-fault current instead of an error.')
        g = st.columns(6)
        std = g[0].selectbox('Calculation convention', ['iec', 'ansi'], format_func=lambda x: 'IEC 60909' if x == 'iec' else 'ANSI / IEEE')
        iec_case = g[1].selectbox('IEC study case', ['max', 'min'], format_func=lambda x: 'Maximum fault' if x == 'max' else 'Minimum fault')
        corr = g[2].selectbox('IEC correction factors', [True, False], format_func=lambda x: 'Apply K_G / K_T' if x else 'Off (uncorrected)')
        ftype = g[3].selectbox('Fault type', ['3ph', 'slg', 'll', 'llg'], format_func=lambda x: {
            '3ph': 'Three-phase', 'slg': 'Single line-to-ground', 'll': 'Line-to-line', 'llg': 'Double line-to-ground'}[x])
        where = g[4].selectbox('Fault location', ['all'] + [str(b['id']) for b in c['buses']],
                               format_func=lambda x: 'All buses' if x == 'all' else f'Bus {x}')
        rf = g[5].number_input('Fault R (ohm)', value=0.0, step=0.001, format='%.3f')
        g = st.columns(6)
        xf = g[0].number_input('Fault X (ohm)', value=0.0, step=0.001, format='%.3f')
        pre = g[1].selectbox('Prefault voltage', ['standard', 'loadflow', 'one'], format_func=lambda x: {
            'standard': 'Standard voltage factor', 'loadflow': 'Solved AC voltage', 'one': '1.0 pu'}[x])
        cmax = g[2].number_input('IEC cmax', value=1.10, step=0.01)
        cmin = g[3].number_input('IEC cmin', value=0.95, step=0.01)
        th = g[4].number_input('Min-case conductor end temp θe (°C)', value=80.0, step=1.0)
        bt = g[5].number_input('Breaking time (s)', value=0.10, step=0.01)
        g = st.columns(6)
        tt = g[0].number_input('Thermal duration (s)', value=1.0, step=0.1)
        mc = g[1].number_input('ANSI momentary cycles', value=0.5, step=0.5)
        ic = g[2].number_input('ANSI interrupt cycles', value=5.0, step=0.5)
        hz = g[3].number_input('System frequency (Hz)', value=50.0, step=1.0)
        bka = g[4].number_input('Breaker interrupt rating (kA)', value=40.0, step=0.1)
    if init:
        new = faults.ensure_fault_data(copy.deepcopy(c), mark_assumed=True)
        U.touch_case(new, 'Missing fault data initialized with defaults')
        st.rerun()
    if run:
        if U.validation()['errors']:
            st.error("Can't run fault study: model validation errors (see the sidebar).")
        else:
            vm = None
            ac, stale = U.get_result('ac')
            if pre == 'loadflow':
                if ac and ac.get('converged') and not stale:
                    vm = {str(r.id): r.Vm for r in ac['buses'].itertuples()}
                else:
                    st.warning('No current AC load-flow solution — using 1.0 pu prefault voltage.')
            try:
                U.put_result('fault', faults.run_fault_study(
                    c, standard=std, iec_case=iec_case, corrections=corr, fault_type=ftype, fault_bus=where, Rf=rf,
                    Xf=xf, prefault=pre, cmax=cmax, cmin=cmin, theta_end=th, break_time=bt, thermal_time=tt,
                    mom_cycles=mc, int_cycles=ic, hz=hz, breaker_ka=bka, loadflow_vm=vm))
            except ValueError as e:
                st.error(f'Fault study failed: {e}')
    r, stale = U.get_result('fault')
    a = faults._assumed_count(c)
    data_status = 'Complete' if not (a['branches'] or a['generators']) else f'{a["branches"] + a["generators"]} assumed'
    if r is None:
        U.metric_cards([('Maximum Initial Sym. RMS', '—', None), ('Maximum Peak / Momentary', '—', None),
                        ('Maximum Fault Level', '—', None), ('Sequence Data', data_status, 'ok' if data_status == 'Complete' else 'warn')])
    else:
        U.stale_note(stale)
        U.metric_cards([('Maximum Initial Sym. RMS', f'{r["max_Ik"]:.2f} kA', None),
                        ('Maximum Peak / Momentary', f'{r["max_peak"]:.2f} kA', None),
                        ('Maximum Fault Level', f'{r["max_MVA"]:.0f} MVA', None),
                        ('Sequence Data', data_status, 'ok' if data_status == 'Complete' else 'warn')])
        for n in r.get('notes', []):
            st.info(n)
        with st.container(border=True):
            h1, h2 = st.columns([5, 1])
            with h1:
                U.card_header('Fault Results')
            with h2:
                U.csv_button(r['results'], 'fault_results.csv')
            df = r['results'][['name', 'baseKV', 'faultLabel', 'Ik', 'peak', 'Ib', 'Ith', 'MVA', 'xr', 'dutyPct',
                               'Z1mag', 'Z2mag', 'Z0mag']].rename(columns=dict(
                name='Bus', baseKV='kV', faultLabel='Fault', Ik="Ik'' kA", peak='Peak/Mom. kA', Ib='Ib/Int. kA',
                Ith='Ith kA', MVA='Fault MVA', xr='X/R', dutyPct='Duty %', Z1mag='|Z1| pu', Z2mag='|Z2| pu', Z0mag='|Z0| pu'))
            st.dataframe(df.style.format(precision=3).format(precision=1, subset=['Fault MVA', 'Duty %']).map(
                lambda v: f'color:{U.BAD}' if v > 100 else f'color:{U.WARN}' if v > 80 else f'color:{U.GOOD}',
                subset=['Duty %']), hide_index=True, width='stretch', height=min(480, 38 + 35 * len(df)))
        a1, a2 = st.columns(2)
        with a1, st.container(border=True):
            U.card_header('Source Contributions', 'positive-sequence')
            st.dataframe(r['contributions'].rename(columns=dict(faultBus='Faulted Bus', source='Source', sourceBus='Source Bus',
                                                                kA='Contribution kA', pct='Contribution %')).style.format(precision=3),
                         hide_index=True, width='stretch', height=300)
        with a2, st.container(border=True):
            U.card_header('IEC 60909 Impedance Correction Factors', 'positive/negative sequence only')
            if len(r['corrections']):
                st.dataframe(r['corrections'].rename(columns=dict(equipment='Equipment', type='Type', where='Bus',
                                                                  factor='Correction Factor')).style.format(precision=4),
                             hide_index=True, width='stretch', height=300)
            else:
                U.notice('Correction factors off or ANSI selected — uncorrected impedances.')
    with st.container(border=True):
        U.card_header('Sequence-Network and Transformer Input Data')
        t1, t2 = st.tabs(['Branches / Transformers', 'Generators / Sources'])
        with t1:
            cols = ['id', 'equipmentType', 'fromConnection', 'toConnection', 'vectorGroup', 'r2', 'x2', 'r0', 'x0',
                    'fromRn', 'fromXn', 'toRn', 'toXn', 'z0mode']
            conn = ['line', 'wye-grounded', 'wye', 'delta']
            cfg = {k: st.column_config.NumberColumn(k, format='%.5g') for k in cols[5:13]}
            cfg.update({'id': st.column_config.TextColumn('ID', disabled=True),
                        'equipmentType': st.column_config.SelectboxColumn('Type', options=['line', 'transformer']),
                        'fromConnection': st.column_config.SelectboxColumn('From winding', options=conn),
                        'toConnection': st.column_config.SelectboxColumn('To winding', options=conn),
                        'vectorGroup': st.column_config.TextColumn('Vector group'),
                        'z0mode': st.column_config.SelectboxColumn('Z0 mode', options=['auto', 'connected', 'blocked'])})
            _seq_editor(c, 'branches', cols, cfg, 'ed_seqbr')
        with t2:
            cols = ['id', 'sourceType', 'ratedMVA', 'ratedKV', 'cosPhiRated', 'r1', 'x1', 'r2', 'x2', 'r0', 'x0', 'rn', 'xn',
                    'steadyFactor', 'minCaseInclude', 'r1Min', 'x1Min']
            cfg = {k: st.column_config.NumberColumn(k, format='%.5g') for k in cols[2:14] + ['r1Min', 'x1Min']}
            cfg.update({'id': st.column_config.TextColumn('ID', disabled=True),
                        'sourceType': st.column_config.SelectboxColumn('Source type', options=[
                            'synchronous-generator', 'external-grid', 'motor', 'converter']),
                        'minCaseInclude': st.column_config.SelectboxColumn('Min-case', options=['auto', 'include', 'exclude'])})
            _seq_editor(c, 'generators', cols, cfg, 'ed_seqgen')


def _seq_editor(c, key, cols, cfg, edkey):
    recs = copy.deepcopy(c[key])
    df = pd.DataFrame([{k: r.get(k) for k in cols} for r in recs], columns=cols)
    edited = st.data_editor(df, column_config=cfg, hide_index=True, width='stretch', num_rows='fixed',
                            key=f'{edkey}_{st.session_state.case_version}', height=min(420, 38 + 35 * len(df)))
    if not edited.equals(df):
        for r, (_, row) in zip(recs, edited.iterrows()):
            for k in cols[1:]:
                v = row[k]
                r[k] = None if (isinstance(v, float) and np.isnan(v)) else (v.item() if hasattr(v, 'item') else v)
                r.pop('seqAssumed', None)
        U.touch_case({**c, key: recs}, 'Sequence data edited')
        st.rerun()


# ================================================================ charts

def view_plots():
    ac, stale = U.get_result('ac')
    if ac is None or not ac.get('converged'):
        U.notice('Run AC load flow to see the bus voltage profiles.')
        return
    U.stale_note(stale)
    b = ac['buses']
    names = b['name'].astype(str)
    a1, a2 = st.columns(2)
    with a1, st.container(border=True):
        U.card_header('Bus Voltage Magnitude')
        colors = [U.BAD if (v < 0.95 or v > 1.05) else U.ACCENT for v in b.Vm]
        fig = go.Figure(go.Bar(x=names, y=b.Vm, marker_color=colors, hovertemplate='%{x}: %{y:.4f} pu<extra></extra>'))
        fig.add_hrect(y0=0.95, y1=1.05, fillcolor=U.GOOD, opacity=0.07, line_width=0)
        for y in (0.95, 1.05):
            fig.add_hline(y=y, line_dash='dot', line_color=U.WARN, line_width=1)
        U.plotly_layout(fig, 300, legend=False)
        fig.update_yaxes(title='pu', range=[min(0.9, b.Vm.min() - 0.01), max(1.1, b.Vm.max() + 0.01)])
        st.plotly_chart(fig, config=dict(displaylogo=False))
    with a2, st.container(border=True):
        U.card_header('Bus Voltage Angle')
        fig = go.Figure(go.Bar(x=names, y=b.Va, marker_color=U.ACCENT2, hovertemplate='%{x}: %{y:.2f}°<extra></extra>'))
        U.plotly_layout(fig, 300, legend=False)
        fig.update_yaxes(title='degrees')
        st.plotly_chart(fig, config=dict(displaylogo=False))
    with st.container(border=True):
        U.card_header('Branch Loading')
        br = ac['branches'].sort_values('loading', ascending=False)
        colors = [U.BAD if v > 100 else U.WARN if v > 80 else '#60769a' for v in br.loading]
        fig = go.Figure(go.Bar(x=br['id'].astype(str), y=br.loading, marker_color=colors,
                               hovertemplate='%{x}: %{y:.1f}%<extra></extra>'))
        fig.add_hline(y=100, line_dash='dot', line_color=U.BAD, line_width=1)
        U.plotly_layout(fig, 280, legend=False)
        fig.update_yaxes(title='% of rating')
        st.plotly_chart(fig, config=dict(displaylogo=False))
