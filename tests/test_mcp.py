"""The MCP server: handshake, tool list, calls, errors and the stdio loop."""

import io
import json

from huntaway import mcp
from huntaway.core import ApiError


def server(result=None, error=None):
    calls = []

    def invoke(name, params):
        calls.append((name, params))
        if error:
            raise ApiError(error)
        return result if result is not None else {"ok": True}

    return mcp.Server(invoke), calls


def test_initialize_negotiates_the_version():
    s, _ = server()
    reply = s.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert reply["result"]["protocolVersion"] == "2025-06-18"
    assert reply["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert "exit code 137" in reply["result"]["instructions"]
    odd = s.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
    assert odd["result"]["protocolVersion"] == mcp.PROTOCOL_VERSIONS[0]


def test_notifications_get_no_reply():
    s, _ = server()
    assert s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_have_schemas_and_safety_hints():
    s, _ = server()
    tools = {t["name"]: t for t in s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
    assert {"status", "close_agent_session", "search_saved_sessions", "events"} <= set(tools)
    assert tools["status"]["annotations"]["readOnlyHint"] is True
    assert tools["close_agent_session"]["annotations"]["destructiveHint"] is True
    assert tools["close_agent_session"]["inputSchema"]["required"] == ["id"]


def test_tool_calls_route_and_wrap():
    s, calls = server(result={"answer": 42})
    reply = s.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "search_saved_sessions", "arguments": {"query": "stripe"}},
        }
    )
    assert calls == [("sessions", {"query": "stripe"})]
    assert reply["result"]["structuredContent"] == {"answer": 42} and not reply["result"]["isError"]
    assert json.loads(reply["result"]["content"][0]["text"]) == {"answer": 42}


def test_tool_errors_are_results_and_unknown_things_are_errors():
    s, _ = server(error="no such session")
    reply = s.handle(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "close_agent_session", "arguments": {"id": "x"}},
        }
    )
    assert reply["result"]["isError"] and "no such session" in reply["result"]["content"][0]["text"]
    assert s.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope"}})["error"]
    assert s.handle({"jsonrpc": "2.0", "id": 6, "method": "nope/nope"})["error"]["code"] == -32601
    assert s.handle({"jsonrpc": "2.0", "id": 7, "method": "ping"})["result"] == {}


def test_stdio_loop():
    s, _ = server()
    inp = io.StringIO(
        '{"jsonrpc":"2.0","id":1,"method":"ping"}\nnot json\n\n'
        '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
    )
    out = io.StringIO()
    mcp.serve_stdio(s, inp, out)
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert lines[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert lines[1]["error"]["code"] == -32700 and len(lines) == 2
