"""
Small readers for what the operating system knows.

Pressure stall information, temperatures, live calls and terminal idle time. Every reader returns a safe empty value
where the feature does not exist.
"""

import json
import os
import shutil
import subprocess  # nosec B404  # fixed commands only, never a shell
import sys
import time
from pathlib import Path
from typing import Any

import psutil

PSI_DIR = Path("/proc/pressure")

# Temperature sensors that describe the CPU package, best first.
CPU_SENSORS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal", "soc_thermal", "acpitz")

# Process names of browsers and call apps that may carry a call.
CALL_APPS = frozenset(
    {
        "chrome",
        "chromium",
        "chromium-browser",
        "google-chrome",
        "msedge",
        "brave",
        "firefox",
        "firefox-bin",
        "vivaldi-bin",
        "opera",
        "zoom",
        "teams",
        "teams-for-linux",
        "slack",
        "discord",
        "signal-desktop",
        "skypeforlinux",
        "webex",
    }
)


def psi() -> dict[str, float]:
    """
    Pressure stall information: the share of the last 10 seconds that tasks stalled on CPU, memory or IO.

    :returns: Keys cpu, memory, memory_full, io, io_full in percent, empty where PSI is missing
    """
    out: dict[str, float] = {}
    for resource in ("cpu", "memory", "io"):
        try:
            text = (PSI_DIR / resource).read_text()
        except OSError:
            continue
        for line in text.splitlines():
            kind, _, rest = line.partition(" ")
            fields = dict(part.split("=", 1) for part in rest.split() if "=" in part)
            try:
                value = float(fields["avg10"])
            except KeyError, ValueError:
                continue
            out[resource if kind == "some" else f"{resource}_full"] = value
    return out


def cpu_temperature() -> float | None:
    """
    Return the CPU package temperature.

    :returns: Degrees Celsius, or None where no sensor is readable
    """
    reader = getattr(psutil, "sensors_temperatures", None)
    if reader is None:
        return None
    try:
        temps = reader()
    except OSError, RuntimeError:
        return None
    for name in CPU_SENSORS:
        entries = temps.get(name)
        if not entries:
            continue
        for entry in entries:
            if entry.label.lower().startswith(("package", "tctl", "tdie")):
                return float(entry.current)
        return float(max(e.current for e in entries))
    return None


def _pw_dump() -> list[dict[str, Any]]:
    tool = shutil.which("pw-dump")
    if not tool:
        return []
    try:
        result = subprocess.run([tool], capture_output=True, text=True, timeout=3)  # nosec B603  # fixed command
        nodes = json.loads(result.stdout or "[]")
    except OSError, subprocess.SubprocessError, ValueError:
        return []
    return nodes if isinstance(nodes, list) else []


def capturing_pids(nodes: list[dict[str, Any]] | None = None) -> set[int]:
    """
    Find processes holding a microphone or camera stream, which is what a call does.

    :param nodes: PipeWire objects, read with pw-dump when None
    :returns: The process ids that own an input stream
    """
    pids: set[int] = set()
    for node in _pw_dump() if nodes is None else nodes:
        if node.get("type") != "PipeWire:Interface:Node":
            continue
        props = (node.get("info") or {}).get("props") or {}
        if not str(props.get("media.class", "")).startswith("Stream/Input/"):
            continue
        try:
            pids.add(int(props.get("application.process.id", 0)))
        except TypeError, ValueError:
            continue
    pids.discard(0)
    return pids


class CallWatch:
    """Knows which app families are in a call. Reads PipeWire at most every few seconds."""

    def __init__(self, every: float = 5.0) -> None:
        self.every = every
        self._at = 0.0
        self._names: frozenset[str] = frozenset()

    def names(self) -> frozenset[str]:
        """
        Return the process names of apps holding an input stream right now.

        :returns: Lowercase names such as chrome or zoom
        """
        now = time.monotonic()
        if now - self._at < self.every:
            return self._names
        self._at = now
        names: set[str] = set()
        for pid in capturing_pids():
            try:
                names.add(psutil.Process(pid).name().lower())
            except psutil.Error, OSError:
                continue
        self._names = frozenset(names)
        return self._names


def tty_input_idle(tty: str | None, now: float | None = None) -> float | None:
    """
    Seconds since anyone typed into a terminal, from the device's access time, the way ``w`` reports it.

    :param tty: A device path such as /dev/pts/3
    :param now: The current time
    :returns: Seconds, or None when the terminal cannot be read
    """
    if not tty:
        return None
    try:
        atime = os.stat(tty).st_atime
    except OSError:
        return None
    return max(0.0, (now or time.time()) - atime)


def write_tty(tty: str, text: str) -> bool:
    """
    Print a note into a terminal the user owns.

    :param tty: A device path such as /dev/pts/3
    :param text: The note; line ends are converted for a raw terminal
    :returns: True when the note was written
    """
    try:
        fd = os.open(tty, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        os.write(fd, text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8", "replace"))
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def desktop_notify(title: str, body: str, urgent: bool = False) -> bool:
    """
    Show a desktop notification.

    :param title: The title
    :param body: The text
    :param urgent: Mark it critical where the desktop supports that
    :returns: True when a notifier ran
    """
    if sys.platform == "darwin":
        tool = shutil.which("osascript")
        if not tool:
            return False
        script = f"display notification {json.dumps(body)} with title {json.dumps(title)}"
        args = [tool, "-e", script]
    else:
        tool = shutil.which("notify-send")
        if not tool:
            return False
        args = [tool, "--app-name=huntaway", f"--urgency={'critical' if urgent else 'normal'}", title, body]
    try:
        subprocess.run(args, capture_output=True, timeout=5, check=False)  # nosec B603  # fixed command
    except OSError, subprocess.SubprocessError:
        return False
    return True


def boot_time() -> float:
    """
    When the machine booted.

    :returns: Seconds since the epoch
    """
    try:
        return float(psutil.boot_time())
    except OSError, RuntimeError:  # pragma: no cover - psutil always has it on Linux and macOS
        return 0.0
