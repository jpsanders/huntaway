"""
The dashboard and the API over plain HTTP, from the standard library.

Security: on 127.0.0.1 the page carries the token, so the browser on this machine needs nothing; the Host header
must name this machine, which stops DNS rebinding. On any other address every request needs the token. Requests that
change something always need it, and a cross origin POST is refused.
"""

import gzip
import hmac
import ipaddress
import json
import os
import queue
import secrets
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlparse

import psutil

from huntaway import __version__, paths
from huntaway.core import METHODS, ApiError, Core, call, set_caller
from huntaway.mcp import Server as McpServer

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json",
    ".woff2": "font/woff2",
}
LOCAL_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}


def load_token() -> str:
    """
    Return the API token, created on first use and kept readable by the owner only.

    :returns: The token
    """
    path = paths.token_file()
    try:
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    return token


def _static(name: str) -> bytes | None:
    if "/" in name or name.startswith("."):
        return None
    try:
        return (resources.files("huntaway") / "web" / name).read_bytes()
    except OSError, FileNotFoundError:
        return None


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return host in LOCAL_NAMES


def _host_ok(header: str, bind: str) -> bool:
    host = header.rsplit(":", 1)[0] if header.count(":") == 1 or header.startswith("[") else header
    host = host.strip("[]").lower()
    if not host:
        return False
    if host in LOCAL_NAMES or host == "::1":
        return True
    try:
        ipaddress.ip_address(host)
        return not _is_loopback(bind)
    except ValueError:
        pass
    if _is_loopback(bind):
        return False
    name = socket.gethostname().lower()
    return host in (name, f"{name}.local", f"{name}.lan") or host.endswith((".ts.net", ".local", ".lan", ".home.arpa"))


def _caller_family(client_port: int) -> set[int]:
    # For a request from this machine, find the process that opened the connection and its parents, so an agent
    # session can never close itself through the API.
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.laddr and conn.laddr.port == client_port and conn.pid:
                pids = {conn.pid}
                proc: psutil.Process | None = psutil.Process(conn.pid)
                while proc is not None and proc.pid > 1:
                    pids.add(proc.pid)
                    proc = proc.parent()
                return pids
    except psutil.Error, OSError:
        pass
    return set()


def _query_params(query: str) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for key, values in parse_qs(query, keep_blank_values=True).items():
        if key == "token":
            continue
        value = values[-1]
        try:
            params[key] = json.loads(value)
        except ValueError:
            params[key] = value
        if isinstance(params[key], (dict, list)) and key not in ("values", "rules"):
            params[key] = value
    return params


