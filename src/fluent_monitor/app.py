#!/usr/bin/env python3
"""
fluent-monitor - live plots of Ansys Fluent report files (.out), no Fluent GUI needed.

Usage (from the project folder, see README.md)
-----
    uv run fluent-monitor                         # last folder used, otherwise asks
    uv run fluent-monitor "C:/path/to/case"       # watch that folder
    uv run fluent-monitor "C:/path/to/case" --open "mach*" "vel*" --view grid

One window. Left: the report files in the folder (new ones appear automatically) and your
combined plots; tick one to show its plot. Right: the plots, as tabs or as a grid.

Each plot shows one curve per quantity in the file(s).
  - click a legend entry to hide/show that curve
  - hover            circle + value of the nearest point
  - mouse wheel      zoom around the mouse (Shift: y axis only, Ctrl: x axis only)
  - left drag        pan;  right drag: stretch/squeeze the axes;  right click: menu (export, ...)
  - "Follow"         keep rescaling to the data; zooming/panning switches it off
  - Reset view / double-click (tabs view): back to the full view, Follow on
  - "Log y"          logarithmic y axis;  "Last N": only the last N iterations (0 = all)
  - grid view        double-click a plot to open it in the tabs view
  - Combine…         tick several files and combine them into one plot (saved per folder)

Built on pyqtgraph + Qt (PySide6), which are made for fast, live-updating plots.
Dependencies are declared in pyproject.toml and installed by uv into the project's .venv.
"""

import argparse
import fnmatch
import math
import os
import re
import sys
import threading
import time
from functools import partial

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

from .core import (GROUP_PREFIX, DebugLog, FileReader, ReportFile, is_counter, is_group,  # noqa: F401
                   load_groups, load_last_folder, save_groups, save_last_folder, short_label, short_name)

Qt = QtCore.Qt

# Rendering settings, chosen by measurement (grid view, 10 plots, 150 % scaling):
# 1 px pens are ~2x cheaper to paint than wider ones, and grid lines only at the major ticks
# avoid most of pyqtgraph's (semi-transparent, many-level) grid painting.
PEN_WIDTH = 1.0
pg.setConfigOptions(background="w", foreground="#303030", antialias=True)

PALETTE = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
           "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]
LINESTYLES = [Qt.PenStyle.SolidLine, Qt.PenStyle.DashLine, Qt.PenStyle.DotLine, Qt.PenStyle.DashDotLine]
HOVER_TOL = 10             # pixels: how close the mouse must be to a curve for the readout


# --------------------------------------------------------------------------- data
class Poller(QtCore.QObject):
    """Background thread that lists the folder and reads the report files. Results are sent to
    the GUI thread with the `results` signal, so file access (which Windows' virus scanner or
    cloud sync can make slow) never freezes the window."""

    results = QtCore.Signal(object)
    SCAN_EVERY = 5.0      # seconds between folder listings

    def __init__(self, interval, log=None):
        super().__init__()
        self.interval = interval
        self.log = log
        self._lock = threading.Lock()
        self._folder, self._pattern, self._paths = "", "*.out", set()
        self._readers = {}
        self._listing = None
        self._last_scan = 0.0
        self._wake = threading.Event()
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="report-poller", daemon=True)
        self._thread.start()

    def configure(self, folder=None, pattern=None, paths=None, reset=False):
        with self._lock:
            if folder is not None and folder != self._folder:
                self._folder, self._listing, self._last_scan = folder, None, 0.0
                reset = True
            if pattern is not None:
                self._pattern = pattern
            if paths is not None:
                self._paths = set(paths)
            if reset:
                self._readers = {}
        self._wake.set()

    def poke(self):
        self._wake.set()

    def stop(self):
        self._stop = True
        self._wake.set()

    def _run(self):
        while not self._stop:
            self._wake.wait(self.interval)
            self._wake.clear()
            if self._stop:
                break
            with self._lock:
                folder, pattern, paths, readers = self._folder, self._pattern, set(self._paths), self._readers
            out = {"files": None, "data": {}}
            now = time.time()
            if folder and now - self._last_scan >= self.SCAN_EVERY:
                self._last_scan = now
                t0 = time.perf_counter()
                try:
                    listing = sorted(f for f in os.listdir(folder) if fnmatch.fnmatch(f.lower(), pattern.lower()))
                except OSError:
                    listing = []
                if self.log:
                    self.log.slow("folder listing", t0, 0.05)
                if listing != self._listing:
                    self._listing = listing
                    out["files"] = listing
            for path in paths:
                r = readers.get(path)
                if r is None:
                    r = readers[path] = FileReader(path)
                t0 = time.perf_counter()
                ev = r.read()
                if self.log:
                    self.log.slow(f"reading {os.path.basename(path)}", t0, 0.05)
                if ev:
                    out["data"][path] = ev
            if out["files"] is not None or out["data"]:
                self.results.emit(out)


# --------------------------------------------------------------------------- per-plot view settings
class ViewState:
    """Settings that survive switching between tabs and grid."""

    def __init__(self):
        self.hidden = set()   # "<file path>|<column>" of curves hidden via the legend
        self.logy = False
        self.follow = True
        self.lastn = "0"
        self.xcol = ""


# --------------------------------------------------------------------------- plot building blocks
class ZoomViewBox(pg.ViewBox):
    """Mouse wheel zooms around the mouse; Shift = y axis only, Ctrl = x axis only."""

    def wheelEvent(self, ev, axis=None):
        if axis is None:
            mods = ev.modifiers()
            ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
            shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
            if ctrl and not shift:
                axis = 0
            elif shift and not ctrl:
                axis = 1
        super().wheelEvent(ev, axis)


