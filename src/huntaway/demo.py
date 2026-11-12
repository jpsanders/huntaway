"""
The dashboard on made up data.

``huntaway demo`` builds a month of history, a handful of agent sessions, saved transcripts and findings in a
temporary folder, then serves the real dashboard on them. Nothing on the machine is read or touched, which makes it
safe for screenshots and for trying huntaway before installing the service.
"""

import math
import os
import random
import tempfile
import time
import webbrowser
from pathlib import Path
from typing import Any

from huntaway import config
from huntaway.agents.harnesses import BY_KEY, Agent
from huntaway.core import ApiError, Core
from huntaway.sampler import Proc, Snapshot
from huntaway.store import Store

GB = 1 << 30
MB = 1 << 20

AGENTS = [
    ("claude", "Refactor the billing service to use idempotency keys", "~/code/acme-api", "busy", 0, 610),
    ("claude", "Fix the flaky checkout test", "~/code/storefront", "idle", 3.2 * 3600, 540),
    ("claude", "Write the Q3 data quality report", "~/work/data-pipeline", "idle", 19 * 3600, 480),
    ("codex", "Migrate CI from pip to uv", "~/code/infra", "unknown", 26 * 3600, 140),
    ("opencode", "Add dark mode to the docs site", "~/code/docs", "unknown", 41 * 60, 230),
    ("gemini", "Summarise the incident timeline", "~/notes", "waiting", 50 * 3600, 310),
    ("claude", "Upgrade Postgres 16 to 17", "~/code/acme-api", "idle", 5 * 86400, 450),
]

PROCS = [
    ("chrome", 7.5, 1480),
    ("node", 54.0, 2210),
    ("claude", 3.1, 610),
    ("code", 4.2, 890),
    ("python3", 96.0, 740),
    ("postgres", 1.2, 380),
    ("slack", 0.8, 520),
    ("gnome-shell", 2.4, 430),
    ("docker", 0.6, 210),
    ("opencode", 0.4, 230),
    ("codex", 0.1, 140),
    ("pipewire", 0.3, 30),
]

APPS = [
    ("claude code", 1.4),
    ("chromium", 1.1),
    ("node", 0.9),
    ("python", 0.7),
    ("code", 0.35),
    ("codex", 0.3),
    ("opencode", 0.25),
    ("docker", 0.2),
    ("postgres", 0.15),
    ("slack", 0.1),
]

TRANSCRIPT = """# {title}

### You · {when}

{title}. Keep the public API the same and add tests for the edge cases.

> **tool** Bash: git status --short

```text
M  src/billing/charge.py
M  tests/test_charge.py
```

### Claude · {when}

I read the charge flow first. A retry after a timeout can charge twice, because nothing ties the second request to
the first. I added an idempotency key per charge, stored with a unique index, and the retry now returns the first
result instead of charging again.

> **tool** Bash: pytest -q tests/test_charge.py

```text
14 passed in 0.82s
```

### You · {when}

Good. Write the migration and note the rollback.
"""


def _wave(t: float, base: float, amp: float, period: float, phase: float = 0.0) -> float:
    return base + amp * math.sin(2 * math.pi * (t / period) + phase)


def _sample_at(t: float, rng: random.Random) -> dict[str, Any]:
    hour = time.localtime(t).tm_hour + time.localtime(t).tm_min / 60
    weekday = time.localtime(t).tm_wday
    work = 1.0 if 9 <= hour <= 19 and weekday < 5 else 0.35
    cpu = max(2.0, min(97.0, work * _wave(t, 34, 14, 5400) + rng.gauss(0, 5) + (30 if rng.random() < 0.01 else 0)))
    agents_rss = max(0.4, work * _wave(t, 3.4, 1.4, 7 * 3600) + (t % 21600) / 21600 * 1.8) * GB
    mem = max(28.0, min(96.0, 49 + agents_rss / GB * 5.5 + rng.gauss(0, 2.0)))
    return {
        "ts": t,
        "cpu": cpu,
        "mem": mem,
        "mem_available": (100 - mem) / 100 * 32 * GB,
        "swap": max(0.0, min(100.0, 6 + (mem - 60) * 1.4 + rng.gauss(0, 1.5))),
        "load1": cpu / 100 * 16 * 0.6,
        "disk_read": abs(rng.gauss(4, 6)) * MB * work,
        "disk_write": abs(rng.gauss(6, 8)) * MB * work,
        "net_rx": abs(rng.gauss(1.2, 1.5)) * MB * work,
        "net_tx": abs(rng.gauss(0.3, 0.4)) * MB * work,
        "temp": 44 + cpu * 0.38 + rng.gauss(0, 1.5),
        "psi_cpu": max(0.0, (cpu - 60) * 0.4 + rng.gauss(0, 0.6)),
        "psi_mem": max(0.0, (mem - 80) * 0.9 + rng.gauss(0, 0.4)),
        "psi_io": max(0.0, rng.gauss(0.6, 0.5) * work),
        "agents": int(2 + work * 5),
        "agents_rss": agents_rss,
    }


