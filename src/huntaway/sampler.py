"""
One sample of the machine: system totals and every process.

With CPU rates computed from deltas, so a sample never sleeps and costs a few milliseconds.
"""

import os
import time
from dataclasses import dataclass, field
from typing import Any

import psutil

from huntaway import osinfo

PROC_ATTRS = ["ppid", "name", "uids", "create_time", "cpu_times", "memory_info", "status"]

FAST_PROC = os.path.isdir("/proc/self/task") and hasattr(os, "sysconf")
CLK_TCK = os.sysconf("SC_CLK_TCK") if FAST_PROC else 100
PAGE = os.sysconf("SC_PAGE_SIZE") if FAST_PROC else 4096
STATES = {
    b"R": "running",
    b"S": "sleeping",
    b"D": "disk-sleep",
    b"Z": "zombie",
    b"T": "stopped",
    b"t": "tracing-stop",
    b"I": "idle",
    b"X": "dead",
}


@dataclass(slots=True)
class Proc:
    """One process in a sample."""

    pid: int
    ppid: int
    name: str
    uid: int
    started: float
    cpu: float
    rss: int
    status: str
    cpu_seconds: float

    def key(self) -> tuple[int, float]:
        """
        Return a key that stays unique when the kernel reuses a pid.

        :returns: Process id and start time
        """
        return (self.pid, self.started)


@dataclass(slots=True)
class Snapshot:
    """The machine at one moment."""

    ts: float
    cpu: float
    cores: int
    mem_total: int
    mem_available: int
    swap_total: int
    swap_used: int
    load: tuple[float, float, float]
    disk_read: float
    disk_write: float
    net_rx: float
    net_tx: float
    temp: float | None
    psi: dict[str, float]
    disk_percent: float
    uptime: float
    procs: list[Proc] = field(default_factory=list)

    @property
    def mem_used_percent(self) -> float:
        """
        Memory in use, as ``free`` counts it (everything not available).

        :returns: Percent of RAM
        """
        return 100.0 * (1 - self.mem_available / self.mem_total) if self.mem_total else 0.0

    @property
    def mem_available_percent(self) -> float:
        """
        Memory available.

        :returns: Percent of RAM
        """
        return 100.0 * self.mem_available / self.mem_total if self.mem_total else 100.0

    @property
    def swap_free_percent(self) -> float:
        """
        Swap free. A machine without swap has none free.

        :returns: Percent of swap
        """
        return 100.0 * (1 - self.swap_used / self.swap_total) if self.swap_total else 0.0

    def summary(self) -> dict[str, Any]:
        """
        Return the system numbers without the process list, for the API and the database.

        :returns: A JSON ready dict
        """
        return {
            "ts": self.ts,
            "cpu": round(self.cpu, 1),
            "cores": self.cores,
            "mem_total": self.mem_total,
            "mem_available": self.mem_available,
            "mem": round(self.mem_used_percent, 1),
            "swap_total": self.swap_total,
            "swap_used": self.swap_used,
            "swap": round(100.0 * self.swap_used / self.swap_total, 1) if self.swap_total else 0.0,
            "load": [round(x, 2) for x in self.load],
            "disk_read": round(self.disk_read),
            "disk_write": round(self.disk_write),
            "net_rx": round(self.net_rx),
            "net_tx": round(self.net_tx),
            "temp": round(self.temp, 1) if self.temp is not None else None,
            "psi": {k: round(v, 2) for k, v in self.psi.items()},
            "disk": round(self.disk_percent, 1),
            "uptime": round(self.uptime),
            "procs": len(self.procs),
        }


