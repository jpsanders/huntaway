"""
Find AI agent sessions among the processes.

And work out, for each, its session id, its transcript files, when it last did anything and the command that resumes
it.
"""

import glob
import json
import os
import re
import shutil
import subprocess  # nosec B404  # fixed commands only, never a shell
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from huntaway import osinfo
from huntaway.sampler import Proc, tree

# Flags that let an agent act without asking. Seen on an agent with no terminal, they are the s1ngularity pattern:
# malware that runs a local agent CLI to search the disk for secrets.
BYPASS_FLAGS = (
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--dangerously-bypass-approvals-and-sandbox",
    "--yolo",
    "--trust-all-tools",
    "--approval-mode=yolo",
)


@dataclass(frozen=True, slots=True)
class Harness:
    """How to recognise one agent harness."""

    key: str
    label: str
    names: frozenset[str]
    markers: tuple[str, ...] = ()


HARNESSES: tuple[Harness, ...] = (
    Harness("claude", "Claude Code", frozenset({"claude"}), ("@anthropic-ai/claude-code", "/claude/versions/")),
    Harness("codex", "Codex", frozenset({"codex"}), ("@openai/codex",)),
    Harness("opencode", "OpenCode", frozenset({"opencode", ".opencode"}), ("opencode-ai",)),
    Harness("gemini", "Gemini CLI", frozenset({"gemini"}), ("@google/gemini-cli",)),
    Harness("qwen", "Qwen Code", frozenset({"qwen"}), ("@qwen-code/qwen-code",)),
    Harness("aider", "Aider", frozenset({"aider"}), ("/bin/aider", "aider-chat")),
    Harness("cursor", "Cursor Agent", frozenset({"cursor-agent"}), ("cursor-agent",)),
    Harness("goose", "Goose", frozenset({"goose"}), ()),
    Harness("amp", "Amp", frozenset({"amp"}), ("@sourcegraph/amp",)),
    Harness("copilot", "Copilot CLI", frozenset({"copilot"}), ("@github/copilot",)),
    Harness("crush", "Crush", frozenset({"crush"}), ()),
    Harness("kiro", "Kiro CLI", frozenset({"kiro-cli", "kiro"}), ()),
)
BY_KEY = {h.key: h for h in HARNESSES}
LAUNCHERS = frozenset({"node", "bun", "deno", "python", "python3", "npx", "MainThread"})


@dataclass(slots=True)
class Agent:
    """One running agent session."""

    harness: str
    label: str
    pid: int
    pids: list[int]
    started: float
    cwd: str = ""
    tty: str | None = None
    rss: int = 0
    cpu: float = 0.0
    session_id: str | None = None
    status: str = "unknown"
    last_active: float = 0.0
    activity: str = "start"
    interactive: bool = False
    title: str = ""
    files: list[str] = field(default_factory=list)
    resume: str = ""
    config_dir: str | None = None
    tmux_pane: str | None = None
    flags: list[str] = field(default_factory=list)
    headless_bypass: bool = False
    pinned: bool = False
    protected: str | None = None

    def idle(self, now: float | None = None) -> float:
        """
        Seconds since the session last did anything.

        :param now: The current time
        :returns: Seconds, zero while busy
        """
        if self.status == "busy":
            return 0.0
        return max(0.0, (now or time.time()) - self.last_active)

    def key(self) -> str:
        """
        Return the stable id the archive and pins use.

        :returns: ``harness:session`` or ``harness:pid-<pid>``
        """
        return f"{self.harness}:{self.session_id or f'pid-{self.pid}'}"

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        """
        Return the session for the API.

        :param now: The current time
        :returns: A JSON ready dict
        """
        now = now or time.time()
        return {
            "id": self.key(),
            "harness": self.harness,
            "label": self.label,
            "pid": self.pid,
            "pids": self.pids,
            "started": self.started,
            "cwd": self.cwd,
            "tty": self.tty,
            "rss": self.rss,
            "cpu": round(self.cpu, 1),
            "session_id": self.session_id,
            "status": self.status,
            "last_active": self.last_active,
            "idle": round(self.idle(now)),
            "activity": self.activity,
            "interactive": self.interactive,
            "title": self.title,
            "files": self.files,
            "resume": self.resume,
            "tmux_pane": self.tmux_pane,
            "flags": self.flags,
            "headless_bypass": self.headless_bypass,
            "pinned": self.pinned,
            "protected": self.protected,
        }


