"""
The huntaway command.

The ``huntaway`` command. Every read command takes ``--json``; every command works with or without the service
running. Exit codes: 0 done, 1 failed, 2 bad usage, 3 the service is not running, 4 serious security findings.
"""

import argparse
import json
import os
import shutil
import subprocess  # nosec B404  # fixed commands only, never a shell
import sys
import time
import webbrowser
from collections.abc import Callable, Sequence
from importlib import resources
from typing import Any

from huntaway import __version__, config, paths
from huntaway.agents.reaper import human, size
from huntaway.core import ApiError

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
TAN, DIM, BOLD, RED, GREEN, YELLOW, RESET = (
    ("\033[38;5;179m", "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[33m", "\033[0m") if USE_COLOR else ("",) * 7
)
SEV_COLOR = {"critical": RED, "high": RED, "medium": YELLOW, "low": TAN, "info": DIM}


def out(data: Any, as_json: bool, human_fn: Callable[[Any], None]) -> None:
    """
    Print a result as JSON or for people.

    :param data: The result
    :param as_json: Print JSON
    :param human_fn: Prints it for people
    """
    if as_json:
        print(json.dumps(data, indent=2, default=str))
    else:
        human_fn(data)


def table(rows: list[list[str]], head: list[str]) -> None:
    """
    Print aligned columns.

    :param rows: Cells
    :param head: Column titles
    """
    widths = [max(len(_plain(str(r[i]))) for r in [head, *rows]) for i in range(len(head))]
    print(DIM + "  ".join(h.ljust(w) for h, w in zip(head, widths)).rstrip() + RESET)
    for r in rows:
        print("  ".join(str(c) + " " * (w - len(_plain(str(c)))) for c, w in zip(r, widths)).rstrip())


def _plain(text: str) -> str:
    import re

    return re.sub(r"\033\[[0-9;]*m", "", text)


def _bar(percent: float, width: int = 20) -> str:
    filled = int(round(max(0.0, min(100.0, percent)) / 100 * width))
    color = RED if percent >= 90 else YELLOW if percent >= 75 else GREEN
    return color + "█" * filled + RESET + DIM + "░" * (width - filled) + RESET


def _client(args: argparse.Namespace) -> Any:
    from huntaway.client import Client

    return Client(local_only=getattr(args, "local", False))


def cmd_status(args: argparse.Namespace) -> int:
    """
    Print the overview.

    :param args: Parsed arguments
    :returns: Exit code
    """
    client = _client(args)
    data = client.call("status")
    data["service_running"] = client.remote()

    def show(d: dict[str, Any]) -> None:
        s = d["system"]
        state = (
            f"{GREEN}running{RESET}"
            if d["service_running"]
            else f"{YELLOW}not running{RESET} (huntaway service install)"
        )
        print(f"{TAN}{BOLD}huntaway{RESET} {d['version']}  service {state}  mode {d['mode']}  memory {d['level']}")
        print()
        print(f"  CPU     {_bar(s['cpu'])} {s['cpu']:5.1f}%   load {s['load'][0]:.2f} on {s['cores']} cores")
        mem_used = s["mem_total"] - s["mem_available"]
        print(f"  Memory  {_bar(s['mem'])} {s['mem']:5.1f}%   {size(mem_used)} of {size(s['mem_total'])}")
        if s["swap_total"]:
            print(f"  Swap    {_bar(s['swap'])} {s['swap']:5.1f}%   {size(s['swap_used'])} of {size(s['swap_total'])}")
        if s.get("psi"):
            p = s["psi"]
            print(
                f"  Stalls  cpu {p.get('cpu', 0):.1f}%  memory {p.get('memory', 0):.1f}%  io {p.get('io', 0):.1f}%"
                f"  {DIM}(last 10 s){RESET}"
            )
        if s.get("temp") is not None:
            print(f"  Temp    {s['temp']:.0f} C")
        print(f"  Disk    {_bar(s['disk'])} {s['disk']:5.1f}%")
        print()
        stale = (
            f"{YELLOW}{s['agents_stale']} stale ({size(s['agents_stale_rss'])}){RESET}"
            if s["agents_stale"]
            else "none stale"
        )
        print(f"  Agents  {s['agents']} sessions holding {size(s['agents_rss'])}, {stale}")
        f = d["findings"]
        bad = f.get("critical", 0) + f.get("high", 0)
        print(
            f"  Security  {RED if bad else GREEN}{bad} serious{RESET}, {f.get('medium', 0)} medium, "
            f"{f.get('low', 0) + f.get('info', 0)} minor"
        )
        svc = d["service"]
        print(
            f"  Service   {size(svc['rss'])} RAM, {svc['cpu_percent']:.2f}% CPU on average, "
            f"{svc['sample_ms']} ms per sample"
        )
        if d["events"]:
            print(f"\n{DIM}Recent{RESET}")
            for e in d["events"][:5]:
                print(f"  {time.strftime('%d %b %H:%M', time.localtime(e['ts']))}  {e['title']}")

    out(data, args.json, show)
    return 0


