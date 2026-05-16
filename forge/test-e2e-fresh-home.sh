#!/usr/bin/env bash
set -euo pipefail

# E2E test: fresh (empty) HOME — full lifecycle.
# Proves the binary can bootstrap from scratch without any pre-existing config.
# Covers codex recommendation: "test-e2e-mon-fresh-home.sh"
#
# Tests: build → empty HOME → extract → hooks → verify → bridge → runtime → watchdog
#
# Usage: ./test-e2e-fresh-home.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$SCRIPT_DIR"

TEST_DIR=$(mktemp -d /tmp/forge-e2e-fresh-XXXXXX)
BRIDGE_PID=0
WORKER_PID=0
SESSION_NAME="claude-forgetest-freshworker"
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

# ---------- Step 2: Create manifest for fresh-home test ----------
echo ""
echo "=== Step 2: Create test manifest (fresh-home) ==="

WORKER_DIR="$TEST_DIR/workers/freshworker"
mkdir -p "$WORKER_DIR"

# Knowledge files
mkdir -p "$WORKER_DIR/knowledge"
echo "# Charter" > "$WORKER_DIR/knowledge/charter.md"
echo "# Playbook" > "$WORKER_DIR/knowledge/playbook.md"

# Skill directory
mkdir -p "$WORKER_DIR/skills/test-skill"
echo "# Test Skill" > "$WORKER_DIR/skills/test-skill/SKILL.md"

# Hook
mkdir -p "$WORKER_DIR/hooks"
cat > "$WORKER_DIR/hooks/test-hook.sh" << 'HOOKEOF'
#!/bin/sh
echo "hook fired"
HOOKEOF

# Manifest — no encrypted creds, no complex readiness
cat > "$WORKER_DIR/manifest.yaml" << 'MANIFEST'
name: freshworker
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
  - $HOME/.claude/skills
  - $HOME/knowledge

files:
  - source: knowledge/charter.md
    dest: $HOME/knowledge/charter.md
  - source: knowledge/playbook.md
    dest: $HOME/knowledge/playbook.md
  - source: skills/test-skill/
    dest: $HOME/.claude/skills/test-skill/

tools:
  - name: tmux
    check: tmux -V
    required: true

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

# ---------- Step 3: Build test binary ----------
echo ""
echo "=== Step 3: Build test worker binary ==="
./build/worker-forge build freshworker \
    --manifest "$WORKER_DIR/manifest.yaml" \
    --target linux/amd64 \
    --output "$TEST_DIR/build" 2>&1 | sed 's/^/  /'

WORKER_BIN="$TEST_DIR/build/freshworker-linux-amd64"
if [[ ! -x "$WORKER_BIN" ]]; then
    WORKER_BIN=$(find "$TEST_DIR/build" -name "freshworker*" -type f -executable 2>/dev/null | head -1)
fi
assert "worker binary built" test -x "$WORKER_BIN"

# ---------- Step 4: Start bridge ----------
echo ""
echo "=== Step 4: Start bridge (TRANSPORT=local) ==="

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

# ---------- Step 5: Run worker with COMPLETELY EMPTY HOME ----------
echo ""
echo "=== Step 5: Run worker (empty HOME) ==="

FRESH_HOME="$TEST_DIR/fresh-home"
mkdir -p "$FRESH_HOME"
# Verify it's truly empty
assert "HOME starts empty" test "$(ls -A "$FRESH_HOME" | wc -l)" -eq 0

tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true

WORKER_LOG="$TEST_DIR/worker.log"
HOME="$FRESH_HOME" \
"$WORKER_BIN" \
    --bridge-url "http://127.0.0.1:$BRIDGE_PORT" \
    --session-prefix "$TEST_TMUX_PREFIX" \
    > "$WORKER_LOG" 2>&1 &
WORKER_PID=$!

sleep 4

# ---------- Step 6: Verify everything was created from scratch ----------
echo ""
echo "=== Step 6: Verify extraction from empty HOME ==="

assert "worker process alive" kill -0 "$WORKER_PID"
assert "tmux session exists" tmux has-session -t "$SESSION_NAME"
assert_log_contains "bridge registered worker" "$BRIDGE_LOG" "Forge worker registered: freshworker"

# Files extracted
assert "charter.md created" test -f "$FRESH_HOME/knowledge/charter.md"
assert "playbook.md created" test -f "$FRESH_HOME/knowledge/playbook.md"
assert_file_contains "charter has content" "$FRESH_HOME/knowledge/charter.md" "# Charter"

