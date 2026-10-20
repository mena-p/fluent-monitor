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
  const sc = u.scales[u.axes[ai].scale];
  if (sc.distr === 3) {                     // log axis: label decades (and 2, 5 when it spans little)
    const decades = Math.log10(sc.max / sc.min);
    return splits.map(v => {
      if (v == null || !(v > 0)) return "";
      const m = Math.round(v / 10 ** Math.floor(Math.log10(v) + 1e-9));
      const show = decades > 2.5 ? m === 1 : decades > 1.2 ? (m === 1 || m === 5 || m === 2 && decades < 1.8) : true;
      return show ? fmtShort(v) : "";
    });
  }
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
  const longest = (values || []).reduce((a, v) => (v && v.length > a.length ? v : a), "");
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
  per: {},                  // key -> {logy, lastN, hidden: [...]}   (saved); lastN null = use S.lastn
  lastn: 0,                 // "Last N" for all plots (0 = all iterations)
  offset: 0,                // server clock - browser clock (s)
  loaded: false,
  epoch: null,              // server's folder generation; data of another epoch is dropped
  autoTick: false,          // first visit of a folder: tick all files once they are listed
  everLoaded: false,
};
const store = {};           // file name -> {v, columns, n, cols: [[...], ...], gen}
const views = {};           // key -> {follow, x: [min,max] | null, y: [min,max] | null}   (not saved)
const plots = new Map();    // key -> Plot (only the current view mode)

const storageKey = () => "fluent-monitor:" + (S.folder || "");
function save() {
  try {
    localStorage.setItem(storageKey(), JSON.stringify(
      {ticked: S.ticked, view: S.view, cols: S.cols, refresh: S.refresh, current: S.current, per: S.per,
       lastn: S.lastn, seenTrn: S.seenTrn}));
  } catch (e) { /* storage unavailable: settings last for this page only */ }
}
function restore() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(storageKey()) || "null"); } catch (e) { saved = null; }
  if (saved) {
    for (const k of ["ticked", "view", "cols", "refresh", "current", "per", "lastn", "seenTrn"]) if (saved[k] != null) S[k] = saved[k];
    for (const p of Object.values(S.per)) {          // before 2.5 a plot's own value was "lastn" (0 = all)
      if (p.lastN === undefined) p.lastN = p.lastn > 0 ? p.lastn : null;
      delete p.lastn;
    }
  } else {
    S.ticked = S.listing.map(f => "f:" + f);      // first visit: show everything
    S.autoTick = !S.listing.length;
    S.current = null;
    S.per = {};
    S.lastn = 0;
    S.seenTrn = S.listing.includes(RESIDUALS);
    if (!S.everLoaded) S.view = narrowMQ.matches ? "tabs" : "grid";
  }
}

