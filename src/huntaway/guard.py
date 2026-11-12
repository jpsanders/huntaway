"""
The rules: what is protected, when memory is short, when a process is a hog.

The rules: what counts as protected, when memory is short, when a process is a hog, and what to do about each. The
guard only decides. The daemon carries out its decisions.
"""

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from huntaway import paths
from huntaway.agents.harnesses import Agent
from huntaway.config import Config, Rule
from huntaway.osinfo import CallWatch
from huntaway.sampler import Proc, Snapshot, descendants, tree

# Never touched: the desktop, sound, terminals (closing one closes every tab), remote access, secrets and calls.
# Linux truncates process names to 15 characters, so entries are matched as prefixes of the truncated name.
BUILTIN_PROTECTED = frozenset(
    {
        "systemd",
        "init",
        "launchd",
        "(sd-pam)",
        "dbus-daemon",
        "dbus-broker",
        "dbus-broker-launch",
        "sshd",
        "ssh-agent",
        "gpg-agent",
        "gnome-shell",
        "gnome-session-binary",
        "gnome-keyring-daemon",
        "mutter",
        "kwin_wayland",
        "kwin_x11",
        "plasmashell",
        "ksmserver",
        "kded5",
        "kded6",
        "sway",
        "hyprland",
        "niri",
        "wayfire",
        "labwc",
        "weston",
        "river",
        "xorg",
        "xwayland",
        "gdm-wayland-session",
        "gdm-x-session",
        "sddm",
        "lightdm",
        "pipewire",
        "pipewire-pulse",
        "wireplumber",
        "pulseaudio",
        "xdg-desktop-portal",
        "xdg-desktop-portal-gnome",
        "xdg-desktop-portal-kde",
        "xdg-desktop-portal-gtk",
        "xdg-document-portal",
        "xdg-permission-store",
        "ibus-daemon",
        "fcitx5",
        "networkmanager",
        "tmux: server",
        "tmux",
        "screen",
        "zellij",
        "gnome-terminal-server",
        "gnome-terminal",
        "ptyxis",
        "ptyxis-agent",
        "konsole",
        "kitty",
        "alacritty",
        "wezterm-gui",
        "foot",
        "ghostty",
        "tilix",
        "xfce4-terminal",
        "terminator",
        "warp",
        "keepassxc",
        "1password",
        "bitwarden",
        "kwalletd5",
        "kwalletd6",
        "zoom",
        "teams",
        "teams-for-linux",
        "windowserver",
        "loginwindow",
        "dock",
        "finder",
        "huntaway",
    }
)


def _short(name: str) -> str:
    return name.lower()[:15]


BUILTIN_SHORT = frozenset(_short(n) for n in BUILTIN_PROTECTED)


@dataclass(slots=True)
class Decision:
    """Something the guard wants done."""

    kind: str
    action: str
    pid: int
    started: float
    name: str
    reason: str
    severity: str = "medium"
    detail: dict[str, Any] = field(default_factory=dict)
    agent: Agent | None = None

    def key(self) -> tuple[int, float]:
        """
        Return the process this is about.

        :returns: Process id and start time
        """
        return (self.pid, self.started)


def read_leases(folder: Path | None = None) -> set[int]:
    """
    Process ids started with ``huntaway run``, whose descendants count as expected heavy work.

    :param folder: The lease folder
    :returns: Live lease pids
    """
    folder = folder or paths.runtime_dir() / "leases"
    pids: set[int] = set()
    try:
        entries = list(folder.glob("*.json"))
    except OSError:
        return pids
    for entry in entries:
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
            pid = int(data["pid"])
        except OSError, ValueError, KeyError, TypeError:
            continue
        if psutil.pid_exists(pid):
            pids.add(pid)
        else:
            entry.unlink(missing_ok=True)
    return pids


