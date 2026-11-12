"""The rules engine: protection, memory emergencies, CPU hogs, caps and user rules. Pure decisions, no signals."""

import os

import pytest

from huntaway import config
from huntaway.agents.harnesses import Agent
from huntaway.guard import Guard
from huntaway.sampler import Proc, Snapshot

GB = 1 << 30
ME = os.getuid()


class NoCalls:
    def __init__(self, names=()):
        self._names = frozenset(names)

    def names(self):
        return self._names


def proc(pid, name, cpu=0.0, rss=100 << 20, uid=ME, ppid=1000):
    return Proc(
        pid=pid,
        ppid=ppid,
        name=name,
        uid=uid,
        started=1000.0 + pid,
        cpu=cpu,
        rss=rss,
        status="running",
        cpu_seconds=0.0,
    )


def snap(procs, ts=10_000.0, available=50.0, swap_free=100.0, cpu=10.0, psi_full=0.0):
    total = 32 * GB
    return Snapshot(
        ts=ts,
        cpu=cpu,
        cores=8,
        mem_total=total,
        mem_available=int(total * available / 100),
        swap_total=8 * GB,
        swap_used=int(8 * GB * (1 - swap_free / 100)),
        load=(1, 1, 1),
        disk_read=0,
        disk_write=0,
        net_rx=0,
        net_tx=0,
        temp=None,
        psi={"memory_full": psi_full},
        disk_percent=10,
        uptime=1,
        procs=procs,
    )


def guard(cfg, calls=()):
    return Guard(cfg, calls=NoCalls(calls), cmdline=lambda p: f"/usr/bin/{p.name} --flag")


def test_builtin_protection_and_truncated_names(cfg):
    g = guard(cfg)
    assert g.protection(proc(10, "gnome-shell"), set(), set()) == "protected name"
    assert g.protection(proc(11, "xdg-desktop-por"), set(), set()) == "protected name"
    assert g.protection(proc(12, "tmux: server"), set(), set()) == "protected name"
    assert g.protection(proc(13, "python3"), set(), set()) is None


def test_system_family_other_users_and_leases(cfg):
    g = guard(cfg)
    assert g.protection(proc(1, "systemd"), set(), set()) == "system process"
    assert g.protection(proc(50, "kworker", ppid=2), set(), set()) == "system process"
    assert g.protection(proc(51, "anything"), {51}, set()) == "system process"
    if ME != 0:
        assert g.protection(proc(52, "anything", uid=ME + 1), set(), set()) == "belongs to another user"
    assert g.protection(proc(53, "cargo"), set(), {53}) == "started with huntaway run"


def test_user_names_patterns_and_calls(cfg):
    cfg = config.set_value(cfg, "protect.names", ["transcriber"])
    cfg = config.set_value(cfg, "protect.patterns", [r"--keep-me"])
    g = guard(cfg, calls=["chrome"])
    assert g.protection(proc(20, "transcriber"), set(), set()) == "protected name"
    assert g.protection(proc(21, "chrome"), set(), set()) == "in a call"
    g.cmdline = lambda p: "node server.js --keep-me"
    g._cmd_cache.clear()
    assert g.protection(proc(22, "node"), set(), set()) == "protected pattern"


def test_calls_can_be_switched_off(cfg):
    g = guard(config.set_value(cfg, "protect.calls", False), calls=["chrome"])
    assert g.protection(proc(21, "chrome"), set(), set()) is None


def test_memory_emergency_waits_for_sustain_then_picks_the_biggest(cfg):
    g = guard(cfg)
    procs = [proc(100, "chrome", rss=3 * GB), proc(101, "node", rss=6 * GB), proc(102, "gnome-shell", rss=9 * GB)]
    assert g.update(snap(procs, ts=100, available=3, swap_free=2), []) == []
    assert g.level == "critical"
    out = g.update(snap(procs, ts=105, available=3, swap_free=2), [])
    assert len(out) == 1 and out[0].pid == 101 and out[0].action == "terminate" and out[0].kind == "memory"
    assert g.update(snap(procs, ts=106, available=3, swap_free=2), []) == []


