"""
Decide which agent sessions are stale, and close one safely.

Save it, check the save, stop the process, then leave the resume command in its terminal.
"""

import time
from dataclasses import dataclass
from typing import Any

from huntaway import actions, osinfo
from huntaway.agents import archive
from huntaway.agents.harnesses import Agent
from huntaway.config import Config
from huntaway.store import Store


def human(seconds: float) -> str:
    """
    Format a duration for people.

    :param seconds: The duration
    :returns: Such as 45s, 12m, 3h 20m or 9d 4h
    """
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        hours, minutes = divmod(seconds // 60, 60)
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(seconds // 3600, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def size(n: float) -> str:
    """
    Format a byte count for people.

    :param n: Bytes
    :returns: Such as 512 MB or 1.4 GB
    """
    for unit, scale in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= scale:
            value = n / scale
            return f"{value:.1f} {unit}" if value < 10 and unit == "GB" else f"{value:.0f} {unit}"
    return f"{int(n)} B"


@dataclass(slots=True)
class Staleness:
    """Whether a session may be closed, and why."""

    stale: bool
    reason: str


def staleness(agent: Agent, cfg: Config, now: float | None = None, pressure: bool = False) -> Staleness:
    """
    Judge one session against the idle rules.

    :param agent: The session
    :param cfg: The settings
    :param now: The current time
    :param pressure: Memory is short, so the shorter idle limit applies
    :returns: The verdict with its reason
    """
    now = now or time.time()
    if not agent.interactive:
        return Staleness(False, "runs without a terminal, so it is doing a job")
    if agent.pinned:
        return Staleness(False, "pinned")
    if agent.protected:
        return Staleness(False, agent.protected)
    if agent.status == "busy":
        return Staleness(False, "working")
    if agent.status == "waiting" and not cfg["agents.include_waiting"]:
        return Staleness(False, "waiting for you")
    limit = cfg["agents.pressure_idle_minutes"] * 60 if pressure else cfg["agents.idle_hours"] * 3600
    idle = agent.idle(now)
    if idle < limit:
        return Staleness(False, f"idle {human(idle)} of {human(limit)}")
    what = "waiting for you" if agent.status == "waiting" else "idle"
    return Staleness(True, f"{what} {human(idle)}")


def close(
    agent: Agent,
    cfg: Config,
    store: Store | None,
    reason: str,
    grace: float = 5.0,
    observe: bool = False,
) -> dict[str, Any]:
    """
    Save a session, check the save, then stop its process and note the resume command in its terminal.

    Nothing is stopped when the save fails or cannot be checked.

    :param agent: The session
    :param cfg: The settings
    :param store: The database for the index and the event
    :param reason: Why: idle, pressure or manual
    :param grace: Seconds between SIGTERM and SIGKILL
    :param observe: Change nothing; record what would have happened
    :returns: What happened: ok, outcome, archive path, resume command and the event title
    """
    label = agent.title or agent.cwd or agent.label
    if observe:
        title = f"Would save and close {agent.label} session '{label}' ({reason})"
        if store is not None:
            store.add_event(
                "close", title, "info", agent.pid, agent.label, {"id": agent.key(), "rss": agent.rss, "observe": True}
            )
        return {"ok": True, "outcome": "observed", "resume": agent.resume, "title": title, "rss": agent.rss}
    saved = archive.save(
        agent, cfg.archive_dir(), store, "pressure" if reason == "pressure" else "closed", cfg["sessions.compress"]
    )
    if not saved.ok:
        title = f"Kept {agent.label} session open: it could not be saved ({saved.error})"
        if store is not None:
            store.add_event("close", title, "medium", agent.pid, agent.label, {"error": saved.error, "id": agent.key()})
        return {"ok": False, "outcome": "not saved", "error": saved.error, "title": title}
    outcome = actions.terminate(agent.pid, agent.started, grace)
    freed = size(agent.rss)
    title = f"Saved and closed {agent.label} session '{label}' ({reason}), freed about {freed}"
    if outcome in ("denied",):
        title = f"Saved {agent.label} session '{label}' but could not stop it (permission denied)"
    if cfg["agents.tty_note"] and agent.tty and outcome in ("terminated", "killed", "gone"):
        time.sleep(0.3)
        note = [
            "",
            f"huntaway saved and closed this {agent.label} session after {reason}.",
            f"  saved to  {saved.path}",
        ]
        if agent.resume:
            note.append(f"  resume    {agent.resume}")
        osinfo.write_tty(agent.tty, "\n".join(note) + "\n\n")
    if store is not None:
        store.add_event(
            "close",
            title,
            "low" if outcome != "denied" else "medium",
            agent.pid,
            agent.label,
            {
                "id": agent.key(),
                "path": str(saved.path),
                "resume": agent.resume,
                "rss": agent.rss,
                "outcome": outcome,
                "idle": agent.idle(),
                "cwd": agent.cwd,
            },
        )
    return {
        "ok": outcome != "denied",
        "outcome": outcome,
        "path": str(saved.path),
        "resume": agent.resume,
        "title": title,
        "rss": agent.rss,
    }
