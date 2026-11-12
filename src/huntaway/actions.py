"""
The only code that touches other processes.

Every action checks the process is still the one that was judged (same pid and same start time), so a reused pid is
never hit.
"""

import os
import signal
import sys
import time

import psutil


def same_process(pid: int, started: float) -> psutil.Process | None:
    """
    Return the live process, if it is still the one that started at ``started``.

    :param pid: The process id
    :param started: Its start time, seconds since the epoch
    :returns: The process, or None when it is gone or the pid was reused
    """
    try:
        proc = psutil.Process(pid)
        if started and abs(proc.create_time() - started) > 1.0:
            return None
        return proc
    except psutil.Error, OSError:
        return None


def terminate(pid: int, started: float, grace: float = 5.0) -> str:
    """
    Ask a process to stop, then force it after the grace time.

    :param pid: The process id
    :param started: Its start time
    :param grace: Seconds between SIGTERM and SIGKILL
    :returns: terminated, killed, gone or denied
    """
    proc = same_process(pid, started)
    if proc is None:
        return "gone"
    try:
        proc.terminate()
    except psutil.NoSuchProcess:
        return "gone"
    except psutil.AccessDenied, OSError:
        return "denied"
    try:
        proc.wait(timeout=grace)
        return "terminated"
    except psutil.TimeoutExpired:
        pass
    except psutil.Error, OSError:
        return "terminated"
    return kill(pid, started)


def kill(pid: int, started: float) -> str:
    """
    Force a process to stop at once.

    :param pid: The process id
    :param started: Its start time
    :returns: killed, gone or denied
    """
    proc = same_process(pid, started)
    if proc is None:
        return "gone"
    try:
        proc.kill()
        proc.wait(timeout=3)
    except psutil.NoSuchProcess:
        return "killed"
    except psutil.TimeoutExpired:
        return "killed"
    except psutil.AccessDenied, OSError:
        return "denied"
    return "killed"


def throttle(pid: int, started: float) -> str:
    """
    Drop a process, every thread of it, to the lowest CPU priority, and its disk priority to idle.

    An unprivileged user cannot raise the priority back, so this lasts until the process exits.

    :param pid: The process id
    :param started: Its start time
    :returns: throttled, gone or denied
    """
    proc = same_process(pid, started)
    if proc is None:
        return "gone"
    changed = False
    tids = [pid]
    if sys.platform.startswith("linux"):
        try:
            tids = [int(t) for t in os.listdir(f"/proc/{pid}/task")]
        except OSError:
            tids = [pid]
    for tid in tids:
        try:
            if os.getpriority(os.PRIO_PROCESS, tid) < 19:
                os.setpriority(os.PRIO_PROCESS, tid, 19)
            changed = True
        except OSError:
            continue
    try:
        if hasattr(psutil, "IOPRIO_CLASS_IDLE"):
            proc.ionice(psutil.IOPRIO_CLASS_IDLE)
    except psutil.Error, OSError, ValueError:
        pass
    return "throttled" if changed else "denied"


def niceness(pid: int) -> int | None:
    """
    Return a process's nice value.

    :param pid: The process id
    :returns: -20 to 19, or None when it cannot be read
    """
    try:
        return os.getpriority(os.PRIO_PROCESS, pid)
    except OSError:
        return None


def alive(pid: int, started: float) -> bool:
    """
    Whether the process still runs.

    :param pid: The process id
    :param started: Its start time
    :returns: True while it runs
    """
    proc = same_process(pid, started)
    if proc is None:
        return False
    try:
        return proc.status() != psutil.STATUS_ZOMBIE
    except psutil.Error, OSError:
        return False


def wait_gone(pid: int, started: float, timeout: float) -> bool:
    """
    Wait for a process to exit.

    :param pid: The process id
    :param started: Its start time
    :param timeout: Seconds to wait
    :returns: True when it exited in time
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(pid, started):
            return True
        time.sleep(0.2)
    return not alive(pid, started)


SIGNALS = {"TERM": signal.SIGTERM, "KILL": signal.SIGKILL, "HUP": signal.SIGHUP, "INT": signal.SIGINT}
