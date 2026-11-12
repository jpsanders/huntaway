"""
Deep scans with rkhunter, chkrootkit, ClamAV and Lynis.

Deep scans with the standard tools: rkhunter, chkrootkit, ClamAV and Lynis.

They need root, so ``sudo huntaway scan --deep`` runs them and writes a JSON report to /var/lib/huntaway/scans,
where the user's daemon reads it. Output is parsed into findings, and rkhunter's routine false alarms (file
properties changed by a package update, the well known hidden files) are marked as such instead of shouting. ClamAV
covers the places downloads and droppers land plus files changed in the last week, not the whole disk, so a scan
takes minutes instead of a day.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess  # nosec B404  # fixed commands only, never a shell
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from huntaway import paths

Finding = dict[str, Any]

BENIGN_HIDDEN = (
    "/etc/.java",
    "/etc/.updated",
    "/etc/.resolv.conf.systemd-resolved.bak",
    "/etc/.gitignore",
    "/etc/.etckeeper",
    "/etc/.git",
    "/dev/.lxc",
    "/dev/.udev",
)
SKIP_DIRS = frozenset(
    {
        ".cache",
        "node_modules",
        ".npm",
        ".git",
        ".venv",
        "venv",
        ".pyenv",
        ".rustup",
        ".cargo",
        ".gradle",
        ".m2",
        "snap",
        ".local/share/Trash",
        ".mozilla",
        ".config/google-chrome",
        ".config/chromium",
    }
)


@dataclass(slots=True)
class ScanResult:
    """One scanner's run."""

    source: str
    status: str
    duration: float = 0.0
    findings: list[Finding] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """
        Return the result as stored in a report.

        :returns: A plain dict
        """
        return {
            "source": self.source,
            "status": self.status,
            "duration": round(self.duration, 1),
            "findings": self.findings,
            "summary": self.summary,
        }


def _fid(source: str, text: str) -> str:
    return f"{source}:{hashlib.sha256(text.encode()).hexdigest()[:16]}"


def parse_rkhunter(text: str, recent_update: bool = False) -> list[Finding]:
    """
    Read rkhunter's warnings, each with its indented detail lines.

    :param text: Output of ``rkhunter --check --rwo`` or the log file
    :param recent_update: A package update ran lately, so changed file properties are expected
    :returns: Findings
    """
    findings: list[Finding] = []
    current: list[str] = []

    def flush() -> None:
        if not current:
            return
        title = current[0]
        detail = "\n".join(line.strip() for line in current[1:] if line.strip())
        severity, hint = "high", ""
        lower = title.lower()
        if "file properties have changed" in lower:
            severity = "low" if recent_update else "medium"
            hint = (
                "After a package update this is expected: refresh the baseline with sudo huntaway scan --deep "
                "--refresh-baseline."
            )
        elif "hidden file found" in lower or "hidden directory found" in lower:
            path = title.split(":", 1)[-1].strip().split(":")[0]
            severity = "info" if path.startswith(BENIGN_HIDDEN) else "medium"
        elif "o/s name or version has changed" in lower or "users responsibility" in lower:
            severity = "info"
        elif "prerequisites" in lower:
            severity = "info"
        elif "possible rootkit" in lower or "rootkit" in lower:
            severity = "critical"
        findings.append(
            {
                "id": _fid("rkhunter", title + detail),
                "severity": severity,
                "title": title,
                "detail": (detail + ("\n" + hint if hint else "")).strip(),
            }
        )
        current.clear()

    for raw in text.splitlines():
        line = re.sub(r"^\[\d\d:\d\d:\d\d\]\s?", "", raw.rstrip())
        if line.startswith("Warning:"):
            flush()
            current.append(line[len("Warning:") :].strip())
        elif current and (line.startswith((" ", "\t")) and line.strip()):
            current.append(line)
        else:
            flush()
    flush()
    return findings


def parse_chkrootkit(text: str) -> list[Finding]:
    """
    Read chkrootkit's alarms.

    :param text: Output of ``chkrootkit -q``
    :returns: Findings
    """
    findings: list[Finding] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        upper = line.upper()
        if re.search(r"\bNOT (INFECTED|FOUND|TESTED|PROMISC)|NOTHING (FOUND|DETECTED)|NO SUSPICIOUS", upper):
            continue
        if re.search(r"\bINFECTED\b", upper):
            severity = "critical"
        elif "VULNERABLE" in upper or "PROMISC" in upper or "PACKET SNIFFER" in upper:
            severity = "high"
        elif "WARNING" in upper or "SUSPICIOUS" in upper or "POSSIBLE" in upper:
            severity = "medium"
        else:
            continue
        findings.append({"id": _fid("chkrootkit", line), "severity": severity, "title": line[:200], "detail": ""})
    return findings


