#!/bin/bash
# Claude Code hooks — single entry point for all hook events
#
# Usage: hooks.sh <event> where event is: stop | start | tool-failure
#
# SECURITY: This hook does NOT need the Telegram token. It extracts the
# response and forwards to bridge via localhost HTTP. Bridge sends to Telegram.
# Token isolation: Claude never sees the token.
#
# ENV VARS (set by bridge via tmux set-environment):
#   BRIDGE_URL    - Full bridge URL (e.g., "http://localhost:8271")
#   TMUX_PREFIX   - Session prefix (e.g., "claude-prod-")
#   SESSIONS_DIR  - Path to session files
#   PORT          - Bridge port (fallback if BRIDGE_URL not set)

set -uo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Shared: tmux env helpers
# ─────────────────────────────────────────────────────────────────────────────

get_session_name() {
    local name
    name=$(tmux display-message -p '#{session_name}' 2>/dev/null || true)
    if [ -z "$name" ] && [ -n "${BRIDGE_SESSION:-}" ]; then
        name="${TMUX_PREFIX:-claude-}${BRIDGE_SESSION}"
    fi
    echo "$name"
}

get_tmux_env() {
    local val
    val=$(tmux show-environment -t "$SESSION_NAME" "$1" 2>/dev/null) && echo "${val#*=}" || true
}

# Load config from tmux session env (takes precedence), fall back to shell env
load_config() {
    SESSION_NAME=$(get_session_name)
    if [ -z "$SESSION_NAME" ]; then exit 0; fi

    local _tmux_bridge_url _tmux_prefix _tmux_sessions_dir _tmux_port
    _tmux_bridge_url="$(get_tmux_env BRIDGE_URL)"
    _tmux_prefix="$(get_tmux_env TMUX_PREFIX)"
    _tmux_sessions_dir="$(get_tmux_env SESSIONS_DIR)"
    _tmux_port="$(get_tmux_env PORT)"
    BRIDGE_URL="${_tmux_bridge_url:-${BRIDGE_URL:-}}"
    TMUX_PREFIX="${_tmux_prefix:-${TMUX_PREFIX:-}}"
    SESSIONS_DIR="${_tmux_sessions_dir:-${SESSIONS_DIR:-}}"
    BRIDGE_PORT="${_tmux_port:-${PORT:-}}"
}

# Extract worker name from session name. Sets BRIDGE_SESSION or exits.
extract_worker_name() {
    BRIDGE_SESSION=""
    if [[ "$SESSION_NAME" == "${TMUX_PREFIX}"* ]]; then
        BRIDGE_SESSION="${SESSION_NAME#"${TMUX_PREFIX}"}"
    fi
    if [ -z "$BRIDGE_SESSION" ]; then
        exit 0
    fi
}

