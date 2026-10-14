/* Fluent monitor - browser frontend (plain JS + uPlot). */
"use strict";
(() => {

// ------------------------------------------------------------------ constants & helpers
const PALETTE = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
                 "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"];   // matplotlib tab10
const DASHES = [null, [6, 4], [2, 3], [8, 3, 2, 3]];                       // after 10 curves
const COUNTERS = ["iteration", "time step", "flow-time", "flow time"];
const HOVER_TOL = 10, TOUCH_TOL = 24, STALE_AFTER = 300;
const FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", "DejaVu Sans", Arial, sans-serif';
const coarse = matchMedia("(hover: none) and (pointer: coarse)").matches;
const narrowMQ = matchMedia("(max-width: 760px)");

const $ = s => document.querySelector(s);
const isCounter = n => COUNTERS.includes(n.trim().toLowerCase());
const shortLabel = n => { const m = /^[^()]+\((.+)\)$/.exec(n); return m ? m[1] : n; };
const stem = f => f.replace(/\.[^.]*$/, "");
const shortName = f => { const s = stem(f); return s.endsWith("-rfile") ? s.slice(0, -6) : s; };
const isGroup = k => k.startsWith("g:");
const keyName = k => k.slice(2);

function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "class") e.className = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c != null) e.append(c);
  return e;
}

function ago(s) {
  if (s < 0) s = 0;
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) return `${(s / 3600).toFixed(1)} h`;
  return `${(s / 86400).toFixed(1)} d`;
}

function fmtValue(v) {                    // hover readout: up to 12 significant digits
  if (v == null) return "–";
  const a = Math.abs(v);
  if (a !== 0 && (a < 1e-4 || a >= 1e12)) return v.toExponential(11).replace(/\.?0+e/, "e");
  return String(parseFloat(v.toPrecision(12)));
}

function fmtShort(v) {                    // log-axis ticks
  if (v === 0) return "0";
  const a = Math.abs(v);
  if (a >= 1e-3 && a < 1e5) return String(parseFloat(v.toPrecision(4)));
  const e = Math.floor(Math.log10(a) + 1e-9);
  const m = parseFloat((v / 10 ** e).toPrecision(3));
  return (m === 1 ? "1" : m === -1 ? "-1" : String(m)) + "e" + e;
}

function tickValues(u, splits, ai) {      // plain numbers, never an offset; exponent form when huge/tiny
  if (!splits.length) return [];
  if (u.scales[u.axes[ai].scale].distr === 3) return splits.map(fmtShort);
  const step = splits.length > 1 ? Math.abs(splits[1] - splits[0]) : (Math.abs(splits[0]) || 1);
  const maxAbs = Math.max(...splits.map(Math.abs));
  if (maxAbs >= 1e6 || (maxAbs > 0 && maxAbs < 1e-3)) {
    const e = Math.floor(Math.log10(maxAbs) + 1e-9), p = 10 ** e;
    const dec = Math.max(0, Math.min(10, -Math.floor(Math.log10(step / p) + 1e-9)));
    return splits.map(v => Math.abs(v) < step * 1e-6 ? "0" : (v / p).toFixed(dec) + "e" + e);
  }
  const dec = Math.max(0, Math.min(12, -Math.floor(Math.log10(step) + 1e-9)));
  return splits.map(v => { const s = v.toFixed(dec); return /^-0(\.0*)?$/.test(s) ? s.slice(1) : s; });
}

function axisSize(u, values, ai, cycle) {  // width of the y axis from its widest tick label
  const axis = u.axes[ai];
  if (cycle > 1) return axis._size;
  let size = axis.ticks.size + axis.gap;
  const longest = (values || []).reduce((a, v) => (v.length > a.length ? v : a), "");
  if (longest) {
    u.ctx.font = axis.font[0];
    size += u.ctx.measureText(longest).width / devicePixelRatio;
  }
  return Math.ceil(size) + 6;
}

function lowerBound(xs, v) {
  let lo = 0, hi = xs.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (xs[m] < v) lo = m + 1; else hi = m; }
  return lo;
}

async function api(url, opts) {
  const r = await fetch(url, Object.assign({cache: "no-store", credentials: "same-origin"}, opts || {}));
  if (r.status === 403) { const e = new Error("access denied"); e.denied = true; throw e; }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}

// ------------------------------------------------------------------ state
const S = {
  folder: null,
  listing: [],              // file names in the folder
  info: {},                 // name -> {v, n, columns, last_change}
  groups: {},               // group name -> [file names]
  ticked: [],               // keys "f:<file>" / "g:<group>"
  view: "tabs", cols: "auto", refresh: 2, current: null,
  per: {},                  // key -> {logy, lastn, hidden: [...]}   (saved)
  offset: 0,                // server clock - browser clock (s)
  loaded: false,
};
const store = {};           // file name -> {v, columns, n, cols: [[...], ...], gen}
const views = {};           // key -> {follow, x: [min,max] | null, y: [min,max] | null}   (not saved)
const plots = new Map();    // key -> Plot (only the current view mode)