class Legend(pg.LegendItem):
    """Legend in its own layout column; clicking an entry's text (or sample) toggles the curve."""

    def mouseDragEvent(self, ev):     # it sits in a layout: don't let it be dragged around
        ev.ignore()

    def mouseClickEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        for sample, label in self.items:
            if label.sceneBoundingRect().contains(ev.scenePos()):
                sample.item.setVisible(not sample.item.isVisible())
                sample.update()
                ev.accept()
                return


# --------------------------------------------------------------------------- one plot
class PlotPanel(QtWidgets.QFrame):
    """One plot: the curves of one report file, or of several files (a combined plot)."""

    def __init__(self, app, key, title, sources, state, compact):
        super().__init__()
        self.app = app
        self.key = key
        self.title = title
        self.sources = sources            # list of ReportFile
        self.combined = is_group(key)
        self.st = state
        self.compact = compact
        self.dirty = True
        self.layout_seen = None
        self.curves = {}                  # (source index, column index) -> PlotDataItem
        self.meta = {}                    # same key -> (ReportFile, column, x column, hide-key, label)
        self.shown = {}                   # same key -> (x, y) arrays currently plotted
        self.xidx = {}                    # source index -> x column index
        self.hover_pos = None             # scene position of the mouse while hovering
        if compact:
            self.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(2)

        # controls
        bar = QtWidgets.QHBoxLayout()
        bar.setSpacing(8)
        if compact:
            t = QtWidgets.QLabel(title)
            f = t.font()
            f.setBold(True)
            t.setFont(f)
            bar.addWidget(t)
        self.cb_log = QtWidgets.QCheckBox("Log y")
        self.cb_log.setChecked(state.logy)
        self.cb_log.toggled.connect(self.on_log)
        self.cb_follow = QtWidgets.QCheckBox("Follow")
        self.cb_follow.setChecked(state.follow)
        self.cb_follow.toggled.connect(self.on_follow_toggled)
        self.ed_lastn = QtWidgets.QLineEdit(state.lastn)
        self.ed_lastn.setValidator(QtGui.QIntValidator(0, 10**9, self))
        self.ed_lastn.setMaximumWidth(self.fontMetrics().horizontalAdvance("0000000") + 12)
        self.ed_lastn.editingFinished.connect(self.on_lastn)
        self.ed_lastn.setToolTip("Only show the last N iterations (0 = all)")
        bar.addWidget(self.cb_log)
        bar.addWidget(self.cb_follow)
        bar.addWidget(QtWidgets.QLabel("Last N:"))
        bar.addWidget(self.ed_lastn)
        self.cmb_x = QtWidgets.QComboBox()
        self.cmb_x.currentTextChanged.connect(self.on_xcol)
        if not compact:
            bar.addSpacing(6)
            bar.addWidget(QtWidgets.QLabel("X axis:"))
            bar.addWidget(self.cmb_x)
            bar.addSpacing(6)
            for text, slot, tip in (
                    ("Show all curves", self.show_all, "Show curves hidden via the legend"),
                    ("Reset view", lambda: self.set_follow(True), "Full view, follow the data (or double-click the plot)"),
                    ("Save image…", self.save_image, "Save this plot as PNG or SVG")):
                b = QtWidgets.QPushButton(text)
                b.setToolTip(tip)
                b.clicked.connect(slot)
                bar.addWidget(b)
        bar.addStretch(1)
        lay.addLayout(bar)

        # plot + legend (legend in its own column so it never covers the curves)
        self.glw = pg.GraphicsLayoutWidget()
        self.glw.ci.setContentsMargins(2, 2, 2, 2)
        self.vb = ZoomViewBox()
        self.plot = self.glw.addPlot(row=0, col=0, viewBox=self.vb)
        self.legend = Legend(labelTextSize="8pt" if compact else "9pt", verSpacing=-6)
        self.glw.addItem(self.legend, row=0, col=1)
        # natural height, top-aligned (otherwise the entries spread over the whole plot height)
        self.legend.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Fixed)
        self.glw.ci.layout.setAlignment(self.legend, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.glw.ci.layout.setColumnStretchFactor(0, 1)
        self.plot.showGrid(x=True, y=True, alpha=0.18)
        self.plot.hideButtons()
        self.plot.setClipToView(True)
        self.plot.setDownsampling(auto=True, mode="peak")
        for name in ("left", "bottom"):
            ax = self.plot.getAxis(name)
            ax.enableAutoSIPrefix(False)     # plain numbers, no "(x1e+03)" scaling
            ax.setStyle(maxTickLevel=0)      # ticks + grid lines only at major ticks (much cheaper)
            if compact:
                f = QtGui.QFont(self.font())
                f.setPointSizeF(max(6.5, f.pointSizeF() - 1.5))
                ax.setStyle(tickFont=f)
        lay.addWidget(self.glw, 1)
        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet("color: #666;")
        if compact:
            f = self.status.font()
            f.setPointSizeF(max(6.5, f.pointSizeF() - 1))
            self.status.setFont(f)
        lay.addWidget(self.status)

        self.vb.sigRangeChangedManually.connect(self.on_manual_range)
        self.glw.scene().sigMouseMoved.connect(self.on_mouse_moved)
        self.glw.scene().sigMouseClicked.connect(self.on_click)
        self.glw.installEventFilter(self)
        self.rebuild()

    # ---- layout
    def _layout_key(self):
        return tuple(s.layout_version for s in self.sources)

    def has_new_layout(self):
        return self._layout_key() != self.layout_seen

    def rebuild(self):
        srcs = self.sources
        self.layout_seen = self._layout_key()
        # x axis choices: counters present in every file (normally just "Iteration")
        per_file = [[c for c in s.columns if is_counter(c)] or s.columns[:1] for s in srcs if s.columns]
        counters = [c for c in per_file[0] if all(c in other for other in per_file[1:])] if per_file else []
        if per_file and not counters:
            counters = per_file[0][:1]
        if self.st.xcol not in counters:
            self.st.xcol = counters[0] if counters else ""
        self.cmb_x.blockSignals(True)
        self.cmb_x.clear()
        self.cmb_x.addItems(counters)
        self.cmb_x.setCurrentText(self.st.xcol)
        self.cmb_x.blockSignals(False)
        xname = self.st.xcol

        self.plot.clear()
        self.legend.clear()
        self.curves.clear()
        self.meta.clear()
        self.shown.clear()
        self.xidx.clear()
        k = 0
        for si, s in enumerate(srcs):
            cols = s.columns
            if not cols:
                continue
            xi = cols.index(xname) if xname in cols else 0
            self.xidx[si] = xi
            for i, name in enumerate(cols):
                if i == xi or is_counter(name):
                    continue
                # combined plots use the full Fluent name ("mach(point-030)") so curves stay unique
                label = name if self.combined else short_label(name)
                pen = pg.mkPen(PALETTE[k % 10], width=PEN_WIDTH,
                               style=LINESTYLES[(k // 10) % len(LINESTYLES)])
                k += 1
                c = pg.PlotDataItem(pen=pen)
                hide_key = f"{s.path}|{name}"
                self.plot.addItem(c)
                self.legend.addItem(c, label)
                c.setVisible(hide_key not in self.st.hidden)
                c.visibleChanged.connect(partial(self.on_curve_visibility, (si, i)))
                self.curves[(si, i)] = c
                self.meta[(si, i)] = (s, i, xi, hide_key, label)
        if len(self.curves) < 2:                  # single curve: the title already names it
            self.legend.hide()
        else:
            self.legend.show()
        for key in self.curves:
            self._style_legend_label(key)
        self.plot.setLabel("bottom", xname or "Iteration")
        if not self.compact:
            self.plot.setTitle(self.title)
            only = next(iter(self.meta.values()))[4] if len(self.meta) == 1 else None
            self.plot.setLabel("left", only or "")
        # hover marker + value box (not part of the auto-range)
        self.marker = pg.ScatterPlotItem(size=11, brush=None, pen=pg.mkPen("k", width=2))
        self.marker.setZValue(1000)
        self.tip = pg.TextItem("", color="#202020", fill=pg.mkBrush(255, 255, 255, 235),
                               border=pg.mkPen("#808080"), anchor=(0, 1))
        self.tip.setZValue(1001)
        self.plot.addItem(self.marker, ignoreBounds=True)
        self.plot.addItem(self.tip, ignoreBounds=True)
        self.hide_hover()
        self.plot.setLogMode(y=self.st.logy)
        if self.st.follow:
            self.vb.enableAutoRange()
        self.update_data()

    def _style_legend_label(self, key):
        for sample, label in self.legend.items:
            if sample.item is self.curves.get(key):
                text = self.meta[key][4]
                label.setText(text, color="#303030" if sample.item.isVisible() else "#b0b0b0")
                sample.update()

    # ---- user actions
    def on_curve_visibility(self, key):
        c = self.curves.get(key)
        if c is None:
            return
        hide_key = self.meta[key][3]
        (self.st.hidden.discard if c.isVisible() else self.st.hidden.add)(hide_key)
        self._style_legend_label(key)
        if self.st.follow:
            self.vb.enableAutoRange()      # re-fit to the visible curves
        self._refresh_hover()

    def show_all(self):
        for c in self.curves.values():
            c.setVisible(True)

    def on_log(self, on):
        self.st.logy = on
        self.plot.setLogMode(y=on)
        if self.st.follow:
            self.vb.enableAutoRange()
        self._refresh_hover()

    def on_lastn(self):
        if self.ed_lastn.text() != self.st.lastn:
            self.st.lastn = self.ed_lastn.text() or "0"
            self.update_data()

    def on_xcol(self, text):
        if text and text != self.st.xcol:
            self.st.xcol = text
            self.rebuild()

    def on_follow_toggled(self, on):
        self.st.follow = on
        if on:
            self.vb.enableAutoRange()
        else:
            self.vb.disableAutoRange()

    def set_follow(self, on):
        if self.cb_follow.isChecked() == on:
            self.on_follow_toggled(on)      # e.g. "Reset view" while already following
        else:
            self.cb_follow.setChecked(on)

    def on_manual_range(self, _mask):
        # zoom / pan by the user: stop following so new data doesn't undo it
        self.vb.disableAutoRange()
        if self.cb_follow.isChecked():
            self.cb_follow.blockSignals(True)
            self.cb_follow.setChecked(False)
            self.cb_follow.blockSignals(False)
            self.st.follow = False

    def on_click(self, ev):
        if not ev.double() or ev.button() != Qt.MouseButton.LeftButton:
            return
        if not self.vb.sceneBoundingRect().contains(ev.scenePos()):
            return
        if self.compact:
            QtCore.QTimer.singleShot(0, lambda: self.app.focus_in_tabs(self.key))   # grid -> tab
        else:
            self.set_follow(True)                                                   # tabs -> reset

    def save_image(self):
        """Save the plot (with legend) as it looks on screen: PNG (2x), or vector PDF / SVG."""
        name = re.sub(r'[\\/:*?"<>|]+', "_", self.title)
        path, flt = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save plot", os.path.join(self.app.folder or os.getcwd(), name + ".png"),
            "PNG image (*.png);;PDF vector graphics (*.pdf);;SVG vector graphics (*.svg)")
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        if ext not in (".png", ".pdf", ".svg"):
            ext = ".pdf" if "pdf" in flt.lower() else ".svg" if "svg" in flt.lower() else ".png"
            path += ext
        self.hide_hover()
        save_widget_image(self.glw, path)

    # ---- hover readout
    def eventFilter(self, obj, ev):
        if obj is self.glw and ev.type() == QtCore.QEvent.Type.Leave:
            self.hide_hover()
        return False

    def on_mouse_moved(self, pos):
        if (QtWidgets.QApplication.mouseButtons() != Qt.MouseButton.NoButton
                or not self.vb.sceneBoundingRect().contains(pos)):
            self.hide_hover()
            return
        self.hover_pos = pos
        self._find_hover()

    def hide_hover(self):
        self.hover_pos = None
        if hasattr(self, "marker"):
            self.marker.hide()
            self.tip.hide()

    def _refresh_hover(self):
        if self.hover_pos is not None:
            self._find_hover()

    def _find_hover(self):
        """Nearest data point of the visible curve closest to the mouse (within HOVER_TOL px)."""
        (x0, x1), (y0, y1) = self.vb.viewRange()
        rect = self.vb.sceneBoundingRect()
        if x1 == x0 or y1 == y0 or rect.width() <= 0:
            return
        sx, sy = rect.width() / (x1 - x0), rect.height() / (y1 - y0)
        mp = self.vb.mapSceneToView(self.hover_pos)
        mx, my = mp.x(), mp.y()
        logy = self.st.logy
        best = None                                   # (distance px, key, index, y in view coords)
        for key, c in self.curves.items():
            if not c.isVisible() or key not in self.shown:
                continue
            X, Y = self.shown[key]
            n = len(X)
            if not n:
                continue
            lo = max(int(np.searchsorted(X, mx - HOVER_TOL / sx, "left")) - 1, 0)
            hi = min(int(np.searchsorted(X, mx + HOVER_TOL / sx, "right")) + 1, n)
            if hi <= lo:
                continue
            xs = X[lo:hi]
            with np.errstate(all="ignore"):
                ys = np.log10(np.where(Y[lo:hi] > 0, Y[lo:hi], np.nan)) if logy else Y[lo:hi]
                px, py = (xs - mx) * sx, (ys - my) * sy
                d = np.hypot(px, py)
            d[~np.isfinite(d)] = np.inf
            j = int(np.argmin(d))
            dist = d[j]
            # also accept the mouse being on the line *between* two samples (zoomed in)
            if len(xs) > 1 and dist > HOVER_TOL:
                m = int(np.searchsorted(xs, mx))
                if 0 < m < len(xs) and np.isfinite(py[m - 1]) and np.isfinite(py[m]) and xs[m] != xs[m - 1]:
                    t = (mx - xs[m - 1]) / (xs[m] - xs[m - 1])
                    dist = min(dist, abs(py[m - 1] + t * (py[m] - py[m - 1])))
            if dist <= HOVER_TOL and (best is None or dist < best[0]):
                best = (dist, key, lo + j, ys[j])
        if best is None:
            self.marker.hide()
            self.tip.hide()
            return
        _, key, j, yv = best
        X, Y = self.shown[key]
        x, y = X[j], Y[j]
        color = self.curves[key].opts["pen"].color()
        self.marker.setData([x], [yv], pen=pg.mkPen(color, width=2))
        name = self.meta[key][4] if len(self.curves) > 1 else self.title
        xs = f"{x:.0f}" if float(x).is_integer() else f"{x:.6g}"
        self.tip.setText(f"{name}\n{self.st.xcol or 'x'} {xs}\n{y:.12g}")
        self.tip.border = pg.mkPen(color, width=1.5)
        self.marker.show()
        self.tip.show()
        # pyqtgraph skips this while the item is hidden: without it the box is stretched by the axes
        self.tip.updateTransform(force=True)
        # box on the side of the point that faces the middle of the plot
        right = x > (x0 + x1) / 2
        top = yv > (y0 + y1) / 2
        self.tip.setAnchor((1.06 if right else -0.06, -0.12 if top else 1.12))
        self.tip.setPos(x, yv)

    # ---- data
    def refresh(self, changed):
        """Called after the files were polled."""
        if self.has_new_layout():
            self.rebuild()
        elif changed or self.dirty:
            self.update_data()
        else:
            self.update_status()

    def update_data(self):
        self.dirty = False
        try:
            n = max(0, int(self.st.lastn or 0))
        except ValueError:
            n = 0
        sl = slice(-n, None) if n else slice(None)
        for key, c in self.curves.items():
            s, i, xi, _, _ = self.meta[key]
            if not s.n:
                c.setData([], [])
                self.shown.pop(key, None)
                continue
            x, y = s.col(xi)[sl].copy(), s.col(i)[sl].copy()
            self.shown[key] = (x, y)
            c.setData(x, y)
        self.update_status()
        self._refresh_hover()

    def update_status(self):
        live = [(si, s) for si, s in enumerate(self.sources) if s.n and si in self.xidx]
        if not live:
            self.status.setText("waiting for data…")
            return
        last_x = max(s.col(self.xidx[si])[-1] for si, s in live)
        changes = [s.last_change for _, s in live if s.last_change]
        age = time.time() - max(changes) if changes else 0
        age_txt = f"{age:.0f} s ago" if age < 120 else f"{age / 60:.0f} min ago"
        extra = f"{len(self.sources)} files" if self.combined else f"{live[0][1].n} points"
        self.status.setText(f"{self.st.xcol} {last_x:g}  ·  {extra}  ·  updated {age_txt}")


def save_widget_image(view, path):
    """Render a pyqtgraph view (QGraphicsView) into PNG (2x resolution), PDF or SVG (vector).
    QGraphicsView.render() scales the scene into the target rectangle."""
    src = view.viewport().rect()
    w, h = src.width(), src.height()
    ext = os.path.splitext(path)[1].lower()
    if ext == ".png":
        img = QtGui.QImage(w * 2, h * 2, QtGui.QImage.Format.Format_ARGB32)
        img.setDevicePixelRatio(2)          # lay out at normal size, rasterise at 2x (sharp text too)
        img.fill(Qt.GlobalColor.white)
        painter = QtGui.QPainter(img)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
        view.render(painter, QtCore.QRectF(0, 0, w, h), src)
        painter.end()
        img.save(path)
    elif ext == ".pdf":
        writer = QtGui.QPdfWriter(path)
        writer.setResolution(96)
        writer.setPageSize(QtGui.QPageSize(QtCore.QSizeF(w * 0.75, h * 0.75), QtGui.QPageSize.Unit.Point))
        writer.setPageMargins(QtCore.QMarginsF(0, 0, 0, 0))
        painter = QtGui.QPainter(writer)
        view.render(painter, QtCore.QRectF(0, 0, writer.width(), writer.height()), src)
        painter.end()
    else:
        from PySide6.QtSvg import QSvgGenerator
        gen = QSvgGenerator()
        gen.setFileName(path)
        gen.setSize(QtCore.QSize(w, h))
        gen.setViewBox(QtCore.QRect(0, 0, w, h))
        gen.setTitle(os.path.splitext(os.path.basename(path))[0])
        painter = QtGui.QPainter(gen)
        view.render(painter, QtCore.QRectF(0, 0, w, h), src)
        painter.end()


# --------------------------------------------------------------------------- main window
class App(QtWidgets.QMainWindow):
    def __init__(self, folder, pattern, interval, open_patterns, view, columns, debug_log=None):
        super().__init__()
        self.folder = folder or ""
        self.pattern = pattern
        self.log = DebugLog(debug_log) if debug_log else None
        self.file_list = None          # report file names in the folder (from the poller)
        self._pending_open = list(open_patterns)
        self._update_queue = []        # plots waiting to be updated (one per event-loop turn)
        self._update_scheduled = False
        self.groups = {}       # combined-plot name -> list of file names (in the watched folder)
        self.reports = {}      # file path -> ReportFile
        self.states = {}       # key -> ViewState
        self.panels = {}       # key -> PlotPanel currently shown
        self.ticked = set()    # keys ticked in the lists
        self._updating_lists = False

        self.setWindowTitle("Fluent report monitor")
        scr = QtGui.QGuiApplication.primaryScreen().availableGeometry()
        self.resize(min(1400, int(scr.width() * 0.9)), min(850, int(scr.height() * 0.85)))

        # ---- left sidebar
        side = QtWidgets.QWidget()
        sl = QtWidgets.QVBoxLayout(side)
        sl.setContentsMargins(8, 8, 8, 8)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Folder:"))
        row.addStretch(1)
        b = QtWidgets.QPushButton("Browse…")
        b.clicked.connect(self.browse)
        row.addWidget(b)
        sl.addLayout(row)
        self.lbl_folder = QtWidgets.QLabel("")
        self.lbl_folder.setWordWrap(True)
        self.lbl_folder.setStyleSheet("color: #666;")
        self.lbl_folder.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        sl.addWidget(self.lbl_folder)

        self.lst_files = QtWidgets.QListWidget()
        self.lst_files.itemChanged.connect(self.on_item_changed)
        sl.addWidget(self._bold("Report files"))
        sl.addWidget(self.lst_files, 3)
        self.lbl_groups = self._bold("Combined plots")
        self.lst_groups = QtWidgets.QListWidget()
        self.lst_groups.itemChanged.connect(self.on_item_changed)
        self.lst_groups.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.lst_groups.customContextMenuRequested.connect(self.on_group_menu)
        self.lst_groups.itemSelectionChanged.connect(
            lambda: self.btn_delete.setEnabled(bool(self.lst_groups.selectedItems())))
        self.btn_delete = QtWidgets.QPushButton("Delete combined plot")
        self.btn_delete.setEnabled(False)
        self.btn_delete.clicked.connect(lambda: [self.delete_group(i.data(Qt.ItemDataRole.UserRole)[len(GROUP_PREFIX):])
                                                 for i in self.lst_groups.selectedItems()])
        sl.addWidget(self.lbl_groups)
        sl.addWidget(self.lst_groups, 1)
        sl.addWidget(self.btn_delete)

        row = QtWidgets.QHBoxLayout()
        for text, slot in (("All", lambda: self.set_all(True)), ("None", lambda: self.set_all(False)),
                           ("Combine…", self.combine)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        sl.addLayout(row)

        box = QtWidgets.QGroupBox("View")
        g = QtWidgets.QGridLayout(box)
        self.rb_tabs = QtWidgets.QRadioButton("Tabs")
        self.rb_grid = QtWidgets.QRadioButton("Grid")
        (self.rb_grid if view == "grid" else self.rb_tabs).setChecked(True)
        self.rb_tabs.toggled.connect(lambda on: on and self.render())
        self.rb_grid.toggled.connect(lambda on: on and self.render())
        g.addWidget(self.rb_tabs, 0, 0)
        g.addWidget(self.rb_grid, 0, 1)
        g.addWidget(QtWidgets.QLabel("Grid columns:"), 1, 0)
        self.cmb_cols = QtWidgets.QComboBox()
        self.cmb_cols.addItems(["auto", "1", "2", "3", "4", "5"])
        self.cmb_cols.setCurrentText(str(columns) if columns else "auto")
        self.cmb_cols.currentTextChanged.connect(lambda _t: self.layout_grid())
        g.addWidget(self.cmb_cols, 1, 1)
        g.addWidget(QtWidgets.QLabel("Refresh (s):"), 2, 0)
        self.spin_interval = QtWidgets.QDoubleSpinBox()
        self.spin_interval.setRange(0.2, 60)
        self.spin_interval.setSingleStep(0.5)
        self.spin_interval.setValue(interval)
        self.spin_interval.valueChanged.connect(self.on_interval)
        g.addWidget(self.spin_interval, 2, 1)
        sl.addWidget(box)
        self.info = QtWidgets.QLabel("")
        self.info.setStyleSheet("color: #666;")
        sl.addWidget(self.info)

        # ---- right: plot area (placeholder page / content page)
        self.area = QtWidgets.QStackedWidget()
        ph = QtWidgets.QLabel("Tick report files on the left to plot them.")
        ph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ph.setStyleSheet("color: #888;")
        self.area.addWidget(ph)
        self.content = None     # QTabWidget (tabs) or QWidget with a grid layout
        self.tabs = None

        split = QtWidgets.QSplitter()
        split.addWidget(side)
        split.addWidget(self.area)
        split.setStretchFactor(1, 1)
        split.setSizes([270, 1000])
        self.setCentralWidget(split)

        # all file access happens in the poller thread
        self.poller = Poller(interval, self.log)
        self.poller.results.connect(self.on_results)
        if not self.folder:
            self.folder = QtWidgets.QFileDialog.getExistingDirectory(
                self, "Folder with Fluent report files", os.getcwd()) or ""
        self._set_folder(self.folder)
        self.render()

        # status lines ("updated 3 s ago") tick once a second
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        if self.log:
            self.log.write(f"folder {self.folder!r}, interval {interval} s, log file {self.log.path}")
            self.info.setToolTip(f"Debug log: {self.log.path}")
            self._stall_last = time.perf_counter()
            self._stall_timer = QtCore.QTimer(self)
            self._stall_timer.setInterval(50)
            self._stall_timer.timeout.connect(self._check_stall)
            self._stall_timer.start()

    def _check_stall(self):
        now = time.perf_counter()
        gap = now - self._stall_last
        self._stall_last = now
        if gap > 0.25:
            self.log.write(f"WINDOW BLOCKED for about {gap * 1000:.0f} ms")

    def closeEvent(self, ev):
        self.poller.stop()
        super().closeEvent(ev)

    def on_interval(self, v):
        self.poller.interval = v
        self.poller.poke()

    @staticmethod
    def _bold(text):
        lbl = QtWidgets.QLabel(text)
        f = lbl.font()
        f.setBold(True)
        lbl.setFont(f)
        return lbl

    # ---- folder / lists
    def browse(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Folder with Fluent report files",
                                                       self.folder or os.getcwd())
        if not d:
            return
        self.ticked.clear()
        for d_ in (self.reports, self.states, self.groups):
            d_.clear()
        self.file_list = None
        self.lst_files.clear()
        self.lst_groups.clear()
        self.render()
        self._set_folder(d)

    def _all_keys(self):
        keys = [self.lst_files.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.lst_files.count())]
        return keys + [GROUP_PREFIX + n for n in sorted(self.groups)]

    def _set_folder(self, folder):
        self.folder = folder
        self.lbl_folder.setText(folder or "(no folder)")
        self.info.setText("reading folder…" if folder else "Choose a folder with Browse…")
        if folder:
            save_last_folder(folder)
            self.groups = load_groups(folder)
        self._rebuild_lists()
        self.poller.configure(folder=folder, pattern=self.pattern, paths=set(), reset=True)

    def _rebuild_lists(self):
        self._updating_lists = True
        self.lst_files.clear()
        for f in self.file_list or []:
            it = QtWidgets.QListWidgetItem(f)
            it.setData(Qt.ItemDataRole.UserRole, os.path.join(self.folder, f))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            self.lst_files.addItem(it)
        self.lst_groups.clear()
        for name in sorted(self.groups):
            it = QtWidgets.QListWidgetItem(name)
            it.setData(Qt.ItemDataRole.UserRole, GROUP_PREFIX + name)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setToolTip("Combines:\n" + "\n".join(self.groups[name]) + "\n\nRight-click to delete")
            self.lst_groups.addItem(it)
        has_groups = bool(self.groups)
        for w in (self.lbl_groups, self.lst_groups, self.btn_delete):
            w.setVisible(has_groups)
        self._updating_lists = False
        self._sync_checks()

    def _sync_checks(self):
        self._updating_lists = True
        for lst in (self.lst_files, self.lst_groups):
            for i in range(lst.count()):
                it = lst.item(i)
                it.setCheckState(Qt.CheckState.Checked if it.data(Qt.ItemDataRole.UserRole) in self.ticked
                                 else Qt.CheckState.Unchecked)
        self._updating_lists = False

    def on_item_changed(self, item):
        if self._updating_lists:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        on = item.checkState() == Qt.CheckState.Checked
        if on == (key in self.ticked):
            return
        (self.ticked.add if on else self.ticked.discard)(key)
        self.toggle(key)

    def selected(self):
        """Ticked keys in display order: files first, then combined plots."""
        return [k for k in self._all_keys() if k in self.ticked]

    def set_all(self, state):
        self.ticked = set(self._all_keys()) if state else set()
        self._sync_checks()
        self.render()

    # ---- combined plots
    def combine(self):
        files = [k for k in self.selected() if not is_group(k)]
        if len(files) < 2:
            QtWidgets.QMessageBox.information(self, "Combine files",
                                              "Tick at least two report files to combine them into one plot.")
            return
        default = " + ".join(short_name(f) for f in files)
        name, ok = QtWidgets.QInputDialog.getText(
            self, "Combine files", "Name of the combined plot:\n\n" + "\n".join(os.path.basename(f) for f in files),
            text=default)
        name = name.strip()
        if not ok or not name:
            return
        if name in self.groups and QtWidgets.QMessageBox.question(
                self, "Combine files", f'A combined plot "{name}" exists already. Replace it?') \
                != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        key = GROUP_PREFIX + name
        self.groups[name] = [os.path.basename(f) for f in files]
        save_groups(self.folder, self.groups)
        self.states.pop(key, None)
        for f in files:                      # show the combination instead of the single files
            self.ticked.discard(f)
        self.ticked.add(key)
        self._rebuild_lists()
        self.render()
        self.select_tab(key)

    def on_group_menu(self, pos):
        it = self.lst_groups.itemAt(pos)
        if it is None:
            return
        menu = QtWidgets.QMenu(self)
        act = menu.addAction(f'Delete "{it.text()}"')
        if menu.exec(self.lst_groups.mapToGlobal(pos)) is act:
            self.delete_group(it.text())

    def delete_group(self, name, confirm=True):
        if confirm and QtWidgets.QMessageBox.question(
                self, "Combined plot", f'Delete the combined plot "{name}"?\n(The report files are not touched.)') \
                != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        key = GROUP_PREFIX + name
        was_shown = key in self.ticked
        self.ticked.discard(key)
        self.states.pop(key, None)
        self.groups.pop(name, None)
        save_groups(self.folder, self.groups)
        self._rebuild_lists()
        if was_shown:
            self.toggle(key)

    # ---- plot area
    def report(self, path):
        rf = self.reports.get(path)
        if rf is None:
            rf = self.reports[path] = ReportFile(path)   # filled by the poller
        return rf

    def _sync_poller(self):
        """Tell the poller which files the plots on screen need."""
        paths = {s.path for p in self.panels.values() for s in p.sources}
        self.poller.configure(paths=paths)

    def _panel(self, key, compact):
        if is_group(key):
            name = key[len(GROUP_PREFIX):]
            present = set(self.file_list or [])
            paths = [os.path.join(self.folder, f) for f in self.groups.get(name, []) if f in present]
            title = name
        else:
            paths = [key]
            title = os.path.splitext(os.path.basename(key))[0]
        sources = [self.report(p) for p in paths]
        st = self.states.setdefault(key, ViewState())
        return PlotPanel(self, key, title, sources, st, compact)

    def view(self):
        return "grid" if self.rb_grid.isChecked() else "tabs"

    def render(self):
        """(Re)create the plot area for the current view mode and selection."""
        for p in self.panels.values():
            p.setParent(None)
            p.deleteLater()
        self.panels.clear()
        if self.content is not None:
            self.area.removeWidget(self.content)
            self.content.deleteLater()
        self.tabs = None
        if self.view() == "tabs":
            self.tabs = QtWidgets.QTabWidget()
            self.tabs.setDocumentMode(True)
            self.tabs.setUsesScrollButtons(True)
            self.tabs.tabBar().setElideMode(Qt.TextElideMode.ElideNone)
            self.tabs.currentChanged.connect(self.on_tab_changed)
            self.content = self.tabs
            self.area.addWidget(self.content)
            for key in self.selected():
                self._add_tab(key)
        else:
            self.content = QtWidgets.QWidget()
            QtWidgets.QGridLayout(self.content).setContentsMargins(4, 4, 4, 4)
            self.area.addWidget(self.content)
            for key in self.selected():
                self.panels[key] = self._panel(key, compact=True)
            self.layout_grid()
        self._update_placeholder()
        self._sync_poller()

    def _add_tab(self, key):
        if key in self.panels:
            return
        p = self._panel(key, compact=False)
        self.panels[key] = p
        order = [k for k in self.selected() if k in self.panels]
        self.tabs.insertTab(order.index(key), p, p.title)
        self._update_placeholder()
        self._sync_poller()

    def _remove_tab(self, key):
        p = self.panels.pop(key, None)
        if p is not None:
            idx = self.tabs.indexOf(p)
            if idx >= 0:
                self.tabs.removeTab(idx)
            p.deleteLater()
        self._update_placeholder()
        self._sync_poller()

    def toggle(self, key):
        if self.view() == "tabs" and self.tabs is not None:
            if key in self.ticked:
                self._add_tab(key)
                self.select_tab(key)
            else:
                self._remove_tab(key)
        else:
            self.render()

    def select_tab(self, key):
        if self.tabs is not None and key in self.panels:
            self.tabs.setCurrentWidget(self.panels[key])

    def on_tab_changed(self, _index):
        p = self.tabs.currentWidget() if self.tabs is not None else None
        if isinstance(p, PlotPanel) and (p.dirty or p.has_new_layout()):
            p.refresh(False)       # bring it up to date before it is painted

    def layout_grid(self):
        if self.view() != "grid" or self.content is None:
            return
        g = self.content.layout()
        for i in range(12):
            g.setColumnStretch(i, 0)
            g.setRowStretch(i, 0)
        keys = [k for k in self.selected() if k in self.panels]
        n = len(keys)
        if not n:
            return
        try:
            ncol = int(self.cmb_cols.currentText())
        except ValueError:
            ncol = math.ceil(math.sqrt(n))
        ncol = max(1, min(ncol, n))
        for k, key in enumerate(keys):
            r, c = divmod(k, ncol)
            g.addWidget(self.panels[key], r, c)
        for c in range(ncol):
            g.setColumnStretch(c, 1)
        for r in range(math.ceil(n / ncol)):
            g.setRowStretch(r, 1)

    def _update_placeholder(self):
        self.area.setCurrentIndex(1 if self.panels and self.content is not None else 0)
        if self.panels and self.content is not None:
            self.area.setCurrentWidget(self.content)

    def focus_in_tabs(self, key):
        self.rb_tabs.setChecked(True)      # triggers render()
        if self.tabs is None:
            self.render()
        self.select_tab(key)

    def _visible(self, panel):
        if self.view() == "grid":
            return True
        return self.tabs is not None and self.tabs.currentWidget() is panel

    # ---- data from the poller thread
    def on_results(self, out):
        t0 = time.perf_counter()
        if out["files"] is not None:
            self.file_list = out["files"]
            self._rebuild_lists()
            self.info.setText(f"{len(self.file_list)} file(s) matching {self.pattern}")
            if self._pending_open:                       # --open patterns, once the folder is known
                for key in self._all_keys():
                    name = key[len(GROUP_PREFIX):] if is_group(key) else os.path.basename(key)
                    if any(fnmatch.fnmatch(name, pat) for pat in self._pending_open):
                        self.ticked.add(key)
                self._pending_open = []
                self._sync_checks()
                t1 = time.perf_counter()
                self.render()
                if self.log:
                    self.log.slow(f"building {len(self.panels)} plots", t1, 0.05)
                t0 = time.perf_counter()
        changed = set()
        for path, events in out["data"].items():
            rf = self.reports.get(path)
            if rf is not None and rf.apply(events):
                changed.add(path)
        for p in self.panels.values():
            if any(s.path in changed for s in p.sources):
                p.dirty = True
        if self.log:
            self.log.slow(f"applying new data ({len(changed)} files changed)", t0, 0.05)
        self.schedule_updates()

    def schedule_updates(self):
        for p in self.panels.values():
            if self._visible(p) and (p.dirty or p.has_new_layout()) and p not in self._update_queue:
                self._update_queue.append(p)
        if self._update_queue and not self._update_scheduled:
            self._update_scheduled = True
            QtCore.QTimer.singleShot(0, self._update_next)

    def _update_next(self):
        """Update and paint ONE plot, then give the event loop a turn (mouse, hover, ...)."""
        self._update_scheduled = False
        while self._update_queue:
            p = self._update_queue.pop(0)
            if p in self.panels.values() and self._visible(p):
                t0 = time.perf_counter()
                p.refresh(True)
                p.glw.viewport().repaint()          # paint now, so the cost stays in this step
                if self.log:
                    self.log.slow(f"updating plot {p.title}", t0, 0.1)
                break
        if self._update_queue:
            self._update_scheduled = True
            QtCore.QTimer.singleShot(1, self._update_next)

    def tick(self):
        for p in self.panels.values():
            if self._visible(p) and not p.dirty:
                p.update_status()


# --------------------------------------------------------------------------- entry points
def main():
    ap = argparse.ArgumentParser(description="Live plots of Fluent report files.")
    ap.add_argument("folder", nargs="?", help="folder with the .out report files "
                                             "(default: the last folder used, otherwise ask)")
    ap.add_argument("--pattern", default="*.out", help="file pattern (default: *.out)")
    ap.add_argument("--interval", type=float, default=2.0, help="refresh interval in seconds (default 2)")
    ap.add_argument("--open", nargs="*", default=[], metavar="GLOB",
                    help='plots to show at start, e.g. --open "mach*" "mfr-rfile.out" or --open "*"')
    ap.add_argument("--view", choices=["tabs", "grid"], default="tabs", help="start in tabs or grid view")
    ap.add_argument("--columns", type=int, default=0, help="grid columns (default: automatic)")
    ap.add_argument("--opengl", action="store_true",
                    help="draw the plots with the graphics card (OpenGL); can be faster on some PCs")
    ap.add_argument("--debug", nargs="?", const=os.path.join(os.path.expanduser("~"), "fluent_monitor_debug.log"),
                    metavar="LOGFILE",
                    help="write timings and window freezes to a log file (default ~/fluent_monitor_debug.log)")
    args = ap.parse_args()
    if args.opengl:
        pg.setConfigOptions(useOpenGL=True)
    folder = args.folder
    if folder and os.path.isfile(folder):        # a report file was dropped on the launcher
        folder = os.path.dirname(folder)
    if folder and not os.path.isdir(folder):
        sys.exit(f"Not a folder: {folder}")
    folder = os.path.abspath(folder) if folder else load_last_folder()
    qapp = pg.mkQApp("Fluent report monitor")
    win = App(folder, args.pattern, args.interval, args.open, args.view, args.columns, args.debug)
    win.show()
    return qapp.exec()


def run():
    """Entry point: like main(), but shows errors in a dialog (the GUI launcher has no console)."""
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback
        msg = traceback.format_exc()
        try:
            pg.mkQApp("Fluent report monitor")
            QtWidgets.QMessageBox.critical(None, "Fluent report monitor", msg)
        except Exception:
            print(msg, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    run()
