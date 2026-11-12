"""
The settings: one schema with defaults and help text.

A TOML loader that validates, and a writer that documents.

The schema is the single source of truth. The dashboard builds its Settings page from it, ``huntaway config``
validates against it, and the file written to disk carries every option's help text as a comment, with options left
at their default commented out.
"""

import math
import os
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from huntaway import paths

SECTIONS: dict[str, str] = {
    "general": "How often huntaway looks, and whether it acts or only records what it would do.",
    "memory": "Keeps the machine from freezing when memory runs out, the way earlyoom does, but agent aware.",
    "cpu": "Calms processes that hold the CPU for a long time.",
    "agents": "Finds AI agent sessions (Claude Code, Codex, OpenCode, Gemini CLI, Aider and more).",
    "sessions": "The archive of saved agent sessions.",
    "protect": "Processes huntaway never touches.",
    "security": "Built in quick checks and deep scans (rkhunter, chkrootkit, ClamAV, Lynis).",
    "judge": "An optional language model that reviews a decision before huntaway ends a process. Off by default.",
    "server": "The dashboard and the API.",
    "retention": "How long history is kept.",
}

ACTIONS = ("throttle", "terminate", "kill", "notify")


@dataclass(frozen=True, slots=True)
class Option:
    """One setting in the schema."""

    key: str
    default: Any
    help: str
    choices: tuple[str, ...] = ()
    low: float | None = None
    high: float | None = None
    secret: bool = False

    @property
    def kind(self) -> str:
        """
        The value type, as the dashboard names it.

        :returns: One of bool, int, float, str and list
        """
        if isinstance(self.default, bool):
            return "bool"
        if isinstance(self.default, int):
            return "int"
        if isinstance(self.default, float):
            return "float"
        if isinstance(self.default, list):
            return "list"
        return "str"


