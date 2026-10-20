# fluent-monitor

Live plots of Ansys Fluent report files (`*.out`), residuals and solver messages,
independent of the Fluent GUI. It runs as a small local web server and you use it in your
browser:
- on your laptop
- on your phone
- in Docker on a server (see [`docker/README.md`](docker/README.md))

All of them run the same program.

Everything runs in an isolated environment managed by [uv](https://docs.astral.sh/uv/).
uv downloads its own Python 3.12 and the locked dependencies (numpy, smbprotocol) into
`.venv` inside this folder, about 80 MB. Nothing is installed into a system Python.

Version 3 is a single app: the earlier Qt desktop window (`fluent-monitor` up to 2.x) is
replaced by the browser page. The page has everything the window had except PDF export
(use SVG) and right-drag axis stretching (use Shift/Ctrl + wheel).

## 1. Install uv (once)

In PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

This installs only `uv.exe` into `%USERPROFILE%\.local\bin`. Open a new terminal afterwards.

## 2. Build

Unzip this folder anywhere, then in a terminal inside it:

```powershell
uv sync --locked
```

This step needs internet once; afterwards everything works offline. When updating from
2.x, run it again: it removes Qt (about 250 MB) and adds the new packages.

## 3. Run on your laptop

**Double-click `Fluent Monitor.bat`.** You can also drag a case folder or a `.out` file
onto it. It starts the monitor and opens the page in your default browser. The console
window is the monitor: keep it open while you use the page, and close it to stop.

From a terminal in this folder:

```powershell
uv run fluent-monitor                                    # last folder used
uv run fluent-monitor "Z:\Pedro Thesis\SPARK\HGrid Level 3"   # that folder
uv run fluent-monitor --lan                              # also reachable from your phone
```

- **Folder:** point it at Fluent's working folder, where the report files (`mach.out`) and
  Fluent's transcript (`fluent-*.trn`) are written. A mapped drive or a `\\server\share\…`
  path works. Without a folder argument it continues with the last folder. On the very first
  start it opens in your home folder; choose the case with *Change folder…* in the page.
- **Starting twice** just opens the page of the monitor that is already running.
- **Laptop only by default:** the monitor listens only on this computer. With `--lan`, your
  phone or another PC on the same network can open it too. The window then prints the
  links. If Windows asks about the firewall, allow Python for your private network.

| option | meaning |
|---|---|
| `folder` | Fluent's working folder (default: the one last chosen in the page) |
| `--lan` | reachable from other devices on the network (default: this computer only) |
| `--port N` | port (default 8050) |
| `--no-browser` | don't open the browser at start |
| `--root FOLDER` | *Change folder…* can only reach this folder and below (default: the whole drive or share) |
| `--interval S` | seconds between file reads (default 1) |
| `--pattern P` | report file pattern (default `*.out`) |
| `--new-token` / `--no-token` | new access token / no token (not recommended with `--lan`) |
| `--verbose`, `--debug [LOGFILE]` | log requests / slow file reads |

Optional: `uv tool install .` puts a `fluent-monitor` command on your PATH. Remove it with
`uv tool uninstall fluent-monitor`.

## Using it

- **☰** opens the file list. On a phone it slides in from the left. Tick files to plot them.
  New files appear by themselves.
- **Tabs / Grid** switch the view. In tabs, the mouse wheel over the tab strip switches
  tabs. In the grid, **⤢** or a double-click opens a plot as a tab. On the first visit of
  a folder all files are shown. After that, your selection, view and per-plot settings
  are remembered per folder in each browser.
- **Mouse:**
  - hover to read values; a circle marks the nearest point
  - wheel to zoom around the mouse (**Shift**: y only, **Ctrl**: x only)
  - drag a box to zoom into it
  - double-click to reset the view and follow new data again
- **Touch:**
  - tap a curve to read its value
  - pinch or drag with two fingers to zoom and pan
  - double-tap to reset
  - one finger scrolls the page
- **Follow:** keeps the newest data in view. Zooming turns it off, so live updates keep
  your view.
- **Log y:** per plot.
- **x: …:** appears for files with more than one counter, e.g. *Time Step* / *flow-time* in
  transient runs.
- **Last iterations** (file panel) sets how many of the newest iterations every plot shows
  (empty: all). A plot can override it with its own *Last* field: a number, or `all`.
  An own value is outlined, and ↺ returns the plot to the common setting. If any plots
  have their own value, the file panel says so and offers *use this for all*.
- **Legend:** click an entry to hide or show that curve. *show all* brings hidden curves
  back.
- **Save ▾:**
  - *PNG image*
  - *SVG (vector, for reports)*, of the current zoom and visible curves. Word,
    LaTeX (via Inkscape) and Inkscape can use it, and Inkscape converts it to PDF.
- **Combine…:** tick two or more files and press *Combine…* to put all their curves into
  one plot. The legend then shows the full Fluent names (`mach(point-030)`). Combined plots
  are remembered per folder. Delete one with its ×.
- **Change folder…** (in ☰, or click the folder name at the top) browses the folders and
  switches all open browsers to another case. It also lists recently used folders.
- **Iteration counter going back:** if you rerun from older data, the plot drops the
  overwritten part, so the newest run wins.
- **Access token:** the token in the link is remembered in a cookie, so you can bookmark
  the page or add it to your phone's home screen. `--new-token` makes old links stop
  working.
- In Fluent you can set the report *plots* inactive; keep the report **files** active.

## Residuals and solver messages

Fluent writes everything it prints to the console into a transcript, `fluent-<date>-<time>-<pid>.trn`.
Fluent 2026 R1 does this automatically ("Auto-Transcript") in its working folder, which is
also where report files with a plain file name (`mach.out`) end up. The monitor reads
transcripts from the folder it watches.

The page offers three data sets from the transcript. They are listed at the top of the file
panel and marked *transcript*:

- **Residuals**: the residual table, live, with log y by default. Equations are whatever
  Fluent prints, species included (e.g. `exhaust`). Earlier Fluent sessions in the folder
  (newest 10 transcripts) are joined on. Re-running from older data replaces the
  overwritten part. Tables from hybrid/FMG initialization are left out.
- **Solver messages**: one dot per iteration with the number of cells affected, for each
  kind of message. For example: *time step reduced (pressure change)*, *time step reduced
  (temperature change)*, *divergence prevention (absolute pressure)*. It uses log y.
- **Printed reports**: report definitions with *Print to Console*, kept out of the residual
  plot. A column counts as a report if a report file in the folder writes that definition,
  or if it has Fluent's default name (`report-def-0`, `…-rdef`).

The page watches the newest session that is still open and iterating. A later short
session, such as one that only interpolated data, doesn't replace it.

**Solver light** in the header:

| light | meaning |
|---|---|
| green *Solver running · it N* | new iterations are arriving |
| green *Converged* | Fluent reported convergence |
| grey *Solver idle* | no new iteration for 5 minutes |
| grey *Fluent closed* | the session's transcript has ended |
| amber | time step reduced, divergence prevention (solution limits hit), turbulent viscosity / temperature / pressure limited |
| red, pulsing | divergence detected (including when Fluent reduces the Courant number or time step and retries), floating point exception, negative cell volume, `Error:` lines |

Amber and red also show a banner above the plots and a mark in the browser tab's title and
icon. Click the light (or *Details*) for the list:

- Messages are grouped by kind, each with:
  - a count
  - the cell count of the latest occurrence
  - the iteration, and how many iterations ago that was (*ongoing* = still happening)
  - the latest line itself
- These are listed but don't change the light:
  - reversed flow
  - "Linear solver stabilized"
  - other `Warning:` lines (mostly graphics/setup, e.g. "Smooth" contours)
- Earlier Fluent sessions are folded away below.

**Mark as seen** turns the light back to green until a *new* message arrives. This is saved
on the server, so it applies to every browser and phone.

## Getting to it from your phone

The monitor only needs to be reachable from the phone. It should **not** be open to the
whole internet: the token protects it, but the traffic is plain HTTP.

- **Laptop and phone on the same network:** start with `--lan` and open the printed
  `http://<laptop address>:8050/?token=…` link on the phone once.
- **Unraid, with its own eduVPN tunnel, reachable over your home network or Tailscale:**
  see [`docker/README.md`](docker/README.md). There the monitor reads the files straight
  from the university's file share (`smb://server/share/folder`).

## Uninstall

Delete this folder. uv's cached Python and packages live in `%LOCALAPPDATA%\uv`;
`uv cache clean` and `uv python uninstall 3.12` remove them.
