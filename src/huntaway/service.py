"""
Install huntaway as a background service, without root.

Install huntaway as a background service: a systemd user unit on Linux, a launch agent on macOS. No root.
"""

import os
import shutil
import subprocess  # nosec B404  # fixed commands only, never a shell
import sys
from importlib import resources
from pathlib import Path

UNIT = """[Unit]
Description=huntaway, the sheepdog for your AI agents
Documentation=man:huntaway(1) https://github.com/jpsanders/huntaway
After=default.target

[Service]
Type=simple
ExecStart={exe} daemon
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1
MemoryMax=300M
CPUQuota=50%

[Install]
WantedBy=default.target
"""

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>io.github.jpsanders.huntaway</string>
    <key>ProgramArguments</key><array><string>{python}</string><string>-m</string><string>huntaway</string>
    <string>daemon</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ProcessType</key><string>Background</string>
    <key>StandardOutPath</key><string>{log}</string>
    <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""

LABEL = "io.github.jpsanders.huntaway"


def executable() -> str:
    """
    Return the command the service runs.

    :returns: The installed huntaway script, or this Python with ``-m huntaway``
    """
    found = shutil.which("huntaway")
    return found if found else f"{sys.executable} -m huntaway"


def unit_path() -> Path:
    """
    Where the systemd user unit goes.

    :returns: ``~/.config/systemd/user/huntaway.service``
    """
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user" / "huntaway.service"


def plist_path() -> Path:
    """
    Where the macOS launch agent goes.

    :returns: ``~/Library/LaunchAgents/io.github.jpsanders.huntaway.plist``
    """
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _run(args: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)  # nosec B603  # fixed commands
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return result.returncode, (result.stdout + result.stderr).strip()


def install_man() -> Path | None:
    """
    Copy the man page where ``man huntaway`` finds it.

    :returns: The installed path, or None when it could not be written
    """
    target = Path.home() / ".local" / "share" / "man" / "man1" / "huntaway.1"
    try:
        page = (resources.files("huntaway") / "data" / "huntaway.1").read_bytes()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(page)
    except OSError:
        return None
    return target


def install(start: bool = True) -> list[str]:
    """
    Install and start the service.

    :param start: Enable and start it now
    :returns: Lines describing what was done
    :raises RuntimeError: On a platform without a supported service manager
    """
    lines = []
    man = install_man()
    if man:
        lines.append(f"man page: {man}")
    if sys.platform == "darwin":
        path = plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        log = Path.home() / "Library" / "Logs" / "huntaway.log"
        path.write_text(PLIST.format(python=sys.executable, log=log), encoding="utf-8")
        lines.append(f"launch agent: {path}")
        if start:
            uid = os.getuid()
            _run(["launchctl", "bootout", f"gui/{uid}", str(path)])
            code, out = _run(["launchctl", "bootstrap", f"gui/{uid}", str(path)])
            lines.append("started" if code == 0 else f"launchctl said: {out}")
        return lines
    if not shutil.which("systemctl"):
        raise RuntimeError("no systemd here: run huntaway daemon from your session start instead")
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(UNIT.format(exe=executable()), encoding="utf-8")
    lines.append(f"unit: {path}")
    _run(["systemctl", "--user", "daemon-reload"])
    if start:
        code, out = _run(["systemctl", "--user", "enable", "--now", "huntaway.service"])
        lines.append("enabled and started" if code == 0 else f"systemctl said: {out}")
    return lines


def uninstall() -> list[str]:
    """
    Stop and remove the service. Settings, history and saved sessions stay.

    :returns: Lines describing what was done
    """
    if sys.platform == "darwin":
        path = plist_path()
        _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)])
        path.unlink(missing_ok=True)
        return [f"removed {path}"]
    _run(["systemctl", "--user", "disable", "--now", "huntaway.service"])
    path = unit_path()
    path.unlink(missing_ok=True)
    _run(["systemctl", "--user", "daemon-reload"])
    return [f"removed {path}", "settings, history and saved sessions are kept"]


def control(action: str) -> tuple[int, str]:
    """
    Start, stop, restart or query the service.

    :param action: start, stop, restart or status
    :returns: Exit code and output
    """
    if sys.platform == "darwin":
        target = f"gui/{os.getuid()}/{LABEL}"
        if action == "status":
            return _run(["launchctl", "print", target])
        if action == "stop":
            return _run(["launchctl", "bootout", target])
        if action == "start":
            return _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path())])
        return _run(["launchctl", "kickstart", "-k", target])
    return _run(["systemctl", "--user", action, "huntaway.service"])


def logs(lines: int = 50) -> str:
    """
    Return the service's recent log.

    :param lines: How many lines
    :returns: The log text
    """
    if sys.platform == "darwin":
        try:
            text = (Path.home() / "Library" / "Logs" / "huntaway.log").read_text(encoding="utf-8")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])
    return _run(["journalctl", "--user", "-u", "huntaway.service", "-n", str(lines), "--no-pager"])[1]
