#!/usr/bin/env bash
# End-to-end verification pipeline for spawn-on-demand tool servers.
# Tests real server spawn, token auth, content serving, and TTL self-destruct.
#
# Usage: ./tests/test_tools_e2e.sh
# Exit: 0 if all pass, 1 on first failure
set -euo pipefail

cd "$(dirname "$0")/.."
PASS=0
FAIL=0
PIDS=()

cleanup() {
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    done
}
trap cleanup EXIT

ok() { echo -e "\033[0;32m✓\033[0m $1"; PASS=$((PASS+1)); }
fail() { echo -e "\033[0;31m✗\033[0m $1: $2"; FAIL=$((FAIL+1)); }

# Wait for a URL to return 200 with optional content check
wait_ready() {
    local url="$1" pattern="${2:-}" tries=50
    for i in $(seq 1 $tries); do
        local body
        body=$(curl -sf "$url" 2>/dev/null) || { sleep 0.1; continue; }
        if [[ -z "$pattern" ]] || echo "$body" | grep -q "$pattern"; then
            return 0
        fi
        sleep 0.1
    done
    return 1
}

# Check bun is available
if ! command -v bun &>/dev/null; then
    echo "SKIP: bun not installed"
    exit 0
fi

# ────────────────────────────────────────────────────────────────────
# Setup: create test data
# ────────────────────────────────────────────────────────────────────

TEST_TMPDIR=$(mktemp -d)
trap "rm -rf $TEST_TMPDIR; cleanup" EXIT

# Create minimal JSONL transcript for rewind
cat > "$TEST_TMPDIR/test.jsonl" << 'JSONL'
{"type":"summary","uuid":"aaaa","message":{"role":"user","content":"hello world"},"timestamp":"2026-01-01T00:00:00Z","cwd":"/tmp","cost_usd":0.01,"duration_ms":1000}
{"type":"summary","uuid":"bbbb","message":{"role":"assistant","content":[{"type":"text","text":"Hi there!"}]},"timestamp":"2026-01-01T00:00:01Z","cwd":"/tmp","cost_usd":0.02,"duration_ms":2000}
{"type":"summary","uuid":"cccc","message":{"role":"user","content":"search test query"},"timestamp":"2026-01-01T00:00:02Z","cwd":"/tmp","cost_usd":0.01,"duration_ms":500}
JSONL

# Create minimal HTML for review
cat > "$TEST_TMPDIR/review.html" << 'HTML'
<!DOCTYPE html><html><head><title>PR Review Test</title></head><body><h1>PR #42 Review</h1></body></html>
HTML

TOKEN="test-token-$(openssl rand -hex 8)"

# ════════════════════════════════════════════════════════════════════
# TEST 1: Rewind — spawn, auth, content, API, search
# ════════════════════════════════════════════════════════════════════

echo ""
echo "── Rewind Server E2E ──────────────────────────────────────────"

REWIND_PORT=10177
bun rewind/src/rewind.ts \
    --port "$REWIND_PORT" --token "$TOKEN" --ttl 30 \
    --jsonl-path "$TEST_TMPDIR/test.jsonl" --name testworker --session-id test-session-123 \
    &>/dev/null &
PIDS+=($!)
# Wait for server to be ready with indexed data
wait_ready "http://localhost:$REWIND_PORT/?token=$TOKEN" '"entries"'

# 1a. No token → 403
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$REWIND_PORT/")
if [[ "$STATUS" == "403" ]]; then
    ok "Rewind: no token → 403"
else
    fail "Rewind: no token" "expected 403, got $STATUS"
fi

# 1b. Wrong token → 403
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$REWIND_PORT/?token=wrong")
if [[ "$STATUS" == "403" ]]; then
    ok "Rewind: wrong token → 403"
else
    fail "Rewind: wrong token" "expected 403, got $STATUS"
fi

# 1c. Valid token → 200 HTML
_RESP_FILE="$TEST_TMPDIR/resp_1c"
STATUS=$(curl -s -o "$_RESP_FILE" -w "%{http_code}" "http://localhost:$REWIND_PORT/?token=$TOKEN")
if [[ "$STATUS" == "200" ]] && grep -q "testworker" "$_RESP_FILE"; then
    ok "Rewind: valid token → 200 HTML with worker name"
else
    fail "Rewind: valid token" "status=$STATUS, body check failed"
fi

# 1d. HTML page embeds transcript data (entries served via page, not separate API)
_RESP_FILE="$TEST_TMPDIR/resp_1d"
curl -s -o "$_RESP_FILE" "http://localhost:$REWIND_PORT/?token=$TOKEN"
if grep -q '"entries"' "$_RESP_FILE"; then
    ok "Rewind: HTML page embeds transcript entries"
