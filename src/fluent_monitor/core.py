"""Shared, GUI-independent parts of fluent-monitor: reading Fluent report files, the data
store, settings (combined plots, access token, folders). Used by the server (web.py)."""

import json
import os
import re
import threading
import time

import numpy as np

QUOTED = re.compile(r'"([^"]*)"')
# Columns that are time/iteration counters rather than monitored quantities.
COUNTER_NAMES = ("iteration", "time step", "flow-time", "flow time")

# matplotlib's "tab10" colours; after 10 curves the same colours repeat with another line style
GROUP_PREFIX = "group::"   # key prefix of combined plots (files use their path as key)


def is_counter(name):
    return name.strip().lower() in COUNTER_NAMES


def is_group(key):
    return key.startswith(GROUP_PREFIX)


def short_name(path):
    """'mach-rfile.out' -> 'mach' (for default names of combined plots)."""
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem[:-6] if stem.endswith("-rfile") else stem


def short_label(name):
    """'vel(point-030)' -> 'point-030' keeps single-file legends short; plain names stay."""
    m = re.fullmatch(r"[^()]+\((.+)\)", name)
    return m.group(1) if m else name



# --------------------------------------------------------------------------- data
class LocalFS:
    """File access on this computer (the default). smbfs.SMBFS offers the same API over SMB."""

    join = staticmethod(os.path.join)
    listdir = staticmethod(os.listdir)

    @staticmethod
    def scandir(folder):
        """[(name, is_dir), ...] of a folder."""
        out = []
        with os.scandir(folder) as it:
            for e in it:
                try:
                    out.append((e.name, e.is_dir()))
                except OSError:
                    pass
        return out

    isdir = staticmethod(os.path.isdir)
    getsize = staticmethod(os.path.getsize)

    @staticmethod
    def read_range(path, offset, n):
        with open(path, "rb") as fh:
            fh.seek(offset)
            return fh.read(n)


LOCAL_FS = LocalFS()


class FileReader:
    """Reads what was appended to a report file since the last call and parses it.
    Used only in the background poller thread, so slow disk access never blocks the window."""

    def __init__(self, path, fs=LOCAL_FS):
        self.path = path
        self.fs = fs
        self.offset = 0
        self.partial = b""
        self.ncols = 0

    def read(self):
        """List of events since the last call: ("reset",), ("columns", names), ("rows", array)."""
        try:
            size = self.fs.getsize(self.path)
        except OSError:
            return []
        events = []
        if size < self.offset:                # file truncated / rewritten by Fluent
            self.offset, self.partial, self.ncols = 0, b"", 0
            events.append(("reset",))
        if size == self.offset:
            return events
        try:
            chunk = self.fs.read_range(self.path, self.offset, size - self.offset)
        except OSError:                       # file locked for a moment on Windows
            return events
        self.offset += len(chunk)
        lines = (self.partial + chunk).split(b"\n")
        self.partial = lines.pop()            # keep the incomplete last line for next time
        rows = []

        def flush():
            if rows:
                events.append(("rows", np.array(rows, dtype=float)))
                rows.clear()

        for raw in lines:
            line = raw.decode("latin-1").strip()
            if not line:
                continue
            if line.startswith("("):          # ("Iteration" "mfr(inlet)" ...) -> real column names
                names = QUOTED.findall(line)
                if names:
                    flush()
                    self.ncols = len(names)
                    events.append(("columns", names))
                continue
            if line.startswith('"'):          # file title / short header
                names = QUOTED.findall(line)
                if len(names) > 1:            # "Iteration" "mach": the report definitions in the file
                    events.append(("defs", [n for n in names if not is_counter(n)]))
                continue
            parts = line.split()
            if not self.ncols:                # headerless file: invent names
                self.ncols = len(parts)
                events.append(("columns", ["Iteration"] + [f"col{i}" for i in range(1, len(parts))]))
            if len(parts) != self.ncols:
                continue
            try:
                rows.append([float(p) for p in parts])
            except ValueError:
                continue
        flush()
        return events