const storageKey = () => "fluent-monitor:" + (S.folder || "");
function save() {
  try {
    localStorage.setItem(storageKey(), JSON.stringify(
      {ticked: S.ticked, view: S.view, cols: S.cols, refresh: S.refresh, current: S.current, per: S.per}));
  } catch (e) { /* storage unavailable: settings last for this page only */ }
}
function restore() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(storageKey()) || "null"); } catch (e) { saved = null; }
  if (saved) {
    for (const k of ["ticked", "view", "cols", "refresh", "current", "per"]) if (saved[k] != null) S[k] = saved[k];
  } else {
    S.ticked = S.listing.map(f => "f:" + f);      // first visit: show everything
    S.view = narrowMQ.matches ? "tabs" : "grid";
  }
}
const per = key => (S.per[key] ||= {logy: false, lastn: 0, hidden: []});
const viewOf = key => (views[key] ||= {follow: true, x: null, y: null});

function keyExists(k) {
  return isGroup(k) ? keyName(k) in S.groups : S.listing.includes(keyName(k));
}
function orderedTicked() {         // files in folder order, then combined plots
  const set = new Set(S.ticked);
  return [...S.listing.map(f => "f:" + f).filter(k => set.has(k)),
          ...Object.keys(S.groups).map(g => "g:" + g).filter(k => set.has(k))];
}
const titleOf = k => (isGroup(k) ? keyName(k) : stem(keyName(k)));
const sourcesOf = k => (isGroup(k) ? (S.groups[keyName(k)] || []).filter(f => S.listing.includes(f)) : [keyName(k)]);

// ------------------------------------------------------------------ plot
class Plot {
  constructor(key, compact) {
    this.key = key;
    this.compact = compact;
    this.vs = viewOf(key);
    this.u = null;
    this.sig = null;
    this.gen = null;
    this.meta = [];
    this.prog = 0;
    this.tip = null;
    this.resizeTimer = 0;

    const p = per(key);
    this.cbFollow = h("input", {type: "checkbox", onchange: () => this.setFollow(this.cbFollow.checked)});
    this.cbLog = h("input", {type: "checkbox", onchange: () => {
      p.logy = this.cbLog.checked; save(); this.vs.y = null; this.sig = null; this.update(true);
    }});
    this.cbLog.checked = !!p.logy;
    this.edLast = h("input", {type: "number", class: "lastn", min: "0", step: "100", placeholder: "all",
                              inputmode: "numeric", title: "Show only the last N iterations (empty: all)",
                              onchange: () => {
                                p.lastn = Math.max(0, parseInt(this.edLast.value, 10) || 0); save();
                                this.edLast.value = p.lastn || "";
                                this.gen = null; this.setFollow(true);
                              }});
    this.edLast.value = p.lastn || "";
    const bar = h("div", {class: "pbar"},
      compact ? h("span", {class: "ptitle", title: titleOf(key)}, titleOf(key)) : null,
      h("label", {title: "Keep the newest data in view"}, this.cbFollow, "Follow"),
      h("label", null, this.cbLog, "Log y"),
      h("label", null, "Last", this.edLast),
      h("span", {class: "spacer"}),
      compact ? h("button", {title: "Open as a tab", onclick: () => openAsTab(key)}, "⤢") : null,
      h("button", {title: "Save as PNG image", onclick: () => this.savePng()}, "PNG"));
    this.chartEl = h("div", {class: "pchart"});
    this.legendEl = h("div", {class: "plegend"});
    this.statusEl = h("div", {class: "pstatus"});
    this.mark = h("div", {class: "hmark", hidden: true});
    this.tipEl = h("div", {class: "htip", hidden: true});
    this.chartEl.append(this.mark, this.tipEl);
    this.el = h("section", {class: "plot"}, bar,
                compact ? null : h("div", {class: "phead"}, titleOf(key)),
                h("div", {class: "pbody"}, this.chartEl, this.legendEl), this.statusEl);
    this.el.__plot = this;
    this.ro = new ResizeObserver(() => {
      clearTimeout(this.resizeTimer);
      this.resizeTimer = setTimeout(() => this.resize(), 120);     // redraw once resizing pauses
    });
    this.ro.observe(this.chartEl);
    if (!compact || !coarse) this.chartEl.addEventListener("dblclick", e => e.preventDefault());
    this.cbFollow.checked = this.vs.follow;
  }

  destroy() {
    this.ro.disconnect();
    clearTimeout(this.resizeTimer);
    if (this.u) this.u.destroy();
    this.u = null;
    this.el.remove();
  }

  // ---- data
  assemble() {
    const p = per(this.key);
    const names = sourcesOf(this.key);
    const srcs = names.map(n => [n, store[n]]).filter(([, s]) => s && s.columns.length);
    const perFile = srcs.map(([, s]) => { const c = s.columns.filter(isCounter); return c.length ? c : s.columns.slice(0, 1); });
    let counters = perFile.length ? perFile[0].filter(c => perFile.every(o => o.includes(c))) : [];
    if (perFile.length && !counters.length) counters = perFile[0].slice(0, 1);
    const xname = counters[0] || "Iteration";
    const combined = isGroup(this.key);
    const meta = [], tables = [];
    let k = 0;
    for (const [file, s] of srcs) {
      const xi = Math.max(0, s.columns.indexOf(xname));
      let x = s.cols[xi];
      const from = p.lastn > 0 ? Math.max(0, x.length - p.lastn) : 0;
      if (from) x = x.slice(from);
      const table = [x];
      s.columns.forEach((name, i) => {
        if (i === xi || isCounter(name)) return;
        let y = from ? s.cols[i].slice(from) : s.cols[i];
        if (p.logy) y = y.map(v => (v != null && v > 0 ? v : null));
        table.push(y);
        meta.push({file, name, label: combined ? name : shortLabel(name), hide: `${file}|${name}`,
                   color: PALETTE[k % 10], dash: DASHES[Math.floor(k / 10) % DASHES.length]});
        k++;
      });
      tables.push(table);
    }
    let data;
    if (!tables.length) data = [[]];
    else if (tables.length === 1) data = tables[0];
    else data = uPlot.join(tables);
    const sig = JSON.stringify([xname, p.logy, this.compact, meta.map(m => m.hide),
                                srcs.map(([n, s]) => s.v)]);
    const gen = srcs.map(([n, s]) => `${n}:${s.gen}`).join("|") + "|" + p.lastn;
    return {xname, meta, data, sig, gen, srcs};
  }

