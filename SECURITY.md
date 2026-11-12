# Security

## Reporting a vulnerability

Please report security problems privately, through
[GitHub's private vulnerability reporting](https://github.com/jpsanders/huntaway/security/advisories/new), not in a
public issue. You will get an answer within a week.

## What huntaway can do on your machine

huntaway runs as your user, not as root. It can signal your own processes (throttle, stop, kill), read their command
lines, working folders and environments, read agent transcripts in your home folder and copy them into its archive.
It cannot touch other users' processes. Only `sudo huntaway scan --deep` and `sudo huntaway scan schedule` run as
root, to run rkhunter, chkrootkit, ClamAV and Lynis and install a weekly timer.

## How it is protected

- The dashboard and API listen on 127.0.0.1 by default. There, the page carries a random token (stored with mode
  600 in the state folder) and the server rejects requests whose Host header names another site, which stops DNS
  rebinding. Requests that change something always need the token, and cross origin POSTs are refused.
- Bound to another address, every request needs the token, sent as a bearer header.
- Every action re-checks the target's process id and start time, so a reused pid is never hit.
- An agent session is closed only after its saved copy reads back identical, and an agent can never close the
  session it runs in through the API or MCP.
- The optional LLM judge can only spare a process, never end one, so a manipulated answer cannot cause harm beyond
  leaving a process running. Process names and command lines reach it as quoted, cleaned, untrusted data. It is off
  by default and points at a local model.
- Saved transcripts can contain secrets an agent saw. The archive lives in your home folder, readable by you only;
  treat it like the harnesses' own transcript folders.
