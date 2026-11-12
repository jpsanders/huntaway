# huntaway(1)

## NAME

huntaway - the sheepdog for your AI agents

## SYNOPSIS

`huntaway` [*COMMAND*] [*OPTIONS*]

`huntaway status` | `top` | `agents` | `sessions` | `events` | `why` | `scan` | `config` | `service` | `daemon` |
`ui` | `token` | `mcp` | `integrate` | `run` | `judge` | `doctor` | `demo` | `guide` | `man`

## DESCRIPTION

huntaway is a small background service for a machine where AI coding agents run. It does four jobs.

It saves and closes agent sessions left idle. Claude Code, Codex, OpenCode, Gemini CLI, Aider and others each hold
hundreds of megabytes while they wait in a terminal. huntaway finds them, saves the full transcript and checks the
copy byte for byte, closes the session, and prints the command that resumes it in its own terminal. Nothing is lost.

It keeps the machine from freezing. When memory runs out it acts before the kernel does: idle agent sessions are
saved and closed first, then the biggest process that is not protected is asked to stop. Processes that hold the CPU
for long are dropped to the lowest priority instead of being killed.

It hunts. Quick checks run every few hours without root: new lines in shell start files, new cron jobs and SSH
keys, programs running from temporary folders, miners, ports open to the network, keys readable by other users, and
agent CLIs started with their permission checks off by something without a terminal. Deep scans with rkhunter,
chkrootkit, ClamAV and Lynis run as root on demand or weekly.

It explains itself. Every action is recorded with the numbers that triggered it. A dashboard on localhost shows the
machine live, the agent sessions, a year of history, saved transcripts with full text search, security findings
and every setting. An MCP server and a skill let agents ask the same questions.

huntaway is light: one Python process, one dependency (psutil), about 30 MB of memory, and a few milliseconds of
CPU per sample.

## QUICK START

```sh
uv tool install git+https://github.com/jpsanders/huntaway   # or: pipx install git+https://...
huntaway service install --observe    # start in observe mode: it records, it never acts
huntaway ui                           # open the dashboard
huntaway integrate                    # give your agents the MCP server and the skill
huntaway config set general.mode enforce   # when you trust what it would have done
```

`huntaway demo` serves the dashboard on made up data without installing anything.

## COMMANDS

Every command that reads takes `--json`. Every command works whether the service runs or not: without it, the
work happens in the calling process. `--local` forces that.

### status

Machine health at a glance: CPU, memory, swap, pressure stalls, temperature and disk, the agent sessions and how much
memory the stale ones hold, security findings by severity, what huntaway itself costs, and the latest actions.
`huntaway` with no command runs `status`.

### top [--mem] [--limit N]

The busiest processes, by CPU or by memory with `--mem`, with the agent each belongs to and the reason it is
protected, if it is.

### agents [list|save|close|reap|pin|unpin] [ID] [--all] [--yes]

`agents` lists the interactive agent sessions: harness, title, status (working, idle, waiting for you), idle time,
memory, and whether it is stale. `--all` adds sessions without a terminal, such as headless runs.

`agents save ID` saves a session now and leaves it running. `agents close ID` saves it, checks the save, then
closes it; it asks first unless `--yes` is given. `agents reap` lists the stale sessions; `agents reap --yes` saves
and closes all of them. `agents pin ID` keeps a session open forever; `unpin` undoes it.

ID is the session id, `harness:session` as listed, or the process id.

### sessions [WORDS] | show ID | restore ID [--force] | delete ID | backup

`sessions` lists saved sessions, newest first. `sessions WORDS` searches every saved transcript and shows the best
matches with the passage that matched. `--harness` narrows to one harness.

`sessions show ID` prints the readable transcript and the resume command. `sessions restore ID` puts the raw files
back where the harness keeps them, so the resume command works even after the harness deleted its own copy; it
never overwrites a file unless `--force` is given. `sessions delete ID` removes a saved session from the archive.
`sessions backup` saves every transcript on disk now.