  update(force) {
    const a = this.assemble();
    this.srcs = a.srcs;
    if (!force && a.sig === this.sig && a.gen === this.gen) { this.updateStatus(); return; }
    if (a.sig !== this.sig || !this.u) this.build(a);
    else {
      this.u.setData(a.data, false);
      this.gen = a.gen;
      if (this.vs.follow) this.fit();
      if (this.tip) this.showTip(this.tip.px, this.tip.py, this.tip.tol, true);
    }
    this.updateStatus();
  }

  build(a) {
    const {xname, meta, data} = a;
    this.meta = meta;
    this.xname = xname;
    this.sig = a.sig;
    this.gen = a.gen;
    const p = per(this.key);
    if (this.u) { this.u.destroy(); this.u = null; }
    this.hideTip();
    const hidden = new Set(p.hidden);
    const fs = this.compact ? 11 : 12;
    const axisCommon = {
      stroke: "#000", font: `${fs}px ${FONT}`, labelFont: `${fs + 1}px ${FONT}`,
      grid: {stroke: "rgba(0,0,0,0.11)", width: 1},
      ticks: {stroke: "#000", width: 1, size: 4},
      values: tickValues,
    };
    const only = meta.length === 1 ? meta[0].label : "";
    const opts = {
      width: Math.max(50, this.chartEl.clientWidth),
      height: Math.max(50, this.chartEl.clientHeight),
      pxAlign: 0,
      legend: {show: false},
      padding: [10, 14, 0, 4],
      scales: {
        x: {time: false, auto: false},
        y: p.logy ? {auto: false, distr: 3, log: 10} : {auto: false},
      },
      axes: [
        Object.assign({}, axisCommon, {label: this.compact ? null : xname, labelSize: this.compact ? 0 : 20,
                                       space: this.compact ? 60 : 70, size: fs + 14}),
        Object.assign({}, axisCommon, {label: (!this.compact && only) ? only : null,
                                       labelSize: (!this.compact && only) ? 20 : 0, space: 32, size: axisSize}),
      ],
      series: [{}].concat(meta.map(m => ({
        label: m.label, stroke: m.color, width: 1.25, dash: m.dash || undefined,
        points: {show: false}, spanGaps: true, show: !hidden.has(m.hide),
      }))),
      cursor: {
        x: false, y: false, points: {show: false},
        drag: {x: true, y: true, setScale: true, dist: 6},
        bind: {dblclick: () => e => this.onDblClick(e)},
      },
      hooks: {
        draw: [u => this.drawFrame(u)],
        setScale: [(u, sc) => this.onScale(sc)],
        setCursor: [u => this.onCursor(u)],
      },
    };
    this.prog++;
    this.u = new uPlot(opts, data, this.chartEl);
    this.prog--;
    this.u.over.addEventListener("wheel", e => this.onWheel(e), {passive: false});
    this.u.over.addEventListener("touchstart", e => this.onTouchStart(e), {passive: false});
    this.u.over.addEventListener("touchmove", e => this.onTouchMove(e), {passive: false});
    this.u.over.addEventListener("touchend", e => this.onTouchEnd(e), {passive: false});
    this.u.over.addEventListener("touchcancel", () => { this.pinch = null; this.tap = null; });
    this.buildLegend();
    if (this.vs.follow || !this.vs.x) this.fit();
    else this.applyRange(this.vs.x, this.vs.y || this.yRange(this.vs.x));
  }

  drawFrame(u) {                 // matplotlib-like box around the axes
    const c = u.ctx, b = u.bbox, r = devicePixelRatio || 1;
    c.save();
    c.setLineDash([]);
    c.strokeStyle = "#000";
    c.lineWidth = Math.max(1, Math.round(r));
    const o = (c.lineWidth % 2) / 2;
    c.strokeRect(Math.round(b.left) + o, Math.round(b.top) + o, Math.round(b.width), Math.round(b.height));
    c.restore();
  }

  // ---- ranges
  dataXRange() {
    const xs = this.u.data[0];
    if (!xs || !xs.length) return null;
    return [xs[0], xs[xs.length - 1]];
  }

