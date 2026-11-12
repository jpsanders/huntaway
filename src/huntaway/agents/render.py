"""
Turn agent transcripts into readable Markdown.

Every prompt and reply in full, tool calls as one line each and tool output trimmed. Unknown line types are skipped,
so a format change degrades instead of failing.
"""

import json
from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Any

TOOL_OUTPUT_CHARS = 600


def _when(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ""


def _clip(text: str, limit: int = TOOL_OUTPUT_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + f"\n... ({len(text) - limit} more characters)"


def _one_line(value: Any, limit: int = 160) -> str:
    if isinstance(value, dict):
        for key in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt"):
            if isinstance(value.get(key), str):
                value = value[key]
                break
        else:
            value = json.dumps(value, ensure_ascii=False)
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _blocks_text(content: Any) -> Iterator[tuple[str, str]]:
    # Yields (kind, text) for Claude message content: text, tool, result.
    if isinstance(content, str):
        yield "text", content
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and block.get("text"):
            yield "text", str(block["text"])
        elif kind == "tool_use":
            yield "tool", f"{block.get('name', 'tool')}: {_one_line(block.get('input'))}"
        elif kind == "tool_result":
            inner = block.get("content")
            if isinstance(inner, list):
                inner = "\n".join(str(b.get("text", "")) for b in inner if isinstance(b, dict))
            yield "result", str(inner or "")


def claude(lines: Iterable[str]) -> Iterator[str]:
    """
    Render a Claude Code JSONL transcript.

    :param lines: The file's lines
    :yields: Markdown fragments
    """
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("isMeta") or row.get("type") not in ("user", "assistant"):
            continue
        message = row.get("message") or {}
        who = "You" if row["type"] == "user" else "Claude"
        if row.get("isSidechain"):
            who = "Subagent" if who == "Claude" else "Task"
        parts = list(_blocks_text(message.get("content")))
        texts = [t for k, t in parts if k == "text" and not t.lstrip().startswith("<")]
        tools = [t for k, t in parts if k == "tool"]
        results = [t for k, t in parts if k == "result"]
        if texts:
            yield f"### {who} · {_when(row.get('timestamp'))}\n\n" + "\n\n".join(t.strip() for t in texts) + "\n"
        for tool in tools:
            yield f"> **tool** {tool}\n"
        for result in results:
            if result.strip():
                yield "```text\n" + _clip(result) + "\n```\n"


def codex(lines: Iterable[str]) -> Iterator[str]:
    """
    Render a Codex rollout.

    :param lines: The file's lines
    :yields: Markdown fragments
    """
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        payload = row.get("payload") if isinstance(row, dict) else None
        if not isinstance(payload, dict) or row.get("type") != "response_item":
            continue
        kind = payload.get("type")
        if kind == "message":
            role = payload.get("role")
            texts = [
                str(c.get("text", ""))
                for c in payload.get("content") or []
                if isinstance(c, dict) and c.get("type") in ("input_text", "output_text")
            ]
            texts = [t for t in texts if t.strip() and not t.lstrip().startswith("<")]
            if texts and role in ("user", "assistant"):
                who = "You" if role == "user" else "Codex"
                yield f"### {who} · {_when(row.get('timestamp'))}\n\n" + "\n\n".join(t.strip() for t in texts) + "\n"
        elif kind in ("function_call", "custom_tool_call", "local_shell_call"):
            args = payload.get("arguments") or payload.get("input") or payload.get("action") or ""
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass
            yield f"> **tool** {payload.get('name', kind)}: {_one_line(args)}\n"
        elif kind in ("function_call_output", "custom_tool_call_output"):
            out = payload.get("output")
            if isinstance(out, dict):
                out = out.get("content") or out.get("output") or json.dumps(out)
            if out:
                yield "```text\n" + _clip(str(out)) + "\n```\n"


def opencode(document: Any) -> Iterator[str]:
    """
    Render an OpenCode session export.

    :param document: The parsed JSON from ``opencode session export``
    :yields: Markdown fragments
    """
    messages = document.get("messages") if isinstance(document, dict) else None
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        info = message.get("info") or message
        role = info.get("role", "")
        who = "You" if role == "user" else "OpenCode"
        texts: list[str] = []
        for part in message.get("parts") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and part.get("text") and not part.get("synthetic"):
                texts.append(str(part["text"]).strip())
            elif part.get("type") == "tool":
                state = part.get("state") or {}
                texts.append(f"> **tool** {part.get('tool', 'tool')}: {_one_line(state.get('input'))}")
        if texts:
            yield f"### {who}\n\n" + "\n\n".join(texts) + "\n"


def markdown(harness: str, title: str, lines: Iterable[str] | None = None, document: Any = None) -> str:
    """
    Render a readable transcript.

    :param harness: claude, codex or opencode; anything else gives an empty body
    :param title: The heading
    :param lines: The transcript's lines (claude, codex)
    :param document: The parsed export (opencode)
    :returns: Markdown
    """
    if harness == "claude" and lines is not None:
        body = list(claude(lines))
    elif harness == "codex" and lines is not None:
        body = list(codex(lines))
    elif harness == "opencode" and document is not None:
        body = list(opencode(document))
    else:
        body = []
    head = f"# {title or 'Agent session'}\n\n"
    return head + ("\n".join(body) if body else "_No readable messages. The raw files are kept next to this one._\n")
