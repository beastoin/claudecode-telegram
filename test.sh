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
    [[ -n "$BRIDGE_PID" ]] && kill "$BRIDGE_PID" 2>/dev/null || true
    [[ -n "$TUNNEL_PID" ]] && kill "$TUNNEL_PID" 2>/dev/null || true
    # Kill any test sessions we created
    tmux kill-session -t "claude-testbot1" 2>/dev/null || true
    tmux kill-session -t "claude-testbot2" 2>/dev/null || true
}

trap cleanup EXIT

require_token() {
    if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
        log "${RED}Error:${NC} TELEGRAM_BOT_TOKEN not set"
        log "Usage: TELEGRAM_BOT_TOKEN='...' ./test.sh"
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

    TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" PORT="$PORT" python3 -u "$SCRIPT_DIR/bridge.py" &
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

    if tmux has-session -t "claude-testbot1" 2>/dev/null; then
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

    local session_dir="$HOME/.claude/telegram/sessions/testbot1"

    if [[ -d "$session_dir" ]]; then
        # Check directory permissions (should be 0700)
        local dir_perms
        dir_perms=$(stat -f "%Lp" "$session_dir" 2>/dev/null || stat -c "%a" "$session_dir" 2>/dev/null)
        if [[ "$dir_perms" == "700" ]]; then
            success "Session directory has secure permissions (0700)"
        else
            fail "Session directory permissions incorrect: $dir_perms"
        fi

        # Check chat_id file if exists
        if [[ -f "$session_dir/chat_id" ]]; then
            local file_perms
            file_perms=$(stat -f "%Lp" "$session_dir/chat_id" 2>/dev/null || stat -c "%a" "$session_dir/chat_id" 2>/dev/null)
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

    if ! tmux has-session -t "claude-testbot2" 2>/dev/null; then
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

# ─────────────────────────────────────────────────────────────────────────────
# Integration tests (require tunnel)
# ─────────────────────────────────────────────────────────────────────────────

test_with_tunnel() {
    if [[ "${SKIP_TUNNEL:-}" == "1" ]]; then
        info "Skipping tunnel tests (SKIP_TUNNEL=1)"
        return 0
    fi

    if ! command -v cloudflared &>/dev/null; then
        info "Skipping tunnel tests (cloudflared not installed)"
        return 0
    fi

    info "Starting tunnel..."
    cloudflared tunnel --url "http://localhost:$PORT" 2>&1 &
    TUNNEL_PID=$!

    sleep 10

    # Get tunnel URL from process
    TUNNEL_URL=$(ps aux | grep cloudflared | grep -o 'https://[^[:space:]]*\.trycloudflare\.com' | head -1 || true)

    if [[ -n "$TUNNEL_URL" ]]; then
        success "Tunnel started: $TUNNEL_URL"

        # Test webhook setup
        local webhook_result
        webhook_result=$(curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook?url=${TUNNEL_URL}")

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
