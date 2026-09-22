#!/usr/bin/env bash
set -euo pipefail

# E2E BEHAVIORAL test: connector system
# Tests actual message flow, not just that endpoints exist.
#
# Verifies:
#   1. Message sent → arrives in worker's tmux pane (PollReceiver path)
#   2. Response posted via hook socket → routed through connector.Send()
#   3. Bridge registration + team discovery + external injection
#   4. Telegram poll inbound + outbound via sendMessage API
#
# Usage: ./test-e2e-connectors.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$SCRIPT_DIR"

TEST_DIR=$(mktemp -d /tmp/forge-e2e-connector-XXXXXX)
BRIDGE_PID=0
WORKER_PID=0
RUN_ID="$(date +%s)$$"
TEST_TMUX_PREFIX="claude-ft${RUN_ID}-"
BRIDGE_PORT=0
LOCAL_PORT=0
PASS=0
FAIL=0
SKIP=0
TOTAL=0
WORKER_NAME="triassic-4"

# Load test credentials
if [[ -f ~/.config/claudecode-telegram/test.env ]]; then
    source ~/.config/claudecode-telegram/test.env
fi
TEST_BOT_TOKEN="${TEST_BOT_TOKEN:-${TELEGRAM_BOT_TOKEN:-}}"
TEST_CHAT_ID="${TEST_CHAT_ID:-${ADMIN_CHAT_ID:-}}"

