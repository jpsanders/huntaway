"""The command line, run in process."""

import json
import sys

import pytest

from huntaway import cli, config, paths


def run(capsys, *argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(list(argv))
    out = capsys.readouterr()
    return exc.value.code, out.out, out.err


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0 and "huntaway" in capsys.readouterr().out


def test_config_set_get_validate_and_reset(capsys):
    code, out, _ = run(capsys, "config", "set", "memory.sustain", "9")
    assert code == 0 and "memory.sustain = 9" in out
    assert config.load()["memory.sustain"] == 9
    code, out, _ = run(capsys, "config", "get", "memory.sustain", "--json")
    assert code == 0 and json.loads(out) == 9
    assert run(capsys, "config", "set", "protect.names", '["a", "b"]')[0] == 0
    assert config.load()["protect.names"] == ["a", "b"]
    code, _, err = run(capsys, "config", "set", "memory.sustain", "-3")
    assert code == 2 and "at least" in err
    assert run(capsys, "config", "validate")[0] == 0
    assert run(capsys, "config", "path")[1].strip() == str(paths.config_file())
    assert run(capsys, "config", "reset")[0] == 0 and config.load()["memory.sustain"] == 4


def test_status_json_works_without_the_service(capsys, monkeypatch):
    monkeypatch.setattr("huntaway.client.Client.remote", lambda self: False)
    config.save(config.set_value(config.load(), "agents.enabled", False))
    code, out, _ = run(capsys, "status", "--json")
    data = json.loads(out)
    assert code == 0 and data["service_running"] is False and "system" in data


def test_events_and_why(capsys, monkeypatch):
    monkeypatch.setattr("huntaway.client.Client.remote", lambda self: False)
    from huntaway.store import Store

    Store(paths.db_file()).add_event("terminate", "Stopped node (pid 42): memory short", "high", 42, "node")
    code, out, _ = run(capsys, "why", "node")
    assert code == 0 and "Stopped node" in out
    assert run(capsys, "why", "nothing-like-this")[0] == 1
    code, out, _ = run(capsys, "events", "--json")
    assert json.loads(out)[0]["pid"] == 42


def test_judge_needs_a_model_then_switches(capsys):
    assert run(capsys, "judge", "veto")[0] == 2
    code, out, _ = run(capsys, "judge", "veto", "--model", "qwen3:4b")
    assert code == 0 and config.load()["judge.mode"] == "veto"
    assert run(capsys, "judge", "off")[0] == 0 and config.load()["judge.mode"] == "off"


def test_run_returns_the_commands_exit_code_and_cleans_up(capsys, monkeypatch):
    monkeypatch.setattr("huntaway.runwrap.shutil.which", lambda name: None)
    assert run(capsys, "run", "--", sys.executable, "-c", "raise SystemExit(3)")[0] == 3
    assert not list((paths.runtime_dir() / "leases").glob("*.json"))
    assert run(capsys, "run", "--", "no-such-command-anywhere")[0] == 127


def test_guide_and_integrate_without_harnesses(capsys):
    code, out, _ = run(capsys, "guide")
    assert code == 0 and "huntaway" in out
    code, out, _ = run(capsys, "integrate", "--dry-run")
    assert "No supported harness" in out
    code, out, _ = run(capsys, "integrate", "--agents-md")
    assert "huntaway run" in out


def test_bare_command_shows_status(capsys, monkeypatch):
    monkeypatch.setattr("huntaway.client.Client.remote", lambda self: False)
    config.save(config.set_value(config.load(), "agents.enabled", False))
    code, out, _ = run(capsys)
    assert code == 0 and "huntaway" in out and "CPU" in out
