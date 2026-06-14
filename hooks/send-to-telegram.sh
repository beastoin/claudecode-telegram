#!/bin/bash
# Claude Code Stop hook - forwards response to bridge (multi-session, secure)
#
# SECURITY: This hook does NOT need the Telegram token. It extracts the
# response and forwards to bridge via localhost HTTP. Bridge sends to Telegram.
# Token isolation: Claude never sees the token.
#
# ENV VARS (set by bridge via tmux set-environment):
#   BRIDGE_URL    - Full bridge URL (e.g., "http://localhost:8271" or "https://remote.example.com")
#   TMUX_PREFIX   - Session prefix (e.g., "claude-prod-")
#   SESSIONS_DIR  - Path to session files
#   PORT          - Bridge port (fallback if BRIDGE_URL not set)
#
# FLAGS:
#   TMUX_FALLBACK=0  - Disable tmux capture fallback (enabled by default)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path')
LAST_MSG=$(echo "$INPUT" | jq -r '.last_assistant_message // empty')

# ─────────────────────────────────────────────────────────────────────────────
# Get session name from tmux (works even without TMUX env var)
# In Docker/sandbox mode, fall back to BRIDGE_SESSION env var
# ─────────────────────────────────────────────────────────────────────────────
SESSION_NAME=$(tmux display-message -p '#{session_name}' 2>/dev/null || true)
if [ -z "$SESSION_NAME" ] && [ -n "${BRIDGE_SESSION:-}" ]; then
    # Docker mode: use BRIDGE_SESSION env var directly
    SESSION_NAME="${TMUX_PREFIX:-claude-}${BRIDGE_SESSION}"
fi
[ -z "$SESSION_NAME" ] && exit 0

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG - Read from shell env first, fall back to tmux session env
# No defaults! Exit if config missing (fail closed for node isolation)
# ─────────────────────────────────────────────────────────────────────────────
get_tmux_env() {
    local val
    val=$(tmux show-environment -t "$SESSION_NAME" "$1" 2>/dev/null) && echo "${val#*=}" || true
}

# Read from tmux session env FIRST (session-specific config takes precedence)
# Only fall back to shell env if tmux env is empty
_tmux_bridge_url="$(get_tmux_env BRIDGE_URL)"
_tmux_prefix="$(get_tmux_env TMUX_PREFIX)"
_tmux_sessions_dir="$(get_tmux_env SESSIONS_DIR)"
_tmux_port="$(get_tmux_env PORT)"
BRIDGE_URL="${_tmux_bridge_url:-${BRIDGE_URL:-}}"
TMUX_PREFIX="${_tmux_prefix:-${TMUX_PREFIX:-}}"
SESSIONS_DIR="${_tmux_sessions_dir:-${SESSIONS_DIR:-}}"
BRIDGE_PORT="${_tmux_port:-${PORT:-}}"

# Fail closed: exit if required config missing (prevents cross-node leakage)
if [ -z "$TMUX_PREFIX" ]; then
    echo "[hook] Missing TMUX_PREFIX in tmux env for $SESSION_NAME" >&2
    exit 0
fi
if [ -z "$SESSIONS_DIR" ]; then
    echo "[hook] Missing SESSIONS_DIR in tmux env for $SESSION_NAME" >&2
    exit 0
fi
# Need either BRIDGE_URL or PORT
if [ -z "$BRIDGE_URL" ] && [ -z "$BRIDGE_PORT" ]; then
    echo "[hook] Missing BRIDGE_URL and PORT in tmux env for $SESSION_NAME" >&2
    exit 0
fi
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
SESSION_ID=$(basename "$TRANSCRIPT_PATH" .jsonl)
if [ -n "$SESSION_ID" ] && [ -d "$SESSION_DIR" ] && [ -f "$TRANSCRIPT_PATH" ]; then
    SESSION_ID_FILE="$SESSION_DIR/claude_session_id"
    echo "$SESSION_ID" > "$SESSION_ID_FILE"
    chmod 600 "$SESSION_ID_FILE"
    CWD_FILE="$SESSION_DIR/claude_session_cwd"
    if [ ! -f "$CWD_FILE" ]; then
        PANE_CWD=$(tmux display-message -t "$SESSION_NAME" -p '#{pane_current_path}' 2>/dev/null || true)
        if [ -n "$PANE_CWD" ]; then
            echo "$PANE_CWD" > "$CWD_FILE"
            chmod 600 "$CWD_FILE"
        fi
    fi
fi
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

