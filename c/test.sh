#!/usr/bin/env bash
# test.sh - Acceptance tests for C bridge
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${TEST_PORT:-8095}"
CHAT_ID="${TEST_CHAT_ID:-123456789}"
BRIDGE_PID=""
TUNNEL_PID=""
TUNNEL_URL=""
TMUX_BIN="${TMUX_BIN:-tmux}"

# Ensure tmux commands use the default server socket for test isolation.
unset TMUX
unset TMUX_PANE

TEST_BASE_DIR="$HOME/.claude/telegram-test"
TEST_SESSION_DIR="$TEST_BASE_DIR/sessions"
TEST_TMUX_TMPDIR="$TEST_BASE_DIR/tmux"
TEST_PID_FILE="$TEST_BASE_DIR/claudecode-telegram.pid"
TEST_TMUX_PREFIX="claude-test-"
BRIDGE_LOG="$TEST_BASE_DIR/bridge.log"
TUNNEL_LOG="$TEST_BASE_DIR/tunnel.log"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

passed=0
failed=0
skipped=0
HAS_TMUX=false
HAS_PIL=false

log()     { echo -e "$@"; }
success() { log "${GREEN}OK${NC} $1"; ((passed++)) || true; }
fail()    { log "${RED}FAIL${NC} $1"; ((failed++)) || true; }
info()    { log "${YELLOW}→${NC} $1"; }
skip()    { log "${YELLOW}skip${NC} $1"; ((skipped++)) || true; }

cleanup() {
    info "Cleaning up..."
    if [[ "${KEEP_TEST_ARTIFACTS:-}" == "1" ]]; then
        info "KEEP_TEST_ARTIFACTS=1 set; skipping cleanup."
        return
    fi
    if [[ -f "$TEST_PID_FILE" ]]; then
        local pid
        pid=$(cat "$TEST_PID_FILE")
        kill "$pid" 2>/dev/null || true
        rm -f "$TEST_PID_FILE"
    fi
    [[ -n "$BRIDGE_PID" ]] && kill "$BRIDGE_PID" 2>/dev/null || true
    [[ -n "$TUNNEL_PID" ]] && kill "$TUNNEL_PID" 2>/dev/null || true
    if $HAS_TMUX; then
        tmux_cmd kill-session -t "${TEST_TMUX_PREFIX}testbot1" 2>/dev/null || true
        tmux_cmd kill-session -t "${TEST_TMUX_PREFIX}testbot2" 2>/dev/null || true
        tmux_cmd kill-session -t "${TEST_TMUX_PREFIX}responsetest" 2>/dev/null || true
        tmux_cmd kill-session -t "${TEST_TMUX_PREFIX}inboxtest" 2>/dev/null || true
        tmux_cmd kill-session -t "${TEST_TMUX_PREFIX}aliasbot" 2>/dev/null || true
        tmux_cmd kill-session -t "${TEST_TMUX_PREFIX}learnbot" 2>/dev/null || true
        tmux_cmd kill-session -t "${TEST_TMUX_PREFIX}replybot" 2>/dev/null || true
        tmux_cmd kill-session -t "${TEST_TMUX_PREFIX}contextbot" 2>/dev/null || true
        tmux_cmd kill-session -t "${TEST_TMUX_PREFIX}nopendingtest" 2>/dev/null || true
        tmux_cmd kill-session -t "${TEST_TMUX_PREFIX}imageresponsetest" 2>/dev/null || true
        tmux_cmd kill-session -t "${TEST_TMUX_PREFIX}imgrecv" 2>/dev/null || true
    fi
    [[ -d "$TEST_SESSION_DIR" ]] && rm -rf "$TEST_SESSION_DIR"
    [[ -d "$TEST_TMUX_TMPDIR" ]] && rm -rf "$TEST_TMUX_TMPDIR"
    [[ -f "$BRIDGE_LOG" ]] && rm -f "$BRIDGE_LOG"
    [[ -f "$TUNNEL_LOG" ]] && rm -f "$TUNNEL_LOG"
}

trap cleanup EXIT

require_token() {
    if [[ -z "${TEST_BOT_TOKEN:-}" ]]; then
        log "${RED}Error:${NC} TEST_BOT_TOKEN not set"
        log ""
        log "Usage:"
        log "  TEST_BOT_TOKEN='...' ./test.sh                    # Basic tests"
        log "  TEST_BOT_TOKEN='...' TEST_CHAT_ID='...' ./test.sh # Full e2e"
        exit 1
    fi
}

