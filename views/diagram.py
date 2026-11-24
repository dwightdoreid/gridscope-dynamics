"""Single-line diagram, drawn from the bus x/y coordinates stored in the case, like GridScope.

Two styles:
  * 'Schematic' — GridScope's own look: buses as round nodes, transformers as dashed lines.
  * 'Standard'  — conventional single-line diagram: buses as thick bus bars, transformers as two
    overlapping circles, generators as a circle with a sine wave, loads as arrows, shunts as
    capacitor / reactor symbols with an earth. All symbols are drawn in data coordinates, so they
    scale together when zooming.

The layout is computed once into a Scene (lines, bus bars / nodes, labels and click targets, each
tagged with the element it belongs to). to_plotly() turns a scene into the read-only plotly figure;
the interactive editor (views/editor.py) draws the same scene as SVG in the browser.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np
import plotly.graph_objects as go

from gridscope.case import _num, is_transformer
from views.common import BAD, MUTED, PANEL, SLATE, TEXT, WARN, ACCENT, plotly_layout

TYPE_COLOR = {'Slack': BAD, 'PV': ACCENT, 'PQ': SLATE}
STYLES = ('Schematic', 'Standard')
OPEN_COLOR = '#3b4a66'
DEVICE_COLOR = '#b6c4dc'
NODE_PX = 30          # schematic bus node diameter (screen pixels)


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


# ----------------------------------------------------------------- shared styling

def _results(lf):
    ok = lf and lf.get('converged')
    br_res = {r['id']: r for _, r in lf['branches'].iterrows()} if ok else {}
    bus_res = {str(r['id']): r for _, r in lf['buses'].iterrows()} if ok else {}
    return br_res, bus_res


def _bus_style(b, r, overlay):
    """(fill, outline, outline width, label under the bus, hover text)."""
    bid = str(b['id'])
    tip = f'<b>{b.get("name", bid)}</b> [{b["type"]}]<br>{b["baseKV"]} kV<br>Load {b["Pd"]} MW / {b["Qd"]} MVAr'
    if overlay is not None:
        v = overlay['voltages'].get(bid)
        label = f'{v:.3f} pu' if v is not None and np.isfinite(v) else '—'
        tip += f'<br>{overlay["label"]}: <b>{label}</b>'
        return _volt_color(v), TEXT, 1.5, label, tip
    vout = r is not None and (r['Vm'] < 0.95 or r['Vm'] > 1.05)
    label = f'{r["Vm"]:.3f} pu' if r is not None else f'{b["baseKV"]} kV'
    if r is not None:
        tip += f'<br>V = {r["Vm"]:.4f} pu ∠ {r["Va"]:.2f}°<br>Net P/Q {r["Pinj"]:.1f} MW / {r["Qinj"]:.1f} MVAr'
    return (BAD if vout else TYPE_COLOR.get(b['type'], SLATE)), (WARN if vout else '#eaf2ff'), \
        (3 if vout else 2), label, tip


def _branch_style(br, rr, open_branches, trafo):
    """(colour, width, out of service, hover text) — colour/width follow the loading."""
    loading = float(rr['loading']) if rr is not None else None
    out = not _num(br.get('status'), 1) or br['id'] in open_branches
    if out:
        color, width = OPEN_COLOR, 2
    else:
        color = BAD if loading and loading > 100 else WARN if loading and loading > 80 else '#60769a'
        width = min(8, 2 + loading / 30) if loading is not None else 3
    tip = f'<b>{br["id"]}</b> ({"transformer" if trafo else "line"})<br>{br["from"]} → {br["to"]}<br>' \
          f'r+jx = {br["r"]}+j{br["x"]} pu<br>Rate {br.get("rate")} MVA'
    if trafo:
        tip += f'<br>Tap {br.get("tap", 1)} / shift {br.get("shift", 0)}°'
    if rr is not None:
        tip += f'<br>P {rr["Pf"]:.1f} MW, Q {rr["Qf"]:.1f} MVAr<br>Loading {loading:.1f}%'
    if out:
        tip += '<br><i>open</i>'
    return color, width, out, tip


# ----------------------------------------------------------------- scene

class Scene:
    """Renderer-independent drawing. Every primitive carries:
         sel  — (kind, id) selected when it is clicked: kind is bus / branch / gen;
         bus  — the bus it moves with when that bus is dragged (None for branch geometry);
         ends — (from bus, to bus) for branch geometry, so a drag can rubber-band it."""

    def __init__(self, style, u, grid):
        self.style, self.u, self.grid = style, u, grid
        self.paths, self.bars, self.nodes, self.labels, self.hits = [], [], [], [], []
        self.bus_pos = {}

    def path(self, xs, ys, color, width, dash='solid', fill=False, sel=None, bus=None, ends=None):
        self.paths.append(dict(x=[float(v) for v in xs], y=[float(v) for v in ys], color=color,
                               width=float(width), dash=dash, fill=fill, sel=sel, bus=bus, ends=ends))

    def label(self, x, y, text, pos, color, size, bus=None, ends=None, dense_hide=False):
        self.labels.append(dict(x=float(x), y=float(y), text=str(text), pos=pos, color=color, size=size,
                                bus=bus, ends=ends, dense_hide=dense_hide))

    def hit(self, x, y, sel, tip, group, bus=None, ends=None):
        self.hits.append(dict(x=float(x), y=float(y), sel=sel, tip=tip, group=group, bus=bus, ends=ends))

    def to_json(self):
        return dict(style=self.style, u=self.u, grid=self.grid, paths=self.paths, bars=self.bars,
                    nodes=self.nodes, labels=self.labels, hits=self.hits,
                    bus_pos={k: [float(v[0]), float(v[1])] for k, v in self.bus_pos.items()})


def build_scene(case, lf=None, overlay=None, open_branches=(), style='Schematic', dense=None):
    """dense: hide kV / branch-id labels (default: more than 40 buses)."""
    if dense is None:
        dense = len(case['buses']) > 40
    if style == 'Standard':
        return _scene_standard(case, lf, overlay, open_branches, dense)
    return _scene_schematic(case, lf, overlay, open_branches)


def build_figure(case, lf=None, overlay=None, open_branches=(), height=610, style='Schematic'):
    """lf: AC load-flow result dict (or None). overlay: {'voltages': {bus: pu}, 'label': str} from a
    dynamic simulation snapshot; open_branches: branch ids drawn as open (tripped).
    style: 'Schematic' (round bus nodes) or 'Standard' (bus bars and IEC/IEEE symbols)."""
    return to_plotly(build_scene(case, lf, overlay, open_branches, style), height)


def _nan_breaks(seqs):
    out = []
    for s in seqs:
        out.extend(s)
        out.append(np.nan)
    return np.array(out, float)


def to_plotly(scene: Scene, height=610):
    fig = go.Figure()
    groups = defaultdict(list)
    for p in scene.paths:
        groups[(p['color'], p['width'], p['dash'], p['fill'])].append(p)
    for (color, width, dash, fill), ps in groups.items():
        fig.add_trace(go.Scatter(x=_nan_breaks(p['x'] for p in ps), y=_nan_breaks(p['y'] for p in ps),
                                 mode='lines', hoverinfo='skip', showlegend=False,
                                 line=dict(color=color, width=width, dash=dash),
                                 fill='toself' if fill else None, fillcolor=color if fill else None))
    bar_groups = defaultdict(list)
    for b in scene.bars:
        bar_groups[(b['color'], b['width'])].append(b)
    for (color, width), bs in bar_groups.items():
        fig.add_trace(go.Scatter(x=_nan_breaks([b['x0'], b['x1']] for b in bs),
                                 y=_nan_breaks([b['y'], b['y']] for b in bs), mode='lines', hoverinfo='skip',
                                 showlegend=False, line=dict(color=color, width=width)))
    if scene.nodes:
        n = scene.nodes
        fig.add_trace(go.Scatter(x=[v['x'] for v in n], y=[v['y'] for v in n], mode='markers+text',
                                 text=[v['text'] for v in n], textposition='middle center',
                                 textfont=dict(color='#08101d', size=10, family='Inter, system-ui'),
                                 marker=dict(size=NODE_PX, color=[v['color'] for v in n],
                                             line=dict(color=[v['outline'] for v in n],
                                                       width=[v['owidth'] for v in n])),
                                 customdata=[['bus', v['bus'], v['tip']] for v in n],
                                 hovertemplate='%{customdata[2]}<extra></extra>', showlegend=False, name='buses'))
    lab_groups = defaultdict(list)
    for lb in scene.labels:
        if not lb['dense_hide']:
            lab_groups[(lb['color'], lb['size'])].append(lb)
    for (color, size), ls in lab_groups.items():
        below = [v['pos'] == 'below node' for v in ls]     # plotly: 30 data units under the node, as before
        fig.add_trace(go.Scatter(x=[v['x'] for v in ls], y=[v['y'] + 30 * b for v, b in zip(ls, below)],
                                 mode='text', text=[_pad(v) for v in ls],
                                 textposition=['middle center' if b else v['pos'] for v, b in zip(ls, below)],
                                 hoverinfo='skip', showlegend=False,
                                 textfont=dict(color=color, size=size, family='Inter, system-ui')))
    hit_groups = defaultdict(list)
    for h in scene.hits:
        hit_groups[h['group']].append(h)
    for group in ('branches', 'devices', 'buses'):      # buses last: on top for clicks
        hs = hit_groups.get(group)
        if not hs:
            continue
        fig.add_trace(go.Scatter(x=[h['x'] for h in hs], y=[h['y'] for h in hs], mode='markers',
                                 customdata=[[h['sel'][0], h['sel'][1], h['tip']] for h in hs],
                                 hovertemplate='%{customdata[2]}<extra></extra>', showlegend=False, name=group,
                                 marker=dict(size=12 if group == 'buses' else 14, color='rgba(0,0,0,0)')))
    plotly_layout(fig, height=height, legend=False)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False, autorange='reversed', scaleanchor='x', scaleratio=1)
    fig.update_layout(plot_bgcolor=PANEL, dragmode='pan', clickmode='event+select',
                      margin=dict(l=0, r=0, t=0, b=0))
    return fig


def _pad(lb):
    return f'{lb["text"]} ' if lb['pos'] == 'middle left' else f' {lb["text"]}' if lb['pos'] == 'middle right' \
        else lb['text']


def _grid_step(u):
    """A round snapping step about half a symbol unit (1, 2, 5, 10, ...)."""
    target = max(u / 2, 0.5)
    for s in (1, 2, 5, 10, 20, 50):
        if s >= target * 0.75:
            return s
    return 50


# ----------------------------------------------------------------- schematic style

def _scene_schematic(case, lf, overlay, open_branches):
    buses = case['buses']
    pos = {str(b['id']): (_num(b.get('x'), 0), _num(b.get('y'), 0)) for b in buses}
    br_res, bus_res = _results(lf)
    u, _ = _scale(pos)
    sc = Scene('Schematic', u, _grid_step(u))
    sc.bus_pos = pos
    for br in case['branches']:
        fa, tb = str(br['from']), str(br['to'])
        a, b = pos.get(fa), pos.get(tb)
        if not a or not b:
            continue
        trafo = br.get('equipmentType') == 'transformer'
        color, width, out, tip = _branch_style(br, br_res.get(br['id']), open_branches, trafo)
        dash = 'dot' if out else 'dash' if trafo else 'solid'
        sel, ends = ('branch', str(br['id'])), (fa, tb)
        sc.path([a[0], b[0]], [a[1], b[1]], color, width, dash, sel=sel, ends=ends)
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        sc.label(mx, my, br['id'], 'top center', MUTED, 10, ends=ends)
        sc.hit(mx, my, sel, tip, 'branches', ends=ends)
    for b in buses:
        bid = str(b['id'])
        f, o, ow, label, tip = _bus_style(b, bus_res.get(bid), overlay)
        x, y = pos[bid]
        sc.nodes.append(dict(bus=bid, x=float(x), y=float(y), color=f, outline=o, owidth=ow, text=bid, tip=tip))
        sc.label(x, y, label, 'below node', TEXT, 11, bus=bid)
    return sc


# ----------------------------------------------------------------- standard style

def _circle(cx, cy, r, n=36):
    t = np.linspace(0, 2 * np.pi, n)
    return cx + r * np.cos(t), cy + r * np.sin(t)


def _scale(pos):
    """Symbol unit (data units) from the typical spacing between buses, so a 9-bus and a 118-bus
    diagram both look right."""
    P = np.array(list(pos.values()), dtype=float)
    if len(P) < 2:
        return 20.0, {k: np.inf for k in pos}
    D = np.hypot(P[:, None, 0] - P[None, :, 0], P[:, None, 1] - P[None, :, 1])
    np.fill_diagonal(D, np.inf)
    nn = D.min(axis=1)
    finite = nn[np.isfinite(nn) & (nn > 0)]
    u = float(np.clip(0.18 * (np.median(finite) if len(finite) else 100), 2.5, 26))
    # horizontal room: nearest bus roughly on the same row, which a bus bar must not run into
    dx = np.abs(P[:, None, 0] - P[None, :, 0])
    same_row = np.abs(P[:, None, 1] - P[None, :, 1]) < 2 * u
    np.fill_diagonal(same_row, False)
    room = np.where(same_row, dx, np.inf).min(axis=1)
    return u, dict(zip(pos, room))


class _Obstacles:
    """Segments already on the drawing; symbols are placed where they keep the most clearance."""

    def __init__(self):
        self.a, self.b, self.owner = [], [], []

    def add(self, a, b, owner=None):
        self.a.append(a)
        self.b.append(b)
        self.owner.append(owner)

    def freeze(self):
        self.A, self.B = np.array(self.a, float).reshape(-1, 2), np.array(self.b, float).reshape(-1, 2)
        self.lo, self.hi = np.minimum(self.A, self.B), np.maximum(self.A, self.B)

    def clearance(self, pts, skip=(), cap=np.inf):
        """Smallest distance from any of pts (k x 2) to any segment not owned by something in skip.
        Segments further than cap from the points' bounding box are ignored (result is then >= cap)."""
        if not len(self.a):
            return cap
        if not hasattr(self, 'A') or len(self.A) != len(self.a):
            self.freeze()
        P = np.asarray(pts, float)
        near = np.all((self.hi >= P.min(0) - cap) & (self.lo <= P.max(0) + cap), axis=1)
        idx = np.nonzero(near)[0]
        if skip:
            skip = set(skip)
            idx = [k for k in idx if self.owner[k] not in skip]
        if not len(idx):
            return cap
        A, B = self.A[idx], self.B[idx]
        P = P[:, None, :]
        AB = B - A
        L2 = np.maximum((AB ** 2).sum(1), 1e-12)
        s = np.clip(((P - A) * AB).sum(2) / L2, 0, 1)
        return float(np.sqrt((((A + s[..., None] * AB) - P) ** 2).sum(2)).min())


