#!/bin/bash
# Claude Code stop hook for iMessage bridge.
# Extracts response from transcript and forwards to bridge.
#
# Environment variables (set by bridge):
#   PORT - Bridge HTTP port (default: 8083)
#   TMUX_PREFIX - tmux session prefix (default: claude-imessage-)
#   SESSIONS_DIR - Session files directory

set -euo pipefail

# Configuration
PORT="${PORT:-8083}"
TMUX_PREFIX="${TMUX_PREFIX:-claude-imessage-}"
SESSIONS_DIR="${SESSIONS_DIR:-$HOME/.claude/imessage/sessions}"
BRIDGE_URL="http://127.0.0.1:${PORT}/response"

# Read JSON from stdin
INPUT=$(cat)

# Extract transcript path from JSON
TRANSCRIPT_PATH=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('transcript_path',''))" 2>/dev/null || echo "")

# Detect tmux session name
TMUX_SESSION=""
if [ -n "${TMUX:-}" ]; then
    TMUX_SESSION=$(tmux display-message -p '#{session_name}' 2>/dev/null || echo "")
fi

# Extract worker name from session name
SESSION_NAME=""
if [[ "$TMUX_SESSION" == ${TMUX_PREFIX}* ]]; then
    SESSION_NAME="${TMUX_SESSION#$TMUX_PREFIX}"
fi

# Exit if not our session
if [ -z "$SESSION_NAME" ]; then
    exit 0
fi

# Check if we have a pending request
PENDING_FILE="${SESSIONS_DIR}/${SESSION_NAME}/pending"
if [ ! -f "$PENDING_FILE" ]; then
    exit 0
fi

# Extract response from transcript
extract_response() {
    local transcript="$1"

    if [ ! -f "$transcript" ]; then
        return 1
    fi

    # Parse transcript to find last assistant response
    python3 << 'PYEOF'
import sys
import json
import re

transcript_path = sys.argv[1] if len(sys.argv) > 1 else ""
if not transcript_path:
    sys.exit(1)

try:
    with open(transcript_path, 'r') as f:
        lines = f.readlines()

    # Find last user message index
    last_user_idx = -1
    for i, line in enumerate(lines):
        try:
            entry = json.loads(line)
            if entry.get('type') == 'user':
                last_user_idx = i
        except:
            pass

    if last_user_idx < 0:
        sys.exit(1)

    # Collect assistant responses after last user message
    responses = []
    for line in lines[last_user_idx + 1:]:
        try:
            entry = json.loads(line)
            if entry.get('type') == 'assistant':
                msg = entry.get('message', {})
                content = msg.get('content', [])
                for block in content:
                    if block.get('type') == 'text':
                        text = block.get('text', '')
                        if text:
                            responses.append(text)
        except:
            pass

    if responses:
        print('\n'.join(responses))
    else:
        sys.exit(1)

except Exception as e:
    sys.exit(1)
PYEOF
}

# Fallback: extract from tmux pane
extract_from_tmux() {
    local session="$1"

    # Capture last 500 lines from tmux pane
    local pane_content
    pane_content=$(tmux capture-pane -t "$session" -p -S -500 2>/dev/null || echo "")

    if [ -z "$pane_content" ]; then
        return 1
    fi

    # Parse Claude Code UI output
    python3 << PYEOF
import sys
import re

content = '''$pane_content'''

# Find response blocks (between markers)
lines = content.split('\n')
in_response = False
response_lines = []
last_response = []

for line in lines:
    # Skip UI elements and tips
    if line.startswith('Tip:'):
        continue
    if '───' in line or line.startswith('●') or line.startswith('❯'):
        if last_response:
            response_lines = last_response[:]
        last_response = []
        in_response = True
        continue

    if in_response and line.strip():
        last_response.append(line)

# Use most recent response
if last_response:
    response_lines = last_response

if response_lines:
    print('\n'.join(response_lines))
else:
    sys.exit(1)
PYEOF
}

# Try transcript first, then tmux fallback
RESPONSE=""
MAX_RETRIES=10
RETRY_DELAY=0.5

for i in $(seq 1 $MAX_RETRIES); do
    if [ -n "$TRANSCRIPT_PATH" ]; then
        RESPONSE=$(extract_response "$TRANSCRIPT_PATH" 2>/dev/null || echo "")
    fi

    if [ -n "$RESPONSE" ]; then
        break
    fi

    sleep "$RETRY_DELAY"
done

# Fallback to tmux capture
if [ -z "$RESPONSE" ] && [ -n "$TMUX_SESSION" ]; then
    RESPONSE=$(extract_from_tmux "$TMUX_SESSION" 2>/dev/null || echo "")
fi

# Exit if no response
if [ -z "$RESPONSE" ]; then
    exit 0
fi

# Send to bridge
PAYLOAD=$(python3 -c "import json,sys; print(json.dumps({'session': sys.argv[1], 'text': sys.argv[2]}))" "$SESSION_NAME" "$RESPONSE")

curl -s -X POST "$BRIDGE_URL" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" \
    --max-time 30 \
    >/dev/null 2>&1 || true

exit 0
