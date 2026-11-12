---
name: huntaway
description: Check this machine's health, explain why a process was stopped (exit code 137), run heavy jobs without being stopped, and find or resume earlier agent sessions. Use when the machine is slow or short of memory, when a build or test run died unexpectedly, before a long build, or when the user asks about earlier work in any agent session.
---

# huntaway

huntaway runs on this machine as a background service. It saves and closes idle agent sessions, stops runaway
processes before memory runs out, runs security checks, and records every action with its reason. Every command
below takes `--json` for output you can parse.

## When something died or the machine is slow

1. `huntaway status --json`: memory, swap, pressure stalls, agent sessions, recent actions.
2. If a process died with exit code 137 or vanished: `huntaway why <name or pid>`. It says whether huntaway stopped
   it and why. If huntaway did not, the kernel's OOM killer may have: `journalctl -k | grep -i oom`.
3. `huntaway top --mem` shows what holds the memory now and what protects each process.

## Before a heavy job

Wrap long builds, test suites and model runs so they are capped instead of stopped:

```sh
huntaway run -- cargo build --release
huntaway run --memory 8G --cpu 400% -- pytest -n 8
```

The exit code is the command's own.

## Finding earlier work

Every session huntaway closed or backed up is searchable by what was said in it:

```sh
huntaway sessions "stripe webhook signature"   # best matches, with the passage
huntaway sessions show <id>                    # the transcript and the resume command
huntaway sessions restore <id>                 # put the files back if the harness deleted them
```

## Agent sessions

`huntaway agents --json` lists open sessions with idle time, memory and whether each is stale.
`huntaway agents reap` lists the stale ones; `huntaway agents reap --yes` saves and closes them. A closed session is
saved and checked first and can be resumed with the command printed in its terminal.

## Rules

- Say what you will close, stop or change before you do it, and ask when the user has not asked for it.
- Never close a session the user is working in. huntaway refuses to close the session that asks.
- Settings: `huntaway config show`, `huntaway config set KEY VALUE`. The full guide: `huntaway guide`.