def parse_clamav(text: str) -> list[Finding]:
    """
    Read ClamAV's detections.

    :param text: Output of clamscan or clamdscan with ``--infected``
    :returns: Findings
    """
    findings = []
    for line in text.splitlines():
        if not line.endswith(" FOUND"):
            continue
        path, _, sig = line[: -len(" FOUND")].rpartition(": ")
        findings.append(
            {
                "id": _fid("clamav", path + sig),
                "severity": "high",
                "title": f"{sig} in {os.path.basename(path)}",
                "detail": path,
            }
        )
    return findings


def parse_lynis(report: str) -> tuple[list[Finding], dict[str, Any]]:
    """
    Read Lynis's report file.

    :param report: The text of /var/log/lynis-report.dat
    :returns: Findings (warnings high, suggestions low) and a summary with the hardening index
    """
    findings: list[Finding] = []
    summary: dict[str, Any] = {}
    for line in report.splitlines():
        key, _, value = line.partition("=")
        if key == "hardening_index":
            try:
                summary["hardening_index"] = int(value)
            except ValueError:
                pass
        elif key in ("warning[]", "suggestion[]"):
            parts = value.split("|")
            test, text = parts[0], parts[1] if len(parts) > 1 else ""
            detail = " ".join(p for p in parts[2:] if p and p != "-")
            severity = "medium" if key == "warning[]" else "info"
            findings.append(
                {"id": _fid("lynis", test + text), "severity": severity, "title": f"{text} [{test}]", "detail": detail}
            )
    summary["warnings"] = sum(1 for f in findings if f["severity"] == "medium")
    summary["suggestions"] = sum(1 for f in findings if f["severity"] == "info")
    return findings, summary


def _run(args: list[str], timeout: float) -> tuple[int, str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)  # nosec B603  # fixed commands
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return 124, out
    except OSError as exc:
        return 127, str(exc)
    return result.returncode, result.stdout + result.stderr


def _recent_update(days: float = 3.0) -> bool:
    for log in ("/var/log/dpkg.log", "/var/log/pacman.log", "/var/log/dnf.rpm.log", "/var/log/zypp/history"):
        try:
            if time.time() - os.stat(log).st_mtime < days * 86400:
                return True
        except OSError:
            continue
    return False


def rkhunter(refresh_baseline: bool = False) -> ScanResult:
    """
    Run rkhunter.

    :param refresh_baseline: Run ``--propupd`` first, after updates you trust
    :returns: The result
    """
    tool = shutil.which("rkhunter")
    if not tool:
        return ScanResult("rkhunter", "missing")
    start = time.monotonic()
    if refresh_baseline:
        _run([tool, "--propupd", "--nocolors"], 600)
    code, out = _run([tool, "--check", "--sk", "--nocolors", "--rwo"], 3600)
    findings = parse_rkhunter(out, _recent_update())
    status = "failed" if code not in (0, 1) else "warnings" if findings else "ok"
    return ScanResult("rkhunter", status, time.monotonic() - start, findings, {"exit": code})


def chkrootkit() -> ScanResult:
    """
    Run chkrootkit.

    :returns: The result
    """
    tool = shutil.which("chkrootkit")
    if not tool:
        return ScanResult("chkrootkit", "missing")
    start = time.monotonic()
    code, out = _run([tool, "-q"], 1800)
    findings = parse_chkrootkit(out)
    status = "failed" if code not in (0, 1) else "warnings" if findings else "ok"
    return ScanResult("chkrootkit", status, time.monotonic() - start, findings, {"exit": code})


def _user_homes() -> list[Path]:
    owner = os.environ.get("SUDO_USER")
    if owner:
        try:
            import pwd

            return [Path(pwd.getpwnam(owner).pw_dir)]
        except KeyError, ImportError:
            pass
    return [Path.home()]


def clamav_targets(paths_wanted: list[str], days: float = 7.0, limit: int = 200_000) -> tuple[list[str], int]:
    """
    List what ClamAV should read: the given folders, or the risky places plus files changed lately.

    :param paths_wanted: Folders from the config; empty means the default set
    :param days: How recent a change counts
    :param limit: Most files to list
    :returns: Paths to scan and how many recent files were listed
    """
    if paths_wanted:
        return [os.path.expanduser(p) for p in paths_wanted if os.path.exists(os.path.expanduser(p))], 0
    targets: list[str] = []
    recent = 0
    cutoff = time.time() - days * 86400
    for home in _user_homes():
        for sub in ("Downloads", ".local/bin", ".config/autostart"):
            if (home / sub).is_dir():
                targets.append(str(home / sub))
        stack = [home]
        while stack and recent < limit:
            folder = stack.pop()
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for entry in entries:
                rel = os.path.relpath(entry.path, home)
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in SKIP_DIRS and rel not in SKIP_DIRS and not rel.startswith("Downloads"):
                        stack.append(Path(entry.path))
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if entry.is_file(follow_symlinks=False) and st.st_mtime >= cutoff and st.st_size < 200 << 20:
                    targets.append(entry.path)
                    recent += 1
    for tmp in ("/tmp", "/var/tmp", "/dev/shm"):  # nosec B108  # scanned, never written
        if os.path.isdir(tmp):
            targets.append(tmp)
    return targets, recent


def clamav(paths_wanted: list[str] | None = None) -> ScanResult:
    """
    Run ClamAV over the targets, through clamd when it runs (much faster), else clamscan.

    :param paths_wanted: Folders to scan instead of the default set
    :returns: The result
    """
    daemon = shutil.which("clamdscan")
    scanner = shutil.which("clamscan")
    if not daemon and not scanner:
        return ScanResult("clamav", "missing")
    targets, recent = clamav_targets(paths_wanted or [])
    if not targets:
        return ScanResult("clamav", "ok", 0.0, [], {"files": 0})
    start = time.monotonic()
    list_file = paths.state_dir() / "clamav-targets.txt"
    list_file.parent.mkdir(parents=True, exist_ok=True)
    list_file.write_text("\n".join(targets) + "\n", encoding="utf-8")
    clamd_up = daemon and os.path.exists("/var/run/clamav/clamd.ctl")
    if clamd_up and daemon:
        args = [daemon, "--multiscan", "--fdpass", "--infected", "--no-summary", f"--file-list={list_file}"]
    else:
        args = [scanner or "clamscan", "-r", "--infected", "--no-summary", f"--file-list={list_file}"]
    code, out = _run(args, 6 * 3600)
    list_file.unlink(missing_ok=True)
    findings = parse_clamav(out)
    status = "failed" if code not in (0, 1) else "warnings" if findings else "ok"
    return ScanResult(
        "clamav",
        status,
        time.monotonic() - start,
        findings,
        {"exit": code, "targets": len(targets), "recent_files": recent, "engine": "clamd" if clamd_up else "clamscan"},
    )


def lynis() -> ScanResult:
    """
    Run a Lynis system audit.

    :returns: The result, with the hardening index in the summary
    """
    tool = shutil.which("lynis")
    if not tool:
        return ScanResult("lynis", "missing")
    start = time.monotonic()
    code, _ = _run([tool, "audit", "system", "--quick", "--no-colors", "--quiet"], 3600)
    try:
        report = Path("/var/log/lynis-report.dat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ScanResult("lynis", "failed", time.monotonic() - start, [], {"exit": code})
    findings, summary = parse_lynis(report)
    summary["exit"] = code
    status = "warnings" if summary.get("warnings") else "ok"
    return ScanResult("lynis", status, time.monotonic() - start, findings, summary)


SCANNERS: dict[str, Callable[..., ScanResult]] = {
    "rkhunter": rkhunter,
    "chkrootkit": chkrootkit,
    "clamav": clamav,
    "lynis": lynis,
}

PACKAGES = {"rkhunter": "rkhunter", "chkrootkit": "chkrootkit", "clamav": "clamav", "lynis": "lynis"}


def available() -> dict[str, bool]:
    """
    Which scanners are installed.

    :returns: Scanner name to installed
    """
    tools = {
        "rkhunter": ["rkhunter"],
        "chkrootkit": ["chkrootkit"],
        "clamav": ["clamscan", "clamdscan"],
        "lynis": ["lynis"],
    }
    return {name: any(shutil.which(t) for t in names) for name, names in tools.items()}


def install_command(missing: list[str]) -> str:
    """
    Return the package manager command that installs the missing scanners.

    :param missing: Scanner names
    :returns: A command for the detected package manager, or an empty string
    """
    if not missing:
        return ""
    pkgs = " ".join(PACKAGES[m] for m in missing)
    if sys.platform == "darwin":
        return f"brew install {' '.join(m for m in missing if m != 'chkrootkit')}".strip()
    for manager, cmd in (
        ("apt-get", "sudo apt-get install -y"),
        ("dnf", "sudo dnf install -y"),
        ("pacman", "sudo pacman -S --needed"),
        ("zypper", "sudo zypper install -y"),
    ):
        if shutil.which(manager):
            if manager in ("dnf",) and "clamav" in missing:
                pkgs = pkgs.replace("clamav", "clamav clamav-update")
            return f"{cmd} {pkgs}"
    return ""


def deep_scan(
    only: list[str] | None = None, refresh_baseline: bool = False, clamav_paths: list[str] | None = None
) -> dict[str, Any]:
    """
    Run the deep scanners and write a report the daemon can read.

    :param only: Scanner names to run, all installed ones when None
    :param refresh_baseline: Refresh rkhunter's file properties first
    :param clamav_paths: Folders for ClamAV instead of the default set
    :returns: The report
    """
    results = []
    for name, fn in SCANNERS.items():
        if only and name not in only:
            continue
        if name == "rkhunter":
            result = fn(refresh_baseline=refresh_baseline)
        elif name == "clamav":
            result = fn(clamav_paths)
        else:
            result = fn()
        results.append(result.to_dict())
    report = {"ts": time.time(), "host": os.uname().nodename, "root": os.geteuid() == 0, "results": results}
    out_dir = paths.shared_scan_dir() if os.geteuid() == 0 else paths.state_dir() / "scans"
    out_dir.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        os.chmod(out_dir, 0o755)  # nosec B103  # reports must be readable by the user's daemon
        os.chmod(out_dir.parent, 0o755)  # nosec B103
    target = out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    target.write_text(json.dumps(report, indent=1), encoding="utf-8")
    os.chmod(target, 0o644)
    reports = sorted(out_dir.glob("*.json"))
    for old in reports[:-30]:
        old.unlink(missing_ok=True)
    report["path"] = str(target)
    return report


def latest_reports() -> list[dict[str, Any]]:
    """
    Deep scan reports, newest first, from the shared folder and the user's own.

    :returns: Parsed reports with their path
    """
    files: list[Path] = []
    for folder in (paths.shared_scan_dir(), paths.state_dir() / "scans"):
        try:
            files += list(folder.glob("*.json"))
        except OSError:
            continue
    out = []
    for path in sorted(files, key=lambda p: p.name, reverse=True)[:30]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            continue
        data["path"] = str(path)
        out.append(data)
    return out


TIMER_SERVICE = """[Unit]
Description=huntaway deep security scan (rkhunter, chkrootkit, ClamAV, Lynis)
Documentation=man:huntaway(1)

[Service]
Type=oneshot
Nice=19
IOSchedulingClass=idle
CPUQuota=50%
ExecStart={exe} scan --deep
"""

TIMER_UNIT = """[Unit]
Description=Weekly huntaway deep security scan

[Timer]
OnCalendar={calendar}
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
"""


def install_timer(exe: str, calendar: str = "Sun 03:00") -> list[str]:
    """
    Install a root systemd timer for weekly deep scans. Needs root.

    :param exe: The huntaway executable
    :param calendar: A systemd OnCalendar expression
    :returns: The files written
    :raises PermissionError: When not run as root
    """
    if os.geteuid() != 0:
        raise PermissionError("installing the scan timer needs root: sudo huntaway scan schedule")
    unit_dir = Path("/etc/systemd/system")
    service = unit_dir / "huntaway-scan.service"
    timer = unit_dir / "huntaway-scan.timer"
    service.write_text(TIMER_SERVICE.format(exe=exe), encoding="utf-8")
    timer.write_text(TIMER_UNIT.format(calendar=calendar), encoding="utf-8")
    _run(["systemctl", "daemon-reload"], 60)
    _run(["systemctl", "enable", "--now", "huntaway-scan.timer"], 60)
    return [str(service), str(timer)]