class _Ctx:
    """Cached reads of a live process: command line, working folder, terminal and environment."""

    def __init__(self) -> None:
        self._cache: dict[tuple[int, float], dict[str, Any]] = {}

    def get(self, proc: Proc) -> dict[str, Any]:
        key = proc.key()
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        info: dict[str, Any] = {"cmdline": [], "cwd": "", "tty": None, "env": {}, "exe": ""}
        try:
            p = psutil.Process(proc.pid)
            with p.oneshot():
                info["cmdline"] = p.cmdline()
                info["exe"] = _safe(p.exe, "")
                info["cwd"] = _safe(p.cwd, "")
                info["tty"] = _safe(p.terminal, None)
        except psutil.Error, OSError:
            pass
        self._cache[key] = info
        return info

    def env(self, proc: Proc) -> dict[str, str]:
        info = self.get(proc)
        if not info["env"]:
            try:
                info["env"] = psutil.Process(proc.pid).environ()
            except psutil.Error, OSError:
                info["env"] = {}
        env: dict[str, str] = info["env"]
        return env

    def prune(self, live: set[tuple[int, float]]) -> None:
        for key in list(self._cache):
            if key not in live:
                del self._cache[key]


def _safe(fn: Callable[[], Any], default: Any) -> Any:
    try:
        return fn()
    except psutil.Error, OSError:
        return default


def match(proc: Proc, ctx: _Ctx, extra: frozenset[str] = frozenset()) -> Harness | None:
    """
    Which harness a process belongs to, if any.

    :param proc: The process
    :param ctx: The cache of process details
    :param extra: More names to treat as agents
    :returns: The harness, or None
    """
    name = proc.name.lower()
    for h in HARNESSES:
        if name in h.names:
            return h
    if name in extra:
        return Harness(name, name, frozenset({name}))
    if name in LAUNCHERS or re.fullmatch(r"\d+(\.\d+){1,3}", name):
        joined = " ".join(ctx.get(proc)["cmdline"][:3]) + " " + str(ctx.get(proc)["exe"])
        for h in HARNESSES:
            if any(m in joined for m in h.markers):
                return h
    return None


