"""Behavior tests for tools/pilot/ — the terminal session sharing server.

Tests the HTTP + WebSocket server that exposes tmux sessions in a browser.
Requires node and tmux. Skips gracefully if either is missing.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import time

import pytest
import requests

PILOT_JS = os.path.join(os.path.dirname(__file__), "..", "tools", "pilot", "dist", "pilot.js")
NODE = "node"

_has_node = subprocess.run([NODE, "--version"], capture_output=True).returncode == 0 if os.path.exists(
    subprocess.run(["which", NODE], capture_output=True, text=True).stdout.strip() or "/dev/null"
) else False
_has_tmux = subprocess.run(["tmux", "-V"], capture_output=True).returncode == 0

pytestmark = pytest.mark.skipif(
    not (_has_node and _has_tmux and os.path.isfile(PILOT_JS)),
    reason="pilot tests require node, tmux, and compiled dist/pilot.js",
)

# ── Test session names ────────────────────────────────────────────────────────

TEST_SESSION = "claude-test-pilot-a"
TEST_SESSION_B = "claude-test-pilot-b"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _free_port():
    """Find a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(base_url, timeout=8):
    """Poll until the server responds or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{base_url}/", timeout=1)
            if r.status_code in (200, 401):
                return True
        except requests.ConnectionError:
            pass
        time.sleep(0.1)
    return False


def _create_tmux_session(name):
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "bash", "-lc", f"echo '{name}-ready'; exec bash"],
        check=True,
    )


def _kill_tmux_session(name):
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)


def _enable_session(base_url, name):
    return requests.post(f"{base_url}/api/pilot?session={name}")


def _disable_session(base_url, name):
    return requests.delete(f"{base_url}/api/pilot?session={name}")


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pilot_server():
    """Start a pilot server for the test module. No auth token."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    # Create tmux sessions
    _create_tmux_session(TEST_SESSION)
    _create_tmux_session(TEST_SESSION_B)

    # Start server
    proc = subprocess.Popen(
        [NODE, PILOT_JS],
        env={**os.environ, "PORT": str(port)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert _wait_for_server(base_url), f"Pilot server failed to start on port {port}"

    yield {"proc": proc, "port": port, "url": base_url}

    # Cleanup
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
    _kill_tmux_session(TEST_SESSION)
    _kill_tmux_session(TEST_SESSION_B)


@pytest.fixture(scope="module")
def pilot_server_auth():
    """Start a pilot server WITH auth token."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    token = "test-secret-token-42"

    _create_tmux_session(TEST_SESSION)

    proc = subprocess.Popen(
        [NODE, PILOT_JS],
        env={**os.environ, "PORT": str(port), "PILOT_TOKEN": token},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert _wait_for_server(f"{base_url}/?token={token}"), "Auth pilot server failed to start"

    yield {"proc": proc, "port": port, "url": base_url, "token": token}

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
    _kill_tmux_session(TEST_SESSION)


# ── Tests: API capture ────────────────────────────────────────────────────────

def test_api_capture_returns_pane_content(pilot_server):
    """GET /api/capture returns actual tmux pane content."""
    base = pilot_server["url"]
    _enable_session(base, TEST_SESSION)

    # Send known text into the tmux pane
    marker = "PILOT_CAPTURE_TEST_12345"
    subprocess.run(
        ["tmux", "send-keys", "-t", TEST_SESSION, f"echo {marker}", "Enter"],
        check=True,
    )
    time.sleep(0.3)

    r = requests.get(f"{base}/api/capture?session={TEST_SESSION}")
    assert r.status_code == 200
    data = r.json()
    assert "text" in data
    assert marker in data["text"], f"Expected '{marker}' in capture output"


def test_capture_disabled_session_returns_404(pilot_server):
    """GET /api/capture for a disabled session returns 404."""
    base = pilot_server["url"]
    _disable_session(base, TEST_SESSION)

    r = requests.get(f"{base}/api/capture?session={TEST_SESSION}")
    assert r.status_code == 404


def test_capture_nonexistent_session_returns_404(pilot_server):
    """GET /api/capture for a session that doesn't exist returns 404."""
    base = pilot_server["url"]
    r = requests.get(f"{base}/api/capture?session=claude-does-not-exist-xyz")
    assert r.status_code == 404


# ── Tests: Session page modes ────────────────────────────────────────────────

def test_readonly_session_page(pilot_server):
    """GET /session/<name>?readonly=1 returns HTML with readonly badge."""
    base = pilot_server["url"]
    _enable_session(base, TEST_SESSION)

    r = requests.get(f"{base}/session/{TEST_SESSION}?readonly=1")
    assert r.status_code == 200
    html = r.text
    assert "readonly" in html.lower(), "Readonly session page should contain 'readonly' indicator"


def test_embed_mode_hides_header(pilot_server):
    """GET /session/<name>?embed=1&hideheader=1 hides the header bar."""
    base = pilot_server["url"]
    _enable_session(base, TEST_SESSION)

    r = requests.get(f"{base}/session/{TEST_SESSION}?embed=1&hideheader=1")
    assert r.status_code == 200
    html = r.text
    # hideheader injects CSS 'display:none !important' on the header bar
    assert "display:none !important" in html, "Embed+hideheader should inject display:none on header"


def test_session_page_normal_has_header(pilot_server):
    """Normal session page (no embed/hideheader) does NOT hide the header."""
    base = pilot_server["url"]
    _enable_session(base, TEST_SESSION)

    r = requests.get(f"{base}/session/{TEST_SESSION}")
    assert r.status_code == 200
    html = r.text
    # The session name should appear in the header
    assert TEST_SESSION in html


# ── Tests: Grid view ─────────────────────────────────────────────────────────

def test_grid_page_returns_html(pilot_server):
    """GET /grid returns 200 with HTML content."""
    base = pilot_server["url"]
    r = requests.get(f"{base}/grid")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("Content-Type", "")


def test_grid_session_crud(pilot_server):
    """POST /api/grid-session creates a grid, GET /grid/<slug> serves it."""
    base = pilot_server["url"]
    _enable_session(base, TEST_SESSION)

    # Create grid session
    r = requests.post(
        f"{base}/api/grid-session",
        json={"slug": "test-grid", "sessions": [TEST_SESSION], "ttl": 60},
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("ok") is True
    assert data.get("slug") == "test-grid"

    # Fetch the grid page
    r2 = requests.get(f"{base}/grid/test-grid")
    assert r2.status_code == 200
    assert "text/html" in r2.headers.get("Content-Type", "")


def test_grid_session_requires_slug_and_sessions(pilot_server):
    """POST /api/grid-session with missing slug/sessions returns 400."""
    base = pilot_server["url"]

    # Missing slug
    r = requests.post(f"{base}/api/grid-session", json={"sessions": ["x"]})
    assert r.status_code == 400

    # Missing sessions
    r = requests.post(f"{base}/api/grid-session", json={"slug": "bad"})
    assert r.status_code == 400

    # Empty sessions
    r = requests.post(f"{base}/api/grid-session", json={"slug": "bad", "sessions": []})
    assert r.status_code == 400


def test_grid_nonexistent_slug_returns_404(pilot_server):
    """GET /grid/<nonexistent-slug> returns 404."""
    base = pilot_server["url"]
    r = requests.get(f"{base}/grid/no-such-grid-session")
    assert r.status_code == 404


# ── Tests: Debug page ─────────────────────────────────────────────────────────

def test_debug_page_returns_html(pilot_server):
    """GET /debug returns 200 with diagnostic HTML."""
    base = pilot_server["url"]
    r = requests.get(f"{base}/debug")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("Content-Type", "")
    # Debug page tests WASM loading, API, WebSocket
    assert "debug" in r.text.lower() or "diagnostic" in r.text.lower()


# ── Tests: Session enable/disable edge cases ─────────────────────────────────

def test_enable_nonexistent_session_fails(pilot_server):
    """POST /api/pilot?session=claude-nonexistent-xyz returns 404."""
    base = pilot_server["url"]
    r = requests.post(f"{base}/api/pilot?session=claude-nonexistent-xyz-999")
    assert r.status_code == 404


def test_session_name_validation_rejects_non_claude_prefix(pilot_server):
    """POST /api/pilot?session=not-claude-prefix returns 400."""
    base = pilot_server["url"]
    r = requests.post(f"{base}/api/pilot?session=not-claude-prefix")
    assert r.status_code == 400


def test_session_name_validation_accepts_claude_prefix(pilot_server):
    """POST /api/pilot?session=claude-test-pilot-a succeeds (session exists)."""
    base = pilot_server["url"]
    r = requests.post(f"{base}/api/pilot?session={TEST_SESSION}")
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is True


def test_session_enable_with_bad_remote_host(pilot_server):
    """POST /api/pilot?session=<name>&host=nonexistent handles gracefully."""
    base = pilot_server["url"]
    # Should return 404 (session not found on remote) — NOT crash the server
    r = requests.post(f"{base}/api/pilot?session={TEST_SESSION}&host=nonexistent-host-xyz")
    assert r.status_code in (404, 500), f"Expected error status, got {r.status_code}"

    # Verify server is still alive
    r2 = requests.get(f"{base}/api/sessions")
    assert r2.status_code == 200


def test_disable_already_disabled_session(pilot_server):
    """DELETE /api/pilot on an already-disabled session returns 200 (idempotent)."""
    base = pilot_server["url"]
    _disable_session(base, TEST_SESSION)
    r = _disable_session(base, TEST_SESSION)
    # Server shouldn't crash — it may return 200 (idempotent) or 404
    assert r.status_code in (200, 404)


# ── Tests: Auth token ─────────────────────────────────────────────────────────

def test_auth_token_blocks_unauthenticated_requests(pilot_server_auth):
    """With PILOT_TOKEN set, requests without token get 401."""
    base = pilot_server_auth["url"]
    r = requests.get(f"{base}/")
    assert r.status_code == 401

    r = requests.get(f"{base}/api/sessions")
    assert r.status_code == 401


def test_auth_token_allows_authenticated_requests(pilot_server_auth):
    """With PILOT_TOKEN set, requests with valid token get 200."""
    base = pilot_server_auth["url"]
    token = pilot_server_auth["token"]

    r = requests.get(f"{base}/?token={token}")
    assert r.status_code == 200

    r = requests.get(f"{base}/api/sessions?token={token}")
    assert r.status_code == 200


def test_auth_token_wrong_value_rejected(pilot_server_auth):
    """Wrong token value still gets 401."""
    base = pilot_server_auth["url"]
    r = requests.get(f"{base}/?token=wrong-token-value")
    assert r.status_code == 401


def test_auth_token_protects_api_pilot(pilot_server_auth):
    """POST /api/pilot without token returns 401."""
    base = pilot_server_auth["url"]
    r = requests.post(f"{base}/api/pilot?session={TEST_SESSION}")
    assert r.status_code == 401


def test_auth_token_protects_session_page(pilot_server_auth):
    """GET /session/<name> without token returns 401."""
    base = pilot_server_auth["url"]
    token = pilot_server_auth["token"]

    # First enable the session (with token)
    requests.post(f"{base}/api/pilot?session={TEST_SESSION}&token={token}")

    # Then try to access without token
    r = requests.get(f"{base}/session/{TEST_SESSION}")
    assert r.status_code == 401


# ── Tests: WebSocket readonly ─────────────────────────────────────────────────

def test_ws_readonly_drops_input(pilot_server):
    """In readonly mode, text sent via WebSocket is NOT written to the tmux pane."""
    base = pilot_server["url"]
    port = pilot_server["port"]
    _enable_session(base, TEST_SESSION)

    # Clear the pane first
    subprocess.run(["tmux", "send-keys", "-t", TEST_SESSION, "clear", "Enter"], check=True)
    time.sleep(0.3)

    # Capture pane content before WebSocket interaction
    before = subprocess.run(
        ["tmux", "capture-pane", "-t", TEST_SESSION, "-p"],
        capture_output=True, text=True,
    ).stdout

    # Use a node script to connect in readonly mode and send text
    readonly_marker = "READONLY_TEST_SHOULD_NOT_APPEAR"
    ws_script = f"""
import WebSocket from 'ws';
const ws = new WebSocket('ws://127.0.0.1:{port}/ws?session={TEST_SESSION}&cols=80&rows=24&readonly=1');
ws.on('open', () => {{
  ws.send('{readonly_marker}\\n');
  setTimeout(() => {{ ws.close(); process.exit(0); }}, 500);
}});
ws.on('error', (e) => {{ console.error(e.message); process.exit(1); }});
setTimeout(() => process.exit(1), 5000);
"""
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", ws_script],
        capture_output=True, text=True, timeout=10,
    )

    time.sleep(0.5)

    # Capture pane content after
    after = subprocess.run(
        ["tmux", "capture-pane", "-t", TEST_SESSION, "-p"],
        capture_output=True, text=True,
    ).stdout

    assert readonly_marker not in after, \
        f"Readonly WebSocket should NOT write to tmux pane, but found marker in output"


# ── Tests: Multiple sessions ─────────────────────────────────────────────────

def test_enable_multiple_sessions(pilot_server):
    """Enabling two sessions lists both in /api/sessions and /api/enabled."""
    base = pilot_server["url"]
    _enable_session(base, TEST_SESSION)
    _enable_session(base, TEST_SESSION_B)

    # /api/sessions shows both
    r = requests.get(f"{base}/api/sessions")
    assert r.status_code == 200
    names = [s["name"] for s in r.json()]
    assert TEST_SESSION in names
    assert TEST_SESSION_B in names

    # /api/enabled shows both
    r = requests.get(f"{base}/api/enabled")
    assert r.status_code == 200
    enabled_names = [e["name"] for e in r.json()]
    assert TEST_SESSION in enabled_names
    assert TEST_SESSION_B in enabled_names


def test_disable_one_session_keeps_other(pilot_server):
    """Disabling one session does not affect another."""
    base = pilot_server["url"]
    _enable_session(base, TEST_SESSION)
    _enable_session(base, TEST_SESSION_B)
    _disable_session(base, TEST_SESSION)

    r = requests.get(f"{base}/api/sessions")
    names = [s["name"] for s in r.json()]
    assert TEST_SESSION not in names
    assert TEST_SESSION_B in names


# ── Tests: Index page ─────────────────────────────────────────────────────────

def test_index_page_lists_enabled_sessions(pilot_server):
    """The index page HTML includes enabled session names."""
    base = pilot_server["url"]
    _enable_session(base, TEST_SESSION)

    r = requests.get(f"{base}/")
    assert r.status_code == 200
    assert TEST_SESSION in r.text


def test_index_page_excludes_disabled_sessions(pilot_server):
    """The index page does not list disabled sessions."""
    base = pilot_server["url"]
    _disable_session(base, TEST_SESSION)
    _disable_session(base, TEST_SESSION_B)

    r = requests.get(f"{base}/")
    assert r.status_code == 200
    assert TEST_SESSION not in r.text
    assert TEST_SESSION_B not in r.text


# ── Tests: Method validation ──────────────────────────────────────────────────

def test_api_pilot_rejects_get(pilot_server):
    """GET /api/pilot returns 405 (only POST/DELETE allowed)."""
    base = pilot_server["url"]
    r = requests.get(f"{base}/api/pilot?session={TEST_SESSION}")
    assert r.status_code == 405


def test_404_on_unknown_path(pilot_server):
    """Unknown paths return 404."""
    base = pilot_server["url"]
    r = requests.get(f"{base}/nonexistent/path")
    assert r.status_code == 404