class Sampler:
    """Takes samples. Keeps the previous counters, so rates come from deltas instead of sleeps."""

    def __init__(self, temp_every: float = 30.0, disk_every: float = 60.0) -> None:
        self.cores = psutil.cpu_count() or 1
        self.temp_every = temp_every
        self.disk_every = disk_every
        self._prev_ts = 0.0
        self._prev_cpu: Any = None
        self._prev_disk: tuple[int, int] | None = None
        self._prev_net: tuple[int, int] | None = None
        self._prev_proc: dict[tuple[int, float], float] = {}
        self._temp: tuple[float, float | None] = (0.0, None)
        self._disk: tuple[float, float] = (0.0, 0.0)
        self._boot = osinfo.boot_time()
        self._uids: dict[tuple[int, float], int] = {}
        self.last_cost = 0.0

    def _system_cpu(self) -> float:
        now = psutil.cpu_times()
        prev, self._prev_cpu = self._prev_cpu, now
        if prev is None:
            return 0.0
        busy_fields = ("user", "nice", "system", "irq", "softirq", "steal")
        total = sum(now) - sum(prev)
        idle = (getattr(now, "idle", 0) + getattr(now, "iowait", 0)) - (
            getattr(prev, "idle", 0) + getattr(prev, "iowait", 0)
        )
        if total <= 0:
            return 0.0
        busy = sum(getattr(now, f, 0) - getattr(prev, f, 0) for f in busy_fields)
        return float(max(0.0, min(100.0, 100.0 * max(busy, total - idle) / total)))

    def _io_rates(self, elapsed: float) -> tuple[float, float, float, float]:
        disk_r = disk_w = net_rx = net_tx = 0.0
        try:
            disk = psutil.disk_io_counters()
        except OSError, RuntimeError:
            disk = None
        if disk is not None:
            cur = (disk.read_bytes, disk.write_bytes)
            if self._prev_disk and elapsed > 0:
                disk_r = max(0, cur[0] - self._prev_disk[0]) / elapsed
                disk_w = max(0, cur[1] - self._prev_disk[1]) / elapsed
            self._prev_disk = cur
        try:
            nics = psutil.net_io_counters(pernic=True)
        except OSError, RuntimeError:
            nics = {}
        rx = sum(c.bytes_recv for n, c in nics.items() if n != "lo" and not n.startswith("lo"))
        tx = sum(c.bytes_sent for n, c in nics.items() if n != "lo" and not n.startswith("lo"))
        if self._prev_net and elapsed > 0:
            net_rx = max(0, rx - self._prev_net[0]) / elapsed
            net_tx = max(0, tx - self._prev_net[1]) / elapsed
        self._prev_net = (rx, tx)
        return disk_r, disk_w, net_rx, net_tx

    def _slow(self, now: float) -> tuple[float | None, float]:
        if now - self._temp[0] >= self.temp_every:
            self._temp = (now, osinfo.cpu_temperature())
        if now - self._disk[0] >= self.disk_every:
            try:
                self._disk = (now, psutil.disk_usage(os.path.expanduser("~")).percent)
            except OSError:
                self._disk = (now, 0.0)
        return self._temp[1], self._disk[1]

    def processes(self, elapsed: float) -> list[Proc]:
        """
        Every process, with CPU use since the previous call.

        :param elapsed: Seconds since the previous call
        :returns: The processes that could be read
        """
        if FAST_PROC:
            return self._processes_linux(elapsed)
        return self._processes_psutil(elapsed)

    def _processes_linux(self, elapsed: float) -> list[Proc]:
        # Reading /proc/<pid>/stat directly costs about a fifth of psutil's three files per process.
        procs: list[Proc] = []
        seen: dict[tuple[int, float], float] = {}
        uid_cache = self._uids
        new_uids: dict[tuple[int, float], int] = {}
        boot = self._boot
        for entry in os.scandir("/proc"):
            name = entry.name
            if not name.isdigit():
                continue
            try:
                fd = os.open(f"/proc/{name}/stat", os.O_RDONLY)
                try:
                    raw = os.read(fd, 1024)
                finally:
                    os.close(fd)
            except OSError:
                continue
            end = raw.rfind(b")")
            if end < 0:
                continue
            comm = raw[raw.find(b"(") + 1 : end].decode("utf-8", "replace")
            fields = raw[end + 2 :].split()
            if len(fields) < 22:
                continue
            pid = int(name)
            started = round(boot + int(fields[19]) / CLK_TCK, 2)
            spent = (int(fields[11]) + int(fields[12])) / CLK_TCK
            key = (pid, started)
            uid = uid_cache.get(key)
            if uid is None:
                try:
                    uid = entry.stat(follow_symlinks=False).st_uid
                except OSError:
                    continue
            new_uids[key] = uid
            seen[key] = spent
            prev = self._prev_proc.get(key)
            cpu = 100.0 * (spent - prev) / elapsed if prev is not None and elapsed > 0 else 0.0
            procs.append(
                Proc(
                    pid=pid,
                    ppid=int(fields[1]),
                    name=comm,
                    uid=uid,
                    started=started,
                    cpu=max(0.0, cpu),
                    rss=int(fields[21]) * PAGE,
                    status=STATES.get(fields[0], ""),
                    cpu_seconds=spent,
                )
            )
        self._prev_proc = seen
        self._uids = new_uids
        return procs

    def _processes_psutil(self, elapsed: float) -> list[Proc]:
        procs: list[Proc] = []
        seen: dict[tuple[int, float], float] = {}
        for p in psutil.process_iter(PROC_ATTRS):
            info = p.info
            times = info.get("cpu_times")
            mem = info.get("memory_info")
            started = info.get("create_time") or 0.0
            if times is None or mem is None:
                continue
            spent = float(times.user + times.system)
            key = (p.pid, started)
            seen[key] = spent
            prev = self._prev_proc.get(key)
            cpu = 100.0 * (spent - prev) / elapsed if prev is not None and elapsed > 0 else 0.0
            uids = info.get("uids")
            procs.append(
                Proc(
                    pid=p.pid,
                    ppid=info.get("ppid") or 0,
                    name=info.get("name") or "?",
                    uid=uids.real if uids is not None else -1,
                    started=started,
                    cpu=max(0.0, cpu),
                    rss=int(mem.rss),
                    status=info.get("status") or "",
                    cpu_seconds=spent,
                )
            )
        self._prev_proc = seen
        return procs

    def sample(self) -> Snapshot:
        """
        Take one sample.

        :returns: The snapshot
        """
        start = time.perf_counter()
        now = time.time()
        elapsed = now - self._prev_ts if self._prev_ts else 0.0
        self._prev_ts = now
        vm = psutil.virtual_memory()
        try:
            sw = psutil.swap_memory()
            swap_total, swap_used = int(sw.total), int(sw.used)
        except OSError, RuntimeError:
            swap_total = swap_used = 0
        try:
            load = os.getloadavg()
        except OSError:
            load = (0.0, 0.0, 0.0)
        disk_r, disk_w, net_rx, net_tx = self._io_rates(elapsed)
        temp, disk_percent = self._slow(now)
        snap = Snapshot(
            ts=now,
            cpu=self._system_cpu(),
            cores=self.cores,
            mem_total=int(vm.total),
            mem_available=int(vm.available),
            swap_total=swap_total,
            swap_used=swap_used,
            load=(float(load[0]), float(load[1]), float(load[2])),
            disk_read=disk_r,
            disk_write=disk_w,
            net_rx=net_rx,
            net_tx=net_tx,
            temp=temp,
            psi=osinfo.psi(),
            disk_percent=disk_percent,
            uptime=now - self._boot if self._boot else 0.0,
            procs=self.processes(elapsed),
        )
        self.last_cost = time.perf_counter() - start
        return snap


def tree(procs: list[Proc]) -> dict[int, list[int]]:
    """
    Children by parent pid.

    :param procs: A process list
    :returns: Parent pid to child pids
    """
    kids: dict[int, list[int]] = {}
    for p in procs:
        kids.setdefault(p.ppid, []).append(p.pid)
    return kids


def descendants(pid: int, kids: dict[int, list[int]]) -> set[int]:
    """
    Every process below a pid.

    :param pid: The root
    :param kids: From ``tree``
    :returns: The descendant pids, without the root
    """
    out: set[int] = set()
    stack = list(kids.get(pid, ()))
    while stack:
        child = stack.pop()
        if child in out:
            continue
        out.add(child)
        stack.extend(kids.get(child, ()))
    return out


def ancestors(pid: int) -> set[int]:
    """
    Return the chain of parents of a live process.

    :param pid: The process
    :returns: Parent pids up to init
    """
    out: set[int] = set()
    try:
        proc: psutil.Process | None = psutil.Process(pid).parent()
        while proc is not None and proc.pid not in out and proc.pid > 0:
            out.add(proc.pid)
            proc = proc.parent()
    except psutil.Error, OSError:
        pass
    return out
