"""
Save an agent session before anything closes it.

The raw transcript files, compressed and checked byte for byte, a readable Markdown copy, the terminal scrollback
when it runs in tmux, and the command that resumes it.
"""

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess  # nosec B404  # fixed commands only, never a shell
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from huntaway.agents import render
from huntaway.agents.harnesses import BY_KEY, Agent, first_prompt_claude, first_prompt_codex, opencode_sessions
from huntaway.store import Store

try:
    from compression import zstd as _zstd
except ImportError:  # pragma: no cover - Python built without zstd
    _zstd = None  # type: ignore[assignment]

CHUNK = 1 << 20
MAX_FILES = 5000


@dataclass(slots=True)
class Saved:
    """The outcome of saving one session."""

    ok: bool
    path: Path
    files: int = 0
    size: int = 0
    stored: int = 0
    error: str = ""
    session: dict[str, Any] = field(default_factory=dict)


def _suffix(compress: bool) -> str:
    if not compress:
        return ""
    return ".zst" if _zstd is not None else ".gz"


def _open_out(path: Path, codec: str) -> io.BufferedIOBase:
    if codec == ".zst" and _zstd is not None:
        return _zstd.open(path, "wb")
    if codec == ".gz":
        return gzip.open(path, "wb", compresslevel=6)
    return open(path, "wb")


def open_stored(path: Path, codec: str | None = None) -> io.BufferedIOBase:
    """
    Open a saved file for reading, whatever it was compressed with.

    :param path: The stored file
    :param codec: .zst, .gz or empty; taken from the file name when None
    :returns: A binary reader
    """
    codec = path.suffix if codec is None else codec
    if codec == ".zst" and _zstd is not None:
        return _zstd.open(path, "rb")
    if codec == ".gz":
        return gzip.open(path, "rb")
    return open(path, "rb")


def _store_file(src: Path, dest: Path) -> tuple[int, str]:
    """
    Copy one file, compressed, and prove the copy decompresses to the bytes read.

    :param src: The source file
    :param dest: Where to write it
    :returns: Bytes read and their sha256
    :raises OSError: When the copy cannot be written or does not match
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    codec = dest.suffix if dest.suffix in (".zst", ".gz") else ""
    digest = hashlib.sha256()
    size = 0
    with open(src, "rb") as reader, _open_out(tmp, codec) as writer:
        while chunk := reader.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
            writer.write(chunk)
    check = hashlib.sha256()
    with open_stored(tmp, codec) as back:
        while chunk := back.read(CHUNK):
            check.update(chunk)
    if check.hexdigest() != digest.hexdigest():
        tmp.unlink(missing_ok=True)
        raise OSError(f"the saved copy of {src} does not match it")
    os.replace(tmp, dest)
    return size, digest.hexdigest()


def _relative(path: Path, base: str | None) -> str:
    if base:
        try:
            return str(path.relative_to(base))
        except ValueError:
            pass
    return "root" + str(path)


def _walk(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for item in paths:
        p = Path(item)
        if p.is_dir():
            out.extend(sorted(x for x in p.rglob("*") if x.is_file()))
        elif p.is_file():
            out.append(p)
        if len(out) >= MAX_FILES:
            break
    return out[:MAX_FILES]


def _tmux_scrollback(pane: str) -> str:
    tool = shutil.which("tmux")
    if not tool or not re.fullmatch(r"%\d+", pane):
        return ""
    try:
        result = subprocess.run(  # nosec B603  # fixed command, pane id validated
            [tool, "capture-pane", "-p", "-J", "-S", "-", "-E", "-", "-t", pane],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except OSError, subprocess.SubprocessError:
        return ""
    return result.stdout if result.returncode == 0 else ""


def _opencode_export(agent: Agent, raw: Path, compress: bool) -> list[tuple[str, Path, int, str]]:
    tool = shutil.which("opencode") or str(Path.home() / ".opencode" / "bin" / "opencode")
    sessions = [s for s in opencode_sessions(agent.cwd) if s["updated"] >= agent.started - 60]
    if not sessions:
        sessions = opencode_sessions(agent.cwd, limit=1)
    saved: list[tuple[str, Path, int, str]] = []
    for s in sessions[:20]:
        sid = s["id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,80}", sid):
            continue
        try:
            result = subprocess.run(  # nosec B603  # fixed command, id validated
                [tool, "session", "export", sid], cwd=agent.cwd or None, capture_output=True, timeout=120
            )
        except OSError, subprocess.SubprocessError:
            continue
        if result.returncode != 0 or not result.stdout.strip():
            continue
        tmp = raw / f"opencode-{sid}.json.src"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(result.stdout)
        dest = raw / f"opencode-{sid}.json{_suffix(compress)}"
        size, digest = _store_file(tmp, dest)
        tmp.unlink(missing_ok=True)
        saved.append((sid, dest, size, digest))
        if not agent.session_id:
            agent.session_id, agent.title = sid, agent.title or s["title"]
    if agent.session_id:
        agent.resume = f"cd {agent.cwd} && opencode --session {agent.session_id}"
    return saved


def session_dir(root: Path, harness: str, sid: str) -> Path:
    """
    Return the folder a session is saved in.

    :param root: The archive root
    :param harness: The harness key
    :param sid: The session id, or pid-<n>
    :returns: ``root/harness/sid`` with unsafe characters replaced
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", sid)[:120] or "unknown"
    return root / re.sub(r"[^a-z0-9_-]", "_", harness) / safe