SCHEMA: tuple[Option, ...] = (
    Option(
        "general.interval",
        5,
        "Seconds between samples. huntaway samples every second while memory is short.",
        low=1,
        high=60,
    ),
    Option(
        "general.mode",
        "enforce",
        "enforce acts on the rules. observe only records what it would have done.",
        choices=("enforce", "observe"),
    ),
    Option("general.notify", True, "Show a desktop notification for every action."),
    Option(
        "general.webhook",
        "",
        "Also POST every action to this URL. ntfy, Slack and Discord webhooks are "
        "recognised, anything else gets JSON.",
    ),
    Option("memory.enabled", True, "Act before the machine runs out of memory."),
    Option(
        "memory.min_available_percent",
        6.0,
        "Act when available memory falls below this share of RAM...",
        low=1,
        high=50,
    ),
    Option(
        "memory.min_swap_free_percent",
        10.0,
        "...while free swap is below this share. A machine without swap " "counts as having none free.",
        low=0,
        high=100,
    ),
    Option(
        "memory.pressure_full",
        40.0,
        "Also act when the kernel's memory pressure (PSI full avg10) passes this "
        "percent, whatever the free memory says. 0 turns it off. Linux only.",
        low=0,
        high=100,
    ),
    Option("memory.sustain", 4, "Seconds the shortage must last before huntaway acts.", low=1, high=600),
    Option(
        "memory.grace",
        5,
        "Seconds between asking a process to stop (SIGTERM) and forcing it (SIGKILL).",
        low=1,
        high=120,
    ),
    Option(
        "memory.prefer_idle_agents",
        True,
        "When memory is short, save and close idle agent sessions before "
        "ending anything else. Nothing is lost: each one can be resumed.",
    ),
    Option(
        "memory.min_victim_mb",
        64,
        "Never end a process holding less memory than this, it would not help.",
        low=0,
        high=1048576,
    ),
    Option(
        "memory.process_max_percent",
        0.0,
        "End any single process holding more than this share of RAM for " "the sustain time. 0 turns it off.",
        low=0,
        high=100,
    ),
    Option("cpu.enabled", True, "Act on processes that hold the CPU."),
    Option(
        "cpu.process_percent",
        95.0,
        "A process at or above this CPU use counts as a hog. 100 is one full core, "
        "so a process on four cores reads 400.",
        low=10,
        high=100000,
    ),
    Option(
        "cpu.system_percent",
        85.0,
        "Only act while the whole machine is at least this busy. A hog on an "
        "otherwise idle machine harms nobody. 0 acts regardless.",
        low=0,
        high=100,
    ),
    Option("cpu.sustain", 30, "Seconds a process must stay a hog before huntaway acts.", low=1, high=86400),
    Option(
        "cpu.action",
        "throttle",
        "throttle drops the process to the lowest CPU and disk priority, which "
        "nobody notices until something else needs the CPU. terminate and kill end it. notify only tells you.",
        choices=ACTIONS,
    ),
    Option("agents.enabled", True, "Find AI agent sessions and show what they hold."),
    Option(
        "agents.reap",
        True,
        "Save and close interactive agent sessions left idle. Each one is archived and "
        "verified first, and its terminal shows the command that resumes it.",
    ),
    Option(
        "agents.idle_hours",
        12.0,
        "A session counts as stale after this many hours without a message, a " "keystroke or CPU work.",
        low=0.1,
        high=8760,
    ),
    Option(
        "agents.pressure_idle_minutes",
        30.0,
        "While memory is short, sessions idle this many minutes are " "closed first.",
        low=1,
        high=10080,
    ),
    Option(
        "agents.include_waiting",
        True,
        "Also close sessions that have waited on a question for you for the " "full idle time.",
    ),
    Option("agents.tty_note", True, "After closing a session, print its resume command in its own terminal."),
    Option(
        "agents.pinned",
        [],
        "Session ids, or process ids, that are never closed. The dashboard's pin button " "adds to this list.",
    ),
    Option(
        "agents.extra",
        [],
        "More process names to treat as agent sessions, for harnesses huntaway does not "
        "know yet. They are listed and closed, and their tmux scrollback is saved.",
    ),
    Option("sessions.archive_dir", "", "Where saved sessions go. Empty means the data folder."),
    Option("sessions.compress", True, "Compress saved transcripts (zstd where Python has it, gzip otherwise)."),
    Option(
        "sessions.backup",
        False,
        "Every few hours, also save every transcript the agents left on disk, open or "
        "not. Claude Code deletes its own after 30 days by default.",
    ),
    Option("sessions.backup_hours", 6.0, "Hours between backup passes.", low=0.25, high=720),
    Option(
        "protect.names",
        [],
        "Process names never touched, on top of the built in list (desktop, audio, "
        "terminals, ssh, password managers, call apps).",
    ),
    Option(
        "protect.patterns",
        [],
        "Regular expressions matched against the full command line. A match is never " "touched.",
    ),
    Option(
        "protect.calls",
        True,
        "Protect browsers and call apps while they hold the microphone or camera, so a "
        "call is never cut. Linux with PipeWire.",
    ),
    Option("security.enabled", True, "Run the built in quick checks."),
    Option("security.quick_hours", 6.0, "Hours between quick checks.", low=0.25, high=720),
    Option("security.ignore", [], "Finding ids, or regular expressions over the finding title, to hide."),
    Option(
        "security.clamav_paths",
        [],
        "Folders the deep ClamAV scan covers. Empty means Downloads, the "
        "temporary folders and files changed in the last 7 days.",
    ),
    Option(
        "judge.mode",
        "off",
        "off: no model is ever called. advise: the model's view is recorded next to "
        "each action. veto: the model may spare a process the rules would end. It can never end one.",
        choices=("off", "advise", "veto"),
    ),
    Option(
        "judge.base_url",
        "http://localhost:11434/v1",
        "Any OpenAI compatible endpoint. The default is a " "local Ollama, so nothing leaves the machine.",
    ),
    Option("judge.model", "", "The model name, for example qwen3:4b on Ollama."),
    Option(
        "judge.api_key_env",
        "",
        "Name of the environment variable that holds the API key. Leave empty for " "a local server.",
    ),
    Option(
        "judge.timeout",
        20.0,
        "Seconds to wait for a verdict. With no verdict in time the rules decide.",
        low=1,
        high=300,
    ),
    Option("server.enabled", True, "Serve the dashboard and the API."),
    Option(
        "server.host",
        "127.0.0.1",
        "127.0.0.1 keeps the dashboard on this machine. 0.0.0.0 opens it to your "
        "LAN, and every request then needs the token.",
    ),
    Option("server.port", 4868, "The port. 4868 spells HUNT on a phone keypad.", low=1, high=65535),
    Option("server.metrics", True, "Serve Prometheus metrics at /metrics."),
    Option("retention.raw_hours", 24, "Keep full resolution samples this many hours.", low=1, high=720),
    Option("retention.minute_days", 14, "Keep one minute averages this many days.", low=1, high=365),
    Option("retention.hour_days", 400, "Keep hourly averages this many days.", low=1, high=3650),
    Option("retention.events_days", 365, "Keep events and findings this many days.", low=1, high=3650),
)