# Build bridge endpoint URL
bridge_endpoint() {
    local path="${1:-}"
    if [ -n "$BRIDGE_URL" ]; then
        echo "${BRIDGE_URL%/}${path}"
    else
        echo "http://localhost:${BRIDGE_PORT}${path}"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Stop hook — extract response and forward to bridge
# ─────────────────────────────────────────────────────────────────────────────

hook_stop() {
    set -e
    INPUT=$(cat)
    TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path')
    LAST_MSG=$(echo "$INPUT" | jq -r '.last_assistant_message // empty')

    load_config

    # Fail closed: exit if required config missing (prevents cross-node leakage)
    if [ -z "$TMUX_PREFIX" ]; then
        echo "[hook] Missing TMUX_PREFIX in tmux env for $SESSION_NAME" >&2; exit 0
    fi
    if [ -z "$SESSIONS_DIR" ]; then
        echo "[hook] Missing SESSIONS_DIR in tmux env for $SESSION_NAME" >&2; exit 0
    fi
    if [ -z "$BRIDGE_URL" ] && [ -z "$BRIDGE_PORT" ]; then
        echo "[hook] Missing BRIDGE_URL and PORT in tmux env for $SESSION_NAME" >&2; exit 0
    fi

    extract_worker_name

    # Save session ID and CWD (atomic writes)
    SESSION_DIR="$SESSIONS_DIR/$BRIDGE_SESSION"
    SESSION_ID=$(basename "$TRANSCRIPT_PATH" .jsonl)
    if [ -n "$SESSION_ID" ] && [ -d "$SESSION_DIR" ] && [ -f "$TRANSCRIPT_PATH" ]; then
        _tmpid=$(mktemp "$SESSION_DIR/.session_id.XXXXXX")
        echo "$SESSION_ID" > "$_tmpid"
        chmod 600 "$_tmpid"
        mv -f "$_tmpid" "$SESSION_DIR/claude_session_id"

        if [ ! -f "$SESSION_DIR/claude_session_cwd" ]; then
            PANE_CWD=$(tmux display-message -t "$SESSION_NAME" -p '#{pane_current_path}' 2>/dev/null || true)
            if [ -n "$PANE_CWD" ]; then
                _tmpcwd=$(mktemp "$SESSION_DIR/.session_cwd.XXXXXX")
                echo "$PANE_CWD" > "$_tmpcwd"
                chmod 600 "$_tmpcwd"
                mv -f "$_tmpcwd" "$SESSION_DIR/claude_session_cwd"
            fi
        fi
    fi

    if [ ! -f "$SESSION_DIR/chat_id" ]; then exit 0; fi
    if [ ! -f "$TRANSCRIPT_PATH" ]; then exit 0; fi

    BRIDGE_ENDPOINT=$(bridge_endpoint /response)
    HOOK_LOG="/tmp/hook-debug-${BRIDGE_SESSION}.log"
    JSONL_HEALTHY=true
    TEXT=""

    # Priority 1: last_assistant_message from hook input (most reliable)
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
                JSONL_HEALTHY=false
                echo "[$(date +%T)] STALE transcript (${TRANSCRIPT_AGE}s), skipping jq" >> "$HOOK_LOG"
            fi
        fi

        if ! $TRANSCRIPT_STALE; then
            for attempt in $(seq 1 10); do
                if _extract_from_transcript; then
                    echo "[$(date +%T)] jq extraction OK on attempt $attempt, len=${#TEXT}" >> "$HOOK_LOG"
                    break
                fi
                echo "[$(date +%T)] jq extraction FAILED attempt $attempt" >> "$HOOK_LOG"
                sleep 0.5
            done
        fi
    fi

    # Fallback: tmux capture (enabled by default, set TMUX_FALLBACK=0 to disable)
    TMUX_FALLBACK_USED=false
    if [ -z "$TEXT" ] || [ "$TEXT" = "null" ]; then
        echo "[$(date +%T)] TEXT empty/null after jq phase, entering tmux fallback" >> "$HOOK_LOG"
        if [ "${TMUX_FALLBACK:-1}" != "0" ] && [ -n "$SESSION_NAME" ]; then
            TMUX_FALLBACK_USED=true
            TEXT=$(_tmux_capture_extract)
        fi
        echo "[$(date +%T)] tmux fallback result: text_len=${#TEXT}" >> "$HOOK_LOG"
    fi

    if $TMUX_FALLBACK_USED && [ -n "$TEXT" ] && [ "$TEXT" != "null" ]; then
        echo "[$(date +%T)] ADDING incomplete warning (tmux fallback used)" >> "$HOOK_LOG"
        TEXT="$TEXT

⚠️ May be incomplete. Retry if needed."
    fi

    # Exit if no text extracted
    if [ -z "$TEXT" ] || [ "$TEXT" = "null" ]; then
        rm -f "$SESSION_DIR/pending"
        exit 0
    fi

    # JSONL health alert (rate-limited by bridge)
    if ! $JSONL_HEALTHY; then
        curl -s -m 3 -X POST "$(bridge_endpoint /health-alert)" \
            -H "Content-Type: application/json" \
            -d "{\"worker\":\"$BRIDGE_SESSION\",\"issue\":\"jsonl_stale\",\"transcript_age\":$TRANSCRIPT_AGE,\"transcript_path\":\"$TRANSCRIPT_PATH\"}" \
            >/dev/null 2>&1 &
        echo "[$(date +%T)] HEALTH ALERT sent: jsonl_stale age=${TRANSCRIPT_AGE}s" >> "$HOOK_LOG"
    fi

    # Forward to bridge via curl+jq (non-blocking, background)
    _forward_to_bridge "$TEXT" "$BRIDGE_SESSION" "$BRIDGE_ENDPOINT" "$SESSION_ID" &

    rm -f "$SESSION_DIR/pending"
}

# Extract assistant text from JSONL transcript tail
_extract_from_transcript() {
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

# Extract response from tmux capture (between ● and ❯/───)
_tmux_capture_extract() {
    tmux capture-pane -t "$SESSION_NAME" -p -S -500 2>/dev/null | awk '
        /^[[:space:]]*● / {
            in_response = 1
            line = $0
            sub(/^[[:space:]]*● */, "", line)
            response = line
            next
        }
        /^[[:space:]]*❯/ || /^[[:space:]]*───/ {
            if (in_response && response != "") {
                if (response !~ /How is Claude doing this session/) {
                    last_response = response
                }
            }
            in_response = 0
            response = ""
        }
        in_response {
            if ($0 ~ /^[·✶✻⏵⎿]/) next
            if ($0 ~ /stop hook/ || $0 ~ /Whirring/ || $0 ~ /Herding/ || $0 ~ /Mulling/ || $0 ~ /Recombobulating/ || $0 ~ /Cooked for/ || $0 ~ /Saut/) next
            if ($0 ~ /interrupting/ || $0 ~ /[↓↑].*tokens/) next
            if ($0 ~ /^[a-z]+:$/) next
            if ($0 ~ /Tip:/) next
            line = $0
            sub(/^[[:space:]]{1,2}/, "", line)
            if (response != "") response = response "\n" line
            else response = line
        }
        END {
            if (response != "" && response !~ /How is Claude doing this session/) {
                print response
            } else if (last_response != "") {
                print last_response
            }
        }
    '
}

# Forward text to bridge via curl+jq (replaces forward-to-bridge.py)
_forward_to_bridge() {
    local text="$1" session="$2" endpoint="$3" session_id="${4:-}"

    # Use timeout/gtimeout if available
    local timeout_cmd="timeout"
    if ! command -v timeout &>/dev/null; then
        if command -v gtimeout &>/dev/null; then
            timeout_cmd="gtimeout"
        else
            timeout_cmd=""
        fi
    fi

    local payload
    if [ -n "$session_id" ]; then
        payload=$(jq -n --arg s "$session" --arg t "$text" --arg sid "$session_id" \
            '{session: $s, text: $t, session_id: $sid}')
    else
        payload=$(jq -n --arg s "$session" --arg t "$text" \
            '{session: $s, text: $t}')
    fi

    ${timeout_cmd:+$timeout_cmd 10} curl -s -m 10 -X POST "$endpoint" \
        -H "Content-Type: application/json" \
        -d "$payload" >/dev/null 2>&1
}

# ─────────────────────────────────────────────────────────────────────────────
# SessionStart hook — re-inject bridge instructions after compact/resume
# ─────────────────────────────────────────────────────────────────────────────

hook_start() {
    set -e
    cat > /dev/null  # drain stdin

    load_config
    if [ -z "$TMUX_PREFIX" ]; then exit 0; fi
    if [ -z "$BRIDGE_URL" ] && [ -z "$BRIDGE_PORT" ]; then exit 0; fi

    extract_worker_name

    local checkin_url
    checkin_url="$(bridge_endpoint /checkin)?name=$BRIDGE_SESSION"

    # 10s timeout: teleported workers reach bridge over Tailscale (~4-5s RTT)
    curl -s --max-time 10 "$checkin_url" 2>/dev/null || true
}

# ─────────────────────────────────────────────────────────────────────────────
# PostToolUseFailure hook — write signal file for POISONED detection
# ─────────────────────────────────────────────────────────────────────────────

hook_tool_failure() {
    # IMPORTANT: Always exit 0 (exit 2 BLOCKS the tool call)
    # IMPORTANT: stdout is injected into Claude context — redirect to /dev/null

    PAYLOAD=$(cat)

    load_config
    if [ -z "$TMUX_PREFIX" ]; then exit 0; fi

    extract_worker_name

    # Derive node name (same logic as bridge.py: "claude-prod-" → "prod")
    local node_name
    node_name=$(echo "$TMUX_PREFIX" | sed 's/-$//' | sed 's/^claude-//')
    [ -z "$node_name" ] && node_name="default"

    # Extract tool name from JSON payload
    local tool
    tool=$(echo "$PAYLOAD" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_name','unknown'))" 2>/dev/null || echo "unknown")

    # Write failure signal file (append)
    local hook_dir="/tmp/claudecode-telegram/${node_name}/${BRIDGE_SESSION}/hooks"
    mkdir -p "$hook_dir" 2>/dev/null
    echo "$(date +%s) ${tool}" >> "${hook_dir}/failures"

    # Trim old entries (keep last 50 lines to prevent unbounded growth)
    local failures_file="${hook_dir}/failures"
    if [ -f "$failures_file" ]; then
        local line_count
        line_count=$(wc -l < "$failures_file")
        if [ "$line_count" -gt 50 ]; then
            tail -50 "$failures_file" > "${failures_file}.tmp" && mv "${failures_file}.tmp" "$failures_file"
        fi
    fi

    exit 0
}

# ─────────────────────────────────────────────────────────────────────────────
# Dispatch
# ─────────────────────────────────────────────────────────────────────────────

case "${1:-}" in
    stop)         hook_stop ;;
    start)        hook_start ;;
    tool-failure) hook_tool_failure ;;
    *)
        echo "Usage: hooks.sh <stop|start|tool-failure>" >&2
        exit 1
        ;;
esac