### events [--kind KINDS] [--limit N] [-f]

What huntaway did and saw, newest first. KINDS is a comma separated list of close, terminate, kill, throttle, alert,
archive, judge, config and service. `-f` keeps printing new events.

### why PID|NAME

Explains what huntaway did to a process. When a process died with exit code 137 and you wonder who killed it, this
is the first thing to run. If huntaway did not touch it, it says so and points at the kernel log.

### scan [--deep] [--only LIST] [--refresh-baseline] [-v] | scan setup | scan schedule [--calendar WHEN]

`scan` runs the quick checks now and lists every finding. `-v` shows the detail. `sudo huntaway scan --deep` runs
the deep scanners that are installed and writes a report to `/var/lib/huntaway/scans`, which the service picks up
within five minutes. `--only` picks scanners, for example `--only rkhunter,clamav`. `--refresh-baseline` refreshes
rkhunter's file properties first, which is right after a system update you trust.

ClamAV reads Downloads, `~/.local/bin`, autostart entries, the temporary folders and every file in the home folder
changed in the last seven days, skipping caches. That takes minutes where a full home scan can take a day.
`security.clamav_paths` replaces that set.

`scan setup` says which scanners are missing and prints the command that installs them. `sudo huntaway scan
schedule` installs a systemd timer that runs the deep scan weekly at low priority.

Exit status 4 means a serious finding (high or critical) is open.

### config [show|get|set|edit|path|validate|reset] [KEY] [VALUE]

`config` shows every option and marks the changed ones. `config get KEY` prints one value. `config set KEY VALUE`
validates and saves it; the service applies it within seconds. Lists take JSON (`'["a", "b"]'`) or commas
(`a,b`). `config edit` opens the file in `$VISUAL` or `$EDITOR`. `config validate` checks the file. `config reset`
writes the defaults and keeps the old file as `config.toml.old`.

### service install|uninstall|start|stop|restart|status|logs

`service install` writes a systemd user unit on Linux or a launch agent on macOS, enables and starts it, and
installs the man page. No root is needed. `--observe` installs in observe mode. `service uninstall` stops and
removes it and keeps settings, history and saved sessions. `service logs` prints the service log.

### daemon [--host HOST] [--port PORT] [--observe]

Runs the service in the foreground. The unit runs this.

### ui [--print]

Opens the dashboard in a browser, signed in. `--print` prints the URL with its token instead.

### token [--rotate]

Prints the API token. `--rotate` makes a new one; restart the service afterwards.

### mcp

Serves the MCP server over stdio. Harnesses start this; see FOR AGENTS.

### integrate [HARNESS...] [--dry-run] [--agents-md]

Registers the MCP server and installs the skill for Claude Code (every config folder, including extra accounts),
Codex, OpenCode, Gemini CLI and Cursor, or the harnesses named. It uses each harness's own CLI where there is one,
because those harnesses rewrite their config files while they run, and backs up any file it edits. `--dry-run`
shows what would change. `--agents-md` prints a paragraph for AGENTS.md or CLAUDE.md for tools without MCP.

### run [--cpu QUOTA] [--memory SIZE] [--nice N] -- COMMAND...

Runs a heavy command capped instead of stopped. The command gets its own systemd scope with a memory ceiling
(default 70% of RAM, `none` for no ceiling) and an optional CPU quota (`200%` is two cores), at a lower priority. A
lease tells the service the work is expected, so the CPU rules leave it alone. The exit code is the command's.

### judge [show|off|advise|veto|ask PID] [--model NAME] [--url URL]

Turns the optional LLM judge on or off (`on` means `veto`), sets its model and endpoint, or asks it about a process.
See THE LLM JUDGE.

### doctor

Checks this machine and says what is missing: the service, Python, pressure stall information, systemd, PipeWire
for call detection, notifications, tmux, zstd, the deep scanners and the skill.

