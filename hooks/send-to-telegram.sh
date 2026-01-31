#!/bin/bash
# Claude Code Stop hook - forwards response to bridge (multi-session, secure)
#
# SECURITY: This hook does NOT need the Telegram token. It extracts the
# response and forwards to bridge via localhost HTTP. Bridge sends to Telegram.
# Token isolation: Claude never sees the token.
#
# ENV VARS (set by bridge via tmux set-environment):
#   BRIDGE_URL    - Full bridge URL (e.g., "http://localhost:8081" or "https://remote.example.com")
#   TMUX_PREFIX   - Session prefix (e.g., "claude-prod-")
#   SESSIONS_DIR  - Path to session files
#   PORT          - Bridge port (fallback if BRIDGE_URL not set)
#
# FLAGS:
#   TMUX_FALLBACK=0  - Disable tmux capture fallback (enabled by default)

set -euo pipefail

INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path')

# ─────────────────────────────────────────────────────────────────────────────
# Get session name from tmux
# ─────────────────────────────────────────────────────────────────────────────
SESSION_NAME=""
if [ -n "${TMUX:-}" ]; then
    SESSION_NAME=$(tmux display-message -p '#{session_name}' 2>/dev/null || true)
fi
[ -z "$SESSION_NAME" ] && exit 0

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG - Read from shell env first, fall back to tmux session env
# No defaults! Exit if config missing (fail closed for node isolation)
# ─────────────────────────────────────────────────────────────────────────────
get_tmux_env() {
    tmux show-environment -t "$SESSION_NAME" "$1" 2>/dev/null | cut -d= -f2-
}

BRIDGE_URL="${BRIDGE_URL:-$(get_tmux_env BRIDGE_URL)}"
TMUX_PREFIX="${TMUX_PREFIX:-$(get_tmux_env TMUX_PREFIX)}"
SESSIONS_DIR="${SESSIONS_DIR:-$(get_tmux_env SESSIONS_DIR)}"
BRIDGE_PORT="${PORT:-$(get_tmux_env PORT)}"

# Fail closed: exit if required config missing (prevents cross-node leakage)
[ -z "$TMUX_PREFIX" ] && exit 0
[ -z "$SESSIONS_DIR" ] && exit 0
# Need either BRIDGE_URL or PORT
[ -z "$BRIDGE_URL" ] && [ -z "$BRIDGE_PORT" ] && exit 0
# ─────────────────────────────────────────────────────────────────────────────

# Extract bridge session name from prefix pattern
BRIDGE_SESSION=""
if [[ "$SESSION_NAME" == ${TMUX_PREFIX}* ]]; then
    BRIDGE_SESSION="${SESSION_NAME#${TMUX_PREFIX}}"
fi

# Not our session - exit silently
[ -z "$BRIDGE_SESSION" ] && exit 0

# Determine file paths
SESSION_DIR="$SESSIONS_DIR/$BRIDGE_SESSION"
CHAT_ID_FILE="$SESSION_DIR/chat_id"
PENDING_FILE="$SESSION_DIR/pending"

[ ! -f "$CHAT_ID_FILE" ] && exit 0
[ ! -f "$TRANSCRIPT_PATH" ] && exit 0

# Build endpoint URL: BRIDGE_URL takes precedence, fall back to localhost:PORT
if [ -n "$BRIDGE_URL" ]; then
    # Strip trailing slash and append /response
    BRIDGE_ENDPOINT="${BRIDGE_URL%/}/response"
else
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
TMUX_FALLBACK_USED=false
if [ -z "$TEXT" ] || [ "$TEXT" = "null" ]; then
    if [ "${TMUX_FALLBACK:-1}" != "0" ] && [ -n "$SESSION_NAME" ]; then
        TMUX_FALLBACK_USED=true
        # Capture pane content (last 500 lines)
        TMUX_CONTENT=$(tmux capture-pane -t "$SESSION_NAME" -p -S -500 2>/dev/null)

        # Extract response: text between ● and ❯/─── separator
        # Keeps fallback to last_response if current is feedback prompt
        TEXT=$(echo "$TMUX_CONTENT" | awk '
            /^[[:space:]]*● / {
                in_response = 1
                line = $0
                sub(/^[[:space:]]*● */, "", line)
                response = line
                next
            }
            /^[[:space:]]*❯/ || /^[[:space:]]*───/ {
                if (in_response && response != "") {
                    # Save response (skip feedback prompt)
                    if (response !~ /How is Claude doing this session/) {
                        last_response = response
                    }
                }
                in_response = 0
                response = ""
            }
            in_response {
                # Skip status lines and UI elements
                if ($0 ~ /^[·✶✻⏵⎿]/) next
                if ($0 ~ /stop hook/ || $0 ~ /Whirring/ || $0 ~ /Herding/ || $0 ~ /Mulling/ || $0 ~ /Recombobulating/ || $0 ~ /Cooked for/ || $0 ~ /Saut/) next
                if ($0 ~ /^[a-z]+:$/) next
                if ($0 ~ /Tip:/) next
                # Continuation of response (remove leading indent)
                line = $0
                sub(/^[[:space:]]{1,2}/, "", line)
                if (response != "") response = response "\n" line
                else response = line
            }
            END {
                # Use current if real, otherwise fallback to last saved
                if (response != "" && response !~ /How is Claude doing this session/) {
                    print response
                } else if (last_response != "") {
                    print last_response
                }
            }
        ')
    fi
fi

# Add warning when using tmux fallback (ultra-short for busy managers)
if $TMUX_FALLBACK_USED && [ -n "$TEXT" ] && [ "$TEXT" != "null" ]; then
    TEXT="$TEXT

⚠️ May be incomplete. Retry if needed."
fi

# Exit if no text extracted
if [ -z "$TEXT" ] || [ "$TEXT" = "null" ]; then
    rm -f "$PENDING_FILE"
    exit 0
fi

# Forward to bridge (non-blocking with timeout)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMPFILE=$(mktemp)
echo "$TEXT" > "$TMPFILE"

# Run forward in background with 5s timeout, then cleanup
(
    timeout 5 python3 "$SCRIPT_DIR/forward-to-bridge.py" "$TMPFILE" "$BRIDGE_SESSION" "$BRIDGE_ENDPOINT" 2>/dev/null
    rm -f "$TMPFILE"
) &

rm -f "$PENDING_FILE"
