// GridScope single-line diagram editor (Streamlit components v2).
//
// Python sends a Scene (views/diagram.py) plus the current selection; this draws it as SVG and
// reports user actions back with setTriggerValue('action', {...}). Python applies the edit to the
// case, re-lays out the diagram and sends the new scene. Pan / zoom / tool / snap stay in the browser.

const NS = 'http://www.w3.org/2000/svg';
const STORE = (window.__gsSldEditor = window.__gsSldEditor || {
  view: null, token: null, tool: 'select', snap: true, pending: null, cleanup: null, flow: 'P',
});
const FLOW_COLOR = { P: '#fbbf24', Q: '#22d3ee' };
const FLOW_UNIT = { P: 'MW', Q: 'MVAr' };
const REDUCED_MOTION = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

const TOOLS = [
  ['select', '⬚', 'Select / move (drag a bus to move it, drag the background to pan)'],
  ['bus', '▬', 'Add bus: click an empty spot'],
  ['line', '━', 'Add line: click two buses'],
  ['transformer', '⚭', 'Add transformer: click two buses'],
  ['gen', 'Ⓖ', 'Add generator: click a bus'],
  ['load', '▼', 'Add load: click a bus'],
  ['shunt', '⊣⊢', 'Add shunt capacitor: click a bus'],
];
const HINT = {
  select: 'Drag a bus to move it · drag the background to pan · wheel to zoom · Del deletes the selection',
  bus: 'Click an empty spot to place a new bus · Esc to stop',
  line: 'Click the first bus, then the second · Esc to stop',
  transformer: 'Click the first bus, then the second · Esc to stop',
  gen: 'Click a bus to add a generator · Esc to stop',
  load: 'Click a bus to add a load · Esc to stop',
  shunt: 'Click a bus to add a shunt · Esc to stop',
};

function el(tag, attrs = {}, parent = null) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}

// tooltips come from case data (bus names, ids): escape everything, then allow <b>, <i>, <br>
function safeTip(s) {
  const esc = String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  return esc.replace(/&lt;(\/?)(b|i)&gt;/g, '<$1$2>').replace(/&lt;br\s*\/?&gt;/g, '<br>');
}

const DASH = { solid: null, dot: '2 4', dash: '8 5' };
const ANCHOR = {
  'middle left': ['end', 'central', -4, 0],
  'middle right': ['start', 'central', 4, 0],
  'top center': ['middle', 'auto', 0, -5],
  'bottom center': ['middle', 'hanging', 0, 5],
  'middle center': ['middle', 'central', 0, 0],
  'below node': ['middle', 'hanging', 0, 19],
};

