"""
The running service and the API every interface shares.

The running service: the sampling loop, the guard's decisions carried out, the agent reaper, security checks,
backups and history. Every operation the dashboard, the MCP server and the CLI offer is a method registered in
``METHODS``, so the three always agree.
"""

import functools
import json
import os
import re
import resource
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

from huntaway import __version__, actions, config, paths
from huntaway.agents import archive, reaper
from huntaway.agents.harnesses import Agent, Finder
from huntaway.guard import Decision, Guard, read_leases
from huntaway.judge import Judge, Verdict
from huntaway.notify import Notifier
from huntaway.sampler import Proc, Sampler, Snapshot
from huntaway.security import checks, scanners
from huntaway.store import Store

RANGES = {
    "15m": 900,
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 86400,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
    "90d": 90 * 86400,
    "1y": 365 * 86400,
}


class ApiError(Exception):
    """A request that cannot be served, with an HTTP status."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class Method:
    """One operation of the API."""

    name: str
    fn: Callable[..., Any]
    readonly: bool
    summary: str
    params: dict[str, dict[str, Any]]


METHODS: dict[str, Method] = {}


def api(summary: str, readonly: bool = True, **params: dict[str, Any]) -> Callable[[Callable[..., Any]], Any]:
    """
    Register a Core method in the API.

    :param summary: What it does, shown to people and to agents
    :param readonly: False when it changes anything
    :param params: JSON schema for each parameter
    :returns: The decorator
    """

    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        name = fn.__name__
        METHODS[name] = Method(name, fn, readonly, summary, params)
        return fn

    return wrap


def app_name(name: str) -> str:
    """
    Group process names into apps for the analytics.

    :param name: A process name
    :returns: The app it belongs to
    """
    lower = name.lower()
    if lower in ("chrome", "chromium", "chromium-browse", "chrome_crashpad", "google-chrome"):
        return "chromium"
    if lower.startswith(
        (
            "firefox",
            "web content",
            "isolated web co",
            "privileged cont",
            "rdd process",
            "socket process",
            "utility process",
        )
    ):
        return "firefox"
    if lower.startswith(("python", "pypy")):
        return "python"
    if lower.startswith(("kworker", "ksoftirqd", "rcu_", "migration", "irq/", "kswapd")):
        return "kernel"
    return lower


class Core:
    """Owns the settings, the database and the live state."""

    def __init__(self, cfg: config.Config | None = None, store: Store | None = None) -> None:
        self.cfg = cfg or config.load()
        self.store = store or Store(paths.db_file())
        self.sampler = Sampler()
        self.finder = Finder()
        self.guard = Guard(self.cfg)
        self.judge = Judge(self.cfg)
        self.notifier = Notifier(self.cfg)
        self.workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="huntaway-work")
        self.lock = threading.RLock()
        self.snap: Snapshot | None = None
        self.agents: list[Agent] = []
        self.live: deque[dict[str, Any]] = deque(maxlen=720)
        self.started = time.time()
        self.running = False
        self.subscribers: list[Callable[[dict[str, Any]], None]] = []
        self._ticks = 0
        self._apps: dict[str, list[float]] = {}
        self._apps_minute = 0
        self._last: dict[str, float] = {}
        self._pending: list[tuple[Decision, Any, float]] = []
        self._closing: set[str] = set()
        self._stop = threading.Event()
        self.stats = {"actions": 0, "closed": 0, "freed": 0}

    # The loop

    def run(self) -> None:
        """Sample until stopped. Called by ``huntaway daemon``."""
        self.running = True
        self.store.add_event("service", f"huntaway {__version__} started in {self.cfg['general.mode']} mode")
        self.tick()
        while not self._stop.is_set():
            interval = 1.0 if self.guard.level != "ok" else float(self.cfg["general.interval"])
            self._stop.wait(interval)
            if self._stop.is_set():
                break
            try:
                self.tick()
            except Exception as exc:  # pragma: no cover - the loop must survive anything a sample throws
                self.store.add_event("service", f"Sampling error: {exc}", "medium")
                self._stop.wait(5)
        self.running = False

    def stop(self) -> None:
        """Ask the loop to stop."""
        self._stop.set()

    def shutdown(self) -> None:
        """Stop the loop and the worker threads."""
        self.stop()
        self.workers.shutdown(wait=False, cancel_futures=True)
        self.judge.close()
        self.notifier.close()
        self.store.add_event("service", "huntaway stopped")

    def _every(self, name: str, seconds: float, now: float) -> bool:
        if now - self._last.get(name, 0.0) >= seconds:
            self._last[name] = now
            return True
        return False

    def tick(self) -> None:
        """Take one sample and act on it."""
        now = time.time()
        self._maybe_reload()
        snap = self.sampler.sample()
        agents = self.agents
        if self.cfg["agents.enabled"] and (self._ticks % 3 == 0 or self.guard.level != "ok"):
            agents = self.finder.find(snap.procs, self.cfg["agents.extra"], now)
            pins = set(self.cfg["agents.pinned"])
            for a in agents:
                a.pinned = a.key() in pins or str(a.pid) in pins or (a.session_id or "") in pins
        elif not self.cfg["agents.enabled"]:
            agents = []
        self._ticks += 1
        if self._every("leases", 10, now):
            self.guard.leases = read_leases()
        with self.lock:
            self.snap = snap
            self.agents = agents
        self._account_apps(snap, agents, now)
        summary = self._summary(snap, agents)
        self.live.append(summary)
        row = {
            "ts": now,
            "cpu": snap.cpu,
            "mem": snap.mem_used_percent,
            "mem_available": snap.mem_available,
            "swap": summary["swap"],
            "load1": snap.load[0],
            "disk_read": snap.disk_read,
            "disk_write": snap.disk_write,
            "net_rx": snap.net_rx,
            "net_tx": snap.net_tx,
            "temp": snap.temp,
            "psi_cpu": snap.psi.get("cpu"),
            "psi_mem": snap.psi.get("memory"),
            "psi_io": snap.psi.get("io"),
            "agents": len(agents),
            "agents_rss": sum(a.rss for a in agents),
        }
        self.store.add_sample(row)
        self._act(self.guard.update(snap, agents, now))
        self._settle_pending(now)
        if self.cfg["agents.enabled"] and self.cfg["agents.reap"] and self._every("reap", 120, now):
            self._reap(now)
        if self._every("rollup", 60, now):
            self.store.rollup(now)
        if self._every("prune", 3600, now):
            self.store.prune(
                self.cfg["retention.raw_hours"],
                self.cfg["retention.minute_days"],
                self.cfg["retention.hour_days"],
                self.cfg["retention.events_days"],
            )
        if self.cfg["security.enabled"] and self._every("quick", self.cfg["security.quick_hours"] * 3600, now):
            self.workers.submit(self._quick_checks)
        if self._every("deep_import", 300, now):
            self._import_deep()
        if self.cfg["sessions.backup"] and self._every("backup", self.cfg["sessions.backup_hours"] * 3600, now):
            self.workers.submit(archive.backup_pass, self.cfg.archive_dir(), self.store)
        for fn in list(self.subscribers):
            try:
                fn(summary)
            except Exception:  # pragma: no cover - a broken subscriber is dropped
                self.subscribers.remove(fn)

    def _maybe_reload(self) -> None:
        path = self.cfg.path or paths.config_file()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        if mtime == self.cfg.mtime:
            return
        try:
            new = config.load(path)
        except config.ConfigError as exc:
            self.cfg.mtime = mtime
            self.store.add_event("config", f"Config not applied: {exc}", "medium")
            self.notifier.send("huntaway: config not applied", str(exc), "medium")
            return
        self._apply(new)
        self.store.add_event("config", "Config reloaded")

    def _apply(self, new: config.Config) -> None:
        self.cfg = new
        self.guard.reload(new)
        self.judge.cfg = new
        self.notifier.cfg = new

    def _summary(self, snap: Snapshot, agents: list[Agent]) -> dict[str, Any]:
        out = snap.summary()
        stale = [a for a in agents if reaper.staleness(a, self.cfg, snap.ts).stale]
        out.update(
            {
                "level": self.guard.level,
                "agents": len(agents),
                "agents_rss": sum(a.rss for a in agents),
                "agents_stale": len(stale),
                "agents_stale_rss": sum(a.rss for a in stale),
                "cost_ms": round(self.sampler.last_cost * 1000, 1),
            }
        )
        return out

    def _account_apps(self, snap: Snapshot, agents: list[Agent], now: float) -> None:
        minute = int(now // 60 * 60)
        if self._apps_minute and minute != self._apps_minute and self._apps:
            self.store.add_apps(self._apps_minute, {k: (v[0], v[1]) for k, v in self._apps.items()})
            self._apps = {}
        self._apps_minute = minute
        labels = {pid: a.label for a in agents for pid in a.pids}
        interval = float(self.cfg["general.interval"]) if self.guard.level == "ok" else 1.0
        rss: dict[str, float] = {}
        for p in snap.procs:
            name = labels.get(p.pid) or app_name(p.name)
            rss[name] = rss.get(name, 0.0) + p.rss
            if p.cpu > 0:
                slot = self._apps.setdefault(name, [0.0, 0.0])
                slot[0] += p.cpu * interval / 100.0
        for name, total in sorted(rss.items(), key=lambda kv: kv[1], reverse=True)[:25]:
            slot = self._apps.setdefault(name, [0.0, 0.0])
            slot[1] = max(slot[1], total)

    # Carrying out decisions

    def _act(self, decisions: list[Decision]) -> None:
        observe = self.cfg["general.mode"] == "observe"
        for d in decisions:
            if d.action in ("terminate", "kill") and d.kind != "memory" and self.judge.mode == "veto":
                facts = self._facts(d)
                self._pending.append((d, self.judge.submit(facts), time.time() + float(self.cfg["judge.timeout"])))
                continue
            self._execute(d, observe)
            if d.action in ("terminate", "kill", "close") and self.judge.mode == "advise":
                future = self.judge.submit(self._facts(d))
                future.add_done_callback(functools.partial(self._advice_done, d))

    def _facts(self, d: Decision) -> dict[str, Any]:
        snap = self.snap
        return {
            "process": d.name,
            "pid": d.pid,
            "command": self.guard.command(Proc(d.pid, 0, d.name, -1, d.started, 0.0, 0, "", 0.0)),
            "rule": d.kind,
            "proposed_action": d.action,
            "reason": d.reason,
            "detail": d.detail,
            "machine": snap.summary() if snap else {},
        }

    def _advice_done(self, d: Decision, future: Future[Verdict]) -> None:
        self._advice(d, future.result())

    def _advice(self, d: Decision, v: Verdict) -> None:
        self.store.add_event(
            "judge",
            f"Judge on {d.name} ({d.action}): {v.verdict}. {v.reason}".strip(),
            "info",
            d.pid,
            d.name,
            v.to_dict(),
        )

    def _settle_pending(self, now: float) -> None:
        keep = []
        observe = self.cfg["general.mode"] == "observe"
        for d, future, deadline in self._pending:
            if future.done():
                verdict = future.result()
                if verdict.verdict == "spare" and not verdict.error:
                    self.guard.spare(d)
                    self.store.add_event(
                        "judge",
                        f"Judge spared {d.name}: {verdict.reason}",
                        "info",
                        d.pid,
                        d.name,
                        {"decision": d.reason, **verdict.to_dict()},
                    )
                    continue
                self._execute(d, observe, verdict)
            elif now > deadline:
                self._execute(d, observe, Verdict("act", "", str(self.cfg["judge.model"]), "no verdict in time"))
            else:
                keep.append((d, future, deadline))
        self._pending = keep

    def _execute(self, d: Decision, observe: bool, verdict: Verdict | None = None) -> None:
        detail = {**d.detail, "kind": d.kind, "reason": d.reason}
        if verdict is not None:
            detail["judge"] = verdict.to_dict()
        if observe:
            self.store.add_event(
                d.action,
                f"Would {d.action} {d.name} (pid {d.pid}): {d.reason}",
                "info",
                d.pid,
                d.name,
                {**detail, "observe": True},
            )
            return
        if d.action == "close" and d.agent is not None:
            self._close_async(d.agent, "pressure")
            return
        if d.action == "notify":
            title = f"{d.name} (pid {d.pid}): {d.reason}"
            self.store.add_event("alert", title, d.severity, d.pid, d.name, detail)
            self.notifier.send("huntaway", title, d.severity)
            return
        if d.action == "throttle":
            outcome = actions.throttle(d.pid, d.started)
            self._record(d, outcome, detail)
            return
        if d.action == "kill":
            future = self.workers.submit(actions.kill, d.pid, d.started)
        else:
            future = self.workers.submit(actions.terminate, d.pid, d.started, float(self.cfg["memory.grace"]))
        future.add_done_callback(functools.partial(self._record_done, d, detail))

    def _record_done(self, d: Decision, detail: dict[str, Any], future: Future[str]) -> None:
        self._record(d, future.result(), detail)

    def _record(self, d: Decision, outcome: str, detail: dict[str, Any]) -> None:
        verbs = {"throttled": "Throttled", "terminated": "Stopped", "killed": "Killed"}
        if outcome not in verbs:
            if outcome == "denied":
                self.store.add_event(
                    d.action,
                    f"Could not {d.action} {d.name} (pid {d.pid}): permission denied",
                    "medium",
                    d.pid,
                    d.name,
                    detail,
                )
            return
        rss = int(d.detail.get("rss", 0))
        title = f"{verbs[outcome]} {d.name} (pid {d.pid}): {d.reason}"
        self.store.add_event(d.action, title, d.severity, d.pid, d.name, {**detail, "outcome": outcome})
        self.stats["actions"] += 1
        if outcome != "throttled":
            self.stats["freed"] += rss
        self.notifier.send("huntaway", title, d.severity, {"pid": d.pid, "name": d.name, "kind": d.kind})

    def _close_async(self, agent: Agent, reason: str) -> None:
        key = agent.key()
        if key in self._closing:
            return
        self._closing.add(key)

        def job() -> dict[str, Any]:
            try:
                result = reaper.close(
                    agent,
                    self.cfg,
                    self.store,
                    reason,
                    float(self.cfg["memory.grace"]),
                    self.cfg["general.mode"] == "observe",
                )
                if result.get("ok") and result.get("outcome") in ("terminated", "killed"):
                    self.stats["closed"] += 1
                    self.stats["freed"] += int(result.get("rss", 0))
                    body = result["title"] + (f"\nResume: {agent.resume}" if agent.resume else "")
                    self.notifier.send("huntaway saved and closed a session", body, "low")
                return result
            finally:
                self._closing.discard(key)

        self.workers.submit(job)

    def _reap(self, now: float) -> None:
        pressure = self.guard.level != "ok"
        for agent in list(self.agents):
            if reaper.staleness(agent, self.cfg, now, pressure).stale:
                self._close_async(agent, "pressure" if pressure else f"idle {reaper.human(agent.idle(now))}")

    def _quick_checks(self) -> None:
        results = checks.run_all(self.store, list(self.agents))
        new_bad = [f for fs in results.values() for f in fs if f["severity"] in ("high", "critical")]
        if new_bad:
            self.notifier.send("huntaway security", f"{len(new_bad)} serious finding(s): {new_bad[0]['title']}", "high")

    def _import_deep(self) -> None:
        reports = scanners.latest_reports()
        if not reports:
            return
        latest = reports[0]
        if str(latest.get("ts")) == self.store.get_meta("deep_ts"):
            return
        for result in latest.get("results", []):
            source = f"deep:{result['source']}"
            if result.get("status") == "missing":
                continue
            new = self.store.upsert_findings(source, result.get("findings", []), latest.get("ts"))
            self.store.add_scan(
                source,
                result.get("duration", 0),
                result.get("status", ""),
                {**result.get("summary", {}), "findings": len(result.get("findings", [])), "new": len(new)},
            )
        self.store.set_meta("deep_ts", str(latest.get("ts")))

    # Helpers for the API

    def _fresh(self) -> Snapshot:
        with self.lock:
            snap = self.snap
        if snap is not None and time.time() - snap.ts < 30:
            return snap
        self.sampler.sample()
        time.sleep(0.3)
        snap = self.sampler.sample()
        agents = self.finder.find(snap.procs, self.cfg["agents.extra"]) if self.cfg["agents.enabled"] else []
        pins = set(self.cfg["agents.pinned"])
        for a in agents:
            a.pinned = a.key() in pins or str(a.pid) in pins or (a.session_id or "") in pins
        with self.lock:
            self.snap, self.agents = snap, agents
        return snap

    def _agent(self, ident: str) -> Agent:
        self._fresh()
        for a in self.agents:
            if ident in (a.key(), str(a.pid), a.session_id):
                return a
        raise ApiError(f"no running agent session matches {ident!r}", 404)

    def _save_cfg(self, new: config.Config) -> None:
        config.save(new)
        try:
            new.mtime = (new.path or paths.config_file()).stat().st_mtime
        except OSError:
            pass
        self._apply(new)

    # The API

    @api("Machine health at a glance: CPU, memory, swap, pressure, agents, findings and the service itself.")
    def status(self) -> dict[str, Any]:
        """
        Return the overview.

        :returns: A JSON ready dict
        """
        snap = self._fresh()
        agents = self.agents
        usage = resource.getrusage(resource.RUSAGE_SELF)
        me = psutil.Process()
        findings = self.store.findings()
        counts: dict[str, int] = {}
        for f in findings:
            counts[f["severity"]] = counts.get(f["severity"], 0) + 1
        return {
            "version": __version__,
            "running": self.running,
            "mode": self.cfg["general.mode"],
            "level": self.guard.level,
            "uptime": round(time.time() - self.started),
            "system": self._summary(snap, agents),
            "service": {
                "rss": me.memory_info().rss,
                "cpu_seconds": round(usage.ru_utime + usage.ru_stime, 2),
                "cpu_percent": round(100 * (usage.ru_utime + usage.ru_stime) / max(1, time.time() - self.started), 2),
                "sample_ms": round(self.sampler.last_cost * 1000, 1),
                "pid": os.getpid(),
            },
            "stats": dict(self.stats),
            "findings": counts,
            "judge": self.judge.mode,
            "events": self.store.events(8),
        }

    @api(
        "The busiest processes now, with what protects each one.",
        sort={"type": "string", "enum": ["cpu", "mem"], "default": "cpu"},
        limit={"type": "integer", "default": 25, "minimum": 1, "maximum": 200},
    )
    def processes(self, sort: str = "cpu", limit: int = 25) -> list[dict[str, Any]]:
        """
        Top processes.

        :param sort: cpu or mem
        :param limit: How many
        :returns: Process rows
        """
        snap = self._fresh()
        procs = sorted(snap.procs, key=lambda p: p.rss if sort == "mem" else p.cpu, reverse=True)[: int(limit)]
        family = {os.getpid()}
        labels = {pid: a.label for a in self.agents for pid in a.pids}
        out = []
        for p in procs:
            cmd = self.guard.command(p)
            exe = cmd.split(" ", 1)[0] if cmd else ""
            out.append(
                {
                    "pid": p.pid,
                    "name": os.path.basename(exe) if len(p.name) == 15 and exe else p.name,
                    "cpu": round(p.cpu, 1),
                    "rss": p.rss,
                    "user": p.uid == os.getuid(),
                    "started": p.started,
                    "command": cmd[:300],
                    "agent": labels.get(p.pid),
                    "nice": actions.niceness(p.pid),
                    "protected": self.guard.protection(p, family, set()),
                }
            )
        return out

    @api("Running AI agent sessions (Claude Code, Codex, OpenCode and more): memory, idle time, stale or not.")
    def agents_list(self) -> list[dict[str, Any]]:
        """
        Agent sessions.

        :returns: One dict per session, with ``stale`` and ``why``
        """
        self._fresh()
        now = time.time()
        out = []
        for a in self.agents:
            verdict = reaper.staleness(a, self.cfg, now, self.guard.level != "ok")
            out.append({**a.to_dict(now), "stale": verdict.stale, "why": verdict.reason})
        return out

    @api(
        "Save an agent session to the archive now, without closing it.",
        readonly=False,
        id={"type": "string", "description": "Session id, harness:session or the pid"},
    )
    def agent_save(self, id: str) -> dict[str, Any]:
        """
        Save one session.

        :param id: The session id or pid
        :returns: Where it was saved
        :raises ApiError: When it cannot be saved
        """
        agent = self._agent(id)
        saved = archive.save(agent, self.cfg.archive_dir(), self.store, "manual", self.cfg["sessions.compress"])
        if not saved.ok:
            raise ApiError(f"could not save it: {saved.error}", 500)
        self.store.add_event(
            "archive",
            f"Saved {agent.label} session '{agent.title or agent.cwd}'",
            "info",
            agent.pid,
            agent.label,
            {"path": str(saved.path), "id": agent.key()},
        )
        return {
            "ok": True,
            "path": str(saved.path),
            "files": saved.files,
            "size": saved.size,
            "stored": saved.stored,
            "resume": agent.resume,
            "id": agent.key(),
        }

    @api(
        "Save an agent session, then close it. The resume command is printed in its terminal.",
        readonly=False,
        id={"type": "string", "description": "Session id, harness:session or the pid"},
    )
    def agent_close(self, id: str) -> dict[str, Any]:
        """
        Save and close one session.

        :param id: The session id or pid
        :returns: What happened
        :raises ApiError: For the session running this request
        """
        agent = self._agent(id)
        if agent.pid in _ancestors_of_caller():
            raise ApiError("that is the session asking; it will not close itself", 409)
        return reaper.close(
            agent,
            self.cfg,
            self.store,
            "manual",
            float(self.cfg["memory.grace"]),
            self.cfg["general.mode"] == "observe",
        )

    @api(
        "Close every stale session (save first). dry_run lists them without closing.",
        readonly=False,
        dry_run={"type": "boolean", "default": True},
    )
    def agents_reap(self, dry_run: bool = True) -> dict[str, Any]:
        """
        Close the stale sessions.

        :param dry_run: Only list them
        :returns: The sessions and what happened to each
        """
        self._fresh()
        now = time.time()
        caller = _ancestors_of_caller()
        stale = [a for a in self.agents if reaper.staleness(a, self.cfg, now).stale and a.pid not in caller]
        if dry_run:
            return {"dry_run": True, "sessions": [a.to_dict(now) for a in stale], "rss": sum(a.rss for a in stale)}
        results = [
            reaper.close(
                a,
                self.cfg,
                self.store,
                f"idle {reaper.human(a.idle(now))}",
                float(self.cfg["memory.grace"]),
                self.cfg["general.mode"] == "observe",
            )
            for a in stale
        ]
        return {"dry_run": False, "results": results, "rss": sum(a.rss for a in stale)}

    @api(
        "Pin a session so it is never closed, or unpin it.",
        readonly=False,
        id={"type": "string"},
        pinned={"type": "boolean", "default": True},
    )
    def agent_pin(self, id: str, pinned: bool = True) -> dict[str, Any]:
        """
        Pin or unpin.

        :param id: The session id or pid
        :param pinned: True to pin
        :returns: The pinned list
        """
        try:
            key = self._agent(id).key()
        except ApiError:
            key = id
        pins = [p for p in self.cfg["agents.pinned"] if p != key]
        if pinned:
            pins.append(key)
        self._save_cfg(config.set_value(self.cfg, "agents.pinned", pins))
        return {"pinned": pins}

    @api(
        "Saved sessions, newest first, or full text search across every saved transcript.",
        query={"type": "string", "default": ""},
        harness={"type": "string", "default": ""},
        limit={"type": "integer", "default": 50},
        offset={"type": "integer", "default": 0},
    )
    def sessions(self, query: str = "", harness: str = "", limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """
        List or search saved sessions.

        :param query: Words to search for
        :param harness: Only this harness
        :param limit: How many
        :param offset: How many to skip
        :returns: Session rows
        """
        return self.store.sessions(query, harness, min(int(limit), 500), int(offset))

    @api(
        "Read a saved session: its details and the readable transcript, in pages.",
        id={"type": "string"},
        offset={"type": "integer", "default": 0},
        length={"type": "integer", "default": 60000},
    )
    def session_read(self, id: str, offset: int = 0, length: int = 60000) -> dict[str, Any]:
        """
        One saved session.

        :param id: The session id
        :param offset: Character offset into the transcript
        :param length: Characters to return
        :returns: The row, the manifest and a slice of the transcript
        :raises ApiError: When it does not exist
        """
        row = self.store.session(id)
        if row is None:
            raise ApiError(f"no saved session {id!r}", 404)
        folder = Path(row["path"])
        try:
            text = (folder / "transcript.md").read_text(encoding="utf-8")
        except OSError:
            text = ""
        manifest: dict[str, Any] = {}
        try:
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        except OSError, ValueError:
            pass
        offset, length = max(0, int(offset)), max(1, min(int(length), 500_000))
        return {
            "session": row,
            "manifest": manifest,
            "total": len(text),
            "offset": offset,
            "text": text[offset : offset + length],
            "screen": (folder / "screen.txt").is_file(),
        }

    @api(
        "Put a saved session's files back where its harness looks, so it can be resumed.",
        readonly=False,
        id={"type": "string"},
        force={"type": "boolean", "default": False},
    )
    def session_restore(self, id: str, force: bool = False) -> dict[str, Any]:
        """
        Restore one session.

        :param id: The session id
        :param force: Overwrite existing files
        :returns: What was restored and the resume command
        :raises ApiError: When it does not exist
        """
        row = self.store.session(id)
        if row is None:
            raise ApiError(f"no saved session {id!r}", 404)
        try:
            report = archive.restore(Path(row["path"]), bool(force))
        except OSError as exc:
            raise ApiError(f"could not restore: {exc}", 500) from None
        self.store.add_event("archive", f"Restored session {id}", "info", detail={"report": report})
        return {"report": report, "resume": row.get("resume")}

    @api("Delete a saved session from the archive.", readonly=False, id={"type": "string"})
    def session_delete(self, id: str) -> dict[str, Any]:
        """
        Delete one saved session.

        :param id: The session id
        :returns: ok
        :raises ApiError: When it does not exist or is outside the archive
        """
        row = self.store.session(id)
        if row is None:
            raise ApiError(f"no saved session {id!r}", 404)
        folder = Path(row["path"]).resolve()
        root = self.cfg.archive_dir().resolve()
        if root not in folder.parents:
            raise ApiError("that session lives outside the archive; delete it by hand", 409)
        shutil.rmtree(folder, ignore_errors=True)
        self.store.delete_session(id)
        self.store.add_event("archive", f"Deleted saved session {id}")
        return {"ok": True}

    @api(
        "What huntaway did and saw, newest first. kind filters: close, terminate, kill, throttle, alert, "
        "archive, judge, config, service.",
        limit={"type": "integer", "default": 50},
        kind={"type": "string", "default": ""},
        since={"type": "number", "default": 0},
    )
    def events(self, limit: int = 50, kind: str = "", since: float = 0) -> list[dict[str, Any]]:
        """
        Events.

        :param limit: How many
        :param kind: Comma separated kinds
        :param since: Only after this time
        :returns: Event rows
        """
        return self.store.events(min(int(limit), 1000), kind or None, float(since))

    @api(
        "History of the machine for charts: CPU, memory, swap, IO, network, temperature, pressure, agents; "
        "the top apps; a weekday by hour heatmap; actions per day.",
        range={"type": "string", "enum": list(RANGES), "default": "24h"},
    )
    def analytics(self, range: str = "24h") -> dict[str, Any]:
        """
        Analytics for a range.

        :param range: 15m, 1h, 6h, 24h, 7d, 30d, 90d or 1y
        :returns: Series, top apps, heatmap and event counts
        :raises ApiError: For an unknown range
        """
        if range not in RANGES:
            raise ApiError(f"range must be one of {', '.join(RANGES)}")
        seconds = RANGES[range]
        harness = self.store.query(
            "select harness, count(*) as n, sum(size) as size from sessions group by harness order by n desc"
        )
        return {
            "range": range,
            "series": self.store.series(seconds),
            "apps": self.store.top_apps(seconds),
            "heatmap": self.store.heatmap(),
            "events": self.store.event_counts(max(seconds, 7 * 86400)),
            "sessions": harness,
            "memory_total": self.snap.mem_total if self.snap else psutil.virtual_memory().total,
            "cores": self.sampler.cores,
        }

    @api("Security findings from the quick checks and the deep scans, with the scanners' state.")
    def security(self) -> dict[str, Any]:
        """
        Return the security view.

        :returns: Findings, last runs, installed scanners and the install command for the missing ones
        """
        ignore = [re.compile(p) for p in self.cfg["security.ignore"]]
        findings = [
            f
            for f in self.store.findings()
            if f["id"] not in self.cfg["security.ignore"] and not any(p.search(f["title"]) for p in ignore)
        ]
        have = scanners.available()
        missing = [k for k, v in have.items() if not v]
        reports = scanners.latest_reports()
        lynis = next(
            (
                r.get("summary", {}).get("hardening_index")
                for rep in reports[:1]
                for r in rep.get("results", [])
                if r.get("source") == "lynis"
            ),
            None,
        )
        return {
            "findings": findings,
            "scans": self.store.last_scans(),
            "scanners": have,
            "install": scanners.install_command(missing),
            "last_deep": reports[0]["ts"] if reports else None,
            "hardening_index": lynis,
        }

    @api("Run the quick security checks now (seconds, no root needed).", readonly=False)
    def security_scan(self) -> dict[str, Any]:
        """
        Run the quick checks.

        :returns: Findings per check
        """
        self._fresh()
        results = checks.run_all(self.store, list(self.agents))
        return {name: len(found) for name, found in results.items()}

    @api("Hide a finding from now on.", readonly=False, id={"type": "string"})
    def finding_ignore(self, id: str) -> dict[str, Any]:
        """
        Ignore a finding.

        :param id: The finding id
        :returns: The ignore list
        """
        ignore = [*self.cfg["security.ignore"], id]
        self._save_cfg(config.set_value(self.cfg, "security.ignore", sorted(set(ignore))))
        return {"ignore": self.cfg["security.ignore"]}

    @api("The settings: every value, the rules and the schema with help text.")
    def config_get(self) -> dict[str, Any]:
        """
        Read the settings.

        :returns: Values, rules, schema and the file path
        """
        return {
            **self.cfg.to_public(),
            "schema": config.schema(),
            "sections": config.SECTIONS,
            "path": str(self.cfg.path or paths.config_file()),
        }

    @api("Which agent harnesses are connected to huntaway, and the commands that connect them.")
    def integrations(self) -> dict[str, Any]:
        """
        Integration status.

        :returns: Dry run steps per harness, the MCP command and endpoints
        """
        from dataclasses import asdict

        from huntaway import integrate

        host = str(self.cfg["server.host"])
        host = "127.0.0.1" if host in ("0.0.0.0", "::") else host  # nosec B104  # display only
        base = f"http://{host}:{self.cfg['server.port']}"
        return {
            "steps": [asdict(s) for s in integrate.run([], dry=True)],
            "command": integrate.command(),
            "agents_md": integrate.AGENTS_MD,
            "mcp_http": f"{base}/mcp",
            "metrics": f"{base}/metrics",
        }

    @api(
        "Change settings. values maps dotted keys (memory.sustain) to values; rules replaces the rule list.",
        readonly=False,
        values={"type": "object", "default": {}},
        rules={"type": "array"},
    )
    def config_set(self, values: dict[str, Any] | None = None, rules: list[Any] | None = None) -> dict[str, Any]:
        """
        Change settings and save them.

        :param values: Dotted key to value
        :param rules: The full rule list, when changing rules
        :returns: The settings after the change
        :raises ApiError: When a value does not fit
        """
        new = self.cfg
        try:
            for key, value in (values or {}).items():
                new = config.set_value(new, key, value)
            if rules is not None:
                new = config.Config(values=new.values, rules=config.parse_rules(rules), path=new.path, mtime=new.mtime)
        except config.ConfigError as exc:
            raise ApiError(str(exc)) from None
        self._save_cfg(new)
        self.store.add_event("config", f"Settings changed: {', '.join(values or {}) or 'rules'}")
        return self.cfg.to_public()

    @api(
        "Act on one process: throttle, terminate, kill or protect (adds its name to the protected list).",
        readonly=False,
        pid={"type": "integer"},
        action={"type": "string", "enum": ["throttle", "terminate", "kill", "protect"]},
    )
    def process_action(self, pid: int, action: str) -> dict[str, Any]:
        """
        Act on a process.

        :param pid: The process id
        :param action: throttle, terminate, kill or protect
        :returns: The outcome
        :raises ApiError: When the process is protected, missing or the action unknown
        """
        snap = self._fresh()
        proc = next((p for p in snap.procs if p.pid == int(pid)), None)
        if proc is None:
            raise ApiError(f"no process {pid}", 404)
        if action == "protect":
            names = sorted({*self.cfg["protect.names"], proc.name})
            self._save_cfg(config.set_value(self.cfg, "protect.names", names))
            return {"ok": True, "protected": names}
        reason = self.guard.protection(proc, {os.getpid()} | _ancestors_of_caller(), set())
        if reason:
            raise ApiError(f"{proc.name} is protected: {reason}", 409)
        if action == "throttle":
            outcome = actions.throttle(proc.pid, proc.started)
        elif action == "terminate":
            outcome = actions.terminate(proc.pid, proc.started, float(self.cfg["memory.grace"]))
        elif action == "kill":
            outcome = actions.kill(proc.pid, proc.started)
        else:
            raise ApiError("action must be throttle, terminate, kill or protect")
        self.store.add_event(
            action,
            f"{outcome.capitalize()} {proc.name} (pid {proc.pid}) by hand",
            "info",
            proc.pid,
            proc.name,
            {"outcome": outcome, "manual": True},
        )
        return {"ok": outcome not in ("denied",), "outcome": outcome}

    @api(
        "Ask the configured model (judge) what it thinks of ending a process. Needs judge.mode on.",
        pid={"type": "integer"},
    )
    def judge_ask(self, pid: int) -> dict[str, Any]:
        """
        Ask the judge about a process.

        :param pid: The process id
        :returns: The verdict
        :raises ApiError: When the judge is off or the process missing
        """
        if self.judge.mode == "off":
            raise ApiError("the judge is off: set judge.mode and judge.model first", 409)
        snap = self._fresh()
        proc = next((p for p in snap.procs if p.pid == int(pid)), None)
        if proc is None:
            raise ApiError(f"no process {pid}", 404)
        d = Decision(
            "manual",
            "terminate",
            proc.pid,
            proc.started,
            proc.name,
            "asked by hand",
            detail={"cpu": proc.cpu, "rss": proc.rss},
        )
        return self.judge.ask(self._facts(d)).to_dict()

    @api("The last ten minutes of samples, for live charts.")
    def live_samples(self) -> list[dict[str, Any]]:
        """
        Recent samples.

        :returns: Up to 720 summaries
        """
        if not self.live:
            self._fresh()
            if self.snap is not None:
                self.live.append(self._summary(self.snap, self.agents))
        return list(self.live)


_caller_pids: threading.local = threading.local()


def _ancestors_of_caller() -> set[int]:
    pids: set[int] = getattr(_caller_pids, "pids", set())
    return pids


def set_caller(pids: set[int]) -> None:
    """
    Remember which processes are making the current request, so a session never closes itself.

    :param pids: The caller's process and its ancestors
    """
    _caller_pids.pids = pids


def call(core: Core, name: str, params: dict[str, Any] | None = None) -> Any:
    """
    Run an API method by name.

    :param core: The core
    :param name: The method
    :param params: Its parameters
    :returns: Its result
    :raises ApiError: For an unknown method or bad parameters
    """
    method = METHODS.get(name)
    if method is None:
        raise ApiError(f"unknown method {name!r}", 404)
    params = dict(params or {})
    unknown = set(params) - set(method.params)
    if unknown:
        raise ApiError(f"{name} does not take {', '.join(sorted(unknown))}")
    try:
        return getattr(core, method.name)(**params)
    except TypeError as exc:
        raise ApiError(f"{name}: {exc}") from None
