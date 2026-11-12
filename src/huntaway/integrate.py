"""
Connect huntaway to agent harnesses.

Register its MCP server and install its skill, so an agent can check the machine, explain a killed process and find
old sessions on its own.

Each harness's own CLI is used when it is installed (``claude mcp add``, ``codex mcp add``), because those harnesses
rewrite their config files while they run. Otherwise the JSON or TOML file is edited, after a backup.
"""

import json
import os
import shlex
import shutil
import subprocess  # nosec B404  # fixed commands only, never a shell
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from huntaway.service import executable

NAME = "huntaway"


@dataclass(slots=True)
class Step:
    """One change made, or that would be made."""

    harness: str
    what: str
    target: str
    done: bool
    note: str = ""

    def line(self) -> str:
        """
        Describe the step for people.

        :returns: One line
        """
        mark = "ok" if self.done else "--"
        return f"[{mark}] {self.harness:9} {self.what:6} {self.target}" + (f"  ({self.note})" if self.note else "")


def command() -> list[str]:
    """
    Return the command a harness runs to start the MCP server.

    :returns: Program and arguments
    """
    return [*shlex.split(executable()), "mcp"]


def skill_text() -> str:
    """
    Return the skill file shipped with the package.

    :returns: Markdown with front matter
    """
    return (resources.files("huntaway") / "data" / "SKILL.md").read_text(encoding="utf-8")


def claude_dirs(home: Path | None = None) -> list[Path]:
    """
    Claude Code config folders: ~/.claude, $CLAUDE_CONFIG_DIR and any ~/.claude* folder that has sessions.

    :param home: The home folder
    :returns: The folders
    """
    home = home or Path.home()
    found = []
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    for candidate in [home / ".claude", *((Path(env),) if env else ()), *sorted(home.glob(".claude*"))]:
        if (
            candidate.is_dir()
            and candidate not in found
            and ((candidate / "projects").is_dir() or (candidate / "settings.json").is_file())
        ):
            found.append(candidate)
    return found


def _backup(path: Path) -> None:
    if path.is_file():
        shutil.copy2(path, path.with_name(path.name + ".huntaway.bak"))


def _write_skill(folder: Path, harness: str, dry: bool) -> Step:
    target = folder / NAME / "SKILL.md"
    if not dry:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(skill_text(), encoding="utf-8")
    return Step(harness, "skill", str(target), not dry)


def _json_server(path: Path, key: str, entry: dict[str, Any], harness: str, dry: bool) -> Step:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except ValueError:
        return Step(harness, "mcp", str(path), False, "has comments or is not plain JSON; add it by hand")
    if not isinstance(data, dict):
        return Step(harness, "mcp", str(path), False, "unexpected layout; add it by hand")
    servers = data.setdefault(key, {})
    if servers.get(NAME) == entry:
        return Step(harness, "mcp", str(path), True, "already there")
    servers[NAME] = entry
    if not dry:
        _backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return Step(harness, "mcp", str(path), not dry)