export default function (component) {
  const { data, setTriggerValue, parentElement } = component;
  if (STORE.cleanup) { try { STORE.cleanup(); } catch (e) { /* previous instance already gone */ } }
  const sc = data.scene;
  const H = data.height || 610;
  const busPos = sc.bus_pos;
  const editable = !!data.editable;
  if (!editable) STORE.tool = 'select';
  const send = (type, payload = {}) => setTriggerValue('action', { type, ...payload, nonce: Math.random() });

  // ------------------------------------------------------------ DOM skeleton
  // keep the same root between updates so keyboard focus (Esc / Del / Ctrl+Z) survives a rerun
  let root = parentElement.querySelector('.gs-sld');
  if (root) {
    root.replaceChildren();
  } else {
    root = document.createElement('div');
    root.className = 'gs-sld';
    root.tabIndex = 0;
    root.addEventListener('focus', () => { STORE.focused = true; });
    root.addEventListener('blur', () => { STORE.focused = false; });
    parentElement.appendChild(root);
  }
  if (!data.maximized || !root.style.height) root.style.height = H + 'px';   // maximized: size() below

  const bar = document.createElement('div');
  bar.className = 'gs-tools';
  root.appendChild(bar);
  const toolBtns = {};
  for (const [name, icon, title] of (editable ? TOOLS : [])) {
    const b = document.createElement('button');
    b.textContent = icon;
    b.title = title;
    b.dataset.tool = name;
    b.onclick = () => setTool(name);
    bar.appendChild(b);
    toolBtns[name] = b;
  }
  const sep = () => { const s = document.createElement('span'); s.className = 'sep'; bar.appendChild(s); };
  const btn = (label, title, fn, cls = '') => {
    const b = document.createElement('button');
    b.textContent = label; b.title = title; b.className = cls; b.onclick = fn; bar.appendChild(b); return b;
  };
  if (editable) {
    sep();
    const delBtn = btn('🗑', 'Delete the selected element (Del)', () => deleteSelected(), 'danger');
    delBtn.disabled = !data.selected;
    const undoBtn = btn('↶', 'Undo the last diagram edit', () => send('undo'));
    undoBtn.disabled = !data.can_undo;
  }
  btn('⤢', 'Fit the diagram to the view', () => { fit(); place(); });
  const maxed = !!data.maximized;
  const maxBtn = btn('⛶', maxed ? 'Restore the normal layout (Esc)' : 'Fill the browser window with the diagram',
                     () => send('maximize', { on: !maxed }), maxed ? 'on' : '');
  btn('⭳', 'Download the diagram as a PNG image', () => exportPng(), 'png');
  if (editable) {
    const snapLbl = document.createElement('label');
    snapLbl.className = 'snap';
    snapLbl.title = `Snap bus positions to a ${sc.grid}-unit grid`;
    const snapBox = document.createElement('input');
    snapBox.type = 'checkbox';
    snapBox.checked = STORE.snap;
    snapBox.onchange = () => { STORE.snap = snapBox.checked; };
    snapLbl.appendChild(snapBox);
    snapLbl.appendChild(document.createTextNode(' snap'));
    bar.appendChild(snapLbl);
  }
  // power-flow arrows: switched here in the browser (no rerun); needs a current AC load flow
  sep();
  const flowSeg = document.createElement('span');
  flowSeg.className = 'seg';
  const hasFlows = sc.flows && sc.flows.length > 0;
  flowSeg.title = hasFlows ? 'Power-flow arrows: off, real power (P) or reactive power (Q)'
    : 'Run AC Load Flow to show power-flow arrows';
  const flowBtns = {};
  for (const [mode, label] of [['off', 'Off'], ['P', 'P'], ['Q', 'Q']]) {
    const b = document.createElement('button');
    b.textContent = label;
    b.dataset.flow = mode;
    b.disabled = !hasFlows;
    b.onclick = () => { STORE.flow = mode; drawFlows(); };
    flowSeg.appendChild(b);
    flowBtns[mode] = b;
  }
  const flowLbl = document.createElement('span');
  flowLbl.className = 'seglbl';
  flowLbl.textContent = 'flow';
  bar.appendChild(flowLbl);
  bar.appendChild(flowSeg);

  const hint = document.createElement('div');
  hint.className = 'gs-hint';
  root.appendChild(hint);
  const tip = document.createElement('div');
  tip.className = 'gs-tip';
  root.appendChild(tip);

  const svg = el('svg', { class: 'gs-svg', width: '100%', height: '100%' }, root);
  const bg = el('rect', { class: 'bg', x: 0, y: 0, width: '100%', height: '100%' }, svg);
  // paint order = hit order: branch / device targets, then the world (bus bars win where they overlap a
  // target, which matters on dense diagrams), then labels and schematic bus nodes on top
  const screenHits = el('g', { class: 'screen' }, svg);
  const world = el('g', { class: 'world' }, svg);
  const gBranch = el('g', {}, world);
  const gFlow = el('g', { class: 'flows' }, world);
  const gBus = el('g', {}, world);
  const gTemp = el('g', { class: 'temp' }, world);
  const screen = el('g', { class: 'screen' }, svg);
  const screenFlow = el('g', { class: 'screen' }, svg);

  // ------------------------------------------------------------ draw the scene
  const selKey = data.selected ? `${data.selected[0]}:${data.selected[1]}` : null;
  const busGroups = {};
  const groupFor = (p) => {
    if (p.bus != null) {
      if (!busGroups[p.bus]) busGroups[p.bus] = el('g', { 'data-bus': p.bus }, gBus);
      return busGroups[p.bus];
    }
    return gBranch;
  };
  // every symbol shows its element's tooltip (lines and symbols are clickable themselves, not only targets)
  const tipBySel = {};
  for (const h of sc.hits) tipBySel[`${h.sel[0]}:${h.sel[1]}`] = tipBySel[`${h.sel[0]}:${h.sel[1]}`] || h.tip;
  for (const b of sc.bars) tipBySel[`bus:${b.bus}`] = b.tip;
  for (const n of sc.nodes) tipBySel[`bus:${n.bus}`] = n.tip;
  const tag = (e, p) => {
    if (p.sel && !e.hasAttribute('data-tip') && tipBySel[`${p.sel[0]}:${p.sel[1]}`]) {
      e.setAttribute('data-tip', tipBySel[`${p.sel[0]}:${p.sel[1]}`]);
    }
    if (p.sel) e.setAttribute('data-sel', `${p.sel[0]}:${p.sel[1]}`);
    if (p.ends) e.setAttribute('data-ends', `${p.ends[0]}|${p.ends[1]}`);
    if (p.sel && `${p.sel[0]}:${p.sel[1]}` === selKey) e.classList.add('sel');
  };

  for (const p of sc.paths) {
    const pts = p.x.map((x, i) => `${x},${p.y[i]}`).join(' ');
    const e = el(p.fill ? 'polygon' : 'polyline', {
      points: pts, stroke: p.color, 'stroke-width': p.width, fill: p.fill ? p.color : 'none',
      'stroke-dasharray': DASH[p.dash], 'vector-effect': 'non-scaling-stroke',
      'stroke-linejoin': 'round',
    }, groupFor(p));
    tag(e, p);
  }
  for (const b of sc.bars) {
    const g = groupFor({ bus: b.bus });
    const line = el('line', {
      x1: b.x0, y1: b.y, x2: b.x1, y2: b.y, stroke: b.color, 'stroke-width': b.width,
      'vector-effect': 'non-scaling-stroke', class: 'busbar',
    }, g);
    tag(line, { sel: ['bus', b.bus] });
    const hit = el('line', {
      x1: b.x0, y1: b.y, x2: b.x1, y2: b.y, stroke: 'transparent', 'stroke-width': 16,
      'vector-effect': 'non-scaling-stroke', class: 'hit bus', 'data-tip': b.tip,
    }, g);
    tag(hit, { sel: ['bus', b.bus] });
  }

  // screen layer: things that keep their pixel size (labels, schematic nodes, click targets)
  const screenItems = [];
  for (const lb of sc.labels) {
    if (lb.dense_hide) continue;
    const [anchor, base, dx, dy] = ANCHOR[lb.pos] || ANCHOR['middle center'];
    const t = el('text', {
      'text-anchor': anchor, 'dominant-baseline': base, fill: lb.color, 'font-size': lb.size,
      class: 'lbl' + (lb.kind === 'branch_id' ? ' brid' : ''), 'data-bus': lb.bus,
      'data-ends': lb.ends ? lb.ends.join('|') : null,
    }, screen);
    t.textContent = lb.text;
    screenItems.push([t, lb.x, lb.y, dx, dy]);
  }
  for (const n of sc.nodes) {
    const g = el('g', { 'data-bus': n.bus, class: 'node' }, screen);
    const c = el('circle', {
      r: 15, fill: n.color, stroke: n.outline, 'stroke-width': n.owidth, class: 'hit bus', 'data-tip': n.tip,
    }, g);
    tag(c, { sel: ['bus', n.bus] });
    const t = el('text', { 'text-anchor': 'middle', 'dominant-baseline': 'central', class: 'nodetxt' }, g);
    t.textContent = n.text;
    screenItems.push([g, n.x, n.y, 0, 0]);
  }
  for (const h of sc.hits) {
    if (h.group === 'buses' && sc.bars.length) continue;   // bus bars are their own click targets
    const c = el('circle', {
      r: 8, fill: 'transparent', class: 'hit ' + (h.group === 'branches' ? 'branch' : 'dev'),
      'data-tip': h.tip, 'data-bus': h.bus, 'data-ends': h.ends ? h.ends.join('|') : null,
    }, screenHits);
    tag(c, { sel: h.sel });
    screenItems.push([c, h.x, h.y, 0, 0]);
  }

  // ------------------------------------------------------------ view transform
  const W = () => root.clientWidth || 800;
  const Hh = () => root.clientHeight || H;
  function fit() {
    const H = Hh();
    let xs = [], ys = [];
    for (const [x, y] of Object.values(busPos)) { xs.push(x); ys.push(y); }
    for (const p of sc.paths) { xs.push(...p.x); ys.push(...p.y); }
    if (!xs.length) { STORE.view = { k: 1, tx: W() / 2 - 400, ty: H / 2 - 300 }; return; }
    const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
    const pad = 60;
    const k = Math.min((W() - 2 * pad) / Math.max(x1 - x0, 1), (H - 2 * pad) / Math.max(y1 - y0, 1), 4);
    STORE.view = { k, tx: W() / 2 - k * (x0 + x1) / 2, ty: H / 2 - k * (y0 + y1) / 2 };
  }
  if (!STORE.view || STORE.token !== data.token) { fit(); STORE.token = data.token; }

  let raf = 0;
  function place() {
    raf = 0;
    const { k, tx, ty } = STORE.view;
    world.setAttribute('transform', `matrix(${k},0,0,${k},${tx},${ty})`);
    for (const [e, x, y, dx, dy] of screenItems.concat(flowItems)) {
      e.setAttribute('transform', `translate(${x * k + tx + dx},${y * k + ty + dy})`);
    }
  }
  const schedule = () => { if (!raf) raf = requestAnimationFrame(place); };

  // ------------------------------------------------------------ power-flow arrows
  let flowItems = [];
  let staticFlows = false;             // PNG export: arrows drawn still at their spread-out positions
  const polyLen = (pts) => pts.slice(1).reduce((s, p, i) => s + Math.hypot(p[0] - pts[i][0], p[1] - pts[i][1]), 0);
  function splitHalf(pts) {
    // [first half from start to the middle, second half from the middle to the end] (by length)
    const half = polyLen(pts) / 2;
    let acc = 0;
    for (let i = 1; i < pts.length; i++) {
      const [ax, ay] = pts[i - 1], [bx, by] = pts[i];
      const seg = Math.hypot(bx - ax, by - ay);
      if (acc + seg >= half && seg > 0) {
        const f = (half - acc) / seg;
        const m = [ax + f * (bx - ax), ay + f * (by - ay)];
        return [pts.slice(0, i).concat([m]), [m].concat(pts.slice(i))];
      }
      acc += seg;
    }
    return [pts, [pts[pts.length - 1]]];
  }
  function pointAt(pts, frac) {
    const total = polyLen(pts);
    let goal = frac * total, acc = 0;
    for (let i = 1; i < pts.length; i++) {
      const [ax, ay] = pts[i - 1], [bx, by] = pts[i];
      const seg = Math.hypot(bx - ax, by - ay);
      if (acc + seg >= goal && seg > 0) {
        const f = (goal - acc) / seg;
        return [ax + f * (bx - ax), ay + f * (by - ay), Math.atan2(by - ay, bx - ax) * 180 / Math.PI];
      }
      acc += seg;
    }
    const [x, y] = pts[pts.length - 1];
    return [x, y, 0];
  }
  const fmtFlow = (v) => Math.abs(v) >= 100 ? Math.abs(v).toFixed(0) : Math.abs(v).toFixed(1);

  function drawFlows() {
    gFlow.replaceChildren();
    screenFlow.replaceChildren();
    flowItems = [];
    const mode = sc.flows && sc.flows.length ? STORE.flow : 'off';
    for (const [m, b] of Object.entries(flowBtns)) b.classList.toggle('on', m === mode);
    root.classList.toggle('flows-on', mode !== 'off');
    if (mode === 'off') { schedule(); return; }
    const color = FLOW_COLOR[mode];
    const vmax = Math.max(1e-6, ...sc.flows.flatMap((f) => f[mode].map(Math.abs)));
    const a = 0.42 * sc.u;                                  // arrow half-length (world units)
    for (const f of sc.flows) {
      const [vf, vt] = f[mode];
      const g = el('g', { 'data-ends': f.ends.join('|') }, gFlow);
      const [h1, h2] = splitHalf(f.pts);
      // value > 0 = power enters the branch at that end: from-half points away from the from bus,
      // to-half points away from the to bus (i.e. towards the middle)
      const halves = [[vf > 0 ? h1 : h1.slice().reverse(), Math.abs(vf)],
                      [vt > 0 ? h2.slice().reverse() : h2, Math.abs(vt)]];
      for (const [pts, mag] of halves) {
        if (mag < 0.05 || pts.length < 2) continue;
        const len = polyLen(pts);
        if (len < 1e-6) continue;
        const n = Math.max(1, Math.min(4, Math.round(len / (5 * sc.u))));
        const speed = sc.u * (1.2 + 3.8 * mag / vmax);       // bigger flow = faster arrows
        const dur = Math.max(0.6, len / speed);
        const d = 'M' + pts.map((p) => `${p[0]},${p[1]}`).join(' L');
        for (let i = 0; i < n; i++) {
          const arrow = el('path', {
            d: `M ${-a} ${-0.62 * a} L ${a} 0 L ${-a} ${0.62 * a} L ${-0.45 * a} 0 Z`,
            fill: color, class: 'farrow',
          }, g);
          if (REDUCED_MOTION || staticFlows) {
            const [x, y, ang] = pointAt(pts, (i + 0.5) / n);
            arrow.setAttribute('transform', `translate(${x},${y}) rotate(${ang})`);
          } else {
            el('animateMotion', {
              dur: `${dur.toFixed(2)}s`, repeatCount: 'indefinite', rotate: 'auto', path: d,
              begin: `${(-i * dur / n).toFixed(2)}s`,
            }, arrow);
          }
        }
      }
      if (sc.dense || Math.max(Math.abs(vf), Math.abs(vt)) < 0.05) continue;   // nothing worth a label
      // label: the sending-end value when both halves agree, otherwise both end values
      const same = (vf > 0) !== (vt > 0);
      const txt = same ? `${fmtFlow(vf > 0 ? vf : vt)} ${FLOW_UNIT[mode]}`
        : `${fmtFlow(vf)} | ${fmtFlow(vt)} ${FLOW_UNIT[mode]}`;
      const [anchor, base, dx, dy] = ANCHOR[f.pos] || ANCHOR['top center'];
      const lbl = el('text', {
        'text-anchor': anchor, 'dominant-baseline': base, fill: color, 'font-size': 10, class: 'lbl flowlbl',
        'data-ends': f.ends.join('|'),
      }, screenFlow);
      lbl.textContent = txt;
      flowItems.push([lbl, f.lab[0], f.lab[1], dx, dy]);
    }
    schedule();
  }
  drawFlows();
  place();

  const toWorld = (ev) => {
    const r = svg.getBoundingClientRect();
    const { k, tx, ty } = STORE.view;
    return [(ev.clientX - r.left - tx) / k, (ev.clientY - r.top - ty) / k];
  };
  const snap = ([x, y]) => STORE.snap ? [Math.round(x / sc.grid) * sc.grid, Math.round(y / sc.grid) * sc.grid] : [x, y];
  const round = (v) => Math.round(v * 10) / 10;

  // ------------------------------------------------------------ tools
  function setTool(name) {
    STORE.tool = name;
    STORE.pending = null;
    gTemp.replaceChildren();
    for (const [n, b] of Object.entries(toolBtns)) b.classList.toggle('on', n === name);
    root.dataset.tool = name;
    hint.textContent = editable ? HINT[name] : 'Click an element to inspect it · drag to pan · wheel to zoom';
  }
  setTool(STORE.tool);

  function deleteSelected() {
    if (!data.selected) return;
    const [kind, id] = data.selected;
    if (kind === 'bus') {
      const n = sc.paths.filter((p) => p.ends && (p.ends[0] === id || p.ends[1] === id))
        .reduce((s, p) => s.add(p.sel[1]), new Set()).size;
      if (!window.confirm(`Delete bus ${id}` + (n ? ` and its ${n} branch(es) and generator(s)?` : '?'))) return;
    }
    send('delete', { kind, id });
  }

  const busOf = (target) => {
    const e = target.closest('[data-bus]');
    return e ? e.getAttribute('data-bus') : null;
  };
  const selOf = (target) => {
    const e = target.closest('[data-sel]');
    if (!e) return null;
    const s = e.getAttribute('data-sel');
    const i = s.indexOf(':');
    return [s.slice(0, i), s.slice(i + 1)];
  };

  // ------------------------------------------------------------ maximized layout, PNG export
  // Maximized: Python pins the whole diagram area (header controls, legend, side panels) over the
  // window; here the drawing grows to the space left below its top edge.
  function size() {
    const h = maxed ? Math.max(320, Math.floor(window.innerHeight - root.getBoundingClientRect().top - 36)) : H;
    if (root.style.height !== h + 'px') { root.style.height = h + 'px'; return true; }
    return false;
  }
  const onResize = () => { if (size()) schedule(); };
  window.addEventListener('resize', onResize);
  requestAnimationFrame(() => {
    const changed = size();
    if (STORE.maxed !== maxed) { STORE.maxed = maxed; fit(); }   // re-fit when switching layouts
    place();
  });

  async function exportPng() {
    const w = svg.clientWidth, h = svg.clientHeight;
    staticFlows = true; drawFlows(); place();
    const clone = svg.cloneNode(true);
    staticFlows = false; drawFlows(); place();
    clone.setAttribute('xmlns', NS);
    clone.setAttribute('width', w);
    clone.setAttribute('height', h);
    clone.setAttribute('font-family', 'Inter, system-ui, sans-serif');
    // what the page's stylesheet does, written onto the elements (a standalone image has no CSS)
    clone.querySelector('.bg').setAttribute('fill', '#121a2e');
    clone.querySelectorAll('.hit, .temp').forEach((e) => e.remove());
    if (root.classList.contains('flows-on')) clone.querySelectorAll('.brid').forEach((e) => e.remove());
    clone.querySelectorAll('.nodetxt').forEach((e) => { e.setAttribute('fill', '#08101d'); e.setAttribute('font-size', 10); });
    clone.querySelectorAll('.flowlbl').forEach((e) => {
      e.setAttribute('font-weight', 600); e.setAttribute('stroke', '#121a2e');
      e.setAttribute('stroke-width', 3); e.setAttribute('paint-order', 'stroke');
    });
    const mode = root.classList.contains('flows-on') ? STORE.flow : null;
    const cap = el('text', { x: 12, y: h - 12, fill: '#9fb0ca', 'font-size': 12 }, clone);
    cap.textContent = (data.title || 'Single-line diagram') +
      (mode ? ` · arrows: ${mode === 'P' ? 'real power (MW)' : 'reactive power (MVAr)'}` : '');
    const img = new Image();
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(new XMLSerializer().serializeToString(clone));
    try { await img.decode(); } catch (e) { hint.textContent = 'Could not render the image.'; return; }
    const scale = 2;
    const canvas = document.createElement('canvas');
    canvas.width = w * scale; canvas.height = h * scale;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(img, 0, 0, w * scale, h * scale);
    canvas.toBlob((blob) => {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `${data.name || 'network'}_diagram.png`;
      root.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
      hint.textContent = `Saved ${a.download}`;
    }, 'image/png');
  }

  // ------------------------------------------------------------ pointer handling
  let drag = null;    // {mode: 'pan'|'bus', ...}

  function rubber(from, to) {
    gTemp.replaceChildren();
    el('line', { x1: from[0], y1: from[1], x2: to[0], y2: to[1], class: 'rubber',
                 'vector-effect': 'non-scaling-stroke' }, gTemp);
  }

  function onDown(ev) {
    if (ev.button !== 0) return;
    root.focus({ preventScroll: true });
    const tool = STORE.tool;
    const bus = busOf(ev.target);
    const onBg = ev.target === bg || ev.target === svg;
    if (editable && tool === 'select' && bus && ev.target.classList.contains('bus')) {
      drag = { mode: 'bus', bus, x0: ev.clientX, y0: ev.clientY, moved: false, start: busPos[bus] };
    } else if (onBg || tool === 'select') {
      drag = { mode: 'pan', x0: ev.clientX, y0: ev.clientY, moved: false,
               tx: STORE.view.tx, ty: STORE.view.ty, target: ev.target };
    } else {
      drag = { mode: 'click', x0: ev.clientX, y0: ev.clientY, moved: false, target: ev.target };
    }
    svg.setPointerCapture(ev.pointerId);
    ev.preventDefault();
  }

  function onMove(ev) {
    if (STORE.pending && !drag) rubber(busPos[STORE.pending], toWorld(ev));
    if (!drag) return;
    const dx = ev.clientX - drag.x0, dy = ev.clientY - drag.y0;
    if (!drag.moved && Math.hypot(dx, dy) > 4) drag.moved = true;
    if (!drag.moved) return;
    tip.style.display = 'none';
    if (drag.mode === 'pan') {
      STORE.view.tx = drag.tx + dx;
      STORE.view.ty = drag.ty + dy;
      schedule();
    } else if (drag.mode === 'bus') {
      const { k } = STORE.view;
      const target = snap([drag.start[0] + dx / k, drag.start[1] + dy / k]);
      drag.to = target;
      const ox = target[0] - drag.start[0], oy = target[1] - drag.start[1];
      root.querySelectorAll(`.world [data-bus="${CSS.escape(drag.bus)}"]`)
        .forEach((e) => e.setAttribute('transform', `translate(${ox},${oy})`));
      for (const [e, x, y, ddx, ddy] of screenItems) {
        if (e.getAttribute('data-bus') === drag.bus) {
          e.setAttribute('transform', `translate(${(x + ox) * k + STORE.view.tx + ddx},${(y + oy) * k + STORE.view.ty + ddy})`);
        }
      }
      // branches attached to the bus: fade them and show straight rubber bands instead
      gTemp.replaceChildren();
      const seen = new Set();
      root.querySelectorAll('[data-ends]').forEach((e) => {
        const [a, b] = e.getAttribute('data-ends').split('|');
        if (a !== drag.bus && b !== drag.bus) return;
        e.classList.add('faded');
        const other = a === drag.bus ? b : a;
        if (seen.has(other) || !busPos[other]) return;
        seen.add(other);
        el('line', { x1: busPos[other][0], y1: busPos[other][1], x2: target[0], y2: target[1],
                     class: 'rubber', 'vector-effect': 'non-scaling-stroke' }, gTemp);
      });
    }
  }

  function onUp(ev) {
    if (!drag) return;
    const d = drag;
    drag = null;
    try { svg.releasePointerCapture(ev.pointerId); } catch (e) { /* already released */ }
    if (d.mode === 'bus' && d.moved && d.to) {
      hint.textContent = `Moving bus ${d.bus}…`;
      send('move', { id: d.bus, x: round(d.to[0]), y: round(d.to[1]) });
      return;
    }
    if (d.moved) return;           // that was a pan
    click(ev, d.target || ev.target);
  }

  function click(ev, target) {
    const tool = STORE.tool;
    const bus = busOf(target);
    const onBg = target === bg || target === svg;
    if (tool === 'select') {
      const sel = selOf(target);
      if (sel) send('select', { kind: sel[0], id: sel[1] });
      else if (onBg && data.selected) send('select', { kind: null, id: null });
      return;
    }
    if (tool === 'bus') {
      if (!onBg) return;
      const [x, y] = snap(toWorld(ev));
      send('add_bus', { x: round(x), y: round(y) });
      return;
    }
    if (!bus) { hint.textContent = 'Click on a bus. ' + HINT[tool]; return; }
    if (tool === 'line' || tool === 'transformer') {
      if (!STORE.pending) {
        STORE.pending = bus;
        hint.textContent = `From bus ${bus} — now click the second bus · Esc to cancel`;
        rubber(busPos[bus], toWorld(ev));
      } else if (STORE.pending === bus) {
        hint.textContent = 'Pick a different bus for the other end.';
      } else {
        const from = STORE.pending;
        STORE.pending = null;
        gTemp.replaceChildren();
        send('add_branch', { from, to: bus, kind: tool });
      }
      return;
    }
    send('add_device', { kind: tool, bus });
  }

  function onWheel(ev) {
    ev.preventDefault();
    const r = svg.getBoundingClientRect();
    const mx = ev.clientX - r.left, my = ev.clientY - r.top;
    const v = STORE.view;
    const f = Math.exp(-ev.deltaY * 0.0015);
    const k = Math.min(Math.max(v.k * f, 0.05), 60);
    v.tx = mx - (mx - v.tx) * (k / v.k);
    v.ty = my - (my - v.ty) * (k / v.k);
    v.k = k;
    schedule();
  }

  function onOver(ev) {
    const e = ev.target.closest('[data-tip]');
    if (!e || drag) { tip.style.display = 'none'; return; }
    tip.innerHTML = safeTip(e.getAttribute('data-tip'));
    const r = root.getBoundingClientRect();
    tip.style.display = 'block';
    const x = ev.clientX - r.left + 14, y = ev.clientY - r.top + 14;
    tip.style.left = Math.min(x, r.width - tip.offsetWidth - 6) + 'px';
    tip.style.top = Math.min(y, r.height - tip.offsetHeight - 6) + 'px';
  }

  function onKey(ev) {
    if (ev.key === 'Escape' && maxed && (!editable || (STORE.tool === 'select' && !STORE.pending))) {
      send('maximize', { on: false }); ev.preventDefault(); return;      // Esc: tool first, then the layout
    }
    if (!editable) return;
    if (ev.key === 'Escape') { setTool('select'); ev.preventDefault(); }
    else if ((ev.key === 'Delete' || ev.key === 'Backspace') && data.selected) { deleteSelected(); ev.preventDefault(); }
    else if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'z' && data.can_undo) { send('undo'); ev.preventDefault(); }
  }

  svg.addEventListener('pointerdown', onDown);
  svg.addEventListener('pointermove', onMove);
  svg.addEventListener('pointerup', onUp);
  svg.addEventListener('wheel', onWheel, { passive: false });
  svg.addEventListener('pointerover', onOver);
  svg.addEventListener('pointermove', onOver);
  svg.addEventListener('pointerleave', () => { tip.style.display = 'none'; });
  root.onkeydown = onKey;            // property, not addEventListener: the root is reused between updates
  const ro = new ResizeObserver(() => schedule());
  ro.observe(root);

  if (data.message) hint.textContent = data.message;
  if (STORE.focused && root.getRootNode().activeElement !== root) root.focus({ preventScroll: true });

  const cleanup = () => {
    ro.disconnect(); if (raf) cancelAnimationFrame(raf);
    window.removeEventListener('resize', onResize);
  };
  STORE.cleanup = cleanup;
  return cleanup;
}
