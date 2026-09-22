#!/usr/bin/env bash
set -euo pipefail

# E2E test: full lifecycle with directory sources, hooks, merge files,
# conflict detection, and watchdog. Tests against TRANSPORT=local bridge.
#
# Usage: ./test-e2e-full.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$SCRIPT_DIR"

TEST_DIR=$(mktemp -d /tmp/forge-e2e-full-XXXXXX)
BRIDGE_PID=0
WORKER_PID=0
SESSION_NAME="claude-forgetest-e2efull"
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

assert_file_contains() {
    local desc="$1" file="$2" expected="$3"
    TOTAL=$((TOTAL + 1))
    if [[ -f "$file" ]] && grep -qF "$expected" "$file" 2>/dev/null; then
        echo "  ✓ $desc"
        PASS=$((PASS + 1))
    else
        echo "  ✗ $desc (file=$file, expected='$expected')"
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

# ---------- Step 1: Build worker-forge ----------
echo "=== Step 1: Build worker-forge ==="
make build 2>&1 | sed 's/^/  /'
assert "worker-forge binary exists" test -x build/worker-forge

# ---------- Step 2: Create test manifest + source files ----------
echo ""
echo "=== Step 2: Create test manifest + source files ==="

WORKER_DIR="$TEST_DIR/workers/e2efull"
mkdir -p "$WORKER_DIR"

# Single file: knowledge/charter.md
mkdir -p "$WORKER_DIR/knowledge"
echo "Team charter v1 — the definitive guide" > "$WORKER_DIR/knowledge/charter.md"

# Directory source: skills/test-skill/ with nested subdir
mkdir -p "$WORKER_DIR/skills/test-skill/sub"
echo "# Test Skill" > "$WORKER_DIR/skills/test-skill/SKILL.md"
echo '#!/bin/bash' > "$WORKER_DIR/skills/test-skill/helpers.sh"
echo "nested content" > "$WORKER_DIR/skills/test-skill/sub/nested.txt"

# Merge file source: memory/state.md (will be pre-planted on disk)
mkdir -p "$WORKER_DIR/memory"
echo "default state" > "$WORKER_DIR/memory/state.md"

# Hook source
mkdir -p "$WORKER_DIR/hooks"
cat > "$WORKER_DIR/hooks/test-hook.sh" << 'HOOKEOF'
#!/bin/sh
echo "hook fired"
HOOKEOF
chmod 755 "$WORKER_DIR/hooks/test-hook.sh"

# Manifest
cat > "$WORKER_DIR/manifest.yaml" << 'MANIFEST'
name: e2efull
version: 0.1.0-test

vars:
  HOME:
    source: env
    required: true
  CLAUDE_CONFIG_DIR:
    source: default
    default: "$HOME/.claude"
    required: true
  BRIDGE_URL:
    source: flag
    default: "http://localhost:19999"
    required: true

dirs:
  - $HOME/.claude/hooks

files:
  - source: knowledge/charter.md
    dest: $HOME/knowledge/charter.md
  - source: skills/test-skill/
    dest: $HOME/.claude/skills/test-skill/
  - source: memory/state.md
    dest: $HOME/memory/state.md
    merge: true

tools:
  - name: tmux
    check: tmux -V
    required: true
  - name: curl
    check: curl --version
    required: false

readiness:
  - name: bridge-reachable
    check: curl -sf $BRIDGE_URL/
    expect: exit 0
    required: true

hooks:
  - event: Stop
    command: $CLAUDE_CONFIG_DIR/hooks/test-hook.sh
    source: hooks/test-hook.sh
MANIFEST

assert "manifest created" test -f "$WORKER_DIR/manifest.yaml"
assert "skill dir source exists" test -d "$WORKER_DIR/skills/test-skill"
assert "hook source exists" test -f "$WORKER_DIR/hooks/test-hook.sh"

# ---------- Step 3: Build test worker binary ----------
echo ""
echo "=== Step 3: Build test worker binary ==="
./build/worker-forge build e2efull \
    --manifest "$WORKER_DIR/manifest.yaml" \
    --target linux/amd64 \
    --output "$TEST_DIR/build" 2>&1 | sed 's/^/  /'

WORKER_BIN="$TEST_DIR/build/e2efull-linux-amd64"
if [[ ! -x "$WORKER_BIN" ]]; then
    WORKER_BIN=$(find "$TEST_DIR/build" -name "e2efull*" -type f -executable 2>/dev/null | head -1)
fi
assert "worker binary built" test -x "$WORKER_BIN"

# ---------- Step 4: Start real bridge ----------
echo ""
echo "=== Step 4: Start real bridge (TRANSPORT=local) ==="

BRIDGE_PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()")
BRIDGE_LOG="$TEST_DIR/bridge.log"

TRANSPORT=local \
PORT="$BRIDGE_PORT" \
SESSIONS_DIR="$TEST_DIR/sessions" \
TMUX_PREFIX="$TEST_TMUX_PREFIX" \
NODE_NAME="" \
PYTHONUNBUFFERED=1 \
python3 "$BRIDGE_DIR/bridge.py" > "$BRIDGE_LOG" 2>&1 &
BRIDGE_PID=$!

for i in $(seq 1 40); do
    if curl -sf "http://127.0.0.1:$BRIDGE_PORT/" >/dev/null 2>&1; then
        break
    fi
    sleep 0.1
done
assert "bridge started" curl -sf -o /dev/null "http://127.0.0.1:$BRIDGE_PORT/"

# ---------- Step 5: Pre-plant merge file ----------
echo ""
echo "=== Step 5: Pre-plant merge file ==="

RUNTIME_HOME="$TEST_DIR/runtime-home"
mkdir -p "$RUNTIME_HOME/memory"
echo "ORIGINAL local state — do not overwrite" > "$RUNTIME_HOME/memory/state.md"
assert "merge file pre-planted" test -f "$RUNTIME_HOME/memory/state.md"

# ---------- Step 6: Run forge worker ----------
echo ""
echo "=== Step 6: Run forge worker ==="

tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true

HOME="$RUNTIME_HOME" \
"$WORKER_BIN" \
    --bridge-url "http://127.0.0.1:$BRIDGE_PORT" \
    --session-prefix "$TEST_TMUX_PREFIX" &
WORKER_PID=$!

sleep 4

# ---------- Step 7: Verify extraction ----------
echo ""
echo "=== Step 7: Verify extraction ==="

assert "charter.md extracted" \
    test -f "$RUNTIME_HOME/knowledge/charter.md"
assert_file_contains "charter.md has correct content" \
    "$RUNTIME_HOME/knowledge/charter.md" "Team charter v1"

assert "SKILL.md extracted (dir source)" \
    test -f "$RUNTIME_HOME/.claude/skills/test-skill/SKILL.md"
assert_file_contains "SKILL.md has correct content" \
    "$RUNTIME_HOME/.claude/skills/test-skill/SKILL.md" "# Test Skill"

assert "helpers.sh extracted (dir source)" \
    test -f "$RUNTIME_HOME/.claude/skills/test-skill/helpers.sh"

assert "nested.txt extracted (nested subdir)" \
    test -f "$RUNTIME_HOME/.claude/skills/test-skill/sub/nested.txt"
assert_file_contains "nested.txt has correct content" \
    "$RUNTIME_HOME/.claude/skills/test-skill/sub/nested.txt" "nested content"

assert_file_contains "merge file preserved original content" \
    "$RUNTIME_HOME/memory/state.md" "ORIGINAL local state"

assert "hook extracted" \
    test -f "$RUNTIME_HOME/.claude/hooks/test-hook.sh"

# Check hook is executable
TOTAL=$((TOTAL + 1))
if [[ -x "$RUNTIME_HOME/.claude/hooks/test-hook.sh" ]]; then
    echo "  ✓ hook is executable"
    PASS=$((PASS + 1))
else
    echo "  ✗ hook is executable"
    FAIL=$((FAIL + 1))
fi

assert "settings.json created" \
    test -f "$RUNTIME_HOME/.claude/settings.json"
assert_file_contains "settings.json references hook" \
    "$RUNTIME_HOME/.claude/settings.json" "test-hook.sh"

# ---------- Step 8: Verify runtime ----------
echo ""
echo "=== Step 8: Verify runtime ==="

assert "tmux session exists" tmux has-session -t "$SESSION_NAME"
assert "worker process alive" kill -0 "$WORKER_PID"
assert_log_contains "bridge logged registration" "$BRIDGE_LOG" "Forge worker registered: e2efull"

# ---------- Step 9: Verify watchdog ----------
echo ""
echo "=== Step 9: Verify watchdog (5s wait) ==="
sleep 5
assert "worker still alive after watchdog ticks" kill -0 "$WORKER_PID"
assert "bridge still alive" kill -0 "$BRIDGE_PID"

# ---------- Step 10: Conflict detection ----------
echo ""
echo "=== Step 10: Conflict detection ==="

# Kill the first worker
kill "$WORKER_PID" 2>/dev/null || true
wait "$WORKER_PID" 2>/dev/null || true
WORKER_PID=0
tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true

# Modify charter.md to create a conflict
echo "TAMPERED charter" > "$RUNTIME_HOME/knowledge/charter.md"

# Run second worker — should fail with conflict error
CONFLICT_LOG="$TEST_DIR/conflict.log"
HOME="$RUNTIME_HOME" \
"$WORKER_BIN" \
    --bridge-url "http://127.0.0.1:$BRIDGE_PORT" \
    --session-prefix "$TEST_TMUX_PREFIX" \
    > "$CONFLICT_LOG" 2>&1 &
CONFLICT_PID=$!

sleep 3
# Check if process exited (conflict should cause exit)
TOTAL=$((TOTAL + 1))
if ! kill -0 "$CONFLICT_PID" 2>/dev/null; then
    echo "  ✓ worker exited on conflict"
    PASS=$((PASS + 1))
else
    echo "  ✗ worker should have exited on conflict (still running)"
    kill "$CONFLICT_PID" 2>/dev/null || true
    FAIL=$((FAIL + 1))
fi
assert_log_contains "conflict error reported" "$CONFLICT_LOG" "conflict"

# Run with --force-extract — should succeed
tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true
FORCE_LOG="$TEST_DIR/force.log"
HOME="$RUNTIME_HOME" \
"$WORKER_BIN" \
    --bridge-url "http://127.0.0.1:$BRIDGE_PORT" \
    --session-prefix "$TEST_TMUX_PREFIX" \
    --force-extract \
    > "$FORCE_LOG" 2>&1 &
WORKER_PID=$!

sleep 4
assert "worker alive with --force-extract" kill -0 "$WORKER_PID"
assert_file_contains "charter.md overwritten by force-extract" \
    "$RUNTIME_HOME/knowledge/charter.md" "Team charter v1"

echo ""
echo "=== Logs ==="
echo "--- Bridge log (last 10 lines) ---"
tail -10 "$BRIDGE_LOG" 2>/dev/null | sed 's/^/  /' || echo "  (empty)"
echo "--- Conflict log ---"
cat "$CONFLICT_LOG" 2>/dev/null | sed 's/^/  /' || echo "  (empty)"
