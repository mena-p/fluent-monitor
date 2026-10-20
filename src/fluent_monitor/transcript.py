"""
Residuals and solver messages from Fluent transcript files (*.trn).

Fluent (2026 R1 by default: "Auto-Transcript") writes everything it prints to the console
into fluent-YYYYMMDD-HHMMSS-PID.trn in its working folder, while it runs. The residual
table looks like

      iter  continuity  x-velocity  y-velocity      energy           k       omega     time/iter
       101  2.3418e-03  1.0275e-04  9.8812e-05  3.1102e-07  2.0456e-04  1.1873e-03  0:00:41  899

Report definitions with "Print to Console" add their own columns (before time/iter); those
go to a second data set ("Printed reports") so the residual plot keeps only residuals.
Other lines are checked against ALERT_RULES (divergence, limited values, reduced time step,
reversed flow, ...).

Qt-free; used by the web server. Data ends up in ordinary ReportFile objects, so plotting,
combining, Last N etc. work exactly as for report files.
"""

import fnmatch
import re
import time

import numpy as np

from .core import LOCAL_FS, ReportFile

TRN_PATTERN = "*.trn"
RESIDUALS = "Residuals"            # names of the two data sets in the file list
PRINTED = "Printed reports"
MESSAGES = "Solver messages"       # cells/faces affected by limiter / time step messages, per iteration
MAX_TRANSCRIPTS = 10               # older sessions are not read (saves time on a slow share)

# Columns of the residual table that are report definitions printed to the console rather than
# equations. Equation names can be anything (species are named by the user, e.g. "exhaust"), so
# report definitions are recognised by name: the definitions found in the folder's report files
# (TranscriptSource.report_names) plus Fluent's default names.
REPORT_NAME = re.compile(r"^(report-def-\d+|.*-rdef|.*-rplot)$", re.I)
_FLOAT = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$")
_RESID = re.compile(r"^[-+]?\d\.\d+[eE][-+]\d+$")       # residual table values: 1.2345e-03
_TIME = re.compile(r"^\d+:\d\d(:\d\d)?$")                  # time/iter: 0:07:43
_NAN = {"nan", "-nan", "+nan", "inf", "-inf", "+inf", "-nan(ind)", "nan(ind)"}
_COUNT = re.compile(r"(\d+)\s+(cells?|faces?)\b", re.I)
_NUMBER = re.compile(r"[-+]?\d[\d.]*(e[-+]?\d+)?%?", re.I)

# (category, severity, title, pattern) - first match wins.
# severity: "critical" (red light), "warning" (amber), "note" (listed only), "good", "info"
ALERT_RULES = [
    ("divergence", "critical", "Divergence detected", r"divergence detected"),
    ("prevention", "warning", "Divergence prevention", r"triggered divergence prevention|solution-limit.{0,40}triggered"),
    ("fpe", "critical", "Floating point exception", r"floating point exception"),
    ("negvol", "critical", "Negative cell volume", r"negative (cell )?volume"),
    ("error", "critical", "Error", r"^\W*error\b"),
    ("timestep", "warning", "Time step reduced", r"reduc\w*\b.{0,60}time[- ]?step|time[- ]?step.{0,60}\breduc"),
    ("turbvisc", "warning", "Turbulent viscosity limited", r"turbulent viscosity limited"),
    ("temperature", "warning", "Temperature limited", r"temperature limited"),
    ("pressure", "warning", "Pressure limited", r"pressure limited"),
    ("amg", "note", "Linear solver stabilized", r"stabiliz\w*.{0,60}(amg|linear solver)|(amg|linear solver).{0,60}stabiliz"),
    ("limited", "warning", "Value limited", r"\blimited to [-+]?\.?\d"),
    # other warnings are mostly about graphics/setup ("Smooth" contours, axis boundary, ...): listed only
    ("warning", "note", "Warning", r"^\W*warning\b"),
    ("reversed", "note", "Reversed flow", r"reversed flow"),
    ("converged", "good", "Solution converged", r"solution is converged"),
    ("complete", "info", "Calculation complete", r"calculation complete"),
    ("stopped", "info", "Calculation stopped", r"^\W*operation stopped"),
]
_RULES = [(c, s, t, re.compile(p, re.I)) for c, s, t, p in ALERT_RULES]
SEVERITY_RANK = {"critical": 3, "warning": 2, "note": 1, "good": 0, "info": 0}


