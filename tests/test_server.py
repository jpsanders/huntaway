"""The HTTP server: pages, the token, the Host check, the API, MCP and metrics."""

import json
import urllib.error
import urllib.request

import pytest

from huntaway.core import Core
from huntaway.server import serve


@pytest.fixture
def live(cfg, store):
    core = Core(cfg, store)
    core.tick()
    srv = serve(core, "127.0.0.1", 0, token="t0ken")
    yield f"http://127.0.0.1:{srv.server_address[1]}", core
    srv.shutdown()


def get(url, token=None, host=None, method="GET", body=None, origin=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if host:
        headers["Host"] = host
    if origin:
        headers["Origin"] = origin
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_health_and_page_carries_the_token_on_loopback(live):
    base, _ = live
    assert get(base + "/healthz") == (200, b"ok\n")
    status, page = get(base + "/")
    assert status == 200 and b'content="t0ken"' in page and b"__VERSION__" not in page


def test_static_files_and_traversal(live):
    base, _ = live
    assert get(base + "/static/app.js")[0] == 200
    assert get(base + "/static/..%2fserver.py")[0] == 404
    assert get(base + "/nothing")[0] == 404


def test_api_needs_the_token_and_a_known_host(live):
    base, _ = live
    assert get(base + "/api/status")[0] == 401
    assert get(base + "/api/status", token="wrong")[0] == 401
    assert get(base + "/api/status", token="t0ken", host="evil.example")[0] == 421
    status, body = get(base + "/api/status", token="t0ken")
    assert status == 200 and json.loads(body)["running"] is False


def test_read_only_over_get_changes_over_post(live, tmp_path):
    base, core = live
    assert get(base + "/api/config_set", token="t0ken")[0] == 405
    status, body = get(base + "/api/config_set", token="t0ken", method="POST", body={"values": {"memory.sustain": 7}})
    assert status == 200 and core.cfg["memory.sustain"] == 7
    status, body = get(base + "/api/config_set", token="t0ken", method="POST", body={"values": {"memory.x": 1}})
    assert status == 400 and b"unknown option" in body
    assert get(base + "/api/events?limit=5", token="t0ken")[0] == 200
    assert get(base + "/api/analytics?range=7d", token="t0ken")[0] == 200
    assert get(base + "/api/analytics?range=2d", token="t0ken")[0] == 400


def test_cross_origin_posts_are_refused(live):
    base, _ = live
    status, _ = get(base + "/api/security_scan", token="t0ken", method="POST", body={}, origin="https://evil.example")
    assert status == 403


def test_mcp_over_http_and_metrics(live):
    base, _ = live
    status, body = get(
        base + "/mcp",
        token="t0ken",
        method="POST",
        body={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "status", "arguments": {}}},
    )
    assert status == 200 and json.loads(body)["result"]["isError"] is False
    status, body = get(base + "/metrics")
    assert status == 200 and b"huntaway_cpu_percent" in body
