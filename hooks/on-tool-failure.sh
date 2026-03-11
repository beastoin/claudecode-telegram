#!/bin/bash
# Claude Code PostToolUseFailure hook - records tool failures for POISONED detection
#
# Writes timestamp + tool name to a signal file that the bridge watchdog reads.
# Bridge checks: >= 3 failures in 120s window → POISONED state.
#
# ENV VARS (set by bridge via tmux set-environment):
#   TMUX_PREFIX   - Session prefix (e.g., "claude-prod-")
#
# IMPORTANT: Always exit 0 (exit 2 BLOCKS the tool call)
# IMPORTANT: stdout is injected into Claude context — redirect to /dev/null

set -uo pipefail

# Read hook payload from stdin
PAYLOAD=$(cat)

# Get session name from tmux
SESSION_NAME=$(tmux display-message -p '#{session_name}' 2>/dev/null || true)
[ -z "$SESSION_NAME" ] && exit 0

# Read TMUX_PREFIX from tmux session env first, fall back to shell env
get_tmux_env() {
    local val
    val=$(tmux show-environment -t "$SESSION_NAME" "$1" 2>/dev/null) && echo "${val#*=}" || true
}

_tmux_prefix="$(get_tmux_env TMUX_PREFIX)"
TMUX_PREFIX="${_tmux_prefix:-${TMUX_PREFIX:-}}"
[ -z "$TMUX_PREFIX" ] && exit 0

# Extract worker name from session
WORKER_NAME=""
if [[ "$SESSION_NAME" == ${TMUX_PREFIX}* ]]; then
    WORKER_NAME="${SESSION_NAME#${TMUX_PREFIX}}"
fi
[ -z "$WORKER_NAME" ] && exit 0

# Derive node name (same logic as bridge.py: "claude-prod-" → "prod")
NODE_NAME=$(echo "$TMUX_PREFIX" | sed 's/-$//' | sed 's/^claude-//')
[ -z "$NODE_NAME" ] && NODE_NAME="default"

# Extract tool name from JSON payload
TOOL=$(echo "$PAYLOAD" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_name','unknown'))" 2>/dev/null || echo "unknown")

# Write failure signal file (append)
HOOK_DIR="/tmp/claudecode-telegram/${NODE_NAME}/${WORKER_NAME}/hooks"
mkdir -p "$HOOK_DIR" 2>/dev/null
echo "$(date +%s) ${TOOL}" >> "${HOOK_DIR}/failures"

# Trim old entries (keep last 50 lines to prevent unbounded growth)
FAILURES_FILE="${HOOK_DIR}/failures"
if [ -f "$FAILURES_FILE" ]; then
    LINE_COUNT=$(wc -l < "$FAILURES_FILE")
    if [ "$LINE_COUNT" -gt 50 ]; then
        tail -50 "$FAILURES_FILE" > "${FAILURES_FILE}.tmp" && mv "${FAILURES_FILE}.tmp" "$FAILURES_FILE"
    fi
fi

exit 0