def save(agent: Agent, root: Path, store: Store | None, reason: str, compress: bool = True) -> Saved:
    """
    Save one session to the archive and index it for search.

    :param agent: The session
    :param root: The archive root
    :param store: The database to index it in, or None
    :param reason: Why it was saved: closed, manual, backup or pressure
    :param compress: Compress the raw files
    :returns: The outcome; ``ok`` is False when anything could not be saved and checked
    """
    now = time.time()
    raw_files: list[dict[str, Any]] = []
    dest = session_dir(root, agent.harness, agent.session_id or f"pid-{agent.pid}-{int(agent.started)}")
    raw = dest / "raw"
    total = 0
    try:
        dest.mkdir(parents=True, exist_ok=True)
        if agent.harness == "opencode":
            for sid, path, size, digest in _opencode_export(agent, raw, compress):
                raw_files.append(
                    {"source": f"opencode:{sid}", "stored": str(path.relative_to(dest)), "size": size, "sha256": digest}
                )
                total += size
            dest_new = session_dir(root, agent.harness, agent.session_id or f"pid-{agent.pid}-{int(agent.started)}")
            if dest_new != dest and raw_files:
                dest_new.parent.mkdir(parents=True, exist_ok=True)
                if not dest_new.exists():
                    os.replace(dest, dest_new)
                    dest, raw = dest_new, dest_new / "raw"
        for src in _walk(agent.files):
            rel = _relative(src, agent.config_dir)
            target = raw / (rel + _suffix(compress))
            size, digest = _store_file(src, target)
            raw_files.append(
                {"source": str(src), "stored": str(target.relative_to(dest)), "size": size, "sha256": digest}
            )
            total += size
        screen = _tmux_scrollback(agent.tmux_pane) if agent.tmux_pane else ""
        if screen.strip():
            (dest / "screen.txt").write_text(screen, encoding="utf-8")
        body = _markdown(agent, dest, raw_files)
        (dest / "transcript.md").write_text(body, encoding="utf-8")
    except OSError as exc:
        return Saved(ok=False, path=dest, error=str(exc))
    if not raw_files and not screen.strip():
        return Saved(ok=False, path=dest, error="nothing to save: no transcript was found for this session")
    label = BY_KEY[agent.harness].label if agent.harness in BY_KEY else agent.harness
    manifest = {
        "format": 1,
        "id": agent.key(),
        "harness": agent.harness,
        "label": label,
        "session_id": agent.session_id,
        "title": agent.title,
        "cwd": agent.cwd,
        "pid": agent.pid,
        "started": agent.started,
        "last_active": agent.last_active,
        "archived": now,
        "reason": reason,
        "resume": agent.resume,
        "config_dir": agent.config_dir,
        "files": raw_files,
        "screen": bool(screen.strip()),
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (dest / "RESUME.txt").write_text(_resume_note(manifest), encoding="utf-8")
    stored = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
    row = {
        "id": agent.key(),
        "harness": agent.harness,
        "session_id": agent.session_id,
        "title": agent.title,
        "cwd": agent.cwd,
        "started": agent.started,
        "ended": now if reason in ("closed", "pressure") else None,
        "archived": now,
        "reason": reason,
        "size": stored,
        "path": str(dest),
        "resume": agent.resume,
        "pid": agent.pid,
        "messages": body.count("\n### "),
    }
    if store is not None:
        store.add_session(row, body)
    return Saved(ok=True, path=dest, files=len(raw_files), size=total, stored=stored, session=row)


def _markdown(agent: Agent, dest: Path, raw_files: list[dict[str, Any]]) -> str:
    title = agent.title or f"{agent.harness} session in {agent.cwd}"
    for entry in raw_files:
        stored = dest / entry["stored"]
        name = stored.name
        try:
            if agent.harness == "claude" and ".jsonl" in name and "/subagents/" not in entry["stored"]:
                with open_stored(stored) as handle:
                    lines = (b.decode("utf-8", "replace") for b in handle)
                    return render.markdown("claude", title, lines=lines)
            if agent.harness == "codex" and name.startswith("rollout-"):
                with open_stored(stored) as handle:
                    lines = (b.decode("utf-8", "replace") for b in handle)
                    return render.markdown("codex", title, lines=lines)
            if agent.harness == "opencode" and name.startswith("opencode-"):
                with open_stored(stored) as handle:
                    return render.markdown("opencode", title, document=json.loads(handle.read()))
            if name.endswith((".md", ".md.zst", ".md.gz")):
                with open_stored(stored) as handle:
                    return f"# {title}\n\n" + handle.read().decode("utf-8", "replace")
        except OSError, ValueError, EOFError:
            continue
    return render.markdown("", title)


def _resume_note(manifest: dict[str, Any]) -> str:
    lines = [
        f"{manifest['label']} session saved by huntaway on {time.strftime('%Y-%m-%d %H:%M', time.localtime())}.",
        f"Reason: {manifest['reason']}.",
        "",
    ]
    if manifest.get("resume"):
        lines += ["Resume it:", "", f"    {manifest['resume']}", ""]
    lines += [
        "If the harness has since deleted its copy, put the files back first:",
        "",
        f"    huntaway sessions restore {manifest['id']}",
        "",
        "transcript.md is the readable copy. raw/ holds the original files"
        + (" (compressed)." if any(f["stored"].endswith((".zst", ".gz")) for f in manifest["files"]) else "."),
    ]
    return "\n".join(lines) + "\n"


def restore(dest: Path, force: bool = False) -> list[str]:
    """
    Put a saved session's raw files back where the harness keeps them.

    :param dest: The session's archive folder
    :param force: Overwrite files that exist
    :returns: One line per file, saying what happened
    :raises OSError: When the manifest is missing
    """
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    report: list[str] = []
    for entry in manifest.get("files", []):
        stored = dest / entry["stored"]
        source = str(entry.get("source", ""))
        if source.startswith("opencode:"):
            tool = shutil.which("opencode") or str(Path.home() / ".opencode" / "bin" / "opencode")
            tmp = dest / "import.json"
            with open_stored(stored) as reader:
                tmp.write_bytes(reader.read())
            try:
                result = subprocess.run(  # nosec B603  # fixed command
                    [tool, "session", "import", str(tmp)], capture_output=True, text=True, timeout=120
                )
                report.append(
                    f"imported {source} into OpenCode"
                    if result.returncode == 0
                    else f"OpenCode refused {source}: {result.stderr.strip()[:200]}"
                )
            except (OSError, subprocess.SubprocessError) as exc:
                report.append(f"could not run opencode: {exc}")
            finally:
                tmp.unlink(missing_ok=True)
            continue
        target = Path(source)
        if not source.startswith("/"):
            report.append(f"skipped {entry['stored']}: no original path")
            continue
        if target.exists() and not force:
            report.append(f"kept {target}: it exists (use --force to overwrite)")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".huntaway-restore")
        with open_stored(stored) as reader, open(tmp, "wb") as writer:
            shutil.copyfileobj(reader, writer, CHUNK)
        os.replace(tmp, target)
        report.append(f"restored {target}")
    return report


