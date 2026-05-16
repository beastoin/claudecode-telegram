#!/usr/bin/env bash
set -euo pipefail

# E2E test for worker-forge: build binary → run → verify tmux + bridge connectivity
# Usage: ./test-e2e.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

TEST_DIR=$(mktemp -d /tmp/forge-e2e-XXXXXX)
MOCK_PORT=0
MOCK_PID=0
WORKER_PID=0
SESSION_NAME="claude-prod-e2etest"
PASS=0
FAIL=0
TOTAL=0

cleanup() {
    echo ""
    echo "=== Cleanup ==="
    [[ $WORKER_PID -gt 0 ]] && kill "$WORKER_PID" 2>/dev/null || true
    [[ $MOCK_PID -gt 0 ]] && kill "$MOCK_PID" 2>/dev/null || true
    tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true
    rm -rf "$TEST_DIR"
    echo ""
    echo "=== Results: $PASS passed, $FAIL failed, $TOTAL total ==="
    [[ $FAIL -eq 0 ]] && echo "ALL PASSED" || echo "SOME FAILED"
    exit "$FAIL"
}
trap cleanup EXIT

assert() {
    local desc="$1"; shift
    TOTAL=$((TOTAL + 1))
    if "$@"; then
        echo "  ✓ $desc"
        PASS=$((PASS + 1))
    else
        echo "  ✗ $desc"
        FAIL=$((FAIL + 1))
    fi
}

assert_file_contains() {
    local desc="$1" file="$2" pattern="$3"
    TOTAL=$((TOTAL + 1))
    if grep -q "$pattern" "$file" 2>/dev/null; then
        echo "  ✓ $desc"
        PASS=$((PASS + 1))
    else
        echo "  ✗ $desc (pattern '$pattern' not found in $file)"
        FAIL=$((FAIL + 1))
    fi
}

# ---------- Step 1: Build worker-forge ----------
echo "=== Step 1: Build worker-forge ==="
make build 2>&1 | sed 's/^/  /'
assert "worker-forge binary exists" test -x build/worker-forge

# ---------- Step 2: Create minimal test manifest ----------
echo ""
echo "=== Step 2: Create test manifest ==="
mkdir -p "$TEST_DIR/workers/e2etest"

cat > "$TEST_DIR/workers/e2etest/manifest.yaml" <<'MANIFEST'
name: e2etest
version: 0.0.1-test

vars:
  HOME:
    source: env
    required: true
    description: "home directory"
  CLAUDE_CONFIG_DIR:
    source: default
    default: "$HOME/.claude"
    required: true
    description: "claude config dir"
  BRIDGE_URL:
    source: flag
    default: "http://localhost:19999"
    required: true
    description: "bridge URL"
  TEST_DIR:
    source: env
    required: false
    default: "/tmp"
    description: "test scratch dir"

dirs:
  - $TEST_DIR/forge-e2e-workdir

files: []
tools: []
readiness: []
hooks: []
MANIFEST

assert "manifest created" test -f "$TEST_DIR/workers/e2etest/manifest.yaml"

# ---------- Step 3: Build test worker binary ----------
echo ""
echo "=== Step 3: Build test worker binary ==="
./build/worker-forge build e2etest \
    --manifest "$TEST_DIR/workers/e2etest/manifest.yaml" \
    --target linux/amd64 \
    --output "$TEST_DIR/build" 2>&1 | sed 's/^/  /'

WORKER_BIN="$TEST_DIR/build/e2etest-linux-amd64"
if [[ ! -x "$WORKER_BIN" ]]; then
    WORKER_BIN=$(find "$TEST_DIR/build" -name "e2etest*" -type f -executable 2>/dev/null | head -1)
fi
assert "worker binary built" test -x "$WORKER_BIN"

# ---------- Step 4: Start mock bridge ----------
echo ""
echo "=== Step 4: Start mock bridge ==="

MOCK_LOG="$TEST_DIR/mock-bridge.log"

# Find a free port
MOCK_PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()")

# Simple Python mock bridge: responds 200 to everything, logs requests
python3 -u - "$MOCK_PORT" "$MOCK_LOG" <<'PYEOF' &
import sys, json, http.server, threading

port = int(sys.argv[1])
logfile = sys.argv[2]

class MockBridge(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # silence default logging

    def _log(self, method, path, body=""):
        with open(logfile, "a") as f:
            f.write(f"{method} {path} {body}\n")

    def do_GET(self):
        self._log("GET", self.path)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode() if length else ""
        self._log("POST", self.path, body)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

server = http.server.HTTPServer(("127.0.0.1", port), MockBridge)
print(f"Mock bridge on :{port}", flush=True)
server.serve_forever()
PYEOF
MOCK_PID=$!

# Wait for mock to be ready
for i in $(seq 1 20); do
    if curl -sf "http://127.0.0.1:$MOCK_PORT/" >/dev/null 2>&1; then
        break
    fi
    sleep 0.1
done
assert "mock bridge started" curl -sf -o /dev/null "http://127.0.0.1:$MOCK_PORT/"

# ---------- Step 5: Run worker binary ----------
echo ""
echo "=== Step 5: Run worker binary ==="

# Kill any leftover session
tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true

export TEST_DIR
"$WORKER_BIN" --bridge-url "http://127.0.0.1:$MOCK_PORT" &
WORKER_PID=$!

# Wait for tmux session + bridge registration
sleep 3

# ---------- Step 6: Verify ----------
echo ""
echo "=== Step 6: Verify ==="

# 6a: tmux session created
assert "tmux session exists" tmux has-session -t "$SESSION_NAME"

# 6b: Worker registered with bridge
assert_file_contains "bridge received health check" "$MOCK_LOG" "GET /"
assert_file_contains "bridge received registration" "$MOCK_LOG" "POST /register"
assert_file_contains "registration contains worker name" "$MOCK_LOG" '"Name":"e2etest"'

# 6c: Working directory was created
assert "workdir created" test -d "$TEST_DIR/forge-e2e-workdir"

# 6d: Worker process is still alive (not crashed)
assert "worker process alive" kill -0 "$WORKER_PID"

# 6e: Wait for watchdog heartbeat (heartbeat uses GET /)
# Count GET / requests — should be >1 after watchdog tick
sleep 5
GET_COUNT=$(grep -c "^GET /" "$MOCK_LOG" 2>/dev/null || echo 0)
assert "watchdog heartbeats (GET / count > 1)" test "$GET_COUNT" -gt 1

echo ""
echo "=== Mock bridge log ==="
cat "$MOCK_LOG" | sed 's/^/  /'
