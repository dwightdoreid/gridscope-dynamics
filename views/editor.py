"""Single-line diagram component: the interactive view (animated power-flow arrows, pan / zoom,
click to inspect) and, with Edit on, the diagram editor.

The browser side (sld_editor.js, a Streamlit components-v2 component) draws the same Scene as the
read-only diagram and reports actions: select, move a bus, add a bus / line / transformer /
generator / load / shunt, delete, undo. apply_action() turns an action into a new case (pure, so it
is unit-tested without a browser); the case then goes through the usual touch_case() so results are
marked out of date and the sidebar validation runs on it.

Element properties are edited in the Selected Element panel (property_form()).
"""
from __future__ import annotations

import copy
from pathlib import Path

import streamlit as st

from gridscope import case as C
from gridscope.edit import _tidy, apply_action  # noqa: F401  (pure edits live in the engine)
from views import common as U

_HERE = Path(__file__).resolve().parent
UNDO_DEPTH = 50

CSS = """
.gs-sld { position: relative; width: 100%; background: #121a2e; border-radius: 10px; overflow: hidden;
          outline: none; font-family: Inter, system-ui, sans-serif; user-select: none; }
.gs-sld:focus-visible { box-shadow: 0 0 0 1px #4ea1ff inset; }
.gs-svg { display: block; touch-action: none; }
.gs-svg .bg { fill: transparent; }
.gs-sld[data-tool="select"] .gs-svg .bg { cursor: grab; }
.gs-sld[data-tool="bus"] .gs-svg .bg { cursor: copy; }
.gs-sld:not([data-tool="select"]) .hit { cursor: crosshair; }
.gs-sld[data-tool="select"] .hit.bus { cursor: move; }
.gs-sld[data-tool="select"] .hit.branch, .gs-sld[data-tool="select"] .hit.dev { cursor: pointer; }
.gs-svg .lbl { pointer-events: none; font-family: Inter, system-ui, sans-serif; }
.gs-svg .nodetxt { pointer-events: none; fill: #08101d; font-size: 10px; }
.gs-svg .sel { filter: drop-shadow(0 0 3px #facc15) drop-shadow(0 0 2px #facc15); }
.gs-svg .busbar.sel { stroke: #facc15; }
.gs-svg .faded { opacity: 0.12; }
.gs-svg .rubber { stroke: #facc15; stroke-width: 1.5; stroke-dasharray: 6 4; fill: none; }
.gs-tools { position: absolute; top: 8px; left: 8px; display: flex; gap: 4px; align-items: center; z-index: 2;
            background: rgba(23, 34, 59, 0.92); border: 1px solid #283754; border-radius: 9px; padding: 4px; }
.gs-tools button { background: transparent; color: #eef4ff; border: 1px solid transparent; border-radius: 6px;
                   min-width: 30px; height: 28px; font-size: 14px; cursor: pointer; padding: 0 6px; }
.gs-tools button:hover:not(:disabled) { background: #283754; }
.gs-tools button.on { background: #4ea1ff; color: #08101d; }
.gs-tools button:disabled { opacity: 0.35; cursor: default; }
.gs-tools button.danger:hover:not(:disabled) { background: #fb7185; color: #08101d; }
.gs-tools .sep { width: 1px; height: 20px; background: #283754; margin: 0 2px; }
.gs-tools .snap { color: #9fb0ca; font-size: 12px; padding: 0 6px; cursor: pointer; }
.gs-tools .seglbl { color: #9fb0ca; font-size: 12px; padding: 0 2px 0 4px; }
.gs-tools .seg { display: inline-flex; border: 1px solid #283754; border-radius: 7px; overflow: hidden; }
.gs-tools .seg button { border-radius: 0; min-width: 34px; font-size: 12px; font-weight: 600; }
.gs-tools .seg button[data-flow="P"].on { background: #fbbf24; }
.gs-tools .seg button[data-flow="Q"].on { background: #22d3ee; }
.gs-tools .seg button[data-flow="off"].on { background: #283754; color: #eef4ff; }
.gs-svg .farrow { pointer-events: none; opacity: 0.95; }
.gs-sld.flows-on .gs-svg .brid { display: none; }
.gs-svg .flowlbl { font-weight: 600; paint-order: stroke; stroke: #121a2e; stroke-width: 3px; }
.gs-hint { position: absolute; left: 10px; bottom: 8px; z-index: 2; color: #9fb0ca; font-size: 12px;
           background: rgba(11, 16, 32, 0.75); padding: 3px 8px; border-radius: 6px; pointer-events: none; }
.gs-tip { position: absolute; display: none; z-index: 3; pointer-events: none; background: #17223b;
          border: 1px solid #283754; color: #eef4ff; font-size: 12px; padding: 6px 8px; border-radius: 6px;
          max-width: 280px; line-height: 1.35; }
"""

