# Changelog

Versions follow [semantic versioning](https://semver.org). Tags carry the bare version, such as `1.0.0`.

## 1.0.0 (2026-11-12)

The first release.

- A background service (systemd user unit on Linux, launch agent on macOS) with a dashboard on localhost.
- Agent sessions: Claude Code, Codex, OpenCode, Gemini CLI, Qwen Code, Aider, Cursor Agent, Goose, Amp, Copilot CLI,
  Crush and Kiro are found, with idle time, memory and status. Stale sessions are saved, verified byte for byte,
  closed, and left with their resume command. Saved transcripts are searchable and restorable.
- Memory protection that acts before the kernel's OOM killer, closing idle agent sessions first.
- CPU hogs throttled to the lowest priority instead of killed.
- User rules with regular expressions, CPU and memory limits and actions.
- Protection for the desktop, terminals, sound, ssh, password managers and live calls.
- Quick security checks without root and deep scans with rkhunter, chkrootkit, ClamAV and Lynis.
- An optional LLM judge that can spare a process and never end one. Off by default.
- An MCP server over stdio and HTTP, a skill, and `huntaway integrate` for Claude Code, Codex, OpenCode, Gemini CLI
  and Cursor.
- `huntaway run` for heavy jobs, capped instead of stopped.
- A year of history with analytics, Prometheus metrics, desktop notifications and webhooks.
- A man page and a full guide for people and agents.
