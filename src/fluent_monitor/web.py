"""
fluent-monitor - live plots of Ansys Fluent report files, residuals and solver messages in the browser.

    uv run fluent-monitor                          # last folder; opens the page in your browser
    uv run fluent-monitor "C:/path/to/case"        # that folder
    uv run fluent-monitor --lan                    # also reachable from other devices (phone)

A small web server: it reads the report files and Fluent's transcript in a background thread and
sends browsers only the rows they don't have yet. Python's standard library + numpy (+ smbprotocol
for smb:// folders). The same program runs in Docker (see docker/README.md).
"""

import argparse
import fnmatch
import gzip
import hmac
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from .core import (LOCAL_FS, _folder_key, DebugLog, FileReader, ReportFile, get_web_token, load_groups,
                   get_setting, load_last_folder, load_web_folder, save_groups, save_last_folder,
                   save_web_folder, set_setting)
from .smbfs import is_smb_path, normalize
from .transcript import TranscriptSource

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_static")
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".txt": "text/plain; charset=utf-8"}
COOKIE = "fm_token"
STATIC_FILES = frozenset(os.listdir(STATIC_DIR))      # the only files /static/ serves
MAX_BODY = 100_000                                     # bytes accepted in a POST
MAX_CONNECTIONS = 128                                  # connections handled at the same time
MAX_PER_IP = 32                                        # ... and at most this many from one address
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
                                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
}