def backup_pass(root: Path, store: Store, home: Path | None = None, budget: float = 20.0) -> int:
    """
    Save every agent transcript on disk that changed since the last pass, within a time budget.

    :param root: The archive root
    :param store: The database, which remembers what was saved
    :param home: The home folder to look in
    :param budget: Seconds to spend at most; the next pass continues
    :returns: How many transcripts were saved
    """
    home = home or Path.home()
    deadline = time.monotonic() + budget
    known = {r["path"]: (r["size"], r["mtime"]) for r in store.query("select path, size, mtime from backup_index")}
    count = 0
    candidates: list[tuple[str, Path, Path]] = []
    for cfg in sorted(home.glob(".claude*")):
        if (cfg / "projects").is_dir():
            candidates += [("claude", cfg, p) for p in (cfg / "projects").glob("*/*.jsonl")]
    codex_home = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    candidates += [("codex", codex_home, p) for p in (codex_home / "sessions").glob("*/*/*/rollout-*.jsonl")]
    for harness, cfg, path in candidates:
        if time.monotonic() > deadline:
            break
        try:
            st = path.stat()
        except OSError:
            continue
        if known.get(str(path)) == (st.st_size, st.st_mtime):
            continue
        sid = path.stem if harness == "claude" else path.stem.split("-", 6)[-1]
        title = first_prompt_claude(str(path)) if harness == "claude" else first_prompt_codex(str(path))
        files = [str(path)]
        if harness == "claude" and path.with_suffix("").is_dir():
            files.append(str(path.with_suffix("")))
        agent = Agent(
            harness=harness,
            label=BY_KEY[harness].label,
            pid=0,
            pids=[],
            started=st.st_ctime,
            session_id=sid,
            title=title,
            files=files,
            config_dir=str(cfg),
            cwd=_cwd_from(path, harness),
            last_active=st.st_mtime,
        )
        if harness == "claude":
            prefix = "" if cfg == home / ".claude" else f"CLAUDE_CONFIG_DIR={cfg} "
            agent.resume = f"cd {agent.cwd or '~'} && {prefix}claude --resume {sid}"
        else:
            agent.resume = f"cd {agent.cwd or '~'} && codex resume {sid}"
        existing = store.session(agent.key())
        result = save(agent, root, store, "backup" if not existing else str(existing["reason"]))
        if result.ok:
            count += 1
            with store.write() as db:
                db.execute(
                    "insert or replace into backup_index (path, size, mtime, archived) values (?, ?, ?, ?)",
                    (str(path), st.st_size, st.st_mtime, time.time()),
                )
    return count


def _cwd_from(path: Path, harness: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for _, line in zip(range(50), handle):
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                cwd = row.get("cwd") or (row.get("payload") or {}).get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        pass
    return ""
