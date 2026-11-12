"""Notifications, run limits, paths, the demo and the man page."""

import json
import random
import subprocess
import sys
import time
from pathlib import Path

from huntaway import config, demo, notify, paths, runwrap
from huntaway.core import call

ROOT = Path(__file__).resolve().parent.parent


def test_webhook_shapes():
    ntfy = notify.webhook_request("https://ntfy.sh/t", "T", "B", "high", {})
    assert ntfy.data == b"B" and ntfy.headers["Priority"] == "high"
    slack = notify.webhook_request("https://hooks.slack.com/x", "T", "B", "info", {})
    assert json.loads(slack.data) == {"text": "*T*\nB"}
    discord = notify.webhook_request("https://discord.com/api/webhooks/x", "T", "B", "info", {})
    assert json.loads(discord.data)["content"].startswith("**T**")
    generic = notify.webhook_request("https://example.org/h", "T", "B", "low", {"pid": 3})
    assert json.loads(generic.data) == {"title": "T", "body": "B", "severity": "low", "pid": 3}


def test_run_limits(monkeypatch):
    assert runwrap.parse_size("2G") == 2 << 30 and runwrap.parse_size("512M") == 512 << 20
    assert runwrap.parse_size("50%") > 0 and runwrap.parse_size("1024") == 1024
    monkeypatch.setattr(runwrap.shutil, "which", lambda name: "/usr/bin/systemd-run")
    monkeypatch.setattr(runwrap.sys, "platform", "linux")
    wrapped = runwrap.build(["make"], "200%", "1G")
    assert wrapped[:4] == ["systemd-run", "--user", "--scope", "--quiet"] and "CPUQuota=200%" in wrapped
    assert wrapped[-2:] == ["--", "make"]
    assert runwrap.build(["make"], None, None) == ["make"]


def test_paths_follow_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HUNTAWAY_DATA_DIR", str(tmp_path / "d"))
    assert paths.db_file() == tmp_path / "d" / "huntaway.db"


def test_demo_core_serves_made_up_data(tmp_path, cfg):
    from huntaway.store import Store

    store = Store(tmp_path / "demo.db")
    demo.seed(store, tmp_path / "arch", time.time(), random.Random(1))
    core = demo.DemoCore(cfg, store)
    core.tick()
    status = call(core, "status")
    assert status["system"]["cores"] == 16 and status["stats"]["closed"] == 23
    agents = call(core, "agents_list")
    stale = [a for a in agents if a["stale"]]
    assert len(agents) == 7 and stale
    result = call(core, "agents_reap", {"dry_run": False})
    assert len(result["results"]) == len(stale)
    assert len(call(core, "agents_list")) == 7 - len(stale)
    assert call(core, "analytics", {"range": "7d"})["apps"]
    assert call(core, "sessions", {"query": "idempotency"})
    assert call(core, "security")["hardening_index"] == 74


def test_man_page_matches_the_manual():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_man.py"), "--check"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_skill_has_front_matter():
    text = (ROOT / "src" / "huntaway" / "data" / "SKILL.md").read_text()
    assert text.startswith("---\nname: huntaway\ndescription: ")


def test_example_config_renders_every_option():
    text = config.render(config.Config(values=config.defaults()))
    for option in config.SCHEMA:
        assert f"{option.key.split('.', 1)[1]} = " in text
