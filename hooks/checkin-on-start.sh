#!/bin/bash
# Claude Code SessionStart hook - re-injects bridge instructions after compaction/resume
#
# Fires on "compact", "resume", "init", and "start" events (configured via matcher in settings.json).
# Calls GET /checkin?name=<worker> and prints the result to stdout, which Claude
# Code injects into the post-compaction context.
#
# ENV VARS (set by bridge via tmux set-environment):
#   BRIDGE_URL    - Full bridge URL (e.g., "http://localhost:8081")
#   TMUX_PREFIX   - Session prefix (e.g., "claude-prod-")
#   PORT          - Bridge port (fallback if BRIDGE_URL not set)

set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Get session name from tmux
# ─────────────────────────────────────────────────────────────────────────────
SESSION_NAME=$(tmux display-message -p '#{session_name}' 2>/dev/null || true)
[ -z "$SESSION_NAME" ] && exit 0

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG - Read from tmux session env first, fall back to shell env
# ─────────────────────────────────────────────────────────────────────────────
get_tmux_env() {
    local val
    val=$(tmux show-environment -t "$SESSION_NAME" "$1" 2>/dev/null) && echo "${val#*=}" || true
}

_tmux_bridge_url="$(get_tmux_env BRIDGE_URL)"
_tmux_prefix="$(get_tmux_env TMUX_PREFIX)"
_tmux_port="$(get_tmux_env PORT)"

BRIDGE_URL="${_tmux_bridge_url:-${BRIDGE_URL:-}}"
TMUX_PREFIX="${_tmux_prefix:-${TMUX_PREFIX:-}}"
BRIDGE_PORT="${_tmux_port:-${PORT:-}}"

# Fail closed: exit if not in a bridge session
[ -z "$TMUX_PREFIX" ] && exit 0
[ -z "$BRIDGE_URL" ] && [ -z "$BRIDGE_PORT" ] && exit 0

# ─────────────────────────────────────────────────────────────────────────────
# Extract worker name from session
# ─────────────────────────────────────────────────────────────────────────────
WORKER_NAME=""
if [[ "$SESSION_NAME" == ${TMUX_PREFIX}* ]]; then
    WORKER_NAME="${SESSION_NAME#${TMUX_PREFIX}}"
fi
[ -z "$WORKER_NAME" ] && exit 0

# ─────────────────────────────────────────────────────────────────────────────
# Build endpoint URL and call /checkin
# ─────────────────────────────────────────────────────────────────────────────
if [ -n "$BRIDGE_URL" ]; then
    CHECKIN_URL="${BRIDGE_URL%/}/checkin?name=$WORKER_NAME"
else
    CHECKIN_URL="http://localhost:${BRIDGE_PORT}/checkin?name=$WORKER_NAME"
fi

# Curl with short timeout; print to stdout for Claude context injection
curl -s --max-time 3 "$CHECKIN_URL" 2>/dev/null || true
