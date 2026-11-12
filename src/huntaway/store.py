"""
The database: one SQLite file in WAL mode.

The database: samples at three resolutions, per app usage, events, saved sessions with full text search, security
findings and baselines. One SQLite file in WAL mode, one connection per thread.
"""

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SAMPLE_COLUMNS = (
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
)

SCHEMA = f"""
create table if not exists samples (ts integer primary key, {", ".join(f"{c} real" for c in SAMPLE_COLUMNS)});
create table if not exists samples_1m (ts integer primary key, {", ".join(f"{c} real" for c in SAMPLE_COLUMNS)},
    cpu_max real, mem_max real);
create table if not exists samples_1h (ts integer primary key, {", ".join(f"{c} real" for c in SAMPLE_COLUMNS)},
    cpu_max real, mem_max real);
create table if not exists apps_1m (ts integer, app text, cpu_s real, rss real, primary key (ts, app));
create table if not exists apps_1h (ts integer, app text, cpu_s real, rss real, primary key (ts, app));
create table if not exists events (
    id integer primary key, ts real, kind text, severity text, title text, pid integer, name text, detail text);
create index if not exists events_ts on events (ts);
create table if not exists sessions (
    id text primary key, harness text, session_id text, title text, cwd text, started real, ended real,
    archived real, reason text, size integer, path text, resume text, pid integer, messages integer);
create virtual table if not exists sessions_fts using fts5(id unindexed, title, body, tokenize='porter unicode61');
create table if not exists findings (
    id text primary key, source text, severity text, title text, detail text, first_seen real, last_seen real,
    active integer default 1);
create table if not exists scans (id integer primary key, ts real, source text, duration real, status text,
    summary text);
create table if not exists baselines (name text, key text, value text, primary key (name, key));
create table if not exists backup_index (path text primary key, size integer, mtime real, archived real);
create table if not exists meta (k text primary key, v text);
"""

SEVERITIES = ("info", "low", "medium", "high", "critical")


