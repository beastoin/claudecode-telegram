#!/bin/bash
# Claude Code Stop hook - forwards response to bridge (multi-session, secure)
#
# SECURITY: This hook does NOT need the Telegram token. It extracts the
# response and forwards to bridge via localhost HTTP. Bridge sends to Telegram.
# Token isolation: Claude never sees the token.
#
# DESIGN: Sends to Telegram if session has chat_id (is telegram-connected).
# Uses polling with timeout to wait for transcript flush (not magic sleep).

set -euo pipefail

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

# Poll until transcript is stable and has content (max 2 seconds)
wait_for_transcript() {
    local deadline_ms=2000
    local sleep_ms=50
    local stable_count=0
    local last_size=-1
    local start_ms
    start_ms=$(date +%s%3N)
    
    while true; do
        local now_ms
        now_ms=$(date +%s%3N)
        local elapsed=$((now_ms - start_ms))
        
        # Timeout reached
        if [ "$elapsed" -ge "$deadline_ms" ]; then
            return 1
        fi
        
        # Get current file size
        local size
        size=$(stat -c %s "$TRANSCRIPT_PATH" 2>/dev/null || echo -1)
        
        if [ "$size" -eq "$last_size" ]; then
            # File size stable, try extraction
            tail -n "+$LAST_USER_LINE" "$TRANSCRIPT_PATH" 2>/dev/null | \
                grep '"type":"assistant"' | \
                jq -rs '[.[].message.content[] | select(.type == "text") | .text] | join("\n\n")' > "$TMPFILE" 2>/dev/null
            
            # Check if we got actual content
            if [ -s "$TMPFILE" ] && [ "$(cat "$TMPFILE")" != "null" ]; then
                stable_count=$((stable_count + 1))
                # Require 2 stable reads to confirm
                if [ "$stable_count" -ge 2 ]; then
                    return 0
                fi
            fi
        else
            stable_count=0
            last_size=$size
        fi
        
        # Sleep 50ms
        sleep 0.05
    done
}

# Wait for transcript with polling
if ! wait_for_transcript; then
    # Timeout - try one final extraction anyway
    tail -n "+$LAST_USER_LINE" "$TRANSCRIPT_PATH" 2>/dev/null | \
        grep '"type":"assistant"' | \
        jq -rs '[.[].message.content[] | select(.type == "text") | .text] | join("\n\n")' > "$TMPFILE" 2>/dev/null
fi

[ ! -s "$TMPFILE" ] && rm -f "$PENDING_FILE" && exit 0

# Forward to bridge
python3 - "$TMPFILE" "$BRIDGE_SESSION" "$BRIDGE_URL" << 'PYEOF'
import sys
import re
import json
import urllib.request

tmpfile, session, bridge_url = sys.argv[1], sys.argv[2], sys.argv[3]

with open(tmpfile) as f:
    text = f.read().strip()

if not text or text == "null":
    sys.exit(0)

if len(text) > 4000:
    text = text[:4000] + "\n..."

def esc(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

# Format markdown to HTML
blocks, inlines = [], []
text = re.sub(r'```(\w*)\n?(.*?)```', lambda m: (blocks.append((m.group(1) or '', m.group(2))), f"\x00B{len(blocks)-1}\x00")[1], text, flags=re.DOTALL)
text = re.sub(r'`([^`\n]+)`', lambda m: (inlines.append(m.group(1)), f"\x00I{len(inlines)-1}\x00")[1], text)
text = esc(text)
text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
text = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<i>\1</i>', text)

for i, (lang, code) in enumerate(blocks):
    text = text.replace(f"\x00B{i}\x00", f'<pre><code class="language-{lang}">{esc(code.strip())}</code></pre>' if lang else f'<pre>{esc(code.strip())}</pre>')
for i, code in enumerate(inlines):
    text = text.replace(f"\x00I{i}\x00", f'<code>{esc(code)}</code>')

# Forward to bridge
data = json.dumps({"session": session, "text": text}).encode()
try:
    req = urllib.request.Request(
        bridge_url,
        data=data,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        if r.status != 200:
            print(f"Bridge error: {r.status}", file=sys.stderr)
except Exception as e:
    print(f"Failed to forward to bridge: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF

rm -f "$PENDING_FILE"