function resetClient() {                         // another case folder: start from scratch
  for (const p of plots.values()) p.destroy();
  plots.clear();
  for (const k of Object.keys(store)) delete store[k];
  for (const k of Object.keys(views)) delete views[k];
  Object.assign(S, {loaded: false, folder: null, epoch: null, listing: [], info: {}, groups: {},
                    ticked: [], current: null, per: {}, lastn: 0, seenTrn: false, autoTick: false});
  plotsEl.replaceChildren();
  tabbar.replaceChildren();
  renderSide();
  $("#placeholder").hidden = false;
}
const RESIDUALS = "Residuals";       // data sets from the Fluent transcript (see transcript.py)
const MESSAGES = "Solver messages";  // cells/faces affected per iteration: drawn as dots
const per = key => (S.per[key] ||= {logy: key === "f:" + RESIDUALS || key === "f:" + MESSAGES, lastN: null, hidden: []});
const lastnOf = key => { const v = per(key).lastN; return v == null ? S.lastn : v; };   // 0 = all
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
    this.edLast = h("input", {type: "text", class: "lastn", inputmode: "numeric", autocomplete: "off",
                              onchange: () => {
                                const t = this.edLast.value.trim().toLowerCase();
                                p.lastN = t === "" ? null : (t === "all" ? 0 : Math.max(0, parseInt(t, 10) || 0));
                                save(); this.lastnChanged(); updateOwnNote();
                              }});
    this.btnLastReset = h("button", {class: "mini", title: "Use the setting for all plots again",
                                     onclick: () => { p.lastN = null; save(); this.lastnChanged(); updateOwnNote(); }}, "↺");
    this.syncLastn();
    this.selX = h("select", {class: "xsel", title: "Quantity on the x axis", hidden: true,
                             onchange: () => { p.xcol = this.selX.value; save(); this.vs.x = null; this.update(true); this.fit(); }});
    this.saveMenu = h("div", {class: "savemenu", hidden: true},
      h("button", {onclick: () => { this.saveMenu.hidden = true; this.savePng(); }}, "PNG image"),
      h("button", {onclick: () => { this.saveMenu.hidden = true; this.saveSvg(); }}, "SVG (vector, for reports)"));
    const saveBtn = h("button", {title: "Save the plot as an image", onclick: e => {
      e.stopPropagation();
      for (const m of document.querySelectorAll(".savemenu")) if (m !== this.saveMenu) m.hidden = true;
      this.saveMenu.hidden = !this.saveMenu.hidden;
      if (!this.saveMenu.hidden) {                 // fixed position: the toolbar may clip overflowing content
        const r = saveBtn.getBoundingClientRect();
        this.saveMenu.style.top = (r.bottom + 4) + "px";
        this.saveMenu.style.left = Math.max(8, r.right - this.saveMenu.offsetWidth) + "px";
      }
    }}, "Save ▾");
    const bar = h("div", {class: "pbar"},
      compact ? h("span", {class: "ptitle", title: titleOf(key)}, titleOf(key)) : null,
      h("label", {title: "Keep the newest data in view"}, this.cbFollow, "Follow"),
      h("label", null, this.cbLog, "Log y"),
      h("label", null, "Last", this.edLast, this.btnLastReset),
      this.selX,
      h("span", {class: "spacer"}),
      compact ? h("button", {title: "Open as a tab", onclick: () => openAsTab(key)}, "⤢") : null,
      h("span", {class: "savewrap"}, saveBtn, this.saveMenu));
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

  // "Last N": own value, or the value for all plots (shown greyed as placeholder)
  syncLastn() {
    const own = per(this.key).lastN;
    this.edLast.value = own == null ? "" : (own === 0 ? "all" : String(own));
    this.edLast.placeholder = S.lastn > 0 ? String(S.lastn) : "all";
    this.edLast.classList.toggle("own", own != null);
    this.btnLastReset.hidden = own == null;
    this.edLast.title = own == null
      ? `Show only the last N iterations of this plot. Now using the setting for all plots (${S.lastn || "all"}).`
      : `This plot's own value (${own || "all iterations"}; type "all" or 0 for all). Clear the field or press ↺ to use the setting for all plots.`;
  }

  lastnChanged() {
    this.syncLastn();
    this.gen = null;
    this.setFollow(true);
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
    const xname = counters.includes(p.xcol) ? p.xcol : (counters[0] || "Iteration");
    if (this.selX && JSON.stringify(counters) !== this.selX.dataset.opts) {   // x axis choice, if there is one
      this.selX.dataset.opts = JSON.stringify(counters);
      this.selX.replaceChildren(...counters.map(c => h("option", {value: c}, "x: " + c)));
      this.selX.hidden = counters.length < 2;
    }
    if (this.selX) this.selX.value = xname;
    const combined = isGroup(this.key);
    const meta = [], tables = [];
    let k = 0;
    for (const [file, s] of srcs) {
      const xi = Math.max(0, s.columns.indexOf(xname));
      let x = s.cols[xi];
      const lastn = lastnOf(this.key);
      const from = lastn > 0 ? Math.max(0, x.length - lastn) : 0;
      if (from) x = x.slice(from);
      const table = [x];
      s.columns.forEach((name, i) => {
        if (i === xi || isCounter(name)) return;
        let y = from ? s.cols[i].slice(from) : s.cols[i];
        if (p.logy) y = y.map(v => (v != null && v > 0 ? v : null));
        table.push(y);
        meta.push({file, name, label: combined || (S.info[file] || {}).kind === "transcript" ? name : shortLabel(name), hide: `${file}|${name}`,
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
    const gen = srcs.map(([n, s]) => `${n}:${s.gen}`).join("|") + "|" + lastnOf(this.key);
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
        ...(m.file === MESSAGES ? {paths: () => null, points: {show: true, size: 5, width: 1, fill: m.color}} : {}),
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
    this.showAll = h("div", {class: "li showall", hidden: !this.meta.some((m, i) => !this.u.series[i + 1].show),
                             title: "Show the hidden curves again", onclick: () => {
      this.meta.forEach((m, i) => { if (!this.u.series[i + 1].show) this.u.setSeries(i + 1, {show: true}); });
      p.hidden = [];
      save();
      this.buildLegend();
      if (this.vs.follow) this.fit();
    }}, "show all");
    this.legendEl.append(this.showAll);
    this.meta.forEach((m, i) => {
      const sw = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      sw.setAttribute("width", "22"); sw.setAttribute("height", "8");
      const dots = m.file === MESSAGES;
      const ln = document.createElementNS("http://www.w3.org/2000/svg", dots ? "circle" : "line");
      for (const [k, v] of Object.entries(dots ? {cx: 11, cy: 4, r: 3, fill: m.color}
                                               : {x1: 0, y1: 4, x2: 22, y2: 4, stroke: m.color, "stroke-width": 2}))
        ln.setAttribute(k, v);
      if (m.dash && !dots) ln.setAttribute("stroke-dasharray", m.dash.join(" "));
      sw.append(ln);
      const item = h("div", {class: "li" + (this.u.series[i + 1].show ? "" : " off"), title: m.name}, sw, h("span", null, m.label));
      item.addEventListener("click", () => {
        const show = !this.u.series[i + 1].show;
        this.u.setSeries(i + 1, {show});
        item.classList.toggle("off", !show);
        this.showAll.hidden = !this.meta.some((mm, j) => !this.u.series[j + 1].show);
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

  // ---- SVG: vector version of what is shown (current zoom, visible curves), for reports
  saveSvg() {
    const u = this.u;
    if (!u) return;
    const r = devicePixelRatio || 1;
    const W = u.width, H = u.height;
    const L = u.bbox.left / r, T = u.bbox.top / r, BW = u.bbox.width / r, BH = u.bbox.height / r;
    const title = titleOf(this.key);
    const items = this.meta.map((m, i) => ({m, i: i + 1})).filter(x => u.series[x.i].show);
    const meas = document.createElement("canvas").getContext("2d");
    meas.font = `12px ${FONT}`;
    const legW = items.length > 1 ? Math.ceil(Math.max(...items.map(x => meas.measureText(x.m.label).width))) + 46 : 0;
    const top = 32, f = v => Math.round(v * 100) / 100;
    const esc = t => String(t).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
    const o = [];
    o.push(`<svg xmlns="http://www.w3.org/2000/svg" width="${W + legW}" height="${H + top}" viewBox="0 0 ${W + legW} ${H + top}" ` +
           `font-family="DejaVu Sans, Helvetica, Arial, sans-serif" font-size="12">`);
    o.push(`<rect width="100%" height="100%" fill="#fff"/>`);
    o.push(`<text x="${f(W / 2)}" y="${top - 11}" text-anchor="middle" font-size="16">${esc(title)}</text>`);
    o.push(`<g transform="translate(0 ${top})">`);
    o.push(`<defs><clipPath id="area"><rect x="${f(L)}" y="${f(T)}" width="${f(BW)}" height="${f(BH)}"/></clipPath></defs>`);
    // grid, ticks, tick labels
    const ticks = [], grid = [], labels = [];
    for (const [ai, sc] of [[0, "x"], [1, "y"]]) {
      const ax = u.axes[ai], sp = ax._splits || [], vals = ax._values || [];
      sp.forEach((v, k) => {
        if (v == null) return;
        const pos = u.valToPos(v, sc);
        if (sc === "x") {
          const x = f(L + pos);
          if (x < L - 0.5 || x > L + BW + 0.5) return;
          grid.push(`M${x} ${f(T)}V${f(T + BH)}`);
          ticks.push(`M${x} ${f(T + BH)}v4`);
          if (vals[k]) labels.push(`<text x="${x}" y="${f(T + BH + 17)}" text-anchor="middle">${esc(vals[k])}</text>`);
        } else {
          const y = f(T + pos);
          if (y < T - 0.5 || y > T + BH + 0.5) return;
          grid.push(`M${f(L)} ${y}H${f(L + BW)}`);
          ticks.push(`M${f(L)} ${y}h-4`);
          if (vals[k]) labels.push(`<text x="${f(L - 7)}" y="${y}" dy="0.35em" text-anchor="end">${esc(vals[k])}</text>`);
        }
      });
    }
    o.push(`<path d="${grid.join("")}" stroke="#000" stroke-opacity="0.11" fill="none"/>`);
    // curves (thinned to ~4 points per pixel column, keeping each column's min and max)
    const xs = u.data[0];
    o.push(`<g clip-path="url(#area)" fill="none" stroke-width="1.25" stroke-linejoin="round">`);
    for (const {m, i} of items) {
      const ys = u.data[i], dots = m.file === MESSAGES, pts = [];
      let col = null, lo = null, hi = null, first = null, last = null;
      const flushCol = () => {
        if (first == null) return;
        const seq = [first, lo, hi, last].filter((p, k, a) => p && a.indexOf(p) === k).sort((a, b) => a.j - b.j);
        for (const p of seq) pts.push(p);
        first = lo = hi = last = null;
      };
      for (let j = 0; j < xs.length; j++) {
        const y = ys[j];
        if (y == null || !isFinite(y)) continue;
        const px = L + u.valToPos(xs[j], "x"), py = T + u.valToPos(y, "y");
        if (!isFinite(px) || !isFinite(py)) continue;
        const p = {x: px, y: py, j};
        if (dots) { pts.push(p); continue; }
        const c = Math.floor(px * 4);
        if (c !== col) { flushCol(); col = c; first = lo = hi = p; }
        if (py < hi.y) hi = p;
        if (py > lo.y) lo = p;
        last = p;
      }
      flushCol();
      if (!pts.length) continue;
      if (dots) {
        o.push(`<g fill="${m.color}" stroke="none">` + pts.map(p => `<circle cx="${f(p.x)}" cy="${f(p.y)}" r="2.5"/>`).join("") + `</g>`);
      } else {
        o.push(`<path stroke="${m.color}"${m.dash ? ` stroke-dasharray="${m.dash.join(" ")}"` : ""} d="M` +
               pts.map(p => `${f(p.x)} ${f(p.y)}`).join("L") + `"/>`);
      }
    }
    o.push(`</g>`);
    o.push(`<path d="${ticks.join("")}" stroke="#000" fill="none"/>`);
    o.push(`<rect x="${f(L)}" y="${f(T)}" width="${f(BW)}" height="${f(BH)}" fill="none" stroke="#000"/>`);
    o.push(...labels);
    const xl = u.axes[0].label, yl = u.axes[1].label;
    if (xl) o.push(`<text x="${f(L + BW / 2)}" y="${f(H - 5)}" text-anchor="middle" font-size="13">${esc(xl)}</text>`);
    if (yl) o.push(`<text transform="translate(13 ${f(T + BH / 2)}) rotate(-90)" text-anchor="middle" font-size="13">${esc(yl)}</text>`);
    items.forEach(({m}, k) => {                                 // legend
      if (items.length < 2) return;
      const x = W + 8, y = T + 8 + k * 18;
      o.push(m.file === MESSAGES ? `<circle cx="${x + 12}" cy="${y}" r="3" fill="${m.color}"/>`
        : `<path d="M${x} ${y}h24" stroke="${m.color}" stroke-width="2"${m.dash ? ` stroke-dasharray="${m.dash.join(" ")}"` : ""}/>`);
      o.push(`<text x="${x + 30}" y="${y}" dy="0.35em">${esc(m.label)}</text>`);
    });
    o.push(`</g></svg>`);
    const blob = new Blob([o.join("\n")], {type: "image/svg+xml"});
    const a = h("a", {href: URL.createObjectURL(blob), download: title.replace(/[\\/:*?"<>|]+/g, "_") + ".svg"});
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  }
}

const hideSaveMenus = () => { for (const m of document.querySelectorAll(".savemenu")) m.hidden = true; };
document.addEventListener("click", hideSaveMenus);
document.addEventListener("scroll", hideSaveMenus, true);

// ------------------------------------------------------------------ page layout
const plotsEl = $("#plots"), tabbar = $("#tabbar");

function render() {
  const keys = orderedTicked();
  $("#placeholder").hidden = keys.length > 0;
  const empty = S.loaded && !S.listing.length;      // e.g. first start in the home folder
  $("#ph-text").textContent = empty ? `No report files or Fluent transcripts in ${S.folder} yet.`
                                    : "Tick report files to plot them.";
  $("#btn-ph-folder").hidden = !empty;
  $("#btn-open-files").hidden = empty;
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
  updateOwnNote();
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
  const item = (key, text, del) => h("li", {class: S.info[keyName(key)] && S.info[keyName(key)].kind === "transcript" ? "trn" : null},
    h("label", {title: text},
      h("input", {type: "checkbox", checked: ticked.has(key), onchange: e => toggleKey(key, e.target.checked)}),
      h("span", null, text)),
    del ? h("button", {class: "del", title: "Delete this combined plot", onclick: del}, "×") : null);
  $("#file-list").replaceChildren(...S.listing.map(f => item("f:" + f, f)));
  if (!S.listing.length) $("#file-list").append(h("li", {class: "hint"}, "No report files or transcripts in this folder yet."));
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
$("#inp-lastn").addEventListener("change", e => {
  S.lastn = Math.max(0, parseInt(e.target.value, 10) || 0);
  e.target.value = S.lastn || "";
  save();
  for (const p of plots.values()) {
    if (per(p.key).lastN == null) p.lastnChanged();
    else p.syncLastn();
  }
});
$("#btn-own-reset").addEventListener("click", () => {
  for (const p of Object.values(S.per)) p.lastN = null;
  save();
  for (const p of plots.values()) p.lastnChanged();
  updateOwnNote();
});

function updateOwnNote() {                  // "2 plots use their own value · reset"
  const own = orderedTicked().filter(k => S.per[k] && S.per[k].lastN != null).length;
  $("#own-note").hidden = !own;
  $("#own-count").textContent = own === 1 ? "1 plot uses its own value" : `${own} plots use their own value`;
}
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
  if (d.epoch !== S.epoch) return false;          // answer for the previous folder
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
  let soon = false;
  try {
    const st = await api("/api/status");
    S.offset = st.server_time - Date.now() / 1000;
    if (S.loaded && st.epoch !== S.epoch) resetClient();      // folder changed (here or in another browser)
    const listed = st.last_read != null && !st.error;         // the folder has been read at least once
    if (!S.loaded && st.last_read == null && !st.error) {    // server is still reading the new folder
      setConn("live", "reading folder…");
      soon = true;
      return;
    }
    const listingChanged = JSON.stringify(st.files.map(f => f.name)) !== JSON.stringify(S.listing)
                        || JSON.stringify(st.groups) !== JSON.stringify(S.groups);
    S.listing = st.files.map(f => f.name);
    S.info = Object.fromEntries(st.files.map(f => [f.name, f]));
    S.groups = st.groups || {};
    if (!S.loaded) {
      S.folder = st.folder;
      S.epoch = st.epoch;
      const caseName = st.folder.split(/[\\/]/).filter(Boolean).pop() || st.folder;
      $("#folder").textContent = st.folder;
      $("#folder").title = st.folder + " – click to change";
      $("#case-name").textContent = caseName;
      $("#case-name").title = st.folder;
      document.title = "Fluent monitor – " + caseName;
      iconState = undefined;
      restore();
      $("#sel-cols").value = S.cols;
      $("#sel-refresh").value = String(S.refresh);
      $("#inp-lastn").value = S.lastn || "";
      S.loaded = S.everLoaded = true;
      render();
    }
    renderSolver(st.solver || {state: "none"});
    if (S.loaded && !S.seenTrn && S.listing.includes(RESIDUALS)) {   // residuals appeared: show them once
      S.seenTrn = true;
      if (!S.ticked.includes("f:" + RESIDUALS)) S.ticked.unshift("f:" + RESIDUALS);
      renderSide();
      render();
    }
    if (S.autoTick && listed && S.listing.length) {
      S.ticked = S.listing.map(f => "f:" + f);
      S.autoTick = false;
      render();
    }
    if (listingChanged || !plots.size) {
      if (listed) S.ticked = S.ticked.filter(keyExists);   // keep the selection while the folder is unreachable
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
    if (st.error) setConn("stale", "report folder not reachable (VPN down?)");
    else if (newest && age > STALE_AFTER) setConn("stale", `no new data for ${ago(age)}`);
    else setConn("live", "live");
  } catch (e) {
    if (e.denied) setConn("down", "access denied – open the link with ?token= again");
    else setConn("down", "server not reachable");
  } finally {
    polling = false;
    if (pollAgain) { pollAgain = false; poll(true); }
    else pollTimer = setTimeout(poll, soon ? 400 : S.refresh * 1000);
  }
}

// ------------------------------------------------------------------ solver health (transcript)
let solver = {state: "none"};
const HEALTH_TEXT = {running: "Solver running", converged: "Converged", idle: "Solver idle", closed: "Fluent closed",
                     warning: "Warning", critical: "Problem"};

function plural(n, w) { return `${n} ${w}${n === 1 ? "" : "s"}`; }

function iterInfo(g) {
  if (g.last_iter == null) return "before the first iteration";
  let t = `iteration ${g.last_iter}`;
  if (g.ago != null) t += g.ago <= 2 ? " · ongoing" : ` · ${plural(g.ago, "iteration")} ago`;
  if (g.count > 1 && g.first_iter != null && g.first_iter !== g.last_iter) t += ` · first at ${g.first_iter}`;
  return t;
}

function renderSolver(sv) {
  solver = sv;
  const chip = $("#health");
  chip.hidden = sv.state === "none";
  if (chip.hidden) { $("#banner").hidden = true; setIcon("ok"); return; }
  const fresh = (sv.current || []).filter(g => g.new && (g.sev === "critical" || g.sev === "warning"));
  const top = fresh[0];
  let text = HEALTH_TEXT[sv.state] || sv.state;
  if (sv.state === "running" && sv.iteration != null) text += ` · it ${sv.iteration}`;
  if (sv.state === "idle" && sv.age != null) text += ` ${ago(sv.age)}`;
  if ((sv.state === "critical" || sv.state === "warning") && top) {
    text = top.title + (fresh.length > 1 ? ` +${fresh.length - 1}` : "");
  }
  chip.className = "health " + sv.state;
  $("#health-text").textContent = text;
  chip.title = text + " – click for the solver messages";
  // banner
  const banner = $("#banner");
  if (top) {
    banner.className = "banner " + sv.state;
    const parts = fresh.slice(0, 3).map(g => `${g.title}${g.amount ? ` (${g.amount})` : ""}${g.count > 1 ? ` ×${g.count}` : ""}`);
    $("#banner-text").textContent = (sv.state === "critical" ? "⛔ " : "⚠ ") + parts.join(" · ")
      + (top.last_iter != null ? ` — last at iteration ${top.last_iter}` : "") + (fresh.length > 3 ? ` · +${fresh.length - 3} more` : "");
    banner.hidden = false;
  } else banner.hidden = true;
  setIcon(sv.state);
  if ($("#dlg-alerts").open) renderAlerts();
}

let iconState = null;
function setIcon(state) {
  const color = state === "critical" ? "%23c62828" : state === "warning" ? "%23e69500" : null;
  if (iconState === color) return;
  iconState = color;
  const line = "%3Cpath d='M1 13 L5 6 L8 9 L15 2' fill='none' stroke='%231f77b4' stroke-width='2'/%3E";
  const dot = color ? `%3Ccircle cx='12' cy='12' r='4' fill='${color}'/%3E` : "";
  $("#favicon").href = `data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E${line}${dot}%3C/svg%3E`;
  const base = document.title.replace(/^(⛔|⚠) /, "");
  document.title = (state === "critical" ? "⛔ " : state === "warning" ? "⚠ " : "") + base;
}

function alertItem(g) {
  const tags = [];
  if (g.new && (g.sev === "critical" || g.sev === "warning")) tags.push(h("span", {class: "tag on"}, "new"));
  else if (g.ago != null && g.ago <= 2) tags.push(h("span", {class: "tag"}, "ongoing"));
  return h("li", null,
    h("span", {class: "sev " + g.sev}),
    h("span", {class: "t"}, g.title, ...tags),
    h("span", {class: "cnt"}, g.count > 1 ? `×${g.count}` : ""),
    h("span", {class: "meta"}, (g.amount ? `last: ${g.amount} · ` : "") + iterInfo(g)),
    h("span", {class: "line"}, g.last_text));
}

function renderAlerts() {
  const sv = solver;
  const cur = sv.current || [];
  $("#alerts-sub").textContent = sv.transcript
    ? `${sv.transcript}${sv.iteration != null ? ` · iteration ${sv.iteration}` : ""} · ${HEALTH_TEXT[sv.state] || sv.state}`
    : "No transcript found";
  $("#alert-list").replaceChildren(...(cur.length ? cur.map(alertItem)
    : [h("li", {class: "empty"}, "No messages in the current Fluent session.")]));
  const old = sv.earlier || [];
  $("#alerts-earlier").hidden = !old.length;
  $("#alert-earlier").replaceChildren(...old.map(alertItem));
  $("#btn-alerts-ack").disabled = !cur.some(g => g.new);
}

function openAlerts() {
  closeSide();
  renderAlerts();
  $("#dlg-alerts").returnValue = "";
  if (typeof $("#dlg-alerts").showModal === "function") $("#dlg-alerts").showModal();
}

async function acknowledge() {
  try { await api("/api/alerts/ack", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"}); }
  catch (e) { alert("Could not mark as seen: " + e.message); return; }
  poll(true);
}

$("#health").addEventListener("click", openAlerts);
$("#btn-banner-details").addEventListener("click", openAlerts);
$("#btn-banner-ack").addEventListener("click", acknowledge);
$("#dlg-alerts").addEventListener("close", () => { if ($("#dlg-alerts").returnValue === "ack") acknowledge(); });
$("#dlg-alerts").addEventListener("click", e => { if (e.target === $("#dlg-alerts")) $("#dlg-alerts").close("close"); });

// ------------------------------------------------------------------ case folder dialog
const dlg = $("#dlg-folder");
let browsePath = "", browseRoot = "", browseCurrent = null;

function rootLabel(root) {
  const parts = root.split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : root;
}

function renderCrumbs(path) {
  const parts = path ? path.split("/") : [];
  const items = [parts.length ? h("button", {type: "button", title: browseRoot, onclick: () => browse("")}, rootLabel(browseRoot))
                              : h("span", {class: "here", title: browseRoot}, rootLabel(browseRoot))];
  parts.forEach((p, i) => {
    items.push(h("span", {class: "sep"}, "/"));
    const sub = parts.slice(0, i + 1).join("/");
    items.push(i === parts.length - 1 ? h("span", {class: "here"}, p)
                                      : h("button", {type: "button", onclick: () => browse(sub)}, p));
  });
  $("#crumbs").replaceChildren(...items);
}

function folderIcon() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 16 16"); svg.setAttribute("width", "16"); svg.setAttribute("height", "16");
  svg.setAttribute("class", "ico");
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
  p.setAttribute("d", "M1.5 3.5h4.5l1.5 1.5h7v8.5h-13z");
  p.setAttribute("fill", "currentColor");
  svg.append(p);
  return svg;
}

const dirButton = (label, title, onclick, extra) => h("li", null,
  h("button", {type: "button", title, onclick}, folderIcon(), h("span", {class: "nm"}, label), extra || null));

async function browse(path) {
  const list = $("#dir-list"), info = $("#dir-info"), use = $("#btn-use");
  list.replaceChildren(h("li", {class: "empty"}, "loading…"));
  use.disabled = true;
  info.textContent = "";
  info.className = "dir-info";
  let r;
  try {
    r = await api("/api/dirs" + (path == null ? "" : "?path=" + encodeURIComponent(path)));
  } catch (e) {
    if (path != null) { browsePath = path; renderCrumbs(path); }
    list.replaceChildren(h("li", {class: "err"}, e.message));
    return;
  }
  browseRoot = r.root;
  browsePath = r.path || "";
  browseCurrent = r.current;
  renderCrumbs(browsePath);
  const join = n => (browsePath ? browsePath + "/" + n : n);
  const rows = [];
  if (browsePath) {
    const up = browsePath.split("/").slice(0, -1).join("/");
    rows.push(dirButton("..", "Up one level", () => browse(up)));
  }
  for (const d of r.dirs) {
    const here = r.current != null && (r.current === join(d) || r.current.startsWith(join(d) + "/"));
    rows.push(dirButton(d, d, () => browse(join(d)),
                        here ? h("span", {class: "cur"}, r.current === join(d) ? "watching" : "▸ watching inside") : null));
  }
  if (!r.dirs.length) rows.push(h("li", {class: "empty"}, "No subfolders."));
  list.replaceChildren(...rows);
  const recent = r.recent.filter(x => x !== browsePath).slice(0, 6);
  $("#recent-box").hidden = !recent.length;
  $("#recent-list").replaceChildren(...recent.map(x =>
    dirButton(x || rootLabel(browseRoot), x, () => browse(x), x === r.current ? h("span", {class: "cur"}, "watching") : null)));
  if (browsePath === r.current) {
    info.textContent = `Watching this folder (${r.reports} report file${r.reports === 1 ? "" : "s"})`;
    use.disabled = true;
  } else {
    info.textContent = r.reports ? `${r.reports} report file${r.reports === 1 ? "" : "s"} here` : "No report files here (yet)";
    info.classList.toggle("has", r.reports > 0);
    use.disabled = false;
  }
}

function openFolderDialog() {
  closeSide();
  if (typeof dlg.showModal !== "function") { alert("This browser cannot show the folder dialog."); return; }
  dlg.returnValue = "";
  dlg.showModal();
  browse(null);
}

dlg.addEventListener("close", async () => {
  if (dlg.returnValue !== "use") return;
  try {
    await api("/api/folder", {method: "POST", headers: {"Content-Type": "application/json"},
                              body: JSON.stringify({path: browsePath})});
  } catch (e) { alert("Could not switch the folder: " + e.message); return; }
  resetClient();
  setConn("live", "reading folder…");
  poll(true);
});
dlg.addEventListener("click", e => { if (e.target === dlg) dlg.close("cancel"); });   // click on the backdrop
$("#folder").addEventListener("click", openFolderDialog);
$("#btn-folder").addEventListener("click", openFolderDialog);
$("#btn-ph-folder").addEventListener("click", openFolderDialog);

document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(true); });
setInterval(() => { for (const p of plots.values()) if (p.el.isConnected) p.updateStatus(); }, 1000);

if (coarse) document.body.classList.add("touch");
poll(true);
})();
