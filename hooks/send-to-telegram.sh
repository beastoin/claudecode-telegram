#!/bin/bash
# Claude Code Stop hook - forwards response to bridge (multi-session, secure)
#
# SECURITY: This hook does NOT need the Telegram token. It extracts the
# response and forwards to bridge via localhost HTTP. Bridge sends to Telegram.
# Token isolation: Claude never sees the token.
#
# FLAGS:
#   TMUX_FALLBACK=0  - Disable tmux capture fallback (enabled by default)

set -euo pipefail

LOG="$HOME/.claude/telegram/hook.log"
TS() { date '+%Y-%m-%d %H:%M:%S'; }

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id')
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path')

# Detect session name from tmux
SESSION_NAME=""
if [ -n "${TMUX:-}" ]; then
    SESSION_NAME=$(tmux display-message -p '#{session_name}' 2>/dev/null || true)
fi

# Extract bridge session name from prefix pattern
TMUX_PREFIX="${TMUX_PREFIX:-claude-}"
BRIDGE_SESSION=""
if [[ "$SESSION_NAME" == ${TMUX_PREFIX}* ]]; then
    BRIDGE_SESSION="${SESSION_NAME#${TMUX_PREFIX}}"
fi

[ -z "$BRIDGE_SESSION" ] && exit 0

# Determine file paths
SESSIONS_DIR="${SESSIONS_DIR:-$HOME/.claude/telegram/sessions}"
SESSION_DIR="$SESSIONS_DIR/$BRIDGE_SESSION"
CHAT_ID_FILE="$SESSION_DIR/chat_id"
PENDING_FILE="$SESSION_DIR/pending"

[ ! -f "$CHAT_ID_FILE" ] && exit 0
[ ! -f "$TRANSCRIPT_PATH" ] && exit 0

# Get bridge URL (BRIDGE_URL env var takes precedence for Docker container mode)
if [ -n "${BRIDGE_URL:-}" ]; then
    # Docker container mode - use host.docker.internal URL
    BRIDGE_ENDPOINT="${BRIDGE_URL}/response"
else
    # Direct mode - use localhost
    PORT_FILE="$SESSIONS_DIR/../port"
    BRIDGE_PORT="${PORT:-8080}"
    [ -f "$PORT_FILE" ] && BRIDGE_PORT=$(cat "$PORT_FILE")
    BRIDGE_ENDPOINT="http://localhost:${BRIDGE_PORT}/response"
fi

# Find last user message line
LAST_USER_LINE=$(grep -n '"type":"user"' "$TRANSCRIPT_PATH" | tail -1 | cut -d: -f1)
[ -z "$LAST_USER_LINE" ] && rm -f "$PENDING_FILE" && exit 0

# Extract text from transcript (with retry for race condition)
extract_from_transcript() {
    local tmp=$(mktemp)
    cat "$TRANSCRIPT_PATH" > "$tmp" 2>/dev/null
    local lines=$(tail -n "+$LAST_USER_LINE" "$tmp" | grep '"type":"assistant"') || { rm -f "$tmp"; return 1; }
    TEXT=$(echo "$lines" | jq -rs '[.[].message.content[] | select(.type == "text") | .text] | join("\n\n")') || { rm -f "$tmp"; return 1; }
    rm -f "$tmp"
    [ -n "$TEXT" ] && [ "$TEXT" != "null" ]
}

# Try transcript extraction first (10 attempts × 500ms = 5s max)
TEXT=""
for attempt in $(seq 1 10); do
    if extract_from_transcript; then
        break
    fi
    sleep 0.5
done

# Fallback: extract from tmux capture (enabled by default, set TMUX_FALLBACK=0 to disable)
if [ -z "$TEXT" ] || [ "$TEXT" = "null" ]; then
    if [ "${TMUX_FALLBACK:-1}" != "0" ] && [ -n "$SESSION_NAME" ]; then
        # Capture pane content (last 500 lines)
        TMUX_CONTENT=$(tmux capture-pane -t "$SESSION_NAME" -p -S -500 2>/dev/null)

        # Extract last response: text between last ● and next ❯ or ─── separator
        TEXT=$(echo "$TMUX_CONTENT" | awk '
            /^●/ {
                in_response = 1
                line = $0
                sub(/^● */, "", line)
                response = line
                next
            }
            /^❯/ || /^───/ {
                if (in_response && response != "") {
                    last_response = response
                }
                in_response = 0
                response = ""
            }
            in_response {
                # Skip status lines and UI elements
                if ($0 ~ /^[·✶⏵⎿]/) next
                if ($0 ~ /stop hook/ || $0 ~ /Whirring/ || $0 ~ /Herding/) next
                if ($0 ~ /^[a-z]+:$/) next
                if ($0 ~ /Tip:/) next
                # Continuation of response (remove 2-space indent)
                line = $0
                sub(/^  /, "", line)
                if (response != "") response = response "\n" line
                else response = line
            }
            END {
                if (response != "") print response
                else if (last_response != "") print last_response
            }
        ')
    fi
fi

# Exit if no text extracted
if [ -z "$TEXT" ] || [ "$TEXT" = "null" ]; then
    rm -f "$PENDING_FILE"
    exit 0
fi

# Forward to bridge
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE"' EXIT
echo "$TEXT" > "$TMPFILE"
python3 "$SCRIPT_DIR/forward-to-bridge.py" "$TMPFILE" "$BRIDGE_SESSION" "$BRIDGE_ENDPOINT" 2>/dev/null

rm -f "$PENDING_FILE"
