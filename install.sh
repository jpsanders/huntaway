#!/bin/sh
# Install huntaway and start it in observe mode.
# Usage: curl -fsSL https://raw.githubusercontent.com/jpsanders/huntaway/main/install.sh | sh
set -eu

REF="${HUNTAWAY_REF:-1.0.0}"
SOURCE="git+https://github.com/jpsanders/huntaway@${REF}"

say() { printf '%s\n' "$*"; }

if ! command -v uv >/dev/null 2>&1; then
    say "Installing uv, the Python package manager huntaway installs with"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="${HOME}/.local/bin:${PATH}"
    export PATH
fi

say "Installing huntaway ${REF}"
uv tool install --force "${SOURCE}"

HUNTAWAY="$(command -v huntaway || printf '%s' "${HOME}/.local/bin/huntaway")"
say "Starting the service in observe mode: it records what it would do and changes nothing"
"${HUNTAWAY}" service install --observe

say ""
say "Done. Next:"
say "  huntaway ui          open the dashboard"
say "  huntaway agents      your agent sessions"
say "  huntaway integrate   give your agents the MCP server and the skill"
say "  huntaway config set general.mode enforce   when the events look right"
