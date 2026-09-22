#!/usr/bin/env bash
set -euo pipefail

# E2E test: forge worker binary → real bridge (TRANSPORT=local)
# Proves the full loop: LocalTransport starts without BOT_TOKEN,
# forge binary registers via POST /register, watchdog heartbeats via GET /
#
# Usage: ./test-e2e-bridge.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$SCRIPT_DIR"

TEST_DIR=$(mktemp -d /tmp/forge-e2e-bridge-XXXXXX)
BRIDGE_PID=0
WORKER_PID=0
# The forge worker binary hardcodes "claude-prod-" + worker name
SESSION_NAME="claude-prod-e2etest"
# Use isolated prefix so the test bridge doesn't touch real prod sessions.
# The bridge only calls export_hook_env on sessions matching its TMUX_PREFIX.
TEST_TMUX_PREFIX="claude-forgetest-"
BRIDGE_PORT=0
PASS=0
FAIL=0
TOTAL=0

cleanup() {
    echo ""
    echo "=== Cleanup ==="
    [[ $WORKER_PID -gt 0 ]] && kill "$WORKER_PID" 2>/dev/null || true
    [[ $BRIDGE_PID -gt 0 ]] && kill "$BRIDGE_PID" 2>/dev/null || true
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

assert_log_contains() {
    local desc="$1" file="$2" pattern="$3"
    TOTAL=$((TOTAL + 1))
    if grep -q "$pattern" "$file" 2>/dev/null; then
        echo "  ✓ $desc"
        PASS=$((PASS + 1))
    else
        echo "  ✗ $desc (pattern '$pattern' not found)"
        FAIL=$((FAIL + 1))
    fi
}

# ---------- Step 1: Build worker-forge + test worker ----------
echo "=== Step 1: Build worker-forge ==="
make build 2>&1 | sed 's/^/  /'
assert "worker-forge binary exists" test -x build/worker-forge

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

# ---------- Step 4: Start real bridge with TRANSPORT=local ----------
echo ""
echo "=== Step 4: Start real bridge (TRANSPORT=local) ==="

BRIDGE_PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()")
BRIDGE_LOG="$TEST_DIR/bridge.log"
TRANSPORT_LOG="$TEST_DIR/transport.log"

TRANSPORT=local \
TRANSPORT_LOG="$TRANSPORT_LOG" \
PORT="$BRIDGE_PORT" \
SESSIONS_DIR="$TEST_DIR/sessions" \
TMUX_PREFIX="$TEST_TMUX_PREFIX" \
NODE_NAME="" \
PYTHONUNBUFFERED=1 \
python3 "$BRIDGE_DIR/bridge.py" > "$BRIDGE_LOG" 2>&1 &
BRIDGE_PID=$!

# Wait for bridge to be ready
for i in $(seq 1 40); do
    if curl -sf "http://127.0.0.1:$BRIDGE_PORT/" >/dev/null 2>&1; then
        break
    fi
    sleep 0.1
done

assert "real bridge started (TRANSPORT=local)" curl -sf -o /dev/null "http://127.0.0.1:$BRIDGE_PORT/"

# Verify it's the real bridge (returns JSON with endpoints)
HEALTH=$(curl -sf "http://127.0.0.1:$BRIDGE_PORT/" 2>/dev/null)
TOTAL=$((TOTAL + 1))
if echo "$HEALTH" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert 'endpoints' in d, 'missing endpoints'
assert 'POST /register' in d['endpoints'], 'missing /register endpoint'
" 2>/dev/null; then
    echo "  ✓ bridge returns API index with /register endpoint"
    PASS=$((PASS + 1))
else
    echo "  ✗ bridge returns API index with /register endpoint"
    FAIL=$((FAIL + 1))
fi

# ---------- Step 5: Run forge worker against real bridge ----------
echo ""
echo "=== Step 5: Run forge worker against real bridge ==="

tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true

export TEST_DIR
"$WORKER_BIN" --bridge-url "http://127.0.0.1:$BRIDGE_PORT" &
WORKER_PID=$!

sleep 3

# ---------- Step 6: Verify full lifecycle ----------
echo ""
echo "=== Step 6: Verify ==="

# 6a: tmux session created
assert "tmux session exists" tmux has-session -t "$SESSION_NAME"

# 6b: Bridge received registration (check bridge stdout log)
assert_log_contains "bridge logged forge registration" "$BRIDGE_LOG" "Forge worker registered: e2etest"

# 6c: POST /register returns 200 (direct test)
REG_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "http://127.0.0.1:$BRIDGE_PORT/register" \
    -H "Content-Type: application/json" \
    -d '{"Name":"manual-test","Host":"vps","Version":"0.0.1"}')
assert "POST /register returns 200" test "$REG_CODE" == "200"

# 6d: Working directory was created
assert "workdir created" test -d "$TEST_DIR/forge-e2e-workdir"

# 6e: Worker process is still alive
assert "worker process alive" kill -0 "$WORKER_PID"

# 6f: Wait for watchdog heartbeats
sleep 5
assert "worker still alive after 5s" kill -0 "$WORKER_PID"

# 6g: Bridge process is still alive
assert "bridge still alive" kill -0 "$BRIDGE_PID"

# 6h: LocalTransport log file has entries (if TRANSPORT_LOG was set)
if [[ -f "$TRANSPORT_LOG" ]]; then
    assert_log_contains "transport logged setup_commands" "$TRANSPORT_LOG" "setup_commands"
fi

echo ""
echo "=== Bridge log (last 20 lines) ==="
tail -20 "$BRIDGE_LOG" | sed 's/^/  /'

echo ""
echo "=== Transport log ==="
if [[ -f "$TRANSPORT_LOG" ]]; then
    cat "$TRANSPORT_LOG" | sed 's/^/  /'
else
    echo "  (no transport log)"
fi