else
    fail "Rewind: HTML entries" "page doesn't contain embedded entries JSON"
fi

# 1e. Search via page query param — FTS5 returns matching entries in embedded JSON
_RESP_FILE="$TEST_TMPDIR/resp_1e"
curl -s -o "$_RESP_FILE" "http://localhost:$REWIND_PORT/?token=$TOKEN&q=search+test"
if grep -q "search test query" "$_RESP_FILE"; then
    ok "Rewind: search via ?q= returns matching content"
else
    fail "Rewind: search" "search page doesn't contain expected content"
fi

# 1f. API stats
STATS=$(curl -s "http://localhost:$REWIND_PORT/api/stats?token=$TOKEN")
if echo "$STATS" | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'input_tokens' in d or 'output_tokens' in d" 2>/dev/null; then
    ok "Rewind: /api/stats returns stats with token fields"
else
    fail "Rewind: /api/stats" "unexpected response: ${STATS:0:200}"
fi

# 1g. API updates
UPDATES=$(curl -s "http://localhost:$REWIND_PORT/api/updates?token=$TOKEN&since=0")
if echo "$UPDATES" | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'total' in d" 2>/dev/null; then
    ok "Rewind: /api/updates returns update count"
else
    fail "Rewind: /api/updates" "unexpected response: ${UPDATES:0:200}"
fi

# Kill rewind
kill "${PIDS[-1]}" 2>/dev/null; wait "${PIDS[-1]}" 2>/dev/null || true
unset 'PIDS[-1]'

# ════════════════════════════════════════════════════════════════════
# TEST 2: Review — spawn, auth, HTML serving, POST auth
# ════════════════════════════════════════════════════════════════════

echo ""
echo "── Review Server E2E ──────────────────────────────────────────"

REVIEW_PORT=10178
bun review/src/serve.ts \
    --port "$REVIEW_PORT" --token "$TOKEN" --ttl 30 \
    --html-path "$TEST_TMPDIR/review.html" --owner TestOrg --repo test-repo --pr-num 42 \
    &>/dev/null &
PIDS+=($!)
wait_ready "http://localhost:$REVIEW_PORT/health"

# 2a. Health endpoint — no auth required
HEALTH=$(curl -s "http://localhost:$REVIEW_PORT/health")
if echo "$HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['ok']" 2>/dev/null; then
    ok "Review: /health returns ok (no auth)"
else
    fail "Review: /health" "unexpected: ${HEALTH:0:200}"
fi

# 2b. No token → 403
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$REVIEW_PORT/")
if [[ "$STATUS" == "403" ]]; then
    ok "Review: no token → 403"
else
    fail "Review: no token" "expected 403, got $STATUS"
fi

# 2c. Valid token → 200 HTML with PR content
_RESP_FILE="$TEST_TMPDIR/resp_2c"
curl -s -o "$_RESP_FILE" "http://localhost:$REVIEW_PORT/?token=$TOKEN"
if grep -q "PR #42 Review" "$_RESP_FILE"; then
    ok "Review: valid token → serves PR HTML"
else
    fail "Review: valid token" "HTML content check failed"
fi

# 2d. /tools/review/<num> path also works
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$REVIEW_PORT/tools/review/42?token=$TOKEN")
if [[ "$STATUS" == "200" ]]; then
    ok "Review: /tools/review/<num> path → 200"
else
    fail "Review: /tools/review path" "expected 200, got $STATUS"
fi

# 2e. POST comment without token → 403
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    -H "Content-Type: application/json" \
    -d '{"body":"test"}' \
    "http://localhost:$REVIEW_PORT/tools/review/comments")
if [[ "$STATUS" == "403" ]]; then
    ok "Review: POST comment without token → 403"
else
    fail "Review: POST comment auth" "expected 403, got $STATUS"
fi

# 2f. POST merge without token → 403
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    -H "Content-Type: application/json" \
    -d '{"merge_method":"squash"}' \
    "http://localhost:$REVIEW_PORT/tools/review/merges")
if [[ "$STATUS" == "403" ]]; then
    ok "Review: POST merge without token → 403"
else
    fail "Review: POST merge auth" "expected 403, got $STATUS"
fi

# Kill review
kill "${PIDS[-1]}" 2>/dev/null; wait "${PIDS[-1]}" 2>/dev/null || true
unset 'PIDS[-1]'

# ════════════════════════════════════════════════════════════════════
# TEST 3: Pilot — Node.js runtime (Bun's node-pty can't attach to tmux)
# ════════════════════════════════════════════════════════════════════

echo ""
echo "── Pilot Server E2E ───────────────────────────────────────────"