BY_KEY: dict[str, Option] = {o.key: o for o in SCHEMA}

RULE_KEYS = {"name", "match", "cpu_percent", "memory_mb", "sustain", "action"}


class ConfigError(ValueError):
    """A config file or value that does not fit the schema."""


@dataclass(slots=True)
class Rule:
    """A user rule: processes whose name or command line matches get their own limits."""

    name: str
    match: re.Pattern[str]
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    sustain: int = 30
    action: str = "terminate"

    def to_dict(self) -> dict[str, Any]:
        """
        Return the rule as it is written to the file.

        :returns: A plain dict
        """
        return {
            "name": self.name,
            "match": self.match.pattern,
            "cpu_percent": self.cpu_percent,
            "memory_mb": self.memory_mb,
            "sustain": self.sustain,
            "action": self.action,
        }


@dataclass(slots=True)
class Config:
    """Validated settings. Read a value with ``cfg["memory.sustain"]``."""

    values: dict[str, Any]
    rules: list[Rule] = field(default_factory=list)
    path: Path | None = None
    mtime: float = 0.0

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def changed(self) -> dict[str, Any]:
        """
        Return the options that differ from their defaults.

        :returns: Key to value
        """
        return {k: v for k, v in self.values.items() if v != BY_KEY[k].default}

    def to_public(self) -> dict[str, Any]:
        """
        Every value, grouped by section, for the API.

        :returns: Section to key to value
        """
        out: dict[str, dict[str, Any]] = {}
        for key, value in self.values.items():
            section, name = key.split(".", 1)
            out.setdefault(section, {})[name] = value
        return {"settings": out, "rules": [r.to_dict() for r in self.rules]}

    def archive_dir(self) -> Path:
        """
        Return the session archive folder.

        :returns: The configured folder, or the default under the data dir
        """
        configured = str(self.values["sessions.archive_dir"]).strip()
        return Path(os.path.expanduser(configured)) if configured else paths.data_dir() / "sessions"


