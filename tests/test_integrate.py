"""Integration with agent harnesses, against files in a temporary home."""

import json
import tomllib

from huntaway import integrate


def test_every_harness_gets_the_server_and_skill(isolated, monkeypatch):
    monkeypatch.setattr(integrate.shutil, "which", lambda name: None)
    (isolated / ".claude").mkdir()
    (isolated / ".claude" / "settings.json").write_text("{}")
    (isolated / ".codex").mkdir()
    (isolated / ".codex" / "config.toml").write_text('model = "gpt-5"\n')
    (isolated / ".config" / "opencode").mkdir(parents=True)
    (isolated / ".config" / "opencode" / "opencode.json").write_text('{"theme": "dark"}')
    (isolated / ".gemini").mkdir()
    (isolated / ".cursor").mkdir()
    steps = integrate.run([])
    assert all(s.done for s in steps), [s.line() for s in steps]
    claude = json.loads((isolated / ".claude.json").read_text())
    assert claude["mcpServers"]["huntaway"]["args"][-1] == "mcp"
    assert (isolated / ".claude" / "skills" / "huntaway" / "SKILL.md").exists()
    codex = tomllib.loads((isolated / ".codex" / "config.toml").read_text())
    assert codex["model"] == "gpt-5" and codex["mcp_servers"]["huntaway"]["args"][-1] == "mcp"
    assert (isolated / ".codex" / "config.toml.huntaway.bak").exists()
    opencode = json.loads((isolated / ".config" / "opencode" / "opencode.json").read_text())
    assert opencode["theme"] == "dark" and opencode["mcp"]["huntaway"]["type"] == "local"
    assert "huntaway" in json.loads((isolated / ".gemini" / "settings.json").read_text())["mcpServers"]
    assert "huntaway" in json.loads((isolated / ".cursor" / "mcp.json").read_text())["mcpServers"]
    again = integrate.run(["claude", "codex"])
    assert all("already there" in s.note for s in again if s.what == "mcp")


def test_dry_run_changes_nothing(isolated, monkeypatch):
    monkeypatch.setattr(integrate.shutil, "which", lambda name: None)
    (isolated / ".gemini").mkdir()
    steps = integrate.run(["gemini"], dry=True)
    assert steps and not steps[0].done and not (isolated / ".gemini" / "settings.json").exists()


def test_files_with_comments_are_left_alone(isolated, monkeypatch):
    monkeypatch.setattr(integrate.shutil, "which", lambda name: None)
    (isolated / ".cursor").mkdir()
    (isolated / ".cursor" / "mcp.json").write_text("// comment\n{}")
    step = integrate.run(["cursor"])[0]
    assert not step.done and "by hand" in step.note


def test_unknown_harness():
    assert "unknown harness" in integrate.run(["vscode"])[0].target


def test_claude_config_dirs_include_extra_accounts(isolated):
    for name in (".claude", ".claude-work"):
        (isolated / name / "projects").mkdir(parents=True)
    (isolated / ".claude-cache").mkdir()
    assert [p.name for p in integrate.claude_dirs(isolated)] == [".claude", ".claude-work"]
