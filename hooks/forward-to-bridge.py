#!/usr/bin/env python3
"""Forward extracted Claude response to bridge as raw markdown.

Bridge handles markdown->Telegram HTML conversion via markdown-it-py.
"""

from __future__ import annotations

import sys
import json
import urllib.error
import urllib.request


def forward_to_bridge(text: str, session: str, bridge_url: str, session_id: str = "") -> bool:
    """Send raw markdown text to bridge via HTTP POST.

    Returns True on success, False on non-200 response.
    Raises urllib.error.URLError or OSError on network failure.
    """
    payload: dict[str, str] = {"session": session, "text": text}
    if session_id:
        payload["session_id"] = session_id
    data: bytes = json.dumps(payload).encode()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    req = urllib.request.Request(bridge_url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as r:
        if r.status != 200:
            print(f"Bridge error: {r.status}", file=sys.stderr)
            return False
    return True


def main() -> int:
    """Forward response text from tmpfile to bridge. Returns exit code."""
    if len(sys.argv) < 4:
        print(f"Usage: {sys.argv[0]} <tmpfile> <session> <bridge_url> [session_id]", file=sys.stderr)
        return 2

    tmpfile: str = sys.argv[1]
    session: str = sys.argv[2]
    bridge_url: str = sys.argv[3]
    session_id: str = sys.argv[4] if len(sys.argv) > 4 else ""

    with open(tmpfile, encoding="utf-8") as f:
        text: str = f.read().strip()

    if not text or text == "null":
        return 0

    # Bridge handles message splitting and markdown conversion
    try:
        forward_to_bridge(text, session, bridge_url, session_id)
    except (urllib.error.URLError, OSError) as e:
        print(f"Failed to forward to bridge: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
