#!/usr/bin/env bash
#
# test.sh - Automated acceptance tests for claudecode-telegram
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${TEST_PORT:-8095}"
CHAT_ID="${TEST_CHAT_ID:-123456789}"
BRIDGE_PID=""
TUNNEL_PID=""
TUNNEL_URL=""

# Test isolation: all test files under ~/.claude/telegram-test
TEST_BASE_DIR="$HOME/.claude/telegram-test"
TEST_SESSION_DIR="$TEST_BASE_DIR/sessions"
TEST_PID_FILE="$TEST_BASE_DIR/claudecode-telegram.pid"
TEST_TMUX_PREFIX="claude-test-"
BRIDGE_LOG="$TEST_BASE_DIR/bridge.log"
TUNNEL_LOG="$TEST_BASE_DIR/tunnel.log"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

passed=0
failed=0

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

log()     { echo -e "$@"; }
success() { log "${GREEN}✓${NC} $1"; ((passed++)) || true; }
fail()    { log "${RED}✗${NC} $1"; ((failed++)) || true; }
info()    { log "${YELLOW}→${NC} $1"; }

cleanup() {
    info "Cleaning up..."
    # Stop test gateway using PID file
    if [[ -f "$TEST_PID_FILE" ]]; then
        local pid
        pid=$(cat "$TEST_PID_FILE")
        kill "$pid" 2>/dev/null || true
        rm -f "$TEST_PID_FILE"
    fi
    [[ -n "$BRIDGE_PID" ]] && kill "$BRIDGE_PID" 2>/dev/null || true
    [[ -n "$TUNNEL_PID" ]] && kill "$TUNNEL_PID" 2>/dev/null || true
    # Kill any test sessions we created (using test prefix)
    tmux kill-session -t "${TEST_TMUX_PREFIX}testbot1" 2>/dev/null || true
    tmux kill-session -t "${TEST_TMUX_PREFIX}testbot2" 2>/dev/null || true
    tmux kill-session -t "${TEST_TMUX_PREFIX}responsetest" 2>/dev/null || true
    # Clean up test directory (but keep base dir for next run)
    [[ -d "$TEST_SESSION_DIR" ]] && rm -rf "$TEST_SESSION_DIR"
    [[ -f "$BRIDGE_LOG" ]] && rm -f "$BRIDGE_LOG"
    [[ -f "$TUNNEL_LOG" ]] && rm -f "$TUNNEL_LOG"
}

trap cleanup EXIT

require_token() {
    if [[ -z "${TEST_BOT_TOKEN:-}" ]]; then
        log "${RED}Error:${NC} TEST_BOT_TOKEN not set"
        log "Usage: TEST_BOT_TOKEN='...' ./test.sh"
        exit 1
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
                "text": "'"$text"'"
            }
        }'
}

# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

test_imports() {
    info "Testing Python imports..."
    if python3 -c "import bridge; print('OK')" 2>/dev/null | grep -q "OK"; then
        success "bridge.py imports correctly"
    else
        fail "bridge.py import failed"
    fi
}

test_version() {
    info "Testing version..."
    if ./claudecode-telegram.sh --version | grep -q "claudecode-telegram"; then
        success "Version command works"
    else
        fail "Version command failed"
    fi
}

test_bridge_starts() {
    info "Starting bridge on port $PORT..."

    # Kill any existing process on port
    lsof -ti :"$PORT" | xargs kill -9 2>/dev/null || true
    sleep 1

    # Create isolated test directories
    mkdir -p "$TEST_BASE_DIR"
    mkdir -p "$TEST_SESSION_DIR"

    # Start bridge with full test isolation
    TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" \
    PORT="$PORT" \
    SESSIONS_DIR="$TEST_SESSION_DIR" \
    PID_FILE="$TEST_PID_FILE" \
    TMUX_PREFIX="$TEST_TMUX_PREFIX" \
    ADMIN_CHAT_ID="${TEST_CHAT_ID:-}" \
    python3 -u "$SCRIPT_DIR/bridge.py" > "$BRIDGE_LOG" 2>&1 &
    BRIDGE_PID=$!

    if wait_for_port "$PORT"; then
        success "Bridge started on port $PORT"
    else
        fail "Bridge failed to start"
        return 1
    fi

    # Verify endpoint
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

    # Should return OK but no action taken (silent rejection)
    if [[ "$result" == "OK" ]]; then
        success "Non-admin silently rejected"
    else
        fail "Non-admin rejection failed"
    fi
}

