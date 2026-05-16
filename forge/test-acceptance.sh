#!/usr/bin/env bash
set -euo pipefail

# Acceptance test: real mon worker manifest → build → check → verify
# Tests the actual user workflow against the real mon manifest.yaml.
# Runs on VPS where source files and tools exist.
#
# Skip conditions:
#   - ~/team/mon/charter.md doesn't exist (not on VPS)
#   - Missing required source files
#
# Usage: ./test-acceptance.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$SCRIPT_DIR"

# Pre-flight: check we're on VPS with source files
if [[ ! -f "$HOME/team/mon/charter.md" ]]; then
    echo "SKIP: acceptance tests require VPS with ~/team/mon/charter.md"
    exit 0
fi

TEST_DIR=$(mktemp -d /tmp/forge-acceptance-XXXXXX)
BRIDGE_PID=0
WORKER_PID=0
SESSION_NAME="claude-forgetest-mon"
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

assert_output_contains() {
    local desc="$1" output="$2" pattern="$3"
    TOTAL=$((TOTAL + 1))
    if echo "$output" | grep -q "$pattern" 2>/dev/null; then
        echo "  ✓ $desc"
        PASS=$((PASS + 1))
    else
        echo "  ✗ $desc (pattern '$pattern' not found)"
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

# ---------- Step 1: Build worker-forge + mon binary ----------
echo "=== Step 1: Build worker-forge ==="
make build 2>&1 | sed 's/^/  /'
assert "worker-forge binary exists" test -x build/worker-forge

echo ""
echo "=== Step 2: Build mon binary (dummy creds) ==="

# Set dummy creds env vars for build (real creds not needed for acceptance test)
export ANTHROPIC_API_KEY="test-key-anthropic"
export GH_TOKEN="test-key-gh"
export STRIPE_RESTRICTED_KEY="test-key-stripe"
export OPENAI_ADMIN_KEY="test-key-openai"
export DEEPGRAM_API_KEY="test-key-deepgram"
export DEEPGRAM_PROJECT_ID="test-project-deepgram"
export MIXPANEL_SA_USERNAME="test-mixpanel-user"
export MIXPANEL_SA_SECRET="test-mixpanel-secret"
export SHOPIFY_ACCESS_TOKEN="test-shopify-token"
export GRAFANA_TOKEN="test-grafana-token"
export GRAFANA_URL="http://grafana.test"
export ANTHROPIC_ADMIN_KEY="test-key-anthropic-admin"
export SENTRY_AUTH_TOKEN="test-sentry-token"

# Generate a temporary age identity for encrypting creds
AGE_KEYGEN="${AGE_KEYGEN:-$(command -v age-keygen 2>/dev/null || echo "$HOME/go/bin/age-keygen")}"
if [[ ! -x "$AGE_KEYGEN" ]]; then
    echo "SKIP: age-keygen not found (install: go install filippo.io/age/cmd/...@latest)"
    exit 0
fi
AGE_KEY_FILE="$TEST_DIR/age-key.txt"
"$AGE_KEYGEN" -o "$AGE_KEY_FILE" 2>/dev/null

./build/worker-forge build mon \
    --manifest workers/mon/manifest.yaml \
    --identity "$AGE_KEY_FILE" \
    --target linux/amd64 \
    --output "$TEST_DIR/build" 2>&1 | sed 's/^/  /'

MON_BIN="$TEST_DIR/build/mon-linux-amd64"
if [[ ! -x "$MON_BIN" ]]; then
    MON_BIN=$(find "$TEST_DIR/build" -name "mon*" -type f -executable 2>/dev/null | head -1)
fi
assert "mon binary built" test -x "$MON_BIN"

# ---------- Step 3: Check mode ----------
echo ""
echo "=== Step 3: Check mode (--check) ==="

# --check doesn't need a real bridge, but needs BRIDGE_URL set for var resolution
# Use bridge port for readiness check to pass
BRIDGE_PORT_CHECK=$(python3 -c "import socket; s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()")
TRANSPORT=local PORT="$BRIDGE_PORT_CHECK" SESSIONS_DIR="$TEST_DIR/sessions-check" TMUX_PREFIX="claude-forgetest-" NODE_NAME="" PYTHONUNBUFFERED=1 \
python3 "$BRIDGE_DIR/bridge.py" > /dev/null 2>&1 &
CHECK_BRIDGE_PID=$!
for i in $(seq 1 40); do curl -sf "http://127.0.0.1:$BRIDGE_PORT_CHECK/" >/dev/null 2>&1 && break; sleep 0.1; done

CHECK_OUTPUT=$(HOME="$TEST_DIR/check-home" "$MON_BIN" --check --bridge-url "http://127.0.0.1:$BRIDGE_PORT_CHECK" --identity "$AGE_KEY_FILE" 2>&1 || true)
kill "$CHECK_BRIDGE_PID" 2>/dev/null || true
echo "$CHECK_OUTPUT" | head -30 | sed 's/^/  /'

# Check mode may fail on readiness (gcloud/kubectl need real auth) but should
# still show partial output. Test that the binary runs and produces output.
TOTAL=$((TOTAL + 1))
if [[ -n "$CHECK_OUTPUT" ]]; then
    echo "  ✓ check mode produced output"
    PASS=$((PASS + 1))
else
    echo "  ✗ check mode produced output"
    FAIL=$((FAIL + 1))
fi

# If check passed fully, verify the report
if echo "$CHECK_OUTPUT" | grep -q "Worker: mon"; then
    assert_output_contains "check shows worker name" "$CHECK_OUTPUT" "Worker: mon"
    assert_output_contains "check shows version" "$CHECK_OUTPUT" "2.0.0"
    assert_output_contains "check shows Tools section" "$CHECK_OUTPUT" "Tools:"
    assert_output_contains "check shows Status" "$CHECK_OUTPUT" "Status:"
else
    # Readiness checks failed — expected in isolated test env
    echo "  ⊘ check mode readiness failed (expected — auth tools not configured)"
fi

# ---------- Step 4: Start isolated bridge ----------
echo ""
echo "=== Step 4: Start isolated bridge ==="

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

# ---------- Step 5: Run mon binary (isolated) ----------
echo ""
echo "=== Step 5: Run mon binary ==="

RUNTIME_HOME="$TEST_DIR/runtime-home"
mkdir -p "$RUNTIME_HOME"

tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true

WORKER_LOG="$TEST_DIR/worker.log"
HOME="$RUNTIME_HOME" \
"$MON_BIN" \
    --bridge-url "http://127.0.0.1:$BRIDGE_PORT" \
    --session-prefix "$TEST_TMUX_PREFIX" \
    --identity "$AGE_KEY_FILE" \
    > "$WORKER_LOG" 2>&1 &
WORKER_PID=$!

sleep 5

# ---------- Step 6: Verify extraction ----------
echo ""
echo "=== Step 6: Verify extraction ==="

# Worker may have exited due to readiness failures (gcloud/kubectl/gh auth).
# Extract + hooks still run, so check those.
WORKER_ALIVE=false
if kill -0 "$WORKER_PID" 2>/dev/null; then
    WORKER_ALIVE=true
fi

if $WORKER_ALIVE; then
    assert "tmux session exists" tmux has-session -t "$SESSION_NAME"
    assert "worker process alive" kill -0 "$WORKER_PID"
    assert_log_contains "bridge registered mon" "$BRIDGE_LOG" "Forge worker registered: mon"
else
    echo "  ⊘ worker exited (expected — readiness checks require real auth)"
    if [[ -f "$WORKER_LOG" ]]; then
        echo "  Worker exit reason:"
        tail -3 "$WORKER_LOG" | sed 's/^/    /'
    fi
fi

assert "charter.md extracted" \
    test -f "$RUNTIME_HOME/team/mon/charter.md"
assert_file_contains "charter.md has content" \
    "$RUNTIME_HOME/team/mon/charter.md" "mon"

assert "playbook.md extracted" \
    test -f "$RUNTIME_HOME/team/mon/playbook.md"

assert "team playbook extracted" \
    test -f "$RUNTIME_HOME/team/playbook.md"

assert "team learnings extracted" \
    test -f "$RUNTIME_HOME/team/learnings.md"

# Skills directories (dir sources)
TOTAL=$((TOTAL + 1))
SKILL_COUNT=$(find "$RUNTIME_HOME/.claude/skills/" -name "SKILL.md" 2>/dev/null | wc -l)
if [[ $SKILL_COUNT -ge 5 ]]; then
    echo "  ✓ skills extracted ($SKILL_COUNT SKILL.md files found)"
    PASS=$((PASS + 1))
else
    echo "  ✗ skills extracted (found $SKILL_COUNT SKILL.md files, want ≥5)"
    FAIL=$((FAIL + 1))
fi

assert "CLAUDE.md extracted" \
    test -f "$RUNTIME_HOME/.claude/CLAUDE.md"

# Hooks
assert "checkin-on-start.sh hook exists" \
    test -f "$RUNTIME_HOME/.claude/hooks/checkin-on-start.sh"
assert "send-to-telegram.sh hook exists" \
    test -f "$RUNTIME_HOME/.claude/hooks/send-to-telegram.sh"

assert "settings.json created" \
    test -f "$RUNTIME_HOME/.claude/settings.json"

# ---------- Step 7: Verify mode ----------
echo ""
echo "=== Step 7: Verify mode (--verify) ==="

# Kill the worker first — verify mode is a separate invocation
kill "$WORKER_PID" 2>/dev/null || true
wait "$WORKER_PID" 2>/dev/null || true
WORKER_PID=0
tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true

VERIFY_OUTPUT=$(HOME="$RUNTIME_HOME" "$MON_BIN" --verify --identity "$AGE_KEY_FILE" 2>&1 || true)
echo "$VERIFY_OUTPUT" | head -20 | sed 's/^/  /'

assert_output_contains "verify shows worker name" "$VERIFY_OUTPUT" "Worker: mon"
assert_output_contains "verify shows VERIFIED" "$VERIFY_OUTPUT" "VERIFIED"

echo ""
echo "=== Logs ==="
echo "--- Worker log (last 10 lines) ---"
tail -10 "$WORKER_LOG" 2>/dev/null | sed 's/^/  /' || echo "  (empty)"
echo "--- Bridge log (last 10 lines) ---"
tail -10 "$BRIDGE_LOG" 2>/dev/null | sed 's/^/  /' || echo "  (empty)"