class Guard:
    """Holds the timers between samples and turns each sample into decisions."""

    def __init__(self, cfg: Config, calls: CallWatch | None = None, cmdline: Callable[[Proc], str] | None = None):
        self.cfg = cfg
        self.calls = calls or CallWatch()
        self.cmdline = cmdline or _read_cmdline
        self.uid = os.getuid()
        self.me = os.getpid()
        self.mem_short_since: float | None = None
        self.cooldown_until = 0.0
        self.hog_since: dict[tuple[int, float], float] = {}
        self.cap_since: dict[tuple[int, float], float] = {}
        self.rule_since: dict[tuple[int, int, float], float] = {}
        self.done: dict[tuple[int, float], float] = {}
        self.spared: dict[tuple[int, float], float] = {}
        self.leases: set[int] = set()
        self.level = "ok"
        self._cmd_cache: dict[tuple[int, float], str] = {}
        self._patterns: list[re.Pattern[str]] = []
        self._names: frozenset[str] = BUILTIN_SHORT
        self.reload(cfg)

    def reload(self, cfg: Config) -> None:
        """
        Apply new settings.

        :param cfg: The settings
        """
        self.cfg = cfg
        self._names = BUILTIN_SHORT | frozenset(_short(n) for n in cfg["protect.names"])
        self._patterns = [re.compile(p) for p in cfg["protect.patterns"]]

    def command(self, proc: Proc) -> str:
        """
        Return a process's command line, read once per process and cached.

        :param proc: The process
        :returns: The command line joined by spaces
        """
        key = proc.key()
        cmd = self._cmd_cache.get(key)
        if cmd is None:
            cmd = self.cmdline(proc)
            self._cmd_cache[key] = cmd
        return cmd

    def protection(self, proc: Proc, family: set[int], leased: set[int]) -> str | None:
        """
        Why a process must not be touched.

        :param proc: The process
        :param family: huntaway's own process and its ancestors
        :param leased: Processes started under ``huntaway run``
        :returns: The reason, or None when the process may be acted on
        """
        if proc.pid <= 2 or proc.ppid == 2 or proc.pid in family:
            return "system process"
        if proc.uid != self.uid and self.uid != 0:
            return "belongs to another user"
        short = _short(proc.name)
        if short in self._names:
            return "protected name"
        if self.cfg["protect.calls"] and proc.name.lower() in self.calls.names():
            return "in a call"
        if self._patterns:
            cmd = self.command(proc)
            if any(p.search(cmd) for p in self._patterns):
                return "protected pattern"
        if proc.pid in leased:
            return "started with huntaway run"
        return None

    def update(self, snap: Snapshot, agents: list[Agent], now: float | None = None) -> list[Decision]:
        """
        Turn one sample into decisions.

        :param snap: The sample
        :param agents: Agent sessions found in it
        :param now: The current time
        :returns: Decisions, most urgent first
        """
        now = now or snap.ts
        live = {p.key() for p in snap.procs}
        for cache in (self.hog_since, self.cap_since, self.done, self.spared, self._cmd_cache):
            for key in [k for k in cache if k not in live]:
                del cache[key]
        self.rule_since = {k: v for k, v in self.rule_since.items() if (k[1], k[2]) in live}
        kids = tree(snap.procs)
        family = {self.me} | _ancestors(self.me, {p.pid: p for p in snap.procs})
        leased: set[int] = set()
        for pid in self.leases:
            leased |= {pid} | descendants(pid, kids)
        protected: dict[int, str] = {}
        for p in snap.procs:
            reason = self.protection(p, family, leased)
            if reason:
                protected[p.pid] = reason
        decisions: list[Decision] = []
        decisions += self._memory(snap, agents, protected, leased, now)
        if decisions:
            return decisions
        decisions += self._process_caps(snap, protected, now)
        decisions += self._cpu(snap, protected, agents, now)
        decisions += self._rules(snap, protected, now)
        return decisions

    def _memory(
        self, snap: Snapshot, agents: list[Agent], protected: dict[int, str], leased: set[int], now: float
    ) -> list[Decision]:
        cfg = self.cfg
        if not cfg["memory.enabled"]:
            self.level = "ok"
            return []
        avail = snap.mem_available_percent
        swap_free = snap.swap_free_percent
        floor = cfg["memory.min_available_percent"]
        full = snap.psi.get("memory_full", 0.0)
        critical = avail < floor and swap_free < cfg["memory.min_swap_free_percent"]
        if cfg["memory.pressure_full"] > 0 and full >= cfg["memory.pressure_full"]:
            critical = True
        tight = critical or avail < floor * 2 or (snap.swap_total > 0 and swap_free < 5 and avail < floor * 3)
        self.level = "critical" if critical else "tight" if tight else "ok"
        if not critical:
            self.mem_short_since = None
            return []
        if self.mem_short_since is None:
            self.mem_short_since = now
        if now - self.mem_short_since < cfg["memory.sustain"] or now < self.cooldown_until:
            return []
        why = f"memory short: {avail:.1f}% available, swap {swap_free:.0f}% free"
        if full >= cfg["memory.pressure_full"] > 0:
            why += f", stalled {full:.0f}% of the last 10 s"
        if cfg["memory.prefer_idle_agents"] and cfg["agents.enabled"]:
            idle = [a for a in agents if a.interactive and not a.pinned and a.status != "busy" and a.idle(now) >= 60]
            idle = [a for a in idle if a.pid not in protected]
            if idle:
                pick = max(idle, key=lambda a: (a.idle(now) >= cfg["agents.pressure_idle_minutes"] * 60, a.rss))
                self.cooldown_until = now + cfg["memory.grace"] + 4
                return [
                    Decision(
                        "memory",
                        "close",
                        pick.pid,
                        pick.started,
                        pick.label,
                        why,
                        "high",
                        {"rss": pick.rss, "available": avail},
                        agent=pick,
                    )
                ]
        floor_bytes = cfg["memory.min_victim_mb"] << 20
        busy_agents = {a.pid for a in agents if a.status == "busy"}
        pool = [p for p in snap.procs if p.pid not in protected and p.rss >= floor_bytes and p.status != "zombie"]
        if not pool:
            return []
        pool.sort(key=lambda p: (p.pid not in busy_agents and p.pid not in leased, p.rss), reverse=True)
        victim = pool[0]
        self.cooldown_until = now + cfg["memory.grace"] + 4
        return [
            Decision(
                "memory",
                "terminate",
                victim.pid,
                victim.started,
                victim.name,
                why,
                "high",
                {"rss": victim.rss, "available": avail},
            )
        ]

    def _process_caps(self, snap: Snapshot, protected: dict[int, str], now: float) -> list[Decision]:
        cap = self.cfg["memory.process_max_percent"]
        if not cap or not self.cfg["memory.enabled"] or not snap.mem_total:
            return []
        out: list[Decision] = []
        for p in snap.procs:
            share = 100.0 * p.rss / snap.mem_total
            key = p.key()
            if share < cap or p.pid in protected:
                self.cap_since.pop(key, None)
                continue
            since = self.cap_since.setdefault(key, now)
            if now - since >= self.cfg["memory.sustain"] and key not in self.done:
                self.done[key] = now
                out.append(
                    Decision(
                        "process_memory",
                        "terminate",
                        p.pid,
                        p.started,
                        p.name,
                        f"holds {share:.0f}% of RAM, over the {cap:g}% limit",
                        "high",
                        {"rss": p.rss},
                    )
                )
        return out

    def _cpu(self, snap: Snapshot, protected: dict[int, str], agents: list[Agent], now: float) -> list[Decision]:
        cfg = self.cfg
        if not cfg["cpu.enabled"]:
            return []
        threshold = cfg["cpu.process_percent"]
        busy = cfg["cpu.system_percent"] <= 0 or snap.cpu >= cfg["cpu.system_percent"]
        action = cfg["cpu.action"]
        out: list[Decision] = []
        for p in snap.procs:
            key = p.key()
            if p.cpu < threshold or p.pid in protected:
                self.hog_since.pop(key, None)
                continue
            since = self.hog_since.setdefault(key, now)
            if not busy or now - since < cfg["cpu.sustain"]:
                continue
            last = self.done.get(key)
            if last is not None and (action != "notify" or now - last < 3600):
                continue
            if key in self.spared and now < self.spared[key]:
                continue
            self.done[key] = now
            out.append(
                Decision(
                    "cpu",
                    action,
                    p.pid,
                    p.started,
                    p.name,
                    f"{p.cpu:.0f}% CPU for {now - since:.0f} s while the machine ran at {snap.cpu:.0f}%",
                    "low" if action in ("throttle", "notify") else "medium",
                    {"cpu": p.cpu},
                )
            )
        return out

    def _rules(self, snap: Snapshot, protected: dict[int, str], now: float) -> list[Decision]:
        if not self.cfg.rules:
            return []
        out: list[Decision] = []
        for index, rule in enumerate(self.cfg.rules):
            for p in snap.procs:
                if p.pid in protected:
                    continue
                over = _over(rule, p)
                key3 = (index, p.pid, p.started)
                if not over or not (rule.match.search(p.name) or rule.match.search(self.command(p))):
                    self.rule_since.pop(key3, None)
                    continue
                since = self.rule_since.setdefault(key3, now)
                key = p.key()
                if now - since < rule.sustain or key in self.done:
                    continue
                if key in self.spared and now < self.spared[key]:
                    continue
                self.done[key] = now
                out.append(
                    Decision(
                        "rule",
                        rule.action,
                        p.pid,
                        p.started,
                        p.name,
                        f"rule '{rule.name}': {over}",
                        "medium",
                        {"rule": rule.name, "cpu": p.cpu, "rss": p.rss},
                    )
                )
        return out

    def spare(self, decision: Decision, seconds: float = 600.0) -> None:
        """
        Leave a process alone for a while, after the judge spared it.

        :param decision: The decision that was vetoed
        :param seconds: How long
        """
        self.spared[decision.key()] = time.time() + seconds
        self.done.pop(decision.key(), None)


def _over(rule: Rule, p: Proc) -> str:
    reasons = []
    if rule.cpu_percent and p.cpu >= rule.cpu_percent:
        reasons.append(f"{p.cpu:.0f}% CPU over {rule.cpu_percent:g}%")
    if rule.memory_mb and p.rss >= rule.memory_mb * (1 << 20):
        reasons.append(f"{p.rss >> 20} MB over {rule.memory_mb:g} MB")
    if not rule.cpu_percent and not rule.memory_mb:
        reasons.append("matched")
    if (rule.cpu_percent or rule.memory_mb) and not reasons:
        return ""
    return ", ".join(reasons)


def _ancestors(pid: int, by_pid: dict[int, Proc]) -> set[int]:
    out: set[int] = set()
    cur = by_pid.get(pid)
    while cur is not None and cur.ppid not in out and cur.ppid > 0:
        out.add(cur.ppid)
        cur = by_pid.get(cur.ppid)
    return out


def _read_cmdline(proc: Proc) -> str:
    try:
        with open(f"/proc/{proc.pid}/cmdline", "rb") as handle:
            return handle.read(8192).replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        try:
            return " ".join(psutil.Process(proc.pid).cmdline())
        except psutil.Error, OSError:
            return ""