### demo [--port PORT] [--no-open]

Serves the dashboard on made up data in a temporary folder. Nothing on the machine is read or touched.

### guide

Prints this manual as Markdown, which suits agents.

### man

Shows this manual as a man page.

## HOW IT DECIDES

### Memory

Memory is critical when available memory is below `memory.min_available_percent` and free swap is below
`memory.min_swap_free_percent`, or when the kernel reports that tasks stalled on memory for more than
`memory.pressure_full` percent of the last ten seconds. It is tight below twice that floor. While memory is short
huntaway samples every second.

When memory stays critical for `memory.sustain` seconds, huntaway picks one victim. With
`memory.prefer_idle_agents`, an idle agent session goes first, sessions idle past `agents.pressure_idle_minutes`
before others, biggest first: it is saved and closed, so nothing is lost. Otherwise the process holding the most
memory goes, skipping protected processes, processes under `memory.min_victim_mb`, busy agent sessions and leased
work until nothing else is left. It gets SIGTERM, and SIGKILL `memory.grace` seconds later. huntaway then waits a few
seconds for the memory to come back before it judges again.

`memory.process_max_percent` stops any single process holding more than that share of RAM.

### CPU

A process at or above `cpu.process_percent` (100 is one full core) for `cpu.sustain` seconds is a hog, but only
while the whole machine is at least `cpu.system_percent` busy: a hog on an idle machine harms nobody. The default
action, throttle, drops every thread of the process to the lowest CPU priority and its disk priority to idle. It
keeps running, and nobody notices until something else needs the CPU. An unprivileged user cannot raise a priority
back, so a throttled process stays low until it exits.

### Rules

Rules give matching processes their own limits. Each one has a regular expression over the process name or full
command line, optional `cpu_percent` and `memory_mb` limits, how long the limit must be passed (`sustain`) and an
action: throttle, terminate, kill or notify. A rule without limits acts on any match after `sustain` seconds.

```toml
[[rules]]
name = "dev servers"
match = "node .*(vite|next dev|webpack)"
memory_mb = 4096
sustain = 30
action = "terminate"

[[rules]]
name = "Windows programs"
match = "\\.exe$"
sustain = 0
action = "kill"
```

### Protection

Some processes are never touched: process 1 and kernel threads, huntaway and its parents, other users' processes,
and a built in list covering desktops (GNOME, KDE, sway, Hyprland and others), display servers, sound (PipeWire,
PulseAudio), terminals and multiplexers (closing one closes every tab inside it), ssh, keyrings and password
managers, and Zoom and Teams. `protect.names` and `protect.patterns` add to that. With `protect.calls`, any browser
or app holding the microphone or camera is protected while it does, so a call is never cut. Processes started with
`huntaway run` are left to the memory rules only.

Every action checks that the process is still the one judged, by process id and start time, so a reused process
id is never hit.

### Observe mode

With `general.mode = "observe"` huntaway records what it would have done, as events, and changes nothing. It saves
nothing either. Start here, read the events for a few days, then switch to enforce.

## AGENT SESSIONS

### Finding them

huntaway knows Claude Code, Codex, OpenCode, Gemini CLI, Qwen Code, Aider, Cursor Agent, Goose, Amp, Copilot CLI,
Crush and Kiro by their process names, and by their packages when they run under node, bun or python.
`agents.extra` adds more names. A launcher wrapping a binary counts as one session, and the memory shown is the
whole process tree.

For Claude Code, huntaway reads the session file Claude Code keeps per process (`sessions/<pid>.json` in its
config folder, `$CLAUDE_CONFIG_DIR` or `~/.claude`), which gives the session id, the folder and whether it is
working, idle or waiting for an answer. For Codex it matches the rollout file by folder and start time. OpenCode is
asked for its sessions through its own CLI.

### When a session is stale