PILOT_PORT=10179
if [[ -f "pilot/pilot.js" ]]; then
    node pilot/pilot.js --port "$PILOT_PORT" --token "$TOKEN" --ttl 30 &>/dev/null &
    PIDS+=($!)
    sleep 2  # Pilot needs extra startup time for node-pty

    # 3a. Pilot runs under Node.js (production runtime)
    if kill -0 "${PIDS[-1]}" 2>/dev/null; then
        ok "Pilot: spawns successfully under Node.js"
    else
        fail "Pilot: spawn" "process exited immediately"
    fi

    # 3b. Unauthorized request → rejected
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$PILOT_PORT/" 2>/dev/null || echo "000")
    if [[ "$STATUS" != "200" ]]; then
        ok "Pilot: unauthorized request rejected"
    else
        fail "Pilot: auth" "expected non-200, got $STATUS"
    fi

    # 3c. Authenticated request → 200 HTML
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$PILOT_PORT/?token=$TOKEN" 2>/dev/null || echo "000")
    if [[ "$STATUS" == "200" ]]; then
        ok "Pilot: authenticated request → 200"
    else
        fail "Pilot: auth access" "expected 200, got $STATUS"
    fi

    # Kill pilot
    kill "${PIDS[-1]}" 2>/dev/null; wait "${PIDS[-1]}" 2>/dev/null || true
    unset 'PIDS[-1]'
else
    echo "⚠ pilot/pilot.js not found — skipping pilot tests"
fi

# ════════════════════════════════════════════════════════════════════
# TEST 4: TTL self-destruct
# ════════════════════════════════════════════════════════════════════

echo ""
echo "── TTL Self-Destruct E2E ──────────────────────────────────────"

TTL_PORT=10176
bun rewind/src/rewind.ts \
    --port "$TTL_PORT" --token "$TOKEN" --ttl 3 \
    --jsonl-path "$TEST_TMPDIR/test.jsonl" --name ttltest --session-id ttl-123 \
    &>/dev/null &
TTL_PID=$!
PIDS+=($TTL_PID)
wait_ready "http://localhost:$TTL_PORT/?token=$TOKEN"

# Verify it's alive
if curl -s -o /dev/null -w "%{http_code}" "http://localhost:$TTL_PORT/?token=$TOKEN" | grep -q "200"; then
    ok "TTL: server alive initially"
else
    fail "TTL: initial check" "server not responding"
fi

# Wait for TTL to expire (3s + 2s buffer)
sleep 5

# Verify it's dead
if kill -0 "$TTL_PID" 2>/dev/null; then
    fail "TTL: self-destruct" "process still alive after TTL"
    kill "$TTL_PID" 2>/dev/null
else
    ok "TTL: server self-destructed after 3s idle"
fi

# ════════════════════════════════════════════════════════════════════
# TEST 5: ToolManager spawn integration (Python)
# ════════════════════════════════════════════════════════════════════

echo ""
echo "── ToolManager Integration ────────────────────────────────────"

# Verify ToolManager resolves correct paths
python3 -c "
import os, sys
sys.path.insert(0, '.')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'fake:token')
os.environ.setdefault('ADMIN_CHAT_ID', '')
os.environ.setdefault('NODE_NAME', 'test')
import bridge
bridge_dir = os.path.dirname(os.path.abspath(bridge.__file__))

# Rewind path
rewind_path = os.path.join(bridge_dir, 'rewind', 'src', 'rewind.ts')
assert os.path.exists(rewind_path), f'Rewind not found: {rewind_path}'

# Review generate path
review_gen = os.path.join(bridge_dir, 'review', 'generate.py')
assert os.path.exists(review_gen), f'Review generate not found: {review_gen}'

# Review serve path
review_serve = os.path.join(bridge_dir, 'review', 'src', 'serve.ts')
assert os.path.exists(review_serve), f'Review serve not found: {review_serve}'

# Pilot path (built JS — production uses Node.js, not Bun)
pilot_path = os.path.join(bridge_dir, 'pilot', 'pilot.js')
assert os.path.exists(pilot_path), f'Pilot not found: {pilot_path}'

print('All tool paths resolve correctly')
" 2>&1
if [[ $? -eq 0 ]]; then
    ok "ToolManager: all script paths resolve correctly"
else
    fail "ToolManager: path resolution" "see output above"
fi

# ════════════════════════════════════════════════════════════════════
# Summary
# ════════════════════════════════════════════════════════════════════

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
TOTAL=$((PASS+FAIL))
echo "  $PASS/$TOTAL passed, $FAIL failed"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [[ $FAIL -gt 0 ]]; then
    exit 1
fi