def _symbol_points(px, y, side, u, reach, r):
    """Points that stand for a device symbol hanging off a bus bar (stub, centre, outline)."""
    cy = y + side * reach
    return [(px, y + side * 0.5 * u), (px, cy), (px - r, cy), (px + r, cy), (px, cy + side * r)]


def _scene_standard(case, lf, overlay, open_branches, dense):
    buses = case['buses']
    pos = {str(b['id']): (_num(b.get('x'), 0), _num(b.get('y'), 0)) for b in buses}
    br_res, bus_res = _results(lf)
    u, room = _scale(pos)
    stub = 0.9 * u
    sc = Scene('Standard', u, _grid_step(u))
    sc.bus_pos = pos
    obs = _Obstacles()

    # --- connections per bus: (branch index, other bus, side) — side -1 = branch leaves upwards
    branches = [br for br in case['branches'] if str(br['from']) in pos and str(br['to']) in pos
                and str(br['from']) != str(br['to'])]
    conns = defaultdict(list)
    for i, br in enumerate(branches):
        a, b = str(br['from']), str(br['to'])
        for here, other in ((a, b), (b, a)):
            dy = pos[other][1] - pos[here][1]
            side = 1 if abs(dy) < u else int(np.sign(dy))   # same row: both ends drop down
            conns[here].append((i, other, side))
    gens = defaultdict(list)
    for g in case.get('generators', []):
        if str(g.get('bus')) in pos:
            gens[str(g['bus'])].append(g)

    # --- bus bars, and where each branch lands on its bar (ordered by where the far end lies)
    bar, tap, n_side = {}, {}, {}
    for b in buses:
        bid = str(b['id'])
        x, y = pos[bid]
        cs = sorted(conns[bid], key=lambda c: (pos[c[1]][0] - x, c[2]))
        n_dev = len(gens[bid]) + bool(_num(b.get('Pd'), 0) or _num(b.get('Qd'), 0)) \
            + bool(_num(b.get('Bs'), 0) or _num(b.get('Gs'), 0))
        length = max(2.6 * u, (len(cs) - 1) * 0.8 * u + 1.6 * u, n_dev * 1.6 * u)
        length = max(1.2 * u, min(length, 0.85 * room[bid]))
        x0, x1 = x - length / 2, x + length / 2
        bar[bid] = (x0, x1, y)
        obs.add((x0, y), (x1, y), ('bar', bid))
        margin = min(0.6 * u, length / 4)
        xs = [x] if len(cs) == 1 else np.linspace(x0 + margin, x1 - margin, len(cs))
        n_side[bid] = {-1: 0, 1: 0}
        for (i, other, side), px in zip(cs, xs):
            tap[(i, bid)] = (float(px), y, side)
            n_side[bid][side] += 1
            # a drop counts as the bus's own on the side it leaves from (devices on the other side ignore it)
            obs.add((px, y), (px, y + side * stub), ('drop', bid, side))

    # --- branch runs between the two drops
    runs = []
    for i, br in enumerate(branches):
        a, b = str(br['from']), str(br['to'])
        ax, ay, aside = tap[(i, a)]
        bx, by, bside = tap[(i, b)]
        sa = np.array([ax, ay + aside * stub])
        sb = np.array([bx, by + bside * stub])
        runs.append((sa, sb))
        obs.add(tuple(sa), tuple(sb), ('run', i))

    # --- transformer symbols: along the run where they are furthest from everything else
    trafo_at = {}
    for i, br in enumerate(branches):
        if not is_transformer(br, case):
            continue
        sa, sb = runs[i]
        d = float(np.hypot(*(sb - sa)))
        if d < 1e-9:
            continue
        e = (sb - sa) / d
        n = np.array([-e[1], e[0]])
        rt = min(0.5 * u, 0.2 * d)
        best, best_c = None, -1.0
        for f in sorted(np.linspace(0.3, 0.7, 9), key=lambda f: abs(f - 0.5)):
            m = sa + f * (sb - sa)
            pts = [m - 1.5 * rt * e, m + 1.5 * rt * e, m + rt * n, m - rt * n, m]
            c = obs.clearance(pts, skip=[('run', i)], cap=2 * u)
            if c > best_c + 1e-9:
                best, best_c = m, c
        trafo_at[i] = (best, e, n, rt)
        for k in (-1, 1):   # the symbol itself becomes an obstacle
            obs.add(tuple(best + k * 1.5 * rt * e), tuple(best + k * 1.5 * rt * e + rt * n), ('trafo', i))
            obs.add(tuple(best + k * 1.5 * rt * e), tuple(best + k * 1.5 * rt * e - rt * n), ('trafo', i))

    # --- generators, loads, shunts: side and position along the bar with the most clearance
    devices = []
    for b in buses:
        bid = str(b['id'])
        x0, x1, y = bar[bid]
        margin = min(0.6 * u, (x1 - x0) / 4)
        cands = sorted(np.linspace(x0 + margin, x1 - margin, 7), key=lambda c: abs(c - pos[bid][0]))
        fewer = -1 if n_side[bid][-1] <= n_side[bid][1] else 1
        items = [('gen', g) for g in gens[bid]]
        if _num(b.get('Pd'), 0) or _num(b.get('Qd'), 0):
            items.append(('load', b))
        if _num(b.get('Bs'), 0) or _num(b.get('Gs'), 0):
            items.append(('shunt', b))
        gen_side = None
        for kind, item in items:
            reach, r = (1.55 * u, 0.55 * u) if kind == 'gen' else (1.35 * u, 0.4 * u)
            # preferred side: generators where fewer branches leave, loads/shunts opposite the generators
            pref = fewer if kind == 'gen' or gen_side is None else -gen_side
            best, best_s = None, -np.inf
            for side in (pref, -pref):
                skip = [('bar', bid), ('drop', bid, -side)]
                for px in cands:
                    c = obs.clearance(_symbol_points(px, y, side, u, reach, r), skip=skip, cap=2 * u)
                    score = min(c, 2 * u) - (0.35 * u if side != pref else 0)
                    if score > best_s + 1e-9:
                        best, best_s = (float(px), side), score
            px, side = best
            if kind == 'gen' and gen_side is None:
                gen_side = side
            devices.append((kind, bid, px, y, side, item))
            pts = _symbol_points(px, y, side, u, reach, r)
            obs.add((px, y), pts[1], ('dev', bid))
            obs.add(pts[2], pts[3], ('dev', bid))
            obs.add(pts[1], pts[4], ('dev', bid))

    # --- branches
    for i, br in enumerate(branches):
        a, b = str(br['from']), str(br['to'])
        trafo = is_transformer(br, case)
        color, width, out, tip = _branch_style(br, br_res.get(br['id']), open_branches, trafo)
        width = min(width, 5)
        dash = 'dot' if out else 'solid'
        sel, ends = ('branch', str(br['id'])), (a, b)
        line = lambda xs, ys, w=width: sc.path(xs, ys, color, w, dash, sel=sel, ends=ends)   # noqa: E731
        (ax, ay, aside), (bx, by, bside) = tap[(i, a)], tap[(i, b)]
        sa, sb = runs[i]
        line([ax, sa[0]], [ay, sa[1]])
        line([bx, sb[0]], [by, sb[1]])
        d = float(np.hypot(*(sb - sa)))
        e = (sb - sa) / d if d > 1e-9 else np.array([1.0, 0.0])
        n = np.array([-e[1], e[0]])
        if n[1] > 0 or (abs(n[1]) < 1e-9 and n[0] < 0):     # label side: up / right
            n = -n
        below = False
        if i in trafo_at:
            mid, e, _, rt = trafo_at[i]
            c1, c2 = mid - 0.5 * rt * e, mid + 0.5 * rt * e
            p1, p2 = c1 - rt * e, c2 + rt * e
            if np.dot(p1 - sa, e) > 0:
                line([sa[0], p1[0]], [sa[1], p1[1]])
            if np.dot(sb - p2, e) > 0:
                line([p2[0], sb[0]], [p2[1], sb[1]])
            for c in (c1, c2):
                line(*_circle(c[0], c[1], rt), w=max(1.5, min(width, 2.5)))
            lab = mid + n * 1.6 * rt
            below = abs(e[0]) >= abs(e[1]) and aside + bside > 0   # run hangs below its buses
            if below:
                lab = mid + np.array([0.0, 1.6 * rt])
        else:
            line([sa[0], sb[0]], [sa[1], sb[1]])
            mid = (sa + sb) / 2
            lab = mid
        pos_txt = 'bottom center' if below else 'middle right' if abs(e[1]) > abs(e[0]) else 'top center'
        sc.label(lab[0], lab[1], br['id'], pos_txt, MUTED, 10, ends=ends, dense_hide=dense)
        sc.hit(mid[0], mid[1], sel, tip, 'branches', ends=ends)

    # --- generators, loads, shunts
    for kind, bid, px, y, side, item in devices:
        if kind == 'gen':
            on = _num(item.get('status'), 1)
            col = DEVICE_COLOR if on else OPEN_COLOR
            sel = ('gen', str(item['id']))
            rg = 0.55 * u
            cy = y + side * (1.0 * u + rg)
            sc.path([px, px], [y, y + side * 1.0 * u], col, 1.8, 'solid' if on else 'dot', sel=sel, bus=bid)
            sc.path(*_circle(px, cy, rg), col, 1.8, sel=sel, bus=bid)
            t = np.linspace(0, 2 * np.pi, 24)
            sc.path(px + np.linspace(-0.6, 0.6, 24) * rg, cy - 0.3 * rg * np.sin(t), col, 1.5, sel=sel, bus=bid)
            sc.hit(px, cy, sel, f'<b>{item["id"]}</b> (generator at bus {bid})<br>'
                                f'P {item.get("Pg")} MW, Q {item.get("Qg")} MVAr<br>'
                                f'Vset {item.get("Vg")} pu' + ('' if on else '<br><i>out of service</i>'),
                   'devices', bus=bid)
        elif kind == 'load':
            sel = ('bus', bid)
            y1, y2 = y + side * 1.1 * u, y + side * 1.6 * u
            w = 0.35 * u
            sc.path([px, px], [y, y1], DEVICE_COLOR, 1.8, sel=sel, bus=bid)
            sc.path([px - w, px + w, px, px - w], [y1, y1, y2, y1], DEVICE_COLOR, 1, fill=True, sel=sel, bus=bid)
            sc.hit(px, (y1 + y2) / 2, sel, f'<b>Load at bus {bid}</b><br>{item["Pd"]} MW / {item["Qd"]} MVAr',
                   'devices', bus=bid)
        else:  # shunt: capacitor (B > 0) or reactor (B < 0), then earth
            sel = ('bus', bid)
            bs = _num(item.get('Bs'), 0)
            seg = lambda xs, ys: sc.path(xs, ys, DEVICE_COLOR, 1.8, sel=sel, bus=bid)   # noqa: E731
            w = 0.4 * u
            y_a, y_b = y + side * 0.7 * u, y + side * 1.1 * u
            seg([px, px], [y, y_a])
            if bs >= 0:
                g = 0.12 * u
                seg([px - w, px + w], [y_a, y_a])
                seg([px - w, px + w], [y_a + side * g, y_a + side * g])
                seg([px, px], [y_a + side * g, y_b])
            else:   # three half-turns of a coil
                r = (y_b - y_a) / 6
                for j in range(3):
                    tt = np.linspace(-np.pi / 2, np.pi / 2, 12)
                    cy = y_a + (2 * j + 1) * r
                    seg(px + abs(r) * np.cos(tt), cy + r * np.sin(tt))
            for j, ww in enumerate((0.4, 0.26, 0.12)):
                yy = y_b + side * j * 0.12 * u
                seg([px - ww * u, px + ww * u], [yy, yy])
            sc.hit(px, y_a, sel, f'<b>Shunt at bus {bid}</b><br>{item.get("Gs", 0)} MW / {bs} MVAr '
                                 f'({"capacitive" if bs >= 0 else "inductive"})', 'devices', bus=bid)

    # --- bus bars (thick lines, colour = bus type / voltage), labels, click targets along the bar
    bar_w = 7 if not dense else 5
    for b in buses:
        bid = str(b['id'])
        x0, x1, y = bar[bid]
        fill, _, _, label, tip = _bus_style(b, bus_res.get(bid), overlay)
        sc.bars.append(dict(bus=bid, x0=float(x0), x1=float(x1), y=float(y), color=fill, width=bar_w, tip=tip))
        for hx in np.linspace(x0, x1, max(3, int((x1 - x0) / (1.2 * u)) + 1)):
            sc.hit(hx, y, ('bus', bid), tip, 'buses', bus=bid)
        sc.label(x0, y, bid, 'middle left', TEXT, 11 if not dense else 9, bus=bid)
        sc.label(x1, y, label, 'middle right', MUTED, 10, bus=bid, dense_hide=dense)
    return sc
