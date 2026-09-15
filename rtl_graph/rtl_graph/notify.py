"""Build-status signal for the web UI.

The graph pipeline is a batch job: a build takes seconds to minutes and the UI must not
draw a half-written `graph.json`.  Two things carry the signal:

  * `<out_dir>/status.json` -- the source of truth, written atomically (tmp + os.replace)
    by `begin()` / `finish()` / `fail()` around a build.  `generation` increments once per
    successful build, so a consumer only has to compare integers.
  * a small read-only HTTP server (`serve()`, `rtl_graph serve`) that publishes that file
    as an endpoint: poll `/api/status`, long-poll `/api/status?since=N&wait=30`, or
    subscribe to `/api/events` (SSE).  Optionally the build also POSTs the status document
    to a webhook (`--notify-url`) so the Node backend can be told rather than ask.

No third-party dependencies: the UI side is expected to be the Node/web half.
See README section 4 for the endpoint contract.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

STATUS_FILE = "status.json"
DESIGN_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SCHEMA = 1


# --------------------------------------------------------------------------- status file
def status_path(out_dir: str) -> str:
    return os.path.join(out_dir, STATUS_FILE)


def read_status(out_dir: str) -> Optional[dict]:
    """The current status document, or None if this directory was never built."""
    try:
        with open(status_path(out_dir)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_status(out_dir: str, doc: dict) -> str:
    """Atomic write: a reader never sees a partial document."""
    os.makedirs(out_dir, exist_ok=True)
    p = status_path(out_dir)
    tmp = p + f".tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    return p


def begin(out_dir: str, design: Optional[str] = None, **extra) -> dict:
    """Mark a build as started.  `generation` keeps the value of the last good build."""
    old = read_status(out_dir) or {}
    doc = {"schema": SCHEMA, "state": "building", "generation": int(old.get("generation", 0)),
           "design": design or old.get("design"), "out_dir": os.path.abspath(out_dir),
           "started": time.time(), "started_iso": _iso(), "finished": None, "finished_iso": None,
           "duration_s": None, "error": None, "files": {}, "stats": {}, **extra}
    write_status(out_dir, doc)
    return doc


def finish(out_dir: str, design: Optional[str] = None, stats: Optional[dict] = None,
           files: Optional[Dict[str, str]] = None, notify_url: Optional[str] = None, **extra) -> dict:
    """Mark the graph as complete and ready to display; bumps `generation`."""
    old = read_status(out_dir) or {}
    now = time.time()
    started = old.get("started")
    doc = {"schema": SCHEMA, "state": "ready", "generation": int(old.get("generation", 0)) + 1,
           "design": design or old.get("design"), "out_dir": os.path.abspath(out_dir),
           "started": started, "started_iso": old.get("started_iso"), "finished": now, "finished_iso": _iso(now),
           "duration_s": round(now - started, 3) if started else None, "error": None,
           "files": files or _found_files(out_dir), "stats": stats or {}, **extra}
    write_status(out_dir, doc)
    if notify_url:
        post_webhook(notify_url, doc)
    return doc


def fail(out_dir: str, error: str, design: Optional[str] = None, notify_url: Optional[str] = None, **extra) -> dict:
    """Mark the build as failed; `generation` stays on the last good build."""
    old = read_status(out_dir) or {}
    now = time.time()
    doc = {"schema": SCHEMA, "state": "error", "generation": int(old.get("generation", 0)),
           "design": design or old.get("design"), "out_dir": os.path.abspath(out_dir),
           "started": old.get("started"), "started_iso": old.get("started_iso"),
           "finished": now, "finished_iso": _iso(now),
           "duration_s": round(now - old["started"], 3) if old.get("started") else None,
           "error": str(error)[:2000], "files": {}, "stats": {}, **extra}
    write_status(out_dir, doc)
    if notify_url:
        post_webhook(notify_url, doc)
    return doc


def _iso(t: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t if t is not None else time.time()))


def _found_files(out_dir: str) -> Dict[str, str]:
    names = ("graph.json", "graph.slim.json", "graph.dot", "graph.svg", "graph.graphml", "summary.txt")
    return {n: n for n in names if os.path.exists(os.path.join(out_dir, n))}


# --------------------------------------------------------------------------- outgoing webhook
def post_webhook(url: str, doc: dict, timeout: float = 5.0) -> bool:
    """Best-effort POST of the status document.  Never raises: a missing UI must not fail a build."""
    body = json.dumps(doc).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "rtl_graph", "X-RTL-Graph-Event": doc.get("state", "")})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


# --------------------------------------------------------------------------- HTTP endpoint
class _Handler(BaseHTTPRequestHandler):
    server_version = "rtl_graph"
    root: str = "."
    cors: str = "*"
    quiet: bool = True

    # -- helpers
    def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: Optional[dict] = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self.cors:
            self.send_header("Access-Control-Allow-Origin", self.cors)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj: Any):
        self._send(code, json.dumps(obj).encode())

    def _dir(self, q: Dict[str, List[str]]) -> Optional[str]:
        """Resolve ?design=<name> to a directory under root; None if the name is unsafe.

        The directory need not exist yet: a client may subscribe before the first build."""
        d = (q.get("design") or [""])[0]
        if not d:
            return self.root
        return os.path.join(self.root, d) if DESIGN_RE.match(d) else None

    def log_message(self, fmt, *args):
        if not self.quiet:
            super().log_message(fmt, *args)

    # -- routes
    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        route = u.path.rstrip("/") or "/"
        if route in ("/", "/api", "/api/health"):
            return self._json(200, {"ok": True, "service": "rtl_graph", "schema": SCHEMA,
                                    "root": os.path.abspath(self.root),
                                    "endpoints": ["/api/health", "/api/designs", "/api/status",
                                                  "/api/events", "/api/graph", "/api/file"]})
        if route == "/api/designs":
            return self._json(200, {"designs": self._designs()})
        if route == "/api/status":
            return self._status(q)
        if route == "/api/events":
            return self._events(q)
        if route == "/api/graph":
            name = "graph.slim.json" if (q.get("slim") or ["0"])[0] not in ("0", "false", "") else "graph.json"
            return self._file(q, name, "application/json")
        if route == "/api/file":
            name = (q.get("name") or ["graph.json"])[0]
            if name not in _found_files(self._dir(q) or "") and name not in (
                    "graph.json", "graph.slim.json", "graph.dot", "graph.svg", "graph.graphml", "summary.txt"):
                return self._json(400, {"error": "unknown file", "name": name})
            ctype = {"json": "application/json", "svg": "image/svg+xml"}.get(name.rsplit(".", 1)[-1], "text/plain")
            return self._file(q, name, ctype)
        return self._json(404, {"error": "no such endpoint", "path": u.path})

    def _designs(self) -> List[dict]:
        out = []
        if read_status(self.root):
            out.append({"design": None, "path": ".", "status": read_status(self.root)})
        for name in sorted(os.listdir(self.root)) if os.path.isdir(self.root) else []:
            d = os.path.join(self.root, name)
            st = read_status(d) if os.path.isdir(d) else None
            if st:
                out.append({"design": name, "path": name, "status": st})
        return out

    def _status(self, q):
        d = self._dir(q)
        if d is None:
            return self._json(404, {"error": "unknown design"})
        since = int((q.get("since") or ["-1"])[0] or -1)
        wait = min(float((q.get("wait") or ["0"])[0] or 0), 300.0)
        deadline = time.time() + wait
        while True:
            st = read_status(d)
            if st is None and wait <= 0:
                return self._json(404, {"error": "never built", "state": "unknown",
                                        "hint": "run `rtl_graph build|run` for this directory"})
            if st is not None and (wait <= 0 or (st.get("state") == "ready"
                                                 and int(st.get("generation", 0)) > since)):
                return self._json(200, st)
            if time.time() >= deadline:                      # waited long enough: report, don't hang
                return self._json(200, {**(st or {"state": "unknown", "generation": 0}), "timeout": True})
            time.sleep(0.25)

    def _events(self, q):
        """Server-Sent Events: one `ready` / `building` / `error` event per state change."""
        d = self._dir(q)
        if d is None:
            return self._json(404, {"error": "unknown design"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        if self.cors:
            self.send_header("Access-Control-Allow-Origin", self.cors)
        self.end_headers()
        last = None
        last_beat = 0.0
        try:
            while True:
                st = read_status(d) or {"state": "unknown", "generation": 0}
                key = (st.get("state"), st.get("generation"))
                now = time.time()
                if key != last:
                    last = key
                    self.wfile.write(f"event: {st.get('state')}\ndata: {json.dumps(st)}\n\n".encode())
                    self.wfile.flush()
                    last_beat = now
                elif now - last_beat > 15:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    last_beat = now
                time.sleep(0.25)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _file(self, q, name: str, ctype: str):
        d = self._dir(q)
        if d is None:
            return self._json(404, {"error": "unknown design"})
        st = read_status(d)
        if st and st.get("state") != "ready" and (q.get("strict") or ["1"])[0] not in ("0", "false"):
            return self._json(409, {"error": "graph not ready", "state": st.get("state"),
                                    "generation": st.get("generation"),
                                    "hint": "wait for /api/status to report ready, or pass strict=0"})
        p = os.path.join(d, name)
        try:
            with open(p, "rb") as f:
                body = f.read()
        except OSError:
            return self._json(404, {"error": "file not found", "name": name})
        gen = str((st or {}).get("generation", 0))
        return self._send(200, body, ctype, {"X-RTL-Graph-Generation": gen})


def serve(root: str, host: str = "127.0.0.1", port: int = 8765, cors: str = "*",
          quiet: bool = True, background: bool = False):
    """Publish the status/graph endpoints for `root` (a build directory, or a parent of several)."""
    handler = type("Handler", (_Handler,), {"root": root, "cors": cors, "quiet": quiet})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    if background:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd
