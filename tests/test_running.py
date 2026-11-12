"""The parts that act: process actions, the service loop, the unit installer, deep scans and more commands."""

import json
import os
import subprocess
import sys
import time

import psutil
import pytest

from huntaway import actions, cli, config, service
from huntaway.core import ApiError, Core, call
from huntaway.guard import Decision
from huntaway.sampler import Proc, Snapshot
from huntaway.security import scanners

GB = 1 << 30


@pytest.fixture
def child():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield proc, psutil.Process(proc.pid).create_time()
    if proc.poll() is None:
        proc.kill()
        proc.wait()


def test_throttle_terminate_and_identity(child):
    proc, started = child
    assert actions.throttle(proc.pid, started) == "throttled"
    assert actions.niceness(proc.pid) == 19
    assert actions.terminate(proc.pid, started + 50, 1) == "gone"
    assert actions.alive(proc.pid, started)
    assert actions.terminate(proc.pid, started, 2) == "terminated"
    assert actions.wait_gone(proc.pid, started, 2)
    assert actions.kill(proc.pid, started) == "gone"


def test_kill(child):
    proc, started = child
    assert actions.kill(proc.pid, started) == "killed"


class FakeSampler:
    cores = 4
    last_cost = 0.002

    def __init__(self, snaps):
        self.snaps = list(snaps)

    def sample(self):
        return self.snaps.pop(0) if len(self.snaps) > 1 else self.snaps[0]


def snapshot(procs, available=50.0, swap_free=100.0, cpu=10.0, ts=None):
    total = 16 * GB
    return Snapshot(
        ts=ts or time.time(),
        cpu=cpu,
        cores=4,
        mem_total=total,
        mem_available=int(total * available / 100),
        swap_total=4 * GB,
        swap_used=int(4 * GB * (1 - swap_free / 100)),
        load=(1, 1, 1),
        disk_read=1,
        disk_write=1,
        net_rx=1,
        net_tx=1,
        temp=50.0,
        psi={"memory": 1.0},
        disk_percent=40,
        uptime=9,
        procs=procs,
    )


def test_tick_records_samples_apps_and_observed_decisions(cfg, store, child):
    proc, started = child
    cfg = config.set_value(cfg, "general.mode", "observe")
    core = Core(cfg, store)
    hog = Proc(proc.pid, 1, "python3", 0, started, 250.0, 300 << 20, "running", 10.0)
    core.sampler = FakeSampler([snapshot([hog], cpu=95)])
    core.guard.leases = set()
    core.guard.uid = 0
    core.tick()
    core.guard.hog_since = {k: v - 100 for k, v in core.guard.hog_since.items()}
    core.tick()
    events = store.events(kind="throttle")
    assert events and events[0]["title"].startswith("Would throttle python3") and actions.niceness(proc.pid) != 19
    assert store.query("select count(*) as n from samples")[0]["n"] >= 1
    status = call(core, "status")
    assert status["mode"] == "observe" and status["system"]["cores"] == 4
    assert call(core, "live_samples")


def test_execute_throttles_and_records_in_enforce_mode(cfg, store, child):
    proc, started = child
    core = Core(cfg, store)
    core._execute(Decision("cpu", "throttle", proc.pid, started, "python3", "test", "low"), observe=False)
    assert actions.niceness(proc.pid) == 19
    assert store.events(kind="throttle")[0]["title"].startswith("Throttled python3")


def test_api_errors_and_config_changes(cfg, store, tmp_path):
    core = Core(cfg, store)
    with pytest.raises(ApiError, match="unknown method"):
        call(core, "nope")
    with pytest.raises(ApiError, match="does not take"):
        call(core, "events", {"colour": 1})
    with pytest.raises(ApiError):
        call(core, "session_read", {"id": "missing"})
    result = call(
        core, "config_set", {"values": {"cpu.action": "notify"}, "rules": [{"match": "vite", "memory_mb": 10}]}
    )
    assert result["settings"]["cpu"]["action"] == "notify" and result["rules"][0]["match"] == "vite"
    assert config.load(cfg.path)["cpu.action"] == "notify"
    with pytest.raises(ApiError):
        call(core, "config_set", {"rules": [{"match": "("}]})
    assert call(core, "finding_ignore", {"id": "x:1"})["ignore"] == ["x:1"]
    assert call(core, "config_get")["sections"]["memory"]