  yRange(xr) {
    const u = this.u, log = per(this.key).logy;
    const xs = u.data[0];
    if (!xs || !xs.length) return null;
    let i0 = 0, i1 = xs.length - 1;
    if (xr) { i0 = lowerBound(xs, xr[0]); i1 = Math.min(xs.length - 1, lowerBound(xs, xr[1] + 1e-12) - 1); }
    let lo = Infinity, hi = -Infinity;
    for (let s = 1; s < u.data.length; s++) {
      if (!u.series[s].show) continue;
      const ys = u.data[s];
      for (let j = i0; j <= i1; j++) {
        const v = ys[j];
        if (v == null || !isFinite(v) || (log && v <= 0)) continue;
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
    }
    if (lo > hi) return null;
    if (log) {
      let a = Math.log10(lo), b = Math.log10(hi);
      if (b - a < 1e-9) { a -= 0.5; b += 0.5; }
      const pad = (b - a) * 0.05;
      return [10 ** (a - pad), 10 ** (b + pad)];
    }
    if (hi - lo <= Math.abs(hi) * 1e-12) {
      const d = Math.abs(hi) * 0.05 || 1;
      return [lo - d, hi + d];
    }
    const pad = (hi - lo) * 0.05;
    return [lo - pad, hi + pad];
  }

  fit() {
    if (!this.u) return;
    const xr = this.dataXRange();
    if (!xr) return;
    let [a, b] = xr;
    if (b - a <= 0) { a -= 1; b += 1; } else { const pad = (b - a) * 0.02; a -= pad; b += pad; }
    const yr = this.yRange(xr) || [0, 1];
    this.applyRange([a, b], yr);
    this.vs.x = null; this.vs.y = null;
  }

  applyRange(xr, yr) {
    if (!this.u || !xr) return;
    this.prog++;
    try {
      this.u.batch(() => {
        this.u.setScale("x", {min: xr[0], max: xr[1]});
        if (yr) this.u.setScale("y", {min: yr[0], max: yr[1]});
      });
    } finally { this.prog--; }
  }

  userRange(xr, yr) {           // zoom / pan by the user: stop following
    this.applyRange(xr, yr);
    this.rememberRange();
  }

  rememberRange() {
    const sx = this.u.scales.x, sy = this.u.scales.y;
    this.vs.x = [sx.min, sx.max];
    this.vs.y = [sy.min, sy.max];
    if (this.vs.follow) { this.vs.follow = false; this.cbFollow.checked = false; }
  }

  onScale(sc) {
    if (this.prog || !this.u || sc !== "y") return;     // box zoom sets x then y
    this.rememberRange();
  }

  setFollow(on) {
    this.vs.follow = on;
    this.cbFollow.checked = on;
    if (on) { this.update(true); this.fit(); }
  }

  onDblClick(e) {
    if (this.compact && !coarse) { openAsTab(this.key); return; }
    this.setFollow(true);
  }

  // ---- zoom with the wheel: around the mouse; Shift = y only, Ctrl = x only
  onWheel(e) {
    if (!this.u) return;
    e.preventDefault();
    let d = e.deltaY || e.deltaX;
    if (e.deltaMode === 1) d *= 33; else if (e.deltaMode === 2) d *= 400;
    d = Math.max(-300, Math.min(300, d));
    const f = Math.pow(1.0015, d);           // > 1 zooms out
    const r = this.u.over.getBoundingClientRect();
    const px = e.clientX - r.left, py = e.clientY - r.top;
    const doX = !e.shiftKey, doY = !e.ctrlKey && !e.metaKey;
    this.zoomAt(px, py, doX ? f : 1, doY ? f : 1);
  }

  zoomAt(px, py, fx, fy) {
    const u = this.u, W = u.over.clientWidth, H = u.over.clientHeight;
    const xr = fx === 1 ? [u.scales.x.min, u.scales.x.max]
                        : [u.posToVal(px - px * fx, "x"), u.posToVal(px + (W - px) * fx, "x")];
    const yr = fy === 1 ? [u.scales.y.min, u.scales.y.max]
                        : [u.posToVal(py + (H - py) * fy, "y"), u.posToVal(py - py * fy, "y")];
    if (!(xr[1] > xr[0]) || !(yr[1] > yr[0])) return;
    this.userRange(xr, yr);
    if (this.tip) this.hideTip();
  }

  // ---- touch: two fingers zoom/pan, tap = value, double tap = reset, one finger scrolls the page
  touchPos(t) {
    const r = this.u.over.getBoundingClientRect();
    return [t.clientX - r.left, t.clientY - r.top];
  }

  onTouchStart(e) {
    if (!this.u) return;
    if (e.touches.length === 2) {
      e.preventDefault();
      this.tap = null;
      const u = this.u, log = per(this.key).logy;
      const lin = v => (log ? Math.log10(v) : v);
      const p = [...e.touches].map(t => this.touchPos(t));
      this.pinch = {
        p, log,
        a: p.map(q => u.posToVal(q[0], "x")),
        b: p.map(q => lin(u.posToVal(q[1], "y"))),
        x0: [u.scales.x.min, u.scales.x.max],
        y0: [lin(u.scales.y.min), lin(u.scales.y.max)],
      };
      this.hideTip();
    } else if (e.touches.length === 1 && !this.pinch) {
      const [x, y] = this.touchPos(e.touches[0]);
      this.tap = {x, y, t: performance.now(), moved: false};
    }
  }

  onTouchMove(e) {
    if (this.pinch && e.touches.length === 2) {
      e.preventDefault();
      const P = this.pinch, u = this.u, W = u.over.clientWidth, H = u.over.clientHeight;
      const q = [...e.touches].map(t => this.touchPos(t));
      // x: the values under the fingers stay under the fingers
      let xr;
      const dp = P.p[1][0] - P.p[0][0], dq = q[1][0] - q[0][0];
      if (Math.abs(dp) > 40 && Math.abs(dq) > 10 && Math.sign(dp) === Math.sign(dq)) {
        const upp = (P.a[1] - P.a[0]) / dq;
        const min = P.a[0] - q[0][0] * upp;
        xr = [min, min + W * upp];
      } else {
        const upp = (P.x0[1] - P.x0[0]) / W, am = (P.a[0] + P.a[1]) / 2, qm = (q[0][0] + q[1][0]) / 2;
        xr = [am - qm * upp, am - qm * upp + W * upp];
      }
      let yr;
      const ep = P.p[1][1] - P.p[0][1], eq = q[1][1] - q[0][1];
      if (Math.abs(ep) > 40 && Math.abs(eq) > 10 && Math.sign(ep) === Math.sign(eq)) {
        const upp = (P.b[0] - P.b[1]) / eq;
        const max = P.b[0] + q[0][1] * upp;
        yr = [max - H * upp, max];
      } else {
        const upp = (P.y0[1] - P.y0[0]) / H, bm = (P.b[0] + P.b[1]) / 2, qm = (q[0][1] + q[1][1]) / 2;
        yr = [bm + qm * upp - H * upp, bm + qm * upp];
      }
      if (P.log) yr = yr.map(v => 10 ** v);
      if (xr[1] > xr[0] && yr[1] > yr[0]) this.userRange(xr, yr);
    } else if (this.tap && e.touches.length === 1) {
      const [x, y] = this.touchPos(e.touches[0]);
      if (Math.hypot(x - this.tap.x, y - this.tap.y) > 10) this.tap.moved = true;
    }
  }

  onTouchEnd(e) {
    if (this.pinch) {
      e.preventDefault();
      if (e.touches.length < 2) this.pinch = null;
      this.tap = null;
      return;
    }
    const t = this.tap;
    this.tap = null;
    if (!t || t.moved || performance.now() - t.t > 500) return;
    e.preventDefault();                       // no emulated mouse events / clicks
    const now = performance.now(), last = this.lastTap;
    if (last && now - last.t < 350 && Math.hypot(t.x - last.x, t.y - last.y) < 30) {
      this.lastTap = null;
      this.hideTip();
      this.setFollow(true);
      return;
    }
    this.lastTap = {x: t.x, y: t.y, t: now};
    this.showTip(t.x, t.y, TOUCH_TOL) || this.hideTip();
  }

  // ---- hover readout
  onCursor(u) {
    if (coarse && this.tip && this.tip.tol === TOUCH_TOL) return;
    const {left, top} = u.cursor;
    if (left == null || left < 0 || u.select.width > 0) { this.hideTip(); return; }
    this.showTip(left, top, HOVER_TOL) || this.hideTip();
  }

  nearest(px, py, tol) {
    const u = this.u, xs = u.data[0];
    if (!xs || !xs.length) return null;
    let lo = lowerBound(xs, u.posToVal(px - tol, "x")) - 1;
    let hi = lowerBound(xs, u.posToVal(px + tol, "x")) + 1;
    lo = Math.max(0, lo); hi = Math.min(xs.length - 1, hi);
    const stride = Math.max(1, Math.floor((hi - lo) / 4000));
    let best = null, bd = tol * tol;
    for (let s = 1; s < u.data.length; s++) {
      if (!u.series[s].show) continue;
      const ys = u.data[s];
      let prev = null;
      for (let j = lo; j <= hi; j += stride) {
        const y = ys[j];
        if (y == null) continue;
        const X = u.valToPos(xs[j], "x"), Y = u.valToPos(y, "y");
        const d = (X - px) ** 2 + (Y - py) ** 2;
        if (d < bd) { bd = d; best = {s, j, X, Y}; }
        if (prev) {                            // the line between two points counts too
          const vx = X - prev.X, vy = Y - prev.Y, L = vx * vx + vy * vy;
          if (L > 0) {
            const t = Math.max(0, Math.min(1, ((px - prev.X) * vx + (py - prev.Y) * vy) / L));
            const ds = (prev.X + t * vx - px) ** 2 + (prev.Y + t * vy - py) ** 2;
            if (ds < bd) { bd = ds; best = t < 0.5 ? {s, j: prev.j, X: prev.X, Y: prev.Y} : {s, j, X, Y}; }
          }
        }
        prev = {j, X, Y};
      }
    }
    return best;
  }

  showTip(px, py, tol, keep) {
    if (!this.u) return false;
    const n = this.nearest(px, py, tol);
    if (!n) { if (keep) this.hideTip(); return false; }
    const u = this.u, m = this.meta[n.s - 1];
    const off = this.overOffset();
    const cx = off[0] + n.X, cy = off[1] + n.Y;
    this.mark.style.left = cx + "px";
    this.mark.style.top = cy + "px";
    this.mark.style.borderColor = m.color;
    this.mark.hidden = false;
    const name = this.meta.length > 1 ? m.label : titleOf(this.key);
    this.tipEl.style.borderColor = m.color;
    this.tipEl.replaceChildren(h("b", null, name), h("br"),
      `${this.xname} ${fmtValue(u.data[0][n.j])}`, h("br"), fmtValue(u.data[n.s][n.j]));
    this.tipEl.hidden = false;
    const W = this.chartEl.clientWidth, tw = this.tipEl.offsetWidth, th = this.tipEl.offsetHeight;
    let tx = cx + 12, ty = cy - th - 10;
    if (tx + tw > W - 2) tx = cx - tw - 12;
    if (tx < 2) tx = 2;
    if (ty < 2) ty = cy + 12;
    this.tipEl.style.left = tx + "px";
    this.tipEl.style.top = ty + "px";
    this.tip = {px, py, tol};
    return true;
  }

  overOffset() {
    const a = this.u.over.getBoundingClientRect(), b = this.chartEl.getBoundingClientRect();
    return [a.left - b.left, a.top - b.top];
  }

  hideTip() {
    this.tip = null;
    this.mark.hidden = true;
    this.tipEl.hidden = true;
  }

  // ---- legend (click an entry to hide/show its curve)
  buildLegend() {
    const p = per(this.key);
    this.legendEl.replaceChildren();
    this.legendEl.hidden = this.meta.length < 2;
    this.meta.forEach((m, i) => {
      const sw = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      sw.setAttribute("width", "22"); sw.setAttribute("height", "8");
      const ln = document.createElementNS("http://www.w3.org/2000/svg", "line");
      for (const [k, v] of Object.entries({x1: 0, y1: 4, x2: 22, y2: 4, stroke: m.color, "stroke-width": 2}))
        ln.setAttribute(k, v);
      if (m.dash) ln.setAttribute("stroke-dasharray", m.dash.join(" "));
      sw.append(ln);
      const item = h("div", {class: "li" + (this.u.series[i + 1].show ? "" : " off"), title: m.name}, sw, h("span", null, m.label));
      item.addEventListener("click", () => {
        const show = !this.u.series[i + 1].show;
        this.u.setSeries(i + 1, {show});
        item.classList.toggle("off", !show);
        p.hidden = p.hidden.filter(x => x !== m.hide);
        if (!show) p.hidden.push(m.hide);
        save();
        if (this.vs.follow) this.fit();
        if (this.tip) this.hideTip();
      });
      this.legendEl.append(item);
    });
  }

  updateStatus() {
    let iter = null, rows = 0, last = 0;
    for (const [n, s] of this.srcs || []) {
      const xs = s.cols[0] || [];
      if (xs.length) iter = Math.max(iter ?? -Infinity, xs[xs.length - 1]);
      rows += s.n;
      last = Math.max(last, (S.info[n] || {}).last_change || 0);
    }
    if (iter == null) { this.statusEl.textContent = "waiting for data…"; this.statusEl.classList.remove("stale"); return; }
    const age = Date.now() / 1000 + S.offset - last;
    this.statusEl.textContent = `${this.xname || "Iteration"} ${fmtValue(iter)} · ${rows.toLocaleString("en")} rows`
      + (last ? ` · updated ${ago(age)} ago` : "");
    this.statusEl.classList.toggle("stale", last > 0 && age > STALE_AFTER);
  }

  resize() {
    if (!this.u) return;
    const w = this.chartEl.clientWidth, hh = this.chartEl.clientHeight;
    if (w < 20 || hh < 20) return;
    if (w === this.u.width && hh === this.u.height) return;
    this.hideTip();
    this.prog++;
    try { this.u.setSize({width: w, height: hh}); } finally { this.prog--; }
  }

  // ---- PNG: title + chart + legend, at screen resolution
  savePng() {
    if (!this.u) return;
    const r = devicePixelRatio || 1, src = this.u.ctx.canvas;
    const title = titleOf(this.key);
    const items = this.meta.map((m, i) => ({m, show: this.u.series[i + 1].show})).filter(x => x.show);
    const c = document.createElement("canvas"), ctx = c.getContext("2d");
    const font = `${12 * r}px ${FONT}`;
    ctx.font = font;
    const legW = items.length > 1 ? Math.max(...items.map(x => ctx.measureText(x.m.label).width)) + 44 * r : 0;
    const top = 34 * r;
    c.width = src.width + legW + 10 * r;
    c.height = src.height + top + 6 * r;
    ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, c.width, c.height);
    ctx.fillStyle = "#000"; ctx.font = `${16 * r}px ${FONT}`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(title, src.width / 2, top / 2);
    ctx.drawImage(src, 0, top);
    if (legW) {
      ctx.font = font; ctx.textAlign = "left";
      items.forEach(({m}, i) => {
        const y = top + 16 * r + i * 18 * r, x = src.width + 6 * r;
        ctx.strokeStyle = m.color; ctx.lineWidth = 2 * r;
        ctx.setLineDash(m.dash ? m.dash.map(d => d * r) : []);
        ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + 24 * r, y); ctx.stroke();
        ctx.fillStyle = "#000"; ctx.fillText(m.label, x + 30 * r, y);
      });
    }
    c.toBlob(blob => {
      const a = h("a", {href: URL.createObjectURL(blob), download: title.replace(/[\\/:*?"<>|]+/g, "_") + ".png"});
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    }, "image/png");
  }
}

// ------------------------------------------------------------------ page layout
const plotsEl = $("#plots"), tabbar = $("#tabbar");

function render() {
  const keys = orderedTicked();
  $("#placeholder").hidden = keys.length > 0;
  $("#btn-tabs").classList.toggle("on", S.view === "tabs");
  $("#btn-grid").classList.toggle("on", S.view === "grid");
  plotsEl.className = S.view;
  tabbar.hidden = S.view !== "tabs" || !keys.length;
  const compact = S.view === "grid";
  for (const [k, p] of plots) {
    if (!keys.includes(k) || p.compact !== compact) { p.destroy(); plots.delete(k); }
  }
  if (S.view === "tabs") {
    if (!keys.includes(S.current)) S.current = keys[0] || null;
    renderTabs(keys);
    for (const p of plots.values()) if (p.key !== S.current) p.el.remove();
    if (S.current) {
      let p = plots.get(S.current);
      if (!p) { p = new Plot(S.current, false); plots.set(S.current, p); }
      if (p.el.parentNode !== plotsEl) plotsEl.replaceChildren(p.el);
      p.update();
      p.resize();
    } else plotsEl.replaceChildren();
  } else {
    const els = keys.map(k => {
      let p = plots.get(k);
      if (!p) { p = new Plot(k, true); plots.set(k, p); }
      return p.el;
    });
    plotsEl.replaceChildren(...els);
    layoutGrid();
    for (const k of keys) plots.get(k).update();
  }
  save();
}

function renderTabs(keys) {
  tabbar.replaceChildren(...keys.map(k => h("button", {
    class: "tab" + (k === S.current ? " on" : ""), title: titleOf(k), "data-key": k,
    onclick: () => selectTab(k),
  }, titleOf(k))));
  const on = tabbar.querySelector(".tab.on");
  if (on) on.scrollIntoView({block: "nearest", inline: "nearest"});
}

function selectTab(k) {
  if (k === S.current) return;
  S.current = k;
  render();
}

function openAsTab(k) {
  S.view = "tabs";
  S.current = k;
  render();
}

function layoutGrid() {
  if (S.view !== "grid") return;
  const n = plots.size;
  if (!n) return;
  const narrow = narrowMQ.matches;
  const cols = narrow ? 1 : (S.cols === "auto" ? Math.ceil(Math.sqrt(n)) : Math.min(n, +S.cols));
  plotsEl.style.gridTemplateColumns = `repeat(${cols}, minmax(0, 1fr))`;
  const rows = Math.ceil(n / cols), gap = 10, padding = 20;
  const hh = Math.max(240, Math.floor((plotsEl.clientHeight - padding - gap * (rows - 1)) / rows));
  for (const p of plots.values()) p.el.style.height = narrow ? "" : hh + "px";
}

// wheel over the tab strip switches tabs
let wheelAcc = 0;
tabbar.addEventListener("wheel", e => {
  e.preventDefault();
  const keys = orderedTicked();
  if (keys.length < 2) return;
  wheelAcc += (e.deltaMode === 1 ? 33 : 1) * (e.deltaY || e.deltaX);
  if (Math.abs(wheelAcc) < 50) return;
  const step = wheelAcc > 0 ? 1 : -1;
  wheelAcc = 0;
  const i = keys.indexOf(S.current);
  selectTab(keys[Math.max(0, Math.min(keys.length - 1, i + step))]);
}, {passive: false});

// ------------------------------------------------------------------ side panel
function renderSide() {
  const ticked = new Set(S.ticked);
  const item = (key, text, del) => h("li", null,
    h("label", {title: text},
      h("input", {type: "checkbox", checked: ticked.has(key), onchange: e => toggleKey(key, e.target.checked)}),
      h("span", null, text)),
    del ? h("button", {class: "del", title: "Delete this combined plot", onclick: del}, "×") : null);
  $("#file-list").replaceChildren(...S.listing.map(f => item("f:" + f, f)));
  if (!S.listing.length) $("#file-list").append(h("li", {class: "hint"}, "No report files in this folder yet."));
  const groups = Object.keys(S.groups);
  $("#group-head").hidden = !groups.length;
  $("#group-list").replaceChildren(...groups.map(g =>
    item("g:" + g, g, () => deleteGroup(g))));
}

function toggleKey(key, on) {
  S.ticked = S.ticked.filter(k => k !== key);
  if (on) S.ticked.push(key);
  if (on && S.view === "tabs") S.current = key;
  render();
  poll(true);
}

async function combine() {
  const files = orderedTicked().filter(k => !isGroup(k)).map(keyName);
  if (files.length < 2) { alert("Tick at least two report files to combine."); return; }
  const name = (prompt("Name of the combined plot:", files.map(shortName).join(" + ")) || "").trim();
  if (!name) return;
  if (S.groups[name] && !confirm(`Replace the combined plot "${name}"?`)) return;
  try {
    const r = await api("/api/groups", {method: "POST", headers: {"Content-Type": "application/json"},
                                        body: JSON.stringify({name, files})});
    S.groups = r.groups;
  } catch (e) { alert("Could not create the combined plot: " + e.message); return; }
  const gk = "g:" + name;
  S.ticked = S.ticked.filter(k => !files.includes(keyName(k)) || isGroup(k));
  if (!S.ticked.includes(gk)) S.ticked.push(gk);
  if (plots.has(gk)) { plots.get(gk).destroy(); plots.delete(gk); }
  S.current = gk;
  renderSide();
  render();
  closeSide();
}

async function deleteGroup(g) {
  if (!confirm(`Delete the combined plot "${g}"?`)) return;
  try {
    const r = await api("/api/groups", {method: "POST", headers: {"Content-Type": "application/json"},
                                        body: JSON.stringify({action: "delete", name: g})});
    S.groups = r.groups;
  } catch (e) { alert("Could not delete: " + e.message); return; }
  S.ticked = S.ticked.filter(k => k !== "g:" + g);
  delete S.per["g:" + g];
  renderSide();
  render();
}

const openSide = () => document.body.classList.add("side-open");
const closeSide = () => document.body.classList.remove("side-open");
$("#btn-menu").addEventListener("click", () => {
  if (narrowMQ.matches) document.body.classList.toggle("side-open");
  else document.body.classList.toggle("side-collapsed");
});
$("#btn-close-side").addEventListener("click", closeSide);
$("#scrim").addEventListener("click", closeSide);
$("#btn-open-files").addEventListener("click", openSide);
$("#btn-all").addEventListener("click", () => {
  const set = new Set(S.ticked);
  for (const f of S.listing) set.add("f:" + f);
  S.ticked = [...set]; renderSide(); render(); poll(true);
});
$("#btn-none").addEventListener("click", () => { S.ticked = []; renderSide(); render(); });
$("#btn-combine").addEventListener("click", combine);
$("#btn-tabs").addEventListener("click", () => { S.view = "tabs"; render(); });
$("#btn-grid").addEventListener("click", () => { S.view = "grid"; render(); });
$("#sel-cols").addEventListener("change", e => { S.cols = e.target.value; save(); layoutGrid(); });
$("#sel-refresh").addEventListener("change", e => { S.refresh = +e.target.value; save(); poll(true); });

let layoutTimer = 0;
addEventListener("resize", () => { clearTimeout(layoutTimer); layoutTimer = setTimeout(layoutGrid, 120); });
narrowMQ.addEventListener("change", () => { closeSide(); layoutGrid(); });

// ------------------------------------------------------------------ polling
function setConn(state, text) {
  const c = $("#conn");
  c.className = "conn " + state;
  $("#conn-text").textContent = text;
  c.title = text;
}

async function fetchFile(name) {
  const info = S.info[name];
  let s = store[name];
  if (s && s.v === info.v && s.n === info.n) return false;
  const from = s && s.v === info.v && info.n >= s.n ? s.n : 0;
  const d = await api(`/api/data?name=${encodeURIComponent(name)}&v=${encodeURIComponent(s ? s.v : "")}&from=${from}`);
  if (!s || d.start === 0 || d.v !== s.v || d.start !== s.n) {
    s = store[name] = {v: d.v, columns: d.columns, n: d.n, cols: d.data.map(c => c.slice()), gen: 0};
    if (s.cols.length < d.columns.length) s.cols = d.columns.map((_, i) => d.data[i] || []);
  } else {
    d.data.forEach((col, i) => { const dst = s.cols[i]; for (const v of col) dst.push(v); });
    s.n = d.n;
  }
  s.gen++;
  return true;
}

let pollTimer = 0, polling = false, pollAgain = false;
async function poll(now) {
  clearTimeout(pollTimer);
  if (polling) { pollAgain = true; return; }
  if (document.hidden && !now) { pollTimer = setTimeout(poll, S.refresh * 1000); return; }
  polling = true;
  try {
    const st = await api("/api/status");
    S.offset = st.server_time - Date.now() / 1000;
    const listingChanged = JSON.stringify(st.files.map(f => f.name)) !== JSON.stringify(S.listing)
                        || JSON.stringify(st.groups) !== JSON.stringify(S.groups);
    S.listing = st.files.map(f => f.name);
    S.info = Object.fromEntries(st.files.map(f => [f.name, f]));
    S.groups = st.groups || {};
    if (!S.loaded) {
      S.folder = st.folder;
      $("#folder").textContent = st.folder;
      $("#folder").title = st.folder;
      document.title = "Fluent monitor – " + st.folder.split(/[\\/]/).filter(Boolean).pop();
      restore();
      $("#sel-cols").value = S.cols;
      $("#sel-refresh").value = String(S.refresh);
      S.loaded = true;
    }
    if (listingChanged || !plots.size) {
      S.ticked = S.ticked.filter(keyExists);
      renderSide();
    }
    const keys = orderedTicked();
    const need = [...new Set(keys.flatMap(sourcesOf))];
    await Promise.all(need.map(fetchFile));
    if (listingChanged || plots.size !== keys.length && S.view === "grid" || (S.view === "tabs" && !plots.has(S.current) && keys.length))
      render();
    else for (const p of plots.values()) if (p.el.isConnected) p.update();
    // connection indicator: data age of the plotted files
    const newest = Math.max(0, ...need.map(n => S.info[n].last_change || 0), ...(need.length ? [] : st.files.map(f => f.last_change || 0)));
    const age = st.server_time - newest;
    if (newest && age > STALE_AFTER) setConn("stale", `no new data for ${ago(age)}`);
    else setConn("live", "live");
  } catch (e) {
    if (e.denied) setConn("down", "access denied – open the link with ?token= again");
    else setConn("down", "server not reachable");
  } finally {
    polling = false;
  }
  if (pollAgain) { pollAgain = false; return poll(true); }
  pollTimer = setTimeout(poll, S.refresh * 1000);
}

document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(true); });
setInterval(() => { for (const p of plots.values()) if (p.el.isConnected) p.updateStatus(); }, 1000);

if (coarse) document.body.classList.add("touch");
poll(true);
})();
