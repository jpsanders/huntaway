"""
Run a heavy job inside limits instead of having it stopped.

``huntaway run -- <command>``: run a heavy job inside limits instead of having it stopped.

The command runs in its own systemd scope with a memory ceiling (and a CPU quota when asked), at a lower priority,
and a lease file tells the daemon it is expected heavy work, so CPU rules leave it alone. Memory emergencies still
apply, but the ceiling means the job alone cannot take the machine down.
"""

import json
import os
import shutil
import signal
import subprocess  # nosec B404  # runs the user's own command, never through a shell
import sys
import time
from types import FrameType

import psutil

from huntaway import paths


def parse_size(text: str) -> int:
    """
    Parse a size such as 8G, 512M or 70%.

    :param text: The size
    :returns: Bytes
    :raises ValueError: When it cannot be read
    """
    text = text.strip().upper()
    total = psutil.virtual_memory().total
    if text.endswith("%"):
        return int(total * float(text[:-1]) / 100)
    units = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
    if text[-1:] in units:
        return int(float(text[:-1]) * units[text[-1]])
    return int(text)


def build(cmd: list[str], cpu: str | None, memory: str | None) -> list[str]:
    """
    Return the command line that applies the limits.

    :param cmd: The user's command
    :param cpu: A CPU quota such as 200% (two cores), or None
    :param memory: A memory ceiling such as 8G or 70%, or None
    :returns: The command to start
    """
    if not sys.platform.startswith("linux") or not shutil.which("systemd-run"):
        return cmd
    props = []
    if cpu:
        props += ["-p", f"CPUQuota={cpu if cpu.endswith('%') else cpu + '%'}"]
    if memory:
        props += ["-p", f"MemoryMax={parse_size(memory)}", "-p", "MemorySwapMax=0"]
    if not props:
        return cmd
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect", *props, "--", *cmd]


def run(cmd: list[str], cpu: str | None = None, memory: str | None = "70%", nice: int = 10) -> int:
    """
    Run a command under a lease and limits, and return its exit code.

    :param cmd: The command
    :param cpu: CPU quota, for example 200%
    :param memory: Memory ceiling, for example 8G; None for no ceiling
    :param nice: Priority offset, 0 to 19
    :returns: The command's exit code
    """
    if not cmd:
        print("usage: huntaway run [--cpu 200%] [--memory 8G] -- command [args]", file=sys.stderr)
        return 2
    lease_dir = paths.runtime_dir() / "leases"
    lease_dir.mkdir(parents=True, exist_ok=True)
    lease = lease_dir / f"{os.getpid()}.json"
    lease.write_text(
        json.dumps({"pid": os.getpid(), "cmd": cmd, "started": time.time(), "cpu": cpu, "memory": memory}),
        encoding="utf-8",
    )
    args = build(cmd, cpu, memory)

    def lower() -> None:
        os.nice(max(0, min(19, nice)))

    try:
        child = subprocess.Popen(args, preexec_fn=lower)  # nosec B603  # the user's own command, no shell
    except FileNotFoundError:
        lease.unlink(missing_ok=True)
        print(f"huntaway run: command not found: {cmd[0]}", file=sys.stderr)
        return 127

    def forward(signum: int, frame: FrameType | None) -> None:
        child.send_signal(signum)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, forward)
    try:
        return child.wait()
    finally:
        lease.unlink(missing_ok=True)
