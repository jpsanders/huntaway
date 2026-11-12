"""
Quick security checks that need no root and no extra tools.

Quick checks that need no root and no extra tools. Each one returns findings, and the ones that compare against a
baseline say what is new since the first run.

They target what goes wrong on a machine where agents run code: a shell start file that gained a line, a new cron
job or autostart entry, a new SSH key, a process running from a temporary folder, a miner, a dev server listening on
every interface, keys readable by other users, and an agent CLI started with its safety checks off by a process
without a terminal (the s1ngularity supply chain attack did exactly that in 2025).
"""

import hashlib
import os
import re
import shutil
import stat
import subprocess  # nosec B404  # fixed commands only, never a shell
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil

from huntaway.agents.harnesses import Agent
from huntaway.store import Store

Finding = dict[str, Any]

SHELL_FILES = (
    ".bashrc",
    ".bash_profile",
    ".bash_login",
    ".profile",
    ".zshrc",
    ".zprofile",
    ".zshenv",
    ".zlogin",
    ".config/fish/config.fish",
)
RISKY_LINES = re.compile(
    r"(shutdown|poweroff|halt|reboot)\b|curl[^|#\n]*\|\s*(ba|z)?sh|wget[^|#\n]*\|\s*(ba|z)?sh|base64\s+(-d|--decode)"
    r"[^|#\n]*\|\s*(ba|z)?sh|/dev/tcp/|\bnc\b[^#\n]*\s-e\s|\bsocat\b[^#\n]*exec:|LD_PRELOAD=|chmod\s+\+s\b",
    re.IGNORECASE,
)
MINERS = frozenset(
    {
        "xmrig",
        "xmr-stak",
        "minerd",
        "cpuminer",
        "cgminer",
        "bfgminer",
        "nbminer",
        "t-rex",
        "lolminer",
        "ethminer",
        "kdevtmpfsi",
        "kinsing",
        "phoenixminer",
        "gminer",
        "teamredminer",
        "srbminer",
    }
)
TEMP_DIRS = ("/tmp/", "/var/tmp/", "/dev/shm/")  # nosec B108  # compared against, never written
CREDENTIAL_FILES = (
    ".ssh/id_rsa",
    ".ssh/id_ed25519",
    ".ssh/id_ecdsa",
    ".aws/credentials",
    ".netrc",
    ".git-credentials",
    ".pypirc",
    ".npmrc",
    ".docker/config.json",
    ".config/gh/hosts.yml",
    ".kube/config",
    ".claude.json",
    ".codex/auth.json",
    ".local/share/opencode/auth.json",
)


def _id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _finding(check: str, severity: str, title: str, detail: str = "", key: str = "") -> Finding:
    return {"id": f"{check}:{_id(check, key or title)}", "severity": severity, "title": title, "detail": detail}


def _hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def shell_files(store: Store, home: Path) -> list[Finding]:
    """
    Shell start files that changed since the baseline, and risky lines in them.

    :param store: The database holding the baseline
    :param home: The home folder
    :returns: Findings
    """
    out: list[Finding] = []
    base = store.baseline("shell_files")
    known = "_taken" in base
    now: dict[str, str] = {}
    for name in SHELL_FILES:
        path = home / name
        digest = _hash(path)
        if not digest:
            continue
        now[name] = digest
        if known and base.get(name) not in (None, digest):
            out.append(
                _finding(
                    "shell",
                    "medium",
                    f"~/{name} changed since the baseline",
                    "Read the change. Malware adds lines here to run at every new shell.",
                    name + digest,
                )
            )
        elif known and name not in base:
            out.append(_finding("shell", "low", f"~/{name} appeared since the baseline", "", name + digest))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or not RISKY_LINES.search(stripped):
                continue
            out.append(
                _finding("shell", "high", f"Risky line in ~/{name}, line {number}", stripped[:200], name + stripped)
            )
    if "_taken" not in base:
        store.set_baseline("shell_files", {"_taken": "1", **now})
    return out


