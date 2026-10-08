#!/usr/bin/env python3
"""
fluent_monitor.py - live plots of Ansys Fluent report files (.out), no Fluent GUI needed.

Usage (from the project folder, see README.md)
-----
    uv run fluent-monitor                         # last folder used, otherwise asks
    uv run fluent-monitor "C:/path/to/case"       # watch that folder
    uv run fluent-monitor "C:/path/to/case" --open "mach*" "vel*" --view grid

One window. Left: the report files in the folder (new ones appear automatically);
tick a file to show its plot. Right: the plots, as tabs or as a grid.

Each plot shows one curve per quantity in the file.
  - click a legend entry to hide/show that curve
  - "Log y"        logarithmic y axis
  - "Follow"       keep rescaling to the data (untick to keep your own zoom)
  - "Last N"       only show the last N iterations (0 = all)
  - mouse wheel    zoom around the mouse (Shift: y axis only, Ctrl: x axis only)
  - hover          circle + value of the nearest point
  - tabs view      scroll over the tab strip to switch tabs; toolbar: zoom, pan, save PNG/PDF/SVG;
                   Home or double-click resets the zoom (switches Follow back on)
  - grid view      double-click a plot to open it in the tabs view

Dependencies (matplotlib, numpy) are declared in pyproject.toml and installed by uv
into the project's own .venv; nothing is installed into your system Python.
"""

import argparse
import bisect
import fnmatch
import math
import os
import re
import sys
import time
import tkinter as tk
from tkinter import filedialog, ttk

import matplotlib

matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
import numpy as np  # noqa: E402

# Plain tick labels: never an offset like "+1.4598e1" at the axis end (sci notation stays allowed).
matplotlib.rcParams["axes.formatter.useoffset"] = False
if sys.platform == "win32":   # match the Windows UI font in the plots
    matplotlib.rcParams["font.family"] = ["Segoe UI", "DejaVu Sans"]


def enable_windows_dpi_awareness():
    """Without this, Windows renders the app at 96 DPI and stretches it -> blurry on scaled displays."""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # system DPI aware (same as Python's IDLE)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def ui_font(size_delta=0, bold=False):
    """The platform UI font (Segoe UI on Windows) at a size relative to the default."""
    from tkinter import font as tkfont
    base = tkfont.nametofont("TkDefaultFont").actual()
    size = abs(base["size"]) + size_delta
    return (base["family"], size, "bold") if bold else (base["family"], size)

QUOTED = re.compile(r'"([^"]*)"')
# Columns that are time/iteration counters rather than monitored quantities.
COUNTER_NAMES = ("iteration", "time step", "flow-time", "flow time")


def is_counter(name):
    return name.strip().lower() in COUNTER_NAMES