def _run(args: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=60,  # nosec B603  # fixed commands
            env={**os.environ, **(env or {})},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return result.returncode, (result.stdout + result.stderr).strip()


def claude(dry: bool = False, home: Path | None = None) -> list[Step]:
    """
    Register the MCP server and install the skill for every Claude Code config folder.

    :param dry: Only say what would change
    :param home: The home folder
    :returns: The steps
    """
    home = home or Path.home()
    steps = []
    cli = shutil.which("claude")
    cmd = command()
    for folder in claude_dirs(home):
        default = folder == home / ".claude"
        env = {} if default else {"CLAUDE_CONFIG_DIR": str(folder)}
        config_file = home / ".claude.json" if default else folder / ".claude.json"
        if cli and not dry:
            _run([cli, "mcp", "remove", "--scope", "user", NAME], env)
            code, out = _run([cli, "mcp", "add", "--scope", "user", NAME, "--", *cmd], env)
            steps.append(
                Step("claude", "mcp", f"{config_file} (claude mcp add)", code == 0, "" if code == 0 else out[:160])
            )
        else:
            steps.append(
                _json_server(
                    config_file,
                    "mcpServers",
                    {"type": "stdio", "command": cmd[0], "args": cmd[1:], "env": {}},
                    "claude",
                    dry,
                )
            )
        steps.append(_write_skill(folder / "skills", "claude", dry))
    return steps


def codex(dry: bool = False, home: Path | None = None) -> list[Step]:
    """
    Register the MCP server and install the skill for Codex.

    :param dry: Only say what would change
    :param home: The home folder
    :returns: The steps
    """
    home = home or Path.home()
    folder = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    if not folder.is_dir():
        return []
    path = folder / "config.toml"
    cmd = command()
    steps = []
    try:
        current = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except tomllib.TOMLDecodeError, OSError:
        current = {}
    if NAME in (current.get("mcp_servers") or {}):
        steps.append(Step("codex", "mcp", str(path), True, "already there"))
    elif shutil.which("codex") and not dry:
        code, out = _run(["codex", "mcp", "add", NAME, "--", *cmd])
        steps.append(Step("codex", "mcp", f"{path} (codex mcp add)", code == 0, "" if code == 0 else out[:160]))
    else:
        block = f"\n[mcp_servers.{NAME}]\ncommand = {json.dumps(cmd[0])}\nargs = {json.dumps(cmd[1:])}\n"
        if not dry:
            _backup(path)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(block)
        steps.append(Step("codex", "mcp", str(path), not dry))
    steps.append(_write_skill(folder / "skills", "codex", dry))
    return steps


def opencode(dry: bool = False, home: Path | None = None) -> list[Step]:
    """
    Register the MCP server and install the skill for OpenCode.

    :param dry: Only say what would change
    :param home: The home folder
    :returns: The steps
    """
    home = home or Path.home()
    folder = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "opencode"
    if not folder.is_dir() and not (home / ".opencode").is_dir():
        return []
    path = folder / "opencode.json"
    if (folder / "opencode.jsonc").is_file() and not path.is_file():
        return [
            Step(
                "opencode",
                "mcp",
                str(folder / "opencode.jsonc"),
                False,
                'add "mcp": {"huntaway": {"type": "local", "command": ' + json.dumps(command()) + "}}",
            )
        ]
    steps = [_json_server(path, "mcp", {"type": "local", "command": command(), "enabled": True}, "opencode", dry)]
    steps.append(_write_skill(folder / "skill", "opencode", dry))
    return steps


def gemini(dry: bool = False, home: Path | None = None) -> list[Step]:
    """
    Register the MCP server for Gemini CLI.

    :param dry: Only say what would change
    :param home: The home folder
    :returns: The steps
    """
    home = home or Path.home()
    folder = home / ".gemini"
    if not folder.is_dir():
        return []
    cmd = command()
    return [_json_server(folder / "settings.json", "mcpServers", {"command": cmd[0], "args": cmd[1:]}, "gemini", dry)]


def cursor(dry: bool = False, home: Path | None = None) -> list[Step]:
    """
    Register the MCP server for Cursor.

    :param dry: Only say what would change
    :param home: The home folder
    :returns: The steps
    """
    home = home or Path.home()
    folder = home / ".cursor"
    if not folder.is_dir():
        return []
    cmd = command()
    return [_json_server(folder / "mcp.json", "mcpServers", {"command": cmd[0], "args": cmd[1:]}, "cursor", dry)]


HARNESSES: dict[str, Callable[..., list[Step]]] = {
    "claude": claude,
    "codex": codex,
    "opencode": opencode,
    "gemini": gemini,
    "cursor": cursor,
}

AGENTS_MD = """## Machine health (huntaway)

This machine runs huntaway. Before a heavy job, run `huntaway status`. Wrap long builds and test runs in
`huntaway run -- <command>` so they are capped instead of stopped. If a process died with exit code 137, run
`huntaway why <name>` before guessing. Old agent sessions are saved: `huntaway sessions "<words>"` finds them.
"""


def run(names: list[str], dry: bool = False) -> list[Step]:
    """
    Integrate with the named harnesses.

    :param names: Harness keys, or all
    :param dry: Only say what would change
    :returns: The steps
    """
    wanted = list(HARNESSES) if not names or "all" in names else names
    steps: list[Step] = []
    for name in wanted:
        fn = HARNESSES.get(name)
        if fn is None:
            steps.append(Step(name, "-", "unknown harness", False, f"pick from {', '.join(HARNESSES)}"))
            continue
        steps.extend(fn(dry=dry))
    return steps