# --------------------------------------------------------------------------- data side
class Monitor:
    """Watches all report files of one folder in a background thread."""

    SCAN_EVERY = 5.0

    def __init__(self, folder, pattern, interval, log=None, fs=LOCAL_FS):
        self.folder = folder
        self.fs = fs
        self.pattern = pattern
        self.interval = interval
        self.log = log
        self.lock = threading.Lock()
        self.files = {}          # name -> ReportFile
        self.readers = {}        # name -> FileReader
        self.listing = []
        self.groups = load_groups(folder)
        self.groups_version = 0
        self.last_read = None    # time of the last completed read pass
        self._last_scan = 0.0
        self.list_error = None
        self.epoch = 1           # +1 whenever the folder changes; browsers drop data of older epochs
        self.trn = TranscriptSource(folder, fs)   # residuals + solver messages from *.trn
        self.virtual = []        # names of the transcript data sets ("Residuals", ...)
        self.ack = self._load_ack(folder)
        self._wake = threading.Event()
        threading.Thread(target=self._run, name="report-reader", daemon=True).start()

    def set_folder(self, folder):
        """Watch another folder from now on (the reader thread starts over)."""
        groups = load_groups(folder)
        with self.lock:
            self.folder = folder
            self.files, self.readers, self.listing = {}, {}, []
            self.trn = TranscriptSource(folder, self.fs)
            self.virtual = []
            self.ack = self._load_ack(folder)
            self.groups = groups
            self.list_error = None
            self.last_read = None
            self._last_scan = 0.0
            self.epoch += 1
        self._wake.set()
        print(f"now watching {folder}", flush=True)

    def _run(self):
        while True:
            try:
                wait = self._pass()
            except Exception as e:            # one bad file must not stop the monitoring
                print(f"error while reading {self.folder}: {type(e).__name__}: {e}", flush=True)
                wait = True
            if wait:
                self._wake.wait(self.interval)
                self._wake.clear()

    def _pass(self):
        """One read pass over the folder. False: the folder changed meanwhile, start over at once."""
        with self.lock:
            epoch, folder = self.epoch, self.folder
        now = time.time()
        if now - self._last_scan >= self.SCAN_EVERY or not self.listing:
            self._last_scan = now
            error = None
            try:
                names = self.fs.listdir(folder)
                listing = sorted(f for f in names if fnmatch.fnmatch(f.lower(), self.pattern.lower()))
            except OSError as e:          # e.g. VPN down: keep the data we have, retry later
                error, listing, names = str(e), None, None
            with self.lock:
                if epoch != self.epoch:   # folder changed meanwhile: start over at once
                    return False
                if error:
                    if error != self.list_error:
                        print(f"cannot list {folder}: {error}  (retrying)", flush=True)
                    self.list_error = error
                else:
                    if self.list_error:
                        print(f"folder reachable again ({len(listing)} report files)", flush=True)
                    self.list_error = None
                    self.listing = listing
                    self.trn.update_listing(names)
                    for name in listing:
                        if name not in self.files:
                            path = self.fs.join(folder, name)
                            self.files[name] = ReportFile(path)
                            self.readers[name] = FileReader(path, self.fs)
        with self.lock:
            todo = [] if self.list_error or epoch != self.epoch else \
                [(n, self.readers[n], self.files[n]) for n in self.listing if n in self.readers]
        for name, reader, rf in todo:
            t0 = time.perf_counter()
            events = reader.read()                    # file access outside the lock
            if self.log:
                self.log.slow(f"reading {name}", t0, 0.05)
            if events:
                with self.lock:
                    if epoch != self.epoch:
                        break
                    rf.apply(events)
        with self.lock:
            trn = self.trn if epoch == self.epoch and not self.list_error else None
        if trn is not None:
            with self.lock:          # report definitions of the folder: not residuals
                trn.report_names = {d.lower() for n in self.listing if n in self.files
                                    for d in self.files[n].defs}
            t0 = time.perf_counter()
            results = trn.read()                       # transcripts: residuals + messages
            if self.log:
                self.log.slow("reading transcripts", t0, 0.05)
            with self.lock:
                if epoch == self.epoch and results:
                    trn.apply(results)
                    sets = trn.datasets()
                    self.virtual = [n for n, _ in sets]
                    self.files.update(sets)
        with self.lock:
            if epoch == self.epoch:
                self.last_read = time.time()
        return True

    @staticmethod
    def _version(rf):
        return f"{rf.layout_version}.{rf.rewinds}"

    def status(self):
        with self.lock:
            files = []
            for name in self.virtual + self.listing:
                rf = self.files.get(name)
                if rf is None:
                    continue
                files.append({"name": name, "v": self._version(rf), "n": rf.n, "columns": rf.columns,
                              "last_change": rf.last_change,
                              "kind": "transcript" if name in self.virtual else "report"})
            return {"folder": self.folder, "epoch": self.epoch, "files": files, "groups": self.groups,
                    "server_time": time.time(), "last_read": self.last_read, "interval": self.interval,
                    "error": self.list_error, "solver": self._solver()}

    # ---- solver health from the transcript
    IDLE_AFTER = 300.0

    def _solver(self):
        """Called with the lock held."""
        t = self.trn
        if not t.current:
            return {"state": "none"}
        snap = t.alerts.snapshot(t.current, t.last_iter, self.ack)
        age = time.time() - t.last_row_time if t.last_row_time else None
        conv = next((g for g in snap["current"] if g["cat"] == "converged"), None)
        if snap["level"] != "ok":
            state = snap["level"]
        elif t.closed:
            state = "closed"                      # "Auto-Transcript Stop Time": Fluent was closed
        elif conv and conv["ago"] is not None and conv["ago"] <= 1:
            state = "converged"
        elif age is None or age > self.IDLE_AFTER:
            state = "idle"
        else:
            state = "running"
        snap.update(state=state, iteration=t.last_iter, age=age)
        return snap

    def acknowledge(self):
        with self.lock:
            pos = self.trn.alerts.last_position()
            if pos:
                self.ack = pos
            folder, ack = self.folder, self.ack
        acks = get_setting("alert_ack", {}) or {}
        acks[_folder_key(folder)] = ack
        set_setting("alert_ack", acks)

    @staticmethod
    def _load_ack(folder):
        return (get_setting("alert_ack", {}) or {}).get(_folder_key(folder))

    def data(self, name, version, start):
        """Rows from `start` on (all rows if the client's version is outdated), column-wise."""
        with self.lock:
            rf = self.files.get(name)
            if rf is None:
                return None
            v = self._version(rf)
            if version != v or start > rf.n or start < 0:
                start = 0
            block = rf.buf[:, start:rf.n].copy() if rf.columns else np.empty((0, 0))
            cols = list(rf.columns)
            n = rf.n
            epoch = self.epoch
        return {"name": name, "epoch": epoch, "v": v, "columns": cols, "start": start, "n": n, "data": block}

    def set_group(self, name, files):
        with self.lock:
            if files:
                self.groups[name] = list(files)
            else:
                self.groups.pop(name, None)
            self.groups_version += 1
            groups = dict(self.groups)
            folder = self.folder
        save_groups(folder, groups)


