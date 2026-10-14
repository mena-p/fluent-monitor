"""
fluent-monitor-web - serve live plots of Fluent report files to any browser (desktop or phone).

    uv run fluent-monitor-web "C:/path/to/case"            # serves on port 8050
    uv run fluent-monitor-web "C:/path/to/case" --port 9000

Open the printed link (it contains an access token) in a browser. The server reads the report
files in a background thread and sends browsers only the rows they don't have yet.
Only Python's standard library + numpy are used here (no Qt).
"""

import argparse
import fnmatch
import gzip
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from .core import (DebugLog, FileReader, ReportFile, get_web_token, load_groups, load_last_folder,
                   save_groups, save_last_folder)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_static")
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".txt": "text/plain; charset=utf-8"}
COOKIE = "fm_token"


# --------------------------------------------------------------------------- data side
class Monitor:
    """Watches all report files of one folder in a background thread."""

    SCAN_EVERY = 5.0

    def __init__(self, folder, pattern, interval, log=None):
        self.folder = folder
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
        self._wake = threading.Event()
        threading.Thread(target=self._run, name="report-reader", daemon=True).start()

    def _run(self):
        while True:
            now = time.time()
            if now - self._last_scan >= self.SCAN_EVERY or not self.listing:
                self._last_scan = now
                try:
                    listing = sorted(f for f in os.listdir(self.folder)
                                     if fnmatch.fnmatch(f.lower(), self.pattern.lower()))
                except OSError:
                    listing = []
                with self.lock:
                    self.listing = listing
                    for name in listing:
                        if name not in self.files:
                            self.files[name] = ReportFile(os.path.join(self.folder, name))
                            self.readers[name] = FileReader(os.path.join(self.folder, name))
            for name in list(self.listing):
                reader = self.readers.get(name)
                if reader is None:
                    continue
                t0 = time.perf_counter()
                events = reader.read()                    # file access outside the lock
                if self.log:
                    self.log.slow(f"reading {name}", t0, 0.05)
                if events:
                    with self.lock:
                        self.files[name].apply(events)
            self.last_read = time.time()
            self._wake.wait(self.interval)
            self._wake.clear()

    @staticmethod
    def _version(rf):
        return f"{rf.layout_version}.{rf.rewinds}"

    def status(self):
        with self.lock:
            files = []
            for name in self.listing:
                rf = self.files.get(name)
                if rf is None:
                    continue
                files.append({"name": name, "v": self._version(rf), "n": rf.n, "columns": rf.columns,
                              "last_change": rf.last_change})
            return {"folder": self.folder, "files": files, "groups": self.groups,
                    "server_time": time.time(), "last_read": self.last_read, "interval": self.interval}

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
        return {"name": name, "v": v, "columns": cols, "start": start, "n": n, "data": block}

    def set_group(self, name, files):
        with self.lock:
            if files:
                self.groups[name] = list(files)
            else:
                self.groups.pop(name, None)
            self.groups_version += 1
            groups = dict(self.groups)
        save_groups(self.folder, groups)


def encode_rows(block):
    """Column-wise list of lists; non-finite values become null (valid JSON)."""
    if block.size and not np.isfinite(block).all():
        return [[float(v) if np.isfinite(v) else None for v in col] for col in block]
    return block.tolist()