def _coerce(opt: Option, value: Any) -> Any:
    kind = opt.kind
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "yes", "on", "1"):
            return True
        if isinstance(value, str) and value.lower() in ("false", "no", "off", "0"):
            return False
        raise ConfigError(f"{opt.key} takes true or false, not {value!r}")
    if kind in ("int", "float"):
        if isinstance(value, bool):
            raise ConfigError(f"{opt.key} takes a number, not {value!r}")
        try:
            number = float(value)
        except TypeError, ValueError:
            raise ConfigError(f"{opt.key} takes a number, not {value!r}") from None
        if not math.isfinite(number):
            raise ConfigError(f"{opt.key} takes a finite number")
        if opt.low is not None and number < opt.low:
            raise ConfigError(f"{opt.key} must be at least {opt.low:g}, not {number:g}")
        if opt.high is not None and number > opt.high:
            raise ConfigError(f"{opt.key} must be at most {opt.high:g}, not {number:g}")
        if kind == "int":
            if number != int(number):
                raise ConfigError(f"{opt.key} takes a whole number, not {value!r}")
            return int(number)
        return number
    if kind == "list":
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{opt.key} takes a list of strings")
        if opt.key in ("protect.patterns", "security.ignore"):
            for pattern in value:
                _compile(opt.key, pattern)
        return list(value)
    if not isinstance(value, str):
        raise ConfigError(f"{opt.key} takes a string, not {value!r}")
    if opt.choices and value not in opt.choices:
        raise ConfigError(f"{opt.key} takes one of {', '.join(opt.choices)}, not {value!r}")
    return value