class Finder:
    """Finds agent sessions in a sample. Keeps caches between calls, so a pass costs little."""

    def __init__(self, home: Path | None = None) -> None:
        self.home = home or Path.home()
        self.ctx = _Ctx()
        self.cpu_seen: dict[int, float] = {}

    def find(self, procs: list[Proc], extra: list[str] | None = None, now: float | None = None) -> list[Agent]:
        """
        Every agent session owned by this user.

        :param procs: The processes from a sample
        :param extra: More process names to treat as agents
        :param now: The current time
        :returns: Agents, one per session, biggest first
        """
        now = now or time.time()
        uid = os.getuid()
        extra_names = frozenset(n.lower() for n in (extra or []))
        by_pid = {p.pid: p for p in procs}
        hits: dict[int, Harness] = {}
        for p in procs:
            if p.uid != uid or p.status == "zombie":
                continue
            h = match(p, self.ctx, extra_names)
            if h is not None:
                hits[p.pid] = h
        kids = tree(procs)
        agents: list[Agent] = []
        merged: set[int] = set()
        for pid in sorted(hits, key=lambda x: by_pid[x].started):
            if pid in merged:
                continue
            h = hits[pid]
            chain = [pid]
            # A launcher such as node often wraps the real binary: fold its same harness child into the session.
            if by_pid[pid].name.lower() in LAUNCHERS:
                for child in kids.get(pid, ()):
                    if hits.get(child) == h:
                        chain.append(child)
                        merged.add(child)
            agents.append(self._build(h, chain, by_pid, kids, now, set(hits) - set(chain)))
        self.ctx.prune({p.key() for p in procs})
        live = {a.pid for a in agents}
        self.cpu_seen = {k: v for k, v in self.cpu_seen.items() if k in live}
        agents.sort(key=lambda a: a.rss, reverse=True)
        return agents

    def _build(
        self,
        h: Harness,
        chain: list[int],
        by_pid: dict[int, Proc],
        kids: dict[int, list[int]],
        now: float,
        others: set[int],
    ) -> Agent:
        main = by_pid[chain[-1]]
        top = by_pid[chain[0]]
        info = self.ctx.get(main)
        family = _family(top.pid, kids, others)
        rss = sum(by_pid[p].rss for p in family if p in by_pid)
        cpu = sum(by_pid[p].cpu for p in family if p in by_pid)
        if cpu >= 10.0:
            self.cpu_seen[main.pid] = now
        tty = info["tty"] or self.ctx.get(top)["tty"]
        cmd = info["cmdline"] or self.ctx.get(top)["cmdline"]
        flags = [f for f in cmd if f.split("=")[0] in {b.split("=")[0] for b in BYPASS_FLAGS} or f in BYPASS_FLAGS]
        agent = Agent(
            harness=h.key,
            label=h.label,
            pid=main.pid,
            pids=chain,
            started=main.started,
            cwd=info["cwd"] or self.ctx.get(top)["cwd"],
            tty=tty,
            rss=rss,
            cpu=cpu,
            interactive=tty is not None,
            last_active=max(top.started, self.cpu_seen.get(main.pid, 0.0)),
            activity="cpu" if self.cpu_seen.get(main.pid, 0.0) > top.started else "start",
            flags=flags,
        )
        agent.headless_bypass = bool(flags) and tty is None
        env = self.ctx.env(main)
        agent.tmux_pane = env.get("TMUX_PANE") if env.get("TMUX") else None
        discover = DISCOVERERS.get(h.key)
        if discover is not None:
            try:
                discover(self, agent, env, now)
            except OSError, ValueError, KeyError, subprocess.SubprocessError:
                pass
        if agent.tty and agent.activity in ("start", "cpu"):
            idle = osinfo.tty_input_idle(agent.tty, now)
            if idle is not None and now - idle > agent.last_active:
                agent.last_active, agent.activity = now - idle, "keyboard"
        if agent.status == "busy":
            agent.last_active, agent.activity = now, "busy"
        return agent

    def _touch(self, agent: Agent, path: str, now: float) -> None:
        stamp, title = transcript_tail(path)
        if stamp > agent.last_active and stamp <= now + 60:
            agent.last_active, agent.activity = stamp, "transcript"
        if title and not agent.title:
            agent.title = title