class Browser:
    """Lets the web page pick another case folder, but only below `root`."""

    SKIP = {"$recycle.bin", "system volume information", "__pycache__", "node_modules"}

    def __init__(self, fs, root, smb, pattern):
        self.fs, self.smb, self.pattern = fs, smb, pattern
        self.root = normalize(root) if smb else os.path.abspath(root)

    def _key(self, path):
        return (normalize(path) if self.smb else os.path.normcase(os.path.abspath(path))).rstrip("/\\").lower()

    def rel(self, folder):
        """'a/b' for root/a/b, '' for the root itself, None if outside the root."""
        if folder is None:
            return None
        r, f = self._key(self.root), self._key(folder)
        if f == r:
            return ""
        sep = "/" if self.smb else os.sep
        if r.endswith(sep):       # local filesystem root like C:\ or /
            r = r[:-1]
        if not f.startswith(r + sep):
            return None
        tail = (normalize(folder) if self.smb else os.path.abspath(folder))[len(r) + 1:]
        return tail.replace("\\", "/").strip("/")

    def resolve(self, rel):
        parts = [p for p in str(rel or "").replace("\\", "/").split("/") if p not in ("", ".")]
        if any(p == ".." or ":" in p or "\x00" in p for p in parts):
            raise ValueError("invalid folder")
        path = self.root
        for p in parts:
            path = self.fs.join(path, p)
        if not self.smb:          # a link / junction inside the root must not lead out of it
            real, root = os.path.realpath(path), os.path.realpath(self.root)
            if real != root and not real.startswith(root.rstrip(os.sep) + os.sep):
                raise ValueError("this folder links to a place outside the allowed folders")
        return path

    def listing(self, rel):
        path = self.resolve(rel)
        dirs, reports = [], 0
        for name, is_dir in self.fs.scandir(path):
            if is_dir:
                if not name.startswith(".") and name.lower() not in self.SKIP:
                    dirs.append(name)
            elif fnmatch.fnmatch(name.lower(), self.pattern.lower()):
                reports += 1
        dirs.sort(key=str.lower)
        return path, dirs, reports


def encode_rows(block):
    """Column-wise list of lists; non-finite values become null (valid JSON)."""
    if block.size and not np.isfinite(block).all():
        return [[float(v) if np.isfinite(v) else None for v in col] for col in block]
    return block.tolist()


