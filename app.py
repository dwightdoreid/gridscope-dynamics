"""
GridScope Dynamics — GridScope's power-system analysis workflow in Python (PowSyBl
OpenLoadFlow + numpy/cvxpy), plus dynamic and transient stability studies with Dynawo.

Run with:   streamlit run app.py
"""
import json

import streamlit as st

st.set_page_config(page_title='GridScope Dynamics — Power System Analysis', page_icon='⚡', layout='wide',
                   initial_sidebar_state='expanded')

from gridscope import case as C          # noqa: E402
from views import common as U            # noqa: E402
from views import dynamic, steady        # noqa: E402

CSS = """
<style>
:root{--bg:#0b1020;--panel:#121a2e;--panel2:#17223b;--line:#283754;--text:#eef4ff;--muted:#9fb0ca;
      --accent:#4ea1ff;--accent2:#2dd4bf;--warn:#f59e0b;--bad:#fb7185;--good:#34d399}
.block-container{padding-top:2.2rem;padding-bottom:2rem;max-width:100%}
[data-testid="stHeader"]{background:transparent;height:0}
[data-testid="stSidebar"]{background:#0e1628;border-right:1px solid var(--line)}
[data-testid="stSidebar"] .stButton button{width:100%;justify-content:flex-start;text-align:left;border:0;
  background:transparent;color:var(--muted);padding:.55rem .75rem;border-radius:8px}
[data-testid="stSidebar"] .stButton button:hover{background:var(--panel2);color:var(--text)}
[data-testid="stSidebar"] .stButton button[kind="primary"]{background:var(--panel2);color:var(--text);font-weight:700}
[data-testid="stVerticalBlockBorderWrapper"]{background:var(--panel);border-color:var(--line)!important;
  border-radius:12px;box-shadow:0 14px 40px rgba(0,0,0,.28)}
.gs-brand{font-size:21px;font-weight:800;letter-spacing:.2px;line-height:2.4rem;white-space:nowrap}.gs-brand span{color:var(--accent)}
.gs-brand small{font-size:11px;color:var(--accent2);font-weight:700;margin-left:6px;vertical-align:super}
.gs-status{font-size:12px;color:var(--muted);text-align:right;line-height:2.4rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.gs-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:.4rem}
.gs-head h3{margin:0;font-size:15px;font-weight:700;padding:0}
.gs-badge{padding:3px 8px;border-radius:999px;font-size:11px;background:#20304d;color:#cfe1ff;font-weight:500}
.gs-notice{padding:10px 12px;border-left:3px solid var(--accent);background:#101c32;color:var(--muted);
  border-radius:5px;font-size:12.5px;line-height:1.5;margin:.2rem 0 .6rem}
.gs-metric{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 15px;margin-bottom:12px}
.gs-metric .label{color:var(--muted);font-size:12px}.gs-metric .value{font-size:21px;font-weight:800;margin-top:4px}
.gs-metric .value.ok,.valOk{color:var(--good)}.gs-metric .value.bad,.valErr{color:var(--bad)}
.gs-metric .value.warn,.valWarn{color:var(--warn)}
.gs-kv{display:flex;justify-content:space-between;color:var(--muted);margin:6px 0;font-size:13px;gap:8px}
.gs-kv b{color:var(--text);text-align:right}
.gs-issue{padding:7px 9px;border-radius:6px;margin-top:6px;font-size:11.5px;line-height:1.4}
.gs-issue.err{background:#2a1420;border-left:3px solid var(--bad);color:#ffd8df}
.gs-issue.warn{background:#2a2312;border-left:3px solid var(--warn);color:#ffe9c0}
.gs-legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin:-4px 0 4px}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:5px}
.gs-side-card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px;margin-top:14px}
.gs-side-card h4{margin:0 0 8px;font-size:14px}
.gs-new{font-size:10px;background:#123a2a;color:#8cffcb;border-radius:999px;padding:1px 6px;margin-left:6px}
</style>
"""

VIEWS = [
    ('network', '◫  Network Diagram', steady.view_network),
    ('buses', '●  Bus Data', steady.view_buses),
    ('branches', '━  Branch Data', steady.view_branches),
    ('generators', '⚡  Generator Data', steady.view_generators),
    ('results', '▦  Analysis Results', steady.view_results),
    ('contingency', '⛒  Contingency (N-1)', steady.view_contingency),
    ('opf', '💲  Optimal Power Flow', steady.view_opf),
    ('faults', '⚠  Short-Circuit / Faults', steady.view_faults),
    ('plots', '⌁  Profiles & Charts', steady.view_plots),
    ('dynamics', '〰  Dynamic Stability  ✦', dynamic.view_dynamics),
]