# --------------------------------------------------------------------------- HTTP side
class Handler(BaseHTTPRequestHandler):
    server_version = "fluent-monitor"
    monitor: Monitor = None
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
        if gz:
            self.send_header("Content-Encoding", "gzip")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, separators=(",", ":"), allow_nan=False))

    def _authorized(self, query):
        if not self.token:
            return True, None
        given = query.get("token", [None])[0]
        if given == self.token:
            return True, f"{COOKIE}={self.token}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict"
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and v == self.token:
                return True, None
        return False, None

    # ---- routes
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        ok, set_cookie = self._authorized(query)
        if not ok:
            return self._send(403, "<h3>Access denied</h3><p>Open the link printed by fluent-monitor-web "
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
        if not ok:
            return self._json({"error": "access denied"}, 403)
        if url.path != "/api/groups":
            return self._json({"error": "not found"}, 404)
        try:
            n = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(min(n, 100000)) or b"{}")
            name = str(req.get("name", "")).strip()[:120]
            files = [str(f) for f in req.get("files", [])][:50]
        except (ValueError, TypeError):
            return self._json({"error": "bad request"}, 400)
        if not name:
            return self._json({"error": "name missing"}, 400)
        known = set(self.monitor.listing)
        files = [f for f in files if f in known]
        if req.get("action") == "delete":
            self.monitor.set_group(name, [])
        elif len(files) >= 2:
            self.monitor.set_group(name, files)
        else:
            return self._json({"error": "need at least two files"}, 400)
        return self._json({"groups": self.monitor.status()["groups"]})

    def _static(self, rel):
        rel = os.path.normpath(rel).replace("\\", "/")
        if rel.startswith("..") or rel.startswith("/"):
            return self._send(404, "not found", "text/plain")
        path = os.path.join(STATIC_DIR, rel)
        ext = os.path.splitext(path)[1].lower()
        if ext not in STATIC_TYPES or not os.path.isfile(path):
            return self._send(404, "not found", "text/plain")
        with open(path, "rb") as fh:
            body = fh.read()
        return self._send(200, body, STATIC_TYPES[ext])


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
    ap = argparse.ArgumentParser(description="Serve live plots of Fluent report files to browsers.")
    ap.add_argument("folder", nargs="?", help="folder with the .out report files (default: last folder used)")
    ap.add_argument("--port", type=int, default=8050, help="port (default 8050)")
    ap.add_argument("--bind", default="0.0.0.0",
                    help="address to listen on (default: all; use 127.0.0.1 for this computer only)")
    ap.add_argument("--pattern", default="*.out", help="file pattern (default: *.out)")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between file reads (default 1)")
    ap.add_argument("--no-token", action="store_true", help="no access token (anyone who can reach the port can look)")
    ap.add_argument("--new-token", action="store_true", help="create a new access token (old links stop working)")
    ap.add_argument("--verbose", action="store_true", help="log every request")
    ap.add_argument("--debug", nargs="?", const=os.path.join(os.path.expanduser("~"), "fluent_monitor_web_debug.log"),
                    metavar="LOGFILE", help="log slow file reads")
    args = ap.parse_args()

    folder = args.folder
    if folder and os.path.isfile(folder):
        folder = os.path.dirname(folder)
    folder = os.path.abspath(folder) if folder else load_last_folder()
    if not folder or not os.path.isdir(folder):
        sys.exit("Give the folder with the report files, e.g.  fluent-monitor-web \"C:\\path\\to\\case\"")
    save_last_folder(folder)

    Handler.monitor = Monitor(folder, args.pattern, args.interval, DebugLog(args.debug) if args.debug else None)
    Handler.token = None if args.no_token else get_web_token(renew=args.new_token)
    Handler.verbose = args.verbose
    try:
        httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
    except OSError as e:
        sys.exit(f"Cannot listen on {args.bind}:{args.port}: {e}\n(Is it already running? Try --port 8051)")
    httpd.daemon_threads = True

    q = f"/?token={Handler.token}" if Handler.token else "/"
    hosts = [socket.gethostname()] + local_addresses() if args.bind in ("0.0.0.0", "") else [args.bind]
    print(f"fluent-monitor-web: watching {folder}")
    print("Open one of these links in a browser (bookmark it; the token is remembered):")
    for h in hosts:
        print(f"    http://{h}:{args.port}{q}")
    print(f"    http://localhost:{args.port}{q}   (on this computer)")
    print("Stop with Ctrl+C.")
    if sys.platform == "win32" and args.bind != "127.0.0.1":
        print("If Windows asks whether to allow Python through the firewall, allow it for the networks "
              "you want to reach it from.")
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("stopped")


def run():
    main()


if __name__ == "__main__":
    run()