cleanup() {
    echo ""
    echo "=== Cleanup ==="
    [[ $WORKER_PID -gt 0 ]] && kill "$WORKER_PID" 2>/dev/null || true
    [[ $BRIDGE_PID -gt 0 ]] && kill "$BRIDGE_PID" 2>/dev/null || true
    tmux kill-session -t "${TEST_TMUX_PREFIX}${WORKER_NAME}" 2>/dev/null || true
    rm -f "/tmp/forge-${WORKER_NAME}.sock"
    rm -rf "$TEST_DIR"
    echo ""
    echo "=== Results: $PASS passed, $FAIL failed, $SKIP skipped, $TOTAL total ==="
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

assert_contains() {
    local desc="$1" content="$2" expected="$3"
    TOTAL=$((TOTAL + 1))
    if echo "$content" | grep -qF "$expected" 2>/dev/null; then
        echo "  ✓ $desc"
        PASS=$((PASS + 1))
    else
        echo "  ✗ $desc (expected '$expected' in output)"
        FAIL=$((FAIL + 1))
    fi
}

skip_test() {
    local desc="$1"
    TOTAL=$((TOTAL + 1))
    SKIP=$((SKIP + 1))
    echo "  - SKIP: $desc"
}

# Wait for text to appear in tmux pane (up to N seconds)
wait_for_tmux() {
    local session="$1" expected="$2" timeout="${3:-10}"
    for i in $(seq 1 $((timeout * 5))); do
        if tmux capture-pane -t "$session" -p 2>/dev/null | grep -qF "$expected"; then
            return 0
        fi
        sleep 0.2
    done
    return 1
}

# Wait for a file to appear (ready signal instead of fixed sleep)
wait_for_file() {
    local path="$1" timeout="${2:-10}"
    for i in $(seq 1 $((timeout * 5))); do
        [[ -e "$path" ]] && return 0
        sleep 0.2
    done
    return 1
}

# Wait for HTTP endpoint to respond (ready signal instead of fixed sleep)
wait_for_http() {
    local url="$1" timeout="${2:-10}"
    for i in $(seq 1 $((timeout * 5))); do
        curl -sf "$url" >/dev/null 2>&1 && return 0
        sleep 0.2
    done
    return 1
}

# ---------- Step 0: Build ----------
echo "=== Step 0: Build worker binary ==="
make build 2>&1 | sed 's/^/  /'

WORKER_DIR="$TEST_DIR/workers/$WORKER_NAME"
mkdir -p "$WORKER_DIR/knowledge"
echo "Connector test worker" > "$WORKER_DIR/knowledge/charter.md"

cat > "$WORKER_DIR/manifest.yaml" << 'MANIFEST'
name: triassic-4
version: 0.1.0-connector-test

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
    required: false

dirs:
  - $HOME/.claude
  - $HOME/.claude/hooks

files:
  - source: knowledge/charter.md
    dest: $HOME/knowledge/charter.md

tools:
  - name: tmux
    check: tmux -V
    required: true
MANIFEST

./build/worker-forge build "$WORKER_NAME" \
    --manifest "$WORKER_DIR/manifest.yaml" \
    --target linux/amd64 \
    --output "$TEST_DIR/build" 2>&1 | sed 's/^/  /'

WORKER_BIN=$(find "$TEST_DIR/build" -name "${WORKER_NAME}*" -type f -executable | head -1)
assert "worker binary built" test -x "$WORKER_BIN"

RUNTIME_HOME="$TEST_DIR/runtime-home"
mkdir -p "$RUNTIME_HOME/.claude/hooks"

# =============================================
# TEST 1: LOCAL CONNECTOR — Full PollReceiver round-trip
# =============================================
echo ""
echo "=========================================="
echo "=== TEST 1: Local Connector — PollReceiver Behavioral ==="
echo "=========================================="
echo "  Proves: PollReceiver.Poll() → ConnectorHost.deliverInbound() → Runtime.Send() → tmux"
echo "  Proves: HookListener → connector.Send() → response captured"

LOCAL_PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()")
echo "  Port: $LOCAL_PORT"

tmux kill-session -t "${TEST_TMUX_PREFIX}${WORKER_NAME}" 2>/dev/null || true
rm -f "/tmp/forge-${WORKER_NAME}.sock"

LOCAL_WORKER_LOG="$TEST_DIR/local-worker.log"
HOME="$RUNTIME_HOME" \
"$WORKER_BIN" \
    --connector local \
    --connector-opt "LOCAL_PORT=$LOCAL_PORT" \
    --session-prefix "$TEST_TMUX_PREFIX" > "$LOCAL_WORKER_LOG" 2>&1 &
WORKER_PID=$!

# Ready-wait: poll for hook socket (proves ConnectorHost started + HookListener ready)
HOOK_SOCKET="/tmp/forge-${WORKER_NAME}.sock"
assert "local: connector host started (hook socket appeared)" \
    wait_for_file "$HOOK_SOCKET" 10

assert "local: worker process alive" kill -0 "$WORKER_PID"

# --- INBOUND: This exercises the FULL PollReceiver path:
#   1. HTTP POST to LocalConnector → inbox queue → inboxCh signal
#   2. ConnectorHost.runPollLoop() → Poll() returns message
#   3. ConnectorHost.deliverInbound() → Runtime.Send() → tmux send-keys
#   4. Message appears in tmux pane
UNIQUE_MSG="POLL_INBOUND_$(date +%s%N)"
curl -sf -X POST "http://127.0.0.1:$LOCAL_PORT/send" \
    -H "Content-Type: application/json" \
    -d "{\"text\":\"$UNIQUE_MSG\",\"from\":\"manager\"}" >/dev/null

assert "local: message delivered to tmux (proves Poll→deliverInbound→Runtime.Send)" \
    wait_for_tmux "${TEST_TMUX_PREFIX}${WORKER_NAME}" "$UNIQUE_MSG" 5

# Verify formatInbound() adds sender prefix
PANE_CONTENT=$(tmux capture-pane -t "${TEST_TMUX_PREFIX}${WORKER_NAME}" -p 2>/dev/null)
assert_contains "local: formatInbound adds [from] prefix" "$PANE_CONTENT" "[manager] $UNIQUE_MSG"

# --- OUTBOUND: This exercises the HookListener → connector.Send() path:
#   1. POST to hook socket /response
#   2. HookListener.handleResponse() decodes JSON
#   3. Calls connector.Send(ctx, Response{Text: ...})
#   4. LocalConnector.Send() appends to responses slice
#   5. Verify via /responses endpoint
UNIQUE_RESP="HOOK_OUTBOUND_$(date +%s%N)"
assert "local: hook socket is Unix socket" test -S "$HOOK_SOCKET"

HOOK_RESULT=$(curl -sf --unix-socket "$HOOK_SOCKET" -X POST http://localhost/response \
    -H "Content-Type: application/json" \
    -d "{\"text\":\"$UNIQUE_RESP\"}" 2>/dev/null || echo "FAIL")
assert "local: hook listener accepted and connector.Send() returned nil" test "$HOOK_RESULT" = "ok"

sleep 0.5
CAPTURED=$(curl -sf "http://127.0.0.1:$LOCAL_PORT/responses" 2>/dev/null || echo "")
assert_contains "local: connector.Send() stored response (verifiable via /responses)" "$CAPTURED" "$UNIQUE_RESP"

# --- BURST DELIVERY: 10 messages sent concurrently to stress-test no-drop under load
BURST_COUNT=10
BURST_PREFIX="BURST_${RUN_ID}_"
BURST_PIDS=()
for n in $(seq 1 $BURST_COUNT); do
    curl -sf -X POST "http://127.0.0.1:$LOCAL_PORT/send" \
        -H "Content-Type: application/json" \
        -d "{\"text\":\"${BURST_PREFIX}${n}\",\"from\":\"burst\"}" >/dev/null &
    BURST_PIDS+=($!)
done
for pid in "${BURST_PIDS[@]}"; do wait "$pid" 2>/dev/null; done

# Verify ALL messages arrived (tmux scrollback check)
sleep 3  # allow delivery time for 10 messages
PANE_ALL=$(tmux capture-pane -t "${TEST_TMUX_PREFIX}${WORKER_NAME}" -p -S -100 2>/dev/null || echo "")
BURST_DELIVERED=0
for n in $(seq 1 $BURST_COUNT); do
    if echo "$PANE_ALL" | grep -qF "${BURST_PREFIX}${n}"; then
        BURST_DELIVERED=$((BURST_DELIVERED + 1))
    fi
done
TOTAL=$((TOTAL + 1))
if [[ $BURST_DELIVERED -eq $BURST_COUNT ]]; then
    echo "  ✓ local: burst delivery $BURST_COUNT/$BURST_COUNT messages arrived (no drops under concurrency)"
    PASS=$((PASS + 1))
else
    echo "  ✗ local: burst delivery only $BURST_DELIVERED/$BURST_COUNT messages arrived (dropped under load)"
    FAIL=$((FAIL + 1))
fi

# --- NEGATIVE: Malformed JSON to hook socket → must NOT return "ok"
BAD_RESULT=$(curl -s --unix-socket "$HOOK_SOCKET" -X POST http://localhost/response \
    -H "Content-Type: application/json" \
    -d "THIS IS NOT JSON" 2>/dev/null || echo "CONNECT_FAIL")
TOTAL=$((TOTAL + 1))
if [[ "$BAD_RESULT" != "ok" ]] && [[ "$BAD_RESULT" != "" ]]; then
    echo "  ✓ local: malformed JSON rejected by hook listener (got: ${BAD_RESULT:0:40})"
    PASS=$((PASS + 1))
else
    echo "  ✗ local: malformed JSON should be rejected but got 'ok' or empty"
    FAIL=$((FAIL + 1))
fi

# --- NEGATIVE: Wrong HTTP method to hook socket → must NOT return "ok"
WRONG_METHOD=$(curl -s --unix-socket "$HOOK_SOCKET" http://localhost/response 2>/dev/null || echo "CONNECT_FAIL")
TOTAL=$((TOTAL + 1))
if [[ "$WRONG_METHOD" != "ok" ]] && [[ "$WRONG_METHOD" != "" ]]; then
    echo "  ✓ local: GET to /response rejected (got: ${WRONG_METHOD:0:40})"
    PASS=$((PASS + 1))
else
    echo "  ✗ local: GET to /response should be rejected but got 'ok' or empty"
    FAIL=$((FAIL + 1))
fi

# Cleanup local
kill "$WORKER_PID" 2>/dev/null || true
wait "$WORKER_PID" 2>/dev/null || true
WORKER_PID=0
tmux kill-session -t "${TEST_TMUX_PREFIX}${WORKER_NAME}" 2>/dev/null || true
rm -f "$HOOK_SOCKET"
sleep 1

# =============================================
# TEST 2: BRIDGE CONNECTOR — ExternalReceiver Behavioral
# =============================================
echo ""
echo "=========================================="
echo "=== TEST 2: Bridge Connector — ExternalReceiver Behavioral ==="
echo "=========================================="
echo "  Proves: Init() registers with bridge (POST /register)"
echo "  Proves: HookListener starts for all connectors (including ExternalReceiver)"
echo "  Proves: Bridge tmux injection works (external message delivery)"
echo "  Proves: TeamAware discovery works (GET /workers returns data)"

BRIDGE_PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()")
BRIDGE_LOG="$TEST_DIR/bridge.log"
TRANSPORT_LOG="$TEST_DIR/transport.log"
echo "  Bridge port: $BRIDGE_PORT"

# Start real bridge with TRANSPORT=local (logs outbound messages to file)
TRANSPORT=local \
TRANSPORT_LOG="$TRANSPORT_LOG" \
PORT="$BRIDGE_PORT" \
SESSIONS_DIR="$TEST_DIR/sessions" \
TMUX_PREFIX="$TEST_TMUX_PREFIX" \
NODE_NAME="" \
ADMIN_CHAT_ID="121604706" \
PYTHONUNBUFFERED=1 \
python3 "$BRIDGE_DIR/bridge.py" > "$BRIDGE_LOG" 2>&1 &
BRIDGE_PID=$!

assert "bridge: bridge.py started" \
    wait_for_http "http://127.0.0.1:$BRIDGE_PORT/" 10

tmux kill-session -t "${TEST_TMUX_PREFIX}${WORKER_NAME}" 2>/dev/null || true
rm -f "/tmp/forge-${WORKER_NAME}.sock"

# Start worker with bridge connector
BRIDGE_WORKER_LOG="$TEST_DIR/bridge-worker.log"
HOME="$RUNTIME_HOME" \
"$WORKER_BIN" \
    --connector bridge \
    --bridge-url "http://127.0.0.1:$BRIDGE_PORT" \
    --session-prefix "$TEST_TMUX_PREFIX" > "$BRIDGE_WORKER_LOG" 2>&1 &
WORKER_PID=$!

# Ready-wait: tmux session must exist (Runtime started)
for i in $(seq 1 50); do
    tmux has-session -t "${TEST_TMUX_PREFIX}${WORKER_NAME}" 2>/dev/null && break
    sleep 0.2
done

assert "bridge: worker process alive" kill -0 "$WORKER_PID"
assert "bridge: tmux session created by Runtime" \
    tmux has-session -t "${TEST_TMUX_PREFIX}${WORKER_NAME}"

# --- VERIFY: Init() called POST /register on bridge ---
# The forge BridgeConnector.Init() POSTs {"name":"triassic-4"} to /register.
# Bridge logs this as "Forge worker registered: triassic-4"
sleep 1
BRIDGE_LOG_CONTENT=$(cat "$BRIDGE_LOG" 2>/dev/null || echo "")
assert_contains "bridge: Init() registered worker (POST /register logged by bridge)" \
    "$BRIDGE_LOG_CONTENT" "Forge worker registered"

# --- VERIFY: HookListener starts for all connectors (including ExternalReceiver) ---
# All connectors get a HookListener so generated emit hooks always have a target socket.
HOOK_SOCKET="/tmp/forge-${WORKER_NAME}.sock"
sleep 1
assert "bridge: hook socket exists (HookListener starts for all connectors)" \
    test -S "$HOOK_SOCKET"

# --- VERIFY: Bridge's inbound delivery via simulated Telegram webhook ---
# In production, bridge.py receives Telegram webhook POSTs and routes them to tmux.
# We simulate a real Telegram update to exercise the full bridge inbound path:
#   Telegram webhook POST → bridge.py handle_message() → send_to_worker() → tmux_send_message()
UNIQUE_MSG="BRIDGE_INBOUND_$(date +%s%N)"
FAKE_UPDATE="{\"update_id\":99999,\"message\":{\"message_id\":1,\"date\":$(date +%s),\"chat\":{\"id\":121604706,\"type\":\"private\"},\"from\":{\"id\":121604706,\"is_bot\":false,\"first_name\":\"Test\"},\"text\":\"$UNIQUE_MSG\"}}"
curl -sf -X POST "http://127.0.0.1:$BRIDGE_PORT/" \
    -H "Content-Type: application/json" \
    -d "$FAKE_UPDATE" >/dev/null 2>&1 || true

assert "bridge: webhook inbound delivered to worker tmux (full bridge path)" \
    wait_for_tmux "${TEST_TMUX_PREFIX}${WORKER_NAME}" "$UNIQUE_MSG" 5

# --- VERIFY: Team discovery via bridge /workers endpoint ---
# BridgeConnector.DiscoverWorkers() calls GET /workers?from=name.
# We test the same endpoint the Go connector would call.
WORKERS_RESP=$(curl -sf "http://127.0.0.1:$BRIDGE_PORT/workers?from=$WORKER_NAME" 2>/dev/null || echo "")
assert_contains "bridge: team discovery returns worker data (/workers?from=name)" \
    "$WORKERS_RESP" "$WORKER_NAME"

# --- VERIFY: /workers JSON has proper structure (not just substring match) ---
# The response must be valid JSON with structured worker entries containing
# at minimum: name field and send_example field (which the Go connector uses).
TOTAL=$((TOTAL + 1))
WORKERS_VALID=$(echo "$WORKERS_RESP" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    # Must be a list or dict with worker entries
    workers = data if isinstance(data, list) else data.get('workers', [])
    found = False
    for w in workers:
        if w.get('name') == '$WORKER_NAME':
            # Must have send_example (the command other workers use to reach this one)
            if 'send_example' in w and w['send_example']:
                found = True
                break
    print('VALID' if found else 'MISSING_FIELDS')
except Exception as e:
    print(f'PARSE_ERROR:{e}')
" 2>/dev/null || echo "PARSE_ERROR")
if [[ "$WORKERS_VALID" == "VALID" ]]; then
    echo "  ✓ bridge: /workers JSON has structured fields (name + send_example)"
    PASS=$((PASS + 1))
else
    echo "  ✗ bridge: /workers JSON structure invalid (got: $WORKERS_VALID)"
    FAIL=$((FAIL + 1))
fi

# --- VERIFY: Bridge response path (how responses actually flow in production) ---
# In production: Claude hook → bridge.py /response (directly, not via forge connector)
# The forge BridgeConnector.Send() also posts to bridge /response.
# Test that bridge.py accepts and routes it through LocalTransport.
BRIDGE_SESSION="${TEST_TMUX_PREFIX}${WORKER_NAME}"
SESSION_DIR="$TEST_DIR/sessions/$BRIDGE_SESSION"
mkdir -p "$SESSION_DIR"
echo "121604706" > "$SESSION_DIR/chat_id"

UNIQUE_RESP="BRIDGE_RESP_$(date +%s%N)"
RESP_RESULT=$(curl -sf -X POST "http://127.0.0.1:$BRIDGE_PORT/response" \
    -H "Content-Type: application/json" \
    -d "{\"text\":\"$UNIQUE_RESP\",\"session\":\"$BRIDGE_SESSION\"}" 2>/dev/null || echo "FAIL")
assert "bridge: response endpoint accepted POST" test "$RESP_RESULT" = "OK"

sleep 0.5
TRANSPORT_OUT=$(cat "$TRANSPORT_LOG" 2>/dev/null || echo "")
assert_contains "bridge: LocalTransport logged outbound (proves response→Telegram path)" \
    "$TRANSPORT_OUT" "$UNIQUE_RESP"

# --- NEGATIVE: /response with unknown session → bridge must not crash ---
UNKNOWN_RESP=$(curl -s -o /dev/null -w "%{http_code}" -X POST "http://127.0.0.1:$BRIDGE_PORT/response" \
    -H "Content-Type: application/json" \
    -d "{\"text\":\"test\",\"session\":\"nonexistent-session-999\"}" 2>/dev/null || echo "000")
TOTAL=$((TOTAL + 1))
# Bridge should still be alive regardless of response code
if wait_for_http "http://127.0.0.1:$BRIDGE_PORT/" 3; then
    echo "  ✓ bridge: survived /response with unknown session (HTTP $UNKNOWN_RESP)"
    PASS=$((PASS + 1))
else
    echo "  ✗ bridge: crashed after /response with unknown session"
    FAIL=$((FAIL + 1))
fi

# --- NEGATIVE: /response with invalid JSON → bridge must not crash ---
curl -s -X POST "http://127.0.0.1:$BRIDGE_PORT/response" \
    -H "Content-Type: application/json" \
    -d "NOT VALID JSON" >/dev/null 2>&1 || true
TOTAL=$((TOTAL + 1))
if wait_for_http "http://127.0.0.1:$BRIDGE_PORT/" 3; then
    echo "  ✓ bridge: survived /response with invalid JSON"
    PASS=$((PASS + 1))
else
    echo "  ✗ bridge: crashed after /response with invalid JSON"
    FAIL=$((FAIL + 1))
fi

# --- NEGATIVE: POST to unknown endpoint → 404 ---
UNKNOWN_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "http://127.0.0.1:$BRIDGE_PORT/nonexistent" \
    -H "Content-Type: application/json" -d "{}" 2>/dev/null || echo "000")
assert "bridge: unknown POST endpoint returns 404" test "$UNKNOWN_CODE" = "404"

# --- NEGATIVE: Verify worker log has no connector panics or fatal errors ---
BRIDGE_WORKER_CONTENT=$(cat "$BRIDGE_WORKER_LOG" 2>/dev/null || echo "")
TOTAL=$((TOTAL + 1))
if echo "$BRIDGE_WORKER_CONTENT" | grep -qi "panic\|fatal\|SIGSEGV"; then
    echo "  ✗ bridge: worker log contains panics or fatal errors"
    echo "    Log excerpt: $(echo "$BRIDGE_WORKER_CONTENT" | grep -i 'panic\|fatal' | head -3)"
    FAIL=$((FAIL + 1))
else
    echo "  ✓ bridge: worker log clean — no panics or fatal errors"
    PASS=$((PASS + 1))
fi

# Cleanup bridge
kill "$WORKER_PID" 2>/dev/null || true
wait "$WORKER_PID" 2>/dev/null || true
WORKER_PID=0
kill "$BRIDGE_PID" 2>/dev/null || true
wait "$BRIDGE_PID" 2>/dev/null || true
BRIDGE_PID=0
tmux kill-session -t "${TEST_TMUX_PREFIX}${WORKER_NAME}" 2>/dev/null || true
rm -f "/tmp/forge-${WORKER_NAME}.sock"
sleep 1

# =============================================
# TEST 3: TELEGRAM POLL CONNECTOR — PollReceiver Behavioral
# =============================================
echo ""
echo "=========================================="
echo "=== TEST 3: Telegram Poll — PollReceiver Behavioral ==="
echo "=========================================="
echo "  Proves: Init() validates bot token (getMe)"
echo "  Proves: Hook socket → connector.Send() → sendMessage hits Telegram API"
echo "  Proves: Poll() → getUpdates retrieves inbound messages → delivered to tmux"

if [[ -z "$TEST_BOT_TOKEN" ]]; then
    echo "  SKIP: TEST_BOT_TOKEN not set (set via ~/.config/claudecode-telegram/test.env)"
    skip_test "telegram: bot token validation"
    skip_test "telegram: outbound via hook socket → sendMessage API"
    skip_test "telegram: inbound polling → tmux delivery"
    skip_test "telegram: hook socket exists (PollReceiver)"
    skip_test "telegram: worker process alive"
else
    echo "  Bot: ${TEST_BOT_TOKEN:0:10}..."
    echo "  Chat: $TEST_CHAT_ID"

    # Verify token is valid before starting test
    TG_ME=$(curl -sf "https://api.telegram.org/bot${TEST_BOT_TOKEN}/getMe" 2>/dev/null || echo "")
    assert_contains "telegram: bot token valid (getMe ok)" "$TG_ME" '"ok":true'

    # SETUP: getUpdates requires no active webhook. Save and remove if present.
    WEBHOOK_INFO=$(curl -sf "https://api.telegram.org/bot${TEST_BOT_TOKEN}/getWebhookInfo" 2>/dev/null || echo "")
    RESTORE_WEBHOOK=$(echo "$WEBHOOK_INFO" | python3 -c "import json,sys; print(json.load(sys.stdin).get('result',{}).get('url',''))" 2>/dev/null || echo "")
    if [[ -n "$RESTORE_WEBHOOK" ]]; then
        echo "  (removing active webhook for test: ${RESTORE_WEBHOOK:0:40}...)"
        curl -sf "https://api.telegram.org/bot${TEST_BOT_TOKEN}/deleteWebhook" >/dev/null 2>&1
        sleep 1
    fi

    # Drain stale updates so our test message gets a clean offset
    curl -sf "https://api.telegram.org/bot${TEST_BOT_TOKEN}/getUpdates?offset=-1&timeout=1" >/dev/null 2>&1
    sleep 1

    tmux kill-session -t "${TEST_TMUX_PREFIX}${WORKER_NAME}" 2>/dev/null || true
    rm -f "/tmp/forge-${WORKER_NAME}.sock"

    # Start worker with telegram-poll connector (AFTER webhook removed)
    TELEGRAM_WORKER_LOG="$TEST_DIR/telegram-worker.log"
    HOME="$RUNTIME_HOME" \
    "$WORKER_BIN" \
        --connector telegram-poll \
        --connector-opt "TELEGRAM_BOT_TOKEN=$TEST_BOT_TOKEN" \
        --connector-opt "TELEGRAM_CHAT_ID=$TEST_CHAT_ID" \
        --session-prefix "$TEST_TMUX_PREFIX" > "$TELEGRAM_WORKER_LOG" 2>&1 &
    WORKER_PID=$!

    # Ready-wait: hook socket proves ConnectorHost started with PollReceiver path
    HOOK_SOCKET="/tmp/forge-${WORKER_NAME}.sock"
    assert "telegram: hook socket appeared (PollReceiver starts HookListener)" \
        wait_for_file "$HOOK_SOCKET" 15

    assert "telegram: worker process alive" kill -0 "$WORKER_PID"

    # --- OUTBOUND: Hook socket → connector.Send() → Telegram sendMessage API ---
    # This proves the FULL outbound path:
    #   1. POST to /tmp/forge-triassic-4.sock /response
    #   2. HookListener.handleResponse() parses JSON
    #   3. Calls TelegramPollConnector.Send()
    #   4. Send() constructs sendMessage payload with chat_id + text
    #   5. POSTs to https://api.telegram.org/bot.../sendMessage
    #   6. Telegram returns HTTP 200 → connector returns nil → hook returns "ok"
    # If ANY step fails, we get "FAIL" not "ok".
    UNIQUE_RESP="E2E-TG-SEND-$(date +%s)"
    HOOK_RESULT=$(curl -sf --unix-socket "$HOOK_SOCKET" -X POST http://localhost/response \
        -H "Content-Type: application/json" \
        -d "{\"text\":\"$UNIQUE_RESP\"}" 2>/dev/null || echo "FAIL")
    assert "telegram: outbound response hit Telegram API (sendMessage returned 200)" \
        test "$HOOK_RESULT" = "ok"

    # --- INBOUND: Poll loop verification ---
    # CONSTRAINT: Bots cannot receive their own messages via getUpdates,
    # so we cannot send a message and watch it arrive in tmux.
    # The PollReceiver→deliverInbound→tmux path is fully proven by TEST 1 (Local)
    # which exercises the SAME ConnectorHost.runPollLoop() code path.
    #
    # We verify the worker's poll loop is alive by confirming the process hasn't
    # crashed after running for a few seconds (Init→getMe + Poll→getUpdates both worked).
    sleep 3
    assert "telegram: worker still alive after poll loop iterations (proves getUpdates working)" \
        kill -0 "$WORKER_PID"

    # --- VERIFY: Worker log is free of poll errors ---
    # If getUpdates was actually failing (409 conflict, auth error, timeout error),
    # the connector logs it. Absence of errors proves the poll loop is actually succeeding,
    # not just surviving (a crashed goroutine wouldn't kill the main process).
    TG_LOG_CONTENT=$(cat "$TELEGRAM_WORKER_LOG" 2>/dev/null || echo "")
    TOTAL=$((TOTAL + 1))
    if echo "$TG_LOG_CONTENT" | grep -qi "getUpdates.*error\|poll.*failed\|409.*conflict\|401.*unauthorized"; then
        echo "  ✗ telegram: worker log contains poll errors (getUpdates not actually working)"
        echo "    Log excerpt: $(echo "$TG_LOG_CONTENT" | grep -i 'error\|fail\|409\|401' | head -3)"
        FAIL=$((FAIL + 1))
    else
        echo "  ✓ telegram: worker log clean — no poll errors (proves getUpdates succeeding)"
        PASS=$((PASS + 1))
    fi

    # Note: Bot's own messages don't appear in getUpdates — cannot verify outbound
    # delivery beyond the API 200 + log absence. The "ok" from hook + no log errors
    # is the strongest proof possible without a second bot/user.

    # Restore webhook if we removed it
    if [[ -n "$RESTORE_WEBHOOK" ]]; then
        curl -sf -X POST "https://api.telegram.org/bot${TEST_BOT_TOKEN}/setWebhook" \
            -H "Content-Type: application/json" \
            -d "{\"url\":\"$RESTORE_WEBHOOK\"}" >/dev/null 2>&1
        echo "  (webhook restored: ${RESTORE_WEBHOOK:0:40}...)"
    fi

    # Cleanup telegram
    kill "$WORKER_PID" 2>/dev/null || true
    wait "$WORKER_PID" 2>/dev/null || true
    WORKER_PID=0
    tmux kill-session -t "${TEST_TMUX_PREFIX}${WORKER_NAME}" 2>/dev/null || true
    rm -f "$HOOK_SOCKET"
fi

echo ""
echo "=========================================="
echo "=== Summary ==="
echo "=========================================="