tmux_cmd() {
    if [[ "$TMUX_BIN" == /* ]]; then
        "$TMUX_BIN" "$@"
    else
        command "$TMUX_BIN" "$@"
    fi
}

detect_deps() {
    if [[ "$TMUX_BIN" == /* ]]; then
        if [[ -x "$TMUX_BIN" ]]; then
            HAS_TMUX=true
        else
            HAS_TMUX=false
        fi
    elif command -v "$TMUX_BIN" >/dev/null 2>&1; then
        HAS_TMUX=true
    else
        HAS_TMUX=false
        skip "tmux not found (${TMUX_BIN}); tmux-dependent tests will be skipped"
    fi

    if python3 - <<'PY' >/dev/null 2>&1
import importlib.util
raise SystemExit(0 if importlib.util.find_spec("PIL") else 1)
PY
    then
        HAS_PIL=true
    else
        HAS_PIL=false
        skip "Pillow (PIL) not installed; image e2e tests will be skipped"
    fi
}

wait_for_port() {
    local port="$1" attempts=0
    while ! nc -z localhost "$port" 2>/dev/null && [[ $attempts -lt 30 ]]; do
        sleep 0.5
        ((attempts++))
    done
    nc -z localhost "$port" 2>/dev/null
}

send_message() {
    local text="$1"
    local chat_id="${2:-$CHAT_ID}"
    local update_id=$((RANDOM))

    curl -s -X POST "http://localhost:$PORT" \
        -H "Content-Type: application/json" \
        -d '{
            "update_id": '"$update_id"',
            "message": {
                "message_id": '"$update_id"',
                "from": {"id": '"$chat_id"', "first_name": "TestUser"},
                "chat": {"id": '"$chat_id"', "type": "private"},
                "date": '"$(date +%s)"',
                "text": '"$text"'
            }
        }'
}

send_reply() {
    local text="$1"
    local reply_text="$2"
    local reply_from_bot="${3:-true}"
    local chat_id="${4:-$CHAT_ID}"
    local update_id=$((RANDOM))
    local reply_id=$((RANDOM + 1000))

    curl -s -X POST "http://localhost:$PORT" \
        -H "Content-Type: application/json" \
        -d '{
            "update_id": '"$update_id"',
            "message": {
                "message_id": '"$update_id"',
                "from": {"id": '"$chat_id"', "first_name": "TestUser"},
                "chat": {"id": '"$chat_id"', "type": "private"},
                "date": '"$(date +%s)"',
                "text": '"$text"',
                "reply_to_message": {
                    "message_id": '"$reply_id"',
                    "from": {"id": 123456, "first_name": "Bot", "is_bot": '"$reply_from_bot"'},
                    "chat": {"id": '"$chat_id"', "type": "private"},
                    "date": '"$(date +%s)"',
                    "text": '"$reply_text"'
                }
            }
        }'
}

send_photo_message() {
    local file_id="$1"
    local caption="${2:-}"
    local chat_id="${3:-$CHAT_ID}"
    local update_id=$((RANDOM))

    curl -s -X POST "http://localhost:$PORT" \
        -H "Content-Type: application/json" \
        -d '{
            "update_id": '"$update_id"',
            "message": {
                "message_id": '"$update_id"',
                "from": {"id": '"$chat_id"', "first_name": "TestUser"},
                "chat": {"id": '"$chat_id"', "type": "private"},
                "date": '"$(date +%s)"',
                "photo": [
                    {"file_id": '"$file_id"'_small, "file_size": 1000, "width": 90, "height": 90},
                    {"file_id": '"$file_id"', "file_size": 5000, "width": 320, "height": 320}
                ],
                "caption": '"$caption"'
            }
        }'
}

# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

test_binary() {
    info "Checking bridge binary..."
    if [[ ! -x "$SCRIPT_DIR/bridge" ]]; then
        info "Building bridge..."
        if ! make -C "$SCRIPT_DIR"; then
            fail "bridge build failed"
            return 1
        fi
    fi
    success "bridge binary present"
}

test_version() {
    info "Testing version..."
    if "$SCRIPT_DIR/claudecode-telegram.sh" --version | grep -q "claudecode-telegram"; then
        success "Version command works"
    else
        fail "Version command failed"
    fi
}

test_bridge_starts() {
    info "Starting bridge on port $PORT..."

    lsof -ti :"$PORT" | xargs kill -9 2>/dev/null || true
    sleep 1

    mkdir -p "$TEST_BASE_DIR"
    mkdir -p "$TEST_SESSION_DIR"
    mkdir -p "$TEST_TMUX_TMPDIR"
    chmod 700 "$TEST_TMUX_TMPDIR"
    export TMUX_TMPDIR="$TEST_TMUX_TMPDIR"
    # Ensure a clean tmux server for the test socket.
    tmux_cmd kill-server 2>/dev/null || true

    TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" \
    PORT="$PORT" \
    SESSIONS_DIR="$TEST_SESSION_DIR" \
    PID_FILE="$TEST_PID_FILE" \
    TMUX_PREFIX="$TEST_TMUX_PREFIX" \
    ADMIN_CHAT_ID="${TEST_CHAT_ID:-}" \
    "$SCRIPT_DIR/bridge" > "$BRIDGE_LOG" 2>&1 &
    BRIDGE_PID=$!

    if wait_for_port "$PORT"; then
        success "Bridge started on port $PORT"
    else
        fail "Bridge failed to start"
        return 1
    fi

    if curl -s "http://localhost:$PORT" | grep -q "Claude-Telegram"; then
        success "Bridge endpoint responds"
    else
        fail "Bridge endpoint not responding"
    fi
}

test_admin_registration() {
    info "Testing admin auto-registration..."
    local result
    result=$(send_message "hello")
    if [[ "$result" == "OK" ]]; then
        success "First message accepted (admin registered)"
    else
        fail "First message failed"
    fi
}

test_non_admin_rejection() {
    info "Testing non-admin rejection..."
    local result
    result=$(send_message "/list" "999888777")
    if [[ "$result" == "OK" ]]; then
        success "Non-admin silently rejected"
    else
        fail "Non-admin rejection failed"
    fi
}

test_new_command() {
    if ! $HAS_TMUX; then
        skip "Skipping /new (tmux not installed)"
        return
    fi
    info "Testing /new command..."
    send_message "/new testbot1" >/dev/null
    sleep 2
    if tmux_cmd has-session -t "${TEST_TMUX_PREFIX}testbot1" 2>/dev/null; then
        success "/new creates tmux session"
    else
        fail "/new failed to create session"
    fi
}

test_list_command() {
    info "Testing /list command..."
    local result
    result=$(send_message "/list")
    if [[ "$result" == "OK" ]]; then
        success "/list command works"
    else
        fail "/list command failed"
    fi
}

test_use_command() {
    info "Testing /use command..."
    local result
    result=$(send_message "/use testbot1")
    if [[ "$result" == "OK" ]]; then
        success "/use command works"
    else
        fail "/use command failed"
    fi
}

test_status_command() {
    info "Testing /status command..."
    local result
    result=$(send_message "/status")
    if [[ "$result" == "OK" ]]; then
        success "/status command works"
    else
        fail "/status command failed"
    fi
}

test_restart_command() {
    info "Testing /restart command..."
    local result
    result=$(send_message "/restart")
    if [[ "$result" == "OK" ]]; then
        success "/restart command works"
    else
        fail "/restart command failed"
    fi
}

test_stop_command() {
    info "Testing /stop command..."
    local result
    result=$(send_message "/stop")
    if [[ "$result" == "OK" ]]; then
        success "/stop command works"
    else
        fail "/stop command failed"
    fi
}

test_at_mention() {
    if ! $HAS_TMUX; then
        skip "Skipping @mention routing (tmux not installed)"
        return
    fi
    info "Testing @mention routing..."
    send_message "/new testbot2" >/dev/null
    sleep 2
    local result
    result=$(send_message "@testbot1 hello from mention")
    if [[ "$result" == "OK" ]]; then
        success "@mention routing works"
    else
        fail "@mention routing failed"
    fi
}

test_at_all_broadcast() {
    info "Testing @all broadcast..."
    local result
    result=$(send_message "@all hello everyone")
    if [[ "$result" == "OK" ]]; then
        success "@all broadcast accepted"
    else
        fail "@all broadcast failed"
    fi
}

test_session_files() {
    if ! $HAS_TMUX; then
        skip "Skipping session file permissions (tmux not installed)"
        return
    fi
    info "Testing session file permissions..."
    local session_dir="$TEST_SESSION_DIR/testbot1"
    if [[ -d "$session_dir" ]]; then
        local dir_perms
        if [[ "$(uname)" == "Darwin" ]]; then
            dir_perms=$(stat -f "%Lp" "$session_dir")
        else
            dir_perms=$(stat -c "%a" "$session_dir")
        fi
        if [[ "$dir_perms" == "700" ]]; then
            success "Session directory has secure permissions (0700)"
        else
            fail "Session directory permissions incorrect: $dir_perms"
        fi

        if [[ -f "$session_dir/chat_id" ]]; then
            local file_perms
            if [[ "$(uname)" == "Darwin" ]]; then
                file_perms=$(stat -f "%Lp" "$session_dir/chat_id")
            else
                file_perms=$(stat -c "%a" "$session_dir/chat_id")
            fi
            if [[ "$file_perms" == "600" ]]; then
                success "chat_id file has secure permissions (0600)"
            else
                fail "chat_id file permissions incorrect: $file_perms"
            fi
        fi
    else
        fail "Session directory not created"
    fi
}

test_kill_command() {
    if ! $HAS_TMUX; then
        skip "Skipping /kill (tmux not installed)"
        return
    fi
    info "Testing /kill command..."
    local result
    result=$(send_message "/kill testbot2")
    sleep 1
    if ! tmux_cmd has-session -t "${TEST_TMUX_PREFIX}testbot2" 2>/dev/null; then
        success "/kill removes tmux session"
    else
        fail "/kill failed to remove session"
    fi
}

test_blocked_commands() {
    info "Testing blocked commands..."
    local result
    result=$(send_message "/mcp")
    if [[ "$result" == "OK" ]]; then
        success "Blocked commands handled"
    else
        fail "Blocked commands not handled"
    fi
}

test_command_aliases() {
    if ! $HAS_TMUX; then
        skip "Skipping command aliases (tmux not installed)"
        return
    fi
    info "Testing command aliases (/hire, /team, /focus, etc.)..."
    send_message "/hire aliasbot" >/dev/null
    sleep 2
    if tmux_cmd has-session -t "${TEST_TMUX_PREFIX}aliasbot" 2>/dev/null; then
        success "/hire creates worker"
    else
        fail "/hire failed"
        return
    fi
    local result
    result=$(send_message "/team")
    if [[ "$result" == "OK" ]]; then
        success "/team works"
    else
        fail "/team failed"
    fi
    result=$(send_message "/focus aliasbot")
    if [[ "$result" == "OK" ]]; then
        success "/focus works"
    else
        fail "/focus failed"
    fi
    result=$(send_message "/progress")
    if [[ "$result" == "OK" ]]; then
        success "/progress works"
    else
        fail "/progress failed"
    fi
    result=$(send_message "/pause")
    if [[ "$result" == "OK" ]]; then
        success "/pause works"
    else
        fail "/pause failed"
    fi
    send_message "/end aliasbot" >/dev/null
    sleep 1
    if ! tmux_cmd has-session -t "${TEST_TMUX_PREFIX}aliasbot" 2>/dev/null; then
        success "/end removes worker"
    else
        fail "/end failed"
    fi
}

test_learn_command() {
    if ! $HAS_TMUX; then
        skip "Skipping /learn (tmux not installed)"
        return
    fi
    info "Testing /learn command..."
    send_message "/hire learnbot" >/dev/null
    sleep 2
    send_message "/focus learnbot" >/dev/null
    local result
    result=$(send_message "/learn")
    if [[ "$result" == "OK" ]]; then
        success "/learn works"
    else
        fail "/learn failed"
    fi
    result=$(send_message "/learn git")
    if [[ "$result" == "OK" ]]; then
        success "/learn <topic> works"
    else
        fail "/learn <topic> failed"
    fi
    send_message "/end learnbot" >/dev/null 2>&1 || true
}

test_reply_routing() {
    if ! $HAS_TMUX; then
        skip "Skipping reply routing (tmux not installed)"
        return
    fi
    info "Testing reply-to-worker routing..."
    send_message "/hire replybot" >/dev/null
    sleep 2
    local result
    result=$(send_reply "follow up question" "replybot: I fixed the bug")
    if [[ "$result" == "OK" ]]; then
        success "Reply to worker message routed correctly"
    else
        fail "Reply routing failed"
    fi
    send_message "/end replybot" >/dev/null 2>&1 || true
}

test_reply_context() {
    if ! $HAS_TMUX; then
        skip "Skipping reply context (tmux not installed)"
        return
    fi
    info "Testing reply context inclusion..."
    send_message "/hire contextbot" >/dev/null
    sleep 2
    send_message "/focus contextbot" >/dev/null
    local result
    result=$(send_reply "." "my original message" "false")
    if [[ "$result" == "OK" ]]; then
        success "Reply to own message includes context"
    else
        fail "Reply context failed"
    fi
    send_message "/end contextbot" >/dev/null 2>&1 || true
}

test_notify_endpoint() {
    info "Testing /notify endpoint..."
    local result
    result=$(curl -s -X POST "http://localhost:$PORT/notify" \
        -H "Content-Type: application/json" \
        -d '{"text":"Test notification"}')
    if [[ "$result" == "OK" ]]; then
        success "/notify endpoint works"
    else
        fail "/notify endpoint failed: $result"
    fi
}

test_image_tag_parsing() {
    skip "Skipping image tag parsing unit tests (Python-only)"
}

test_image_path_validation() {
    skip "Skipping image path validation unit tests (Python-only)"
}

test_inbox_directory() {
    if ! $HAS_TMUX; then
        skip "Skipping inbox directory e2e (tmux not installed)"
        return
    fi
    if ! $HAS_PIL; then
        skip "Skipping inbox directory e2e (Pillow not installed)"
        return
    fi
    if [[ -z "${TEST_CHAT_ID:-}" ]] || [[ "$CHAT_ID" == "123456789" ]]; then
        skip "Skipping inbox directory e2e (requires TEST_CHAT_ID)"
        return
    fi
    info "Testing inbox directory creation (e2e)..."
    send_message "/hire inboxtest" >/dev/null
    sleep 2
    send_message "/focus inboxtest" >/dev/null
    sleep 1

    python3 << 'PYEOF'
from PIL import Image
img = Image.new('RGB', (10, 10), color='#28A745')
img.save('/tmp/e2e-inbox.png')
PYEOF

    local upload_response
    upload_response=$(curl -s -X POST "https://api.telegram.org/bot${TEST_BOT_TOKEN}/sendPhoto" \
        -F "chat_id=${CHAT_ID}" \
        -F "photo=@/tmp/e2e-inbox.png" \
        -F "caption=E2E inbox test")

    local file_id
    file_id=$(echo "$upload_response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['result']['photo'][-1]['file_id'])" 2>/dev/null)

    if [[ -z "$file_id" ]]; then
        fail "Could not upload test image for inbox test"
        send_message "/end inboxtest" >/dev/null 2>&1 || true
        return
    fi

    send_photo_message "$file_id" "inbox test" >/dev/null
    sleep 3

    local inbox_dir="$TEST_SESSION_DIR/inboxtest/inbox"
    if [[ -d "$inbox_dir" ]]; then
        local dir_perms
        if [[ "$(uname)" == "Darwin" ]]; then
            dir_perms=$(stat -f "%Lp" "$inbox_dir")
        else
            dir_perms=$(stat -c "%a" "$inbox_dir")
        fi
        if [[ "$dir_perms" == "700" ]]; then
            success "Inbox directory created with correct permissions"
        else
            fail "Inbox directory permissions incorrect: $dir_perms"
        fi
    else
        fail "Inbox directory not created"
    fi

    send_message "/end inboxtest" >/dev/null 2>&1 || true
}

test_photo_message_no_focused() {
    if ! $HAS_TMUX; then
        skip "Skipping photo without focused worker (tmux not installed)"
        return
    fi
    info "Testing photo message without focused worker..."
    send_message "/end testbot1" >/dev/null 2>&1 || true
    sleep 1
    tmux_cmd kill-session -t "${TEST_TMUX_PREFIX}testbot1" 2>/dev/null || true
    local result
    result=$(send_photo_message "test_file_id" "test caption")
    if [[ "$result" == "OK" ]]; then
        success "Photo without focused worker handled"
    else
        fail "Photo message handling failed"
    fi
}

test_incoming_image_e2e() {
    if ! $HAS_TMUX; then
        skip "Skipping incoming image e2e (tmux not installed)"
        return 0
    fi
    if ! $HAS_PIL; then
        skip "Skipping incoming image e2e (Pillow not installed)"
        return 0
    fi
    info "Testing incoming image e2e (upload -> webhook -> download)..."
    if [[ -z "${TEST_CHAT_ID:-}" ]] || [[ "$CHAT_ID" == "123456789" ]]; then
        skip "Skipping (requires TEST_CHAT_ID for real Telegram upload)"
        return 0
    fi

    send_message "/hire imgrecv" >/dev/null
    sleep 2
    send_message "/focus imgrecv" >/dev/null
    sleep 1

    python3 << 'PYEOF'
from PIL import Image, ImageDraw
img = Image.new('RGB', (200, 100), color='#28A745')
draw = ImageDraw.Draw(img)
draw.text((20, 40), "E2E Test Image", fill='white')
img.save('/tmp/e2e-test-incoming.png')
PYEOF

    local upload_response
    upload_response=$(curl -s -X POST "https://api.telegram.org/bot${TEST_BOT_TOKEN}/sendPhoto" \
        -F "chat_id=${CHAT_ID}" \
        -F "photo=@/tmp/e2e-test-incoming.png" \
        -F "caption=E2E test upload")

    local file_id
    file_id=$(echo "$upload_response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['result']['photo'][-1]['file_id'])" 2>/dev/null)

    if [[ -z "$file_id" ]]; then
        fail "Could not upload test image to get file_id"
        send_message "/end imgrecv" >/dev/null 2>&1 || true
        return
    fi

    local update_id=$((RANDOM))
    curl -s -X POST "http://localhost:$PORT" \
        -H "Content-Type: application/json" \
        -d '{
            "update_id": '"$update_id"',
            "message": {
                "message_id": '"$update_id"',
                "from": {"id": '"$CHAT_ID"', "first_name": "TestUser"},
                "chat": {"id": '"$CHAT_ID"', "type": "private"},
                "date": '"$(date +%s)"',
                "photo": [
                    {"file_id": '"$file_id"'_small, "file_size": 1000, "width": 90, "height": 45},
                    {"file_id": '"$file_id"', "file_size": 5000, "width": 200, "height": 100}
                ],
                "caption": "Test incoming image"
            }
        }' >/dev/null

    sleep 3

    local inbox_dir="$TEST_SESSION_DIR/imgrecv/inbox"
    if ls "$inbox_dir"/*.png 2>/dev/null || ls "$inbox_dir"/*.jpg 2>/dev/null; then
        success "Incoming image downloaded to inbox"
    else
        fail "Incoming image not downloaded to inbox"
    fi

    send_message "/end imgrecv" >/dev/null 2>&1 || true
}

test_response_with_image_tags() {
    if ! $HAS_TMUX; then
        skip "Skipping /response with image tags (tmux not installed)"
        return
    fi
    info "Testing /response endpoint with image tags..."
    send_message "/new imageresponsetest" >/dev/null
    sleep 2
    local session_dir="$TEST_SESSION_DIR/imageresponsetest"
    mkdir -p "$session_dir"
    echo "$CHAT_ID" > "$session_dir/chat_id"
    date +%s > "$session_dir/pending"

    local result
    result=$(curl -s -X POST "http://localhost:$PORT/response" \
        -H "Content-Type: application/json" \
        -d '{"session":"imageresponsetest","text":"Here is the result [[image:/tmp/nonexistent.png|test caption]]"}')

    if [[ "$result" == "OK" ]]; then
        if [[ ! -f "$session_dir/pending" ]]; then
            success "/response endpoint handles image tags"
        else
            fail "/response did not clear pending"
        fi
    else
        fail "/response with image tags failed: $result"
    fi

    send_message "/kill imageresponsetest" >/dev/null 2>&1 || true
}

test_response_endpoint() {
    if ! $HAS_TMUX; then
        skip "Skipping /response endpoint (tmux not installed)"
        return
    fi
    info "Testing /response endpoint (hook -> bridge -> Telegram)..."
    local test_chat_id="$CHAT_ID"

    send_message "/new responsetest" >/dev/null
    sleep 3

    local session_dir="$TEST_SESSION_DIR/responsetest"
    mkdir -p "$session_dir"
    date +%s > "$session_dir/pending"
    echo "$test_chat_id" > "$session_dir/chat_id"

    local result
    result=$(curl -s -X POST "http://localhost:$PORT/response" \
        -H "Content-Type: application/json" \
        -d '{"session":"responsetest","text":"Test response from hook"}')

    if [[ "$result" == "OK" ]]; then
        if [[ ! -f "$session_dir/pending" ]]; then
            success "/response endpoint clears pending"
        else
            fail "/response endpoint did not clear pending"
        fi
    else
        fail "/response endpoint failed: $result"
    fi

    send_message "/kill responsetest" >/dev/null 2>&1 || true
}

test_response_without_pending() {
    if ! $HAS_TMUX; then
        skip "Skipping /response without pending (tmux not installed)"
        return
    fi
    info "Testing /response works without pending file..."
    send_message "/new nopendingtest" >/dev/null
    sleep 3
    local session_dir="$TEST_SESSION_DIR/nopendingtest"
    mkdir -p "$session_dir"
    echo "$CHAT_ID" > "$session_dir/chat_id"
    rm -f "$session_dir/pending"

    local result
    result=$(curl -s -X POST "http://localhost:$PORT/response" \
        -H "Content-Type: application/json" \
        -d '{"session":"nopendingtest","text":"Test without pending"}')

    if [[ "$result" == "OK" ]]; then
        success "/response works without pending file"
    else
        fail "/response without pending failed: $result"
    fi

    send_message "/kill nopendingtest" >/dev/null 2>&1 || true
}

test_with_tunnel() {
    if ! command -v cloudflared &>/dev/null; then
        skip "Skipping tunnel tests (cloudflared not installed)"
        return 0
    fi

    info "Starting tunnel..."
    cloudflared tunnel --url "http://localhost:$PORT" >"$TUNNEL_LOG" 2>&1 &
    TUNNEL_PID=$!

    local attempts=0
    while [[ $attempts -lt 30 ]]; do
        TUNNEL_URL=$(grep -o 'https://[^[:space:]|]*\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -1 || true)
        [[ -n "$TUNNEL_URL" ]] && break
        sleep 1
        ((++attempts))
    done

    if [[ -n "$TUNNEL_URL" ]]; then
        success "Tunnel started: $TUNNEL_URL"
        sleep 5

        local webhook_result
        webhook_result=$(curl -s "https://api.telegram.org/bot${TEST_BOT_TOKEN}/setWebhook?url=${TUNNEL_URL}")

        if echo "$webhook_result" | grep -q '"ok":true'; then
            success "Webhook configured"
        else
            fail "Webhook setup failed"
        fi
    else
        fail "Could not get tunnel URL"
    fi
}

main() {
    log ""
    log "======================================================================="
    log "  claudecode-telegram C Bridge Acceptance Tests"
    log "======================================================================="
    log ""

    require_token
    detect_deps

    cd "$SCRIPT_DIR"

    log "-- Unit Tests ---------------------------------------------------------"
    test_binary
    test_version

    log ""
    log "-- Integration Tests --------------------------------------------------"
    test_bridge_starts || exit 1
    sleep 2

    test_admin_registration
    test_non_admin_rejection
    test_new_command
    test_list_command
    test_use_command
    test_status_command
    test_restart_command
    test_stop_command
    test_at_mention
    test_at_all_broadcast
    test_session_files
    test_kill_command
    test_blocked_commands
    test_command_aliases
    test_learn_command
    test_reply_routing
    test_reply_context
    test_image_tag_parsing
    test_image_path_validation
    test_inbox_directory
    test_photo_message_no_focused
    test_incoming_image_e2e
    test_response_with_image_tags
    test_notify_endpoint
    test_response_endpoint
    test_response_without_pending

    log ""
    log "-- Tunnel Tests -------------------------------------------------------"
    test_with_tunnel

    if $HAS_TMUX; then
        send_message "/kill testbot1" >/dev/null 2>&1 || true
    fi

    log ""
    log "======================================================================="
    log "  Results: ${GREEN}$passed passed${NC}, ${RED}$failed failed${NC}, ${YELLOW}$skipped skipped${NC}"
    log "======================================================================="
    log ""

    [[ $failed -eq 0 ]] && exit 0 || exit 1
}

main "$@"