# --------------------------------------------------------------------------- HTTP side
class Handler(BaseHTTPRequestHandler):
    server_version = "fluent-monitor"
    sys_version = ""             # don't advertise the Python version
    timeout = 20                 # seconds a client may stay silent before its connection is closed
    monitor: Monitor = None
    browser: Browser = None
    token = None
    verbose = False

    def log_message(self, fmt, *args):
        if self.verbose:
            super().log_message(fmt, *args)

    # ---- helpers
    def _send(self, code, body, ctype="application/json", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        gz = len(body) > 1500 and "gzip" in self.headers.get("Accept-Encoding", "")
        if gz:
            body = gzip.compress(body, compresslevel=5)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        if gz:
            self.send_header("Content-Encoding", "gzip")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                pass                  # the browser gave up on this request (tab closed, reload, stopped page)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, separators=(",", ":"), allow_nan=False))

    def _authorized(self, query):
        if not self.token:
            return True, None
        given = query.get("token", [None])[0]
        if given is not None and hmac.compare_digest(given.encode(), self.token.encode()):
            return True, f"{COOKIE}={self.token}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict"
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and hmac.compare_digest(v.encode(), self.token.encode()):
                return True, None
        return False, None

    def _same_origin(self):
        """Refuse requests a browser sends on behalf of another site (CSRF). Browsers send
        Origin on POSTs; it must name this server."""
        origin = self.headers.get("Origin")
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            return False
        if origin is None:
            return True               # not a browser (e.g. curl with the token)
        return urllib.parse.urlsplit(origin).netloc.lower() == self.headers.get("Host", "").lower()

    # ---- routes
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query, keep_blank_values=True)
        ok, set_cookie = self._authorized(query)
        if not ok:
            return self._send(403, "<h3>Access denied</h3><p>Open the link printed by fluent-monitor "
                                   "(it ends with <code>?token=...</code>) once in this browser.</p>",
                              "text/html; charset=utf-8")
        if set_cookie and url.path == "/":
            # remember the token in a cookie and show a clean URL
            return self._send(303, "", "text/plain", {"Set-Cookie": set_cookie, "Location": "/"})
        if url.path == "/":
            return self._static("index.html")
        if url.path.startswith("/static/"):
            return self._static(url.path[len("/static/"):])
        if url.path == "/api/status":
            return self._json(self.monitor.status())
        if url.path == "/api/dirs":
            return self._dirs(query.get("path", [None])[0])
        if url.path == "/api/data":
            name = query.get("name", [""])[0]
            version = query.get("v", [""])[0]
            try:
                start = int(query.get("from", ["0"])[0])
            except ValueError:
                start = 0
            d = self.monitor.data(name, version, start)
            if d is None:
                return self._json({"error": "unknown file"}, 404)
            d["data"] = encode_rows(d["data"])
            return self._json(d)
        return self._send(404, "not found", "text/plain")

    do_HEAD = do_GET

    def do_POST(self):
        url = urllib.parse.urlsplit(self.path)
        ok, _ = self._authorized(urllib.parse.parse_qs(url.query))
        if not ok or not self._same_origin():
            self.close_connection = True
            return self._json({"error": "access denied"}, 403)
        if self.headers.get_content_type() != "application/json":
            self.close_connection = True
            return self._json({"error": "JSON expected"}, 415)
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        if not 0 <= n <= MAX_BODY:
            self.close_connection = True
            return self._json({"error": "bad request size"}, 413 if n > MAX_BODY else 400)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(req, dict):
                raise ValueError
        except (ValueError, TypeError, UnicodeDecodeError, RecursionError):
            # RecursionError: deeply nested JSON. Treat every parse failure as a bad request
            # rather than letting it become an unhandled traceback.
            return self._json({"error": "bad request"}, 400)
        except TimeoutError:              # announced more bytes than it sent
            self.close_connection = True
            return
        if url.path == "/api/folder":
            return self._set_folder(req.get("path"))
        if url.path == "/api/alerts/ack":
            self.monitor.acknowledge()
            return self._json({"ok": True})
        if url.path != "/api/groups":
            return self._json({"error": "not found"}, 404)
        name, files = req.get("name", ""), req.get("files", [])
        if not isinstance(name, str) or not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            return self._json({"error": "bad request"}, 400)
        name, files = name.strip()[:120], files[:50]
        if not name:
            return self._json({"error": "name missing"}, 400)
        known = set(self.monitor.listing) | set(self.monitor.virtual)
        files = [f for f in files if f in known]
        if req.get("action") == "delete":
            self.monitor.set_group(name, [])
        elif len(files) >= 2:
            self.monitor.set_group(name, files)
        else:
            return self._json({"error": "need at least two files"}, 400)
        return self._json({"groups": self.monitor.status()["groups"]})

    # ---- case folder
    def _dirs(self, rel):
        b, m = self.browser, self.monitor
        current = b.rel(m.folder)
        if rel is None:
            rel = current if current is not None else ""
        try:
            _, dirs, reports = b.listing(rel)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except OSError as e:
            return self._json({"error": f"cannot open this folder: {e}", "path": b.rel(b.resolve(rel)),
                               "root": b.root}, 502)
        _, recent = load_web_folder()
        recent = [r for r in (b.rel(f) for f in recent) if r is not None]
        return self._json({"root": b.root, "path": b.rel(b.resolve(rel)), "dirs": dirs, "reports": reports,
                           "current": current, "recent": recent})

    def _set_folder(self, rel):
        b = self.browser
        try:
            path = b.resolve(rel)
            if not b.fs.isdir(path):
                return self._json({"error": "folder not found"}, 404)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except OSError as e:
            return self._json({"error": f"cannot open this folder: {e}"}, 502)
        self.monitor.set_folder(path)
        save_web_folder(path)
        return self._json({"folder": path, "epoch": self.monitor.epoch})

    def _static(self, rel):
        # only the files shipped in web_static, by exact name (no paths: on Windows a request for
        # /static/C:/... would otherwise reach any drive)
        ext = os.path.splitext(rel)[1].lower()
        if rel not in STATIC_FILES or ext not in STATIC_TYPES:
            return self._send(404, "not found", "text/plain")
        path = os.path.join(STATIC_DIR, rel)
        with open(path, "rb") as fh:
            body = fh.read()
        return self._send(200, body, STATIC_TYPES[ext])