# Dir source expanded
assert "skill SKILL.md created" test -f "$FRESH_HOME/.claude/skills/test-skill/SKILL.md"
assert_file_contains "SKILL.md has content" "$FRESH_HOME/.claude/skills/test-skill/SKILL.md" "# Test Skill"

# Hooks installed
assert "hook script created" test -f "$FRESH_HOME/.claude/hooks/test-hook.sh"

TOTAL=$((TOTAL + 1))
if [[ -x "$FRESH_HOME/.claude/hooks/test-hook.sh" ]]; then
    echo "  ✓ hook is executable"
    PASS=$((PASS + 1))
else
    echo "  ✗ hook is executable"
    FAIL=$((FAIL + 1))
fi

# Settings.json created from scratch (no pre-existing)
assert "settings.json created" test -f "$FRESH_HOME/.claude/settings.json"
assert_file_contains "settings.json has hook" "$FRESH_HOME/.claude/settings.json" "test-hook.sh"

# Dirs created
assert ".claude dir created" test -d "$FRESH_HOME/.claude"
assert ".claude/hooks dir created" test -d "$FRESH_HOME/.claude/hooks"
assert ".claude/skills dir created" test -d "$FRESH_HOME/.claude/skills"
assert "knowledge dir created" test -d "$FRESH_HOME/knowledge"

# ---------- Step 7: Verify mode ----------
echo ""
echo "=== Step 7: Verify mode ==="

kill "$WORKER_PID" 2>/dev/null || true
wait "$WORKER_PID" 2>/dev/null || true
WORKER_PID=0
tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true

VERIFY_OUTPUT=$(HOME="$FRESH_HOME" "$WORKER_BIN" --verify 2>&1 || true)
echo "$VERIFY_OUTPUT" | head -15 | sed 's/^/  /'

TOTAL=$((TOTAL + 1))
if echo "$VERIFY_OUTPUT" | grep -q "VERIFIED"; then
    echo "  ✓ verify mode VERIFIED"
    PASS=$((PASS + 1))
else
    echo "  ✗ verify mode VERIFIED"
    FAIL=$((FAIL + 1))
fi

# ---------- Step 8: Tamper detection ----------
echo ""
echo "=== Step 8: Tamper detection (--verify after file change) ==="

echo "TAMPERED" > "$FRESH_HOME/knowledge/charter.md"
TAMPER_OUTPUT=$(HOME="$FRESH_HOME" "$WORKER_BIN" --verify 2>&1 || true)

TOTAL=$((TOTAL + 1))
if echo "$TAMPER_OUTPUT" | grep -qi "mismatch\|error\|fail"; then
    echo "  ✓ verify detects tamper"
    PASS=$((PASS + 1))
else
    echo "  ✗ verify detects tamper (output: $TAMPER_OUTPUT)"
    FAIL=$((FAIL + 1))
fi

# ---------- Step 9: No files leaked outside fresh HOME ----------
echo ""
echo "=== Step 9: Isolation check ==="

# The worker should NOT have written files outside FRESH_HOME
TOTAL=$((TOTAL + 1))
REAL_HOME="/home/claude"
# Check for any files created by this test in the real home
# (This is a safety check — forge should respect HOME override)
if [[ ! -d "$REAL_HOME/.claude/skills/test-skill" ]]; then
    echo "  ✓ no files leaked to real HOME"
    PASS=$((PASS + 1))
else
    echo "  ✗ files leaked to real HOME"
    FAIL=$((FAIL + 1))
fi

# ---------- Step 10: Watchdog survival ----------
echo ""
echo "=== Step 10: Watchdog restart ==="

# Restart the worker and let watchdog tick
tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true
echo "# Charter" > "$FRESH_HOME/knowledge/charter.md"

HOME="$FRESH_HOME" \
"$WORKER_BIN" \
    --bridge-url "http://127.0.0.1:$BRIDGE_PORT" \
    --session-prefix "$TEST_TMUX_PREFIX" \
    --force-extract \
    > "$WORKER_LOG" 2>&1 &
WORKER_PID=$!

sleep 6
assert "worker alive after watchdog tick" kill -0 "$WORKER_PID"

echo ""
echo "=== Logs ==="
echo "--- Worker log ---"
cat "$WORKER_LOG" 2>/dev/null | sed 's/^/  /' || echo "  (empty)"