A session is stale when it is interactive (it has a terminal), it is not working, it is not pinned, and its last
activity is older than `agents.idle_hours`. Activity is the newest of: the last message in its transcript, the last
status change its harness recorded, the last time it used real CPU, and, for harnesses without a transcript, the
last keystroke in its terminal. Idle sessions keep appending bookkeeping lines without timestamps to their
transcripts, so huntaway reads the newest timestamped message rather than the file's modification time.

Sessions waiting on a question count as stale after the same time unless `agents.include_waiting` is false.
Headless runs (no terminal) are never closed: they are doing a job.

### Closing one

huntaway saves the session, then checks the copy: every file is compressed, read back and compared with the bytes
it read, by SHA-256. If anything fails, the session stays open and the failure is an event. Then the process gets
SIGTERM and, after the grace time, SIGKILL. With `agents.tty_note`, the session's terminal then shows where it was
saved and the command that resumes it, for example `cd ~/code/api && claude --resume 0b6f...`. An agent can never
close the session it runs in.

### The archive

Each saved session is a folder under `sessions.archive_dir` (default `~/.local/share/huntaway/sessions`):

```text
claude/<session id>/
  raw/...              the original files, compressed with zstd (gzip where Python lacks zstd)
  transcript.md        every prompt and reply, tool calls on one line each, tool output trimmed
  screen.txt           the terminal scrollback, when the session ran in tmux
  manifest.json        what was saved, from where, with each file's SHA-256
  RESUME.txt           how to resume it
```

Transcripts are indexed for full text search. `sessions.backup` also saves every transcript the harnesses left on
disk every `sessions.backup_hours`, open or not. Claude Code deletes its own transcripts after 30 days by default;
with backup on, nothing is ever lost.

## SECURITY

The quick checks need no root and run every `security.quick_hours`. The first run takes a baseline; later runs
report what is new.

- shell: new or changed shell start files (`.bashrc`, `.zshrc`, `.profile` and others), and risky lines in them:
  shutdown commands, `curl | sh`, `base64 -d | sh`, `/dev/tcp`, `nc -e`, `LD_PRELOAD`.
- persistence: new cron lines, autostart entries, systemd user units, launch agents and SSH authorized keys.
- processes: miners, programs running from `/tmp`, `/var/tmp` or `/dev/shm`, programs whose file was deleted, and
  Windows programs.
- listeners: ports newly open to the network.
- credentials: SSH keys, cloud credentials and agent tokens readable by other users.
- agents: agent CLIs running with their permission checks off. Without a terminal this is high severity: the
  s1ngularity supply chain attack of 2025 ran local agent CLIs this way to search the disk for secrets.

Deep scans parse rkhunter, chkrootkit, ClamAV and Lynis output into the same findings. rkhunter's usual false
alarms are marked as such: changed file properties after a package update are low, and well known hidden files are
notes. Lynis adds a hardening index from 0 to 100.

`security.ignore` hides findings by id or by a regular expression over the title. The dashboard's Ignore button
adds to it.

## THE LLM JUDGE

The judge is off by default. When it is on, a language model reviews a decision before huntaway ends a process.

- advise: the rules act as usual, and the model's view is recorded next to the action.
- veto: before a terminate or kill decided by the CPU rules, a cap or a rule, the model may spare the process; it is
  then left alone for ten minutes. With no answer within `judge.timeout`, the rules decide.

The judge can never end a process, only spare one, so a wrong or manipulated answer can at most leave a process
running, which is what happens without huntaway. Memory emergencies never wait for it. Calls run on their own
thread, so a slow model never delays sampling. Process names and command lines are written by whoever started the
process, so they reach the model as quoted data with control characters removed, and the model is told they are
untrusted.

Any OpenAI compatible endpoint works. The default endpoint is a local Ollama, so nothing leaves the machine:

