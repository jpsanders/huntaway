"""
A Model Context Protocol server for agent harnesses.

A Model Context Protocol server, so any agent harness can ask huntaway about the machine, find and resume old
sessions, and act. It speaks JSON-RPC 2.0 over stdio (``huntaway mcp``) and over HTTP at ``/mcp``. Tools map one to
one onto the API methods; the ones that change something are marked as such for the harness.
"""

import json
import sys
from collections.abc import Callable
from typing import Any, TextIO

from huntaway import __version__
from huntaway.core import METHODS, ApiError

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "huntaway watches this machine. Use status first when the machine is slow, a process died with exit code 137, "
    "or memory is short. events explains every process huntaway stopped and why. agents_list shows the agent "
    "sessions still open and how long each has been idle; agent_close saves one (verified) before closing it, and "
    "the session can always be resumed. sessions searches every saved transcript, so you can find past work by "
    "what was said. Closing or ending things changes the user's machine: say what you will do before you do it."
)

TOOL_NAMES = {
    "status": "status",
    "processes": "top_processes",
    "agents_list": "list_agent_sessions",
    "agent_save": "save_agent_session",
    "agent_close": "close_agent_session",
    "agents_reap": "close_stale_sessions",
    "agent_pin": "pin_agent_session",
    "sessions": "search_saved_sessions",
    "session_read": "read_saved_session",
    "session_restore": "restore_saved_session",
    "events": "events",
    "analytics": "analytics",
    "security": "security_findings",
    "security_scan": "run_security_checks",
    "config_get": "get_settings",
    "config_set": "change_settings",
    "process_action": "act_on_process",
}
DESTRUCTIVE = {"agent_close", "agents_reap", "process_action", "session_restore"}


def tools() -> list[dict[str, Any]]:
    """
    Return the tool list for ``tools/list``.

    :returns: MCP tool definitions
    """
    out = []
    for method_name, tool_name in TOOL_NAMES.items():
        method = METHODS[method_name]
        required = [k for k, v in method.params.items() if "default" not in v]
        out.append(
            {
                "name": tool_name,
                "description": method.summary,
                "inputSchema": {
                    "type": "object",
                    "properties": method.params,
                    "required": required,
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": method.readonly,
                    "destructiveHint": method_name in DESTRUCTIVE,
                    "idempotentHint": method.readonly,
                    "openWorldHint": False,
                },
            }
        )
    return out


BY_TOOL = {v: k for k, v in TOOL_NAMES.items()}


class Server:
    """Handles MCP messages. ``invoke`` runs an API method, in process or through the daemon."""

    def __init__(self, invoke: Callable[[str, dict[str, Any]], Any]) -> None:
        self.invoke = invoke

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """
        Answer one JSON-RPC message.

        :param message: The request or notification
        :returns: The response, or None for a notification
        """
        mid = message.get("id")
        method = message.get("method", "")
        params = message.get("params") or {}
        if mid is None:
            return None
        try:
            result = self._dispatch(method, params)
        except ApiError as exc:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": str(exc)}}
        except KeyError as exc:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {exc}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            asked = str(params.get("protocolVersion", ""))
            return {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "huntaway", "title": "huntaway", "version": __version__},
                "instructions": INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": tools()}
        if method == "tools/call":
            name = str(params.get("name", ""))
            if name not in BY_TOOL:
                raise ApiError(f"unknown tool {name!r}")
            try:
                result = self.invoke(BY_TOOL[name], params.get("arguments") or {})
            except ApiError as exc:
                return {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True}
            text = json.dumps(result, indent=1, default=str)
            if len(text) > 200_000:
                text = text[:200_000] + "\n... (cut; ask for less with limit or length)"
            out: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": False}
            if isinstance(result, dict):
                out["structuredContent"] = result
            return out
        if method in ("resources/list", "prompts/list"):
            return {method.split("/")[0]: []}
        raise KeyError(method)


def serve_stdio(server: Server, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    """
    Serve MCP over stdio: one JSON message per line.

    :param server: The handler
    :param stdin: Input stream
    :param stdout: Output stream
    """
    reader = stdin or sys.stdin
    writer = stdout or sys.stdout
    for line in reader:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            writer.write(
                json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n"
            )
            writer.flush()
            continue
        batch = message if isinstance(message, list) else [message]
        replies = [r for r in (server.handle(m) for m in batch if isinstance(m, dict)) if r is not None]
        for reply in replies:
            writer.write(json.dumps(reply, default=str) + "\n")
        writer.flush()