class Server(ThreadingHTTPServer):
    daemon_threads = True
    _slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
    _per_ip = {}                       # client ip -> open connections from it
    _per_ip_lock = threading.Lock()

    def process_request(self, request, client_address):
        """At most MAX_CONNECTIONS connections at once, and at most MAX_PER_IP from any single
        address. A connection that never finishes its request (slowloris) only holds a slot until
        `timeout`; capping per address keeps one remote host from grabbing them all meanwhile."""
        ip = client_address[0] if client_address else ""
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self._per_ip_lock:
            if self._per_ip.get(ip, 0) >= MAX_PER_IP:
                self._slots.release()
                self.shutdown_request(request)
                return
            self._per_ip[ip] = self._per_ip.get(ip, 0) + 1
        try:
            super().process_request(request, client_address)
        except Exception:
            self._release(ip)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(client_address[0] if client_address else "")

    def _release(self, ip):
        with self._per_ip_lock:
            left = self._per_ip.get(ip, 0) - 1
            if left > 0:
                self._per_ip[ip] = left
            else:
                self._per_ip.pop(ip, None)
        self._slots.release()

    def handle_error(self, request, client_address):
        """Browsers drop connections all the time (reload, closed tab): no traceback for those."""
        if isinstance(sys.exc_info()[1], (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def local_addresses():
    addrs = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    try:   # the address used for outgoing traffic (no packet is sent)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(a for a in addrs if not a.startswith("127."))


def main():
    ap = argparse.ArgumentParser(prog="fluent-monitor",
                                 description="Live plots of Fluent report files, residuals and solver messages "
                                             "in the browser.")
    ap.add_argument("folder", nargs="?",
                    help="folder with the .out report files (Fluent's working folder), or smb://server/share/path "
                         "(credentials in SMB_USERNAME / SMB_PASSWORD / SMB_DOMAIN); default: the folder last "
                         "chosen in the page, otherwise $FLUENT_FOLDER, otherwise your home folder. You can switch "
                         "folders in the page.")
    ap.add_argument("--root", default=os.environ.get("FM_ROOT") or None,
                    help="the web page may switch to any folder below this one (default: the share for "
                         "smb:// folders, otherwise the drive / filesystem root); $FM_ROOT")
    ap.add_argument("--port", type=int, default=8050, help="port (default 8050)")
    ap.add_argument("--lan", action="store_true",
                    help="also accept connections from other devices (phone, other PCs); default: this computer only")
    ap.add_argument("--bind", default=os.environ.get("FM_BIND") or None,
                    help="address to listen on (default 127.0.0.1, with --lan 0.0.0.0); $FM_BIND")
    ap.add_argument("--no-browser", action="store_true", default=bool(os.environ.get("FM_NO_BROWSER")),
                    help="don't open the page in the browser at start; $FM_NO_BROWSER")
    ap.add_argument("--pattern", default="*.out", help="file pattern (default: *.out)")
    ap.add_argument("--interval", type=float, default=float(os.environ.get("FM_INTERVAL", 1.0)),
                    help="seconds between file reads (default 1)")
    ap.add_argument("--public-url", default=os.environ.get("FM_PUBLIC_URL"),
                    help="address browsers use to reach this server, e.g. http://tower:8050 (only for the "
                         "printed link; useful in Docker)")
    ap.add_argument("--no-token", action="store_true", help="no access token (anyone who can reach the port can look)")
    ap.add_argument("--new-token", action="store_true", help="create a new access token (old links stop working)")
    ap.add_argument("--verbose", action="store_true", help="log every request")
    ap.add_argument("--debug", nargs="?", const=os.path.join(os.path.expanduser("~"), "fluent_monitor_web_debug.log"),
                    metavar="LOGFILE", help="log slow file reads")
    args = ap.parse_args()
    if not args.bind:
        args.bind = "0.0.0.0" if args.lan else "127.0.0.1"

    env_folder = os.environ.get("FLUENT_FOLDER") or None
    saved, _ = load_web_folder()
    if env_folder and env_folder != get_setting("web_env_folder"):
        saved = None                  # $FLUENT_FOLDER was changed since the last start: it wins once
        set_setting("web_env_folder", env_folder)
    base = args.folder or env_folder or args.root or saved or load_last_folder() or os.path.expanduser("~")
    smb = is_smb_path(base)
    if smb:
        from .smbfs import SMBFS
        fs = SMBFS()
        if not os.environ.get("SMB_USERNAME"):
            print("warning: SMB_USERNAME is not set; trying a guest login", flush=True)
    else:
        fs = LOCAL_FS
        if os.path.isfile(base):
            base = os.path.dirname(base)
    if args.root:
        root = args.root
    elif smb:
        root = SMBFS.root(base)
    else:
        root = os.path.splitdrive(os.path.abspath(base))[0] + os.sep
    browser = Browser(fs, root, smb, args.pattern)

    def usable(f):
        return bool(f) and is_smb_path(f) == smb and browser.rel(f) is not None
    # explicit argument > folder chosen in the web page (unless $FLUENT_FOLDER changed) > $FLUENT_FOLDER
    # > last folder > root
    folder = next((f for f in (args.folder, saved if usable(saved) else None, env_folder,
                               None if smb else load_last_folder(), base, browser.root) if f and is_smb_path(f) == smb))
    fallback = False
    if smb:
        folder = normalize(folder)
    else:
        if os.path.isfile(folder):
            folder = os.path.dirname(folder)
        folder = os.path.abspath(folder)
        if not os.path.isdir(folder):        # renamed, moved or a drive that isn't connected
            missing, folder, fallback = folder, existing_parent(folder), True
            print(f"note: {missing} doesn't exist (any more); starting in {folder} instead. "
                  f"Choose the case with \"Change folder…\" in the page.", flush=True)
            if not args.root and browser.rel(folder) is None:   # e.g. the whole drive is gone
                browser = Browser(fs, os.path.splitdrive(folder)[0] + os.sep, smb, args.pattern)
        if not fallback:              # keep remembering the real case (e.g. a drive connected later)
            save_last_folder(folder)
    if fallback:
        pass
    elif browser.rel(folder) is not None:
        save_web_folder(folder)       # appears under "Recent" in the folder dialog
    else:
        print(f"warning: {folder} is outside {browser.root}; the web page can only switch to folders "
              f"inside {browser.root}", flush=True)

    home = os.path.expanduser("~")
    if not os.access(home, os.W_OK):
        print(f"warning: cannot write to {home}: the access token and settings are not kept between starts "
              f"(in Docker: chown 99:100 the data folder)", flush=True)
    token = None if args.no_token else get_web_token(renew=args.new_token)
    q = f"/?token={token}" if token else "/"
    local_url = f"http://localhost:{args.port}{q}"
    try:
        httpd = Server((args.bind, args.port), Handler)
    except OSError as e:
        if _already_running(args.port):
            print(f"fluent-monitor is already running on port {args.port}; opening it in the browser.")
            if not args.no_browser:
                webbrowser.open(local_url)
            return
        sys.exit(f"Cannot listen on {args.bind}:{args.port}: {e}\n(Another program uses the port? Try --port 8051)")
    httpd.daemon_threads = True
    Handler.monitor = Monitor(folder, args.pattern, args.interval, DebugLog(args.debug) if args.debug else None, fs)
    Handler.browser = browser
    Handler.token = token
    Handler.verbose = args.verbose

    print(f"fluent-monitor: watching {folder}  (the page can switch to folders inside {browser.root})")
    if args.bind == "127.0.0.1":
        print(f"Open in a browser on this computer:  {local_url}")
        print("(Start with --lan to reach it from your phone or another PC too.)")
    else:
        print("Open one of these links in a browser (bookmark it; the token is remembered):")
        if args.public_url:
            print(f"    {args.public_url.rstrip('/')}{q}")
        else:
            hosts = [socket.gethostname()] + local_addresses() if args.bind in ("0.0.0.0", "") else [args.bind]
            for h in hosts:
                print(f"    http://{h}:{args.port}{q}")
            print(f"    {local_url}   (on this computer)")
        if sys.platform == "win32":
            print("If Windows asks whether to allow Python through the firewall, allow it for the networks "
                  "you want to reach it from.")
    if not args.no_browser:                         # interactive start (not Docker)
        print("Keep this window open while you use the page. Stop with Ctrl+C or by closing the window.")
    sys.stdout.flush()
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(local_url)).start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("stopped")
    sys.stdout.flush()


def existing_parent(path):
    """The nearest folder above `path` that exists, else the home folder."""
    p = os.path.abspath(path)
    while True:
        parent = os.path.dirname(p)
        if os.path.isdir(parent):
            return parent
        if parent == p:
            return os.path.expanduser("~")
        p = parent


def _already_running(port):
    """Is a fluent-monitor server answering on this port of this computer?"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2) as r:
            return r.headers.get("Server", "").startswith("fluent-monitor")
    except urllib.error.HTTPError as e:              # 403 without token: still ours if the header says so
        return e.headers.get("Server", "").startswith("fluent-monitor")
    except OSError:
        return False


def run():
    main()


if __name__ == "__main__":
    run()
