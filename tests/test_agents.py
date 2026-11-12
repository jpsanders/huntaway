"""Agent sessions: detection, activity, titles, saving, restoring, rendering, staleness and closing."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from huntaway import config
from huntaway.agents import archive, reaper, render
from huntaway.agents.harnesses import Agent, Finder, claude_slug, first_prompt_claude, transcript_tail
from huntaway.sampler import Proc

ME = os.getuid()


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def write_claude(home: Path, pid: int, sid: str, cwd: str, status="idle", updated=None, cfg_dir=".claude"):
    cfg = home / cfg_dir
    (cfg / "sessions").mkdir(parents=True, exist_ok=True)
    (cfg / "sessions" / f"{pid}.json").write_text(
        json.dumps(
            {
                "pid": pid,
                "sessionId": sid,
                "cwd": cwd,
                "status": status,
                "kind": "interactive",
                "updatedAt": int((updated or time.time()) * 1000),
                "name": "auto-1",
                "nameSource": "derived",
            }
        )
    )
    project = cfg / "projects" / claude_slug(cwd)
    project.mkdir(parents=True, exist_ok=True)
    t0 = time.time() - 7200
    lines = [
        {"type": "user", "timestamp": iso(t0), "message": {"role": "user", "content": "Fix the flaky test please"}},
        {
            "type": "assistant",
            "timestamp": iso(t0 + 60),
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Found it: a race."},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}},
                ],
            },
        },
        {
            "type": "user",
            "timestamp": iso(t0 + 70),
            "message": {"role": "user", "content": [{"type": "tool_result", "content": "3 passed"}]},
        },
        {"type": "ai-title", "aiTitle": "Fix the flaky checkout test", "sessionId": sid},
        {"type": "mode", "mode": "default", "sessionId": sid},
    ]
    path = project / f"{sid}.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    (project / sid / "tool-results").mkdir(parents=True)
    (project / sid / "tool-results" / "r1.txt").write_text("big output")
    return path, t0 + 70


class FakeCtx:
    def __init__(self, table):
        self.table = table

    def get(self, proc):
        return self.table[proc.pid]

    def env(self, proc):
        return self.table[proc.pid].get("env", {})

    def prune(self, live):
        pass


def p(pid, name, ppid=1, rss=300 << 20, cpu=0.0, started=None):
    return Proc(
        pid=pid,
        ppid=ppid,
        name=name,
        uid=ME,
        started=started or time.time() - 3600,
        cpu=cpu,
        rss=rss,
        status="sleeping",
        cpu_seconds=0,
    )


def test_slug_matches_claude_codes_folder_names():
    assert claude_slug("/home/a.b/my_proj") == "-home-a-b-my-proj"


def test_transcript_tail_ignores_lines_without_timestamps(isolated):
    path, last = write_claude(isolated, 4242, "abc-123", "/w/proj")
    stamp, title = transcript_tail(str(path))
    assert abs(stamp - last) < 1.5
    assert title == "Fix the flaky checkout test"
    assert first_prompt_claude(str(path)) == "Fix the flaky test please"


def test_claude_session_is_found_with_its_transcript_and_resume_command(isolated):
    path, last = write_claude(isolated, 4242, "abc-123", "/w/proj", cfg_dir=".claude-work")
    finder = Finder(home=isolated)
    finder.ctx = FakeCtx(
        {
            4242: {
                "cmdline": ["claude"],
                "cwd": "/w/proj",
                "tty": "/dev/pts/77",
                "exe": "",
                "env": {"CLAUDE_CONFIG_DIR": str(isolated / ".claude-work")},
            }
        }
    )
    agents = finder.find([p(4242, "claude")])
    assert len(agents) == 1
    a = agents[0]
    assert a.session_id == "abc-123" and a.status == "idle" and a.interactive
    assert a.files[0] == str(path) and a.files[1].endswith("abc-123")
    assert a.title == "Fix the flaky checkout test"
    assert a.resume == "cd /w/proj && CLAUDE_CONFIG_DIR=~/.claude-work claude --resume abc-123"


def test_status_time_is_activity_when_newer(isolated):
    write_claude(isolated, 4242, "abc-123", "/w/proj", updated=time.time() - 60)
    finder = Finder(home=isolated)
    finder.ctx = FakeCtx({4242: {"cmdline": ["claude"], "cwd": "/w/proj", "tty": None, "exe": "", "env": {}}})
    a = finder.find([p(4242, "claude")])[0]
    assert a.activity == "status" and a.idle() < 120
    assert not a.interactive


def test_launcher_and_binary_fold_into_one_session(isolated):
    finder = Finder(home=isolated)
    finder.ctx = FakeCtx(
        {
            10: {
                "cmdline": ["node", "/usr/lib/node_modules/@openai/codex/bin/codex.js"],
                "cwd": "/w",
                "tty": "/dev/pts/1",
                "exe": "/usr/bin/node",
                "env": {},
            },
            11: {"cmdline": ["/x/codex"], "cwd": "/w", "tty": "/dev/pts/1", "exe": "/x/codex", "env": {}},
        }
    )
    agents = finder.find([p(10, "node", rss=50 << 20), p(11, "codex", ppid=10, rss=40 << 20)])
    assert len(agents) == 1 and agents[0].pid == 11 and agents[0].pids == [10, 11]
    assert agents[0].rss == 90 << 20


def test_codex_rollout_is_matched_by_folder_and_start_time(isolated):
    started = time.time() - 600
    day = isolated / ".codex" / "sessions" / "2026" / "11" / "12"
    day.mkdir(parents=True)
    rollout = day / "rollout-2026-11-12T10-00-00-0199.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "timestamp": iso(started + 2),
                "type": "session_meta",
                "payload": {"id": "0199", "cwd": "/w", "timestamp": iso(started + 2)},
            }
        )
        + "\n"
        + json.dumps(
            {
                "timestamp": iso(started + 30),
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "Migrate CI to uv"},
            }
        )
        + "\n"
    )
    other = day / "rollout-2026-11-12T10-00-01-0200.jsonl"
    other.write_text(json.dumps({"type": "session_meta", "payload": {"id": "0200", "cwd": "/elsewhere"}}) + "\n")
    finder = Finder(home=isolated)
    finder.ctx = FakeCtx({20: {"cmdline": ["codex"], "cwd": "/w", "tty": "/dev/pts/2", "exe": "", "env": {}}})
    a = finder.find([p(20, "codex", started=started)])[0]
    assert a.session_id == "0199" and a.title == "Migrate CI to uv"
    assert a.resume == "cd /w && codex resume 0199"


def test_bypass_flags_without_a_terminal_are_flagged(isolated):
    finder = Finder(home=isolated)
    finder.ctx = FakeCtx(
        {
            30: {
                "cmdline": ["claude", "-p", "--dangerously-skip-permissions"],
                "cwd": "/",
                "tty": None,
                "exe": "",
                "env": {},
            }
        }
    )
    a = finder.find([p(30, "claude")])[0]
    assert a.flags == ["--dangerously-skip-permissions"] and a.headless_bypass


def agent(**kw):
    base = dict(
        harness="claude",
        label="Claude Code",
        pid=1,
        pids=[1],
        started=0.0,
        interactive=True,
        status="idle",
        last_active=time.time() - 13 * 3600,
    )
    base.update(kw)
    return Agent(**base)


@pytest.mark.parametrize(
    ("kw", "stale", "why"),
    [
        ({}, True, "idle"),
        ({"interactive": False}, False, "doing a job"),
        ({"pinned": True}, False, "pinned"),
        ({"status": "busy"}, False, "working"),
        ({"last_active": time.time() - 3600}, False, "idle 1h"),
        ({"status": "waiting"}, True, "waiting for you"),
    ],
)
def test_staleness(cfg, kw, stale, why):
    verdict = reaper.staleness(agent(**kw), cfg)
    assert verdict.stale is stale and why in verdict.reason


def test_waiting_sessions_can_be_kept(cfg):
    cfg = config.set_value(cfg, "agents.include_waiting", False)
    assert not reaper.staleness(agent(status="waiting"), cfg).stale


def test_pressure_uses_the_short_limit(cfg):
    a = agent(last_active=time.time() - 45 * 60)
    assert not reaper.staleness(a, cfg).stale
    assert reaper.staleness(a, cfg, pressure=True).stale


def test_save_verifies_indexes_and_restores(isolated, cfg, store):
    path, _ = write_claude(isolated, 4242, "abc-123", "/w/proj")
    a = agent(
        pid=4242,
        session_id="abc-123",
        files=[str(path), str(path.with_suffix(""))],
        cwd="/w/proj",
        title="Fix the flaky checkout test",
        config_dir=str(isolated / ".claude"),
        resume="cd /w/proj && claude --resume abc-123",
    )
    saved = archive.save(a, cfg.archive_dir(), store, "manual")
    assert saved.ok and saved.files == 2
    manifest = json.loads((saved.path / "manifest.json").read_text())
    assert manifest["files"][0]["source"] == str(path)
    assert all(f["stored"].endswith((".zst", ".gz")) for f in manifest["files"])
    text = (saved.path / "transcript.md").read_text()
    assert "Fix the flaky test please" in text and "**tool** Bash: pytest -q" in text and "3 passed" in text
    assert "claude --resume abc-123" in (saved.path / "RESUME.txt").read_text()
    hits = store.sessions("race")
    assert hits and hits[0]["id"] == "claude:abc-123"
    original = path.read_bytes()
    path.unlink()
    report = archive.restore(saved.path)
    assert any("restored" in line for line in report)
    assert path.read_bytes() == original
    assert any("kept" in line for line in archive.restore(saved.path))


def test_save_without_any_transcript_fails(cfg, store):
    saved = archive.save(agent(pid=999, files=[]), cfg.archive_dir(), store, "manual")
    assert not saved.ok and "nothing to save" in saved.error


def test_render_codex_rollout():
    lines = [
        json.dumps(
            {
                "type": "response_item",
                "timestamp": "2026-11-12T10:00:00Z",
                "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            }
        ),
        json.dumps(
            {
                "type": "response_item",
                "payload": {"type": "function_call", "name": "shell", "arguments": json.dumps({"command": ["ls"]})},
            }
        ),
        json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "output": "a.txt"}}),
        "not json",
    ]
    text = render.markdown("codex", "T", lines=lines)
    assert "### You" in text and "**tool** shell" in text and "a.txt" in text


def test_render_opencode_export():
    doc = {
        "messages": [
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "hello"}]},
            {
                "info": {"role": "assistant"},
                "parts": [{"type": "tool", "tool": "bash", "state": {"input": {"command": "ls"}}}],
            },
        ]
    }
    text = render.markdown("opencode", "T", document=doc)
    assert "hello" in text and "**tool** bash: ls" in text


def test_close_saves_then_stops_a_real_process(isolated, cfg, store, tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(
            json.dumps({"type": "user", "timestamp": iso(time.time()), "message": {"content": "hello"}}) + "\n"
        )
        import psutil

        started = psutil.Process(child.pid).create_time()
        a = agent(
            pid=child.pid,
            pids=[child.pid],
            started=started,
            files=[str(transcript)],
            session_id="s1",
            resume="claude --resume s1",
            tty=None,
        )
        result = reaper.close(a, cfg, store, "idle 13h", grace=3)
        assert result["ok"] and result["outcome"] in ("terminated", "killed")
        assert child.wait(timeout=5) is not None
        assert Path(result["path"], "manifest.json").exists()
        assert store.events(kind="close")[0]["title"].startswith("Saved and closed")
    finally:
        if child.poll() is None:
            child.kill()


def test_observe_mode_changes_nothing(cfg, store):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        a = agent(pid=child.pid, files=[], session_id="s2")
        result = reaper.close(a, cfg, store, "idle", observe=True)
        assert result["outcome"] == "observed" and child.poll() is None
        assert not cfg.archive_dir().exists()
    finally:
        child.kill()


def test_close_refuses_when_nothing_can_be_saved(cfg, store):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        result = reaper.close(agent(pid=child.pid, files=[]), cfg, store, "idle")
        assert not result["ok"] and child.poll() is None
    finally:
        child.kill()


def test_human_formats():
    assert reaper.human(42) == "42s" and reaper.human(3 * 3600 + 60) == "3h 1m" and reaper.human(90000) == "1d 1h"
    assert reaper.size(1536 << 20) == "1.5 GB" and reaper.size(5 << 20) == "5 MB"