```sh
huntaway judge veto --model qwen3:4b
huntaway judge veto --model gpt-5-mini --url https://api.openai.com/v1
huntaway config set judge.api_key_env OPENAI_API_KEY
huntaway judge ask 4242
huntaway judge off
```

## THE DASHBOARD

The service serves a dashboard at `http://127.0.0.1:4868` (4868 spells HUNT on a phone keypad). Pages: Overview
(live charts, agent sessions, biggest processes, recent actions), Agents, Analytics (CPU, memory and swap, pressure
stalls, memory held by agents, disk, network and temperature, from 15 minutes to a year; the top apps; a weekday by
hour heatmap; actions per day), Saved sessions with search, Security, Events, Rules and protection (with the judge),
Settings (every option, generated from the schema) and Integrations.

On 127.0.0.1 the page carries the API token, so the browser on the machine needs nothing; the server refuses
requests whose Host header names another site, which stops DNS rebinding. With `server.host = "0.0.0.0"` the
dashboard is open to the network and every request needs the token (`huntaway token`). Cross origin POSTs are
refused. Prometheus metrics are at `/metrics`.

## FOR AGENTS

### The MCP server

`huntaway integrate` registers it. By hand, for any MCP client:

```json
{"mcpServers": {"huntaway": {"command": "huntaway", "args": ["mcp"]}}}
```

It is also served over HTTP at `http://127.0.0.1:4868/mcp`, with the token as a bearer header. Tools:

| Tool | What it does |
| --- | --- |
| status | Machine health, agents, findings and recent actions |
| top_processes | The busiest processes and what protects each |
| list_agent_sessions | Agent sessions with idle time, memory and the stale verdict |
| save_agent_session | Save a session now, without closing it |
| close_agent_session | Save, check, then close a session |
| close_stale_sessions | Close every stale session; dry_run (the default) only lists them |
| pin_agent_session | Keep a session open forever, or unpin it |
| search_saved_sessions | Full text search across every saved transcript |
| read_saved_session | Read a saved transcript, in pages |
| restore_saved_session | Put a session's files back so it can be resumed |
| events | What huntaway did and why |
| analytics | History for charts, top apps, heatmap |
| security_findings | Findings and scanner state |
| run_security_checks | Run the quick checks now |
| get_settings | Every setting with its schema |
| change_settings | Change settings |
| act_on_process | Throttle, terminate, kill or protect one process |

Tools that change things carry the MCP destructive hint, so harnesses ask before calling them.

### The skill

`huntaway integrate` installs a skill (`SKILL.md`) into Claude Code, Codex and OpenCode. It tells an agent when to
reach for huntaway: a slow machine, a process that died with 137, a heavy build to run, or old work to find.

### Rules for agents

- Before a heavy job, run `huntaway status`. Wrap long builds and test suites in `huntaway run -- COMMAND`.
- When a process died with exit code 137, run `huntaway why NAME` before guessing.
- To find earlier work, run `huntaway sessions "WORDS"` and `huntaway sessions show ID`.
- Say what you will close or stop before doing it. Never close a session the user is working in.

## CONFIGURATION

The settings live in `~/.config/huntaway/config.toml` (on macOS, `~/Library/Application Support/huntaway`). The
file documents every option; options left commented out keep their default. The service reloads the file within
seconds of a change, and refuses an invalid one with an event and a notification, keeping the old settings.

<!-- options start -->
### [general]

How often huntaway looks, and whether it acts or only records what it would do.

- `interval` (default `5`): Seconds between samples. huntaway samples every second while memory is short.
- `mode` (default `"enforce"`): enforce acts on the rules. observe only records what it would have done. One of:
  enforce, observe.
- `notify` (default `true`): Show a desktop notification for every action.
- `webhook` (default `""`): Also POST every action to this URL. ntfy, Slack and Discord webhooks are recognised,
  anything else gets JSON.

### [memory]

Keeps the machine from freezing when memory runs out, the way earlyoom does, but agent aware.