class Store:
    """Thread safe access to the database. Each thread gets its own connection."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path) if str(path) != ":memory:" else path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._all: list[sqlite3.Connection] = []
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.write() as db:
            db.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("pragma journal_mode=wal")
            conn.execute("pragma synchronous=normal")
            conn.execute("pragma busy_timeout=10000")
            self._local.conn = conn
            with self._write_lock:
                self._all.append(conn)
        return conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """
        Open a connection inside one write transaction, serialised across threads.

        :yields: The connection
        :raises BaseException: Whatever the block raised, after the transaction is rolled back
        """
        conn = self._conn()
        with self._write_lock:
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def query(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> list[dict[str, Any]]:
        """
        Run a read query.

        :param sql: The statement
        :param params: Its parameters
        :returns: Rows as dicts
        """
        return [dict(r) for r in self._conn().execute(sql, params).fetchall()]

    def close(self) -> None:
        """Close every connection this store opened, from any thread."""
        with self._write_lock:
            for conn in self._all:
                conn.close()
            self._all.clear()
        self._local = threading.local()

    # Samples and analytics

    def add_sample(self, row: dict[str, Any]) -> None:
        """
        Store one full resolution sample.

        :param row: ``ts`` plus any of ``SAMPLE_COLUMNS``
        """
        cols = ["ts", *[c for c in SAMPLE_COLUMNS if c in row]]
        with self.write() as db:
            db.execute(
                f"insert or replace into samples ({', '.join(cols)}) values ({', '.join('?' * len(cols))})",
                [int(row["ts"]) if c == "ts" else row[c] for c in cols],
            )

    def add_apps(self, minute: int, usage: dict[str, tuple[float, float]]) -> None:
        """
        Store per app CPU seconds and peak memory for one minute.

        :param minute: The minute's start, seconds since the epoch
        :param usage: App name to (CPU seconds, resident bytes)
        """
        with self.write() as db:
            db.executemany(
                "insert into apps_1m (ts, app, cpu_s, rss) values (?, ?, ?, ?) on conflict (ts, app) do update "
                "set cpu_s = cpu_s + excluded.cpu_s, rss = max(rss, excluded.rss)",
                [(minute, app, cpu, rss) for app, (cpu, rss) in usage.items()],
            )

    def rollup(self, now: float | None = None) -> None:
        """
        Fold raw samples into minutes and minutes into hours, then apply nothing else. Idempotent.

        :param now: The current time
        """
        now = now or time.time()
        avg = ", ".join(f"avg({c})" for c in SAMPLE_COLUMNS)
        cols = ", ".join(SAMPLE_COLUMNS)
        minute_end = int(now // 60 * 60)
        hour_end = int(now // 3600 * 3600)
        with self.write() as db:
            last_m = db.execute("select coalesce(max(ts), 0) from samples_1m").fetchone()[0]
            db.execute(
                f"insert or replace into samples_1m (ts, {cols}, cpu_max, mem_max) select ts / 60 * 60, {avg}, "
                "max(cpu), max(mem) from samples where ts >= ? and ts < ? group by ts / 60",
                (last_m, minute_end),
            )
            last_h = db.execute("select coalesce(max(ts), 0) from samples_1h").fetchone()[0]
            db.execute(
                f"insert or replace into samples_1h (ts, {cols}, cpu_max, mem_max) select ts / 3600 * 3600, {avg}, "
                "max(cpu_max), max(mem_max) from samples_1m where ts >= ? and ts < ? group by ts / 3600",
                (last_h, hour_end),
            )
            last_ah = db.execute("select coalesce(max(ts), 0) from apps_1h").fetchone()[0]
            db.execute(
                "insert or replace into apps_1h (ts, app, cpu_s, rss) select ts / 3600 * 3600, app, sum(cpu_s), "
                "max(rss) from apps_1m where ts >= ? and ts < ? group by ts / 3600, app",
                (last_ah, hour_end),
            )

    def prune(self, raw_hours: int, minute_days: int, hour_days: int, events_days: int) -> None:
        """
        Drop history older than the retention settings.

        :param raw_hours: Keep raw samples this long
        :param minute_days: Keep minute rows this long
        :param hour_days: Keep hour rows this long
        :param events_days: Keep events, scans and inactive findings this long
        """
        now = time.time()
        with self.write() as db:
            db.execute("delete from samples where ts < ?", (now - raw_hours * 3600,))
            db.execute("delete from samples_1m where ts < ?", (now - minute_days * 86400,))
            db.execute("delete from apps_1m where ts < ?", (now - minute_days * 86400,))
            db.execute("delete from samples_1h where ts < ?", (now - hour_days * 86400,))
            db.execute("delete from apps_1h where ts < ?", (now - hour_days * 86400,))
            db.execute("delete from events where ts < ?", (now - events_days * 86400,))
            db.execute("delete from scans where ts < ?", (now - events_days * 86400,))
            db.execute("delete from findings where active = 0 and last_seen < ?", (now - events_days * 86400,))

    def series(self, seconds: int, points: int = 360, now: float | None = None) -> dict[str, Any]:
        """
        Return samples over a time range, bucketed to about ``points`` rows from the best table.

        :param seconds: How far back
        :param points: The rough number of buckets wanted
        :param now: The current time
        :returns: ``{"ts": [...], "<column>": [...], "table": name, "step": seconds}``
        """
        now = now or time.time()
        start = now - seconds
        if seconds <= 6 * 3600:
            table = "samples"
        elif seconds <= 8 * 86400:
            table = "samples_1m"
        else:
            table = "samples_1h"
        step = max({"samples": 1, "samples_1m": 60, "samples_1h": 3600}[table], int(seconds / max(points, 1)))
        cols = ", ".join(f"avg({c}) as {c}" for c in SAMPLE_COLUMNS)
        rows = self.query(
            f"select ts / {step} * {step} as ts, {cols} from {table} where ts >= ? group by ts / {step} order by ts",
            (start,),
        )
        out: dict[str, Any] = {"table": table, "step": step, "ts": [r["ts"] for r in rows]}
        for c in SAMPLE_COLUMNS:
            out[c] = [None if r[c] is None else round(r[c], 2) for r in rows]
        return out

    def top_apps(self, seconds: int, limit: int = 10, now: float | None = None) -> list[dict[str, Any]]:
        """
        Return the apps that used the most CPU and memory over a range.

        :param seconds: How far back
        :param limit: How many apps
        :param now: The current time
        :returns: ``[{"app", "cpu_hours", "rss_peak"}]`` by CPU time
        """
        now = now or time.time()
        table = "apps_1m" if seconds <= 8 * 86400 else "apps_1h"
        return self.query(
            f"select app, round(sum(cpu_s) / 3600.0, 3) as cpu_hours, max(rss) as rss_peak from {table} "
            "where ts >= ? group by app order by sum(cpu_s) desc limit ?",
            (now - seconds, limit),
        )

    def heatmap(self, days: int = 28, now: float | None = None) -> list[list[float | None]]:
        """
        Average CPU by weekday and hour of day, local time.

        :param days: How far back
        :param now: The current time
        :returns: Seven rows (Monday first) of 24 values
        """
        now = now or time.time()
        rows = self.query(
            "select cast(strftime('%w', ts, 'unixepoch', 'localtime') as integer) as dow, "
            "cast(strftime('%H', ts, 'unixepoch', 'localtime') as integer) as hour, avg(cpu) as cpu "
            "from samples_1h where ts >= ? group by dow, hour",
            (now - days * 86400,),
        )
        grid: list[list[float | None]] = [[None] * 24 for _ in range(7)]
        for r in rows:
            grid[(r["dow"] + 6) % 7][r["hour"]] = round(r["cpu"], 1)
        return grid

    # Events

    def add_event(
        self,
        kind: str,
        title: str,
        severity: str = "info",
        pid: int | None = None,
        name: str | None = None,
        detail: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> int:
        """
        Record something huntaway did or saw.

        :param kind: kill, terminate, throttle, close, archive, alert, finding, judge, config or service
        :param title: One line for people
        :param severity: info, low, medium, high or critical
        :param pid: The process involved
        :param name: Its name
        :param detail: Anything else, stored as JSON
        :param ts: When, default now
        :returns: The event id
        """
        with self.write() as db:
            cur = db.execute(
                "insert into events (ts, kind, severity, title, pid, name, detail) values (?, ?, ?, ?, ?, ?, ?)",
                (ts or time.time(), kind, severity, title, pid, name, json.dumps(detail or {}, default=str)),
            )
            return int(cur.lastrowid or 0)

    def events(self, limit: int = 100, kind: str | None = None, since: float = 0.0) -> list[dict[str, Any]]:
        """
        Recent events, newest first.

        :param limit: How many
        :param kind: Only this kind, or several separated by commas
        :param since: Only events after this time
        :returns: Events with ``detail`` decoded
        """
        sql = "select * from events where ts > ?"
        params: list[Any] = [since]
        if kind:
            kinds = [k.strip() for k in kind.split(",") if k.strip()]
            sql += f" and kind in ({', '.join('?' * len(kinds))})"
            params.extend(kinds)
        sql += " order by ts desc, id desc limit ?"
        params.append(limit)
        rows = self.query(sql, tuple(params))
        for r in rows:
            try:
                r["detail"] = json.loads(r["detail"] or "{}")
            except ValueError:
                r["detail"] = {}
        return rows

    def event_counts(self, seconds: int, now: float | None = None) -> list[dict[str, Any]]:
        """
        Actions per day and kind.

        :param seconds: How far back
        :param now: The current time
        :returns: ``[{"day", "kind", "n"}]``
        """
        now = now or time.time()
        return self.query(
            "select date(ts, 'unixepoch', 'localtime') as day, kind, count(*) as n from events where ts >= ? "
            "group by day, kind order by day",
            (now - seconds,),
        )

    # Sessions

    def add_session(self, row: dict[str, Any], body: str) -> None:
        """
        Index a saved session for listing and full text search.

        :param row: The ``sessions`` columns
        :param body: The readable transcript to search
        """
        cols = list(row)
        with self.write() as db:
            db.execute(
                f"insert or replace into sessions ({', '.join(cols)}) values ({', '.join('?' * len(cols))})",
                [row[c] for c in cols],
            )
            db.execute("delete from sessions_fts where id = ?", (row["id"],))
            db.execute(
                "insert into sessions_fts (id, title, body) values (?, ?, ?)",
                (row["id"], row.get("title") or "", body[:4_000_000]),
            )

    def sessions(self, query: str = "", harness: str = "", limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """
        List saved sessions, newest first, or the best matches for a search.

        :param query: Words to search the transcripts for
        :param harness: Only this harness
        :param limit: How many
        :param offset: How many to skip
        :returns: Session rows, with a ``snippet`` when searching
        """
        params: list[Any] = []
        if query.strip():
            sql = (
                "select s.*, snippet(sessions_fts, 2, '[', ']', ' ... ', 16) as snippet from sessions_fts f "
                "join sessions s on s.id = f.id where sessions_fts match ?"
            )
            params.append(_fts_query(query))
            if harness:
                sql += " and s.harness = ?"
                params.append(harness)
            sql += " order by rank limit ? offset ?"
        else:
            sql = "select * from sessions"
            if harness:
                sql += " where harness = ?"
                params.append(harness)
            sql += " order by archived desc limit ? offset ?"
        params.extend([limit, offset])
        try:
            return self.query(sql, tuple(params))
        except sqlite3.OperationalError:
            return []

    def session(self, sid: str) -> dict[str, Any] | None:
        """
        One saved session.

        :param sid: The id, ``harness:session``
        :returns: The row, or None
        """
        rows = self.query("select * from sessions where id = ?", (sid,))
        return rows[0] if rows else None

    def delete_session(self, sid: str) -> None:
        """
        Forget a saved session. The caller removes its files.

        :param sid: The id
        """
        with self.write() as db:
            db.execute("delete from sessions where id = ?", (sid,))
            db.execute("delete from sessions_fts where id = ?", (sid,))

    # Findings and scans

    def upsert_findings(self, source: str, findings: list[dict[str, Any]], now: float | None = None) -> list[str]:
        """
        Replace the active findings of one source. Findings not reported again become inactive.

        :param source: The check or scanner that produced them
        :param findings: Dicts with id, severity, title and detail
        :param now: The current time
        :returns: Ids that are new since the previous report
        """
        now = now or time.time()
        new: list[str] = []
        with self.write() as db:
            known = {r[0]: r[1] for r in db.execute("select id, active from findings where source = ?", (source,))}
            db.execute("update findings set active = 0 where source = ?", (source,))
            for f in findings:
                if f["id"] not in known or not known[f["id"]]:
                    new.append(f["id"])
                db.execute(
                    "insert into findings (id, source, severity, title, detail, first_seen, last_seen, active) "
                    "values (?, ?, ?, ?, ?, ?, ?, 1) on conflict (id) do update set severity = excluded.severity, "
                    "title = excluded.title, detail = excluded.detail, last_seen = excluded.last_seen, active = 1",
                    (f["id"], source, f["severity"], f["title"], f.get("detail", ""), now, now),
                )
        return new

    def findings(self, active: bool = True) -> list[dict[str, Any]]:
        """
        Security findings, worst first.

        :param active: Only findings seen in their source's latest run
        :returns: Finding rows
        """
        order = "case severity " + " ".join(f"when '{s}' then {i}" for i, s in enumerate(SEVERITIES)) + " end"
        sql = "select * from findings" + (" where active = 1" if active else "") + f" order by {order} desc, title"
        return self.query(sql)

    def add_scan(self, source: str, duration: float, status: str, summary: dict[str, Any]) -> None:
        """
        Record one run of a check or scanner.

        :param source: Its name
        :param duration: Seconds it took
        :param status: ok, warnings, failed or missing
        :param summary: Counts and notes
        """
        with self.write() as db:
            db.execute(
                "insert into scans (ts, source, duration, status, summary) values (?, ?, ?, ?, ?)",
                (time.time(), source, duration, status, json.dumps(summary, default=str)),
            )

    def last_scans(self) -> list[dict[str, Any]]:
        """
        Return the latest run of each check and scanner.

        :returns: One row per source
        """
        rows = self.query(
            "select s.* from scans s join (select source, max(ts) as ts from scans group by source) l "
            "on l.source = s.source and l.ts = s.ts order by s.source"
        )
        for r in rows:
            try:
                r["summary"] = json.loads(r["summary"] or "{}")
            except ValueError:
                r["summary"] = {}
        return rows

    # Baselines and small state

    def baseline(self, name: str) -> dict[str, str]:
        """
        Read a stored baseline.

        :param name: The check's name
        :returns: Key to value; empty when never taken
        """
        return {r["key"]: r["value"] for r in self.query("select key, value from baselines where name = ?", (name,))}

    def set_baseline(self, name: str, values: dict[str, str]) -> None:
        """
        Replace a baseline.

        :param name: The check's name
        :param values: Key to value
        """
        with self.write() as db:
            db.execute("delete from baselines where name = ?", (name,))
            db.executemany(
                "insert into baselines (name, key, value) values (?, ?, ?)", [(name, k, v) for k, v in values.items()]
            )

    def get_meta(self, key: str, default: str = "") -> str:
        """
        Read a small state value.

        :param key: The key
        :param default: Returned when unset
        :returns: The value
        """
        rows = self.query("select v from meta where k = ?", (key,))
        return str(rows[0]["v"]) if rows else default

    def set_meta(self, key: str, value: str) -> None:
        """
        Write a small state value.

        :param key: The key
        :param value: The value
        """
        with self.write() as db:
            db.execute("insert or replace into meta (k, v) values (?, ?)", (key, value))


def _fts_query(text: str) -> str:
    # Quote every word, so user input is never read as FTS5 syntax; the last word matches as a prefix.
    words = [w.replace('"', "") for w in text.split() if w.replace('"', "")]
    if not words:
        return '""'
    quoted = [f'"{w}"' for w in words]
    quoted[-1] += "*"
    return " ".join(quoted)
