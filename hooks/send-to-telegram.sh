#!/bin/bash
# Claude Code Stop hook - forwards response to bridge (multi-session, secure)
#
# SECURITY: This hook does NOT need the Telegram token. It extracts the
# response and forwards to bridge via localhost HTTP. Bridge sends to Telegram.
# Token isolation: Claude never sees the token.
#
# Install: copy to ~/.claude/hooks/ and add to ~/.claude/settings.json

set -euo pipefail

# Bridge endpoint (same port as webhook server)
BRIDGE_PORT="${PORT:-8080}"
BRIDGE_URL="http://localhost:${BRIDGE_PORT}/response"

INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path')

# Detect session name from tmux
SESSION_NAME=""
if [ -n "${TMUX:-}" ]; then
    SESSION_NAME=$(tmux display-message -p '#{session_name}' 2>/dev/null || true)
fi

# Extract name from prefix pattern (configurable via TMUX_PREFIX env)
TMUX_PREFIX="${TMUX_PREFIX:-claude-}"
BRIDGE_SESSION=""
if [[ "$SESSION_NAME" == ${TMUX_PREFIX}* ]]; then
    BRIDGE_SESSION="${SESSION_NAME#${TMUX_PREFIX}}"
fi

# Determine file paths based on session type
if [ -n "$BRIDGE_SESSION" ]; then
    # Multi-session mode: per-session files
    SESSION_DIR=~/.claude/telegram/sessions/"$BRIDGE_SESSION"
    PENDING_FILE="$SESSION_DIR/pending"
else
    # Legacy single-session mode (fallback) - still check pending
    PENDING_FILE=~/.claude/telegram_pending
fi

# Only respond to Telegram-initiated messages (check pending file)
[ ! -f "$PENDING_FILE" ] && exit 0

PENDING_TIME=$(cat "$PENDING_FILE" 2>/dev/null || echo "")
NOW=$(date +%s)
[ -z "$PENDING_TIME" ] || [ $((NOW - PENDING_TIME)) -gt 600 ] && rm -f "$PENDING_FILE" && exit 0
[ ! -f "$TRANSCRIPT_PATH" ] && rm -f "$PENDING_FILE" && exit 0

# Extract response text from transcript
LAST_USER_LINE=$(grep -n '"type":"user"' "$TRANSCRIPT_PATH" | tail -1 | cut -d: -f1)
[ -z "$LAST_USER_LINE" ] && rm -f "$PENDING_FILE" && exit 0

TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE"' EXIT

tail -n "+$LAST_USER_LINE" "$TRANSCRIPT_PATH" | \
  grep '"type":"assistant"' | \
  jq -rs '[.[].message.content[] | select(.type == "text") | .text] | join("\n\n")' > "$TMPFILE" 2>/dev/null

[ ! -s "$TMPFILE" ] && exit 0

# Format and forward to bridge
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

# Forward to bridge (no token needed!)
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