def _num(tok):
    if _FLOAT.match(tok):
        return float(tok)
    if tok.lower() in _NAN:
        return float("nan")
    return None


def message_label(cat, title, text):
    """'Time step reduced (pressure change)', 'Divergence prevention (absolute pressure)', ..."""
    t = text.strip()
    if cat in ("warning", "error"):                        # "Warning: Invalid data at line:" -> its text
        rest = re.sub(r"^\W*(warning|error)\s*:?\s*", "", t, flags=re.I).rstrip(" :.")
        return f"{title}: {rest[:70]}" if rest else title
    m = re.search(r"temporarily reducing (.+?) to (\S+)", t, re.I)
    if m:                                                  # pseudo-transient: Fluent retries itself
        return f"{title} ({m.group(1)} reduced to {m.group(2)})"
    m = re.search(r"due to (?:an? )?(?:excessive )?(.+?)\.?$", t, re.I)
    if m:
        return f"{title} ({m.group(1).strip().lower()})"
    m = re.match(r"(.+?) solution-limit", t, re.I)
    if m:
        return f"{title} ({m.group(1).strip().lower()})"
    m = re.search(r"\bof ([a-z][\w-]*(?: \d+)?)\.?$", t, re.I)       # reversed flow ... of pressure-outlet 5.
    if cat == "reversed" and m:
        return f"{title} ({m.group(1)})"
    return title


class TextTail:
    """Complete lines appended to a text file since the last call."""

    def __init__(self, path, fs=LOCAL_FS):
        self.path, self.fs = path, fs
        self.offset, self.partial = 0, b""
        self.lineno = 0

    def read(self):
        """-> (truncated, [(lineno, text), ...])"""
        try:
            size = self.fs.getsize(self.path)
        except OSError:
            return False, []
        if size < self.offset:
            return True, []
        if size == self.offset:
            return False, []
        try:
            chunk = self.fs.read_range(self.path, self.offset, size - self.offset)
        except OSError:
            return False, []
        self.offset += len(chunk)
        parts = (self.partial + chunk).split(b"\n")
        self.partial = parts.pop()
        out = []
        for raw in parts:
            self.lineno += 1
            out.append((self.lineno, raw.decode("latin-1").rstrip("\r")))
        return False, out


class _Session:
    """Parse state of one transcript file."""

    def __init__(self, name, path, fs):
        self.name = name
        self.tail = TextTail(path, fs)
        self.names = None          # equation / report names of the current table header
        self.last_iter = None
        self.closed = False        # "Auto-Transcript Stop Time" seen: this Fluent session ended
        self.has_rows = False


