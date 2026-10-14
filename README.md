# fluent-monitor

Live plots of Ansys Fluent report files (`*.out`) in one window, as tabs or a grid,
independent of the Fluent GUI. One plot per report file, one curve per quantity.

Everything runs in an isolated environment managed by [uv](https://docs.astral.sh/uv/):
uv downloads its own Python 3.12 and the locked dependencies (pyqtgraph, PySide6 = Qt,
numpy) into `.venv` inside this folder (about 300 MB, mostly Qt). Nothing is installed
into a system Python, and no compiler is needed (all dependencies ship prebuilt Windows
wheels).

Version 2 draws with pyqtgraph/Qt instead of matplotlib: a refresh of 10 plots in the grid
view takes about a tenth of the time it did before. Since 2.1 all file access (folder
listing, reading the report files) happens in a background thread, so a slow disk, a
virus scanner or cloud sync can delay new data but never freeze the window, and plots are
updated one at a time so the mouse/hover stay responsive in between.

Since 2.2 there is also a **web version** (`fluent-monitor-web`, see below) to follow
the run from a phone or any browser.

If the window still freezes, start it with `--debug`, reproduce the freeze, and look at
(or send) the log file: it records which step took how long.

## 1. Install uv (once)

In PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

This installs only the single `uv.exe` into `%USERPROFILE%\.local\bin`. Open a new terminal afterwards.
(Alternatives: `winget install --id=astral-sh.uv -e`, or download `uv.exe` from the
GitHub releases page.)

## 2. Build

Unzip this folder anywhere, then in a terminal inside it:

```powershell
uv sync --locked
```

uv fetches Python 3.12 and the exact package versions in `uv.lock` into `.venv`.
This step needs internet once; afterwards everything works offline.
When updating from version 1, just run `uv sync --locked` again: it removes matplotlib
and installs the new packages.

## 3. Run

Any of these:

- **Double-click `Fluent Monitor.bat`.** It opens the last folder used, or asks for one.
  Drag a case folder or any `.out` file onto the .bat to watch that folder.
- From a terminal in this folder:
  ```powershell
  uv run fluent-monitor "C:\Users\Pedro\Documents\Faculdade\Semesterarbeit\Fluent"
  uv run fluent-monitor "C:\...\Fluent" --view grid --open "*"
  ```

Options:

| option | meaning |
|---|---|
| `folder` | folder with the report files (default: last folder used, otherwise a dialog) |
| `--open GLOB ...` | files to show at start, e.g. `--open "*"` or `--open "mach*" "mfr-rfile.out"` |
| `--view tabs\|grid` | start view (default `tabs`) |
| `--columns N` | grid columns (default: automatic) |
| `--interval S` | refresh interval in seconds (default 2) |
| `--pattern P` | file pattern (default `*.out`) |
| `--opengl` | draw with the graphics card (OpenGL); try it if the grid view is still slow |
| `--debug [LOGFILE]` | write timings and window freezes to a log (default `%USERPROFILE%\fluent_monitor_debug.log`) |

## Optional: install as a command

To get `fluent-monitor` (with console) and `fluent-monitor-gui` (no console window)
on your PATH in their own isolated environment, run inside this folder:

```powershell
uv tool install .
```

Remove again with `uv tool uninstall fluent-monitor`.

## Using it

- **Left:** report files in the folder (new ones appear automatically) and your combined
  plots. Tick to plot. The divider between the list and the plots can be dragged.
- **Tabs** view: one full-size plot per tab. Scroll the mouse wheel over the tab bar to
  switch tabs. Buttons: *Show all curves*, *Reset view*, *Save image…* (PNG at 2x
  resolution, or vector PDF / SVG for reports).
- **Grid** view: all ticked plots at once; double-click a plot to open it as a tab.
- **Hover** over a curve: a circle marks the nearest data point and a box shows its value.
- **Zoom/pan:** mouse wheel zooms around the mouse; **Shift**+wheel zooms only the y axis,
  **Ctrl**+wheel only the x axis. Left-drag pans, right-drag stretches/squeezes the axes,
  right-click opens pyqtgraph's menu. Zooming or panning turns *Follow* off so live
  updates keep your view; *Reset view*, a double-click on the plot (tabs view) or ticking
  *Follow* returns to the full, auto-scaled view.
- **Legend:** click an entry (text or line sample) to hide/show that curve.
- *Log y*, *Follow* (auto-rescale) and *Last N* (only the last N iterations) are per plot.
- **Combine…**: tick two or more report files and click *Combine…* to put all their
  curves into one plot (legend shows the full Fluent names, e.g. `mach(point-030)`).
  Combined plots appear under *Combined plots*, work like any other plot, are remembered
  per case folder, and are deleted with right-click → Delete (or the *Delete* button).
  Hover over one to see which files it contains.
- If you rerun from an older data file and the iteration counter goes back, the
  plot drops the overwritten part, so the newest run wins.
- In Fluent you can set the report plots inactive; keep the report **files** active.

## Web version: watch from your phone or any browser

`fluent-monitor-web` is a small web server (Python standard library + numpy; no Qt
needed) that serves the same plots to any browser: desktop, tablet or phone. It reads the
report files in a background thread and sends each browser only the rows it hasn't got yet,
so a phone on mobile data receives a few hundred bytes per refresh.

Start it on a computer that can read the case folder:

```powershell
uv run fluent-monitor-web "C:\...\Fluent"              # port 8050
uv run fluent-monitor-web "C:\...\Fluent" --port 9000
```

or double-click / drag a folder onto **`Fluent Monitor Web.bat`**. It prints links like

```
http://MACHINE-NAME:8050/?token=Dv_TSM6...
http://10.12.34.56:8050/?token=Dv_TSM6...
```

Open one of them **once** in each browser. The token is then kept in a cookie and the
address bar shows a plain URL that you can bookmark or add to the home screen. Without the
token the server answers "access denied". The token stays the same between restarts;
`--new-token` makes a new one, which stops old links and bookmarks from working.

| option | meaning |
|---|---|
| `folder` | folder with the report files (default: last folder used) |
| `--port N` | port (default 8050) |
| `--bind ADDR` | listen address; `127.0.0.1` = this computer only (default: all networks) |
| `--interval S` | seconds between file reads (default 1) |
| `--pattern P` | file pattern (default `*.out`) |
| `--new-token` / `--no-token` | new access token / no token (not recommended) |
| `--verbose`, `--debug [LOGFILE]` | log requests / slow file reads |

**In the browser:**
- **☰** opens the file list. On a phone it slides in from the left.
- **Tabs / Grid** switch the view. The first visit shows all files; after that, your
  selection, view and per-plot settings are remembered in each browser.
- **Mouse:**
  - hover to read values
  - wheel to zoom around the mouse (**Shift**: y only, **Ctrl**: x only)
  - drag a box to zoom into it
  - double-click to reset the view and follow new data again (in the grid, a double-click
    opens the plot as a tab)
- **Touch:**
  - tap a curve to read its value
  - pinch or drag with two fingers to zoom and pan
  - double-tap to reset
  - one finger scrolls the page as usual
- Legend, *Follow*, *Log y*, *Last N*, *Combine…* and the × for deleting combined plots
  work as in the desktop app. Combined plots are shared with the desktop app because both
  use the same settings file.
- **PNG** downloads the plot with its legend at screen resolution.
- The dot next to *live* turns orange when the plotted files haven't changed for 5 minutes
  (solver stopped?) and red when the server can't be reached.

### Getting to it from the phone

The server only needs to be reachable from the phone. It should **not** be open to the
whole internet: the token protects it, but the traffic is plain HTTP.

1. **Run it on the Fluent machine and connect the phone to eduVPN.** Install the
   eduVPN app on the phone and log in with your university account, the same way as on
   the laptop. Then open the link that uses the machine's university address
   (e.g. `http://10.x.x.x:8050/?token=…`).
   - Windows Firewall: the first start asks whether Python may accept connections. Allow it
     for the networks you need ("Domain" on most university machines). Without admin rights
     the prompt may be refused silently. If the phone can't connect, ask IT for an inbound
     rule for that port, or use option 2.
   - Some universities block traffic between VPN clients and office machines on unusual
     ports. Test once with `http://ADDRESS:8050` from the laptop while it is on eduVPN. If
     that works, the phone will work too.
2. **Run it on another computer that has the case folder mounted** (your laptop, a home
   server, a Linux VM), and reach that computer from the phone, e.g. with
   [Tailscale](https://tailscale.com) or your home network. On Linux:
   `uv run fluent-monitor-web /mnt/fluent-case`. Only the computer running the server
   needs access to the files. The phone needs access only to that computer.
3. Only on your own laptop: `--bind 127.0.0.1` and open `http://localhost:8050/?token=…`.

The desktop app and the web server can run at the same time, also on the same folder.

## Uninstall

Delete this folder. uv's cached Python and packages live in
`%LOCALAPPDATA%\uv`; `uv cache clean` and `uv python uninstall 3.12` remove them.
