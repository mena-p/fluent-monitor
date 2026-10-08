# fluent-monitor

Live plots of Ansys Fluent report files (`*.out`) in one window, as tabs or a grid,
independent of the Fluent GUI. One plot per report file, one curve per quantity.

Everything runs in an isolated environment managed by [uv](https://docs.astral.sh/uv/):
uv downloads its own Python 3.12 and the locked dependencies (matplotlib, numpy) into
`.venv` inside this folder. Nothing is installed into a system Python, and no
compiler is needed (all dependencies ship prebuilt Windows wheels).

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

uv fetches Python 3.12 (with tkinter) and the exact package versions in `uv.lock`
into `.venv`. This step needs internet once; afterwards everything works offline.

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

## Optional: install as a command

To get `fluent-monitor` (with console) and `fluent-monitor-gui` (no console window)
on your PATH in their own isolated environment, run inside this folder:

```powershell
uv tool install .
```

Remove again with `uv tool uninstall fluent-monitor`.

## Using it

- Left: report files in the folder (new ones appear automatically). Tick to plot.
- **Tabs** view: full-size plot with toolbar (zoom, pan, save PNG/PDF/SVG).
- **Grid** view: all ticked plots at once; double-click a plot to open it as a tab.
- Scroll over the tab strip to switch tabs.
- Hover over a curve: a circle marks the nearest data point and a box shows its value.
- Mouse wheel on a plot zooms around the mouse; **Shift**+wheel zooms only the y axis,
  **Ctrl**+wheel only the x axis. Zooming turns *Follow* off so live updates keep your view;
  the toolbar's **Home** button, a double-click on the plot (tabs view) or ticking
  *Follow* returns to the full, auto-scaled view.
- Click a legend entry to hide/show a curve. *Log y*, *Follow* (auto-rescale) and
  *Last N* (only the last N iterations) are per plot.
- If you rerun from an older data file and the iteration counter goes back, the
  plot drops the overwritten part, so the newest run wins.
- In Fluent you can set the report plots inactive; keep the report **files** active.

## Uninstall

Delete this folder. uv's cached Python and packages live in
`%LOCALAPPDATA%\uv`; `uv cache clean` and `uv python uninstall 3.12` remove them.
