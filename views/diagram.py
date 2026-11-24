"""Single-line diagram (plotly), drawn from the bus x/y coordinates stored in the case, like GridScope."""
from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from gridscope.case import _num
from views.common import BAD, LINE, MUTED, PANEL, SLATE, TEXT, WARN, ACCENT, plotly_layout

TYPE_COLOR = {'Slack': BAD, 'PV': ACCENT, 'PQ': SLATE}


def _volt_color(v):
    """Diverging colour for an overlay value in pu (0.9 .. 1.1)."""
    if v is None or not np.isfinite(v):
        return '#475569'
    t = float(np.clip((v - 0.9) / 0.2, 0, 1))
    stops = [(0, (251, 113, 133)), (0.25, (245, 158, 11)), (0.5, (52, 211, 153)), (0.75, (78, 161, 255)),
             (1, (167, 139, 250))]
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            f = (t - t0) / (t1 - t0)
            return 'rgb(%d,%d,%d)' % tuple(int(a + f * (b - a)) for a, b in zip(c0, c1))
    return 'rgb(167,139,250)'


def build_figure(case, lf=None, overlay=None, open_branches=(), height=610):
    """lf: AC load-flow result dict (or None). overlay: {'voltages': {bus: pu}, 'label': str} from a
    dynamic simulation snapshot; open_branches: branch ids drawn as open (tripped)."""
    buses = case['buses']
    pos = {str(b['id']): (_num(b.get('x'), 0), _num(b.get('y'), 0)) for b in buses}
    br_res = {r['id']: r for _, r in lf['branches'].iterrows()} if lf and lf.get('converged') else {}
    bus_res = {str(r['id']): r for _, r in lf['buses'].iterrows()} if lf and lf.get('converged') else {}
    fig = go.Figure()

    mid_x, mid_y, mid_text, mid_custom = [], [], [], []
    for br in case['branches']:
        a, b = pos.get(str(br['from'])), pos.get(str(br['to']))
        if not a or not b:
            continue
        rr = br_res.get(br['id'])
        loading = float(rr['loading']) if rr is not None else None
        out_of_service = not _num(br.get('status'), 1) or br['id'] in open_branches
        if out_of_service:
            color, width, dash = '#3b4a66', 2, 'dot'
        else:
            color = BAD if loading and loading > 100 else WARN if loading and loading > 80 else '#60769a'
            width = min(8, 2 + loading / 30) if loading is not None else 3
            dash = 'dash' if br.get('equipmentType') == 'transformer' else 'solid'
        fig.add_trace(go.Scatter(x=[a[0], b[0]], y=[a[1], b[1]], mode='lines', hoverinfo='skip',
                                 line=dict(color=color, width=width, dash=dash), showlegend=False))
        mid_x.append((a[0] + b[0]) / 2)
        mid_y.append((a[1] + b[1]) / 2)
        mid_text.append(str(br['id']))
        tip = f'<b>{br["id"]}</b> ({br.get("equipmentType", "line")})<br>{br["from"]} → {br["to"]}<br>' \
              f'r+jx = {br["r"]}+j{br["x"]} pu<br>Rate {br.get("rate")} MVA'
        if rr is not None:
            tip += f'<br>P {rr["Pf"]:.1f} MW, Q {rr["Qf"]:.1f} MVAr<br>Loading {loading:.1f}%'
        if out_of_service:
            tip += '<br><i>open</i>'
        mid_custom.append(['branch', str(br['id']), tip])
    # invisible markers at branch midpoints make branches clickable
    fig.add_trace(go.Scatter(x=mid_x, y=mid_y, mode='markers+text', text=mid_text, textposition='top center',
                             textfont=dict(color=MUTED, size=10), marker=dict(size=14, color='rgba(0,0,0,0)'),
                             customdata=mid_custom, hovertemplate='%{customdata[2]}<extra></extra>',
                             showlegend=False, name='branches'))

    bx, by, fill, outline, owidth, labels, custom, inner = [], [], [], [], [], [], [], []
    for b in buses:
        bid = str(b['id'])
        x, y = pos[bid]
        r = bus_res.get(bid)
        tip = f'<b>{b.get("name", bid)}</b> [{b["type"]}]<br>{b["baseKV"]} kV<br>Load {b["Pd"]} MW / {b["Qd"]} MVAr'
        if overlay is not None:
            v = overlay['voltages'].get(bid)
            fill.append(_volt_color(v))
            labels.append(f'{v:.3f} pu' if v is not None and np.isfinite(v) else '—')
            outline.append(TEXT)
            owidth.append(1.5)
            tip += f'<br>{overlay["label"]}: <b>{labels[-1]}</b>'
        else:
            vout = r is not None and (r['Vm'] < 0.95 or r['Vm'] > 1.05)
            fill.append(BAD if vout else TYPE_COLOR.get(b['type'], SLATE))
            outline.append(WARN if vout else '#eaf2ff')
            owidth.append(3 if vout else 2)
            labels.append(f'{r["Vm"]:.3f} pu' if r is not None else f'{b["baseKV"]} kV')
            if r is not None:
                tip += f'<br>V = {r["Vm"]:.4f} pu ∠ {r["Va"]:.2f}°<br>Net P/Q {r["Pinj"]:.1f} MW / {r["Qinj"]:.1f} MVAr'
        bx.append(x)
        by.append(y)
        inner.append(bid)
        custom.append(['bus', bid, tip])
    fig.add_trace(go.Scatter(x=bx, y=by, mode='markers+text', text=inner, textposition='middle center',
                             textfont=dict(color='#08101d', size=10, family='Inter, system-ui'),
                             marker=dict(size=30, color=fill, line=dict(color=outline, width=owidth)),
                             customdata=custom, hovertemplate='%{customdata[2]}<extra></extra>',
                             showlegend=False, name='buses'))
    fig.add_trace(go.Scatter(x=bx, y=[y + 30 for y in by], mode='text', text=labels, hoverinfo='skip',
                             textfont=dict(color=TEXT, size=11), showlegend=False))
    plotly_layout(fig, height=height, legend=False)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False, autorange='reversed', scaleanchor='x', scaleratio=1)
    fig.update_layout(plot_bgcolor=PANEL, dragmode='pan', clickmode='event+select',
                      margin=dict(l=0, r=0, t=0, b=0))
    return fig