def _crontab() -> str:
    tool = shutil.which("crontab")
    if not tool:
        return ""
    try:
        result = subprocess.run([tool, "-l"], capture_output=True, text=True, timeout=5)  # nosec B603  # fixed command
    except OSError, subprocess.SubprocessError:
        return ""
    return result.stdout if result.returncode == 0 else ""


def persistence(store: Store, home: Path, crontab: Callable[[], str] = _crontab) -> list[Finding]:
    """
    Find new ways for something to start on its own: cron, autostart, user services, SSH keys.

    :param store: The database holding the baseline
    :param home: The home folder
    :param crontab: Reads the user's crontab
    :returns: Findings for entries added since the baseline
    """
    entries: dict[str, str] = {}
    for line in crontab().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries[f"cron: {line[:300]}"] = "cron"
    for folder, kind in (
        (home / ".config" / "autostart", "autostart"),
        (home / ".config" / "systemd" / "user", "user service"),
        (home / "Library" / "LaunchAgents", "launch agent"),
    ):
        try:
            for item in sorted(folder.iterdir()):
                if item.suffix in (".desktop", ".service", ".timer", ".plist"):
                    entries[f"{kind}: {item.name} {_hash(item)[:12]}"] = kind
        except OSError:
            continue
    keys = home / ".ssh" / "authorized_keys"
    try:
        for line in keys.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 2 and not line.startswith("#"):
                entries[f"ssh key: {parts[-1] if len(parts) > 2 else parts[1][-16:]} {_id(line)}"] = "ssh key"
    except OSError:
        pass
    base = store.baseline("persistence")
    if "_taken" not in base:
        store.set_baseline("persistence", {"_taken": "1", **entries})
        return []
    out: list[Finding] = []
    for entry, kind in entries.items():
        if entry not in base:
            severity = "high" if kind in ("ssh key", "cron") else "medium"
            out.append(_finding("persist", severity, f"New {kind} since the baseline", entry, entry))
    return out


def processes(procs: list[dict[str, Any]] | None = None) -> list[Finding]:
    """
    Find processes running from a temporary folder or a deleted file, miners and Windows programs.

    :param procs: Dicts with pid, name and exe; every process when None
    :returns: Findings
    """
    if procs is None:
        procs = [p.info for p in psutil.process_iter(["pid", "name", "exe"])]
    out: list[Finding] = []
    for info in procs:
        name = str(info.get("name") or "")
        exe = str(info.get("exe") or "")
        pid = info.get("pid")
        lower = name.lower()
        if lower in MINERS:
            out.append(_finding("proc", "critical", f"Crypto miner running: {name} (pid {pid})", exe, lower + exe))
        if exe.startswith(TEMP_DIRS):
            out.append(_finding("proc", "high", f"{name} runs from a temporary folder (pid {pid})", exe, exe))
        if exe.endswith(" (deleted)") and not exe.startswith(("/memfd:", "/usr/", "/opt/", "/snap/")):
            out.append(
                _finding(
                    "proc",
                    "medium",
                    f"{name} runs from a file that was deleted (pid {pid})",
                    "Normal right after an update; a program that deletes itself is not.",
                    exe,
                )
            )
        if lower.endswith(".exe") and os.name != "nt":
            out.append(
                _finding(
                    "proc",
                    "low",
                    f"Windows program running: {name} (pid {pid})",
                    "Usually Wine. Unexpected ones deserve a look.",
                    lower,
                )
            )
    return out


