#!/usr/bin/env python3
"""Forward extracted Claude response to bridge."""

import sys
import re
import json
import urllib.request


def esc(s):
    """Escape HTML special characters."""
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def markdown_to_html(text):
    """Convert markdown to Telegram-compatible HTML."""
    # Extract code blocks and inline code first (to protect from other formatting)
    blocks, inlines = [], []

    # Code blocks: ```lang\ncode```
    text = re.sub(
        r'```(\w*)\n?(.*?)```',
        lambda m: (blocks.append((m.group(1) or '', m.group(2))), f"\x00B{len(blocks)-1}\x00")[1],
        text, flags=re.DOTALL
    )

    # Inline code: `code`
    text = re.sub(
        r'`([^`\n]+)`',
        lambda m: (inlines.append(m.group(1)), f"\x00I{len(inlines)-1}\x00")[1],
        text
    )

    # Escape HTML
    text = esc(text)

    # Bold: **text**
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)

    # Italic: *text* (but not **text**)
    text = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<i>\1</i>', text)

    # Restore code blocks
    for i, (lang, code) in enumerate(blocks):
        if lang:
            text = text.replace(f"\x00B{i}\x00", f'<pre><code class="language-{lang}">{esc(code.strip())}</code></pre>')
        else:
            text = text.replace(f"\x00B{i}\x00", f'<pre>{esc(code.strip())}</pre>')

    # Restore inline code
    for i, code in enumerate(inlines):
        text = text.replace(f"\x00I{i}\x00", f'<code>{esc(code)}</code>')

    return text


def forward_to_bridge(text, session, bridge_url):
    """Send formatted text to bridge via HTTP POST."""
    data = json.dumps({"session": session, "text": text}).encode()
    req = urllib.request.Request(
        bridge_url,
        data=data,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        if r.status != 200:
            print(f"Bridge error: {r.status}", file=sys.stderr)
            return False
    return True


def main():
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <tmpfile> <session> <bridge_url>", file=sys.stderr)
        sys.exit(2)

    tmpfile, session, bridge_url = sys.argv[1], sys.argv[2], sys.argv[3]

    with open(tmpfile) as f:
        text = f.read().strip()

    if not text or text == "null":
        sys.exit(0)

    # Truncate if too long for Telegram
    if len(text) > 4000:
        text = text[:4000] + "\n..."

    text = markdown_to_html(text)

    try:
        forward_to_bridge(text, session, bridge_url)
    except Exception as e:
        print(f"Failed to forward to bridge: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