def _compile(where: str, pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ConfigError(f"{where}: {pattern!r} is not a valid regular expression ({exc})") from None


def parse_rules(raw: Any) -> list[Rule]:
    """
    Validate the ``[[rules]]`` tables.

    :param raw: The list read from TOML or sent by the dashboard
    :returns: The rules
    :raises ConfigError: When a rule is malformed
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("rules must be a list of tables")
    rules: list[Rule] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ConfigError(f"rule {index} must be a table")
        unknown = set(item) - RULE_KEYS
        if unknown:
            raise ConfigError(f"rule {index}: unknown keys {', '.join(sorted(unknown))}")
        if not isinstance(item.get("match"), str) or not item["match"]:
            raise ConfigError(f"rule {index} needs match, a regular expression over the name or command line")
        action = item.get("action", "terminate")
        if action not in ACTIONS:
            raise ConfigError(f"rule {index}: action must be one of {', '.join(ACTIONS)}")
        try:
            cpu = float(item.get("cpu_percent", 0))
            mem = float(item.get("memory_mb", 0))
            sustain = int(item.get("sustain", 30))
        except TypeError, ValueError:
            raise ConfigError(f"rule {index}: cpu_percent, memory_mb and sustain take numbers") from None
        if cpu < 0 or mem < 0 or sustain < 0:
            raise ConfigError(f"rule {index}: limits cannot be negative")
        rules.append(
            Rule(
                name=str(item.get("name") or item["match"]),
                match=_compile(f"rule {index}", item["match"]),
                cpu_percent=cpu,
                memory_mb=mem,
                sustain=sustain,
                action=action,
            )
        )
    return rules


def defaults() -> dict[str, Any]:
    """
    Every option at its default.

    :returns: Key to value, lists copied
    """
    return {o.key: (list(o.default) if isinstance(o.default, list) else o.default) for o in SCHEMA}


def from_mapping(data: dict[str, Any], path: Path | None = None, mtime: float = 0.0) -> Config:
    """
    Validate a parsed TOML document.

    :param data: Section tables plus an optional ``rules`` list
    :param path: Where it came from
    :param mtime: The file's modification time
    :returns: The config
    :raises ConfigError: On an unknown key or a bad value
    """
    values = defaults()
    for section, table in data.items():
        if section == "rules":
            continue
        if section not in SECTIONS:
            raise ConfigError(f"unknown section [{section}]")
        if not isinstance(table, dict):
            raise ConfigError(f"[{section}] must be a table")
        for name, value in table.items():
            key = f"{section}.{name}"
            if key not in BY_KEY:
                raise ConfigError(f"unknown option {key}")
            values[key] = _coerce(BY_KEY[key], value)
    return Config(values=values, rules=parse_rules(data.get("rules")), path=path, mtime=mtime)


def load(path: Path | None = None) -> Config:
    """
    Read the config file. A missing file gives the defaults.

    :param path: The file, default ``paths.config_file()``
    :returns: The config
    :raises ConfigError: When the file is not valid TOML or does not fit the schema
    """
    path = path or paths.config_file()
    try:
        raw = path.read_bytes()
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return Config(values=defaults(), path=path)
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from None
    return from_mapping(data, path, mtime)


def set_value(cfg: Config, key: str, value: Any) -> Config:
    """
    Return a copy of the config with one option changed and validated.

    :param cfg: The current config
    :param key: A dotted key such as ``memory.sustain``
    :param value: The new value; strings are converted to the option's type
    :returns: The new config
    :raises ConfigError: For an unknown key or a bad value
    """
    if key not in BY_KEY:
        close = [k for k in BY_KEY if k.endswith("." + key.split(".")[-1])]
        hint = f" (did you mean {', '.join(close)}?)" if close else ""
        raise ConfigError(f"unknown option {key}{hint}")
    values = dict(cfg.values)
    values[key] = _coerce(BY_KEY[key], value)
    return Config(values=values, rules=list(cfg.rules), path=cfg.path, mtime=cfg.mtime)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = repr(value)
        return text if any(c in text for c in ".en") else text + ".0"
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return _toml_string(str(value))


def _toml_string(text: str) -> str:
    out = ['"']
    for char in text:
        code = ord(char)
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        elif char == "\r":
            out.append("\\r")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _wrap(text: str, width: int = 116) -> Iterable[str]:
    line = ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            yield line
            line = word
        else:
            line = f"{line} {word}" if line else word
    if line:
        yield line


def render(cfg: Config) -> str:
    """
    Render the config as a commented TOML document, with options at their default commented out.

    :param cfg: The config
    :returns: TOML text that ``load`` reads back to the same values
    """
    lines = [
        "# huntaway configuration.",
        "# Edit this file, use the dashboard's Settings page, or run: huntaway config set KEY VALUE",
        "# Options left commented out keep their default. huntaway reloads this file within a few seconds.",
        "# The full guide: man huntaway, or huntaway guide.",
        "",
    ]
    for section, about in SECTIONS.items():
        lines.append(f"[{section}]")
        lines.extend(f"# {part}" for part in _wrap(about))
        for opt in SCHEMA:
            if not opt.key.startswith(section + "."):
                continue
            name = opt.key.split(".", 1)[1]
            value = cfg.values[opt.key]
            lines.append("")
            lines.extend(f"# {part}" for part in _wrap(opt.help))
            if opt.choices:
                lines.append(f"# One of: {', '.join(opt.choices)}.")
            prefix = "" if value != opt.default else "# "
            lines.append(f"{prefix}{name} = {_toml_value(value)}")
        lines.append("")
    lines.append("# Rules give matching processes their own limits. match is a regular expression over the process")
    lines.append("# name or its full command line. action is one of: throttle, terminate, kill, notify.")
    if not cfg.rules:
        lines.extend(
            [
                "#",
                "# [[rules]]",
                '# name = "dev servers"',
                '# match = "node .*(vite|next dev|webpack)"',
                "# memory_mb = 4096",
                "# sustain = 30",
                '# action = "terminate"',
            ]
        )
    for rule in cfg.rules:
        lines.append("")
        lines.append("[[rules]]")
        for key, value in rule.to_dict().items():
            lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


def save(cfg: Config, path: Path | None = None) -> Path:
    """
    Write the config atomically, readable by the owner only.

    :param cfg: The config
    :param path: The file, default the config's own path or ``paths.config_file()``
    :returns: The path written
    """
    path = path or cfg.path or paths.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(render(cfg))
    os.replace(tmp, path)
    return path


def schema() -> list[dict[str, Any]]:
    """
    Return the schema for the dashboard's Settings page.

    :returns: One dict per option
    """
    return [
        {
            "key": o.key,
            "section": o.key.split(".", 1)[0],
            "name": o.key.split(".", 1)[1],
            "kind": o.kind,
            "default": o.default,
            "help": o.help,
            "choices": list(o.choices),
            "low": o.low,
            "high": o.high,
        }
        for o in SCHEMA
    ]