def listeners(store: Store) -> list[Finding]:
    """
    Ports open to the network, flagging the ones new since the baseline.

    :param store: The database holding the baseline
    :returns: Findings
    """
    try:
        conns = psutil.net_connections(kind="inet")
    except psutil.Error, OSError:
        return []
    found: dict[str, str] = {}
    for c in conns:
        if c.status != psutil.CONN_LISTEN or not c.laddr:
            continue
        ip, port = c.laddr.ip, c.laddr.port
        if ip.startswith("127.") or ip == "::1":
            continue
        name = ""
        if c.pid:
            try:
                name = psutil.Process(c.pid).name()
            except psutil.Error, OSError:
                name = ""
        found[f"{port}/{name or '?'}"] = ip
    base = store.baseline("listeners")
    if "_taken" not in base:
        store.set_baseline("listeners", {"_taken": "1", **found})
        return []
    out = []
    for key, ip in found.items():
        if key not in base and key != "_taken":
            port_text, name = key.split("/", 1)
            out.append(
                _finding(
                    "net",
                    "medium",
                    f"New port open to the network: {port_text} ({name}) on {ip}",
                    "Dev servers bound to 0.0.0.0 are reachable by everyone on the network.",
                    key,
                )
            )
    return out


def credentials(home: Path) -> list[Finding]:
    """
    Key and token files that other users can read.

    :param home: The home folder
    :returns: Findings
    """
    out = []
    for name in CREDENTIAL_FILES:
        path = home / name
        try:
            mode = path.stat().st_mode
        except OSError:
            continue
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            out.append(
                _finding("creds", "medium", f"~/{name} is readable by other users", f"Run: chmod 600 ~/{name}", name)
            )
    return out


def agent_guard(agents: list[Agent]) -> list[Finding]:
    """
    Agent CLIs running with their permission checks off. Without a terminal, that is how malware drives them.

    :param agents: The agent sessions found
    :returns: Findings
    """
    out = []
    for a in agents:
        if not a.flags:
            continue
        flags = " ".join(a.flags)
        if a.headless_bypass:
            out.append(
                _finding(
                    "agent",
                    "high",
                    f"{a.label} runs with checks off and no terminal (pid {a.pid})",
                    f"Flags: {flags}. Started from {a.cwd or 'an unknown folder'}. Check what started "
                    "it: the s1ngularity attack ran agent CLIs this way to hunt for secrets.",
                    f"{a.harness}{a.pid}{a.started}",
                )
            )
        else:
            out.append(
                _finding(
                    "agent",
                    "info",
                    f"{a.label} session runs with permission checks off (pid {a.pid})",
                    f"Flags: {flags}. Fine when you started it.",
                    f"{a.harness}{a.pid}{a.started}",
                )
            )
    return out


def run_all(store: Store, agents: list[Agent], home: Path | None = None) -> dict[str, list[Finding]]:
    """
    Run every quick check and store the results.

    :param store: The database
    :param agents: The agent sessions found
    :param home: The home folder
    :returns: Check name to findings
    """
    home = home or Path.home()
    checks: dict[str, Callable[[], list[Finding]]] = {
        "shell": lambda: shell_files(store, home),
        "persistence": lambda: persistence(store, home),
        "processes": lambda: processes(),
        "listeners": lambda: listeners(store),
        "credentials": lambda: credentials(home),
        "agents": lambda: agent_guard(agents),
    }
    results: dict[str, list[Finding]] = {}
    for name, check in checks.items():
        start = time.perf_counter()
        try:
            findings = check()
            status = "warnings" if any(f["severity"] not in ("info",) for f in findings) else "ok"
        except (OSError, ValueError, psutil.Error) as exc:
            findings, status = [], "failed"
            store.add_scan(f"quick:{name}", time.perf_counter() - start, status, {"error": str(exc)})
            continue
        store.upsert_findings(f"quick:{name}", findings)
        store.add_scan(f"quick:{name}", time.perf_counter() - start, status, {"findings": len(findings)})
        results[name] = findings
    return results


def rebaseline(store: Store) -> None:
    """
    Forget the baselines, so the next quick check takes new ones. For after a change you made yourself.

    :param store: The database
    """
    for name in ("shell_files", "persistence", "listeners"):
        store.set_baseline(name, {})