_COMPONENT = st.components.v2.component(
    'gridscope_sld_editor', css=CSS, js=(_HERE / 'sld_editor.js').read_text())


# ----------------------------------------------------------------- session glue

def commit(new_case, msg, select=None):
    """Store an edit made from the diagram (or its property panel), with undo."""
    ss = st.session_state
    stack = ss.setdefault('sld_undo', [])
    stack.append(copy.deepcopy(ss.case))
    del stack[:-UNDO_DEPTH]
    U.touch_case(new_case, msg)
    ss.inspect = select
    ss.sld_message = msg


def undo():
    ss = st.session_state
    stack = ss.get('sld_undo') or []
    if stack:
        U.touch_case(stack.pop(), 'Undo')
        ss.sld_message = 'Undone'
        sel = ss.get('inspect')
        if sel and not _exists(ss.case, sel):
            ss.inspect = None


def _exists(case, sel):
    kind, eid = sel
    pool = {'bus': case['buses'], 'branch': case['branches'], 'gen': case['generators']}.get(kind, [])
    return any(str(x['id']) == str(eid) for x in pool)


def render(scene, height=610, editable=True):
    """Mount the diagram (editor when editable); handle the action it sent (if any) and rerun."""
    ss = st.session_state
    sel = ss.get('inspect')
    if sel and not _exists(ss.case, sel):
        sel = ss.inspect = None
    payload = dict(scene=scene.to_json(), selected=list(sel) if sel else None, height=height,
                   token=ss.get('case_load', 0), can_undo=bool(ss.get('sld_undo')),
                   message=ss.pop('sld_message', None), editable=editable,
                   name=U.slug(ss.case['name']), title=ss.case['name'], maximized=bool(ss.get('diag_max')))
    # height 'content': the drawing sets its own height (fixed, or the window's when maximized)
    res = _COMPONENT(key='sld_editor', data=payload, height='content', on_action_change=lambda: None)
    act = getattr(res, 'action', None) if res is not None else None
    if not act:
        return
    if act.get('type') == 'maximize':
        ss.diag_max = bool(act.get('on'))
        st.rerun()
    if not editable and act.get('type') != 'select':
        return                                     # view mode: only clicks to inspect
    if act.get('type') == 'undo':
        undo()
        st.rerun()
    new_case, msg, select = apply_action(ss.case, act)
    if new_case is not None:
        commit(new_case, msg, select)
        st.rerun()
    changed = select != (list(sel) if sel else None)
    ss.inspect = select
    if msg:
        ss.sld_message = msg
    if changed or msg:
        st.rerun()


# ----------------------------------------------------------------- property panel