def metrics(core: Core) -> str:
    """
    Prometheus metrics.

    :param core: The core
    :returns: The text exposition format
    """
    snap = core.snap
    lines = [f'huntaway_info{{version="{__version__}"}} 1']
    if snap is not None:
        s = core._summary(snap, core.agents)
        values = {
            "cpu_percent": s["cpu"],
            "memory_used_percent": s["mem"],
            "memory_available_bytes": s["mem_available"],
            "swap_used_bytes": s["swap_used"],
            "load1": s["load"][0],
            "agents": s["agents"],
            "agents_rss_bytes": s["agents_rss"],
            "agents_stale": s["agents_stale"],
            "sample_seconds": s["cost_ms"] / 1000.0,
        }
        for key, value in snap.psi.items():
            values[f"pressure_{key}_avg10"] = value
        if snap.temp is not None:
            values["cpu_temperature_celsius"] = snap.temp
        lines += [f"huntaway_{k} {v}" for k, v in values.items()]
    lines += [f"huntaway_{k}_total {v}" for k, v in core.stats.items()]
    counts: dict[str, int] = {}
    for f in core.store.findings():
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    lines += [f'huntaway_findings{{severity="{k}"}} {v}' for k, v in counts.items()]
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    """One request."""

    core: Core
    token: str
    bind: str
    mcp: McpServer
    server_version = f"huntaway/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base class's name
        """Stay quiet; the daemon has its own events."""

    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        if len(body) > 1024 and "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body, compresslevel=5)
            extra = {**(extra or {}), "Content-Encoding": "gzip"}
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data, default=str).encode(), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _authorised(self, query: str = "") -> bool:
        given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        given = given or self.headers.get("X-Huntaway-Token", "")
        if not given and query:
            given = (parse_qs(query).get("token") or [""])[-1]
        return bool(given) and hmac.compare_digest(given.encode(), self.token.encode())

    def _guard(self, need_token: bool, query: str = "") -> bool:
        if not _host_ok(self.headers.get("Host", ""), self.bind):
            self._error(HTTPStatus.MISDIRECTED_REQUEST, "unknown host name")
            return False
        if need_token and not self._authorised(query):
            self._error(HTTPStatus.UNAUTHORIZED, "token required: run huntaway token")
            return False
        return True

    def do_HEAD(self) -> None:  # noqa: N802 - the base class's naming
        """Answer HEAD like GET without a body."""
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        """Serve pages, static files, read only API calls, the event stream and metrics."""
        url = urlparse(self.path)
        path = url.path
        if path == "/healthz":
            self._send(200, b"ok\n", "text/plain")
            return
        if path in ("/", "/index.html"):
            if not self._guard(False):
                return
            page = _static("index.html") or b"<h1>huntaway</h1><p>The dashboard files are missing.</p>"
            if _is_loopback(self.bind):
                page = page.replace(b'content="__TOKEN__"', f'content="{self.token}"'.encode())
            page = page.replace(b"__VERSION__", __version__.encode())
            self._send(200, page, STATIC_TYPES[".html"], {"Cache-Control": "no-store"})
            return
        if path.startswith("/static/"):
            name = path.removeprefix("/static/")
            body = _static(name)
            if body is None:
                self._error(404, "not found")
                return
            ctype = STATIC_TYPES.get(os.path.splitext(name)[1], "application/octet-stream")
            self._send(200, body, ctype, {"Cache-Control": "no-cache", "ETag": f'"{__version__}-{len(body)}"'})
            return
        if path == "/metrics":
            if not self.core.cfg["server.metrics"]:
                self._error(404, "metrics are off")
                return
            if not self._guard(not _is_loopback(self.bind), url.query):
                return
            self._send(200, metrics(self.core).encode(), "text/plain; version=0.0.4")
            return
        if path == "/api/stream":
            if self._guard(True, url.query):
                self._stream()
            return
        if path.startswith("/api/"):
            if not self._guard(True, url.query):
                return
            name = path.removeprefix("/api/")
            method = METHODS.get(name)
            if method is None:
                self._error(404, f"unknown method {name}")
                return
            if not method.readonly:
                self._error(405, f"{name} changes things: use POST")
                return
            self._call(name, _query_params(url.query))
            return
        self._error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        """Serve API calls that change things, and MCP."""
        url = urlparse(self.path)
        if not self._guard(True, ""):
            return
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host", ""):
            self._error(403, "cross origin requests are refused")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, 4 << 20)) if length else b""
            body = json.loads(raw or b"{}")
        except ValueError, OSError:
            self._error(400, "the body must be JSON")
            return
        if url.path == "/mcp":
            set_caller(_caller_family(self.client_address[1]) if _is_loopback(self.client_address[0]) else set())
            replies = [
                r
                for r in (
                    self.mcp.handle(m) for m in (body if isinstance(body, list) else [body]) if isinstance(m, dict)
                )
                if r is not None
            ]
            if not replies:
                self._send(202, b"", "application/json")
            else:
                self._json(200, replies if isinstance(body, list) else replies[0])
            return
        if url.path.startswith("/api/"):
            if not isinstance(body, dict):
                self._error(400, "the body must be a JSON object")
                return
            self._call(url.path.removeprefix("/api/"), body)
            return
        self._error(404, "not found")

    def _call(self, name: str, params: dict[str, Any]) -> None:
        method = METHODS.get(name)
        local = _is_loopback(self.client_address[0])
        set_caller(_caller_family(self.client_address[1]) if local and method and not method.readonly else set())
        try:
            result = call(self.core, name, params)
        except ApiError as exc:
            self._error(exc.status, str(exc))
            return
        except Exception as exc:  # the dashboard gets a message instead of a dropped connection
            self.core.store.add_event("service", f"API error in {name}: {exc!r}"[:300], "medium")
            self._error(500, f"{name} failed: {exc}")
            return
        self._json(200, result)

    def _stream(self) -> None:
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=50)

        def push(summary: dict[str, Any]) -> None:
            try:
                q.put_nowait(summary)
            except queue.Full:
                pass

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.core.subscribers.append(push)
        try:
            while True:
                try:
                    item = q.get(timeout=15)
                    self.wfile.write(b"data: " + json.dumps(item, default=str).encode() + b"\n\n")
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except BrokenPipeError, ConnectionResetError, OSError:
            pass
        finally:
            if push in self.core.subscribers:
                self.core.subscribers.remove(push)


class Server(ThreadingHTTPServer):
    """The HTTP server, with threads that do not block shutdown."""

    daemon_threads = True
    allow_reuse_address = True


def serve(core: Core, host: str, port: int, token: str | None = None) -> Server:
    """
    Start the dashboard and API on a background thread.

    :param core: The core
    :param host: The address to bind
    :param port: The port; 0 picks a free one
    :param token: The API token, default the stored one
    :returns: The running server
    """
    token = token or load_token()

    def invoke(name: str, params: dict[str, Any]) -> Any:
        return call(core, name, params)

    handler = type("BoundHandler", (Handler,), {"core": core, "token": token, "bind": host, "mcp": McpServer(invoke)})
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    server_class: type[Server] = type("BoundServer", (Server,), {"address_family": family})
    server = server_class((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="huntaway-http", daemon=True)
    thread.start()
    return server