def seed(store: Store, archive_root: Path, now: float, rng: random.Random) -> None:
    """
    Fill a database with a month of plausible history.

    :param store: The empty database
    :param archive_root: Where to write the saved transcripts
    :param now: The current time
    :param rng: The random source
    """
    cols = [
        "ts",
        "cpu",
        "mem",
        "mem_available",
        "swap",
        "load1",
        "disk_read",
        "disk_write",
        "net_rx",
        "net_tx",
        "temp",
        "psi_cpu",
        "psi_mem",
        "psi_io",
        "agents",
        "agents_rss",
    ]
    with store.write() as db:
        for table, step, span in (
            ("samples_1h", 3600, 31 * 86400),
            ("samples_1m", 60, 8 * 86400),
            ("samples", 10, 6 * 3600),
        ):
            extra = ", cpu_max, mem_max" if table != "samples" else ""
            rows = []
            t = now - span
            while t < now:
                s = _sample_at(t, rng)
                values = [int(t // step * step), *[s[c] for c in cols[1:]]]
                if extra:
                    values += [min(100.0, s["cpu"] * 1.4), min(100.0, s["mem"] + 4)]
                rows.append(values)
                t += step
            marks = ", ".join("?" * (len(cols) + (2 if extra else 0)))
            db.executemany(f"insert or replace into {table} ({', '.join(cols)}{extra}) values ({marks})", rows)
        for table, step, span in (("apps_1m", 60, 8 * 86400), ("apps_1h", 3600, 31 * 86400)):
            app_rows: list[tuple[int, str, float, float]] = []
            t = now - span
            while t < now:
                hour = time.localtime(t).tm_hour
                work = 1.0 if 9 <= hour <= 19 else 0.3
                for app, weight in APPS:
                    app_rows.append(
                        (
                            int(t // step * step),
                            app,
                            weight * work * step * rng.uniform(0.6, 1.4) * 0.6,
                            weight * 700 * MB * rng.uniform(0.8, 1.2),
                        )
                    )
                t += step
            db.executemany(f"insert or replace into {table} (ts, app, cpu_s, rss) values (?, ?, ?, ?)", app_rows)
    events = [
        (
            "close",
            "low",
            "Saved and closed Claude Code session 'Draft the release notes' (idle 14h), freed about 520 MB",
        ),
        (
            "close",
            "low",
            "Saved and closed Codex session 'Bump the Terraform providers' (idle 1d 2h), freed about 160 MB",
        ),
        ("throttle", "low", "Throttled node (pid 48211): 180% CPU for 42 s while the machine ran at 91%"),
        ("terminate", "high", "Stopped python3 (pid 51877): memory short: 4.1% available, swap 6% free"),
        ("close", "high", "Saved and closed Claude Code session 'Spike: vector search' (pressure), freed about 610 MB"),
        ("judge", "info", "Judge spared cargo (build nearly done): a release build at 96% that ends soon"),
        ("alert", "medium", "vite (pid 50112): rule 'dev servers': 4.4 GB over 4096 MB"),
        ("archive", "info", "Saved Claude Code session 'Plan the Postgres upgrade'"),
        ("config", "info", "Settings changed: agents.idle_hours"),
    ]
    for day in range(14, -1, -1):
        for _ in range(rng.randint(1, 5)):
            kind, sev, title = rng.choice(events)
            ts = now - day * 86400 - rng.uniform(0, 80000)
            if ts < now:
                store.add_event(kind, title, sev, rng.randint(2000, 90000), title.split(" ")[1], {"demo": True}, ts)
    for i, (harness, title, cwd, _, idle, rss) in enumerate(AGENTS * 2):
        sid = f"demo-{harness}-{i:02d}"
        folder = archive_root / harness / sid
        folder.mkdir(parents=True, exist_ok=True)
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(now - (i + 1) * 40000))
        body = TRANSCRIPT.format(title=title, when=when)
        (folder / "transcript.md").write_text(body, encoding="utf-8")
        (folder / "manifest.json").write_text("{}", encoding="utf-8")
        label = BY_KEY[harness].label
        resume = {
            "claude": f"cd {cwd} && claude --resume {sid}",
            "codex": f"cd {cwd} && codex resume {sid}",
            "opencode": f"cd {cwd} && opencode --session {sid}",
            "gemini": f"cd {cwd} && gemini --resume",
        }
        store.add_session(
            {
                "id": f"{harness}:{sid}",
                "harness": harness,
                "session_id": sid,
                "title": title,
                "cwd": cwd.replace("~", "/home/you"),
                "started": now - (i + 1) * 45000,
                "ended": now - (i + 1) * 40000,
                "archived": now - (i + 1) * 40000,
                "reason": "closed" if i % 3 else "backup",
                "size": rng.randint(80, 900) * 1024,
                "path": str(folder),
                "resume": resume[harness],
                "pid": 0,
                "messages": rng.randint(6, 140),
            },
            body + f"\n{label}",
        )
    store.upsert_findings(
        "quick:listeners",
        [
            {
                "id": "net:demo1",
                "severity": "medium",
                "title": "New port open to the network: 5173 (node) on 0.0.0.0",
                "detail": "Dev servers bound to 0.0.0.0 are reachable by everyone on the network.",
            }
        ],
    )
    store.upsert_findings(
        "quick:credentials",
        [
            {
                "id": "creds:demo",
                "severity": "medium",
                "title": "~/.aws/credentials is readable by other users",
                "detail": "Run: chmod 600 ~/.aws/credentials",
            }
        ],
    )
    store.upsert_findings(
        "quick:agents",
        [
            {
                "id": "agent:demo",
                "severity": "info",
                "title": "Claude Code session runs with permission checks off " "(pid 48862)",
                "detail": "Flags: --dangerously-skip-permissions. Fine when you started it.",
            }
        ],
    )
    store.upsert_findings(
        "deep:rkhunter",
        [
            {
                "id": "rk:demo",
                "severity": "low",
                "title": "The file properties have changed:",
                "detail": "File: /usr/bin/curl\nAfter a package update this is expected.",
            }
        ],
    )
    store.upsert_findings(
        "deep:lynis",
        [
            {
                "id": "ly:demo",
                "severity": "info",
                "title": "Consider hardening SSH configuration [SSH-7408]",
                "detail": "AllowTcpForwarding (set YES to NO)",
            }
        ],
    )
    for source, status in (
        ("quick:shell", "ok"),
        ("quick:persistence", "ok"),
        ("quick:processes", "ok"),
        ("quick:listeners", "warnings"),
        ("quick:credentials", "warnings"),
        ("quick:agents", "ok"),
        ("deep:rkhunter", "warnings"),
        ("deep:chkrootkit", "ok"),
        ("deep:clamav", "ok"),
        ("deep:lynis", "warnings"),
    ):
        store.add_scan(source, rng.uniform(0.01, 90), status, {"findings": 1 if status != "ok" else 0})
    store.rollup(now)


class DemoCore(Core):
    """A core whose machine is made up. Actions change the made up state, never a real process."""

    def __init__(self, cfg: config.Config, store: Store) -> None:
        super().__init__(cfg, store)
        self.rng = random.Random(7)  # nosec B311  # made up chart data, not security
        self.closed: set[int] = set()
        self.started = time.time() - 6 * 86400
        self.stats = {"actions": 41, "closed": 23, "freed": int(11.6 * GB)}
        now = time.time()
        self._make_agents(now)
        for t in range(int(now) - 600, int(now), 5):
            self.live.append(self._summary(self._snapshot(float(t)), self.agents))

    def _make_agents(self, now: float) -> None:
        agents = []
        for i, (harness, title, cwd, status, idle, rss) in enumerate(AGENTS):
            pid = 41000 + i * 371
            if pid in self.closed:
                continue
            agents.append(
                Agent(
                    harness=harness,
                    label=BY_KEY[harness].label,
                    pid=pid,
                    pids=[pid],
                    started=now - idle - 3600,
                    cwd=cwd.replace("~", "/home/you"),
                    tty=f"/dev/pts/{i + 2}",
                    rss=rss * MB,
                    cpu=38.0 if status == "busy" else 0.3,
                    session_id=f"live-{harness}-{i}",
                    status=status,
                    last_active=now - idle,
                    activity="transcript",
                    interactive=True,
                    title=title,
                    resume=f"cd {cwd} && "
                    + {
                        "claude": "claude --resume",
                        "codex": "codex resume",
                        "opencode": "opencode --session",
                        "gemini": "gemini --resume",
                    }[harness]
                    + f" live-{harness}-{i}",
                    flags=["--dangerously-skip-permissions"] if i == 1 else [],
                )
            )
        self.agents = agents

    def _snapshot(self, now: float) -> Snapshot:
        s = _sample_at(now, self.rng)
        procs = [
            Proc(
                pid=30000 + i,
                ppid=1,
                name=name,
                uid=os.getuid(),
                started=now - 3600 * (i + 1),
                cpu=max(0.0, cpu + self.rng.gauss(0, cpu * 0.15)),
                rss=int(mem * MB),
                status="running",
                cpu_seconds=0.0,
            )
            for i, (name, cpu, mem) in enumerate(PROCS)
        ]
        return Snapshot(
            ts=now,
            cpu=s["cpu"],
            cores=16,
            mem_total=32 * GB,
            mem_available=int(s["mem_available"]),
            swap_total=8 * GB,
            swap_used=int(s["swap"] / 100 * 8 * GB),
            load=(s["load1"], 4.1, 3.8),
            disk_read=s["disk_read"],
            disk_write=s["disk_write"],
            net_rx=s["net_rx"],
            net_tx=s["net_tx"],
            temp=s["temp"],
            psi={"cpu": s["psi_cpu"], "memory": s["psi_mem"], "memory_full": 0.0, "io": s["psi_io"]},
            disk_percent=61.0,
            uptime=6 * 86400,
            procs=procs,
        )

    def tick(self) -> None:
        """Make up one sample and publish it."""
        now = time.time()
        snap = self._snapshot(now)
        self._make_agents(now)
        with self.lock:
            self.snap = snap
        self.sampler.last_cost = self.rng.uniform(0.011, 0.016)
        summary = self._summary(snap, self.agents)
        self.live.append(summary)
        for fn in list(self.subscribers):
            fn(summary)

    def _fresh(self) -> Snapshot:
        if self.snap is None:
            self.tick()
        assert self.snap is not None  # nosec B101  # set by tick
        return self.snap

    def processes(self, sort: str = "cpu", limit: int = 25) -> list[dict[str, Any]]:
        """
        Return the made up processes.

        :param sort: cpu or mem
        :param limit: How many
        :returns: Process rows
        """
        snap = self._fresh()
        rows = sorted(snap.procs, key=lambda p: p.rss if sort == "mem" else p.cpu, reverse=True)[: int(limit)]
        guard = {"gnome-shell": "protected name", "pipewire": "protected name", "chrome": "in a call"}
        agents = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode"}
        return [
            {
                "pid": p.pid,
                "name": p.name,
                "cpu": round(p.cpu, 1),
                "rss": p.rss,
                "user": True,
                "started": p.started,
                "command": p.name,
                "agent": agents.get(p.name),
                "nice": 0,
                "protected": guard.get(p.name),
            }
            for p in rows
        ]

    def agent_save(self, id: str) -> dict[str, Any]:
        """
        Pretend to save a session.

        :param id: The session id
        :returns: A made up result
        """
        return {
            "ok": True,
            "path": "/home/you/.local/share/huntaway/sessions/demo",
            "files": 2,
            "size": 2_400_000,
            "stored": 410_000,
            "resume": "",
            "id": id,
        }

    def agent_close(self, id: str) -> dict[str, Any]:
        """
        Pretend to save and close a session.

        :param id: The session id
        :returns: A made up result
        :raises ApiError: When the session is not in the demo
        """
        agent = next((a for a in self.agents if id in (a.key(), str(a.pid))), None)
        if agent is None:
            raise ApiError("no such session in the demo", 404)
        self.closed.add(agent.pid)
        self.agents = [a for a in self.agents if a.pid != agent.pid]
        title = f"Saved and closed {agent.label} session '{agent.title}' (manual), freed about {agent.rss // MB} MB"
        self.store.add_event("close", title, "low", agent.pid, agent.label, {"demo": True})
        return {"ok": True, "outcome": "terminated", "title": title, "resume": agent.resume, "rss": agent.rss}

    def agents_reap(self, dry_run: bool = True) -> dict[str, Any]:
        """
        Pretend to close the stale sessions.

        :param dry_run: Only list them
        :returns: A made up result
        """
        listing = self.agents_list()
        stale = [a for a in listing if a["stale"]]
        if dry_run:
            return {"dry_run": True, "sessions": stale, "rss": sum(a["rss"] for a in stale)}
        results = [self.agent_close(a["id"]) for a in stale]
        return {"dry_run": False, "results": results, "rss": sum(a["rss"] for a in stale)}

    def process_action(self, pid: int, action: str) -> dict[str, Any]:
        """
        Pretend to act on a process.

        :param pid: The process id
        :param action: The action
        :returns: A made up result
        """
        return {"ok": True, "outcome": "demo: nothing was touched"}

    def security_scan(self) -> dict[str, Any]:
        """
        Pretend to run the quick checks.

        :returns: Counts per check
        """
        return {"shell": 0, "persistence": 0, "processes": 0, "listeners": 1, "credentials": 1, "agents": 1}

    def security(self) -> dict[str, Any]:
        """
        Return the made up security view.

        :returns: Findings and scanner state
        """
        return {
            "findings": self.store.findings(),
            "scans": self.store.last_scans(),
            "scanners": {"rkhunter": True, "chkrootkit": True, "clamav": True, "lynis": True},
            "install": "",
            "last_deep": time.time() - 2 * 86400,
            "hardening_index": 74,
        }

    def integrations(self) -> dict[str, Any]:
        """
        Return made up integration state.

        :returns: Steps and commands
        """
        steps = [
            {"harness": "claude", "what": "mcp", "target": "/home/you/.claude.json", "done": True, "note": ""},
            {
                "harness": "claude",
                "what": "skill",
                "target": "/home/you/.claude/skills/huntaway/SKILL.md",
                "done": True,
                "note": "",
            },
            {"harness": "codex", "what": "mcp", "target": "/home/you/.codex/config.toml", "done": True, "note": ""},
            {
                "harness": "opencode",
                "what": "mcp",
                "target": "/home/you/.config/opencode/opencode.json",
                "done": False,
                "note": "",
            },
        ]
        base = f"http://127.0.0.1:{self.cfg['server.port']}"
        from huntaway import integrate

        return {
            "steps": steps,
            "command": ["/home/you/.local/bin/huntaway", "mcp"],
            "agents_md": integrate.AGENTS_MD,
            "mcp_http": f"{base}/mcp",
            "metrics": f"{base}/metrics",
        }


def main(port: int = 4869, open_browser: bool = True, host: str = "127.0.0.1") -> int:
    """
    Serve the dashboard on made up data until interrupted.

    :param port: The port
    :param open_browser: Open the dashboard in a browser
    :param host: The address to bind
    :returns: Exit code
    """
    from huntaway.server import serve

    root = Path(tempfile.mkdtemp(prefix="huntaway-demo-"))
    for name, sub in (
        ("HUNTAWAY_CONFIG_DIR", "config"),
        ("HUNTAWAY_DATA_DIR", "data"),
        ("HUNTAWAY_STATE_DIR", "state"),
    ):
        os.environ[name] = str(root / sub)
    values = config.defaults()
    values.update(
        {
            "server.port": port,
            "general.notify": False,
            "agents.reap": False,
            "security.enabled": False,
            "sessions.archive_dir": str(root / "data" / "sessions"),
        }
    )
    cfg = config.Config(values=values, path=root / "config" / "config.toml")
    config.save(cfg)
    store = Store(root / "data" / "huntaway.db")
    now = time.time()
    seed(store, cfg.archive_dir(), now, random.Random(42))  # nosec B311  # made up data
    core = DemoCore(config.load(cfg.path), store)
    try:
        server = serve(core, host, port, token="demo")  # nosec B106  # the demo serves made up data
    except OSError as exc:
        print(f"huntaway demo: cannot listen on {host}:{port}: {exc}")
        return 1
    url = f"http://{host}:{server.server_address[1]}/#token=demo"
    print(f"huntaway demo on {url}\nMade up data in {root}. Nothing on this machine is touched. Ctrl+C stops it.")
    if open_browser:
        webbrowser.open(url)
    try:
        while True:
            core.tick()
            time.sleep(2)
    except KeyboardInterrupt:
        server.shutdown()
    return 0