class ReportFile:
    """The data of one report file (lives in the GUI thread, filled from FileReader events)."""

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.title = os.path.splitext(self.name)[0]
        self.layout_version = 0    # bumps when the column set changes
        self.defs = []             # names of the report definitions written to this file
        self.rewinds = 0           # bumps when stored rows were replaced (iteration counter went back)
        self._reset()

    def _reset(self):
        self.columns = []          # names from the "( ... )" header line
        self.buf = np.empty((0, 0))  # one row per column, grown by doubling (no per-redraw conversion)
        self.n = 0                 # number of data rows stored
        self.last_change = None    # wall-clock time new data last arrived

    def _set_columns(self, names):
        if names != self.columns:
            self.columns = names
            self.buf = np.empty((len(names), 1024))
            self.n = 0
            self.layout_version += 1

    def ensure_columns(self, names):
        """Add the columns of `names` that are missing; stored data stays, new columns are NaN."""
        if not self.columns:
            self._set_columns(list(names))
            return
        new = [n for n in names if n not in self.columns]
        if new:
            buf = np.full((len(self.columns) + len(new), self.buf.shape[1]), np.nan)
            buf[:len(self.columns)] = self.buf
            self.buf, self.columns = buf, self.columns + new
            self.layout_version += 1

    def col(self, i):
        """Column i as a numpy view (no copy)."""
        return self.buf[i, :self.n]

    def _grow(self, need):
        if need > self.buf.shape[1]:
            grown = np.empty((self.buf.shape[0], max(1024, 2 * need)))
            grown[:, :self.n] = self.buf[:, :self.n]
            self.buf = grown

    def _add_rows(self, arr):
        if not self.columns or arr.shape[1] != len(self.columns):
            return False
        x = arr[:, 0]
        if (self.n == 0 or x[0] > self.buf[0, self.n - 1]) and (len(x) < 2 or np.all(np.diff(x) > 0)):
            self._grow(self.n + len(arr))         # normal case: rows simply continue
            self.buf[:, self.n:self.n + len(arr)] = arr.T
            self.n += len(arr)
            return True
        for row in arr:                           # iteration counter went back somewhere
            # (older data file re-read and run again): the newest history wins
            if self.n and row[0] <= self.buf[0, self.n - 1]:
                self.n = int(np.searchsorted(self.buf[0, :self.n], row[0], "left"))
                self.rewinds += 1
            self._grow(self.n + 1)
            self.buf[:, self.n] = row
            self.n += 1
        return True

    def apply(self, events):
        """Apply FileReader events. Returns True if anything changed."""
        changed = False
        for ev in events:
            if ev[0] == "reset":
                self._reset()
                self.layout_version += 1
                changed = True
            elif ev[0] == "columns":
                self._set_columns(ev[1])
                changed = True
            elif ev[0] == "rows":
                changed = self._add_rows(ev[1]) or changed
            elif ev[0] == "defs":
                self.defs = ev[1]
        if changed:
            self.last_change = time.time()
        return changed


class DebugLog:
    """--debug: writes how long each step takes and when the window was blocked."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self.write(f"--- fluent-monitor debug log, started {time.strftime('%Y-%m-%d %H:%M:%S')} ---")

    def write(self, msg):
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(f"{time.strftime('%H:%M:%S')}  [{threading.current_thread().name}]  {msg}\n")
            except OSError:
                pass

    def slow(self, what, t0, limit):
        dt = time.perf_counter() - t0
        if dt >= limit:
            self.write(f"{what}: {dt * 1000:.0f} ms")


# --------------------------------------------------------------------------- settings
# ~/.fluent_monitor.json: {"last_folder": "...", "groups": {"<folder>": {"<name>": ["a.out", ...]}}}
SETTINGS = os.path.join(os.path.expanduser("~"), ".fluent_monitor.json")
OLD_SETTINGS = os.path.join(os.path.expanduser("~"), ".fluent_monitor_last_folder.txt")


def get_web_token(renew=False):
    """Access token of the web server, created once and kept in the settings file
    (so a bookmarked link keeps working across restarts)."""
    import secrets
    with _settings_lock:
        d = _load_settings()
        tok = d.get("web_token")
        if renew or not tok:
            tok = secrets.token_urlsafe(16)
            d["web_token"] = tok
            _save_settings(d)
    return tok


def _load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


_settings_lock = threading.RLock()   # the web server saves from several threads


def _save_settings(d):
    try:
        with open(SETTINGS, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=1)
    except OSError:
        pass


def _folder_key(folder):
    return os.path.normcase(os.path.abspath(folder))


def load_last_folder():
    d = _load_settings().get("last_folder")
    if not d:   # settings file of the first version
        try:
            with open(OLD_SETTINGS, encoding="utf-8") as fh:
                d = fh.read().strip()
        except OSError:
            d = None
    return d if d and os.path.isdir(d) else None


def save_last_folder(folder):
    d = _load_settings()
    d["last_folder"] = folder
    _save_settings(d)


def load_groups(folder):
    g = _load_settings().get("groups", {}).get(_folder_key(folder), {})
    return {str(k): [str(f) for f in v] for k, v in g.items() if isinstance(v, list)}


def load_web_folder():
    """Folder last chosen in the web app, and the recently used ones (newest first)."""
    d = _load_settings()
    recent = [str(f) for f in d.get("web_recent", []) if isinstance(f, str)]
    return d.get("web_folder"), recent


def save_web_folder(folder, keep=10):
    with _settings_lock:
        d = _load_settings()
        d["web_folder"] = folder
        d["web_recent"] = ([folder] + [f for f in d.get("web_recent", []) if f != folder])[:keep]
        _save_settings(d)


def get_setting(key, default=None):
    return _load_settings().get(key, default)


def set_setting(key, value):
    with _settings_lock:
        d = _load_settings()
        d[key] = value
        _save_settings(d)


def save_groups(folder, groups):
    with _settings_lock:
        d = _load_settings()
        all_groups = d.setdefault("groups", {})
        if groups:
            all_groups[_folder_key(folder)] = groups
        else:
            all_groups.pop(_folder_key(folder), None)
        _save_settings(d)