# --------------------------------------------------------------------------- data
class ReportFile:
    """Incrementally reads a Fluent report file. Only new bytes are read each poll."""

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.title = os.path.splitext(self.name)[0]
        self._reset()

    def _reset(self):
        self.offset = 0
        self.partial = b""
        self.columns = []          # names from the "( ... )" header line
        self.data = []             # one list of floats per column
        self.last_change = None    # wall-clock time new data last arrived
        self.layout_version = 0    # bumps when the column set changes

    def _set_columns(self, names):
        if names != self.columns:
            self.columns = names
            self.data = [[] for _ in names]
            self.layout_version += 1

    def _add_row(self, values):
        x = values[0]
        xs = self.data[0]
        # Iteration counter went back (e.g. older data file re-read and run again):
        # discard the overwritten part so the newest history wins.
        if xs and x <= xs[-1]:
            cut = bisect.bisect_left(xs, x)
            for col in self.data:
                del col[cut:]
        for col, v in zip(self.data, values):
            col.append(v)

    def poll(self):
        """Read anything appended since last poll. Returns True if data changed."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return False
        if size < self.offset:            # file truncated / rewritten by Fluent
            self._reset()
            self.layout_version += 1
        if size == self.offset:
            return False
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read(size - self.offset)
        except OSError:                   # file locked for a moment on Windows
            return False
        self.offset += len(chunk)
        buf = self.partial + chunk
        lines = buf.split(b"\n")
        self.partial = lines.pop()        # keep incomplete last line for next poll

        changed = False
        for raw in lines:
            line = raw.decode("latin-1").strip()
            if not line:
                continue
            if line.startswith("("):      # ("Iteration" "mfr(inlet)" ...) -> real column names
                names = QUOTED.findall(line)
                if names:
                    self._set_columns(names)
                    changed = True
                continue
            if line.startswith('"'):      # file title / short header
                continue
            parts = line.split()
            if not self.columns:          # headerless file: invent names
                self._set_columns(["Iteration"] + [f"col{i}" for i in range(1, len(parts))])
            if len(parts) != len(self.columns):
                continue
            try:
                values = [float(p) for p in parts]
            except ValueError:
                continue
            self._add_row(values)
            changed = True
        if changed:
            self.last_change = time.time()
        return changed



# --------------------------------------------------------------------------- per-file view settings
class ViewState:
    """Settings that survive switching between tabs and grid."""

    def __init__(self):
        self.hidden = set()   # column names hidden via the legend
        self.logy = False
        self.follow = True
        self.lastn = "0"
        self.xcol = ""


# --------------------------------------------------------------------------- one plot
class Toolbar(NavigationToolbar2Tk):
    """matplotlib toolbar where Home means "follow the data again" and zoom/pan stop following."""

    def __init__(self, canvas, window, panel):
        self.panel = panel
        super().__init__(canvas, window, pack_toolbar=False)

    def home(self, *args):
        self.panel.set_follow(True)

    def _limits(self):
        ax = self.panel.ax
        return ax.get_xlim(), ax.get_ylim()

    def release_zoom(self, event):
        before = self._limits()
        super().release_zoom(event)
        if self._limits() != before:
            self.panel.set_follow(False)

    def release_pan(self, event):
        before = self._limits()
        super().release_pan(event)
        if self._limits() != before:
            self.panel.set_follow(False)


ZOOM_PER_NOTCH = 1.25   # mouse-wheel zoom factor per wheel notch


class PlotPanel:
    def __init__(self, parent, app, report, state, compact):
        self.app = app
        self.rf = report
        self.st = state
        self.compact = compact
        self.frame = ttk.Frame(parent, borderwidth=1, relief="solid" if compact else "flat")
        self.dirty = True

        # controls
        bar = ttk.Frame(self.frame, padding=(4, 2))
        bar.pack(side=tk.TOP, fill=tk.X)
        if compact:
            ttk.Label(bar, text=report.title, font=ui_font(bold=True)).pack(side=tk.LEFT, padx=(0, 8))
        self.logy = tk.BooleanVar(value=state.logy)
        self.follow = tk.BooleanVar(value=state.follow)
        self.lastn = tk.StringVar(value=state.lastn)
        self.xcol = tk.StringVar(value=state.xcol)
        ttk.Checkbutton(bar, text="Log y", variable=self.logy, command=self.settings_changed).pack(side=tk.LEFT)
        ttk.Checkbutton(bar, text="Follow", variable=self.follow, command=self.settings_changed).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(bar, text="Last N:").pack(side=tk.LEFT, padx=(8, 2))
        e = ttk.Entry(bar, textvariable=self.lastn, width=6)
        e.pack(side=tk.LEFT)
        e.bind("<Return>", lambda _e: self.settings_changed())
        e.bind("<FocusOut>", lambda _e: self.settings_changed())
        self.xbox = ttk.Combobox(bar, textvariable=self.xcol, state="readonly", width=12)
        self.xbox.bind("<<ComboboxSelected>>", lambda _e: self.settings_changed(rebuild=True))
        if not compact:
            ttk.Label(bar, text="X axis:").pack(side=tk.LEFT, padx=(10, 2))
            self.xbox.pack(side=tk.LEFT)
            ttk.Button(bar, text="Show all curves", command=self.show_all).pack(side=tk.LEFT, padx=(10, 0))

        # status + toolbar at the bottom, figure fills the rest
        self.status = ttk.Label(self.frame, text="", foreground="#555", padding=(6, 0, 6, 1),
                                font=ui_font(-1 if compact else 0))
        self.status.pack(side=tk.BOTTOM, fill=tk.X)
        self.fig = Figure(figsize=(4, 3) if compact else (8, 5), dpi=100, layout="constrained")
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.frame)
        if not compact:
            tb = Toolbar(self.canvas, self.frame, self)
            tb.update()
            tb.pack(side=tk.BOTTOM, fill=tk.X)
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.canvas.mpl_connect("pick_event", self.on_pick)
        self.canvas.mpl_connect("button_press_event", self.on_click)
        # hover readout (drawn with blitting, so moving the mouse never redraws the curves)
        self.canvas.mpl_connect("draw_event", self.on_draw)
        self.canvas.mpl_connect("motion_notify_event", self.on_motion)
        self.canvas.mpl_connect("figure_leave_event", lambda _e: self.hide_hover())
        self.canvas.mpl_connect("scroll_event", self.on_scroll)
        self.bg = None          # saved image of the plot without the hover marker
        self.hover_xy = None    # mouse position (display pixels) while hovering a plot
        self.hover_hit = None   # (column index, data index) under the mouse

        self.lines = {}     # column index -> Line2D
        self.leg_map = {}   # legend artist -> column index
        self.layout_seen = -1
        self.xi = 0
        self.rebuild()

    def destroy(self):
        self.frame.destroy()

    def settings_changed(self, rebuild=False):
        s = self.st
        s.logy, s.follow, s.lastn, s.xcol = self.logy.get(), self.follow.get(), self.lastn.get(), self.xcol.get()
        self.rebuild() if rebuild else self.redraw()

    # ---- layout
    def rebuild(self, now=False):
        rf = self.rf
        self.layout_seen = rf.layout_version
        cols = rf.columns
        counters = [c for c in cols if is_counter(c)] or cols[:1]
        self.xbox["values"] = counters
        if self.xcol.get() not in counters:
            self.xcol.set(counters[0] if counters else "")
            self.st.xcol = self.xcol.get()
        self.ax.clear()
        self.lines.clear()
        self.leg_map.clear()
        self.xi = cols.index(self.xcol.get()) if self.xcol.get() in cols else 0
        small = self.compact
        palette = matplotlib.colormaps["tab10"].colors
        k = 0
        for i, name in enumerate(cols):
            if i == self.xi or is_counter(name):
                continue
            # 10 colours, then the same colours dashed, so 11+ curves stay distinguishable
            (ln,) = self.ax.plot([], [], lw=1.2 if small else 1.4, label=self._label(name),
                                 color=palette[k % 10], ls="-" if k < 10 else "--")
            k += 1
            ln.set_visible(name not in self.st.hidden)
            self.lines[i] = ln
        self.ax.set_xlabel(self.xcol.get() or "Iteration", fontsize=8 if small else 10)
        if not small:
            self.ax.set_title(rf.title)
        self.ax.tick_params(labelsize=7 if small else 9)
        self.ax.grid(True, which="both", alpha=0.3)
        if len(self.lines) > 1:   # single-curve files: the title already names the curve
            leg = self.ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0),
                                 fontsize="x-small" if small else "small",
                                 frameon=False, borderaxespad=0, handlelength=1.5)
            for legline, legtext, i in zip(leg.get_lines(), leg.get_texts(), self.lines):
                for artist in (legline, legtext):
                    artist.set_picker(6)
                    self.leg_map[artist] = i
                self._style_legend(i)
        if len(self.lines) == 1 and not small:
            self.ax.set_ylabel(self._label(cols[next(iter(self.lines))]))
        # hover marker + value box; animated = not part of normal draws (blitted on top)
        (self.marker,) = self.ax.plot([], [], "o", ms=8 if not small else 7, mfc="none", mew=1.8,
                                      animated=True, zorder=10, label="_hover")
        self.annot = self.ax.annotate(
            "", xy=(0, 0), xytext=(12, 12), textcoords="offset points", animated=True,
            fontsize=8 if small else 9, zorder=11, annotation_clip=False, multialignment="left",
            bbox=dict(boxstyle="round,pad=0.35", fc="white", ec="0.5", lw=0.8, alpha=0.95))
        self.marker.set_visible(False)
        self.annot.set_visible(False)
        self.hover_hit = None
        self.redraw(now=now)

    @staticmethod
    def _label(name):
        # "vel(point-030)" -> "point-030" keeps the legend short; plain names stay.
        m = re.fullmatch(r"[^()]+\((.+)\)", name)
        return m.group(1) if m else name

    def _style_legend(self, i):
        vis = self.lines[i].get_visible()
        for artist, j in self.leg_map.items():
            if j == i:
                artist.set_alpha(1.0 if vis else 0.25)

    def on_pick(self, event):
        i = self.leg_map.get(event.artist)
        if i is None:
            return
        ln = self.lines[i]
        ln.set_visible(not ln.get_visible())
        name = self.rf.columns[i]
        (self.st.hidden.discard if ln.get_visible() else self.st.hidden.add)(name)
        self._style_legend(i)
        self.redraw()

    def on_click(self, event):
        if not event.dblclick:
            return
        if self.compact:
            self.app.focus_in_tabs(self.rf.path)     # grid: open this plot as a tab
        elif event.inaxes is self.ax:
            self.set_follow(True)                    # tabs: reset zoom, follow the data again

    def set_follow(self, on):
        self.follow.set(on)
        self.st.follow = on
        if on:
            self.redraw()

    # ---- mouse-wheel zoom: wheel = both axes, Shift = y only, Ctrl = x only (around the mouse)
    def on_scroll(self, event):
        if event.inaxes is not self.ax or not event.step:
            return
        mods = set(getattr(event, "modifiers", ()) or ())
        ctrl, shift = "ctrl" in mods, "shift" in mods
        zoom_x = ctrl or not shift
        zoom_y = shift or not ctrl
        f = ZOOM_PER_NOTCH ** (-event.step)          # wheel up (step > 0) = zoom in
        if zoom_x:
            self._zoom_axis(self.ax.xaxis, self.ax.get_xlim, self.ax.set_xlim, event.xdata, f)
        if zoom_y:
            self._zoom_axis(self.ax.yaxis, self.ax.get_ylim, self.ax.set_ylim, event.ydata, f)
        if self.follow.get():
            self.follow.set(False)                   # otherwise the next data update undoes the zoom
            self.st.follow = False
        self.canvas.draw_idle()

    @staticmethod
    def _zoom_axis(axis, get_lim, set_lim, center, f):
        # zoom in the axis' own scale space, so log axes zoom evenly too
        tr = axis.get_transform()
        lo, hi = get_lim()
        with np.errstate(all="ignore"):
            a, b = tr.transform(np.array([lo, hi]))
            c = tr.transform(np.array([center]))[0]
            if not np.isfinite([a, b, c]).all():
                return
            na, nb = tr.inverted().transform(np.array([c + (a - c) * f, c + (b - c) * f]))
        if np.isfinite([na, nb]).all() and na != nb:
            set_lim(na, nb)

    # ---- hover readout
    def on_draw(self, _event):
        """After every full draw: remember the clean image, then put the marker back on top."""
        self.bg = self.canvas.copy_from_bbox(self.fig.bbox)
        if self.hover_xy is not None:
            self._find_hover()
            self._draw_hover_artists()

    def on_motion(self, event):
        if event.inaxes is not self.ax or event.button is not None:
            self.hide_hover()       # outside the plot, or panning/zooming with a button held
            return
        self.hover_xy = (event.x, event.y)
        self._find_hover()
        self._blit_hover()

    def hide_hover(self):
        if self.hover_xy is None and not self.marker.get_visible():
            return
        self.hover_xy = None
        self.hover_hit = None
        self.marker.set_visible(False)
        self.annot.set_visible(False)
        self._blit_hover()

    def _find_hover(self):
        """Nearest data point of the visible curve closest to the mouse (within a few pixels)."""
        mx, my = self.hover_xy
        ratio = self.canvas.device_pixel_ratio or 1
        tol = 10 * ratio                       # pixels on screen
        trans = self.ax.transData
        inv = trans.inverted()
        xa = inv.transform([(mx - tol, my), (mx + tol, my)])[:, 0]
        x_lo, x_hi = min(xa), max(xa)
        best = None                            # (distance, column, index, display point)
        for i, ln in self.lines.items():
            if not ln.get_visible():
                continue
            xd, yd = ln.get_data()
            n = len(xd)
            if not n:
                continue
            lo = max(int(np.searchsorted(xd, x_lo, "left")) - 1, 0)   # include the neighbours
            hi = min(int(np.searchsorted(xd, x_hi, "right")) + 1, n)  # just outside the window
            if hi <= lo:
                continue
            with np.errstate(all="ignore"):
                pts = trans.transform(np.column_stack([xd[lo:hi], yd[lo:hi]]))
            ok = np.isfinite(pts).all(axis=1)
            if not ok.any():
                continue
            d = np.hypot(pts[:, 0] - mx, pts[:, 1] - my)
            d[~ok] = np.inf
            k = int(np.argmin(d))
            dist = d[k]
            # also accept the mouse being on the line *between* two samples (zoomed in)
            if len(pts) > 1 and dist > tol:
                px, py = pts[:, 0], pts[:, 1]
                j = int(np.searchsorted(px, mx))
                if 0 < j < len(px) and ok[j - 1] and ok[j] and px[j] != px[j - 1]:
                    t = (mx - px[j - 1]) / (px[j] - px[j - 1])
                    dist = min(dist, abs(my - (py[j - 1] + t * (py[j] - py[j - 1]))))
            if dist <= tol and (best is None or dist < best[0]):
                best = (dist, i, lo + k, pts[k])
        if best is None:
            self.hover_hit = None
            self.marker.set_visible(False)
            self.annot.set_visible(False)
            return
        _, i, k, (px, py) = best
        ln = self.lines[i]
        xd, yd = ln.get_data()
        x, y = xd[k], yd[k]
        self.hover_hit = (i, k)
        self.marker.set_data([x], [y])
        self.marker.set_color(ln.get_color())
        self.marker.set_visible(True)
        name = ln.get_label() if len(self.lines) > 1 else self.rf.title
        xname = self.xcol.get() or "x"
        xs = f"{x:.0f}" if float(x).is_integer() else f"{x:.6g}"
        self.annot.set_text(f"{name}\n{xname} {xs}\n{y:.12g}")
        self.annot.xy = (x, y)
        # put the box on the side of the point that faces the middle of the plot
        bb = self.ax.bbox
        right = px > (bb.x0 + bb.x1) / 2
        top = py > (bb.y0 + bb.y1) / 2
        self.annot.set_position((-12 if right else 12, -12 if top else 12))
        self.annot.set_horizontalalignment("right" if right else "left")
        self.annot.set_verticalalignment("top" if top else "bottom")
        self.annot.get_bbox_patch().set_edgecolor(ln.get_color())
        self.annot.set_visible(True)

    def _draw_hover_artists(self):
        if self.marker.get_visible():
            self.ax.draw_artist(self.marker)
            self.ax.draw_artist(self.annot)

    def _blit_hover(self):
        if self.bg is None:
            return
        self.canvas.restore_region(self.bg)
        self._draw_hover_artists()
        self.canvas.blit(self.fig.bbox)

    def show_all(self):
        self.st.hidden.clear()
        for i, ln in self.lines.items():
            ln.set_visible(True)
            self._style_legend(i)
        self.redraw()

    # ---- data
    def refresh(self, changed, now=False):
        """Called every tick after the file was polled (now=True: draw immediately)."""
        if self.rf.layout_version != self.layout_seen:
            self.rebuild(now=now)
        elif changed or self.dirty:
            self.redraw(now=now)
        else:
            self._update_status()

    def redraw(self, now=False):
        self.dirty = False
        rf = self.rf
        if not rf.data or not rf.data[0]:
            self.status.config(text="waiting for data…")
            self.canvas.draw_idle()
            return
        try:
            n = max(0, int(float(self.lastn.get() or 0)))
        except ValueError:
            n = 0
        sl = slice(-n, None) if n else slice(None)
        x = np.asarray(rf.data[self.xi][sl])
        for i, ln in self.lines.items():
            ln.set_data(x, np.asarray(rf.data[i][sl]))
        self.ax.set_yscale("log" if self.logy.get() else "linear")
        if self.follow.get():
            self.ax.set_autoscale_on(True)     # zooming (wheel/toolbar) switched autoscale off
            self.ax.relim(visible_only=True)
            self.ax.autoscale_view()
        self._update_status()
        if now:
            self.canvas.draw()
        else:
            self.canvas.draw_idle()

    def _update_status(self):
        rf = self.rf
        if not rf.data or not rf.data[0]:
            return
        age = time.time() - rf.last_change if rf.last_change else 0
        age_txt = f"{age:.0f} s ago" if age < 120 else f"{age / 60:.0f} min ago"
        self.status.config(text=f"{self.xcol.get()} {rf.data[self.xi][-1]:g}  ·  "
                                f"{len(rf.data[0])} points  ·  updated {age_txt}")


# --------------------------------------------------------------------------- main window
class App:
    def __init__(self, root, folder, pattern, interval, open_patterns, view, columns):
        self.root = root
        self.pattern = pattern
        self.interval_ms = int(interval * 1000)
        self.folder = tk.StringVar(value=folder or "")
        self.vars = {}        # path -> BooleanVar (ticked in the list)
        self.checkboxes = {}  # path -> Checkbutton
        self.reports = {}     # path -> ReportFile
        self.states = {}      # path -> ViewState
        self.panels = {}      # path -> PlotPanel currently shown
        self.last_scan = 0.0

        root.title("Fluent report monitor")
        # sizes below are in pixels at 100 % display scaling; scale them to the real DPI
        self.scale = s = max(1.0, root.winfo_fpixels("1i") / 96)
        w = min(int(1400 * s), int(root.winfo_screenwidth() * 0.9))    # never larger than the screen
        h = min(int(850 * s), int(root.winfo_screenheight() * 0.85))
        root.geometry(f"{w}x{h}")

        # ---- left sidebar
        side = ttk.Frame(root, padding=8)
        side.pack(side=tk.LEFT, fill=tk.Y)
        frow = ttk.Frame(side)
        frow.pack(fill=tk.X)
        ttk.Label(frow, text="Folder:").pack(side=tk.LEFT)
        ttk.Button(frow, text="Browse…", command=self.browse).pack(side=tk.RIGHT)
        self.folder_label = ttk.Label(side, text="", foreground="#555", wraplength=int(230 * s))
        self.folder_label.pack(fill=tk.X, pady=(2, 8))

        listbox = ttk.Frame(side)
        listbox.pack(fill=tk.BOTH, expand=True)
        self.lcanvas = tk.Canvas(listbox, highlightthickness=0, width=int(240 * s))
        sb = ttk.Scrollbar(listbox, orient=tk.VERTICAL, command=self.lcanvas.yview)
        self.listframe = ttk.Frame(self.lcanvas)
        self.listframe.bind("<Configure>",
                            lambda e: self.lcanvas.configure(scrollregion=self.lcanvas.bbox("all")))
        self.lcanvas.create_window((0, 0), window=self.listframe, anchor="nw")
        self.lcanvas.configure(yscrollcommand=sb.set)
        self.lcanvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        brow = ttk.Frame(side)
        brow.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(brow, text="All", width=6, command=lambda: self.set_all(True)).pack(side=tk.LEFT)
        ttk.Button(brow, text="None", width=6, command=lambda: self.set_all(False)).pack(side=tk.LEFT, padx=4)

        opts = ttk.LabelFrame(side, text="View", padding=6)
        opts.pack(fill=tk.X, pady=(10, 0))
        self.view = tk.StringVar(value=view)
        ttk.Radiobutton(opts, text="Tabs", value="tabs", variable=self.view,
                        command=self.render).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(opts, text="Grid", value="grid", variable=self.view,
                        command=self.render).grid(row=0, column=1, sticky="w", padx=(10, 0))
        ttk.Label(opts, text="Grid columns:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.cols = tk.StringVar(value=str(columns) if columns else "auto")
        cb = ttk.Combobox(opts, textvariable=self.cols, width=6, state="readonly",
                          values=["auto", "1", "2", "3", "4", "5"])
        cb.grid(row=1, column=1, sticky="w", pady=(6, 0), padx=(10, 0))
        cb.bind("<<ComboboxSelected>>", lambda _e: self.layout_grid())
        ttk.Label(opts, text="Refresh (s):").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.ivar = tk.StringVar(value=f"{interval:g}")
        ttk.Spinbox(opts, from_=0.5, to=60, increment=0.5, textvariable=self.ivar,
                    width=6).grid(row=2, column=1, sticky="w", pady=(6, 0), padx=(10, 0))
        self.info = ttk.Label(side, text="", foreground="#555")
        self.info.pack(fill=tk.X, pady=(6, 0))

        ttk.Separator(root, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y)

        # ---- right: plot area
        self.area = ttk.Frame(root)
        self.area.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.container = None   # holds the tab strip + stack (tabs) or the grid
        self.tabbar = None      # ttk.Notebook used only as the tab strip
        self.stack = None       # frame in which all tab plots are stacked
        self.tab_pages = {}     # path -> (empty) notebook page
        self.current = None     # path of the plot shown in the tabs view
        self._wheel_acc = 0
        self.placeholder = None
        # tab strip without the notebook's (empty) page area below the tabs
        style = ttk.Style(root)
        style.layout("Strip.TNotebook", [])
        style.configure("Strip.TNotebook", borderwidth=0)

        if not self.folder.get():
            self.browse(initial=True)
        self.scan()
        for path, var in self.vars.items():
            if any(fnmatch.fnmatch(os.path.basename(path), p) for p in open_patterns):
                var.set(True)
        self.render()
        root.after(self.interval_ms, self.tick)

    # ---- folder / file list
    def browse(self, initial=False):
        d = filedialog.askdirectory(title="Folder with Fluent report files",
                                    initialdir=self.folder.get() or os.getcwd())
        if not d:
            return
        self.folder.set(d)
        if not initial:
            for child in self.listframe.winfo_children():
                child.destroy()
            for d_ in (self.vars, self.checkboxes, self.reports, self.states):
                d_.clear()
            self.scan()
            self.render()

    def scan(self):
        self.last_scan = time.time()
        folder = self.folder.get()
        self.folder_label.config(text=folder)
        if not os.path.isdir(folder):
            self.info.config(text="Folder not found")
            return
        if folder != getattr(self, "_saved_folder", None):
            save_last_folder(folder)
            self._saved_folder = folder
        found = sorted(f for f in os.listdir(folder) if fnmatch.fnmatch(f.lower(), self.pattern.lower()))
        new = [os.path.join(folder, f) for f in found if os.path.join(folder, f) not in self.vars]
        if new:
            for path in new:
                self.vars[path] = tk.BooleanVar(value=False)
                self.checkboxes[path] = ttk.Checkbutton(
                    self.listframe, text=os.path.basename(path), variable=self.vars[path],
                    command=lambda p=path: self.toggle(p))
            for child in self.listframe.winfo_children():
                child.pack_forget()
            for path in sorted(self.vars):          # keep alphabetical order
                self.checkboxes[path].pack(anchor="w", pady=1)
        self.info.config(text=f"{len(self.vars)} file(s) matching {self.pattern}")

    def selected(self):
        return [p for p in sorted(self.vars) if self.vars[p].get()]

    def set_all(self, state):
        for var in self.vars.values():
            var.set(state)
        self.render()

    def toggle(self, path):
        if self.view.get() == "tabs" and self.tabbar is not None:
            if self.vars[path].get():
                self._add_tab(path)
                self.select_tab(path)
            else:
                self._remove_tab(path)
        else:
            self.render()

    # ---- building the plot area
    def _panel(self, parent, path, compact):
        rf = self.reports.get(path)
        if rf is None:
            rf = self.reports[path] = ReportFile(path)
            rf.poll()
        st = self.states.setdefault(path, ViewState())
        return PlotPanel(parent, self, rf, st, compact)

    # Tabs view = a tab strip (ttk.Notebook with empty pages) above a "stack" in which
    # every plot is laid out at full size, on top of each other. Switching tabs only
    # raises the right plot, so it never has to resize or re-layout when shown.
    def _add_tab(self, path):
        if path in self.panels:
            return
        p = self._panel(self.stack, path, compact=False)
        p.frame.grid(row=0, column=0, sticky="nsew")
        self.panels[path] = p
        page = ttk.Frame(self.tabbar, height=0)
        self.tab_pages[path] = page
        order = [q for q in self.selected() if q in self.panels]
        idx = order.index(path)
        if idx >= len(self.tabbar.tabs()):
            self.tabbar.add(page, text=p.rf.title)
        else:
            self.tabbar.insert(idx, page, text=p.rf.title)
        self._update_placeholder()

    def _remove_tab(self, path):
        p = self.panels.pop(path, None)
        page = self.tab_pages.pop(path, None)
        if page is not None:
            self.tabbar.forget(page)
            page.destroy()
        if p:
            p.destroy()
        if self.current not in self.panels:
            self.current = None
            self._on_tab_changed()
        self._update_placeholder()

    def select_tab(self, path):
        page = self.tab_pages.get(path)
        if page is not None:
            self.tabbar.select(page)
            self._on_tab_changed()

    def _on_tab_changed(self, _event=None):
        try:
            sel = self.tabbar.select()
        except tk.TclError:
            return
        path = next((q for q, pg in self.tab_pages.items() if str(pg) == sel), None)
        if path is None or path == self.current:
            return
        p = self.panels[path]
        if p.dirty or p.rf.layout_version != p.layout_seen:
            p.refresh(False, now=True)   # bring it up to date *before* it becomes visible
        p.frame.tkraise()
        self.current = path

    def _on_tab_wheel(self, event):
        """Scroll over the tab strip to switch tabs (Windows/macOS: delta, X11: Button-4/5)."""
        tabs = self.tabbar.tabs()
        if not tabs:
            return "break"
        if event.num == 4:
            step = -1
        elif event.num == 5:
            step = 1
        else:   # accumulate so high-resolution wheels / touchpads don't skip tabs
            self._wheel_acc += event.delta
            if abs(self._wheel_acc) < 120:
                return "break"
            step = -1 if self._wheel_acc > 0 else 1
            self._wheel_acc = 0
        idx = self.tabbar.index(self.tabbar.select()) + step
        if 0 <= idx < len(tabs):
            self.tabbar.select(idx)
            self._on_tab_changed()
        return "break"

    def render(self):
        """(Re)create the plot area for the current view mode and selection."""
        for p in self.panels.values():
            p.destroy()
        self.panels.clear()
        self.tab_pages.clear()
        self.current = None
        for w in (self.container,):
            if w is not None:
                w.destroy()
        self.tabbar = self.stack = None
        if self.view.get() == "tabs":
            self.container = ttk.Frame(self.area)
            self.container.pack(fill=tk.BOTH, expand=True)
            self.tabbar = ttk.Notebook(self.container, style="Strip.TNotebook")
            self.tabbar.pack(side=tk.TOP, fill=tk.X)
            self.tabbar.bind("<<NotebookTabChanged>>", self._on_tab_changed)
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.tabbar.bind(seq, self._on_tab_wheel)
            self.stack = ttk.Frame(self.container)
            self.stack.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
            self.stack.grid_rowconfigure(0, weight=1)
            self.stack.grid_columnconfigure(0, weight=1)
            for path in self.selected():
                self._add_tab(path)
            self._on_tab_changed()
        else:
            self.container = ttk.Frame(self.area, padding=4)
            self.container.pack(fill=tk.BOTH, expand=True)
            for path in self.selected():
                self.panels[path] = self._panel(self.container, path, compact=True)
            self.layout_grid()
        self._update_placeholder()

    def layout_grid(self):
        if self.view.get() != "grid" or self.container is None:
            return
        paths = [p for p in self.selected() if p in self.panels]
        n = len(paths)
        for i in range(12):   # reset weights from previous layouts
            self.container.grid_columnconfigure(i, weight=0, uniform="")
            self.container.grid_rowconfigure(i, weight=0, uniform="")
        if not n:
            return
        try:
            ncol = int(self.cols.get())
        except ValueError:
            ncol = math.ceil(math.sqrt(n))
        ncol = max(1, min(ncol, n))
        nrow = math.ceil(n / ncol)
        for k, path in enumerate(paths):
            r, c = divmod(k, ncol)
            self.panels[path].frame.grid(row=r, column=c, sticky="nsew", padx=3, pady=3)
        for c in range(ncol):
            self.container.grid_columnconfigure(c, weight=1, uniform="col")
        for r in range(nrow):
            self.container.grid_rowconfigure(r, weight=1, uniform="row")

    def _update_placeholder(self):
        if self.placeholder is not None:
            self.placeholder.destroy()
            self.placeholder = None
        if not self.panels:
            self.placeholder = ttk.Label(self.area, text="Tick report files on the left to plot them.",
                                         foreground="#777", anchor="center")
            self.placeholder.place(relx=0.5, rely=0.5, anchor="center")

    def focus_in_tabs(self, path):
        self.view.set("tabs")
        self.render()
        self.select_tab(path)

    def _visible(self, panel):
        if self.view.get() == "grid":
            return True
        return self.current is not None and self.panels.get(self.current) is panel

    # ---- periodic update
    def tick(self):
        try:
            self.interval_ms = max(200, int(float(self.ivar.get()) * 1000))
        except ValueError:
            pass
        if time.time() - self.last_scan > 5:
            self.scan()
        for p in list(self.panels.values()):
            changed = p.rf.poll()
            if changed:
                p.dirty = True
            if self._visible(p):        # hidden tabs are redrawn when you switch to them
                try:
                    p.refresh(changed)
                except tk.TclError:
                    pass
        self.root.after(self.interval_ms, self.tick)


SETTINGS = os.path.join(os.path.expanduser("~"), ".fluent_monitor_last_folder.txt")


def load_last_folder():
    try:
        with open(SETTINGS, encoding="utf-8") as fh:
            d = fh.read().strip()
        return d if os.path.isdir(d) else None
    except OSError:
        return None


def save_last_folder(folder):
    try:
        with open(SETTINGS, "w", encoding="utf-8") as fh:
            fh.write(folder)
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description="Live plots of Fluent report files.")
    ap.add_argument("folder", nargs="?", help="folder with the .out report files "
                                             "(default: the last folder used, otherwise ask)")
    ap.add_argument("--pattern", default="*.out", help="file pattern (default: *.out)")
    ap.add_argument("--interval", type=float, default=2.0, help="refresh interval in seconds (default 2)")
    ap.add_argument("--open", nargs="*", default=[], metavar="GLOB",
                    help='files to show at start, e.g. --open "mach*" "mfr-rfile.out" or --open "*"')
    ap.add_argument("--view", choices=["tabs", "grid"], default="tabs", help="start in tabs or grid view")
    ap.add_argument("--columns", type=int, default=0, help="grid columns (default: automatic)")
    args = ap.parse_args()
    folder = args.folder
    if folder and os.path.isfile(folder):        # a report file was dropped on the launcher
        folder = os.path.dirname(folder)
    if folder and not os.path.isdir(folder):
        sys.exit(f"Not a folder: {folder}")
    folder = os.path.abspath(folder) if folder else load_last_folder()
    enable_windows_dpi_awareness()   # must run before the first Tk window exists
    root = tk.Tk()
    App(root, folder, args.pattern, args.interval, args.open, args.view, args.columns)
    root.mainloop()


def run():
    """Entry point: like main(), but shows errors in a dialog (the GUI launcher has no console)."""
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback
        msg = traceback.format_exc()
        try:
            from tkinter import messagebox
            r = tk.Tk(); r.withdraw()
            messagebox.showerror("Fluent report monitor", msg)
        except Exception:
            print(msg, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    run()