- `enabled` (default `true`): Act before the machine runs out of memory.
- `min_available_percent` (default `6.0`): Act when available memory falls below this share of RAM...
- `min_swap_free_percent` (default `10.0`): ...while free swap is below this share. A machine without swap counts as
  having none free.
- `pressure_full` (default `40.0`): Also act when the kernel's memory pressure (PSI full avg10) passes this percent,
  whatever the free memory says. 0 turns it off. Linux only.
- `sustain` (default `4`): Seconds the shortage must last before huntaway acts.
- `grace` (default `5`): Seconds between asking a process to stop (SIGTERM) and forcing it (SIGKILL).
- `prefer_idle_agents` (default `true`): When memory is short, save and close idle agent sessions before ending
  anything else. Nothing is lost: each one can be resumed.
- `min_victim_mb` (default `64`): Never end a process holding less memory than this, it would not help.
- `process_max_percent` (default `0.0`): End any single process holding more than this share of RAM for the sustain
  time. 0 turns it off.

### [cpu]

Calms processes that hold the CPU for a long time.

- `enabled` (default `true`): Act on processes that hold the CPU.
- `process_percent` (default `95.0`): A process at or above this CPU use counts as a hog. 100 is one full core, so a
  process on four cores reads 400.
- `system_percent` (default `85.0`): Only act while the whole machine is at least this busy. A hog on an otherwise
  idle machine harms nobody. 0 acts regardless.
- `sustain` (default `30`): Seconds a process must stay a hog before huntaway acts.
- `action` (default `"throttle"`): throttle drops the process to the lowest CPU and disk priority, which nobody
  notices until something else needs the CPU. terminate and kill end it. notify only tells you. One of: throttle,
  terminate, kill, notify.

### [agents]

Finds AI agent sessions (Claude Code, Codex, OpenCode, Gemini CLI, Aider and more).

- `enabled` (default `true`): Find AI agent sessions and show what they hold.
- `reap` (default `true`): Save and close interactive agent sessions left idle. Each one is archived and verified
  first, and its terminal shows the command that resumes it.
- `idle_hours` (default `12.0`): A session counts as stale after this many hours without a message, a keystroke or
  CPU work.
- `pressure_idle_minutes` (default `30.0`): While memory is short, sessions idle this many minutes are closed first.
- `include_waiting` (default `true`): Also close sessions that have waited on a question for you for the full idle
  time.
- `tty_note` (default `true`): After closing a session, print its resume command in its own terminal.
- `pinned` (default `[]`): Session ids, or process ids, that are never closed. The dashboard's pin button adds to
  this list.
- `extra` (default `[]`): More process names to treat as agent sessions, for harnesses huntaway does not know yet.
  They are listed and closed, and their tmux scrollback is saved.

### [sessions]

The archive of saved agent sessions.

- `archive_dir` (default `""`): Where saved sessions go. Empty means the data folder.
- `compress` (default `true`): Compress saved transcripts (zstd where Python has it, gzip otherwise).
- `backup` (default `false`): Every few hours, also save every transcript the agents left on disk, open or not.
  Claude Code deletes its own after 30 days by default.
- `backup_hours` (default `6.0`): Hours between backup passes.

### [protect]

Processes huntaway never touches.

- `names` (default `[]`): Process names never touched, on top of the built in list (desktop, audio, terminals, ssh,
  password managers, call apps).
- `patterns` (default `[]`): Regular expressions matched against the full command line. A match is never touched.
- `calls` (default `true`): Protect browsers and call apps while they hold the microphone or camera, so a call is
  never cut. Linux with PipeWire.

### [security]

Built in quick checks and deep scans (rkhunter, chkrootkit, ClamAV, Lynis).