def transcript_tail(path: str, tail: int = 262144) -> tuple[float, str]:
    """
    When a transcript last gained a real message, and the title its harness gave it.

    Idle sessions keep appending lines without a timestamp (titles, modes), so the file's modification time says
    little. This reads the end of the file and returns the newest line that carries a timestamp. Files that are
    not JSON lines fall back to their modification time.

    :param path: The transcript
    :param tail: Bytes to read from the end
    :returns: Seconds since the epoch (0 when unreadable) and a title (empty when none)
    """
    try:
        with open(path, "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - tail))
            chunk = handle.read()
            mtime = os.fstat(handle.fileno()).st_mtime
    except OSError:
        return 0.0, ""
    if not path.endswith(".jsonl"):
        return mtime, ""
    stamp, title = 0.0, ""
    for line in reversed(chunk.splitlines()):
        if not title and b'"aiTitle"' in line:
            try:
                title = str(json.loads(line).get("aiTitle") or "")
            except ValueError, AttributeError:
                pass
        if stamp or b'"timestamp"' not in line:
            if stamp and title:
                break
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        stamp = _iso(row.get("timestamp")) if isinstance(row, dict) else 0.0
    return stamp, " ".join(title.split())[:120]


def _family(top: int, kids: dict[int, list[int]], stop: set[int]) -> set[int]:
    # The process and everything below it, without other agent sessions started from inside it.
    out = {top}
    stack = list(kids.get(top, ()))
    while stack:
        child = stack.pop()
        if child in out or child in stop:
            continue
        out.add(child)
        stack.extend(kids.get(child, ()))
    return out


def claude_slug(cwd: str) -> str:
    """
    Return the folder name Claude Code uses for a project under ``projects/``.

    :param cwd: The project's path
    :returns: The path with every character outside letters and digits replaced by a dash
    """
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _claude(f: Finder, agent: Agent, env: dict[str, str], now: float) -> None:
    cfg = Path(env.get("CLAUDE_CONFIG_DIR") or f.home / ".claude")
    agent.config_dir = str(cfg)
    meta_path = cfg / "sessions" / f"{agent.pid}.json"
    meta: dict[str, Any] = {}
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    sid = meta.get("sessionId")
    if meta.get("kind") and meta.get("kind") != "interactive":
        agent.interactive = False
    agent.status = str(meta.get("status") or "unknown")
    updated = max(float(meta.get("updatedAt") or 0), float(meta.get("statusUpdatedAt") or 0)) / 1000.0
    if updated > agent.last_active:
        agent.last_active, agent.activity = updated, "status"
    if meta.get("nameSource") not in (None, "derived"):
        agent.title = str(meta.get("name") or "")
    cwd = str(meta.get("cwd") or agent.cwd)
    agent.cwd = cwd
    if sid:
        agent.session_id = str(sid)
        transcript = cfg / "projects" / claude_slug(cwd) / f"{sid}.jsonl"
        if not transcript.is_file():
            found = glob.glob(str(cfg / "projects" / "*" / f"{glob.escape(str(sid))}.jsonl"))
            transcript = Path(found[0]) if found else transcript
        if transcript.is_file():
            agent.files.append(str(transcript))
            f._touch(agent, str(transcript), now)
            extra_dir = transcript.with_suffix("")
            if extra_dir.is_dir():
                agent.files.append(str(extra_dir))
        prefix = "" if cfg == f.home / ".claude" else f"CLAUDE_CONFIG_DIR={_short(str(cfg), f.home)} "
        agent.resume = f"cd {_quote(_short(cwd, f.home))} && {prefix}claude --resume {sid}"
    if not agent.title and agent.files:
        agent.title = first_prompt_claude(agent.files[0])


def first_prompt_claude(path: str, limit: int = 400) -> str:
    """
    Return the first thing the user typed in a Claude Code transcript.

    :param path: The JSONL transcript
    :param limit: Lines to read at most
    :returns: One line, up to 90 characters
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for i, line in enumerate(handle):
                if i >= limit:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("type") != "user" or row.get("isMeta"):
                    continue
                content = (row.get("message") or {}).get("content")
                text = content if isinstance(content, str) else ""
                if isinstance(content, list):
                    text = " ".join(
                        b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
                    )
                text = " ".join(text.split())
                if text and not text.startswith("<"):
                    return text[:90]
    except OSError:
        return ""
    return ""


def _codex(f: Finder, agent: Agent, env: dict[str, str], now: float) -> None:
    home = Path(env.get("CODEX_HOME") or f.home / ".codex")
    agent.config_dir = str(home)
    best: tuple[float, str, str] | None = None
    started = agent.started
    day_dirs = sorted(glob.glob(str(home / "sessions" / "*" / "*" / "*")))[-40:]
    for day in day_dirs:
        for path in glob.glob(os.path.join(day, "rollout-*.jsonl")):
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                continue
            if mtime < started - 5:
                continue
            meta = _codex_meta(path)
            if meta.get("cwd") and meta["cwd"] != agent.cwd:
                continue
            created = _iso(meta.get("timestamp"))
            # A fresh session is created within seconds of the process; a resumed one is older, rank it lower.
            score = -abs(created - started) if created else -1e9
            if best is None or score > best[0]:
                best = (score, path, str(meta.get("id") or ""))
    if best is not None:
        agent.files.append(best[1])
        agent.session_id = best[2] or None
        f._touch(agent, best[1], now)
        agent.title = agent.title or first_prompt_codex(best[1])
        if agent.session_id:
            agent.resume = f"cd {_quote(_short(agent.cwd, f.home))} && codex resume {agent.session_id}"


def _codex_meta(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            row = json.loads(handle.readline() or "{}")
    except OSError, ValueError:
        return {}
    payload = row.get("payload") if isinstance(row, dict) else None
    return payload if isinstance(payload, dict) else {}


def first_prompt_codex(path: str, limit: int = 400) -> str:
    """
    Return the first message the user sent in a Codex rollout.

    :param path: The JSONL rollout
    :param limit: Lines to read at most
    :returns: One line, up to 90 characters
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for i, line in enumerate(handle):
                if i >= limit:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                payload = row.get("payload") or {}
                text = ""
                if row.get("type") == "event_msg" and payload.get("type") == "user_message":
                    text = str(payload.get("message", ""))
                elif payload.get("type") == "message" and payload.get("role") == "user":
                    text = " ".join(str(c.get("text", "")) for c in payload.get("content") or [] if isinstance(c, dict))
                text = " ".join(text.split())
                if text and not text.startswith("<"):
                    return text[:90]
    except OSError:
        return ""
    return ""


def _iso(value: Any) -> float:
    if not isinstance(value, str):
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def opencode_sessions(cwd: str, limit: int = 20) -> list[dict[str, Any]]:
    """
    Return the newest OpenCode sessions of a project, from OpenCode's own CLI.

    :param cwd: The project folder
    :param limit: How many
    :returns: Dicts with id, title and updated (seconds since the epoch)
    """
    tool = shutil.which("opencode") or str(Path.home() / ".opencode" / "bin" / "opencode")
    if not os.path.exists(tool):
        return []
    try:
        result = subprocess.run(  # nosec B603  # fixed command, no shell
            [tool, "session", "list", "--format", "json", "-n", str(limit)],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=30,
        )
        data = json.loads(result.stdout or "[]")
    except OSError, subprocess.SubprocessError, ValueError:
        return []
    rows = data if isinstance(data, list) else data.get("sessions", []) if isinstance(data, dict) else []
    out = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        raw_time = row.get("time")
        times: dict[str, Any] = raw_time if isinstance(raw_time, dict) else {}
        updated = row.get("updated") or times.get("updated") or row.get("updatedAt") or 0
        try:
            stamp = float(updated)
        except TypeError, ValueError:
            stamp = _iso(updated)
        if stamp > 1e12:
            stamp /= 1000.0
        out.append({"id": str(row["id"]), "title": str(row.get("title") or ""), "updated": stamp})
    return out


def _opencode(f: Finder, agent: Agent, env: dict[str, str], now: float) -> None:
    # OpenCode keeps sessions in a database behind its own server; it is asked for the session at archive time.
    agent.config_dir = str(f.home / ".local" / "share" / "opencode")


def _gemini(f: Finder, agent: Agent, env: dict[str, str], now: float) -> None:
    import hashlib

    base = f.home / ".gemini" / "tmp"
    candidates = [base / hashlib.sha256(agent.cwd.encode()).hexdigest()]
    for folder in candidates:
        if folder.is_dir():
            files = [str(p) for p in folder.rglob("*") if p.is_file() and p.stat().st_mtime >= agent.started - 5]
            agent.files.extend(sorted(files)[:200])
            for path in files:
                f._touch(agent, path, now)
    agent.resume = f"cd {_quote(_short(agent.cwd, f.home))} && gemini --resume"


def _aider(f: Finder, agent: Agent, env: dict[str, str], now: float) -> None:
    for name in (".aider.chat.history.md", ".aider.input.history"):
        path = os.path.join(agent.cwd, name)
        if os.path.isfile(path):
            agent.files.append(path)
            f._touch(agent, path, now)
    agent.resume = f"cd {_quote(_short(agent.cwd, f.home))} && aider --restore-chat-history"


DISCOVERERS: dict[str, Callable[[Finder, Agent, dict[str, str], float], None]] = {
    "claude": _claude,
    "codex": _codex,
    "opencode": _opencode,
    "gemini": _gemini,
    "aider": _aider,
}


def _short(path: str, home: Path) -> str:
    text = str(home)
    return "~" + path[len(text) :] if path == text or path.startswith(text + os.sep) else path


def _quote(path: str) -> str:
    if re.fullmatch(r"[~\w@%+=:,./-]+", path):
        return path
    return "'" + path.replace("'", "'\"'\"'") + "'"