def test_memory_emergency_closes_idle_agents_first(cfg):
    cfg = config.set_value(cfg, "agents.enabled", True)
    g = guard(cfg)
    agent = Agent(
        harness="claude",
        label="Claude Code",
        pid=200,
        pids=[200],
        started=1200,
        interactive=True,
        status="idle",
        last_active=0,
        rss=500 << 20,
    )
    procs = [proc(101, "node", rss=6 * GB), proc(200, "claude", rss=500 << 20)]
    g.update(snap(procs, ts=100, available=3, swap_free=2), [agent])
    out = g.update(snap(procs, ts=110, available=3, swap_free=2), [agent])
    assert out[0].action == "close" and out[0].agent is agent


def test_psi_full_triggers_without_low_free_memory(cfg):
    g = guard(cfg)
    procs = [proc(101, "node", rss=6 * GB)]
    g.update(snap(procs, ts=100, available=30, psi_full=60), [])
    assert g.update(snap(procs, ts=110, available=30, psi_full=60), [])[0].pid == 101


def test_tight_memory_is_reported_without_acting(cfg):
    g = guard(cfg)
    assert g.update(snap([proc(101, "node")], available=9), []) == []
    assert g.level == "tight"


def test_cpu_hog_is_throttled_once_when_the_machine_is_busy(cfg):
    g = guard(cfg)
    hog = [proc(300, "python3", cpu=180)]
    assert g.update(snap(hog, ts=0, cpu=95), []) == []
    out = g.update(snap(hog, ts=31, cpu=95), [])
    assert out[0].action == "throttle" and out[0].pid == 300
    assert g.update(snap(hog, ts=62, cpu=95), []) == []


def test_cpu_hog_on_an_idle_machine_is_left_alone(cfg):
    g = guard(cfg)
    hog = [proc(300, "python3", cpu=180)]
    g.update(snap(hog, ts=0, cpu=20), [])
    assert g.update(snap(hog, ts=100, cpu=20), []) == []


def test_process_memory_cap(cfg):
    g = guard(config.set_value(cfg, "memory.process_max_percent", 50))
    big = [proc(400, "leaky", rss=20 * GB)]
    g.update(snap(big, ts=0), [])
    out = g.update(snap(big, ts=10), [])
    assert out[0].kind == "process_memory" and out[0].action == "terminate"


def test_rules_match_name_or_command_with_thresholds(cfg):
    cfg.rules = config.parse_rules([{"name": "dev servers", "match": "vite", "memory_mb": 1024, "sustain": 5}])
    g = guard(cfg)
    g.cmdline = lambda p: "node /x/node_modules/.bin/vite dev"
    small = [proc(500, "node", rss=512 << 20)]
    big = [proc(500, "node", rss=2 * GB)]
    assert g.update(snap(small, ts=0), []) == []
    g.update(snap(big, ts=1), [])
    out = g.update(snap(big, ts=7), [])
    assert out[0].kind == "rule" and "dev servers" in out[0].reason


def test_rule_without_thresholds_acts_on_sight(cfg):
    cfg.rules = config.parse_rules([{"match": r"\.exe$", "action": "kill", "sustain": 0}])
    g = guard(cfg)
    out = g.update(snap([proc(600, "setup.exe")], ts=0), [])
    assert out[0].action == "kill"


def test_spared_processes_are_left_alone(cfg):
    g = guard(cfg)
    hog = [proc(300, "python3", cpu=180)]
    g.update(snap(hog, ts=0, cpu=95), [])
    decision = g.update(snap(hog, ts=31, cpu=95), [])[0]
    g.spare(decision, seconds=1e12)
    assert g.update(snap(hog, ts=70, cpu=95), []) == []


@pytest.mark.parametrize("disabled", ["memory.enabled", "cpu.enabled"])
def test_sections_can_be_switched_off(cfg, disabled):
    g = guard(config.set_value(cfg, disabled, False))
    procs = [proc(101, "node", cpu=300, rss=6 * GB)]
    g.update(snap(procs, ts=0, available=1, swap_free=0, cpu=99), [])
    out = g.update(snap(procs, ts=100, available=1, swap_free=0, cpu=99), [])
    kinds = {d.kind for d in out}
    assert ("memory" not in kinds) if disabled == "memory.enabled" else ("cpu" not in kinds)