def test_process_action_refuses_protected_and_missing(cfg, store):
    core = Core(cfg, store)
    core.tick()
    with pytest.raises(ApiError, match="no process"):
        call(core, "process_action", {"pid": 10**7, "action": "kill"})
    with pytest.raises(ApiError, match="protected"):
        call(core, "process_action", {"pid": os.getpid(), "action": "kill"})


def test_reload_picks_up_a_changed_file(cfg, store):
    config.save(cfg)
    core = Core(config.load(cfg.path), store)
    time.sleep(0.01)
    config.save(config.set_value(cfg, "cpu.sustain", 99), cfg.path)
    os.utime(cfg.path, (time.time() + 5, time.time() + 5))
    core._maybe_reload()
    assert core.cfg["cpu.sustain"] == 99
    cfg.path.write_text("[general]\ninterval = 0\n")
    os.utime(cfg.path, (time.time() + 9, time.time() + 9))
    core._maybe_reload()
    assert core.cfg["cpu.sustain"] == 99 and store.events(kind="config")[0]["severity"] == "medium"


def test_service_unit_is_written(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "_run", lambda args: calls.append(args) or (0, ""))
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(service.sys, "platform", "linux")
    lines = service.install()
    unit = service.unit_path().read_text()
    assert "ExecStart=/usr/bin/huntaway daemon" in unit and "MemoryMax=300M" in unit
    assert ["systemctl", "--user", "enable", "--now", "huntaway.service"] in calls
    assert any("man page" in line for line in lines)
    assert (isolated / ".local" / "share" / "man" / "man1" / "huntaway.1").exists()
    service.uninstall()
    assert not service.unit_path().exists()


def test_deep_scan_runs_what_is_installed(isolated, monkeypatch):
    monkeypatch.setattr(scanners.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "chkrootkit" else None)
    monkeypatch.setattr(scanners, "_run", lambda args, timeout: (0, "INFECTED: Possible Malicious Linux.Xor.DDoS"))
    monkeypatch.setattr(scanners.os, "geteuid", lambda: 1000)
    report = scanners.deep_scan()
    by = {r["source"]: r for r in report["results"]}
    assert by["rkhunter"]["status"] == "missing" and by["chkrootkit"]["findings"][0]["severity"] == "critical"
    assert scanners.latest_reports()[0]["results"]
    assert scanners.available()["chkrootkit"]


def test_clamav_targets_recent_files(isolated):
    (isolated / "Downloads").mkdir()
    (isolated / "project").mkdir()
    (isolated / "project" / "new.py").write_text("x")
    (isolated / ".cache").mkdir()
    (isolated / ".cache" / "skip.bin").write_text("x")
    targets, recent = scanners.clamav_targets([])
    assert str(isolated / "Downloads") in targets and str(isolated / "project" / "new.py") in targets
    assert not any(".cache" in t for t in targets) and recent == 1


def run(capsys, *argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(list(argv))
    out = capsys.readouterr()
    return exc.value.code, out.out


def test_more_commands(capsys, monkeypatch):
    monkeypatch.setattr("huntaway.client.Client.remote", lambda self: False)
    config.save(config.set_value(config.load(), "agents.enabled", False))
    assert run(capsys, "top", "--limit", "3")[0] == 0
    code, out = run(capsys, "agents", "--json")
    assert code == 0 and json.loads(out) == []
    assert "No interactive agent sessions" in run(capsys, "agents")[1]
    assert "No saved sessions yet" in run(capsys, "sessions")[1]
    assert run(capsys, "agents", "save")[0] == 2
    assert "rkhunter" in run(capsys, "scan", "setup")[1]
    assert run(capsys, "doctor")[0] == 0
    assert len(run(capsys, "token")[1].strip()) > 20
    assert run(capsys, "config", "--json")[0] == 0
    assert "huntaway" in run(capsys, "man")[1]