- `enabled` (default `true`): Run the built in quick checks.
- `quick_hours` (default `6.0`): Hours between quick checks.
- `ignore` (default `[]`): Finding ids, or regular expressions over the finding title, to hide.
- `clamav_paths` (default `[]`): Folders the deep ClamAV scan covers. Empty means Downloads, the temporary folders
  and files changed in the last 7 days.

### [judge]

An optional language model that reviews a decision before huntaway ends a process. Off by default.

- `mode` (default `"off"`): off: no model is ever called. advise: the model's view is recorded next to each action.
  veto: the model may spare a process the rules would end. It can never end one. One of: off, advise, veto.
- `base_url` (default `"http://localhost:11434/v1"`): Any OpenAI compatible endpoint. The default is a local Ollama,
  so nothing leaves the machine.
- `model` (default `""`): The model name, for example qwen3:4b on Ollama.
- `api_key_env` (default `""`): Name of the environment variable that holds the API key. Leave empty for a local
  server.
- `timeout` (default `20.0`): Seconds to wait for a verdict. With no verdict in time the rules decide.

### [server]

The dashboard and the API.

- `enabled` (default `true`): Serve the dashboard and the API.
- `host` (default `"127.0.0.1"`): 127.0.0.1 keeps the dashboard on this machine. 0.0.0.0 opens it to your LAN, and
  every request then needs the token.
- `port` (default `4868`): The port. 4868 spells HUNT on a phone keypad.
- `metrics` (default `true`): Serve Prometheus metrics at /metrics.

### [retention]

How long history is kept.

- `raw_hours` (default `24`): Keep full resolution samples this many hours.
- `minute_days` (default `14`): Keep one minute averages this many days.
- `hour_days` (default `400`): Keep hourly averages this many days.
- `events_days` (default `365`): Keep events and findings this many days.
<!-- options end -->

## FILES

- `~/.config/huntaway/config.toml`: the settings.
- `~/.local/share/huntaway/huntaway.db`: history, events, the session index and findings (SQLite).
- `~/.local/share/huntaway/sessions/`: the archive of saved sessions.
- `~/.local/state/huntaway/token`: the API token, mode 600.
- `$XDG_RUNTIME_DIR/huntaway/leases/`: leases from `huntaway run`.
- `/var/lib/huntaway/scans/`: deep scan reports written as root.
- `~/.config/systemd/user/huntaway.service`: the service unit on Linux.
- `~/Library/LaunchAgents/io.github.jpsanders.huntaway.plist`: the launch agent on macOS.

## ENVIRONMENT

- `HUNTAWAY_CONFIG_DIR`, `HUNTAWAY_DATA_DIR`, `HUNTAWAY_STATE_DIR`: override the folders above.
- `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`, `XDG_RUNTIME_DIR`: respected on Linux.
- `CLAUDE_CONFIG_DIR`, `CODEX_HOME`: read from each agent process, so every account is found.
- `NO_COLOR`: plain output.

## EXIT STATUS

- 0: done.
- 1: failed.
- 2: bad usage or an invalid value.
- 3: the service is not running (`service status`, `ui`).
- 4: a serious security finding is open (`scan`).
- 127: `run` could not find the command. Otherwise `run` exits with the command's own status.

## EXAMPLES

```sh
huntaway agents reap                  # which sessions are stale, and how much they hold
huntaway agents reap --yes            # save and close them
huntaway sessions "stripe webhook"    # find the session where that was fixed
huntaway sessions show claude:0b6f2c  # read it, with the resume command
huntaway why node                     # what happened to my dev server?
huntaway run --memory 8G -- cargo build --release
huntaway config set agents.idle_hours 6
huntaway events -f                    # watch huntaway work
sudo huntaway scan --deep --only rkhunter,clamav
```

## SEE ALSO

earlyoom(1), systemd-oomd(8), rkhunter(8), chkrootkit(1), clamscan(1), lynis(8), systemd-run(1)

Project page: https://github.com/jpsanders/huntaway

## AUTHOR

James Phillip Sanders. MIT licence.
