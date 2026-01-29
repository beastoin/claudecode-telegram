#!/bin/bash
# Claude Code Stop hook - forwards response to bridge (multi-session, secure)
#
# SECURITY: This hook does NOT need the Telegram token. It extracts the
# response and forwards to bridge via localhost HTTP. Bridge sends to Telegram.
# Token isolation: Claude never sees the token.
#
# DESIGN: Sends to Telegram if session has chat_id (is telegram-connected).
#
# DEBUG: Set HOOK_DEBUG=1 to enable logging to ~/.claude/telegram/hook.log

set -euo pipefail

# Debug logging - uncomment next 2 lines to enable
# exec 2>> "$HOME/.claude/telegram/hook.log"
# set -x

BRIDGE_PORT="${PORT:-8080}"
INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path')

# Detect session name from tmux
SESSION_NAME=""
if [ -n "${TMUX:-}" ]; then
    SESSION_NAME=$(tmux display-message -p '#{session_name}' 2>/dev/null || true)
fi

# Extract name from prefix pattern
TMUX_PREFIX="${TMUX_PREFIX:-claude-}"
BRIDGE_SESSION=""
if [[ "$SESSION_NAME" == ${TMUX_PREFIX}* ]]; then
    BRIDGE_SESSION="${SESSION_NAME#${TMUX_PREFIX}}"
fi

# Determine file paths
SESSIONS_DIR="${SESSIONS_DIR:-$HOME/.claude/telegram/sessions}"
if [ -n "$BRIDGE_SESSION" ]; then
    SESSION_DIR="$SESSIONS_DIR/$BRIDGE_SESSION"
    CHAT_ID_FILE="$SESSION_DIR/chat_id"
    PENDING_FILE="$SESSION_DIR/pending"
    PORT_FILE="$SESSIONS_DIR/../port"
    if [ -f "$PORT_FILE" ]; then
        BRIDGE_PORT=$(cat "$PORT_FILE")
    fi
else
    exit 0
fi

BRIDGE_URL="http://localhost:${BRIDGE_PORT}/response"

[ ! -f "$CHAT_ID_FILE" ] && exit 0
[ ! -f "$TRANSCRIPT_PATH" ] && exit 0

# Get last user line for extraction
LAST_USER_LINE=$(grep -n '"type":"user"' "$TRANSCRIPT_PATH" | tail -1 | cut -d: -f1)
[ -z "$LAST_USER_LINE" ] && rm -f "$PENDING_FILE" && exit 0

TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE"' EXIT

# Extract assistant response from transcript
extract_response() {
    local assistant_lines

    # Get assistant messages after last user message
    assistant_lines=$(tail -n "+$LAST_USER_LINE" "$TRANSCRIPT_PATH" 2>/dev/null | grep '"type":"assistant"') || return 1

    # Extract text content with jq
    echo "$assistant_lines" | jq -rs '[.[].message.content[] | select(.type == "text") | .text] | join("\n\n")' > "$TMPFILE" 2>/dev/null || return 1

    # Verify we got actual content
    [ -s "$TMPFILE" ] && [ "$(cat "$TMPFILE")" != "null" ]
}

# Simple retry: try up to 3 times with 100ms delay
for attempt in 1 2 3; do
    if extract_response; then
        break
    fi
    if [ "$attempt" -lt 3 ]; then
        sleep 0.1
    else
        rm -f "$PENDING_FILE"
        exit 0
    fi
done

# Forward to bridge
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$SCRIPT_DIR/forward-to-bridge.py" "$TMPFILE" "$BRIDGE_SESSION" "$BRIDGE_URL"

rm -f "$PENDING_FILE"