# Extract assistant text from the tail of the JSONL.
# Reads last 500 lines (fast on any file size), skips tool_result user entries.
extract_from_transcript() {
    TEXT=$(tail -500 "$TRANSCRIPT_PATH" 2>/dev/null | jq -rs '
      reverse |
      reduce .[] as $line (
        {found: false, texts: []};
        if .found then .
        elif ($line.type == "user" and ([$line.message.content[]? | select(.type == "tool_result")] | length == 0)) then .found = true
        elif ($line.type == "assistant") then
          .texts += [$line.message.content[]? | select(.type == "text") | .text | select(. != null and . != "(no content)")]
        else .
        end
      ) | .texts | reverse | join("\n\n")
    ') || return 1
    [ -n "$TEXT" ]
}

HOOK_LOG="/tmp/hook-debug-${BRIDGE_SESSION}.log"
TEXT=""

# Priority 1: Use last_assistant_message from hook input (most reliable)
if [ -n "$LAST_MSG" ]; then
    TEXT="$LAST_MSG"
    echo "[$(date +%T)] session=$BRIDGE_SESSION using last_assistant_message, len=${#TEXT}" >> "$HOOK_LOG"
fi

# Priority 2: Extract from transcript via jq (skip if stale)
if [ -z "$TEXT" ] || [ "$TEXT" = "null" ]; then
    TRANSCRIPT_STALE=false
    if [ -f "$TRANSCRIPT_PATH" ]; then
        TRANSCRIPT_MTIME=$(stat -c %Y "$TRANSCRIPT_PATH" 2>/dev/null || stat -f%m "$TRANSCRIPT_PATH" 2>/dev/null || echo 0)
        NOW=$(date +%s)
        TRANSCRIPT_AGE=$(( NOW - TRANSCRIPT_MTIME ))
        echo "[$(date +%T)] session=$BRIDGE_SESSION transcript age=${TRANSCRIPT_AGE}s" >> "$HOOK_LOG"
        if [ "$TRANSCRIPT_AGE" -gt 30 ]; then
            TRANSCRIPT_STALE=true
            echo "[$(date +%T)] STALE transcript, skipping jq" >> "$HOOK_LOG"
        fi
    fi

    if ! $TRANSCRIPT_STALE; then
        for attempt in $(seq 1 10); do
            if extract_from_transcript; then
                echo "[$(date +%T)] jq extraction OK on attempt $attempt, len=${#TEXT}" >> "$HOOK_LOG"
                break
            fi
            echo "[$(date +%T)] jq extraction FAILED attempt $attempt" >> "$HOOK_LOG"
            sleep 0.5
        done
    fi
fi

# Fallback: extract from tmux capture (enabled by default, set TMUX_FALLBACK=0 to disable)
TMUX_FALLBACK_USED=false
if [ -z "$TEXT" ] || [ "$TEXT" = "null" ]; then
    echo "[$(date +%T)] TEXT empty/null after jq phase, entering tmux fallback" >> "$HOOK_LOG"
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
                if ($0 ~ /interrupting/ || $0 ~ /[↓↑].*tokens/) next
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
    echo "[$(date +%T)] tmux fallback result: text_len=${#TEXT}" >> "$HOOK_LOG"
fi

# Add warning when using tmux fallback (ultra-short for busy managers)
if $TMUX_FALLBACK_USED && [ -n "$TEXT" ] && [ "$TEXT" != "null" ]; then
    echo "[$(date +%T)] ADDING incomplete warning (tmux fallback used)" >> "$HOOK_LOG"
    TEXT="$TEXT

⚠️ May be incomplete. Retry if needed."
fi

# Exit if no text extracted
if [ -z "$TEXT" ] || [ "$TEXT" = "null" ]; then
    rm -f "$PENDING_FILE"
    exit 0
fi

# Forward to bridge (non-blocking with timeout)
TMPFILE=$(mktemp)
echo "$TEXT" > "$TMPFILE"

# Run forward in background with 5s timeout, then cleanup
# Use gtimeout on macOS (GNU coreutils), fall back to no timeout if unavailable
TIMEOUT_CMD="timeout"
if ! command -v timeout &>/dev/null; then
    if command -v gtimeout &>/dev/null; then
        TIMEOUT_CMD="gtimeout"
    else
        TIMEOUT_CMD=""
    fi
fi
(
    ${TIMEOUT_CMD:+$TIMEOUT_CMD 5} python3 "$SCRIPT_DIR/forward-to-bridge.py" "$TMPFILE" "$BRIDGE_SESSION" "$BRIDGE_ENDPOINT" "$SESSION_ID"
    rm -f "$TMPFILE"
) &

rm -f "$PENDING_FILE"