class TranscriptSource:
    """All transcripts of a folder -> residual data, printed-report data and alerts.

    read() does the file access and parsing (call it outside any lock); apply() puts the
    result into the ReportFile objects (call it under the lock that guards them)."""

    def __init__(self, folder, fs=LOCAL_FS):
        self.folder, self.fs = folder, fs
        self.sessions = {}                 # transcript name -> _Session
        self.order = []                    # transcript names, oldest first
        self.residuals = ReportFile(RESIDUALS)
        self.printed = ReportFile(PRINTED)
        self.messages = ReportFile(MESSAGES)
        self.alerts = AlertLog()
        self.report_names = set()          # report definitions of the folder (lower case), set by the owner
        self.last_iter = None              # newest iteration seen in the newest transcript
        self.last_row_time = None          # when the last residual row arrived
        self._rebuild = False

    @property
    def current(self):
        """The session to watch: the newest one still open that iterated, else the newest that
        iterated (a later short session, e.g. only interpolating, doesn't hide the real run)."""
        for want_open in (True, False):
            for n in reversed(self.order):
                ss = self.sessions[n]
                if ss.has_rows and (not want_open or not ss.closed):
                    return n
        return self.order[-1] if self.order else None

    @property
    def closed(self):
        c = self.current
        return bool(c and self.sessions[c].closed)

    def update_listing(self, names):
        """names: all file names of the folder. Picks up new transcripts."""
        trns = sorted(n for n in names if fnmatch.fnmatch(n.lower(), TRN_PATTERN))[-MAX_TRANSCRIPTS:]
        for n in trns:
            if n not in self.sessions:
                if self.order and n < self.order[-1]:
                    self._rebuild = True       # an older transcript appeared: read all again in order
                self.sessions[n] = _Session(n, self.fs.join(self.folder, n), self.fs)
        self.order = sorted(self.sessions)

    def read(self):
        """-> list of parse results for apply()."""
        if self._rebuild:
            self._rebuild = False
            for n in self.order:
                self.sessions[n] = _Session(n, self.fs.join(self.folder, n), self.fs)
            out = [("reset",)]
        else:
            out = []
        for n in self.order:
            s = self.sessions[n]
            truncated, lines = s.tail.read()
            if truncated:                      # rewritten: start over with everything
                self._rebuild = True
                return out
            if lines:
                out.extend(self._parse(s, lines))
        return out

    def _parse(self, s, lines):
        out = []
        rows, row_names = [], None

        def flush():
            nonlocal rows
            if rows:
                out.append(("rows", s.name, row_names, np.array(rows, dtype=float)))
                rows = []

        for lineno, text in lines:
            toks = text.split()
            if not toks:
                continue
            t0 = toks[0].lower()
            if t0 == "iter" and len(toks) >= 2:          # table header
                flush()
                end = next((i for i, t in enumerate(toks) if t.lower() == "time/iter"), None)
                # only the solver's table has time/iter (initialization tables like
                # "iter scalar-0 scalar-1" of hybrid/FMG initialization don't)
                s.names = toks[1:end] if end else None
                row_names = s.names
                continue
            if t0 == "auto-transcript" and len(toks) > 1 and toks[1].lower() == "stop":
                s.closed = True                            # Fluent was closed
                continue
            if s.names and t0.isdigit() and len(toks) >= len(s.names) + 1:
                # a row has exactly the header's values, written like 1.2345e-03, then time/iter
                # (other number lines, e.g. after "Warning: Invalid data at line:", must not count)
                vt = toks[1:len(s.names) + 1]
                rest = toks[len(s.names) + 1:]
                vals = [_num(t) if (_RESID.match(t) or t.lower() in _NAN) else None for t in vt]
                if None not in vals and (not rest or _TIME.match(rest[0])) and len(rest) <= 2:
                    if row_names is not s.names:
                        flush()
                        row_names = s.names
                    it = int(toks[0])
                    rows.append([it] + vals)
                    s.last_iter = it
                    s.has_rows = True
                    continue
            if (not any(c.isalpha() for c in text) or t0[0] in "(>;/"           # GUI journal / typed commands
                    or t0 == "error" and len(toks) > 1 and toks[1].lower() == "object:"):
                continue
            for cat, sev, title, rx in _RULES:
                if rx.search(text):
                    flush()
                    # Fluent prints messages before the residual row of the iteration they belong to
                    it = s.last_iter + 1 if s.last_iter is not None and cat not in ("converged", "complete", "stopped") \
                        else s.last_iter
                    out.append(("alert", s.name, lineno, it, cat, sev, message_label(cat, title, text), text.strip()))
                    break
        flush()
        return out

    def apply(self, results):
        """Returns True if the data sets changed."""
        changed = False
        now = time.time()
        for r in results:
            if r[0] == "reset":
                self.residuals = ReportFile(RESIDUALS)
                self.printed = ReportFile(PRINTED)
                self.messages = ReportFile(MESSAGES)
                self.alerts = AlertLog()
                self.last_iter = None
                changed = True
            elif r[0] == "rows":
                _, trn, names, arr = r
                rep_idx = [i for i, n in enumerate(names) if self.is_report(n)]
                res_idx = [i for i in range(len(names)) if i not in rep_idx]
                for rf, idx in ((self.residuals, res_idx), (self.printed, rep_idx)):
                    if idx:
                        cols = ["Iteration"] + [names[i] for i in idx]
                        rf.ensure_columns(cols)
                        full = np.full((len(arr), len(rf.columns)), np.nan)
                        full[:, 0] = arr[:, 0]
                        for i in idx:
                            full[:, rf.columns.index(names[i])] = arr[:, i + 1]
                        if rf._add_rows(full):
                            rf.last_change = now
                            changed = True
                if trn == self.current:
                    self.last_iter = int(arr[-1, 0])
                    self.last_row_time = now
            elif r[0] == "alert":
                _, trn, lineno, it, cat, sev, title, text = r
                self.alerts.add(trn, lineno, it, cat, sev, title, text, now)
                m = _COUNT.search(text)
                if m and it is not None and SEVERITY_RANK[sev] >= 1:
                    changed = self._add_message(title, it, float(m.group(1)), now) or changed
        return changed

    def is_report(self, name):
        return name.lower() in self.report_names or bool(REPORT_NAME.match(name))

    def _add_message(self, label, it, amount, now):
        """Cells/faces affected per iteration, one column per kind of message."""
        rf = self.messages
        rf.ensure_columns(["Iteration", label])
        col = rf.columns.index(label)
        if rf.n and rf.buf[0, rf.n - 1] == it:           # another message of the same iteration
            rf.buf[col, rf.n - 1] = amount
        else:
            row = np.full((1, len(rf.columns)), np.nan)
            row[0, 0], row[0, col] = it, amount
            if not rf._add_rows(row):
                return False
        rf.last_change = now
        return True

    def datasets(self):
        """Non-empty data sets: [(name, ReportFile), ...]."""
        return [(n, rf) for n, rf in ((RESIDUALS, self.residuals), (MESSAGES, self.messages),
                                      (PRINTED, self.printed)) if rf.columns]