def property_form(case, sel):
    """Editable properties of the selected bus / branch / generator. Returns True when it rendered."""
    kind, eid = sel
    ver = st.session_state.case_version
    if kind == 'bus':
        b = next((b for b in case['buses'] if str(b['id']) == str(eid)), None)
        if b is None:
            return False
        with st.form(f'pf_bus_{eid}_{ver}', border=False):
            st.markdown(f'<h4 style="margin:0 0 6px">Bus {eid}</h4>', unsafe_allow_html=True)
            name = st.text_input('Name', b.get('name', ''))
            c1, c2 = st.columns(2)
            btype = c1.selectbox('Type', C.BUS_TYPES, index=list(C.BUS_TYPES).index(b['type'])
                                 if b['type'] in C.BUS_TYPES else 2)
            kv = c2.number_input('Base kV', value=float(C._num(b['baseKV'], 138)), min_value=0.0, format='%g')
            pd_, qd = c1.number_input('Load P (MW)', value=float(C._num(b['Pd'], 0)), format='%g'), \
                c2.number_input('Load Q (MVAr)', value=float(C._num(b['Qd'], 0)), format='%g')
            gs, bs = c1.number_input('Shunt G (MW)', value=float(C._num(b['Gs'], 0)), format='%g'), \
                c2.number_input('Shunt B (MVAr)', value=float(C._num(b['Bs'], 0)), format='%g',
                                help='Positive = capacitive')
            vm = c1.number_input('V set (pu)', value=float(C._num(b['Vm'], 1)), min_value=0.0, step=0.01,
                                 format='%.4f')
            va = c2.number_input('Angle (°)', value=float(C._num(b['Va'], 0)), format='%g')
            if st.form_submit_button('Apply', type='primary', width='stretch'):
                new = copy.deepcopy(case)
                nb = next(x for x in new['buses'] if str(x['id']) == str(eid))
                nb.update(name=name, type=btype, baseKV=_tidy(kv), Pd=pd_, Qd=qd, Gs=gs, Bs=bs, Vm=vm, Va=va)
                if new != case:
                    commit(new, f'Bus {eid} edited', ['bus', str(eid)])
                    st.rerun()
        gens = [g for g in case['generators'] if str(g['bus']) == str(eid)]
        if gens:
            st.caption('Generators here: ' + ', '.join(str(g['id']) for g in gens) + ' (click one to edit it)')
        return True

    if kind == 'branch':
        br = next((x for x in case['branches'] if str(x['id']) == str(eid)), None)
        if br is None:
            return False
        with st.form(f'pf_br_{eid}_{ver}', border=False):
            st.markdown(f'<h4 style="margin:0 0 6px">{eid} <span class="gs-badge">{br["from"]} → {br["to"]}</span>'
                        f'</h4>', unsafe_allow_html=True)
            c1, c2 = st.columns(2)
            types = ['line', 'transformer']
            et = c1.selectbox('Type', types, index=types.index(br.get('equipmentType', 'line'))
                              if br.get('equipmentType') in types else 0)
            on = c2.toggle('In service', value=bool(C._num(br.get('status'), 1)))
            r = c1.number_input('R (pu)', value=float(C._num(br['r'], 0)), format='%.6g', step=0.001)
            x = c2.number_input('X (pu)', value=float(C._num(br['x'], 0.1)), format='%.6g', step=0.001)
            b = c1.number_input('B (pu)', value=float(C._num(br['b'], 0)), format='%.6g', step=0.001)
            rate = c2.number_input('Rating (MVA)', value=float(C._num(br.get('rate'), 0)), min_value=0.0, format='%g')
            tap = c1.number_input('Tap ratio', value=float(C._num(br.get('tap'), 1)), min_value=0.0, step=0.0125,
                                  format='%.4f')
            shift = c2.number_input('Phase shift (°)', value=float(C._num(br.get('shift'), 0)), format='%g')
            if st.form_submit_button('Apply', type='primary', width='stretch'):
                new = copy.deepcopy(case)
                nb = next(y for y in new['branches'] if str(y['id']) == str(eid))
                nb.update(equipmentType=et, status=1 if on else 0, r=r, x=x, b=b, rate=rate, tap=tap, shift=shift)
                if new != case:
                    commit(new, f'Branch {eid} edited', ['branch', str(eid)])
                    st.rerun()
        return True

    if kind == 'gen':
        g = next((x for x in case['generators'] if str(x['id']) == str(eid)), None)
        if g is None:
            return False
        with st.form(f'pf_gen_{eid}_{ver}', border=False):
            st.markdown(f'<h4 style="margin:0 0 6px">{eid} <span class="gs-badge">bus {g["bus"]}</span></h4>',
                        unsafe_allow_html=True)
            c1, c2 = st.columns(2)
            pg = c1.number_input('P (MW)', value=float(C._num(g['Pg'], 0)), format='%g')
            vg = c2.number_input('V set (pu)', value=float(C._num(g['Vg'], 1)), min_value=0.0, step=0.01,
                                 format='%.4f')
            pmin = c1.number_input('Pmin (MW)', value=float(C._num(g['Pmin'], 0)), format='%g')
            pmax = c2.number_input('Pmax (MW)', value=float(C._num(g['Pmax'], 0)), format='%g')
            qmin = c1.number_input('Qmin (MVAr)', value=float(C._num(g['Qmin'], 0)), format='%g')
            qmax = c2.number_input('Qmax (MVAr)', value=float(C._num(g['Qmax'], 0)), format='%g')
            on = st.toggle('In service', value=bool(C._num(g.get('status'), 1)))
            if st.form_submit_button('Apply', type='primary', width='stretch'):
                new = copy.deepcopy(case)
                ng = next(y for y in new['generators'] if str(y['id']) == str(eid))
                ng.update(Pg=pg, Vg=vg, Pmin=pmin, Pmax=pmax, Qmin=qmin, Qmax=qmax, status=1 if on else 0)
                if new != case:
                    commit(new, f'Generator {eid} edited', ['gen', str(eid)])
                    st.rerun()
        st.caption('Dynamic machine data and controls: Dynamic Stability view.')
        return True
    return False