def header():
    c = U.case()
    cols = st.columns([2.0, 0.8, 1.45, 0.7, 1.55, 0.85, 0.95, 1.6], vertical_alignment='center')
    cols[0].markdown('<div class="gs-brand">Grid<span>Scope</span><small>DYNAMICS</small></div>', unsafe_allow_html=True)
    if cols[1].button('＋ New', width='stretch', help='New network'):
        U.set_case(C.blank_case())
        st.rerun()
    pick = cols[2].selectbox('Case', list(C.BUILT_IN_CASES), index=1, label_visibility='collapsed', key='case_pick')
    if cols[3].button('Load', width='stretch', help='Load the selected built-in case'):
        U.set_case(C.load_builtin(pick))
        st.session_state.view = 'network'
        st.rerun()
    if cols[4].button('▶ Run AC Load Flow', type='primary', width='stretch'):
        with st.spinner('Solving…'):
            ok = steady.run_ac_load_flow()
        st.session_state.view = 'results' if ok else 'network'
        st.rerun()
    # filled after the view has run: edits made while rendering the view must be in the saved file
    st.session_state['_save_slot'] = cols[5].empty()
    with cols[6].popover('Open', icon=':material/upload_file:', width='stretch', help='Open a GridScope JSON network'):
        up = st.file_uploader('GridScope JSON network', type=['json'], key='upload')
        # the uploader keeps returning the same file on every rerun: load it once only, or every later
        # edit would be replaced by the file again
        fid = getattr(up, 'file_id', None) or (up and (up.name, up.size))
        if up is not None and fid != st.session_state.get('loaded_upload'):
            st.session_state.loaded_upload = fid
            try:
                U.set_case(C.load_json_text(up.getvalue().decode('utf-8')))
                st.session_state.view = 'network'
                st.rerun()
            except (ValueError, KeyError) as e:
                st.error(f'Could not read that file: {e}')
    cols[7].markdown(f'<div class="gs-status">{st.session_state.status}</div>', unsafe_allow_html=True)


def sidebar():
    with st.sidebar:
        for key, label, _ in VIEWS:
            if st.button(label, key=f'nav_{key}', type='primary' if st.session_state.view == key else 'secondary'):
                st.session_state.view = key
                st.rerun()
        c = U.case()
        s = C.summary(c)
        with st.container(border=True):
            st.markdown('<h4 style="margin:0;font-size:14px">Case Summary</h4>', unsafe_allow_html=True)
            name = st.text_input('Name', c['name'], key=f'case_name_{st.session_state.case_version}')
            if name != c['name']:
                U.touch_case({**c, 'name': name}, 'Case renamed')
                st.rerun()
            st.markdown(''.join(f'<div class="gs-kv"><span>{k}</span><b>{v}</b></div>' for k, v in [
                ('Base MVA', f'{s["Base MVA"]:g}'), ('Buses', s['Buses']), ('Branches', s['Branches']),
                ('Generators', s['Generators']), ('Total Load', f'{s["Total load"]:.1f} MW')]),
                unsafe_allow_html=True)
        v = U.validation()
        if not v['errors'] and not v['warnings']:
            status = '<b class="valOk">OK</b>'
        elif v['errors']:
            status = f'<b class="valErr">{len(v["errors"])} error(s)</b>'
        else:
            status = f'<b class="valWarn">{len(v["warnings"])} warning(s)</b>'
        issues = ''.join(f'<div class="gs-issue err">{i["message"]}</div>' for i in v['errors'][:8]) + \
            ''.join(f'<div class="gs-issue warn">{i["message"]}</div>' for i in v['warnings'][:8])
        more = len(v['errors']) + len(v['warnings']) - 16
        st.markdown(f'<div class="gs-side-card"><h4>Model Validation</h4><div class="gs-kv"><span>Status</span>{status}</div>'
                    f'<div style="max-height:220px;overflow:auto">{issues}</div>'
                    + (f'<div class="gs-kv"><span>… and {more} more</span></div>' if more > 0 else '') + '</div>',
                    unsafe_allow_html=True)
        st.caption('Engines: PowSyBl OpenLoadFlow · numpy · cvxpy · Dynawo. Not a certified tool — check results '
                   'independently before engineering use.')


def main():
    st.markdown(CSS, unsafe_allow_html=True)
    U.init_state()
    header()
    sidebar()
    dict((k, f) for k, _, f in VIEWS)[st.session_state.view]()
    c = U.case()
    st.session_state.pop('_save_slot').download_button(
        '⭳ Save', json.dumps(U.clean_case_for_export(c), indent=1), file_name=f'{U.slug(c["name"])}.json',
        mime='application/json', width='stretch', help='Download the network with all its dynamic settings')


main()