def cmd_top(args: argparse.Namespace) -> int:
    """
    Print the busiest processes.

    :param args: Parsed arguments
    :returns: Exit code
    """
    rows = _client(args).call("processes", sort="mem" if args.mem else "cpu", limit=args.limit)

    def show(data: list[dict[str, Any]]) -> None:
        table(
            [
                [
                    str(p["pid"]),
                    p["name"][:28],
                    f"{p['cpu']:.1f}",
                    size(p["rss"]),
                    p.get("agent") or "",
                    DIM + (p["protected"] or "") + RESET,
                ]
                for p in data
            ],
            ["PID", "NAME", "CPU%", "MEMORY", "AGENT", "PROTECTED"],
        )

    out(rows, args.json, show)
    return 0


def cmd_agents(args: argparse.Namespace) -> int:
    """
    List agent sessions, or act on one.

    :param args: Parsed arguments
    :returns: Exit code
    """
    client = _client(args)
    action = args.action
    if action == "list":
        rows = client.call("agents_list")
        if not args.all:
            rows = [r for r in rows if r["interactive"]]

        def show(data: list[dict[str, Any]]) -> None:
            if not data:
                print("No interactive agent sessions running.")
                return
            table(
                [
                    [
                        r["label"],
                        str(r["pid"]),
                        (r["title"] or r["cwd"])[:44],
                        {"busy": "working", "unknown": ""}.get(r["status"], r["status"]),
                        human(r["idle"]),
                        size(r["rss"]),
                        (YELLOW + "stale" + RESET) if r["stale"] else DIM + r["why"][:28] + RESET,
                    ]
                    for r in data
                ],
                ["HARNESS", "PID", "SESSION", "STATUS", "IDLE", "MEMORY", "VERDICT"],
            )
            stale = [r for r in data if r["stale"]]
            total = sum(r["rss"] for r in data)
            print(f"\n{len(data)} sessions, {size(total)}. {len(stale)} stale, {size(sum(r['rss'] for r in stale))}.")
            if stale:
                print(f"{DIM}Close the stale ones (each is saved first): huntaway agents reap --yes{RESET}")

        out(rows, args.json, show)
        return 0
    if action == "reap":
        result = client.call("agents_reap", dry_run=not args.yes)
        if args.json:
            out(result, True, print)
        elif result["dry_run"]:
            for s in result["sessions"]:
                print(
                    f"would close  {s['label']:12} {s['pid']:>8}  idle {human(s['idle']):>7}  {size(s['rss']):>7}  "
                    f"{(s['title'] or s['cwd'])[:50]}"
                )
            print(
                f"\n{len(result['sessions'])} stale sessions, {size(result['rss'])}. Add --yes to save and close them."
            )
        else:
            for r in result["results"]:
                print(r.get("title") or r)
        return 0
    if not args.id:
        print(f"huntaway agents {action} needs a session id or pid", file=sys.stderr)
        return 2
    if action == "save":
        result = client.call("agent_save", id=args.id)
        out(result, args.json, lambda r: print(f"Saved to {r['path']}\nResume: {r['resume'] or 'n/a'}"))
    elif action == "close":
        if not args.yes and sys.stdin.isatty():
            if input(f"Save and close session {args.id}? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1
        result = client.call("agent_close", id=args.id)
        out(result, args.json, lambda r: print(r.get("title") or r))
    elif action in ("pin", "unpin"):
        result = client.call("agent_pin", id=args.id, pinned=action == "pin")
        out(result, args.json, lambda r: print("Pinned: " + (", ".join(r["pinned"]) or "none")))
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    """
    List, search, show, restore or delete saved sessions.

    :param args: Parsed arguments
    :returns: Exit code
    """
    client = _client(args)
    words = args.words or []
    verb = words[0] if words and words[0] in ("show", "restore", "delete", "backup") else "search"
    if verb == "backup":
        from huntaway.agents import archive
        from huntaway.store import Store

        cfg = config.load()
        n = archive.backup_pass(cfg.archive_dir(), Store(paths.db_file()), budget=3600)
        print(f"Saved {n} transcripts.")
        return 0
    if verb in ("show", "restore", "delete"):
        if len(words) < 2:
            print(f"huntaway sessions {verb} needs a session id", file=sys.stderr)
            return 2
        sid = words[1]
        if verb == "show":
            data = client.call("session_read", id=sid, length=10_000_000)
            if args.json:
                out(data, True, print)
            else:
                print(data["text"])
                m = data["session"]
                print(f"\n{DIM}Saved in {m['path']}{RESET}")
                if m.get("resume"):
                    print(f"{DIM}Resume: {m['resume']}{RESET}")
            return 0
        if verb == "restore":
            data = client.call("session_restore", id=sid, force=args.force)
            out(
                data,
                args.json,
                lambda d: print("\n".join(d["report"]) + (f"\nResume: {d['resume']}" if d.get("resume") else "")),
            )
            return 0
        data = client.call("session_delete", id=sid)
        out(data, args.json, lambda d: print("Deleted."))
        return 0
    rows = client.call("sessions", query=" ".join(words), harness=args.harness or "", limit=args.limit)

    def show(data: list[dict[str, Any]]) -> None:
        if not data:
            print("No saved sessions match." if words else "No saved sessions yet.")
            return
        for r in data:
            when = time.strftime("%d %b %Y %H:%M", time.localtime(r["archived"]))
            print(f"{TAN}{r['id']}{RESET}  {DIM}{when}  {r['reason']}  {size(r['size'] or 0)}{RESET}")
            print(f"  {r['title'] or r['cwd']}")
            if r.get("snippet"):
                print(f"  {DIM}{' '.join(r['snippet'].split())[:150]}{RESET}")

    out(rows, args.json, show)
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    """
    Print what huntaway did, or follow it.

    :param args: Parsed arguments
    :returns: Exit code
    """
    client = _client(args)
    since = 0.0
    while True:
        rows = client.call("events", limit=args.limit, kind=args.kind or "", since=since)
        if args.json and not args.follow:
            out(rows, True, print)
            return 0
        for e in reversed(rows):
            color = SEV_COLOR.get(e["severity"], "")
            stamp = time.strftime("%d %b %H:%M:%S", time.localtime(e["ts"]))
            print(f"{DIM}{stamp}{RESET} {color}{e['kind']:9}{RESET} {e['title']}")
            since = max(since, e["ts"])
        if not args.follow:
            return 0
        time.sleep(2)


def cmd_why(args: argparse.Namespace) -> int:
    """
    Explain what huntaway did to a process, by pid or name.

    :param args: Parsed arguments
    :returns: Exit code
    """
    rows = _client(args).call("events", limit=1000, kind="kill,terminate,throttle,close,alert,judge")
    target = args.target.lower()
    hits = [
        e
        for e in rows
        if str(e.get("pid")) == target or target in str(e.get("name") or "").lower() or target in e["title"].lower()
    ]
    if args.json:
        out(hits, True, print)
        return 0
    if not hits:
        print(
            f"huntaway has not acted on anything matching {args.target!r}. If it died with exit code 137, look at"
            " the kernel log: journalctl -k | grep -i oom"
        )
        return 1
    for e in hits[:20]:
        print(f"{time.strftime('%d %b %H:%M:%S', time.localtime(e['ts']))}  {e['title']}")
        detail = e.get("detail") or {}
        if detail.get("judge"):
            print(f"  judge: {detail['judge']}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    """
    Run the quick checks or the deep scan, set the scanners up, or schedule them.

    :param args: Parsed arguments
    :returns: Exit code; 4 when serious findings exist
    """
    from huntaway.security import scanners

    if args.action == "setup":
        have = scanners.available()
        missing = [k for k, v in have.items() if not v]
        for name, ok in have.items():
            print(f"  {name:11} {GREEN + 'installed' + RESET if ok else YELLOW + 'missing' + RESET}")
        command = scanners.install_command(missing)
        if command:
            print(f"\nInstall the missing ones with:\n  {command}")
        return 0
    if args.action == "schedule":
        from huntaway.service import executable

        try:
            files = scanners.install_timer(executable(), args.calendar)
        except PermissionError as exc:
            print(exc, file=sys.stderr)
            return 1
        print("Weekly deep scan scheduled:\n  " + "\n  ".join(files))
        return 0
    if args.deep:
        if os.geteuid() != 0:
            print(
                "The deep scan needs root for rkhunter, chkrootkit and Lynis: sudo huntaway scan --deep",
                file=sys.stderr,
            )
            return 1
        only = args.only.split(",") if args.only else None
        cfg = config.load()
        report = scanners.deep_scan(only, args.refresh_baseline, cfg["security.clamav_paths"])
        if args.json:
            out(report, True, print)
        else:
            for r in report["results"]:
                bad = [f for f in r["findings"] if f["severity"] in ("high", "critical")]
                print(
                    f"{r['source']:11} {r['status']:9} {len(r['findings']):3} findings, {len(bad)} serious, "
                    f"{r['duration']:.0f} s"
                )
            print(f"\nReport: {report['path']}. The service picks it up within five minutes.")
        serious = any(f["severity"] in ("high", "critical") for r in report["results"] for f in r["findings"])
        return 4 if serious else 0
    client = _client(args)
    client.call("security_scan")
    data = client.call("security")

    def show(d: dict[str, Any]) -> None:
        if not d["findings"]:
            print(f"{GREEN}No findings.{RESET}")
        for f in d["findings"]:
            color = SEV_COLOR.get(f["severity"], "")
            print(f"{color}{f['severity']:8}{RESET} {f['title']}")
            if f.get("detail") and args.verbose:
                print(f"         {DIM}{f['detail'][:300]}{RESET}")
        missing = [k for k, v in d["scanners"].items() if not v]
        if missing:
            print(f"\n{DIM}Deep scanners missing: {', '.join(missing)}. huntaway scan setup{RESET}")
        if not d.get("last_deep"):
            print(f"{DIM}No deep scan yet: sudo huntaway scan --deep{RESET}")

    out(data, args.json, show)
    serious = any(f["severity"] in ("high", "critical") for f in data["findings"])
    return 4 if serious else 0


def cmd_config(args: argparse.Namespace) -> int:
    """
    Show, read, change, edit, check or reset the settings.

    :param args: Parsed arguments
    :returns: Exit code
    """
    path = paths.config_file()
    action = args.action
    if action == "path":
        print(path)
        return 0
    if action == "edit":
        if not path.exists():
            config.save(config.load(), path)
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
        return subprocess.call([*editor.split(), str(path)])  # nosec B603  # the user's own editor
    if action == "reset":
        if path.exists():
            path.replace(path.with_suffix(".toml.old"))
        config.save(config.Config(values=config.defaults(), path=path), path)
        print(f"Defaults written to {path} (the old file is config.toml.old).")
        return 0
    try:
        cfg = config.load(path)
    except config.ConfigError as exc:
        print(f"{RED}invalid:{RESET} {exc}", file=sys.stderr)
        return 1
    if action == "validate":
        print(f"{GREEN}valid{RESET}: {path}" + ("" if path.exists() else " (missing, so all defaults)"))
        return 0
    if action == "get":
        if not args.key or args.key not in config.BY_KEY:
            print(f"unknown option {args.key!r}; huntaway config show lists them", file=sys.stderr)
            return 2
        out(cfg[args.key], args.json, lambda v: print(json.dumps(v) if isinstance(v, list) else v))
        return 0
    if action == "set":
        if not args.key or args.value is None:
            print("usage: huntaway config set KEY VALUE", file=sys.stderr)
            return 2
        value: Any = args.value
        if config.BY_KEY.get(args.key) and config.BY_KEY[args.key].kind == "list" and value.startswith("["):
            value = json.loads(value)
        try:
            new = config.set_value(cfg, args.key, value)
        except config.ConfigError as exc:
            print(exc, file=sys.stderr)
            return 2
        config.save(new, path)
        print(f"{args.key} = {json.dumps(new[args.key])}  {DIM}(the service applies it within seconds){RESET}")
        return 0
    if args.json:
        out(cfg.to_public(), True, print)
        return 0
    for opt in config.SCHEMA:
        value = cfg[opt.key]
        mark = "" if value == opt.default else f"  {TAN}(changed){RESET}"
        print(f"{opt.key:34} {json.dumps(value)}{mark}")
    print(f"\n{DIM}{path}{RESET}")
    return 0


def cmd_service(args: argparse.Namespace) -> int:
    """
    Install, remove, start, stop or inspect the background service.

    :param args: Parsed arguments
    :returns: Exit code; 3 when it is not running
    """
    from huntaway import service

    action = args.action
    if action == "install":
        cfg = config.load()
        if args.observe and cfg["general.mode"] != "observe":
            config.save(config.set_value(cfg, "general.mode", "observe"))
        elif not paths.config_file().exists():
            config.save(cfg)
        try:
            lines = service.install(start=not args.no_start)
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1
        print("\n".join(lines))
        cfg = config.load()
        print(f"\nDashboard: http://{cfg['server.host']}:{cfg['server.port']}  (huntaway ui opens it)")
        print("Connect your agents: huntaway integrate")
        return 0
    if action == "uninstall":
        print("\n".join(service.uninstall()))
        return 0
    if action == "logs":
        print(service.logs(args.lines))
        return 0
    code, text = service.control(action)
    if text:
        print(text)
    if action == "status":
        return 0 if code == 0 else 3
    return 0 if code == 0 else 1


def cmd_daemon(args: argparse.Namespace) -> int:
    """
    Run the service in the foreground. This is what the systemd unit runs.

    :param args: Parsed arguments
    :returns: Exit code
    """
    import signal

    from huntaway.core import Core
    from huntaway.server import load_token, serve

    try:
        cfg = config.load()
    except config.ConfigError as exc:
        print(f"huntaway: {exc}", file=sys.stderr)
        return 1
    if args.observe:
        cfg = config.set_value(cfg, "general.mode", "observe")
    core = Core(cfg)
    server = None
    if cfg["server.enabled"]:
        host = args.host or cfg["server.host"]
        port = args.port if args.port is not None else cfg["server.port"]
        try:
            server = serve(core, host, port, load_token())
            print(f"huntaway {__version__}: dashboard on http://{host}:{server.server_address[1]}", flush=True)
        except OSError as exc:
            print(f"huntaway: cannot listen on {host}:{port}: {exc}", file=sys.stderr)
            return 1

    def stop(signum: int, frame: Any) -> None:
        core.stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    core.run()
    if server is not None:
        server.shutdown()
    core.shutdown()
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    """
    Open the dashboard in a browser.

    :param args: Parsed arguments
    :returns: Exit code
    """
    from huntaway.client import Client

    client = Client()
    if not client.remote():
        print(
            "The service is not running. Start it with huntaway service install, or try huntaway demo.", file=sys.stderr
        )
        return 3
    url = f"{client.base}/#token={client.token()}"
    print(url if args.print else f"Opening {client.base}")
    if not args.print:
        webbrowser.open(url)
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    """
    Print or replace the API token.

    :param args: Parsed arguments
    :returns: Exit code
    """
    from huntaway.server import load_token

    if args.rotate:
        paths.token_file().unlink(missing_ok=True)
        print(load_token())
        print(f"{DIM}Restart the service to use it: huntaway service restart{RESET}", file=sys.stderr)
        return 0
    print(load_token())
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    """
    Serve MCP over stdio, through the service when it runs.

    :param args: Parsed arguments
    :returns: Exit code
    """
    from huntaway.client import Client
    from huntaway.core import set_caller
    from huntaway.mcp import Server, serve_stdio
    from huntaway.sampler import ancestors

    client = Client()
    set_caller({os.getpid()} | ancestors(os.getpid()))

    def invoke(name: str, params: dict[str, Any]) -> Any:
        if name in ("agent_close", "agents_reap"):
            return _local_close(client, name, params)
        return client.call(name, **params)

    serve_stdio(Server(invoke))
    return 0


def _local_close(client: Any, name: str, params: dict[str, Any]) -> Any:
    # The daemon cannot see which session is asking through stdio, so closes run here, where it can.
    from huntaway.core import call

    return call(client.core(), name, params)


def cmd_integrate(args: argparse.Namespace) -> int:
    """
    Connect huntaway to agent harnesses.

    :param args: Parsed arguments
    :returns: Exit code
    """
    from huntaway import integrate

    if args.agents_md:
        print(integrate.AGENTS_MD)
        return 0
    steps = integrate.run(args.harness, dry=args.dry_run)
    if not steps:
        print("No supported harness found (Claude Code, Codex, OpenCode, Gemini CLI, Cursor).")
        return 1
    for step in steps:
        print(step.line())
    if args.dry_run:
        print(f"\n{DIM}Nothing changed. Run again without --dry-run to apply.{RESET}")
    else:
        print(
            f"\n{DIM}Restart open agent sessions to load the MCP server. For other tools: huntaway integrate "
            f"--agents-md prints a paragraph for AGENTS.md or CLAUDE.md.{RESET}"
        )
    return 0 if all(s.done for s in steps) or args.dry_run else 1


def cmd_run(args: argparse.Namespace) -> int:
    """
    Run a heavy command under limits and a lease.

    :param args: Parsed arguments
    :returns: The command's exit code
    """
    from huntaway import runwrap

    cmd = list(args.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    return runwrap.run(cmd, args.cpu, None if args.memory == "none" else args.memory, args.nice)


def cmd_judge(args: argparse.Namespace) -> int:
    """
    Turn the LLM judge on or off, or ask it about a process.

    :param args: Parsed arguments
    :returns: Exit code
    """
    path = paths.config_file()
    cfg = config.load(path)
    if args.action in ("off", "advise", "veto", "on"):
        mode = "veto" if args.action == "on" else args.action
        if mode != "off" and not cfg["judge.model"] and not args.model:
            print("Set a model first, for example: huntaway judge veto --model qwen3:4b", file=sys.stderr)
            return 2
        new = config.set_value(cfg, "judge.mode", mode)
        if args.model:
            new = config.set_value(new, "judge.model", args.model)
        if args.url:
            new = config.set_value(new, "judge.base_url", args.url)
        config.save(new, path)
        print(
            f"judge.mode = {mode}"
            + (f", model {new['judge.model']} at {new['judge.base_url']}" if mode != "off" else "")
        )
        return 0
    if args.action == "ask":
        if args.pid is None:
            print("usage: huntaway judge ask PID", file=sys.stderr)
            return 2
        result = _client(args).call("judge_ask", pid=args.pid)
        out(result, args.json, lambda r: print(f"{r['verdict']}: {r['reason'] or r['error']}  ({r['model']})"))
        return 0
    print(f"judge.mode = {cfg['judge.mode']}, model = {cfg['judge.model'] or '(none)'}, url = {cfg['judge.base_url']}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """
    Check the environment and say what is missing.

    :param args: Parsed arguments
    :returns: Exit code
    """
    from huntaway import integrate, osinfo
    from huntaway.client import Client
    from huntaway.security import scanners

    checks: list[tuple[str, bool, str]] = []
    try:
        cfg = config.load()
        checks.append(("config", True, str(cfg.path)))
    except config.ConfigError as exc:
        checks.append(("config", False, str(exc)))
        cfg = config.Config(values=config.defaults())
    client = Client(cfg)
    checks.append(
        ("service", client.remote(), client.base if client.remote() else "not running: huntaway service " "install")
    )
    checks.append(("python", sys.version_info >= (3, 14), sys.version.split()[0]))
    checks.append(("pressure (PSI)", bool(osinfo.psi()), "/proc/pressure" if osinfo.psi() else "not available"))
    checks.append(("systemd user", bool(shutil.which("systemctl")), "systemctl --user"))
    checks.append(
        (
            "call detection",
            bool(shutil.which("pw-dump")),
            "pw-dump (PipeWire)" if shutil.which("pw-dump") else "pw-dump missing: calls in a browser are not detected",
        )
    )
    checks.append(("notifications", bool(shutil.which("notify-send") or shutil.which("osascript")), "notify-send"))
    checks.append(("tmux scrollback", bool(shutil.which("tmux")), "tmux"))
    try:
        import compression.zstd  # noqa: F401

        checks.append(("zstd", True, "compression.zstd"))
    except ImportError:
        checks.append(("zstd", False, "gzip is used instead"))
    for name, ok in scanners.available().items():
        checks.append((f"scanner {name}", ok, "installed" if ok else "huntaway scan setup"))
    for folder in integrate.claude_dirs():
        checks.append(("claude skill", (folder / "skills" / "huntaway" / "SKILL.md").exists(), str(folder)))
    for label, ok, note in checks:
        print(f"  {GREEN + 'ok ' + RESET if ok else YELLOW + '-- ' + RESET} {label:18} {DIM}{note}{RESET}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """
    Serve the dashboard on made up data, touching nothing on this machine.

    :param args: Parsed arguments
    :returns: Exit code
    """
    from huntaway import demo

    return int(demo.main(args.port, not args.no_open, args.host))


def cmd_guide(args: argparse.Namespace) -> int:
    """
    Print the full guide, written for people and agents alike.

    :param args: Parsed arguments
    :returns: Exit code
    """
    print((resources.files("huntaway") / "data" / "MANUAL.md").read_text(encoding="utf-8"))
    return 0


def cmd_man(args: argparse.Namespace) -> int:
    """
    Show the man page.

    :param args: Parsed arguments
    :returns: Exit code
    """
    page = resources.files("huntaway") / "data" / "huntaway.1"
    if shutil.which("man") and sys.stdout.isatty():
        with resources.as_file(page) as path:
            return subprocess.call(["man", "-l", str(path)])  # nosec B603 B607  # man, fixed arguments
    print(page.read_text(encoding="utf-8"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """
    Return the argument parser.

    :returns: The parser
    """
    parser = argparse.ArgumentParser(
        prog="huntaway",
        description="The sheepdog for your AI agents: saves and closes idle agent sessions, stops runaway "
        "processes before the machine freezes, and hunts rootkits.",
        epilog="Run huntaway guide for the full manual, or man huntaway.",
    )
    parser.add_argument("--version", action="version", version=f"huntaway {__version__}")
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    def add(
        name: str, fn: Callable[[argparse.Namespace], int], text: str, json_flag: bool = True, local: bool = True
    ) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=text, description=text)
        p.set_defaults(fn=fn)
        if json_flag:
            p.add_argument("--json", action="store_true", help="print JSON")
        if local:
            p.add_argument("--local", action="store_true", help="work in this process even if the service runs")
        return p

    add("status", cmd_status, "machine health, agents, findings and recent actions")
    p = add("top", cmd_top, "the busiest processes and what protects them")
    p.add_argument("--mem", action="store_true", help="sort by memory")
    p.add_argument("--limit", type=int, default=20)
    p = add("agents", cmd_agents, "agent sessions: list, save, close, reap, pin")
    p.add_argument("action", nargs="?", default="list", choices=["list", "save", "close", "reap", "pin", "unpin"])
    p.add_argument("id", nargs="?", help="session id, harness:session, or pid")
    p.add_argument("--all", action="store_true", help="include sessions without a terminal")
    p.add_argument("--yes", action="store_true", help="do it without asking")
    p = add("sessions", cmd_sessions, "saved sessions: search WORDS, show ID, restore ID, delete ID, backup")
    p.add_argument("words", nargs="*", help="search words, or show|restore|delete ID, or backup")
    p.add_argument("--harness", help="only claude, codex, opencode, ...")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--force", action="store_true", help="restore over existing files")
    p = add("events", cmd_events, "what huntaway did and why")
    p.add_argument("--kind", help="close, terminate, kill, throttle, alert, archive, judge, config, service")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("-f", "--follow", action="store_true", help="keep printing new events")
    p = add("why", cmd_why, "explain what happened to a process, by pid or name")
    p.add_argument("target")
    p = add("scan", cmd_scan, "quick security checks, or --deep with rkhunter, chkrootkit, ClamAV and Lynis")
    p.add_argument("action", nargs="?", default="run", choices=["run", "setup", "schedule"])
    p.add_argument("--deep", action="store_true", help="run the deep scanners (needs sudo)")
    p.add_argument("--only", help="comma separated: rkhunter,chkrootkit,clamav,lynis")
    p.add_argument("--refresh-baseline", action="store_true", help="refresh rkhunter's file baseline first")
    p.add_argument("--calendar", default="Sun 03:00", help="when the weekly scan runs (systemd OnCalendar)")
    p.add_argument("-v", "--verbose", action="store_true", help="show finding details")
    p = add("config", cmd_config, "show, get, set, edit, validate or reset the settings", local=False)
    p.add_argument(
        "action", nargs="?", default="show", choices=["show", "get", "set", "edit", "path", "validate", "reset"]
    )
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p = add(
        "service", cmd_service, "install, uninstall, start, stop, restart, status or logs", json_flag=False, local=False
    )
    p.add_argument("action", choices=["install", "uninstall", "start", "stop", "restart", "status", "logs"])
    p.add_argument("--observe", action="store_true", help="install in observe mode: record, never act")
    p.add_argument("--no-start", action="store_true")
    p.add_argument("--lines", type=int, default=50)
    p = add("daemon", cmd_daemon, "run the service in the foreground", json_flag=False, local=False)
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--observe", action="store_true")
    p = add("ui", cmd_ui, "open the dashboard", json_flag=False, local=False)
    p.add_argument("--print", action="store_true", help="print the URL with its token instead of opening it")
    p = add("token", cmd_token, "print the API token", json_flag=False, local=False)
    p.add_argument("--rotate", action="store_true", help="make a new one")
    add("mcp", cmd_mcp, "serve the MCP server over stdio, for agent harnesses", json_flag=False, local=False)
    p = add(
        "integrate",
        cmd_integrate,
        "add the MCP server and skill to Claude Code, Codex, OpenCode, Gemini, Cursor",
        json_flag=False,
        local=False,
    )
    p.add_argument("harness", nargs="*", help="claude, codex, opencode, gemini, cursor or all (default)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--agents-md", action="store_true", help="print a paragraph for AGENTS.md or CLAUDE.md")
    p = add("run", cmd_run, "run a heavy command capped instead of stopped", json_flag=False, local=False)
    p.add_argument("--cpu", help="CPU quota, for example 200%% (two cores)")
    p.add_argument("--memory", default="70%", help="memory ceiling: 8G, 70%% or none (default 70%%)")
    p.add_argument("--nice", type=int, default=10)
    p.add_argument("command", nargs=argparse.REMAINDER)
    p = add("judge", cmd_judge, "the optional LLM judge: off, advise, veto, ask PID", local=False)
    p.add_argument("action", nargs="?", default="show", choices=["show", "off", "on", "advise", "veto", "ask"])
    p.add_argument("pid", nargs="?", type=int)
    p.add_argument("--model")
    p.add_argument("--url")
    add("doctor", cmd_doctor, "check this machine and say what is missing", json_flag=False, local=False)
    p = add("demo", cmd_demo, "the dashboard on made up data", json_flag=False, local=False)
    p.add_argument("--port", type=int, default=4869)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--no-open", action="store_true")
    add("guide", cmd_guide, "print the full guide (for people and agents)", json_flag=False, local=False)
    add("man", cmd_man, "show the man page", json_flag=False, local=False)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """
    Run the command line.

    :param argv: Arguments, default ``sys.argv[1:]``
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        args = parser.parse_args(["status", *(argv or [])])
    try:
        code = args.fn(args)
    except ApiError as exc:
        print(f"huntaway: {exc}", file=sys.stderr)
        code = 1
    except KeyboardInterrupt:
        code = 130
    except BrokenPipeError:
        code = 0
    sys.exit(code)