class AlertLog:
    """Solver messages grouped by kind (numbers stripped, so "in 12 cells" and "in 3 cells"
    are the same group; different boundary zones stay separate)."""

    MAX_GROUPS = 200

    def __init__(self):
        self.groups = {}

    def add(self, trn, lineno, it, cat, sev, title, text, now):
        key = f"{trn}|{cat}|" + _NUMBER.sub("#", (text if cat == "reversed" else title).lower())
        g = self.groups.get(key)
        if g is None:
            if len(self.groups) >= self.MAX_GROUPS:
                return
            g = self.groups[key] = {"key": key, "trn": trn, "cat": cat, "sev": sev, "title": title,
                                    "count": 0, "first_iter": it, "first_pos": [trn, lineno]}
        m = _COUNT.search(text)
        g.update(title=title, count=g["count"] + 1, last_iter=it, last_pos=[trn, lineno], last_text=text[:300],
                 last_time=now, amount=(f"{m.group(1)} {m.group(2)}" if m else None))

    def snapshot(self, current, last_iter, ack):
        """Summary for the browser. ack = [trn, lineno] up to which the user has seen messages."""
        ack = tuple(ack) if ack else ("", 0)
        cur, old = [], []
        for g in self.groups.values():
            d = {k: g[k] for k in ("cat", "sev", "title", "count", "first_iter", "last_iter", "last_text",
                                   "amount", "trn")}
            d["new"] = tuple(g["last_pos"]) > ack
            d["ago"] = (last_iter - g["last_iter"]) if (last_iter is not None and g["last_iter"] is not None
                                                        and g["trn"] == current) else None
            (cur if g["trn"] == current else old).append(d)
        key = lambda d: (-SEVERITY_RANK[d["sev"]], -(d["last_iter"] or 0))
        cur.sort(key=key)
        old.sort(key=key)
        new_sev = [d["sev"] for d in cur if d["new"]]
        level = ("critical" if "critical" in new_sev else "warning" if "warning" in new_sev else "ok")
        return {"level": level, "current": cur, "earlier": old[:50], "transcript": current}

    def last_position(self):
        pos = [tuple(g["last_pos"]) for g in self.groups.values()]
        return list(max(pos)) if pos else None
