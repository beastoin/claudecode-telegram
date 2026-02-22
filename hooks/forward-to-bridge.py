#!/usr/bin/env python3
"""Forward extracted Claude response to bridge as raw markdown.

Bridge handles markdown→Telegram HTML conversion via markdown-it-py.
Requests are HMAC-signed with HOOK_SECRET for endpoint authentication.

Self-healing: On 403 (stale secret after bridge restart), triggers
/checkin to re-export fresh HOOK_SECRET, re-reads from tmux, retries once.
"""

import hashlib
import hmac
import os
import subprocess
import sys
import json
import urllib.error
import urllib.parse
import urllib.request


def compute_hook_signature(body: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 signature for hook request."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def read_hook_secret_from_tmux(session):
    """Read fresh HOOK_SECRET from tmux session environment."""
    # Try to get the actual tmux session name
    tmux_session = None
    try:
        tmux_session = subprocess.check_output(
            ["tmux", "display-message", "-p", "#{session_name}"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        prefix = os.environ.get("TMUX_PREFIX", "")
        tmux_session = f"{prefix}{session}" if prefix else session

    try:
        out = subprocess.check_output(
            ["tmux", "show-environment", "-t", tmux_session, "HOOK_SECRET"],
            text=True, stderr=subprocess.PIPE,
        ).strip()
    except Exception as e:
        print(f"Self-heal: failed to read HOOK_SECRET from tmux '{tmux_session}': {e}", file=sys.stderr)
        return ""

    if out.startswith("HOOK_SECRET="):
        return out.split("=", 1)[1]
    return ""


def forward_to_bridge(text, session, bridge_url):
    """Send raw markdown text to bridge via HTTP POST.

    On 403 (stale HOOK_SECRET), triggers /checkin to refresh the secret
    from the bridge, re-reads it from tmux env, and retries once.
    """
    data = json.dumps({"session": session, "text": text}).encode()
    hook_secret = os.environ.get("HOOK_SECRET", "")

    def send_once(secret):
        headers = {"Content-Type": "application/json"}
        if secret:
            headers["X-Hook-Signature"] = compute_hook_signature(data, secret)
        req = urllib.request.Request(bridge_url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status != 200:
                print(f"Bridge error: {r.status}", file=sys.stderr)
                return False
        return True

    try:
        return send_once(hook_secret)
    except urllib.error.HTTPError as e:
        if e.code != 403:
            raise

        # Self-heal: 403 means stale secret. Trigger /checkin to re-export.
        print("Self-heal: 403 on /response, calling /checkin to refresh HOOK_SECRET", file=sys.stderr)
        checkin_url = bridge_url.replace("/response", f"/checkin?name={urllib.parse.quote(session)}", 1)
        if checkin_url == bridge_url:
            print(f"Self-heal: could not derive /checkin URL from {bridge_url}", file=sys.stderr)
            raise

        try:
            with urllib.request.urlopen(checkin_url, timeout=10):
                pass  # /checkin re-exports HOOK_SECRET to tmux env
        except Exception as checkin_err:
            print(f"Self-heal: /checkin failed: {checkin_err}", file=sys.stderr)
            raise e

        fresh_secret = read_hook_secret_from_tmux(session)
        if not fresh_secret:
            print("Self-heal: HOOK_SECRET still missing after /checkin", file=sys.stderr)
            raise

        print("Self-heal: retrying with fresh HOOK_SECRET", file=sys.stderr)
        return send_once(fresh_secret)


def main():
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <tmpfile> <session> <bridge_url>", file=sys.stderr)
        sys.exit(2)

    tmpfile, session, bridge_url = sys.argv[1], sys.argv[2], sys.argv[3]

    with open(tmpfile) as f:
        text = f.read().strip()

    if not text or text == "null":
        sys.exit(0)

    # Bridge handles message splitting and markdown conversion
    try:
        forward_to_bridge(text, session, bridge_url)
    except Exception as e:
        print(f"Failed to forward to bridge: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