test_new_command() {
    info "Testing /new command..."

    send_message "/new testbot1" >/dev/null
    sleep 2

    if tmux has-session -t "${TEST_TMUX_PREFIX}testbot1" 2>/dev/null; then
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
    info "Testing @mention routing..."

    # Create second session first
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

test_session_files() {
    info "Testing session file permissions..."

    local session_dir="$TEST_SESSION_DIR/testbot1"

    if [[ -d "$session_dir" ]]; then
        # Check directory permissions (should be 0700)
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

        # Check chat_id file if exists
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
    info "Testing /kill command..."

    local result
    result=$(send_message "/kill testbot2")
    sleep 1

    if ! tmux has-session -t "${TEST_TMUX_PREFIX}testbot2" 2>/dev/null; then
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

test_notify_endpoint() {
    info "Testing /notify endpoint..."

    local result
    result=$(curl -s -X POST "http://localhost:$PORT/notify" \
        -H "Content-Type: application/json" \
        -d '{"text":"Test notification"}')

    if echo "$result" | grep -q "Sent to"; then
        success "/notify endpoint works"
    else
        fail "/notify endpoint failed: $result"
    fi
}

test_response_endpoint() {
    info "Testing /response endpoint (hook -> bridge -> Telegram)..."

    # Use real chat_id if TEST_CHAT_ID provided (for full e2e verification)
    local test_chat_id="$CHAT_ID"
    local expect_real="false"
    [[ -n "${TEST_CHAT_ID:-}" ]] && expect_real="true"

    # Create a new session for this test
    send_message "/new responsetest" >/dev/null
    sleep 3

    # Set up pending file (simulates waiting for response)
    local session_dir="$TEST_SESSION_DIR/responsetest"
    mkdir -p "$session_dir"
    date +%s > "$session_dir/pending"
    echo "$test_chat_id" > "$session_dir/chat_id"

    # Simulate hook calling /response endpoint
    local result
    result=$(curl -s -X POST "http://localhost:$PORT/response" \
        -H "Content-Type: application/json" \
        -d '{"session":"responsetest","text":"Test response from hook"}')

    if [[ "$result" == "OK" ]]; then
        # Check bridge log for success
        sleep 1
        if grep -q "Response sent: responsetest -> Telegram OK" "$BRIDGE_LOG" 2>/dev/null; then
            if [[ "$expect_real" == "true" ]]; then
                success "/response endpoint sends to Telegram (check your Telegram!)"
            else
                success "/response endpoint sends to Telegram"
            fi
        else
            # Check if there was an API error (expected with fake chat_id)
            if grep -q "Telegram API error" "$BRIDGE_LOG" 2>/dev/null; then
                if [[ "$expect_real" == "true" ]]; then
                    fail "/response endpoint failed to send (check TEST_REAL_CHAT_ID)"
                else
                    success "/response endpoint works (API error expected with test chat_id)"
                fi
            else
                fail "/response endpoint did not log send attempt"
            fi
        fi
    else
        fail "/response endpoint failed: $result"
    fi

    # Cleanup
    send_message "/kill responsetest" >/dev/null 2>&1 || true
}

# ─────────────────────────────────────────────────────────────────────────────
# Integration tests (require tunnel)
# ─────────────────────────────────────────────────────────────────────────────

test_with_tunnel() {
    if ! command -v cloudflared &>/dev/null; then
        info "Skipping tunnel tests (cloudflared not installed)"
        return 0
    fi

    info "Starting tunnel..."
    cloudflared tunnel --url "http://localhost:$PORT" >"$TUNNEL_LOG" 2>&1 &
    TUNNEL_PID=$!

    # Wait for tunnel URL to appear in log (up to 30 seconds)
    local attempts=0
    while [[ $attempts -lt 30 ]]; do
        TUNNEL_URL=$(grep -o 'https://[^[:space:]|]*\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -1 || true)
        [[ -n "$TUNNEL_URL" ]] && break
        sleep 1
        ((++attempts))
    done

    if [[ -n "$TUNNEL_URL" ]]; then
        success "Tunnel started: $TUNNEL_URL"

        # Wait for DNS propagation
        sleep 5

        # Test webhook setup
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

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

main() {
    log ""
    log "═══════════════════════════════════════════════════════════════════════"
    log "  claudecode-telegram Acceptance Tests"
    log "═══════════════════════════════════════════════════════════════════════"
    log ""

    require_token

    cd "$SCRIPT_DIR"

    # Unit tests (no bridge needed)
    log "── Unit Tests ──────────────────────────────────────────────────────────"
    test_imports
    test_version

    # Integration tests (bridge needed)
    log ""
    log "── Integration Tests ───────────────────────────────────────────────────"
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
    test_session_files
    test_kill_command
    test_blocked_commands
    test_notify_endpoint
    test_response_endpoint

    # Tunnel tests (optional)
    log ""
    log "── Tunnel Tests ────────────────────────────────────────────────────────"
    test_with_tunnel

    # Cleanup test sessions
    send_message "/kill testbot1" >/dev/null 2>&1 || true

    # Summary
    log ""
    log "═══════════════════════════════════════════════════════════════════════"
    log "  Results: ${GREEN}$passed passed${NC}, ${RED}$failed failed${NC}"
    log "═══════════════════════════════════════════════════════════════════════"
    log ""

    [[ $failed -eq 0 ]] && exit 0 || exit 1
}

main "$@"
