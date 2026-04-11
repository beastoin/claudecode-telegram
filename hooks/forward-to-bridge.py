#!/usr/bin/env python3
"""Forward extracted Claude response to bridge as raw markdown.

Bridge handles markdown->Telegram HTML conversion via markdown-it-py.
"""

import os
import sys
import json
import urllib.error
import urllib.request


def forward_to_bridge(text, session, bridge_url, session_id=""):
    """Send raw markdown text to bridge via HTTP POST."""
    payload = {"session": session, "text": text}
    if session_id:
        payload["session_id"] = session_id
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(bridge_url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as r:
        if r.status != 200:
            print(f"Bridge error: {r.status}", file=sys.stderr)
            return False
    return True


def main():
    if len(sys.argv) < 4:
        print(f"Usage: {sys.argv[0]} <tmpfile> <session> <bridge_url> [session_id]", file=sys.stderr)
        sys.exit(2)

    tmpfile, session, bridge_url = sys.argv[1], sys.argv[2], sys.argv[3]
    session_id = sys.argv[4] if len(sys.argv) > 4 else ""

    with open(tmpfile) as f:
        text = f.read().strip()

    if not text or text == "null":
        sys.exit(0)

    # Bridge handles message splitting and markdown conversion
    try:
        forward_to_bridge(text, session, bridge_url, session_id)
    except Exception as e:
        print(f"Failed to forward to bridge: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
