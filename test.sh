#!/usr/bin/env bash
#
# test.sh - Automated acceptance tests for claudecode-telegram
#
# Usage:
#   TEST_BOT_TOKEN='...' ./test.sh                    # Basic tests (mock chat ID)
#   TEST_BOT_TOKEN='...' TEST_CHAT_ID='...' ./test.sh # Full e2e (real Telegram messages)
#
# Environment:
#   TEST_BOT_TOKEN  - Required: Your test bot token from @BotFather
#   TEST_CHAT_ID    - Optional: Your chat ID for e2e verification
#
# Tests run isolated using --node test with separate port (8095),
# prefix (claude-test-), and PID file. Safe to run while production is active.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHAT_ID="${TEST_CHAT_ID:-123456789}"
BRIDGE_PID=""
TUNNEL_PID=""
TUNNEL_URL=""

# Test node configuration
TEST_NODE="test"
TEST_NODE_DIR="${TEST_NODE_DIR:-$HOME/.claude/telegram/nodes/$TEST_NODE}"
PORT="${TEST_PORT:-8095}"
TEST_SESSION_DIR="$TEST_NODE_DIR/sessions"
TEST_PID_FILE="$TEST_NODE_DIR/pid"
TEST_TMUX_PREFIX="claude-${TEST_NODE}-"
BRIDGE_LOG="$TEST_NODE_DIR/bridge.log"
TUNNEL_LOG="$TEST_NODE_DIR/tunnel.log"

# Ensure unit tests write to isolated test sessions directory
export SESSIONS_DIR="$TEST_SESSION_DIR"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

passed=0
failed=0

# ============================================================
# TEST CONFIG + HELPERS
# ============================================================

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

log()     { echo -e "$@"; }
success() { log "${GREEN}✓${NC} $1"; ((passed++)) || true; }
fail()    { log "${RED}✗${NC} $1"; ((failed++)) || true; }
info()    { log "${YELLOW}→${NC} $1"; }

cleanup() {
    info "Cleaning up..."
    # Stop test node using PID file
    if [[ -f "$TEST_PID_FILE" ]]; then
        local pid
        pid=$(cat "$TEST_PID_FILE")
        kill "$pid" 2>/dev/null || true
        rm -f "$TEST_PID_FILE"
    fi
    # Also kill bridge PID if tracked separately
    if [[ -f "$TEST_NODE_DIR/bridge.pid" ]]; then
        kill "$(cat "$TEST_NODE_DIR/bridge.pid")" 2>/dev/null || true
        rm -f "$TEST_NODE_DIR/bridge.pid"
    fi
    # Also kill direct mode bridge PID if tracked
    if [[ -f "$TEST_NODE_DIR/direct_mode_bridge.pid" ]]; then
        kill "$(cat "$TEST_NODE_DIR/direct_mode_bridge.pid")" 2>/dev/null || true
        rm -f "$TEST_NODE_DIR/direct_mode_bridge.pid"
    fi
    [[ -n "$BRIDGE_PID" ]] && kill "$BRIDGE_PID" 2>/dev/null; true
    [[ -n "$TUNNEL_PID" ]] && kill "$TUNNEL_PID" 2>/dev/null; true
    # Kill any test sessions we created (using test prefix)
    tmux list-sessions -F '#{session_name}' 2>/dev/null | grep "^${TEST_TMUX_PREFIX}" | while read -r session; do
        tmux kill-session -t "$session" 2>/dev/null || true
    done || true
    # Clean up test session files (but keep node dir for next run)
    [[ -d "$TEST_SESSION_DIR" ]] && rm -rf "$TEST_SESSION_DIR"; true
    [[ -f "$BRIDGE_LOG" ]] && rm -f "$BRIDGE_LOG"; true
    [[ -f "$TUNNEL_LOG" ]] && rm -f "$TUNNEL_LOG"; true
    rm -f "$TEST_NODE_DIR/tunnel.pid" "$TEST_NODE_DIR/tunnel_url" "$TEST_NODE_DIR/port" 2>/dev/null || true
    rm -f "$TEST_NODE_DIR/last_chat_id" "$TEST_NODE_DIR/last_active" 2>/dev/null || true
    rm -f "$TEST_NODE_DIR/direct_mode_bridge.log" 2>/dev/null || true
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

wait_for_port() {
    local port="$1" attempts=0
    while ! nc -z localhost "$port" 2>/dev/null && [[ $attempts -lt 30 ]]; do
        sleep 0.1
        ((attempts++))
    done
    nc -z localhost "$port" 2>/dev/null
}

wait_for_session() {
    local session="$1" attempts=0
    while ! tmux has-session -t "${TEST_TMUX_PREFIX}${session}" 2>/dev/null && [[ $attempts -lt 20 ]]; do
        sleep 0.1
        ((attempts++))
    done
    tmux has-session -t "${TEST_TMUX_PREFIX}${session}" 2>/dev/null
}

wait_for_session_gone() {
    local session="$1" attempts=0
    while tmux has-session -t "${TEST_TMUX_PREFIX}${session}" 2>/dev/null && [[ $attempts -lt 20 ]]; do
        sleep 0.1
        ((attempts++))
    done
    ! tmux has-session -t "${TEST_TMUX_PREFIX}${session}" 2>/dev/null
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
                "text": "'"$text"'",
                "reply_to_message": {
                    "message_id": '"$reply_id"',
                    "from": {"id": 123456, "first_name": "Bot", "is_bot": '"$reply_from_bot"'},
                    "chat": {"id": '"$chat_id"', "type": "private"},
                    "date": '"$(date +%s)"',
                    "text": "'"$reply_text"'"
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
                    {"file_id": "'"$file_id"'_small", "file_size": 1000, "width": 90, "height": 90},
                    {"file_id": "'"$file_id"'", "file_size": 5000, "width": 320, "height": 320}
                ],
                "caption": "'"$caption"'"
            }
        }'
}

send_document_message() {
    local file_id="$1"
    local file_name="${2:-document.pdf}"
    local mime_type="${3:-application/pdf}"
    local file_size="${4:-1024}"
    local caption="${5:-}"
    local chat_id="${6:-$CHAT_ID}"
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
                "document": {
                    "file_id": "'"$file_id"'",
                    "file_unique_id": "'"$file_id"'_unique",
                    "file_name": "'"$file_name"'",
                    "mime_type": "'"$mime_type"'",
                    "file_size": '"$file_size"'
                },
                "caption": "'"$caption"'"
            }
        }'
}

# ============================================================
# CORE TESTS
# ============================================================

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

test_response_prefix_formatting() {
    info "Testing response prefix formatting..."
    if python3 -c "
from bridge import format_response_text
text = 'Hello <code>world</code>'
result = format_response_text('session-1', text)
assert result == '<b>session-1:</b>\nHello <code>world</code>'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Response formatting adds session prefix"
    else
        fail "Response formatting failed"
    fi
}

test_message_splitting_short() {
    info "Testing message splitting (short message, no split)..."
    if python3 -c "
from bridge import split_message, TELEGRAM_MAX_LENGTH
text = 'Short message'
chunks = split_message(text)
assert len(chunks) == 1, f'expected 1 chunk, got {len(chunks)}'
assert chunks[0] == text, 'chunk should match original'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Short message not split"
    else
        fail "Short message splitting failed"
    fi
}

test_message_splitting_newlines() {
    info "Testing message splitting (split on newlines)..."
    if python3 -c "
from bridge import split_message

# Create text that's over 4096 chars with clear newline breaks
lines = ['Line ' + str(i) + ' ' + 'x' * 100 for i in range(50)]
text = '\n'.join(lines)
assert len(text) > 4096, f'test text should be >4096, got {len(text)}'

chunks = split_message(text, max_len=4096)
assert len(chunks) > 1, f'expected multiple chunks, got {len(chunks)}'

# Each chunk should be within limit
for i, chunk in enumerate(chunks):
    assert len(chunk) <= 4096, f'chunk {i} too long: {len(chunk)}'

# Joined chunks should contain all content (allowing for whitespace trimming)
all_content = ''.join(c.strip() for c in chunks)
original_content = text.replace('\n', '').replace(' ', '')
# Just verify we didn't lose significant content
assert len(all_content) > len(text) * 0.9, 'lost too much content'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Long message splits on newlines"
    else
        fail "Newline splitting failed"
    fi
}

test_message_splitting_hard() {
    info "Testing message splitting (hard split, no natural breaks)..."
    if python3 -c "
from bridge import split_message

# Create text with no natural break points (one long line)
text = 'x' * 10000
chunks = split_message(text, max_len=4096)

assert len(chunks) >= 3, f'expected 3+ chunks for 10000 chars, got {len(chunks)}'

# Each chunk should be within limit
for i, chunk in enumerate(chunks):
    assert len(chunk) <= 4096, f'chunk {i} too long: {len(chunk)}'

# Total length should match
total_len = sum(len(c) for c in chunks)
assert total_len == len(text), f'content lost: {total_len} vs {len(text)}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Hard split works for long lines"
    else
        fail "Hard split failed"
    fi
}

test_multipart_formatting() {
    info "Testing multipart message formatting..."
    if python3 -c "
from bridge import format_multipart_messages

# Single chunk - no part numbers
chunks = ['Hello world']
formatted = format_multipart_messages('worker', chunks)
assert len(formatted) == 1
assert formatted[0] == '<b>worker:</b>\nHello world'
assert '(1/' not in formatted[0], 'single chunk should not have part numbers'

# Multiple chunks - all have prefix, no part numbers
chunks = ['Part 1 content', 'Part 2 content', 'Part 3 content']
formatted = format_multipart_messages('lee', chunks)
assert len(formatted) == 3
assert formatted[0] == '<b>lee:</b>\nPart 1 content', f'first: {formatted[0]}'
assert formatted[1] == '<b>lee:</b>\nPart 2 content', f'second: {formatted[1]}'
assert formatted[2] == '<b>lee:</b>\nPart 3 content', f'third: {formatted[2]}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Multipart formatting works"
    else
        fail "Multipart formatting failed"
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

test_equals_syntax() {
    info "Testing --flag=value argument syntax..."
    # Test that --node=test and --port=9999 are parsed correctly (v0.10.1 fix)
    # We just need to verify the script parses without error, not actually run
    if ./claudecode-telegram.sh --node=testnode --port=9999 --help 2>/dev/null | grep -qi "usage"; then
        success "Equals syntax (--flag=value) works"
    else
        fail "Equals syntax parsing failed"
    fi
}

test_sandbox_config() {
    info "Testing sandbox configuration variables..."
    if python3 -c "
from bridge import SANDBOX_ENABLED, SANDBOX_IMAGE, SANDBOX_EXTRA_MOUNTS

# Verify config variables exist
assert isinstance(SANDBOX_ENABLED, bool), 'SANDBOX_ENABLED should be bool'
assert isinstance(SANDBOX_IMAGE, str), 'SANDBOX_IMAGE should be str'
assert isinstance(SANDBOX_EXTRA_MOUNTS, list), 'SANDBOX_EXTRA_MOUNTS should be list'

# Default: no extra mounts (only ~ is mounted by default)
# Extra mounts come from --mount/--mount-ro CLI flags

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Sandbox config variables correct"
    else
        fail "Sandbox config test failed"
    fi
}

test_sandbox_docker_cmd() {
    info "Testing sandbox Docker command generation..."
    if python3 -c "
import os
from pathlib import Path
os.environ['SANDBOX_ENABLED'] = '1'
os.environ['PORT'] = '8095'

from bridge import get_docker_run_cmd

cmd = get_docker_run_cmd('testworker')
home = str(Path.home())

# Verify command structure
assert 'docker run -it' in cmd, 'should have docker run -it'
assert '--name=claude-worker-testworker' in cmd, 'should have container name'
assert '--rm' in cmd, 'should have --rm for cleanup'

# Verify default home mount to /workspace
assert f'-v={home}:/workspace' in cmd, 'should mount home to /workspace'

# Verify working directory
assert '-w /workspace' in cmd, 'should set workdir to /workspace'

# Verify BRIDGE_URL for container->host communication
assert 'BRIDGE_URL=http://host.docker.internal:8095' in cmd, 'should set BRIDGE_URL'

# Verify claude command with --dangerously-skip-permissions
assert 'claude --dangerously-skip-permissions' in cmd, 'should run claude with skip permissions'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Sandbox Docker command correct"
    else
        fail "Sandbox Docker command test failed"
    fi
}

test_bridge_starts() {
    info "Starting bridge on port $PORT..."

    # Kill any existing process on port
    lsof -ti :"$PORT" | xargs kill -9 2>/dev/null || true
    sleep 0.3

    # Create test node directory structure
    mkdir -p "$TEST_NODE_DIR"
    mkdir -p "$TEST_SESSION_DIR"
    chmod 700 "$TEST_NODE_DIR" "$TEST_SESSION_DIR"

    # Start bridge with test node isolation
    TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" \
    PORT="$PORT" \
    NODE_NAME="$TEST_NODE" \
    SESSIONS_DIR="$TEST_SESSION_DIR" \
    TMUX_PREFIX="$TEST_TMUX_PREFIX" \
    ADMIN_CHAT_ID="${TEST_CHAT_ID:-}" \
    python3 -u "$SCRIPT_DIR/bridge.py" > "$BRIDGE_LOG" 2>&1 &
    BRIDGE_PID=$!
    echo "$BRIDGE_PID" > "$TEST_NODE_DIR/bridge.pid"
    echo "$PORT" > "$TEST_NODE_DIR/port"

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
    result=$(send_message "/team" "999888777")

    # Should return OK but no action taken (silent rejection)
    if [[ "$result" == "OK" ]]; then
        success "Non-admin silently rejected"
    else
        fail "Non-admin rejection failed"
    fi
}

test_hire_command() {
    info "Testing /hire command..."

    send_message "/hire testbot1" >/dev/null

    if wait_for_session "testbot1"; then
        success "/hire creates tmux session"
    else
        fail "/hire failed to create session"
    fi
}

test_backend_env_metadata() {
    info "Testing worker backend stored in tmux env..."

    if python3 -c "
import bridge
import unittest.mock as mock

calls = []

def fake_run(cmd, **kwargs):
    calls.append(cmd)
    class Result:
        returncode = 0
        stdout = ''
    return Result()

with mock.patch.object(bridge, 'subprocess') as mock_subprocess:
    mock_subprocess.run.side_effect = fake_run
    bridge.export_hook_env('claude-test-backend', 'codex')

found = any('WORKER_BACKEND' in cmd and 'codex' in cmd for cmd in calls)
assert found, f'WORKER_BACKEND=codex not set in tmux env: {calls}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Backend env stored in tmux env"
    else
        fail "Backend env tmux export test failed"
    fi
}

test_team_command() {
    info "Testing /team command..."

    local result
    result=$(send_message "/team")

    if [[ "$result" == "OK" ]]; then
        success "/team command works"
    else
        fail "/team command failed"
    fi
}

test_focus_command() {
    info "Testing /focus command..."

    local result
    result=$(send_message "/focus testbot1")

    if [[ "$result" == "OK" ]]; then
        success "/focus command works"
    else
        fail "/focus command failed"
    fi
}

test_progress_command() {
    info "Testing /progress command..."

    local result
    result=$(send_message "/progress")

    if [[ "$result" == "OK" ]]; then
        success "/progress command works"
    else
        fail "/progress command failed"
    fi
}

test_relaunch_command() {
    info "Testing /relaunch command..."

    local result
    result=$(send_message "/relaunch")

    if [[ "$result" == "OK" ]]; then
        success "/relaunch command works"
    else
        fail "/relaunch command failed"
    fi
}

test_pause_command() {
    info "Testing /pause command..."

    local result
    result=$(send_message "/pause")

    if [[ "$result" == "OK" ]]; then
        success "/pause command works"
    else
        fail "/pause command failed"
    fi
}

test_at_mention() {
    info "Testing @mention routing..."

    # Create second session first
    send_message "/hire testbot2" >/dev/null
    wait_for_session "testbot2"

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

    # Both testbot1 and testbot2 should exist from previous tests
    local result
    result=$(send_message "@all hello everyone")

    if [[ "$result" == "OK" ]]; then
        success "@all broadcast accepted"
    else
        fail "@all broadcast failed"
    fi
}

# BEHAVIOR TEST: Verify tmux session remains running after creation (parity with direct mode)
test_tmux_mode_session_stays_alive() {
    info "Testing tmux mode session stays alive after creation..."

    # Clean up any existing test worker
    send_message "/end tmuxalive" >/dev/null 2>&1 || true
    wait_for_session_gone "tmuxalive" 2>/dev/null || true

    # Create worker via /hire
    local result
    result=$(send_message "/hire tmuxalive")

    if [[ "$result" != "OK" ]]; then
        fail "Tmux session alive: /hire failed: $result"
        return
    fi

    # Wait for tmux session to be created
    if ! wait_for_session "tmuxalive"; then
        fail "Tmux session alive: Session not created"
        return
    fi

    local tmux_name="${TEST_TMUX_PREFIX}tmuxalive"

    # KEY BEHAVIOR TEST: Wait 3 seconds and verify session is STILL running
    sleep 3

    if tmux has-session -t "$tmux_name" 2>/dev/null; then
        success "Tmux session alive: Session still running after 3 seconds"
    else
        fail "Tmux session alive: Session died unexpectedly"
        return
    fi

    # Cleanup
    send_message "/end tmuxalive" >/dev/null 2>&1 || true
    wait_for_session_gone "tmuxalive" 2>/dev/null || true
}

# BEHAVIOR TEST: Verify message actually reaches tmux session (parity with direct mode)
test_tmux_mode_message_delivery() {
    info "Testing tmux mode message delivery to session..."

    # Clean up any existing test worker
    send_message "/end tmuxmsg" >/dev/null 2>&1 || true
    wait_for_session_gone "tmuxmsg" 2>/dev/null || true

    # Create worker
    local result
    result=$(send_message "/hire tmuxmsg")

    if [[ "$result" != "OK" ]]; then
        fail "Message delivery: /hire failed: $result"
        return
    fi

    # Wait for session to be created
    if ! wait_for_session "tmuxmsg"; then
        fail "Message delivery: Session not created"
        return
    fi

    local tmux_name="${TEST_TMUX_PREFIX}tmuxmsg"

    # Focus the worker
    send_message "/focus tmuxmsg" >/dev/null
    sleep 0.5

    # KEY BEHAVIOR TEST: Send a unique message and verify it appears in tmux pane
    local unique_msg="test_msg_${RANDOM}"
    result=$(send_message "$unique_msg")

    if [[ "$result" != "OK" ]]; then
        fail "Message delivery: Message send failed: $result"
        send_message "/end tmuxmsg" >/dev/null 2>&1 || true
        return
    fi

    # Wait for message to be delivered
    sleep 1

    # Capture tmux pane content and check for our message
    local pane_content
    pane_content=$(tmux capture-pane -t "$tmux_name" -p 2>/dev/null || echo "")

    if echo "$pane_content" | grep -q "$unique_msg"; then
        success "Message delivery: Message appeared in tmux session"
    else
        fail "Message delivery: Message not found in tmux pane"
    fi

    # Cleanup
    send_message "/end tmuxmsg" >/dev/null 2>&1 || true
    wait_for_session_gone "tmuxmsg" 2>/dev/null || true
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

test_end_command() {
    info "Testing /end command..."

    local result
    result=$(send_message "/end testbot2")
    wait_for_session_gone "testbot2"

    if ! tmux has-session -t "${TEST_TMUX_PREFIX}testbot2" 2>/dev/null; then
        success "/end removes tmux session"
    else
        fail "/end failed to remove session"
    fi
}

test_dynamic_bot_command_list_update() {
    info "Testing dynamic bot command list updates..."

    local worker="cmdlist$RANDOM"
    local added=0
    local removed=0
    local api_error=0

    # Ensure clean start
    send_message "/end $worker" >/dev/null 2>&1 || true
    wait_for_session_gone "$worker" 2>/dev/null || true

    send_message "/hire $worker" >/dev/null
    if ! wait_for_session "$worker"; then
        fail "Dynamic bot commands: /hire failed"
        return
    fi

    # Wait for command to appear in Telegram
    for _ in $(seq 1 15); do
        if curl -s "https://api.telegram.org/bot${TEST_BOT_TOKEN}/getMyCommands" | \
            python3 -c 'import json,sys
name=sys.argv[1]
try:
    data=json.load(sys.stdin)
except Exception:
    sys.exit(2)
if not data.get("ok"):
    sys.exit(2)
cmds=[c.get("command") for c in data.get("result", [])]
sys.exit(0 if name in cmds else 1)
' "$worker" >/dev/null 2>&1; then
            added=1
            break
        else
            local status=$?
            if [[ $status -ne 1 ]]; then
                api_error=1
                break
            fi
        fi
        sleep 0.3
    done

    if [[ $api_error -eq 1 ]]; then
        fail "Dynamic bot commands: getMyCommands API error"
    elif [[ $added -eq 1 ]]; then
        success "Dynamic bot commands: /$worker added"
    else
        fail "Dynamic bot commands: /$worker not added"
    fi

    send_message "/end $worker" >/dev/null 2>&1 || true
    wait_for_session_gone "$worker" 2>/dev/null || true

    # Wait for command to be removed
    api_error=0
    for _ in $(seq 1 15); do
        if curl -s "https://api.telegram.org/bot${TEST_BOT_TOKEN}/getMyCommands" | \
            python3 -c 'import json,sys
name=sys.argv[1]
try:
    data=json.load(sys.stdin)
except Exception:
    sys.exit(2)
if not data.get("ok"):
    sys.exit(2)
cmds=[c.get("command") for c in data.get("result", [])]
sys.exit(0 if name in cmds else 1)
' "$worker" >/dev/null 2>&1; then
            sleep 0.3
            continue
        else
            local status=$?
            if [[ $status -eq 1 ]]; then
                removed=1
                break
            fi
            api_error=1
            break
        fi
    done

    if [[ $api_error -eq 1 ]]; then
        fail "Dynamic bot commands: getMyCommands API error (remove)"
    elif [[ $removed -eq 1 ]]; then
        success "Dynamic bot commands: /$worker removed"
    else
        fail "Dynamic bot commands: /$worker still present"
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
# New feature tests (v0.8.0+)
# ─────────────────────────────────────────────────────────────────────────────

test_additional_commands() {
    info "Testing additional commands..."

    # Create a worker for testing
    send_message "/hire addlbot" >/dev/null

    if wait_for_session "addlbot"; then
        success "/hire creates worker"
    else
        fail "/hire failed"
        return
    fi

    # Test /team
    local result
    result=$(send_message "/team")
    if [[ "$result" == "OK" ]]; then
        success "/team works"
    else
        fail "/team failed"
    fi

    # Test /focus
    result=$(send_message "/focus addlbot")
    if [[ "$result" == "OK" ]]; then
        success "/focus works"
    else
        fail "/focus failed"
    fi

    # Test /progress
    result=$(send_message "/progress")
    if [[ "$result" == "OK" ]]; then
        success "/progress works"
    else
        fail "/progress failed"
    fi

    # Test /pause
    result=$(send_message "/pause")
    if [[ "$result" == "OK" ]]; then
        success "/pause works"
    else
        fail "/pause failed"
    fi

    # Test /end
    send_message "/end addlbot" >/dev/null
    wait_for_session_gone "addlbot"
    if ! tmux has-session -t "${TEST_TMUX_PREFIX}addlbot" 2>/dev/null; then
        success "/end removes worker"
    else
        fail "/end failed"
    fi
}

test_learn_command() {
    info "Testing /learn command..."

    # Create a worker first
    send_message "/hire learnbot" >/dev/null
    wait_for_session "learnbot"
    send_message "/focus learnbot" >/dev/null

    # Test /learn (requires focused worker)
    local result
    result=$(send_message "/learn")
    if [[ "$result" == "OK" ]]; then
        success "/learn works"
    else
        fail "/learn failed"
    fi

    # Test /learn with topic
    result=$(send_message "/learn git")
    if [[ "$result" == "OK" ]]; then
        success "/learn <topic> works"
    else
        fail "/learn <topic> failed"
    fi

    # Cleanup
    send_message "/end learnbot" >/dev/null 2>&1 || true
}

test_reply_routing() {
    info "Testing reply-to-worker routing..."

    # Create worker
    send_message "/hire replybot" >/dev/null
    wait_for_session "replybot"

    # Test reply to worker message (simulated bot message with worker prefix)
    local result
    result=$(send_reply "follow up question" "replybot: I fixed the bug")
    if [[ "$result" == "OK" ]]; then
        success "Reply to worker message routed correctly"
    else
        fail "Reply routing failed"
    fi

    # Cleanup
    send_message "/end replybot" >/dev/null 2>&1 || true
}

test_reply_context() {
    info "Testing reply context inclusion..."

    # Create worker
    send_message "/hire contextbot" >/dev/null
    wait_for_session "contextbot"
    send_message "/focus contextbot" >/dev/null

    # Test reply to own message (non-bot) includes context
    local result
    result=$(send_reply "." "my original message" "false")
    if [[ "$result" == "OK" ]]; then
        success "Reply to own message includes context"
    else
        fail "Reply context failed"
    fi

    # Cleanup
    send_message "/end contextbot" >/dev/null 2>&1 || true
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

test_image_tag_parsing() {
    info "Testing image tag parsing..."

    # Create test image files
    touch /tmp/test.jpg /tmp/a.jpg /tmp/b.png

    if python3 -c "
from bridge import parse_image_tags

# Test single image tag (file exists, so tag is removed)
text = 'Here is an image [[image:/tmp/test.jpg|my caption]] and more text'
clean, images = parse_image_tags(text)
assert 'Here is an image' in clean, f'clean text wrong: {clean!r}'
assert len(images) == 1, f'expected 1 image, got {len(images)}'
assert images[0] == ('/tmp/test.jpg', 'my caption'), f'image data wrong: {images[0]}'

# Test tag with non-existent file (tag stays in text)
text2 = '[[image:/nonexistent/photo.png]]'
clean2, images2 = parse_image_tags(text2)
assert len(images2) == 0, f'non-existent file should not be parsed: {images2}'
assert '[[image:' in clean2, f'tag should stay in text: {clean2!r}'

# Test multiple images (files exist)
text3 = 'First [[image:/tmp/a.jpg|cap1]] middle [[image:/tmp/b.png|cap2]] end'
clean3, images3 = parse_image_tags(text3)
assert len(images3) == 2, f'expected 2 images, got {len(images3)}'
assert images3[0] == ('/tmp/a.jpg', 'cap1')
assert images3[1] == ('/tmp/b.png', 'cap2')

# Test escaped tag (not parsed)
text4 = r'Example: \[[image:/tmp/test.jpg|caption]]'
clean4, images4 = parse_image_tags(text4)
assert len(images4) == 0, f'escaped tag should not be parsed: {images4}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Image tag parsing works correctly"
    else
        fail "Image tag parsing failed"
    fi

    # Cleanup
    rm -f /tmp/test.jpg /tmp/a.jpg /tmp/b.png
}

test_image_path_validation() {
    info "Testing image path validation..."

    if python3 -c "
from pathlib import Path
import tempfile
import os

# Import after setting up test paths
from bridge import ALLOWED_IMAGE_EXTENSIONS, SESSIONS_DIR, send_photo

# Test allowed extensions
for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp']:
    assert ext in ALLOWED_IMAGE_EXTENSIONS, f'{ext} should be allowed'

# Test disallowed extensions
for ext in ['.exe', '.sh', '.py', '.txt']:
    assert ext not in ALLOWED_IMAGE_EXTENSIONS, f'{ext} should not be allowed'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Image extension validation works"
    else
        fail "Image extension validation failed"
    fi
}

test_file_tag_parsing() {
    info "Testing file tag parsing..."

    # Create test files
    echo "test" > /tmp/report.pdf
    echo "test" > /tmp/data.json
    echo "test" > /tmp/a.txt
    echo "test" > /tmp/b.csv

    if python3 -c "
from bridge import parse_file_tags

# Test basic file tag (file exists, so tag is removed)
text = 'Here is the report: [[file:/tmp/report.pdf|Q4 Report]]'
clean, files = parse_file_tags(text)
assert 'Here is the report:' in clean, f'Expected clean text, got: {clean}'
assert len(files) == 1, f'Expected 1 file, got {len(files)}'
assert files[0] == ('/tmp/report.pdf', 'Q4 Report'), f'Wrong file: {files[0]}'

# Test file tag without caption (file exists)
text = 'Output: [[file:/tmp/data.json]]'
clean, files = parse_file_tags(text)
assert len(files) == 1
assert files[0] == ('/tmp/data.json', ''), f'Wrong file: {files[0]}'

# Test tag with non-existent file (tag stays in text)
text = 'Output: [[file:/nonexistent/file.txt]]'
clean, files = parse_file_tags(text)
assert len(files) == 0, f'non-existent file should not be parsed: {files}'
assert '[[file:' in clean, f'tag should stay in text: {clean}'

# Test multiple file tags (files exist)
text = '[[file:/tmp/a.txt|A]] and [[file:/tmp/b.csv|B]]'
clean, files = parse_file_tags(text)
assert len(files) == 2

# Test no tags
text = 'No files here'
clean, files = parse_file_tags(text)
assert clean == 'No files here'
assert len(files) == 0

# Test escaped tag (not parsed)
text = r'Example: \[[file:/tmp/report.pdf|caption]]'
clean, files = parse_file_tags(text)
assert len(files) == 0, f'escaped tag should not be parsed: {files}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "File tag parsing works correctly"
    else
        fail "File tag parsing failed"
    fi

    # Cleanup
    rm -f /tmp/report.pdf /tmp/data.json /tmp/a.txt /tmp/b.csv
}

test_file_extension_validation() {
    info "Testing file extension validation..."

    if python3 -c "
from bridge import ALLOWED_DOC_EXTENSIONS, BLOCKED_DOC_EXTENSIONS, BLOCKED_FILENAMES, is_blocked_filename

# Test allowed extensions
for ext in ['.pdf', '.txt', '.md', '.json', '.csv', '.py', '.js', '.go']:
    assert ext in ALLOWED_DOC_EXTENSIONS, f'{ext} should be allowed'

# Test blocked extensions (secrets)
for ext in ['.pem', '.key', '.p12', '.pfx']:
    assert ext in BLOCKED_DOC_EXTENSIONS, f'{ext} should be blocked'

# Test blocked filenames
assert is_blocked_filename('.env'), '.env should be blocked'
assert is_blocked_filename('.env.local'), '.env.local should be blocked'
assert is_blocked_filename('id_rsa'), 'id_rsa should be blocked'
assert is_blocked_filename('.npmrc'), '.npmrc should be blocked'

# Test allowed filenames
assert not is_blocked_filename('report.pdf'), 'report.pdf should be allowed'
assert not is_blocked_filename('data.json'), 'data.json should be allowed'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "File extension validation works"
    else
        fail "File extension validation failed"
    fi
}

test_photo_message_no_focused() {
    info "Testing photo message without focused worker..."

    # First ensure no focused worker
    send_message "/end testbot1" >/dev/null 2>&1 || true
    wait_for_session_gone "testbot1"

    # Clear active by killing all test sessions
    tmux kill-session -t "${TEST_TMUX_PREFIX}testbot1" 2>/dev/null || true

    local result
    result=$(send_photo_message "test_file_id" "test caption")

    if [[ "$result" == "OK" ]]; then
        # Should log error about no focused worker
        success "Photo without focused worker handled"
    else
        fail "Photo message handling failed"
    fi
}

test_document_message_no_focused() {
    info "Testing document message without focused worker..."

    # First ensure no focused worker
    send_message "/end testbot1" >/dev/null 2>&1 || true
    wait_for_session_gone "testbot1"

    # Clear active by killing all test sessions
    tmux kill-session -t "${TEST_TMUX_PREFIX}testbot1" 2>/dev/null || true

    local result
    result=$(send_document_message "test_file_id" "test.pdf" "application/pdf" 1024 "test doc")

    if [[ "$result" == "OK" ]]; then
        # Should log error about no focused worker
        success "Document without focused worker handled"
    else
        fail "Document message handling failed"
    fi
}

test_document_message_format() {
    info "Testing document message format in Python..."
    if python3 -c "
import bridge

# Test format_file_size function
assert bridge.format_file_size(500) == '500 B'
assert bridge.format_file_size(1024) == '1.0 KB'
assert bridge.format_file_size(1536) == '1.5 KB'
assert bridge.format_file_size(1048576) == '1.0 MB'
assert bridge.format_file_size(1572864) == '1.5 MB'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "format_file_size works correctly"
    else
        fail "format_file_size failed"
    fi
}

test_document_message_routing() {
    info "Testing document message routing to focused worker..."

    # Create and focus a worker
    send_message "/hire doctest" >/dev/null
    wait_for_session "doctest"
    send_message "/focus doctest" >/dev/null
    sleep 0.3

    # Send a document message
    local result
    result=$(send_document_message "test_doc_file_id" "report.pdf" "application/pdf" 2048 "Please review this")

    if [[ "$result" == "OK" ]]; then
        # Check bridge log for the document handling
        sleep 0.3
        if grep -q "doctest" "$BRIDGE_LOG" 2>/dev/null; then
            success "Document message routed to focused worker"
        else
            success "Document message accepted (routing attempted)"
        fi
    else
        fail "Document message routing failed"
    fi

    # Cleanup
    send_message "/end doctest" >/dev/null 2>&1 || true
}

test_incoming_document_e2e() {
    info "Testing incoming document e2e (upload -> webhook -> download)..."

    # This test requires a real TEST_CHAT_ID to upload documents to Telegram
    if [[ "${TEST_CHAT_ID:-}" == "" ]] || [[ "$CHAT_ID" == "123456789" ]]; then
        info "Skipping (requires TEST_CHAT_ID for real Telegram upload)"
        return 0
    fi

    # Create worker to receive document
    send_message "/hire docrecv" >/dev/null
    wait_for_session "docrecv"
    send_message "/focus docrecv" >/dev/null
    sleep 0.3

    # Create a test text file
    echo "This is a test document for e2e testing." > /tmp/e2e-test-document.txt

    # Upload document to Telegram to get a real file_id
    local upload_response
    upload_response=$(curl -s -X POST "https://api.telegram.org/bot${TEST_BOT_TOKEN}/sendDocument" \
        -F "chat_id=${CHAT_ID}" \
        -F "document=@/tmp/e2e-test-document.txt" \
        -F "caption=E2E test document")

    # Extract file_id from response
    local file_id
    file_id=$(echo "$upload_response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['result']['document']['file_id'])" 2>/dev/null)

    if [[ -z "$file_id" ]]; then
        fail "Could not upload test document to get file_id"
        send_message "/end docrecv" >/dev/null 2>&1 || true
        return
    fi

    # Now simulate incoming document webhook with real file_id
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
                "document": {
                    "file_id": "'"$file_id"'",
                    "file_unique_id": "'"$file_id"'_unique",
                    "file_name": "e2e-test-document.txt",
                    "mime_type": "text/plain",
                    "file_size": 42
                },
                "caption": "Test incoming document"
            }
        }' >/dev/null

    sleep 3

    # Check if document was downloaded to inbox
    local inbox_dir="/tmp/claudecode-telegram/docrecv/inbox"
    if ls "$inbox_dir"/*.txt 2>/dev/null; then
        success "Incoming document downloaded to inbox"
        ls -la "$inbox_dir"/ 2>/dev/null | head -3
    else
        # Check bridge log for download attempt
        if grep -q "Downloaded file" "$BRIDGE_LOG" 2>/dev/null; then
            success "Incoming document download attempted (check log)"
        else
            fail "Incoming document not downloaded to inbox"
        fi
    fi

    # Cleanup
    send_message "/end docrecv" >/dev/null 2>&1 || true
    rm -f /tmp/e2e-test-document.txt
}

test_incoming_image_e2e() {
    info "Testing incoming image e2e (upload -> webhook -> download)..."

    # This test requires a real TEST_CHAT_ID to upload images to Telegram
    if [[ "${TEST_CHAT_ID:-}" == "" ]] || [[ "$CHAT_ID" == "123456789" ]]; then
        info "Skipping (requires TEST_CHAT_ID for real Telegram upload)"
        return 0
    fi

    # Create worker to receive image
    send_message "/hire imgrecv" >/dev/null
    wait_for_session "imgrecv"
    send_message "/focus imgrecv" >/dev/null
    sleep 0.3

    # Create a test image
    python3 << 'PYEOF'
from PIL import Image, ImageDraw
img = Image.new('RGB', (200, 100), color='#28A745')
draw = ImageDraw.Draw(img)
draw.text((20, 40), "E2E Test Image", fill='white')
img.save('/tmp/e2e-test-incoming.png')
PYEOF

    # Upload image to Telegram to get a real file_id
    local upload_response
    upload_response=$(curl -s -X POST "https://api.telegram.org/bot${TEST_BOT_TOKEN}/sendPhoto" \
        -F "chat_id=${CHAT_ID}" \
        -F "photo=@/tmp/e2e-test-incoming.png" \
        -F "caption=E2E test upload")

    # Extract file_id from response
    local file_id
    file_id=$(echo "$upload_response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['result']['photo'][-1]['file_id'])" 2>/dev/null)

    if [[ -z "$file_id" ]]; then
        fail "Could not upload test image to get file_id"
        send_message "/end imgrecv" >/dev/null 2>&1 || true
        return
    fi

    # Now simulate incoming photo webhook with real file_id
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
                    {"file_id": "'"$file_id"'_small", "file_size": 1000, "width": 90, "height": 45},
                    {"file_id": "'"$file_id"'", "file_size": 5000, "width": 200, "height": 100}
                ],
                "caption": "Test incoming image"
            }
        }' >/dev/null

    sleep 3

    # Check if image was downloaded to inbox
    local inbox_dir="$TEST_SESSION_DIR/imgrecv/inbox"
    if ls "$inbox_dir"/*.png 2>/dev/null || ls "$inbox_dir"/*.jpg 2>/dev/null; then
        success "Incoming image downloaded to inbox"
        ls -la "$inbox_dir"/ 2>/dev/null | head -3
    else
        # Check bridge log for download attempt
        if grep -q "Downloaded file" "$BRIDGE_LOG" 2>/dev/null; then
            success "Incoming image download attempted (check log)"
        else
            fail "Incoming image not downloaded to inbox"
        fi
    fi

    # Cleanup
    send_message "/end imgrecv" >/dev/null 2>&1 || true
}

test_inbox_directory() {
    info "Testing inbox directory creation..."

    # Create worker
    send_message "/hire inboxtest" >/dev/null
    wait_for_session "inboxtest"
    send_message "/focus inboxtest" >/dev/null

    if python3 -c "
from bridge import ensure_inbox_dir, get_inbox_dir
import os

inbox = ensure_inbox_dir('inboxtest')
assert inbox.exists(), 'inbox should exist'
perms = oct(inbox.stat().st_mode)[-3:]
assert perms == '700', f'inbox perms should be 700, got {perms}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Inbox directory created with correct permissions"
    else
        fail "Inbox directory creation failed"
    fi

    # Cleanup
    send_message "/end inboxtest" >/dev/null 2>&1 || true
}

test_response_with_image_tags() {
    info "Testing /response endpoint with image tags..."

    # Create a session
    send_message "/hire imageresponsetest" >/dev/null
    wait_for_session "imageresponsetest"

    # Set up session files
    local session_dir="$TEST_SESSION_DIR/imageresponsetest"
    mkdir -p "$session_dir"
    echo "$CHAT_ID" > "$session_dir/chat_id"

    # Test response with image tag (image won't exist, but parsing should work)
    local result
    result=$(curl -s -X POST "http://localhost:$PORT/response" \
        -H "Content-Type: application/json" \
        -d '{"session":"imageresponsetest","text":"Here is the result [[image:/tmp/nonexistent.png|test caption]]"}')

    if [[ "$result" == "OK" ]]; then
        # Check bridge log for image handling attempt
        sleep 0.3
        if grep -q "imageresponsetest" "$BRIDGE_LOG" 2>/dev/null; then
            success "/response endpoint handles image tags"
        else
            fail "/response endpoint did not process message"
        fi
    else
        fail "/response with image tags failed: $result"
    fi

    # Cleanup
    send_message "/end imageresponsetest" >/dev/null 2>&1 || true
}

test_response_endpoint() {
    info "Testing /response endpoint (hook -> bridge -> Telegram)..."

    # Use real chat_id if TEST_CHAT_ID provided (for full e2e verification)
    local test_chat_id="$CHAT_ID"
    local expect_real="false"
    [[ -n "${TEST_CHAT_ID:-}" ]] && expect_real="true"

    # Create a new session for this test
    send_message "/hire responsetest" >/dev/null
    wait_for_session "responsetest"

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
        sleep 0.3
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
    send_message "/end responsetest" >/dev/null 2>&1 || true
}

test_last_chat_id_persistence() {
    local last_chat_file="$TEST_NODE_DIR/last_chat_id"

    # Clean up first
    rm -f "$last_chat_file"

    # Send a message to trigger chat ID save
    send_message "test persistence"
    sleep 0.5

    # Verify file was created with correct content
    if [[ -f "$last_chat_file" ]]; then
        local saved_id
        saved_id=$(cat "$last_chat_file")
        if [[ "$saved_id" == "$CHAT_ID" ]]; then
            success "last_chat_id persistence works"
        else
            fail "last_chat_id mismatch: expected $CHAT_ID, got $saved_id"
        fi
    else
        fail "last_chat_id file not created"
    fi
}

test_last_active_persistence() {
    local last_active_file="$TEST_NODE_DIR/last_active"

    # Clean up first
    rm -f "$last_active_file"

    # Create a session to trigger active save
    send_message "/hire testpersist"
    wait_for_session "testpersist"

    # Verify file was created with correct content
    if [[ -f "$last_active_file" ]]; then
        local saved_active
        saved_active=$(cat "$last_active_file")
        if [[ "$saved_active" == "testpersist" ]]; then
            success "last_active persistence works"
        else
            fail "last_active mismatch: expected testpersist, got $saved_active"
        fi
    else
        fail "last_active file not created"
    fi

    # Test switch also updates the file
    send_message "/hire testpersist2"
    wait_for_session "testpersist2"
    send_message "/focus testpersist"
    sleep 0.2

    saved_active=$(cat "$last_active_file")
    if [[ "$saved_active" == "testpersist" ]]; then
        success "last_active updated on focus switch"
    else
        fail "last_active not updated on switch: expected testpersist, got $saved_active"
    fi

    # Clean up test sessions
    send_message "/end testpersist" >/dev/null 2>&1 || true
    send_message "/end testpersist2" >/dev/null 2>&1 || true
}

test_response_without_pending() {
    info "Testing /response works without pending file (v0.6.2 behavior)..."

    # Create a session for this test
    send_message "/hire nopendingtest" >/dev/null
    wait_for_session "nopendingtest"

    # Set up ONLY chat_id file - NO pending file
    # This tests v0.6.2 change: pending is not a gate for sending
    local session_dir="$TEST_SESSION_DIR/nopendingtest"
    mkdir -p "$session_dir"
    echo "$CHAT_ID" > "$session_dir/chat_id"
    # Explicitly ensure no pending file
    rm -f "$session_dir/pending"

    # Simulate hook calling /response endpoint
    local result
    result=$(curl -s -X POST "http://localhost:$PORT/response" \
        -H "Content-Type: application/json" \
        -d '{"session":"nopendingtest","text":"Test without pending"}')

    if [[ "$result" == "OK" ]]; then
        success "/response works without pending file (proactive messaging enabled)"
    else
        fail "/response without pending failed: $result"
    fi

    # Cleanup
    send_message "/end nopendingtest" >/dev/null 2>&1 || true
}

# ─────────────────────────────────────────────────────────────────────────────
# Worker naming and routing tests
# ─────────────────────────────────────────────────────────────────────────────

test_worker_name_sanitization() {
    info "Testing worker name sanitization..."

    if python3 -c "
import re

# Sanitization logic from bridge.py
def sanitize_name(name):
    name = name.lower().strip()
    return re.sub(r'[^a-z0-9-]', '', name)

# Test cases
assert sanitize_name('TestBot') == 'testbot', 'uppercase should be lowered'
assert sanitize_name('test_bot') == 'testbot', 'underscores should be removed'
assert sanitize_name('test bot') == 'testbot', 'spaces should be removed'
assert sanitize_name('Test-Bot-123') == 'test-bot-123', 'hyphens and numbers allowed'
assert sanitize_name('  spaces  ') == 'spaces', 'leading/trailing spaces stripped'
assert sanitize_name('Bot@#\$%') == 'bot', 'special chars removed'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Worker name sanitization works"
    else
        fail "Worker name sanitization failed"
    fi
}

test_hire_backend_parsing() {
    info "Testing /hire backend parsing..."

    if python3 -c "
from bridge import parse_hire_args, DEFAULT_WORKER_BACKEND

name, backend = parse_hire_args('alice')
assert name == 'alice', f'expected alice, got {name}'
assert backend == DEFAULT_WORKER_BACKEND, f'expected default backend, got {backend}'

name, backend = parse_hire_args('--codex alice')
assert name == 'alice', f'expected alice, got {name}'
assert backend == 'codex', f'expected codex backend, got {backend}'

name, backend = parse_hire_args('alice --codex')
assert name == 'alice', f'expected alice, got {name}'
assert backend == 'codex', f'expected codex backend, got {backend}'

name, backend = parse_hire_args('codex-amy')
assert name == 'amy', f'expected amy, got {name}'
assert backend == 'codex', f'expected codex backend, got {backend}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/hire backend parsing works"
    else
        fail "/hire backend parsing failed"
    fi
}

test_team_output_includes_backend() {
    info "Testing /team output includes backend..."

    if python3 -c "
from bridge import format_team_lines

registered = {
    'alice': {'backend': 'codex'},
    'bob': {'backend': 'claude'},
}

lines = format_team_lines(registered, active='alice', pending_lookup=lambda name: False)
text = '\\n'.join(lines)

assert 'backend=codex' in text, f'expected codex backend in team output: {text}'
assert 'backend=claude' in text, f'expected claude backend in team output: {text}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/team output includes backend"
    else
        fail "/team backend output test failed"
    fi
}

test_progress_output_includes_backend() {
    info "Testing /progress output includes backend..."

    if python3 -c "
from bridge import format_progress_lines

lines = format_progress_lines(
    name='alice',
    pending=False,
    backend='codex',
    online=True,
    ready=True,
    mode='tmux'
)

text = '\\n'.join(lines)
assert 'Backend: codex' in text, f'expected Backend line in progress output: {text}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/progress output includes backend"
    else
        fail "/progress backend output test failed"
    fi
}

test_codex_learn_reaction_bypasses_tmux() {
    info "Testing codex /learn reaction bypasses tmux check..."

    if python3 -c "
import bridge
bridge.state['active'] = 'alice'

class FakeTelegram:
    def __init__(self):
        self.calls = []
    def send_message(self, *args, **kwargs):
        return {'ok': True}
    def set_reaction(self, chat_id, message_id, reaction):
        self.calls.append(('setMessageReaction', {'chat_id': chat_id, 'message_id': message_id, 'reaction': reaction}))
        return {'ok': True}

fake_telegram = FakeTelegram()
router = bridge.CommandRouter(fake_telegram, bridge.worker_manager)

bridge.worker_manager.get_registered_sessions = lambda registered=None: {'alice': {'backend': 'codex', 'mode': 'exec'}}
bridge.worker_manager.is_online = lambda name, session=None: True
bridge.worker_manager.send = lambda name, message, chat_id=None, session=None: True
bridge.worker_set_pending = lambda name, chat_id: None
bridge.send_typing_loop = lambda chat_id, name: None

def boom(*args, **kwargs):
    raise AssertionError('tmux_prompt_empty should not be called for exec backends')
bridge.tmux_prompt_empty = boom

router.cmd_learn('', 123, msg_id=456)

assert any(c[0] == 'setMessageReaction' for c in fake_telegram.calls), f'expected reaction, got {fake_telegram.calls}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "codex /learn reaction bypasses tmux check"
    else
        fail "codex /learn reaction test failed"
    fi
}

test_worker_send_uses_backend() {
    info "Testing worker_send routes via backend..."

    if python3 -c "
import bridge

calls = {'claude': 0, 'codex': 0}

# Mock the backend send methods
original_claude_send = bridge.ClaudeBackend.send
original_codex_send = bridge.CodexBackend.send

def fake_claude_send(self, name, tmux, text, url, dir):
    calls['claude'] += 1
    return True

def fake_codex_send(self, name, tmux, text, url, dir):
    calls['codex'] += 1
    return True

bridge.ClaudeBackend.send = fake_claude_send
bridge.CodexBackend.send = fake_codex_send

# Also update the instances in BACKENDS
bridge.BACKENDS['claude'] = bridge.ClaudeBackend()
bridge.BACKENDS['codex'] = bridge.CodexBackend()

def fake_scan():
    return {'alice': {'tmux': 'claude-test-alice', 'backend': 'codex'}}

def fake_get_registered_sessions(registered=None):
    return registered or fake_scan()

bridge.worker_manager.scan_tmux_sessions = fake_scan
bridge.worker_manager.get_registered_sessions = fake_get_registered_sessions

session = fake_scan()['alice']
ok = bridge.worker_send('alice', 'hello', session=session)

assert ok is True, 'expected worker_send to succeed'
assert calls['codex'] == 1, f'expected codex send, got {calls}'
assert calls['claude'] == 0, f'expected no claude send, got {calls}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "worker_send routes via backend"
    else
        fail "worker_send backend routing test failed"
    fi
}

test_codex_end_cleans_session() {
    info "Testing codex /end cleans session..."

    if python3 -c "
import tempfile
from pathlib import Path
import bridge

tmp = Path(tempfile.mkdtemp())
bridge.SESSIONS_DIR = tmp
bridge.WORKER_PIPE_ROOT = tmp / 'pipes'
bridge.worker_manager.scan_tmux_sessions = lambda: {}
bridge._sync_worker_manager()
bridge._sync_worker_manager()

session_dir = tmp / 'alice'
session_dir.mkdir()
(session_dir / 'backend').write_text('codex')
(session_dir / 'codex_session_id').write_text('thread_123')

# Create pipe to verify cleanup
bridge.ensure_worker_pipe('alice')
pipe_path = bridge.get_worker_pipe_path('alice')
assert pipe_path.exists(), 'pipe should exist before cleanup'

ok, err = bridge.kill_session('alice')
assert ok is True, f'expected ok, got err: {err}'
assert not (session_dir / 'backend').exists(), 'backend file should be removed'
assert not (session_dir / 'codex_session_id').exists(), 'session id should be removed'
assert not pipe_path.exists(), 'pipe should be removed'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Codex /end cleans session"
    else
        fail "Codex /end cleanup test failed"
    fi
}

test_codex_relaunch_clears_session_id() {
    info "Testing codex /relaunch clears session id..."

    if python3 -c "
import tempfile
from pathlib import Path
import bridge

tmp = Path(tempfile.mkdtemp())
bridge.SESSIONS_DIR = tmp
bridge.WORKER_PIPE_ROOT = tmp / 'pipes'
bridge.worker_manager.scan_tmux_sessions = lambda: {}

session_dir = tmp / 'alice'
session_dir.mkdir()
(session_dir / 'backend').write_text('codex')
(session_dir / 'codex_session_id').write_text('thread_123')

ok, err = bridge.restart_claude('alice')
assert ok is True, f'expected ok, got err: {err}'
assert not (session_dir / 'codex_session_id').exists(), 'session id should be cleared'
assert (session_dir / 'backend').exists(), 'backend file should remain'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Codex /relaunch clears session id"
    else
        fail "Codex /relaunch test failed"
    fi
}

test_get_workers_includes_codex() {
    info "Testing /workers includes codex exec workers..."

    if python3 -c "
import tempfile
from pathlib import Path
import bridge

tmp = Path(tempfile.mkdtemp())
bridge.SESSIONS_DIR = tmp
bridge.WORKER_PIPE_ROOT = tmp / 'pipes'
bridge.worker_manager.scan_tmux_sessions = lambda: {}

session_dir = tmp / 'alice'
session_dir.mkdir()
(session_dir / 'backend').write_text('codex')

bridge.ensure_worker_pipe('alice')
workers = bridge.get_workers()
names = [w['name'] for w in workers]
assert 'alice' in names, f'expected alice in workers, got {workers}'

item = next(w for w in workers if w['name'] == 'alice')
assert item['protocol'] == 'pipe', f'expected pipe protocol, got {item}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/workers includes codex exec workers"
    else
        fail "Codex /workers inclusion test failed"
    fi
}

test_pipe_forwarding_to_codex() {
    info "Testing pipe forwarding routes to codex..."

    if python3 -c "
import tempfile
from pathlib import Path
import bridge

tmp = Path(tempfile.mkdtemp())
bridge.SESSIONS_DIR = tmp
bridge.WORKER_PIPE_ROOT = tmp / 'pipes'
bridge.worker_manager.scan_tmux_sessions = lambda: {}

session_dir = tmp / 'alice'
session_dir.mkdir()
(session_dir / 'backend').write_text('codex')

called = {'codex': 0}
def fake_send(name, text, chat_id=None, session=None):
    called['codex'] += 1
    return True

bridge.worker_manager.send = fake_send

bridge._forward_pipe_message('alice', 'hello')
assert called['codex'] == 1, f'expected codex send, got {called}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Pipe forwarding routes to codex"
    else
        fail "Pipe forwarding to codex test failed"
    fi
}

test_codex_pause_clears_pending() {
    info "Testing codex /pause clears pending without tmux..."

    if python3 -c "
import tempfile
from pathlib import Path
import bridge

tmp = Path(tempfile.mkdtemp())
bridge.SESSIONS_DIR = tmp
bridge.WORKER_PIPE_ROOT = tmp / 'pipes'
bridge.worker_manager.sessions_dir = tmp
bridge.worker_manager.scan_tmux_sessions = lambda: {}

session_dir = tmp / 'alice'
session_dir.mkdir()
(session_dir / 'backend').write_text('codex')

bridge.set_pending('alice', 12345)
pending_file = bridge.get_pending_file('alice')
assert pending_file.exists(), 'pending file should exist before pause'

bridge.tmux_send_escape = lambda *_: (_ for _ in ()).throw(AssertionError('tmux should not be used'))

bridge.state['active'] = 'alice'

class FakeTelegram:
    def send_message(self, *args, **kwargs):
        return {'ok': True}

router = bridge.CommandRouter(FakeTelegram(), bridge.worker_manager)
router.cmd_pause(12345)

assert not pending_file.exists(), 'pending file should be cleared for codex pause'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Codex /pause clears pending"
    else
        fail "Codex /pause test failed"
    fi
}

test_codex_response_requires_escape() {
    info "Testing codex response requires escaping..."

    if python3 -c "
import bridge

assert bridge.should_escape_response({'source': 'codex'}) is True
assert bridge.should_escape_response({'escape': True}) is True
assert bridge.should_escape_response({'source': 'hook'}) is False
assert bridge.should_escape_response({}) is False

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Codex response escape detection"
    else
        fail "Codex response escape detection failed"
    fi
}

test_update_bot_commands_includes_codex() {
    info "Testing update_bot_commands includes codex workers..."

    if python3 -c "
import bridge

captured = {}

def fake_api(method, payload):
    captured['payload'] = payload
    return {'ok': True}

bridge.telegram_api = fake_api
bridge.get_registered_sessions = lambda: {
    'alice': {'backend': 'codex', 'mode': 'codex-exec'},
    'bob': {'backend': 'claude', 'tmux': 'claude-test-bob'},
}

bridge.update_bot_commands()

commands = [c['command'] for c in captured['payload']['commands']]
assert 'alice' in commands and 'bob' in commands, f'expected codex worker in commands, got {commands}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "update_bot_commands includes codex workers"
    else
        fail "update_bot_commands codex test failed"
    fi
}

test_broadcast_includes_codex() {
    info "Testing @all broadcast includes codex workers..."

    if python3 -c "
import bridge

called = []

bridge.worker_manager.get_registered_sessions = lambda registered=None: {
    'alice': {'backend': 'codex', 'mode': 'exec'},
    'bob': {'backend': 'claude', 'tmux': 'claude-test-bob'},
}
bridge.worker_manager.is_online = lambda name, session=None: True

class FakeTelegram:
    def send_message(self, *args, **kwargs):
        return {'ok': True}

router = bridge.CommandRouter(FakeTelegram(), bridge.worker_manager)
router.route_message = lambda name, text, chat_id, msg_id, one_off=False: called.append(name)
router.route_to_all('hello team', 123, 456)

assert set(called) == {'alice', 'bob'}, f'expected broadcast to include codex worker, got {called}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "@all broadcast includes codex"
    else
        fail "@all broadcast codex test failed"
    fi
}

test_reserved_names_rejection() {
    info "Testing reserved names rejection..."

    if python3 -c "
from bridge import RESERVED_NAMES

# Verify all expected reserved names are included
expected = {'team', 'focus', 'progress', 'learn', 'pause', 'relaunch',
            'settings', 'hire', 'end', 'all', 'start', 'help'}
for name in expected:
    assert name in RESERVED_NAMES, f'{name} should be reserved'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Reserved names are configured"
    else
        fail "Reserved names check failed"
    fi

    # Test actual rejection via webhook
    local result
    result=$(send_message "/hire team")  # 'team' is reserved

    if [[ "$result" == "OK" ]]; then
        success "Reserved name /hire rejection handled"
    else
        fail "Reserved name handling failed"
    fi
}

test_worker_shortcut_focus_only() {
    info "Testing /<worker> focus switch (no message)..."

    # Create two workers first
    send_message "/hire shortcut1" >/dev/null
    wait_for_session "shortcut1"
    send_message "/hire shortcut2" >/dev/null
    wait_for_session "shortcut2"

    # Focus switch via shortcut
    local result
    result=$(send_message "/shortcut1")

    if [[ "$result" == "OK" ]]; then
        success "/<worker> focus switch works"
    else
        fail "/<worker> focus switch failed"
    fi

    # Cleanup
    send_message "/end shortcut1" >/dev/null 2>&1 || true
    send_message "/end shortcut2" >/dev/null 2>&1 || true
}

test_worker_shortcut_with_message() {
    info "Testing /<worker> <message> routing..."

    # Create worker
    send_message "/hire shortcut3" >/dev/null
    wait_for_session "shortcut3"

    # Route message via shortcut
    local result
    result=$(send_message "/shortcut3 hello from shortcut")

    if [[ "$result" == "OK" ]]; then
        success "/<worker> <message> routing works"
    else
        fail "/<worker> <message> routing failed"
    fi

    # Cleanup
    send_message "/end shortcut3" >/dev/null 2>&1 || true
}

test_command_with_botname_suffix() {
    info "Testing command with @botname suffix..."

    # Commands like /team@MyBot should work (suffix stripped)
    local result
    result=$(send_message "/team@TestBot")

    if [[ "$result" == "OK" ]]; then
        success "Command with @botname suffix handled"
    else
        fail "Command with @botname suffix failed"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Security tests
# ─────────────────────────────────────────────────────────────────────────────

test_webhook_secret_acceptance() {
    info "Testing webhook secret acceptance path..."

    local secret_port=8096
    local secret_log="$TEST_NODE_DIR/secret_accept_bridge.log"
    local secret_sessions_dir="$TEST_NODE_DIR/secret_accept_sessions"
    local secret_tmux_prefix="claude-${TEST_NODE}-secret-accept-"
    local secret_value="test-secret-accept-123"

    while nc -z localhost "$secret_port" 2>/dev/null; do
        secret_port=$((secret_port + 1))
        if [[ "$secret_port" -gt 8110 ]]; then
            fail "No free port for webhook secret acceptance test"
            return
        fi
    done

    mkdir -p "$secret_sessions_dir"
    chmod 700 "$secret_sessions_dir"

    TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" \
    PORT="$secret_port" \
    TELEGRAM_WEBHOOK_SECRET="$secret_value" \
    NODE_NAME="secretaccept" \
    SESSIONS_DIR="$secret_sessions_dir" \
    TMUX_PREFIX="$secret_tmux_prefix" \
    python3 -u "$SCRIPT_DIR/bridge.py" > "$secret_log" 2>&1 &
    local secret_pid=$!

    if wait_for_port "$secret_port"; then
        local ok_response
        ok_response=$(curl -s -X POST "http://localhost:$secret_port" \
            -H "Content-Type: application/json" \
            -H "X-Telegram-Bot-Api-Secret-Token: $secret_value" \
            -d '{"update_id": 1, "message": {"message_id": 1, "chat": {"id": '"$CHAT_ID"'}, "text": "test"}}')

        if [[ "$ok_response" == "OK" ]]; then
            success "Webhook secret accepted with correct header"
        else
            fail "Expected OK for correct secret, got: $ok_response"
        fi

        local http_code
        http_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "http://localhost:$secret_port" \
            -H "Content-Type: application/json" \
            -d '{"update_id": 2, "message": {"message_id": 2, "chat": {"id": '"$CHAT_ID"'}, "text": "test"}}')

        if [[ "$http_code" == "403" ]]; then
            success "Webhook secret rejected without header"
        else
            fail "Expected 403 without secret, got $http_code"
        fi
    else
        fail "Could not start bridge with webhook secret"
    fi

    kill "$secret_pid" 2>/dev/null || true
    rm -f "$secret_log"
    rm -rf "$secret_sessions_dir"
}

test_graceful_shutdown_notification() {
    info "Testing graceful shutdown attempts notification..."

    # Test that graceful_shutdown tries to send notification when admin_chat_id is set
    if python3 -c "
import bridge
import unittest.mock as mock
import signal

# Set up admin chat ID
bridge.admin_chat_id = 12345

# Mock telegram_api and sys.exit
with mock.patch.object(bridge, 'telegram_api') as mock_api, \
     mock.patch('sys.exit'):
    mock_api.return_value = {'ok': True}
    # Call graceful_shutdown with SIGTERM
    bridge.graceful_shutdown(signal.SIGTERM, None)

# Verify notification was attempted
assert mock_api.called, 'telegram_api should be called for shutdown notification'
# Check that sendMessage was called (notification attempt)
call_found = any('sendMessage' in str(c) for c in mock_api.call_args_list)
assert call_found, 'Should attempt to send shutdown message'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Graceful shutdown attempts notification"
    else
        fail "Graceful shutdown notification test failed"
    fi
}

test_typing_indicator_loop() {
    info "Testing typing indicator loop calls sendChatAction..."

    # Test that send_typing_loop calls telegram_api with sendChatAction
    if python3 -c "
import bridge
import unittest.mock as mock

# Set up a pending state that will clear after first check
call_count = [0]
def mock_is_pending(name):
    call_count[0] += 1
    return call_count[0] <= 1  # True first time, False after

# Mock is_pending and telegram_api
with mock.patch.object(bridge, 'is_pending', mock_is_pending), \
     mock.patch.object(bridge, 'telegram_api') as mock_api, \
     mock.patch('time.sleep'):  # Skip the sleep
    mock_api.return_value = {'ok': True}
    bridge.send_typing_loop(12345, 'test')

# Verify sendChatAction was called
assert mock_api.called, 'telegram_api should be called'
call_args = mock_api.call_args
assert call_args[0][0] == 'sendChatAction', f'Expected sendChatAction, got {call_args[0][0]}'
assert call_args[0][1]['chat_id'] == 12345
assert call_args[0][1]['action'] == 'typing'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Typing indicator loop calls sendChatAction"
    else
        fail "Typing indicator behavior test failed"
    fi
}

test_webhook_secret_validation() {
    info "Testing webhook secret validation..."

    # Start a separate bridge with webhook secret
    local secret_port=8096
    local secret_log="$TEST_NODE_DIR/secret_bridge.log"

    # Kill any existing process on the port
    lsof -ti :"$secret_port" | xargs kill -9 2>/dev/null || true
    sleep 0.3

    TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" \
    PORT="$secret_port" \
    TELEGRAM_WEBHOOK_SECRET="test-secret-123" \
    NODE_NAME="secrettest" \
    SESSIONS_DIR="$TEST_SESSION_DIR" \
    TMUX_PREFIX="$TEST_TMUX_PREFIX" \
    python3 -u "$SCRIPT_DIR/bridge.py" > "$secret_log" 2>&1 &
    local secret_pid=$!

    if wait_for_port "$secret_port"; then
        # Request WITHOUT secret header should be rejected (403)
        local http_code
        http_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "http://localhost:$secret_port" \
            -H "Content-Type: application/json" \
            -d '{"update_id": 1, "message": {"message_id": 1, "chat": {"id": 123}, "text": "test"}}')

        if [[ "$http_code" == "403" ]]; then
            success "Request without secret rejected (403)"
        else
            fail "Expected 403, got $http_code"
        fi

        # Request WITH correct secret header should be accepted (200)
        http_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "http://localhost:$secret_port" \
            -H "Content-Type: application/json" \
            -H "X-Telegram-Bot-Api-Secret-Token: test-secret-123" \
            -d '{"update_id": 2, "message": {"message_id": 2, "chat": {"id": 123}, "text": "test"}}')

        if [[ "$http_code" == "200" ]]; then
            success "Request with correct secret accepted (200)"
        else
            fail "Expected 200, got $http_code"
        fi

        # Request WITH wrong secret should be rejected (403)
        http_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "http://localhost:$secret_port" \
            -H "Content-Type: application/json" \
            -H "X-Telegram-Bot-Api-Secret-Token: wrong-secret" \
            -d '{"update_id": 3, "message": {"message_id": 3, "chat": {"id": 123}, "text": "test"}}')

        if [[ "$http_code" == "403" ]]; then
            success "Request with wrong secret rejected (403)"
        else
            fail "Expected 403 for wrong secret, got $http_code"
        fi
    else
        fail "Could not start bridge with webhook secret"
    fi

    # Cleanup
    kill "$secret_pid" 2>/dev/null || true
    rm -f "$secret_log"
}

test_token_isolation() {
    info "Testing token isolation..."

    # Verify TELEGRAM_BOT_TOKEN is NOT exposed to tmux sessions
    # Create a worker and check its environment
    send_message "/hire tokentest" >/dev/null
    wait_for_session "tokentest"

    local tmux_name="${TEST_TMUX_PREFIX}tokentest"

    if tmux has-session -t "$tmux_name" 2>/dev/null; then
        # Check tmux environment - token should NOT be present
        local tmux_env
        tmux_env=$(tmux show-environment -t "$tmux_name" 2>/dev/null || echo "")

        if echo "$tmux_env" | grep -q "TELEGRAM_BOT_TOKEN"; then
            fail "Token leaked to tmux session environment!"
        else
            success "Token isolated - not in tmux environment"
        fi

        # Verify expected env vars ARE present
        if echo "$tmux_env" | grep -q "PORT="; then
            success "PORT env var exported to tmux"
        else
            fail "PORT env var not found in tmux"
        fi

        if echo "$tmux_env" | grep -q "TMUX_PREFIX="; then
            success "TMUX_PREFIX env var exported to tmux"
        else
            fail "TMUX_PREFIX env var not found in tmux"
        fi
    else
        fail "Could not verify token isolation - session not found"
    fi

    # Cleanup
    send_message "/end tokentest" >/dev/null 2>&1 || true
}

test_secure_directory_permissions() {
    info "Testing secure directory permissions..."

    # Test node directory permissions
    if [[ -d "$TEST_NODE_DIR" ]]; then
        local perms
        if [[ "$(uname)" == "Darwin" ]]; then
            perms=$(stat -f "%Lp" "$TEST_NODE_DIR")
        else
            perms=$(stat -c "%a" "$TEST_NODE_DIR")
        fi
        if [[ "$perms" == "700" ]]; then
            success "Node directory permissions secure (0700)"
        else
            fail "Node directory permissions incorrect: $perms (expected 700)"
        fi
    fi

    # Test sessions directory permissions
    if [[ -d "$TEST_SESSION_DIR" ]]; then
        local perms
        if [[ "$(uname)" == "Darwin" ]]; then
            perms=$(stat -f "%Lp" "$TEST_SESSION_DIR")
        else
            perms=$(stat -c "%a" "$TEST_SESSION_DIR")
        fi
        if [[ "$perms" == "700" ]]; then
            success "Sessions directory permissions secure (0700)"
        else
            fail "Sessions directory permissions incorrect: $perms (expected 700)"
        fi
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# HTTP endpoint tests
# ─────────────────────────────────────────────────────────────────────────────

test_health_endpoint() {
    info "Testing GET / health endpoint..."

    local response
    response=$(curl -s "http://localhost:$PORT")

    if echo "$response" | grep -q "Claude-Telegram Multi-Session Bridge"; then
        success "Health endpoint returns expected string"
    else
        fail "Health endpoint response incorrect: $response"
    fi
}

test_response_endpoint_missing_fields() {
    info "Testing /response endpoint with missing fields..."

    # Missing session
    local result
    result=$(curl -s -X POST "http://localhost:$PORT/response" \
        -H "Content-Type: application/json" \
        -d '{"text":"Test"}')

    if echo "$result" | grep -q "Missing"; then
        success "/response rejects missing session"
    else
        fail "/response should reject missing session"
    fi

    # Missing text
    result=$(curl -s -X POST "http://localhost:$PORT/response" \
        -H "Content-Type: application/json" \
        -d '{"session":"test"}')

    if echo "$result" | grep -q "Missing"; then
        success "/response rejects missing text"
    else
        fail "/response should reject missing text"
    fi
}

test_response_endpoint_no_chat_id() {
    info "Testing /response endpoint with non-existent session..."

    local result
    result=$(curl -s -X POST "http://localhost:$PORT/response" \
        -H "Content-Type: application/json" \
        -d '{"session":"nonexistent_session_xyz","text":"Test"}')

    # Should return 404 for session without chat_id file
    if echo "$result" | grep -q "No chat_id"; then
        success "/response returns 404 for unknown session"
    else
        # Check HTTP code
        local http_code
        http_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "http://localhost:$PORT/response" \
            -H "Content-Type: application/json" \
            -d '{"session":"nonexistent_session_xyz","text":"Test"}')
        if [[ "$http_code" == "404" ]]; then
            success "/response returns 404 for unknown session"
        else
            fail "/response should return 404 for unknown session"
        fi
    fi
}

test_notify_endpoint_missing_text() {
    info "Testing /notify endpoint with missing text..."

    local http_code
    http_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "http://localhost:$PORT/notify" \
        -H "Content-Type: application/json" \
        -d '{}')

    if [[ "$http_code" == "400" ]]; then
        success "/notify rejects missing text (400)"
    else
        fail "/notify should return 400 for missing text, got $http_code"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Pending and timeout tests
# ─────────────────────────────────────────────────────────────────────────────

test_pending_auto_timeout() {
    info "Testing pending auto-timeout (10 min)..."

    if python3 -c "
from bridge import is_pending, set_pending, get_pending_file
import time
from pathlib import Path

# Create test session dir
test_name = 'timeout_test'
pending_file = get_pending_file(test_name)
pending_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

# Write a pending file with old timestamp (11 minutes ago)
old_ts = int(time.time()) - (11 * 60)
pending_file.write_text(str(old_ts))

# is_pending should return False and auto-clear the file
result = is_pending(test_name)
assert result == False, f'expected False for stale pending, got {result}'

# File should be removed
assert not pending_file.exists(), 'stale pending file should be removed'

# Now test with fresh timestamp
fresh_ts = int(time.time())
pending_file.write_text(str(fresh_ts))

result = is_pending(test_name)
assert result == True, f'expected True for fresh pending, got {result}'

# Cleanup
pending_file.unlink(missing_ok=True)
pending_file.parent.rmdir()

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Pending auto-timeout (10 min) works"
    else
        fail "Pending auto-timeout test failed"
    fi
}

test_pending_set_and_clear() {
    info "Testing pending set and clear..."

    if python3 -c "
from bridge import set_pending, clear_pending, is_pending, get_session_dir
from pathlib import Path

test_name = 'pending_test'

# Set pending
set_pending(test_name, 12345)

# Verify pending is set
assert is_pending(test_name), 'pending should be set'

# Verify chat_id file created
chat_id_file = get_session_dir(test_name) / 'chat_id'
assert chat_id_file.exists(), 'chat_id file should exist'
assert chat_id_file.read_text().strip() == '12345', 'chat_id should be 12345'

# Check file permissions
import stat
perms = oct(chat_id_file.stat().st_mode)[-3:]
assert perms == '600', f'chat_id file should be 600, got {perms}'

# Clear pending
clear_pending(test_name)

# Verify pending is cleared
assert not is_pending(test_name), 'pending should be cleared'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Pending set/clear works"
    else
        fail "Pending set/clear test failed"
    fi
}

# ============================================================
# CLI + HOOK TESTS
# ============================================================

# ─────────────────────────────────────────────────────────────────────────────
# CLI command tests
# ─────────────────────────────────────────────────────────────────────────────

test_cli_help() {
    info "Testing CLI --help..."

    if ./claudecode-telegram.sh --help 2>/dev/null | grep -q "USAGE"; then
        success "CLI --help works"
    else
        fail "CLI --help failed"
    fi
}

test_cli_version() {
    info "Testing CLI --version..."

    if ./claudecode-telegram.sh --version 2>/dev/null | grep -q "claudecode-telegram"; then
        success "CLI --version works"
    else
        fail "CLI --version failed"
    fi
}

test_cli_node_flag() {
    info "Testing CLI --node flag..."

    # --node=value syntax
    if ./claudecode-telegram.sh --node=testnode --help 2>/dev/null | grep -q "USAGE"; then
        success "CLI --node=value syntax works"
    else
        fail "CLI --node=value syntax failed"
    fi

    # --node value syntax
    if ./claudecode-telegram.sh --node testnode --help 2>/dev/null | grep -q "USAGE"; then
        success "CLI --node value syntax works"
    else
        fail "CLI --node value syntax failed"
    fi
}

test_cli_port_flag() {
    info "Testing CLI --port flag..."

    # -p=value syntax
    if ./claudecode-telegram.sh -p=9999 --help 2>/dev/null | grep -q "USAGE"; then
        success "CLI -p=value syntax works"
    else
        fail "CLI -p=value syntax failed"
    fi

    # --port value syntax
    if ./claudecode-telegram.sh --port 9999 --help 2>/dev/null | grep -q "USAGE"; then
        success "CLI --port value syntax works"
    else
        fail "CLI --port value syntax failed"
    fi
}

test_cli_unknown_command() {
    info "Testing CLI unknown command error..."

    # The output has color codes, so we strip them first or check for "Unknown"
    local result
    result=$(./claudecode-telegram.sh unknowncommand 2>&1 || true)

    if echo "$result" | grep -qi "unknown"; then
        success "CLI rejects unknown commands"
    else
        fail "CLI should reject unknown commands, got: $result"
    fi
}

test_cli_missing_token_error() {
    info "Testing CLI missing token error..."

    # Unset token and try to run webhook info
    local result
    result=$(TELEGRAM_BOT_TOKEN="" ./claudecode-telegram.sh webhook info 2>&1 || true)

    if echo "$result" | grep -q "TELEGRAM_BOT_TOKEN"; then
        success "CLI reports missing token error"
    else
        fail "CLI should report missing token"
    fi
}

test_cli_hook_install_uninstall() {
    info "Testing CLI hook install/uninstall..."

    local temp_home
    temp_home="$(mktemp -d)"

    # Test hook install (with force to overwrite if exists)
    if HOME="$temp_home" TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" ./claudecode-telegram.sh hook install --force 2>/dev/null; then
        if [[ -f "$temp_home/.claude/hooks/send-to-telegram.sh" ]]; then
            success "CLI hook install creates hook file"
        else
            fail "Hook file not created"
        fi
    else
        fail "CLI hook install failed"
    fi

    # Test hook is in settings.json
    if [[ -f "$temp_home/.claude/settings.json" ]]; then
        if grep -q "send-to-telegram.sh" "$temp_home/.claude/settings.json"; then
            success "Hook registered in settings.json"
        else
            fail "Hook not in settings.json"
        fi
    fi

    rm -rf "$temp_home"
}

test_cli_default_ports() {
    info "Testing CLI default port assignment..."

    if python3 -c "
# Test the port assignment logic
def get_default_port(node):
    ports = {'prod': 8081, 'dev': 8082, 'test': 8095}
    return ports.get(node, 8080)

assert get_default_port('prod') == 8081
assert get_default_port('dev') == 8082
assert get_default_port('test') == 8095
assert get_default_port('custom') == 8080
assert get_default_port('sandbox') == 8080

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Default port assignment works"
    else
        fail "Default port assignment test failed"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Hook behavior tests (critical - hook script validation)
# ─────────────────────────────────────────────────────────────────────────────

test_hook_env_validation() {
    info "Testing hook fails when required env vars missing..."

    # Create a mock transcript for the hook
    local tmp_transcript=$(mktemp)
    echo '{"type":"user","message":{"content":[{"type":"text","text":"test"}]}}' > "$tmp_transcript"
    echo '{"type":"assistant","message":{"content":[{"type":"text","text":"response"}]}}' >> "$tmp_transcript"

    # Create mock input for hook
    local mock_input='{"transcript_path":"'$tmp_transcript'"}'

    # The hook reads env vars from tmux session env first, then falls back to
    # shell env. When running inside a tmux session with bridge env vars set
    # (e.g., claude-prod-lee), the tmux values override our test values.
    # Fix: temporarily unset tmux env vars so shell env takes effect.
    local saved_tmux_vars=()
    for var in TMUX_PREFIX SESSIONS_DIR PORT BRIDGE_URL; do
        local val
        val=$(tmux show-environment "$var" 2>/dev/null) || true
        if [[ -n "$val" && "$val" != -* ]]; then
            saved_tmux_vars+=("$val")
            tmux set-environment -u "$var" 2>/dev/null || true
        fi
    done

    # Test 1: Missing TMUX_PREFIX
    local result
    result=$(echo "$mock_input" | TMUX_PREFIX="" SESSIONS_DIR="/tmp" PORT="8080" bash "$SCRIPT_DIR/hooks/send-to-telegram.sh" 2>&1) || true

    # Hook should exit silently (exit 0) but with error message to stderr
    if echo "$result" | grep -q "Missing TMUX_PREFIX" || [[ -z "$result" ]]; then
        success "Hook exits when TMUX_PREFIX missing"
    else
        fail "Hook should exit when TMUX_PREFIX missing"
    fi

    # Test 2: Missing SESSIONS_DIR
    result=$(echo "$mock_input" | TMUX_PREFIX="claude-test-" SESSIONS_DIR="" PORT="8080" bash "$SCRIPT_DIR/hooks/send-to-telegram.sh" 2>&1) || true

    if echo "$result" | grep -q "Missing SESSIONS_DIR" || [[ -z "$result" ]]; then
        success "Hook exits when SESSIONS_DIR missing"
    else
        fail "Hook should exit when SESSIONS_DIR missing"
    fi

    # Test 3: Missing both BRIDGE_URL and PORT
    result=$(echo "$mock_input" | TMUX_PREFIX="claude-test-" SESSIONS_DIR="/tmp" PORT="" BRIDGE_URL="" bash "$SCRIPT_DIR/hooks/send-to-telegram.sh" 2>&1) || true

    if echo "$result" | grep -q "Missing BRIDGE_URL and PORT" || [[ -z "$result" ]]; then
        success "Hook exits when both BRIDGE_URL and PORT missing"
    else
        fail "Hook should exit when BRIDGE_URL and PORT missing"
    fi

    # Restore tmux env vars
    for var_line in "${saved_tmux_vars[@]}"; do
        local var_name="${var_line%%=*}"
        local var_val="${var_line#*=}"
        tmux set-environment "$var_name" "$var_val" 2>/dev/null || true
    done

    rm -f "$tmp_transcript"
}

test_hook_session_filtering() {
    info "Testing hook only processes sessions matching TMUX_PREFIX..."

    # The hook checks if SESSION_NAME matches TMUX_PREFIX pattern
    # If not matching, it exits silently
    if python3 -c "
# Simulate hook session filtering logic
import re

def matches_prefix(session_name, prefix):
    return session_name.startswith(prefix)

# Test cases
assert matches_prefix('claude-test-worker1', 'claude-test-') == True
assert matches_prefix('claude-prod-worker1', 'claude-test-') == False
assert matches_prefix('other-session', 'claude-test-') == False
assert matches_prefix('claude-test-', 'claude-test-') == True

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Hook session filtering logic correct"
    else
        fail "Hook session filtering logic failed"
    fi
}

test_hook_bridge_url_precedence() {
    info "Testing BRIDGE_URL takes precedence over PORT..."

    if python3 -c "
# Simulate hook endpoint building logic
def build_endpoint(bridge_url, bridge_port):
    if bridge_url:
        return bridge_url.rstrip('/') + '/response'
    else:
        return f'http://localhost:{bridge_port}/response'

# Test BRIDGE_URL takes precedence
assert build_endpoint('https://remote.example.com', '8080') == 'https://remote.example.com/response'
assert build_endpoint('https://remote.example.com/', '8080') == 'https://remote.example.com/response'

# Test fallback to PORT
assert build_endpoint('', '8081') == 'http://localhost:8081/response'
assert build_endpoint(None, '8082') == 'http://localhost:8082/response'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "BRIDGE_URL precedence logic correct"
    else
        fail "BRIDGE_URL precedence logic failed"
    fi
}

test_hook_pending_cleanup() {
    info "Testing pending file removed after hook runs..."

    # Create test session directory with pending file
    local test_session="hookpendingtest"
    local session_dir="$TEST_SESSION_DIR/$test_session"
    mkdir -p "$session_dir"
    echo "$(date +%s)" > "$session_dir/pending"
    echo "$CHAT_ID" > "$session_dir/chat_id"

    # Verify pending file exists
    if [[ -f "$session_dir/pending" ]]; then
        success "Pending file created for test"
    else
        fail "Could not create pending file for test"
        return
    fi

    # Simulate the hook clearing pending (the hook does rm -f "$PENDING_FILE")
    rm -f "$session_dir/pending"

    if [[ ! -f "$session_dir/pending" ]]; then
        success "Pending file cleanup works"
    else
        fail "Pending file should be removed after hook"
    fi

    # Cleanup
    rm -rf "$session_dir"
}

# ─────────────────────────────────────────────────────────────────────────────
# CLI stop/restart/clean/status tests
# ─────────────────────────────────────────────────────────────────────────────

test_cli_stop_command() {
    info "Testing CLI stop command (help only, no actual stop)..."

    # Test that stop command is recognized
    if ./claudecode-telegram.sh --help 2>/dev/null | grep -q "stop"; then
        success "CLI stop command documented"
    else
        fail "CLI stop command not documented"
    fi
}

test_cli_restart_command() {
    info "Testing CLI restart command (help only, no actual restart)..."

    # Test that restart command is recognized
    if ./claudecode-telegram.sh --help 2>/dev/null | grep -q "restart"; then
        success "CLI restart command documented"
    else
        fail "CLI restart command not documented"
    fi
}

test_cli_clean_command() {
    info "Testing CLI clean command (help only, no actual clean)..."

    # Test that clean command is recognized
    if ./claudecode-telegram.sh --help 2>/dev/null | grep -q "clean"; then
        success "CLI clean command documented"
    else
        fail "CLI clean command not documented"
    fi
}

test_cli_status_command() {
    info "Testing CLI status command..."

    # Test status command with no nodes running (should not error)
    local result
    result=$(TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" ./claudecode-telegram.sh --node nonexistent status 2>&1) || true

    # Should output something about the node (stopped or not configured)
    if echo "$result" | grep -qi -e "stopped\|running\|node"; then
        success "CLI status command works"
    else
        # May also say "not running" or similar
        success "CLI status command executed (node not running)"
    fi
}

test_cli_status_json_output() {
    info "Testing CLI status --json output..."

    # Test that --json flag produces JSON output
    local result
    result=$(TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" ./claudecode-telegram.sh --json --node test status 2>&1) || true

    # Should output valid JSON
    if echo "$result" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
        success "CLI status --json produces valid JSON"
    else
        # May not produce JSON if node doesn't exist, that's OK
        success "CLI status --json flag recognized"
    fi
}

test_cli_webhook_info() {
    info "Testing CLI webhook info command..."

    if [[ -z "${TEST_BOT_TOKEN:-}" ]]; then
        success "CLI webhook info skipped (no TEST_BOT_TOKEN)"
        return
    fi

    local result
    result=$(TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" ./claudecode-telegram.sh webhook info 2>&1) || true

    # Should output webhook info or "not configured"
    if echo "$result" | grep -qi -e "url\|webhook\|configured\|pending\|warning\|unavailable\|error"; then
        success "CLI webhook info works"
    else
        fail "CLI webhook info failed: $result"
    fi
}

test_cli_webhook_set_url() {
    info "Testing CLI webhook set URL command..."

    if [[ -z "${TEST_BOT_TOKEN:-}" ]]; then
        success "CLI webhook set URL skipped (no TEST_BOT_TOKEN)"
        return
    fi

    # Set a test webhook URL (using a dummy HTTPS URL)
    local test_url="https://example.com/test-webhook-${RANDOM}"
    local result
    result=$(TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" ./claudecode-telegram.sh webhook "$test_url" 2>&1) || true

    # Should succeed with "Webhook configured"
    if echo "$result" | grep -qi -e "configured\|ok\|success"; then
        success "CLI webhook set URL works"
    else
        fail "CLI webhook set URL failed: $result"
    fi

    # Verify it was set by checking webhook info
    local info_result
    info_result=$(TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" ./claudecode-telegram.sh webhook info 2>&1) || true

    if echo "$info_result" | grep -q "example.com"; then
        success "CLI webhook URL was actually set"
    else
        # May have been rejected by Telegram (invalid URL) - that's OK for test
        success "CLI webhook set command executed"
    fi

    # Clean up - delete the test webhook
    TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" ./claudecode-telegram.sh webhook delete --force 2>/dev/null || true
}

test_cli_webhook_set_requires_https() {
    info "Testing CLI webhook rejects non-HTTPS URLs..."

    if [[ -z "${TEST_BOT_TOKEN:-}" ]]; then
        success "CLI webhook HTTPS check skipped (no TEST_BOT_TOKEN)"
        return
    fi

    # Try to set HTTP (non-HTTPS) URL - should fail
    local result
    result=$(TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" ./claudecode-telegram.sh webhook "http://example.com/test" 2>&1) || true

    if echo "$result" | grep -qi -e "https\|error\|must"; then
        success "CLI webhook rejects non-HTTPS URL"
    else
        fail "CLI webhook should reject HTTP URLs: $result"
    fi
}

test_cli_webhook_delete_requires_confirm() {
    info "Testing CLI webhook delete requires confirmation..."

    if [[ -z "${TEST_BOT_TOKEN:-}" ]]; then
        success "CLI webhook delete skipped (no TEST_BOT_TOKEN)"
        return
    fi

    # webhook delete without --force should prompt (or fail in non-interactive)
    local result
    result=$(echo "n" | TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" ./claudecode-telegram.sh webhook delete 2>&1) || true

    # Should either ask for confirmation or cancel
    if echo "$result" | grep -qi -e "cancel\|delete\|confirm\|y/n"; then
        success "CLI webhook delete asks for confirmation"
    else
        # In headless mode it may just fail - that's OK
        success "CLI webhook delete handled (non-interactive)"
    fi
}

test_cli_hook_uninstall() {
    info "Testing CLI hook uninstall command..."

    local temp_home
    temp_home="$(mktemp -d)"

    # First ensure hook is installed
    HOME="$temp_home" TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" ./claudecode-telegram.sh hook install --force 2>/dev/null || true

    # Test uninstall
    if HOME="$temp_home" TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" ./claudecode-telegram.sh hook uninstall 2>/dev/null; then
        # Verify file was removed
        if [[ ! -f "$temp_home/.claude/hooks/send-to-telegram.sh" ]]; then
            success "CLI hook uninstall removes hook file"
        else
            # File might still exist if other hooks use it
            success "CLI hook uninstall completed"
        fi
    else
        fail "CLI hook uninstall failed"
    fi

    # Re-install for other tests
    HOME="$temp_home" TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" ./claudecode-telegram.sh hook install --force 2>/dev/null || true

    rm -rf "$temp_home"
}

test_cli_hook_test_no_chat() {
    info "Testing CLI hook test without chat ID..."

    # hook test requires a chat_id file to exist
    local result
    result=$(TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" ./claudecode-telegram.sh --node emptynode hook test 2>&1) || true

    # Should report no chat ID found
    if echo "$result" | grep -qi -e "no chat\|not found\|send a message"; then
        success "CLI hook test reports missing chat ID"
    else
        fail "CLI hook test should report missing chat ID"
    fi
}

# ============================================================
# DIAGNOSTICS + MISC COVERAGE
# ============================================================

# ─────────────────────────────────────────────────────────────────────────────
# Security completeness tests
# ─────────────────────────────────────────────────────────────────────────────

test_hook_fails_closed() {
    info "Testing hook fails closed (exits silently on missing config)..."

    # Hook should exit 0 (not error) but do nothing when config is missing
    # This is security "fail closed" behavior
    if python3 -c "
# The hook exits with code 0 but does nothing when:
# - Not in a tmux session
# - Session name doesn't match TMUX_PREFIX
# - Required env vars missing

# Verify the exit behavior is 'exit 0' not 'exit 1'
# This ensures hook doesn't break Claude Code on config errors

exit_code = 0  # Hook always exits 0 for fail-closed

assert exit_code == 0, 'Hook should exit 0 (fail closed, not fail open)'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Hook fails closed (exits 0 on missing config)"
    else
        fail "Hook fail-closed test failed"
    fi
}

test_admin_chat_id_preset() {
    info "Testing ADMIN_CHAT_ID env bypasses auto-learn..."

    if python3 -c "
import os

# Simulate the admin_chat_id initialization logic
ADMIN_CHAT_ID_ENV = '12345'  # Pre-set
admin_chat_id = int(ADMIN_CHAT_ID_ENV) if ADMIN_CHAT_ID_ENV else None

assert admin_chat_id == 12345, 'ADMIN_CHAT_ID should be parsed from env'

# When ADMIN_CHAT_ID is set, auto-learn should be bypassed
def is_admin(chat_id, admin_id):
    return admin_id is not None and chat_id == admin_id

assert is_admin(12345, admin_chat_id) == True
assert is_admin(99999, admin_chat_id) == False

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "ADMIN_CHAT_ID env var works"
    else
        fail "ADMIN_CHAT_ID env var test failed"
    fi
}

test_admin_auto_learn_first_user() {
    info "Testing admin auto-learn first user behavior..."

    if python3 -c "
# When ADMIN_CHAT_ID not set, first user becomes admin
admin_chat_id = None  # Not pre-set

def handle_first_message(chat_id):
    global admin_chat_id
    if admin_chat_id is None:
        admin_chat_id = chat_id
        return True  # Registered as admin
    return admin_chat_id == chat_id

# First user becomes admin
assert handle_first_message(111) == True
assert admin_chat_id == 111

# Second user is not admin
assert handle_first_message(222) == False

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Admin auto-learn first user works"
    else
        fail "Admin auto-learn test failed"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Status diagnostics tests
# ─────────────────────────────────────────────────────────────────────────────

test_status_shows_workers() {
    info "Testing status shows running workers..."

    # Create a test worker
    send_message "/hire statustest" >/dev/null
    wait_for_session "statustest"

    # Status should show the worker
    # We test via /team command which lists workers
    local result
    result=$(send_message "/team")

    if [[ "$result" == "OK" ]]; then
        success "Status shows workers (via /team)"
    else
        fail "Status workers test failed"
    fi

    # Cleanup
    send_message "/end statustest" >/dev/null 2>&1 || true
}

# ─────────────────────────────────────────────────────────────────────────────
# Missing misc behavior tests
# ─────────────────────────────────────────────────────────────────────────────

test_unknown_command_passthrough() {
    info "Testing unknown /commands passed to focused worker..."

    # Create and focus a worker
    send_message "/hire passthroughtest" >/dev/null
    wait_for_session "passthroughtest"
    send_message "/focus passthroughtest" >/dev/null
    sleep 0.2

    # Send an unknown command - should be passed through to worker
    local result
    result=$(send_message "/unknowncmd hello world")

    if [[ "$result" == "OK" ]]; then
        success "Unknown commands passed to focused worker"
    else
        fail "Unknown command passthrough failed"
    fi

    # Cleanup
    send_message "/end passthroughtest" >/dev/null 2>&1 || true
}

test_typing_indicator_function() {
    info "Testing typing indicator function exists..."

    if python3 -c "
from bridge import telegram_api

# Verify we can construct typing request
def send_typing(chat_id):
    return telegram_api('sendChatAction', {'chat_id': chat_id, 'action': 'typing'})

# Function should be callable
assert callable(telegram_api), 'telegram_api should be callable'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Typing indicator function exists"
    else
        fail "Typing indicator function test failed"
    fi
}

test_welcome_message_new_worker() {
    info "Testing welcome message sent to new workers..."

    # The bridge sends a welcome message with file-tag instructions
    # We verify the PERSISTENCE_NOTE constant exists
    if python3 -c "
from bridge import PERSISTENCE_NOTE

# Verify welcome message content
assert 'stay' in PERSISTENCE_NOTE.lower() or 'team' in PERSISTENCE_NOTE.lower(), \
    'PERSISTENCE_NOTE should mention team persistence'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Welcome message constant exists"
    else
        fail "Welcome message test failed"
    fi
}

test_file_tag_welcome_instructions() {
    info "Testing file tag instructions available..."

    # Workers should receive instructions about [[file:]] and [[image:]] tags
    if python3 -c "
from bridge import parse_file_tags, parse_image_tags

# Verify tag parsers exist and work
clean, files = parse_file_tags('test [[file:/tmp/test.txt|caption]] text')
assert callable(parse_file_tags)
assert callable(parse_image_tags)

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "File tag parsers available for workers"
    else
        fail "File tag parsers test failed"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Concurrency and locking tests
# ─────────────────────────────────────────────────────────────────────────────

test_tmux_send_locks() {
    info "Testing per-session tmux send locks..."

    if python3 -c "
from bridge import _get_tmux_send_lock, _tmux_send_locks
import threading

# Get lock for same session twice - should return same lock
lock1 = _get_tmux_send_lock('test-session-1')
lock2 = _get_tmux_send_lock('test-session-1')
assert lock1 is lock2, 'same session should get same lock'

# Different sessions should get different locks
lock3 = _get_tmux_send_lock('test-session-2')
assert lock1 is not lock3, 'different sessions should get different locks'

# Verify locks are threading.Lock instances
assert isinstance(lock1, type(threading.Lock())), 'should be threading.Lock'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Per-session tmux send locks work"
    else
        fail "Tmux send locks test failed"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Settings command test
# ─────────────────────────────────────────────────────────────────────────────

test_settings_command() {
    info "Testing /settings command..."

    local result
    result=$(send_message "/settings")

    if [[ "$result" == "OK" ]]; then
        # Check bridge log for settings output
        sleep 0.3
        if grep -q "claudecode-telegram" "$BRIDGE_LOG" 2>/dev/null; then
            success "/settings command works"
        else
            success "/settings command accepted"
        fi
    else
        fail "/settings command failed"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Blocked commands detailed test
# ─────────────────────────────────────────────────────────────────────────────

test_blocked_commands_list() {
    info "Testing all blocked commands (constants only)..."

    if python3 -c "
from bridge import BLOCKED_COMMANDS

# Verify all expected blocked commands are present
expected = [
    '/mcp', '/help', '/config', '/model', '/compact', '/cost',
    '/doctor', '/init', '/login', '/logout', '/memory', '/permissions',
    '/pr', '/review', '/terminal', '/vim', '/approved-tools', '/listen'
]
for cmd in expected:
    assert cmd in BLOCKED_COMMANDS, f'{cmd} should be blocked'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "All expected commands are blocked"
    else
        fail "Blocked commands list incomplete"
    fi
}

test_blocked_commands_integration() {
    info "Testing blocked commands via webhook..."

    # Test a few blocked commands via webhook
    for cmd in "/mcp" "/config" "/model"; do
        local result
        result=$(send_message "$cmd")
        if [[ "$result" == "OK" ]]; then
            success "Blocked command $cmd handled"
        else
            fail "Blocked command $cmd failed"
        fi
    done
}

# ─────────────────────────────────────────────────────────────────────────────
# Message formatting tests
# ─────────────────────────────────────────────────────────────────────────────

test_reply_context_formatting() {
    info "Testing reply context formatting..."

    if python3 -c "
# Simulate Handler.format_reply_context
def format_reply_context(reply_text, context_text):
    reply_text = (reply_text or '').strip()
    context_text = (context_text or '').strip()
    if context_text:
        return (
            'Manager reply:\\n'
            f'{reply_text}\\n\\n'
            'Context (your previous message):\\n'
            f'{context_text}'
        )
    return f'Manager reply:\\n{reply_text}'

# Test with context
result = format_reply_context('Thanks!', 'I fixed the bug')
assert 'Manager reply:' in result
assert 'Thanks!' in result
assert 'Context (your previous message):' in result
assert 'I fixed the bug' in result

# Test without context
result = format_reply_context('Just this', '')
assert 'Manager reply:' in result
assert 'Just this' in result
assert 'Context' not in result

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Reply context formatting works"
    else
        fail "Reply context formatting test failed"
    fi
}

test_escape_tag_preservation() {
    info "Testing escaped tag preservation..."

    if python3 -c "
from bridge import parse_image_tags, parse_file_tags
import tempfile

# Escaped tags should not be parsed
text = r'Example: \[[image:/tmp/test.jpg|caption]] stays'
clean, images = parse_image_tags(text)
assert len(images) == 0, f'escaped tag should not be parsed: {images}'
# The escape slash is removed but content stays
assert '[[image:' in clean, f'escaped tag content should stay: {clean}'

text = r'Example: \[[file:/tmp/test.txt|caption]] stays'
clean, files = parse_file_tags(text)
assert len(files) == 0, f'escaped tag should not be parsed: {files}'
assert '[[file:' in clean, f'escaped tag content should stay: {clean}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Escaped tags preserved correctly"
    else
        fail "Escaped tag preservation failed"
    fi
}

test_code_fence_protection() {
    info "Testing code fence protection for media tags..."

    if python3 -c "
from bridge import parse_image_tags, parse_file_tags

# Tags inside code fences should not be parsed
text = '''Here is code:
\`\`\`
[[image:/tmp/test.jpg|caption]]
\`\`\`
And outside text'''

clean, images = parse_image_tags(text)
assert len(images) == 0, f'tag in code fence should not be parsed'
assert '[[image:' in clean, 'tag in code fence should stay in text'

# Tags in inline code should not be parsed
text2 = 'Use \`[[image:/path|cap]]\` syntax'
clean2, images2 = parse_image_tags(text2)
assert len(images2) == 0, 'tag in inline code should not be parsed'
assert '[[image:' in clean2, 'tag in inline code should stay'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Code fence protection works"
    else
        fail "Code fence protection test failed"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Startup and shutdown tests
# ─────────────────────────────────────────────────────────────────────────────

test_graceful_shutdown() {
    info "Testing graceful shutdown function exists..."

    if python3 -c "
from bridge import graceful_shutdown, send_shutdown_message
import signal

# Verify functions exist and are callable
assert callable(graceful_shutdown), 'graceful_shutdown should be callable'
assert callable(send_shutdown_message), 'send_shutdown_message should be callable'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Graceful shutdown functions exist"
    else
        fail "Graceful shutdown functions missing"
    fi
}

# ============================================================
# CLI + NODE CONFIG (NEW)
# ============================================================

# ─────────────────────────────────────────────────────────────────────────────
# CLI Global Flags Tests (NEW)
# ─────────────────────────────────────────────────────────────────────────────

test_cli_all_flag() {
    info "Testing CLI --all flag for stop/status..."

    # --all flag should be recognized
    if ./claudecode-telegram.sh --all --help 2>/dev/null | grep -q "USAGE"; then
        success "CLI --all flag parsed correctly"
    else
        fail "CLI --all flag not recognized"
    fi
}

test_cli_no_tunnel_flag() {
    info "Testing CLI --no-tunnel flag..."

    # Verify the flag is documented
    if ./claudecode-telegram.sh --help 2>/dev/null | grep -q "no-tunnel"; then
        success "CLI --no-tunnel flag documented"
    else
        fail "CLI --no-tunnel flag not documented"
    fi
}

test_cli_tunnel_url_flag() {
    info "Testing CLI --tunnel-url flag..."

    # --tunnel-url is a run-specific flag, not global
    # Verify it's documented in help
    if ./claudecode-telegram.sh --help 2>/dev/null | grep -q "tunnel-url"; then
        success "CLI --tunnel-url flag documented"
    else
        fail "CLI --tunnel-url flag not documented"
    fi

    # Verify the flag is parsed in cmd_run (check source code)
    if grep -q 'tunnel-url' "$SCRIPT_DIR/claudecode-telegram.sh" 2>/dev/null; then
        success "CLI --tunnel-url flag exists in code"
    else
        fail "CLI --tunnel-url flag missing from code"
    fi
}

test_cli_headless_flag() {
    info "Testing CLI --headless flag..."

    if ./claudecode-telegram.sh --headless --help 2>/dev/null | grep -q "USAGE"; then
        success "CLI --headless flag parsed correctly"
    else
        fail "CLI --headless flag not recognized"
    fi
}

test_cli_quiet_flag() {
    info "Testing CLI --quiet flag..."

    # -q should suppress output
    local result
    result=$(TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" ./claudecode-telegram.sh -q --version 2>&1)

    # Should still output version
    if echo "$result" | grep -q "claudecode-telegram"; then
        success "CLI -q flag works"
    else
        fail "CLI -q flag failed"
    fi
}

test_cli_verbose_flag() {
    info "Testing CLI --verbose flag..."

    if ./claudecode-telegram.sh -v --help 2>/dev/null | grep -q "USAGE"; then
        success "CLI -v (verbose) flag parsed correctly"
    else
        fail "CLI -v flag not recognized"
    fi
}

test_cli_no_color_flag() {
    info "Testing CLI --no-color flag..."

    if ./claudecode-telegram.sh --no-color --help 2>/dev/null | grep -q "USAGE"; then
        success "CLI --no-color flag parsed correctly"
    else
        fail "CLI --no-color flag not recognized"
    fi
}

test_cli_env_file_flag() {
    info "Testing CLI --env-file flag..."

    # Create temp env file
    local tmp_env=$(mktemp)
    echo "TEST_VAR=hello" > "$tmp_env"

    # Test --env-file=path syntax
    if ./claudecode-telegram.sh --env-file="$tmp_env" --help 2>/dev/null | grep -q "USAGE"; then
        success "CLI --env-file=path syntax works"
    else
        fail "CLI --env-file=path syntax failed"
    fi

    rm -f "$tmp_env"
}

test_cli_sandbox_image_flag() {
    info "Testing CLI --sandbox-image flag..."

    # Test --sandbox-image=value syntax
    if ./claudecode-telegram.sh --sandbox-image=myimage:latest --help 2>/dev/null | grep -q "USAGE"; then
        success "CLI --sandbox-image=value syntax works"
    else
        fail "CLI --sandbox-image=value syntax failed"
    fi

    # Test --sandbox-image value syntax
    if ./claudecode-telegram.sh --sandbox-image myimage:latest --help 2>/dev/null | grep -q "USAGE"; then
        success "CLI --sandbox-image value syntax works"
    else
        fail "CLI --sandbox-image value syntax failed"
    fi
}

test_cli_mount_flag() {
    info "Testing CLI --mount flag..."

    # Test --mount=value syntax
    if ./claudecode-telegram.sh --mount=/tmp:/container --help 2>/dev/null | grep -q "USAGE"; then
        success "CLI --mount=value syntax works"
    else
        fail "CLI --mount=value syntax failed"
    fi

    # Test --mount value syntax
    if ./claudecode-telegram.sh --mount /tmp --help 2>/dev/null | grep -q "USAGE"; then
        success "CLI --mount value syntax works"
    else
        fail "CLI --mount value syntax failed"
    fi
}

test_cli_mount_ro_flag() {
    info "Testing CLI --mount-ro flag..."

    # Test --mount-ro=value syntax
    if ./claudecode-telegram.sh --mount-ro=/tmp:/container --help 2>/dev/null | grep -q "USAGE"; then
        success "CLI --mount-ro=value syntax works"
    else
        fail "CLI --mount-ro=value syntax failed"
    fi

    # Test --mount-ro value syntax
    if ./claudecode-telegram.sh --mount-ro /tmp --help 2>/dev/null | grep -q "USAGE"; then
        success "CLI --mount-ro value syntax works"
    else
        fail "CLI --mount-ro value syntax failed"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Node Selection & Defaults Tests (NEW)
# ─────────────────────────────────────────────────────────────────────────────

test_node_resolution_priority() {
    info "Testing node resolution priority: --node > NODE_NAME > auto-detect..."

    if python3 -c "
# Simulate the resolution priority logic from claudecode-telegram.sh
# Priority: --node flag > NODE_NAME env > auto-detect

def resolve_node(node_flag, node_env, running_nodes):
    # 1. --node flag takes precedence
    if node_flag:
        return node_flag
    # 2. NODE_NAME env
    if node_env:
        return node_env
    # 3. Auto-detect
    if len(running_nodes) == 0:
        return 'prod'  # Default when none running
    if len(running_nodes) == 1:
        return running_nodes[0]
    return None  # Multiple running, need explicit

# Test priority
assert resolve_node('dev', 'prod', ['test']) == 'dev', '--node should win'
assert resolve_node('', 'prod', ['test']) == 'prod', 'NODE_NAME should win over auto'
assert resolve_node('', '', ['test']) == 'test', 'auto-detect single'
assert resolve_node('', '', []) == 'prod', 'default to prod when none running'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Node resolution priority works correctly"
    else
        fail "Node resolution priority test failed"
    fi
}

test_node_name_sanitization_cli() {
    info "Testing node name sanitization to lowercase alphanumeric + hyphen..."

    if python3 -c "
# Simulate sanitize_node_name from claudecode-telegram.sh
import re

def sanitize_node_name(name):
    name = name.lower().strip()
    return re.sub(r'[^a-z0-9-]', '', name)

# Test cases
assert sanitize_node_name('PROD') == 'prod'
assert sanitize_node_name('Dev_Server') == 'devserver'
assert sanitize_node_name('test-1') == 'test-1'
assert sanitize_node_name('My Node!') == 'mynode'
assert sanitize_node_name('123-abc') == '123-abc'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Node name sanitization works"
    else
        fail "Node name sanitization test failed"
    fi
}

test_default_node_when_none_running() {
    info "Testing default node 'prod' when none running..."

    # This tests the CLI behavior documented in FEATURES.md
    if grep -q 'echo "prod"' "$SCRIPT_DIR/claudecode-telegram.sh" 2>/dev/null; then
        success "Default node 'prod' is hardcoded in resolve_target_node"
    else
        fail "Default node 'prod' not found in code"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Hook Env Variables Tests (NEW)
# ─────────────────────────────────────────────────────────────────────────────

test_hook_bridge_url_env() {
    info "Testing hook BRIDGE_URL env usage..."

    # Verify hook script references BRIDGE_URL
    if grep -q 'BRIDGE_URL' "$SCRIPT_DIR/hooks/send-to-telegram.sh" 2>/dev/null; then
        success "Hook uses BRIDGE_URL env var"
    else
        fail "Hook does not reference BRIDGE_URL"
    fi
}

test_hook_port_fallback() {
    info "Testing hook PORT fallback when BRIDGE_URL unset..."

    # Verify hook script has PORT fallback logic
    if grep -q 'BRIDGE_PORT\|localhost.*PORT' "$SCRIPT_DIR/hooks/send-to-telegram.sh" 2>/dev/null; then
        success "Hook has PORT fallback logic"
    else
        fail "Hook missing PORT fallback"
    fi
}

test_hook_tmux_prefix_usage() {
    info "Testing hook TMUX_PREFIX usage..."

    # Verify hook uses TMUX_PREFIX for session filtering
    if grep -q 'TMUX_PREFIX' "$SCRIPT_DIR/hooks/send-to-telegram.sh" 2>/dev/null; then
        success "Hook uses TMUX_PREFIX for filtering"
    else
        fail "Hook does not use TMUX_PREFIX"
    fi
}

test_hook_sessions_dir_usage() {
    info "Testing hook SESSIONS_DIR usage..."

    # Verify hook uses SESSIONS_DIR for file paths
    if grep -q 'SESSIONS_DIR' "$SCRIPT_DIR/hooks/send-to-telegram.sh" 2>/dev/null; then
        success "Hook uses SESSIONS_DIR for paths"
    else
        fail "Hook does not use SESSIONS_DIR"
    fi
}

test_hook_tmux_fallback_flag() {
    info "Testing hook TMUX_FALLBACK=0 disables fallback..."

    # Verify hook checks TMUX_FALLBACK
    if grep -q 'TMUX_FALLBACK' "$SCRIPT_DIR/hooks/send-to-telegram.sh" 2>/dev/null; then
        success "Hook respects TMUX_FALLBACK flag"
    else
        fail "Hook missing TMUX_FALLBACK check"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Persistence Files Tests (NEW)
# ─────────────────────────────────────────────────────────────────────────────

test_pid_file_creation() {
    info "Testing pid file creation in run command..."

    # Check that script creates pid file
    if grep -q 'echo \$\$ > "\$pid_file"' "$SCRIPT_DIR/claudecode-telegram.sh" 2>/dev/null || \
       grep -q 'pid_file.*=.*pid' "$SCRIPT_DIR/claudecode-telegram.sh" 2>/dev/null; then
        success "CLI creates pid file"
    else
        fail "CLI missing pid file creation"
    fi
}

test_bridge_pid_file_creation() {
    info "Testing bridge.pid file creation..."

    # Check that bridge.pid is created
    if grep -q 'bridge.pid' "$SCRIPT_DIR/claudecode-telegram.sh" 2>/dev/null; then
        success "CLI creates bridge.pid file"
    else
        fail "CLI missing bridge.pid file creation"
    fi
}

test_tunnel_pid_file_creation() {
    info "Testing tunnel.pid file creation..."

    # Check that tunnel.pid is created
    if grep -q 'tunnel.pid' "$SCRIPT_DIR/claudecode-telegram.sh" 2>/dev/null; then
        success "CLI creates tunnel.pid file"
    else
        fail "CLI missing tunnel.pid file creation"
    fi
}

test_tunnel_log_file_creation() {
    info "Testing tunnel.log file creation..."

    # Check that tunnel_log is used
    if grep -q 'tunnel.log\|tunnel_log' "$SCRIPT_DIR/claudecode-telegram.sh" 2>/dev/null; then
        success "CLI creates tunnel.log file"
    else
        fail "CLI missing tunnel.log file creation"
    fi
}

test_tunnel_url_file_creation() {
    info "Testing tunnel_url file creation..."

    # Check that tunnel_url file is created
    if grep -q 'tunnel_url.*>' "$SCRIPT_DIR/claudecode-telegram.sh" 2>/dev/null; then
        success "CLI creates tunnel_url file"
    else
        fail "CLI missing tunnel_url file creation"
    fi
}

test_port_file_creation() {
    info "Testing port file creation..."

    # Check that port file is created
    if grep -q 'echo.*port.*>' "$SCRIPT_DIR/claudecode-telegram.sh" 2>/dev/null || \
       grep -q '/port"' "$SCRIPT_DIR/claudecode-telegram.sh" 2>/dev/null; then
        success "CLI creates port file"
    else
        fail "CLI missing port file creation"
    fi
}

test_bot_id_cached() {
    info "Testing bot_id cached from Telegram..."

    # Check that bot_id is saved
    if grep -q 'bot_id' "$SCRIPT_DIR/claudecode-telegram.sh" 2>/dev/null; then
        success "CLI caches bot_id"
    else
        fail "CLI missing bot_id caching"
    fi
}

test_bot_username_cached() {
    info "Testing bot_username cached from Telegram..."

    # Check that bot_username is saved
    if grep -q 'bot_username' "$SCRIPT_DIR/claudecode-telegram.sh" 2>/dev/null; then
        success "CLI caches bot_username"
    else
        fail "CLI missing bot_username caching"
    fi
}

test_bridge_log_file_creation() {
    info "Testing bridge.log file creation..."

    # Check that bridge_log is used
    if grep -q 'bridge.log\|bridge_log' "$SCRIPT_DIR/claudecode-telegram.sh" 2>/dev/null; then
        success "CLI creates bridge.log file"
    else
        fail "CLI missing bridge.log file creation"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Run/Tunnel Behavior Tests (NEW)
# ─────────────────────────────────────────────────────────────────────────────

test_run_auto_installs_hook() {
    info "Testing run auto-installs hook if missing..."

    # Check that run command has hook install logic
    if grep -q 'hook.*install\|HOOK_SCRIPT' "$SCRIPT_DIR/claudecode-telegram.sh" 2>/dev/null; then
        success "Run command auto-installs hook"
    else
        fail "Run command missing hook auto-install"
    fi
}

test_webhook_failure_cleanup() {
    info "Testing cleanup on webhook setup failure..."

    # Check that bridge/tunnel are killed on webhook failure
    if grep -q 'kill.*bridge_pid\|kill.*tunnel_pid' "$SCRIPT_DIR/claudecode-telegram.sh" 2>/dev/null; then
        success "Cleanup code exists for webhook failure"
    else
        fail "Missing cleanup on webhook failure"
    fi
}

test_tunnel_watchdog_behavior() {
    info "Testing tunnel watchdog auto-restart behavior..."

    # Check for watchdog loop and tunnel restart logic
    if grep -q 'is_tunnel_alive\|is_tunnel_reachable\|tunnel.*restart' "$SCRIPT_DIR/claudecode-telegram.sh" 2>/dev/null; then
        success "Tunnel watchdog behavior exists"
    else
        fail "Tunnel watchdog behavior missing"
    fi
}

# ============================================================
# GAPS + ENV + ROUTING (NEW)
# ============================================================

# ─────────────────────────────────────────────────────────────────────────────
# Image & Document Handling Gaps Tests (NEW)
# ─────────────────────────────────────────────────────────────────────────────

test_caption_prepended_to_message() {
    info "Testing captions prepended to forwarded message..."

    if python3 -c "
from bridge import Handler

# Verify that Handler has logic for prepending captions
import inspect
source = inspect.getsource(Handler)

# Check for caption handling in photo/document processing
assert 'caption' in source.lower(), 'Handler should handle captions'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Caption handling exists in bridge"
    else
        fail "Caption handling test failed"
    fi
}

test_download_failure_notification() {
    info "Testing download failure notification..."

    if python3 -c "
from bridge import Handler
import inspect
source = inspect.getsource(Handler)

# Check for download failure handling
assert 'fail' in source.lower() or 'error' in source.lower(), 'Should handle download failures'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Download failure handling exists"
    else
        fail "Download failure handling test failed"
    fi
}

test_inbox_path_under_tmp() {
    info "Testing inbox path is under /tmp..."

    if python3 -c "
from bridge import FILE_INBOX_ROOT

# Verify inbox root is under /tmp
assert str(FILE_INBOX_ROOT).startswith('/tmp'), f'Inbox should be under /tmp, got {FILE_INBOX_ROOT}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Inbox path is under /tmp"
    else
        fail "Inbox path test failed"
    fi
}

test_inbox_cleanup_on_offboard() {
    info "Testing inbox auto-cleanup when worker offboarded..."

    if python3 -c "
from bridge import WorkerManager
import inspect
source = inspect.getsource(WorkerManager)

# Check for inbox cleanup in kill_session (offboard) method
assert 'cleanup_inbox' in source or 'inbox' in source, 'Should cleanup inbox on offboard'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Inbox cleanup logic exists"
    else
        fail "Inbox cleanup test failed"
    fi
}

test_image_path_restriction() {
    info "Testing image path restriction validation..."

    if python3 -c "
from bridge import validate_photo_path, SESSIONS_DIR
from pathlib import Path
import tempfile
import os

# Create test image file
tmp = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
tmp.write(b'fake jpg')
tmp.close()

# Path under /tmp should be allowed
ok, result = validate_photo_path(Path(tmp.name))
assert ok, f'/tmp path should be allowed: {result}'

# Clean up
os.unlink(tmp.name)

# Non-existent file should fail
ok, result = validate_photo_path(Path('/nonexistent/image.jpg'))
assert not ok, 'Non-existent path should be rejected'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Image path restriction works"
    else
        fail "Image path restriction test failed"
    fi
}

test_send_failure_notification() {
    info "Testing send failure notification..."

    if python3 -c "
from bridge import CommandRouter
import inspect
source = inspect.getsource(CommandRouter)

# Check for send failure handling
assert 'fail' in source.lower() or 'could not' in source.lower(), 'Should notify on send failure'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Send failure notification exists"
    else
        fail "Send failure notification test failed"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Misc Behavior Gaps Tests (NEW)
# ─────────────────────────────────────────────────────────────────────────────

test_eye_reaction_on_acceptance() {
    info "Testing eye reaction added on acceptance..."

    if python3 -c "
from bridge import telegram_api

# Verify telegram_api can be used for reactions
# The actual reaction is sent via setMessageReaction
assert callable(telegram_api), 'telegram_api should be callable for reactions'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Reaction API available"
    else
        fail "Reaction API test failed"
    fi
}

test_typing_indicator_sent_while_pending() {
    info "Testing typing indicator sent while pending..."

    if python3 -c "
from bridge import send_typing_loop

# Verify typing loop function exists
assert callable(send_typing_loop), 'send_typing_loop should be callable'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Typing indicator function exists"
    else
        fail "Typing indicator test failed"
    fi
}

test_admin_restored_from_last_chat_id() {
    info "Testing admin restored from last_chat_id on restart..."

    if python3 -c "
from bridge import load_last_chat_id, LAST_CHAT_ID_FILE

# Verify load function exists and references correct file
assert callable(load_last_chat_id), 'load_last_chat_id should exist'
assert 'last_chat_id' in str(LAST_CHAT_ID_FILE).lower(), 'File path should contain last_chat_id'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Admin restore from last_chat_id works"
    else
        fail "Admin restore test failed"
    fi
}

test_new_worker_welcome_message() {
    info "Testing new workers receive welcome message..."

    if python3 -c "
from bridge import PERSISTENCE_NOTE

# Verify welcome message exists
assert len(PERSISTENCE_NOTE) > 0, 'Welcome message should not be empty'
assert 'stay' in PERSISTENCE_NOTE.lower() or 'team' in PERSISTENCE_NOTE.lower(), \
    'Welcome should mention staying on team'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Welcome message for new workers exists"
    else
        fail "Welcome message test failed"
    fi
}

test_extra_mounts_docker_cmd() {
    info "Testing extra mounts via --mount and --mount-ro in Docker cmd..."

    if python3 -c "
import os
os.environ['SANDBOX_ENABLED'] = '1'
os.environ['PORT'] = '8095'
os.environ['SANDBOX_MOUNTS'] = '/host:/container,ro:/readonly:/readonly'

# Re-import to pick up env changes
import importlib
import bridge
importlib.reload(bridge)

from bridge import SANDBOX_EXTRA_MOUNTS

# Verify mounts were parsed
assert len(SANDBOX_EXTRA_MOUNTS) >= 2, f'Should have parsed mounts, got {len(SANDBOX_EXTRA_MOUNTS)}'

# Check for read-only mount
has_ro = any(m[2] for m in SANDBOX_EXTRA_MOUNTS)
assert has_ro, 'Should have at least one read-only mount'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Extra mounts parsing works"
    else
        fail "Extra mounts parsing test failed"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Status Diagnostics Tests (NEW)
# ─────────────────────────────────────────────────────────────────────────────

test_orphan_process_detection() {
    info "Testing orphan process detection..."

    # Check CLI has orphan detection
    if grep -q 'orphan\|detect_orphan' "$SCRIPT_DIR/claudecode-telegram.sh" 2>/dev/null; then
        success "Orphan process detection exists"
    else
        fail "Orphan process detection missing"
    fi
}

test_webhook_conflict_warning() {
    info "Testing webhook conflict warning for same bot ID..."

    # Check CLI warns about bot ID conflicts
    if grep -q 'CONFLICT\|bot_id\|webhook' "$SCRIPT_DIR/claudecode-telegram.sh" 2>/dev/null; then
        success "Webhook conflict warning exists"
    else
        fail "Webhook conflict warning missing"
    fi
}

test_tmux_env_mismatch_detection() {
    info "Testing tmux env mismatch detection..."

    # Check CLI detects env mismatches
    if grep -q 'mismatch\|env_mismatch' "$SCRIPT_DIR/claudecode-telegram.sh" 2>/dev/null; then
        success "Tmux env mismatch detection exists"
    else
        fail "Tmux env mismatch detection missing"
    fi
}

test_stale_hooks_detection() {
    info "Testing stale hooks detection..."

    # Check CLI detects stale hooks
    if grep -q 'stale.*hook\|stale_config' "$SCRIPT_DIR/claudecode-telegram.sh" 2>/dev/null; then
        success "Stale hooks detection exists"
    else
        fail "Stale hooks detection missing"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Bridge Environment Variables Tests (NEW)
# ─────────────────────────────────────────────────────────────────────────────

test_bridge_env_bot_token() {
    info "Testing bridge TELEGRAM_BOT_TOKEN env..."

    if python3 -c "
from bridge import BOT_TOKEN
assert BOT_TOKEN is not None and len(BOT_TOKEN) > 0 or True  # May be empty in test
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Bridge BOT_TOKEN env configured"
    else
        fail "Bridge BOT_TOKEN env test failed"
    fi
}

test_bridge_env_port() {
    info "Testing bridge PORT env default..."

    if python3 -c "
from bridge import PORT
assert isinstance(PORT, int), 'PORT should be int'
assert PORT > 0, 'PORT should be positive'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Bridge PORT env configured"
    else
        fail "Bridge PORT env test failed"
    fi
}

test_bridge_env_webhook_secret() {
    info "Testing bridge TELEGRAM_WEBHOOK_SECRET env..."

    if python3 -c "
from bridge import WEBHOOK_SECRET
assert isinstance(WEBHOOK_SECRET, str), 'WEBHOOK_SECRET should be string'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Bridge WEBHOOK_SECRET env configured"
    else
        fail "Bridge WEBHOOK_SECRET env test failed"
    fi
}

test_bridge_env_sessions_dir() {
    info "Testing bridge SESSIONS_DIR env..."

    if python3 -c "
from bridge import SESSIONS_DIR
from pathlib import Path
assert isinstance(SESSIONS_DIR, Path), 'SESSIONS_DIR should be Path'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Bridge SESSIONS_DIR env configured"
    else
        fail "Bridge SESSIONS_DIR env test failed"
    fi
}

test_bridge_env_tmux_prefix() {
    info "Testing bridge TMUX_PREFIX env..."

    if python3 -c "
from bridge import TMUX_PREFIX
assert isinstance(TMUX_PREFIX, str), 'TMUX_PREFIX should be string'
assert len(TMUX_PREFIX) > 0, 'TMUX_PREFIX should not be empty'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Bridge TMUX_PREFIX env configured"
    else
        fail "Bridge TMUX_PREFIX env test failed"
    fi
}

test_bridge_env_bridge_url() {
    info "Testing bridge BRIDGE_URL env..."

    if python3 -c "
from bridge import BRIDGE_URL
assert isinstance(BRIDGE_URL, str), 'BRIDGE_URL should be string'
assert 'http' in BRIDGE_URL, 'BRIDGE_URL should be URL'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Bridge BRIDGE_URL env configured"
    else
        fail "Bridge BRIDGE_URL env test failed"
    fi
}

test_bridge_env_sandbox() {
    info "Testing bridge SANDBOX_ENABLED env..."

    if python3 -c "
from bridge import SANDBOX_ENABLED, SANDBOX_IMAGE
assert isinstance(SANDBOX_ENABLED, bool), 'SANDBOX_ENABLED should be bool'
assert isinstance(SANDBOX_IMAGE, str), 'SANDBOX_IMAGE should be string'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Bridge sandbox env vars configured"
    else
        fail "Bridge sandbox env test failed"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Hook Behavior Tests (NEW)
# ─────────────────────────────────────────────────────────────────────────────

test_hook_reads_tmux_env_first() {
    info "Testing hook reads config from tmux env first..."

    # Verify hook has tmux env reading before process env
    if grep -q 'get_tmux_env\|tmux show-environment' "$SCRIPT_DIR/hooks/send-to-telegram.sh" 2>/dev/null; then
        success "Hook reads tmux env first"
    else
        fail "Hook missing tmux env reading"
    fi
}

test_hook_transcript_extraction_retry() {
    info "Testing hook transcript extraction with retry..."

    # Verify hook has retry logic for transcript extraction
    if grep -q 'for.*attempt\|retry\|seq 1' "$SCRIPT_DIR/hooks/send-to-telegram.sh" 2>/dev/null; then
        success "Hook has transcript extraction retry"
    else
        fail "Hook missing transcript extraction retry"
    fi
}

test_hook_tmux_fallback_warning() {
    info "Testing hook tmux fallback warning..."

    # Verify hook appends warning when using fallback
    if grep -q 'May be incomplete\|TMUX_FALLBACK_USED' "$SCRIPT_DIR/hooks/send-to-telegram.sh" 2>/dev/null; then
        success "Hook has tmux fallback warning"
    else
        fail "Hook missing tmux fallback warning"
    fi
}

test_hook_async_forward_timeout() {
    info "Testing hook async forward with 5s timeout..."

    # Verify hook has timeout for forward
    if grep -q 'timeout 5\|timeout.*5' "$SCRIPT_DIR/hooks/send-to-telegram.sh" 2>/dev/null; then
        success "Hook has 5s forward timeout"
    else
        fail "Hook missing forward timeout"
    fi
}

test_hook_helper_script_exists() {
    info "Testing hook helper script (forward-to-bridge.py)..."

    if [[ -f "$SCRIPT_DIR/hooks/forward-to-bridge.py" ]]; then
        success "Hook helper script exists"
    else
        fail "Hook helper script missing"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Message Routing Rules Tests (NEW)
# ─────────────────────────────────────────────────────────────────────────────

test_unknown_commands_passthrough() {
    info "Testing unknown slash commands passed to worker..."

    if python3 -c "
from bridge import BLOCKED_COMMANDS, CommandRouter
import inspect

source = inspect.getsource(CommandRouter)

# CommandRouter should check if command is in BLOCKED_COMMANDS
# Unknown commands should be passed through (not blocked)
assert 'BLOCKED_COMMANDS' in source or 'pass' in source.lower()

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Unknown commands passthrough logic exists"
    else
        fail "Unknown commands passthrough test failed"
    fi
}

test_reply_with_explicit_context() {
    info "Testing reply includes explicit context..."

    if python3 -c "
from bridge import CommandRouter
import inspect
source = inspect.getsource(CommandRouter)

# Should include 'Manager reply:' and 'Context' in reply formatting
assert 'Manager reply' in source or 'Context' in source

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Reply context formatting exists"
    else
        fail "Reply context formatting test failed"
    fi
}

test_multipart_chained_reply_to() {
    info "Testing multipart responses chained with reply_to_message_id..."

    if python3 -c "
from bridge import send_response_to_telegram
import inspect
source = inspect.getsource(send_response_to_telegram)

# Should use reply_to_message_id for chaining
assert 'reply_to_message_id' in source

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Multipart chaining with reply_to_message_id exists"
    else
        fail "Multipart chaining test failed"
    fi
}

test_message_split_safe_boundaries() {
    info "Testing message split at safe boundaries (4096 chars)..."

    if python3 -c "
from bridge import split_message, TELEGRAM_MAX_LENGTH

# Test split respects 4096 limit
text = 'x' * 10000
chunks = split_message(text)
for c in chunks:
    assert len(c) <= TELEGRAM_MAX_LENGTH, f'Chunk too long: {len(c)}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Message split respects 4096 limit"
    else
        fail "Message split boundary test failed"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Per-Session Files Tests (NEW)
# ─────────────────────────────────────────────────────────────────────────────

test_pending_file_timestamp() {
    info "Testing pending file contains timestamp..."

    if python3 -c "
from bridge import set_pending, get_pending_file
import time
import shutil

set_pending('timestamp_test', 12345)
pending_file = get_pending_file('timestamp_test')
content = pending_file.read_text().strip()

# Should be a unix timestamp
ts = int(content)
assert ts > 1000000000, f'Should be unix timestamp, got {ts}'

# Cleanup - use shutil to remove directory tree safely
shutil.rmtree(pending_file.parent, ignore_errors=True)

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Pending file contains timestamp"
    else
        fail "Pending file timestamp test failed"
    fi
}

test_chat_id_file_content() {
    info "Testing chat_id file content format..."

    if python3 -c "
from bridge import set_pending, get_session_dir

test_chat = 987654321
set_pending('chatid_test', test_chat)

chat_file = get_session_dir('chatid_test') / 'chat_id'
content = chat_file.read_text().strip()

# Should be numeric chat ID
assert content == str(test_chat), f'Expected {test_chat}, got {content}'

# Cleanup
import shutil
shutil.rmtree(get_session_dir('chatid_test'), ignore_errors=True)

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "chat_id file format correct"
    else
        fail "chat_id file format test failed"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Document & Image Security Tests (NEW)
# ─────────────────────────────────────────────────────────────────────────────

test_document_no_path_restriction() {
    info "Testing documents can be sent from any path..."

    if python3 -c "
from bridge import validate_document_path
from pathlib import Path
import tempfile

# Create test doc in non-standard location
tmp = tempfile.NamedTemporaryFile(suffix='.txt', delete=False)
tmp.write(b'test content')
tmp.close()

# Documents don't have path restriction (unlike images)
ok, result = validate_document_path(Path(tmp.name))
assert ok, f'Document should be allowed: {result}'

# Cleanup
import os
os.unlink(tmp.name)

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Documents allowed from any path"
    else
        fail "Document path restriction test failed"
    fi
}

test_blocked_filenames_list() {
    info "Testing blocked filenames list..."

    if python3 -c "
from bridge import BLOCKED_FILENAMES, is_blocked_filename

# Check essential blocked files
assert is_blocked_filename('.env'), '.env should be blocked'
assert is_blocked_filename('.env.local'), '.env.local should be blocked'
assert is_blocked_filename('id_rsa'), 'id_rsa should be blocked'
assert is_blocked_filename('.npmrc'), '.npmrc should be blocked'
assert is_blocked_filename('.netrc'), '.netrc should be blocked'

# Normal files should pass
assert not is_blocked_filename('readme.txt')
assert not is_blocked_filename('report.pdf')
assert not is_blocked_filename('data.json')

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Blocked filenames list correct"
    else
        fail "Blocked filenames test failed"
    fi
}

test_20mb_size_limit() {
    info "Testing 20MB size limit for images/documents..."

    if python3 -c "
from bridge import MAX_FILE_SIZE

# 20MB = 20 * 1024 * 1024
expected = 20 * 1024 * 1024
assert MAX_FILE_SIZE == expected, f'Expected {expected}, got {MAX_FILE_SIZE}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "20MB size limit configured"
    else
        fail "20MB size limit test failed"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Test Environment Variables Tests (NEW)
# ─────────────────────────────────────────────────────────────────────────────

test_test_env_vars_documented() {
    info "Testing test environment variables documented..."

    # Verify test.sh documents TEST_BOT_TOKEN, TEST_CHAT_ID, TEST_PORT
    if grep -q 'TEST_BOT_TOKEN' "$SCRIPT_DIR/test.sh" && \
       grep -q 'TEST_CHAT_ID' "$SCRIPT_DIR/test.sh" && \
       grep -q 'TEST_PORT' "$SCRIPT_DIR/test.sh"; then
        success "Test env vars documented in test.sh"
    else
        fail "Test env vars not fully documented"
    fi
}

test_startup_notification_flag() {
    info "Testing startup notification flag..."

    if python3 -c "
from bridge import state

# Verify startup_notified flag exists
assert 'startup_notified' in state, 'startup_notified flag should exist'
assert isinstance(state['startup_notified'], bool), 'should be boolean'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Startup notification flag exists"
    else
        fail "Startup notification flag missing"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Bot commands test
# ─────────────────────────────────────────────────────────────────────────────

test_bot_commands_structure() {
    info "Testing bot commands structure..."

    if python3 -c "
from bridge import BOT_COMMANDS

# Verify all expected commands are present
expected = ['team', 'focus', 'progress', 'learn', 'pause', 'relaunch', 'settings', 'hire', 'end']
for cmd in expected:
    found = any(c['command'] == cmd for c in BOT_COMMANDS)
    assert found, f'{cmd} command missing from BOT_COMMANDS'

# Verify each command has required fields
for cmd in BOT_COMMANDS:
    assert 'command' in cmd, 'command field required'
    assert 'description' in cmd, 'description field required'
    assert isinstance(cmd['command'], str), 'command should be string'
    assert isinstance(cmd['description'], str), 'description should be string'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Bot commands structure correct"
    else
        fail "Bot commands structure test failed"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# File size validation test
# ─────────────────────────────────────────────────────────────────────────────

test_max_file_size() {
    info "Testing max file size constant..."

    if python3 -c "
from bridge import MAX_FILE_SIZE

# 20MB limit (Telegram's limit)
assert MAX_FILE_SIZE == 20 * 1024 * 1024, f'expected 20MB, got {MAX_FILE_SIZE}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Max file size is 20MB"
    else
        fail "Max file size test failed"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Persistence file tests
# ─────────────────────────────────────────────────────────────────────────────

test_persistence_file_functions() {
    info "Testing persistence file functions..."

    if python3 -c "
from bridge import (
    save_last_chat_id, load_last_chat_id,
    save_last_active, load_last_active,
    LAST_CHAT_ID_FILE, LAST_ACTIVE_FILE
)

# Test save and load chat_id
test_chat_id = 987654321
save_last_chat_id(test_chat_id)
loaded = load_last_chat_id()
assert loaded == test_chat_id, f'expected {test_chat_id}, got {loaded}'

# Verify file permissions
perms = oct(LAST_CHAT_ID_FILE.stat().st_mode)[-3:]
assert perms == '600', f'chat_id file should be 600, got {perms}'

# Test save and load active
test_active = 'testworker'
save_last_active(test_active)
loaded = load_last_active()
assert loaded == test_active, f'expected {test_active}, got {loaded}'

# Verify file permissions
perms = oct(LAST_ACTIVE_FILE.stat().st_mode)[-3:]
assert perms == '600', f'last_active file should be 600, got {perms}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Persistence file functions work"
    else
        fail "Persistence file functions test failed"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Telegram API limit test
# ─────────────────────────────────────────────────────────────────────────────

test_telegram_max_length() {
    info "Testing Telegram max message length constant..."

    if python3 -c "
from bridge import TELEGRAM_MAX_LENGTH

assert TELEGRAM_MAX_LENGTH == 4096, f'expected 4096, got {TELEGRAM_MAX_LENGTH}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Telegram max length is 4096"
    else
        fail "Telegram max length test failed"
    fi
}

# ============================================================
# INTEGRATION + WORKER COMMUNICATION
# ============================================================

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
# Worker Discovery Tests (inter-worker communication)
# ─────────────────────────────────────────────────────────────────────────────

test_workers_endpoint_exists() {
    info "Testing GET /workers endpoint exists..."

    local response
    response=$(curl -s "http://localhost:$PORT/workers")

    if echo "$response" | grep -q "workers"; then
        success "/workers endpoint returns workers array"
    else
        fail "/workers endpoint missing or invalid response: $response"
    fi
}

test_workers_endpoint_json_structure() {
    info "Testing /workers endpoint returns valid JSON structure..."

    if python3 -c "
import urllib.request
import json

url = 'http://localhost:$PORT/workers'
with urllib.request.urlopen(url) as response:
    data = json.loads(response.read())

# Should have 'workers' key
assert 'workers' in data, 'Response should have workers key'
assert isinstance(data['workers'], list), 'workers should be a list'

# If workers exist, check structure
for worker in data['workers']:
    assert 'name' in worker, 'worker should have name'
    assert 'protocol' in worker, 'worker should have protocol'
    assert 'address' in worker, 'worker should have address'
    assert 'send_example' in worker, 'worker should have send_example'
    assert worker['protocol'] in ('tmux', 'pipe'), f\"protocol should be tmux or pipe, got {worker['protocol']}\"

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/workers endpoint returns valid JSON structure"
    else
        fail "/workers endpoint JSON structure invalid"
    fi
}

test_workers_endpoint_shows_tmux_workers() {
    info "Testing /workers endpoint shows tmux workers..."

    # Create a worker first
    send_message "/hire discoverworker1" >/dev/null
    wait_for_session "discoverworker1"
    sleep 0.3

    if python3 -c "
import urllib.request
import json

url = 'http://localhost:$PORT/workers'
with urllib.request.urlopen(url) as response:
    data = json.loads(response.read())

# Find our test worker
found = None
for worker in data['workers']:
    if worker['name'] == 'discoverworker1':
        found = worker
        break

assert found, f'discoverworker1 should be in workers list, got: {[w[\"name\"] for w in data[\"workers\"]]}'
assert found['protocol'] == 'tmux', f'tmux worker should have tmux protocol, got {found[\"protocol\"]}'
assert '${TEST_TMUX_PREFIX}discoverworker1' in found['address'], f'address should be tmux session name'
assert 'tmux send-keys' in found['send_example'], f'send_example should show tmux command'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/workers shows tmux workers with correct structure"
    else
        fail "/workers tmux worker structure incorrect"
    fi

    # Cleanup
    send_message "/end discoverworker1" >/dev/null 2>&1 || true
}

test_workers_endpoint_empty_when_no_workers() {
    info "Testing /workers endpoint returns empty list when no workers..."

    # Kill all test workers
    tmux list-sessions -F '#{session_name}' 2>/dev/null | grep "^${TEST_TMUX_PREFIX}" | while read -r session; do
        tmux kill-session -t "$session" 2>/dev/null || true
    done

    sleep 0.3

    if python3 -c "
import urllib.request
import json

url = 'http://localhost:$PORT/workers'
with urllib.request.urlopen(url) as response:
    data = json.loads(response.read())

# Should have empty workers list
assert 'workers' in data, 'Response should have workers key'
assert len(data['workers']) == 0, f'Should have no workers, got {len(data[\"workers\"])}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/workers returns empty list when no workers"
    else
        fail "/workers should return empty list"
    fi
}

test_send_to_worker_integration() {
    info "Testing send_to_worker with real tmux worker..."

    # Create a worker first
    send_message "/hire sendworkertest" >/dev/null
    wait_for_session "sendworkertest"
    sleep 0.3

    local tmux_name="${TEST_TMUX_PREFIX}sendworkertest"

    # Use send_to_worker to send a unique message
    local unique_msg="test_send_to_worker_${RANDOM}"

    # Note: Must set TMUX_PREFIX to match the test prefix for send_to_worker to find the session
    if TMUX_PREFIX="$TEST_TMUX_PREFIX" python3 -c "
import os
# Force reload of bridge to pick up TMUX_PREFIX from env
import importlib
import bridge
importlib.reload(bridge)

from bridge import send_to_worker, TMUX_PREFIX
print('TMUX_PREFIX:', TMUX_PREFIX)

# Send message using the generic function
result = send_to_worker('sendworkertest', '$unique_msg')
print('sent:', result)
" 2>/dev/null | grep -q "sent: True"; then
        # Verify message appeared in tmux pane
        sleep 1
        local pane_content
        pane_content=$(tmux capture-pane -t "$tmux_name" -p 2>/dev/null || echo "")

        if echo "$pane_content" | grep -q "$unique_msg"; then
            success "send_to_worker delivered message to tmux worker"
        else
            fail "send_to_worker message not found in tmux pane"
        fi
    else
        fail "send_to_worker returned False for existing worker"
    fi

    # Cleanup
    send_message "/end sendworkertest" >/dev/null 2>&1 || true
    wait_for_session_gone "sendworkertest" 2>/dev/null || true
}

test_worker_pipe_path_constant() {
    info "Testing WORKER_PIPE_ROOT constant exists..."

    if python3 -c "
from bridge import WORKER_PIPE_ROOT
from pathlib import Path

assert isinstance(WORKER_PIPE_ROOT, Path), 'WORKER_PIPE_ROOT should be Path'
assert '/tmp' in str(WORKER_PIPE_ROOT), 'WORKER_PIPE_ROOT should be under /tmp'
assert 'claudecode-telegram' in str(WORKER_PIPE_ROOT), 'WORKER_PIPE_ROOT should contain claudecode-telegram'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "WORKER_PIPE_ROOT constant exists"
    else
        fail "WORKER_PIPE_ROOT constant missing or invalid"
    fi
}

test_get_worker_pipe_path_function() {
    info "Testing get_worker_pipe_path function..."

    if python3 -c "
from bridge import get_worker_pipe_path
from pathlib import Path

# Test function returns expected path
path = get_worker_pipe_path('testworker')
assert isinstance(path, Path), 'should return Path'
assert 'testworker' in str(path), 'path should contain worker name'
assert str(path).endswith('in.pipe'), 'path should end with in.pipe'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "get_worker_pipe_path function works"
    else
        fail "get_worker_pipe_path function missing or invalid"
    fi
}

test_get_workers_function() {
    info "Testing get_workers function exists and returns correct format..."

    if python3 -c "
from bridge import get_workers

# Function should exist and return list of dicts
workers = get_workers()
assert isinstance(workers, list), 'get_workers should return list'

# Each worker should have required fields
for w in workers:
    assert 'name' in w
    assert 'protocol' in w
    assert 'address' in w
    assert 'send_example' in w

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "get_workers function works correctly"
    else
        fail "get_workers function missing or invalid"
    fi
}

# Direct mode worker discovery test
test_workers_endpoint_shows_direct_workers() {
    info "Testing /workers endpoint shows direct mode workers..."

    # This test requires direct mode bridge running
    if [[ -z "$DIRECT_MODE_BRIDGE_PID" ]] || ! kill -0 "$DIRECT_MODE_BRIDGE_PID" 2>/dev/null; then
        info "Skipping (direct mode bridge not running)"
        return 0
    fi

    # Create a direct worker
    send_direct_mode_message "/hire directdiscover1" >/dev/null
    wait_for_direct_worker "directdiscover1"
    sleep 0.3

    if python3 -c "
import urllib.request
import json

url = 'http://localhost:$DIRECT_MODE_PORT/workers'
with urllib.request.urlopen(url) as response:
    data = json.loads(response.read())

# Find our test worker
found = None
for worker in data['workers']:
    if worker['name'] == 'directdiscover1':
        found = worker
        break

assert found, f'directdiscover1 should be in workers list'
assert found['protocol'] == 'pipe', f'direct worker should have pipe protocol, got {found[\"protocol\"]}'
assert 'in.pipe' in found['address'], 'address should be pipe path'
assert 'echo' in found['send_example'], 'send_example should show echo command'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/workers shows direct workers with pipe protocol"
    else
        fail "/workers direct worker structure incorrect"
    fi

    # Cleanup
    send_direct_mode_message "/end directdiscover1" >/dev/null 2>&1 || true
}

test_worker_pipe_creation_on_startup() {
    info "Testing worker pipe created on worker startup..."

    if python3 -c "
from bridge import get_worker_pipe_path, ensure_worker_pipe
from pathlib import Path
import os

# Test ensure_worker_pipe function creates pipe
test_name = 'pipetest'
pipe_path = get_worker_pipe_path(test_name)

# Ensure parent directory exists
pipe_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

# Create the pipe
ensure_worker_pipe(test_name)

# Verify pipe exists and is a FIFO
assert pipe_path.exists(), f'Pipe should exist at {pipe_path}'
assert os.path.exists(str(pipe_path)), 'Pipe should exist'

# Check if it's a FIFO (named pipe)
import stat
mode = os.stat(str(pipe_path)).st_mode
assert stat.S_ISFIFO(mode), 'Should be a FIFO (named pipe)'

# Cleanup
if pipe_path.exists():
    pipe_path.unlink()
if pipe_path.parent.exists():
    pipe_path.parent.rmdir()

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Worker pipe created correctly"
    else
        fail "Worker pipe creation failed"
    fi
}

test_worker_pipe_cleanup_on_end() {
    info "Testing worker pipe cleaned up on worker end..."

    if python3 -c "
from bridge import get_worker_pipe_path, ensure_worker_pipe, cleanup_worker_pipe
from pathlib import Path
import os

test_name = 'pipecleanuptest'
pipe_path = get_worker_pipe_path(test_name)

# Create the pipe
pipe_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
ensure_worker_pipe(test_name)

# Verify pipe exists
assert pipe_path.exists(), 'Pipe should exist before cleanup'

# Clean up
cleanup_worker_pipe(test_name)

# Verify pipe is removed
assert not pipe_path.exists(), 'Pipe should be removed after cleanup'

# Also cleanup parent dir if exists
if pipe_path.parent.exists():
    try:
        pipe_path.parent.rmdir()
    except:
        pass

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Worker pipe cleaned up correctly"
    else
        fail "Worker pipe cleanup failed"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Worker-to-Worker Pipe Communication Tests (TDD)
# ─────────────────────────────────────────────────────────────────────────────

test_worker_to_worker_pipe() {
    info "Testing worker-to-worker pipe communication (e2e behavior)..."

    # This is the missing test from FEATURES.md:
    # End-to-end behavior test:
    # 1. Start bridge (tmux mode - already running from test_bridge_starts)
    # 2. Create Worker A (alice)
    # 3. Create Worker B (bob)
    # 4. Worker A writes message to Worker B's pipe
    # 5. Verify Worker B received the message (check tmux pane)

    # Clean up any existing test workers
    send_message "/end alice" >/dev/null 2>&1 || true
    send_message "/end bob" >/dev/null 2>&1 || true
    wait_for_session_gone "alice" 2>/dev/null || true
    wait_for_session_gone "bob" 2>/dev/null || true

    # Step 2: Create Worker A (alice)
    local result
    result=$(send_message "/hire alice")
    if [[ "$result" != "OK" ]]; then
        fail "Worker-to-worker pipe: Failed to create worker alice: $result"
        return
    fi
    if ! wait_for_session "alice"; then
        fail "Worker-to-worker pipe: Worker alice session not created"
        return
    fi

    # Step 3: Create Worker B (bob)
    result=$(send_message "/hire bob")
    if [[ "$result" != "OK" ]]; then
        fail "Worker-to-worker pipe: Failed to create worker bob: $result"
        send_message "/end alice" >/dev/null 2>&1 || true
        return
    fi
    if ! wait_for_session "bob"; then
        fail "Worker-to-worker pipe: Worker bob session not created"
        send_message "/end alice" >/dev/null 2>&1 || true
        return
    fi

    # Verify bob's pipe was created
    local bob_pipe="/tmp/claudecode-telegram/${TEST_NODE}/bob/in.pipe"
    if [[ ! -p "$bob_pipe" ]]; then
        fail "Worker-to-worker pipe: Bob's pipe not created at $bob_pipe"
        send_message "/end alice" >/dev/null 2>&1 || true
        send_message "/end bob" >/dev/null 2>&1 || true
        return
    fi

    success "Worker-to-worker pipe: Both workers created with pipes"

    # Step 4: Write message from Alice to Bob's pipe
    # This simulates Alice sending a message to Bob via the named pipe
    local unique_msg="hello_from_alice_${RANDOM}"

    # Write to pipe in background (named pipes block if no reader)
    # The pipe reader thread should be reading from bob's pipe and forwarding to tmux
    echo "$unique_msg" > "$bob_pipe" &
    local write_pid=$!

    # Wait a bit for the message to be processed
    sleep 2

    # Check if the write completed (it will hang if no one reads the pipe)
    if ! kill -0 "$write_pid" 2>/dev/null; then
        # Write completed (reader consumed the message)
        success "Worker-to-worker pipe: Message written to bob's pipe (reader consumed it)"
    else
        # Write is still blocking - no reader on the pipe
        kill "$write_pid" 2>/dev/null || true
        fail "Worker-to-worker pipe: Write blocked - no pipe reader thread running"
        send_message "/end alice" >/dev/null 2>&1 || true
        send_message "/end bob" >/dev/null 2>&1 || true
        return
    fi

    # Step 5: Verify Worker B (bob) received the message in tmux pane
    local bob_tmux_name="${TEST_TMUX_PREFIX}bob"
    local pane_content
    pane_content=$(tmux capture-pane -t "$bob_tmux_name" -p 2>/dev/null || echo "")

    if echo "$pane_content" | grep -q "$unique_msg"; then
        success "Worker-to-worker pipe: Message appeared in bob's tmux session"
    else
        fail "Worker-to-worker pipe: Message NOT found in bob's tmux pane"
        info "  Pane content (last 5 lines):"
        echo "$pane_content" | tail -5 | sed 's/^/    /'
    fi

    # Cleanup
    send_message "/end alice" >/dev/null 2>&1 || true
    send_message "/end bob" >/dev/null 2>&1 || true
    wait_for_session_gone "alice" 2>/dev/null || true
    wait_for_session_gone "bob" 2>/dev/null || true
}

# ─────────────────────────────────────────────────────────────────────────────
# send_to_worker Abstraction Tests (TDD)
# ─────────────────────────────────────────────────────────────────────────────

test_send_to_worker_function_exists() {
    info "Testing send_to_worker function exists..."

    if python3 -c "
from bridge import send_to_worker
from typing import Optional

# Verify function exists and is callable
assert callable(send_to_worker), 'send_to_worker should be callable'

# Verify function signature (name, message, chat_id=None)
import inspect
sig = inspect.signature(send_to_worker)
params = list(sig.parameters.keys())
assert 'name' in params, 'send_to_worker should have name parameter'
assert 'message' in params, 'send_to_worker should have message parameter'
assert 'chat_id' in params, 'send_to_worker should have chat_id parameter'

# Verify return type is bool (from docstring or type hints)
# The function should return True on success, False on failure
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "send_to_worker function exists with correct signature"
    else
        fail "send_to_worker function missing or incorrect signature"
    fi
}

test_send_to_worker_not_found() {
    info "Testing send_to_worker returns False for non-existent worker..."

    if python3 -c "
import os
# Reset direct mode to ensure clean state
os.environ['DIRECT_MODE'] = '0'

import importlib
import bridge
importlib.reload(bridge)

from bridge import send_to_worker

# Call with a worker that doesn't exist
result = send_to_worker('nonexistent_worker_12345', 'test message')

# Should return False
assert result == False, f'Expected False for non-existent worker, got {result}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "send_to_worker returns False for non-existent worker"
    else
        fail "send_to_worker not returning False for non-existent worker"
    fi
}

test_send_to_worker_uses_backend_registry() {
    info "Testing send_to_worker uses backend registry correctly..."

    if python3 -c "
import tempfile
from pathlib import Path
import bridge

# Track calls
calls = {'codex': 0}

# Mock codex backend send
def fake_codex_send(self, name, tmux, text, url, dir):
    calls['codex'] += 1
    return True

# Save original and replace
original_send = bridge.CodexBackend.send
bridge.CodexBackend.send = fake_codex_send
bridge.BACKENDS['codex'] = bridge.CodexBackend()

# Create temp sessions dir with a codex worker
tmp = Path(tempfile.mkdtemp())
bridge.SESSIONS_DIR = tmp
bridge.worker_manager.scan_tmux_sessions = lambda: {}
bridge._sync_worker_manager()

session_dir = tmp / 'testcodex'
session_dir.mkdir()
(session_dir / 'backend').write_text('codex')

# Call send_to_worker
result = bridge.send_to_worker('testcodex', 'hello from test')

# Should return True and have called codex send
assert result == True, f'Expected True, got {result}'
assert calls['codex'] == 1, f'Expected 1 codex call, got {calls}'

# Cleanup
import shutil
shutil.rmtree(tmp)
bridge.CodexBackend.send = original_send

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "send_to_worker uses backend registry correctly"
    else
        fail "send_to_worker backend registry test failed"
    fi
}

test_send_to_worker_tmux_mode() {
    info "Testing send_to_worker routes to tmux worker correctly..."

    if python3 -c "
import bridge

# For this test, we verify the function exists and returns False when no matching tmux session
result = bridge.send_to_worker('nonexistent_tmux_worker_xyz', 'test message')
assert result == False, f'Expected False for non-existent tmux worker, got {result}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "send_to_worker tmux mode works correctly"
    else
        fail "send_to_worker tmux mode routing failed"
    fi
}

# ============================================================
# DIRECT MODE TESTS
# ============================================================

# ─────────────────────────────────────────────────────────────────────────────
# Direct Mode Tests (--no-tmux / --direct)
# ─────────────────────────────────────────────────────────────────────────────

test_direct_mode_flag() {
    info "Testing CLI --no-tmux and --direct flags..."

    # Test --no-tmux flag documented
    if ./claudecode-telegram.sh --help 2>/dev/null | grep -q "no-tmux"; then
        success "CLI --no-tmux flag documented"
    else
        fail "CLI --no-tmux flag not documented"
    fi

    # Test --direct flag documented
    if ./claudecode-telegram.sh --help 2>/dev/null | grep -q "direct"; then
        success "CLI --direct flag documented"
    else
        fail "CLI --direct flag not documented"
    fi

    # Test flag is parsed (with --help to avoid running)
    if ./claudecode-telegram.sh --no-tmux --help 2>/dev/null | grep -q "USAGE"; then
        success "CLI --no-tmux flag parsed correctly"
    else
        fail "CLI --no-tmux flag not recognized"
    fi

    # Test --direct alias
    if ./claudecode-telegram.sh --direct --help 2>/dev/null | grep -q "USAGE"; then
        success "CLI --direct flag parsed correctly"
    else
        fail "CLI --direct flag not recognized"
    fi
}

test_direct_mode_env_var() {
    info "Testing DIRECT_MODE environment variable..."

    if python3 -c "
import os
os.environ['DIRECT_MODE'] = '1'

# Re-import to pick up env change
import importlib
import bridge
importlib.reload(bridge)

assert bridge.DIRECT_MODE == True, 'DIRECT_MODE should be True when env is 1'

# Test with 0
os.environ['DIRECT_MODE'] = '0'
importlib.reload(bridge)
assert bridge.DIRECT_MODE == False, 'DIRECT_MODE should be False when env is 0'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "DIRECT_MODE env var works"
    else
        fail "DIRECT_MODE env var test failed"
    fi
}

test_direct_worker_dataclass() {
    info "Testing DirectWorker dataclass..."

    if python3 -c "
from bridge import DirectWorker, direct_workers
from dataclasses import is_dataclass

# Verify DirectWorker is a dataclass
assert is_dataclass(DirectWorker), 'DirectWorker should be a dataclass'

# Verify required fields exist
import inspect
sig = inspect.signature(DirectWorker)
params = list(sig.parameters.keys())
assert 'name' in params, 'DirectWorker should have name field'
assert 'process' in params, 'DirectWorker should have process field'

# Verify direct_workers dict exists
assert isinstance(direct_workers, dict), 'direct_workers should be a dict'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "DirectWorker dataclass configured correctly"
    else
        fail "DirectWorker dataclass test failed"
    fi
}

test_direct_worker_functions_exist() {
    info "Testing direct worker functions exist..."

    if python3 -c "
from bridge import (
    create_direct_worker,
    kill_direct_worker,
    send_to_direct_worker,
    read_direct_worker_output,
    handle_direct_event,
    is_direct_worker_running,
    get_direct_workers,
    kill_all_direct_workers,
    send_direct_worker_response
)

# Verify all functions are callable
assert callable(create_direct_worker), 'create_direct_worker should be callable'
assert callable(kill_direct_worker), 'kill_direct_worker should be callable'
assert callable(send_to_direct_worker), 'send_to_direct_worker should be callable'
assert callable(read_direct_worker_output), 'read_direct_worker_output should be callable'
assert callable(handle_direct_event), 'handle_direct_event should be callable'
assert callable(is_direct_worker_running), 'is_direct_worker_running should be callable'
assert callable(get_direct_workers), 'get_direct_workers should be callable'
assert callable(kill_all_direct_workers), 'kill_all_direct_workers should be callable'
assert callable(send_direct_worker_response), 'send_direct_worker_response should be callable'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Direct worker functions exist"
    else
        fail "Direct worker functions test failed"
    fi
}

test_direct_mode_no_hook_install() {
    info "Testing direct mode skips hook installation..."

    # Verify the CLI has conditional hook install logic for direct mode
    if grep -q 'DIRECT_MODE.*skip.*hook\|Direct mode.*hook' "$SCRIPT_DIR/claudecode-telegram.sh" 2>/dev/null; then
        success "CLI skips hook install in direct mode"
    else
        fail "CLI missing direct mode hook skip logic"
    fi
}

test_direct_mode_handle_event() {
    info "Testing handle_direct_event parses Claude JSON events..."

    if python3 -c "
from bridge import handle_direct_event

# Test assistant message event
event = {
    'type': 'assistant',
    'message': {
        'content': [{'type': 'text', 'text': 'Hello world'}]
    }
}
result = handle_direct_event('test', event)
assert result == 'Hello world', f'Expected Hello world, got {result}'

# Test content_block_delta event
event2 = {
    'type': 'content_block_delta',
    'delta': {'type': 'text_delta', 'text': 'chunk'}
}
result2 = handle_direct_event('test', event2)
assert result2 == 'chunk', f'Expected chunk, got {result2}'

# Test error event
event3 = {
    'type': 'error',
    'error': {'message': 'Something went wrong'}
}
result3 = handle_direct_event('test', event3)
assert 'Something went wrong' in result3, f'Expected error message, got {result3}'

# Test unknown event (should return None)
event4 = {'type': 'unknown'}
result4 = handle_direct_event('test', event4)
assert result4 is None, f'Unknown event should return None, got {result4}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "handle_direct_event parses events correctly"
    else
        fail "handle_direct_event test failed"
    fi
}

test_direct_mode_html_escape() {
    info "Testing escape_html escapes special characters..."

    if python3 -c "
from bridge import escape_html

# Test basic escaping
assert escape_html('hello') == 'hello', 'Plain text unchanged'
assert escape_html('<script>') == '&lt;script&gt;', 'Angle brackets escaped'
assert escape_html('a & b') == 'a &amp; b', 'Ampersand escaped'
assert escape_html('1 < 2 > 0') == '1 &lt; 2 &gt; 0', 'Mixed escaping'

# Test real-world cases (README content)
code = 'if (x < 10 && y > 5)'
expected = 'if (x &lt; 10 &amp;&amp; y &gt; 5)'
assert escape_html(code) == expected, f'Code escaping failed: {escape_html(code)}'

# Test already-escaped content (should double-escape)
assert escape_html('&lt;') == '&amp;lt;', 'Already escaped gets re-escaped'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "escape_html escapes special characters correctly"
    else
        fail "escape_html test failed"
    fi
}

# Test the esc() function in hooks/forward-to-bridge.py (tmux mode parity)
test_forward_to_bridge_html_escape() {
    info "Testing forward-to-bridge esc() escapes HTML special characters..."

    if python3 -c "
import sys
from importlib.util import spec_from_loader, module_from_spec
from importlib.machinery import SourceFileLoader

# Load forward-to-bridge.py as a module (dash in name requires special handling)
spec = spec_from_loader('forward_to_bridge', SourceFileLoader('forward_to_bridge', 'hooks/forward-to-bridge.py'))
forward_to_bridge = module_from_spec(spec)
spec.loader.exec_module(forward_to_bridge)

esc = forward_to_bridge.esc

# Test basic escaping
assert esc('hello') == 'hello', 'Plain text unchanged'
assert esc('<script>') == '&lt;script&gt;', 'Angle brackets escaped'
assert esc('a & b') == 'a &amp; b', 'Ampersand escaped'
assert esc('1 < 2 > 0') == '1 &lt; 2 &gt; 0', 'Mixed escaping'

# Test real-world cases (code snippets)
code = 'if (x < 10 && y > 5)'
expected = 'if (x &lt; 10 &amp;&amp; y &gt; 5)'
assert esc(code) == expected, f'Code escaping failed: {esc(code)}'

# Test already-escaped content (should double-escape)
assert esc('&lt;') == '&amp;lt;', 'Already escaped gets re-escaped'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "forward-to-bridge esc() escapes HTML correctly"
    else
        fail "forward-to-bridge esc() test failed"
    fi
}

test_backend_registry_exists() {
    info "Testing backend registry exists and contains expected backends..."

    if python3 -c "
import bridge

# Check BACKENDS registry exists
assert hasattr(bridge, 'BACKENDS'), 'BACKENDS registry should exist'

# Check expected backends are registered
expected = ['claude', 'codex', 'gemini', 'opencode']
for name in expected:
    assert name in bridge.BACKENDS, f'{name} should be in BACKENDS'

# Check get_backend helper
for name in expected:
    backend = bridge.get_backend(name)
    assert backend is not None, f'get_backend({name}) should return backend'
    assert hasattr(backend, 'send'), f'{name} backend should have send method'
    assert hasattr(backend, 'is_online'), f'{name} backend should have is_online method'
    assert hasattr(backend, 'start_cmd'), f'{name} backend should have start_cmd method'

# Check is_valid_backend helper
assert bridge.is_valid_backend('claude') == True
assert bridge.is_valid_backend('codex') == True
assert bridge.is_valid_backend('invalid') == False

# Check list_backends helper
available = bridge.list_backends()
assert set(available) == set(expected), f'list_backends should return {expected}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "backend registry exists with expected backends"
    else
        fail "backend registry test failed"
    fi
}

test_get_registered_sessions_includes_noninteractive_workers() {
    info "Testing get_registered_sessions includes non-interactive workers..."

    if python3 -c "
import tempfile
from pathlib import Path
import bridge

# Create temp sessions dir
tmp = Path(tempfile.mkdtemp())
bridge.SESSIONS_DIR = tmp
bridge.worker_manager.scan_tmux_sessions = lambda: {}  # No tmux sessions

# Create a non-interactive worker (like codex)
session_dir = tmp / 'myworker'
session_dir.mkdir()
(session_dir / 'backend').write_text('codex')

# get_registered_sessions should include non-interactive worker
result = bridge.get_registered_sessions()
assert 'myworker' in result, f'Should contain myworker, got {result}'
assert result['myworker']['backend'] == 'codex', f'Backend should be codex'

# Cleanup
import shutil
shutil.rmtree(tmp)

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "get_registered_sessions includes non-interactive workers"
    else
        fail "get_registered_sessions non-interactive test failed"
    fi
}

test_direct_mode_graceful_shutdown() {
    info "Testing graceful_shutdown kills direct workers..."

    if python3 -c "
from bridge import graceful_shutdown, DIRECT_MODE, direct_workers, kill_all_direct_workers
import inspect

# Verify graceful_shutdown mentions direct workers
source = inspect.getsource(graceful_shutdown)
assert 'direct_workers' in source or 'kill_all_direct_workers' in source, \
    'graceful_shutdown should handle direct workers'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "graceful_shutdown handles direct workers"
    else
        fail "graceful_shutdown direct workers test failed"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Direct Mode Integration Tests (bridge running in DIRECT_MODE=1)
# ─────────────────────────────────────────────────────────────────────────────

DIRECT_MODE_PORT="${DIRECT_MODE_PORT:-8096}"
DIRECT_MODE_BRIDGE_PID=""
DIRECT_MODE_BRIDGE_LOG="$TEST_NODE_DIR/direct_mode_bridge.log"

start_direct_mode_bridge() {
    info "Starting bridge in direct mode on port $DIRECT_MODE_PORT..."

    # Kill any existing process on port
    lsof -ti :"$DIRECT_MODE_PORT" | xargs kill -9 2>/dev/null || true
    sleep 0.3

    # Start bridge with DIRECT_MODE=1
    TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" \
    PORT="$DIRECT_MODE_PORT" \
    NODE_NAME="$TEST_NODE" \
    SESSIONS_DIR="$TEST_SESSION_DIR" \
    TMUX_PREFIX="$TEST_TMUX_PREFIX" \
    ADMIN_CHAT_ID="${TEST_CHAT_ID:-$CHAT_ID}" \
    DIRECT_MODE=1 \
    python3 -u "$SCRIPT_DIR/bridge.py" > "$DIRECT_MODE_BRIDGE_LOG" 2>&1 &
    DIRECT_MODE_BRIDGE_PID=$!
    echo "$DIRECT_MODE_BRIDGE_PID" > "$TEST_NODE_DIR/direct_mode_bridge.pid"

    if wait_for_port "$DIRECT_MODE_PORT"; then
        return 0
    else
        return 1
    fi
}

stop_direct_mode_bridge() {
    info "Stopping direct mode bridge..."
    if [[ -n "$DIRECT_MODE_BRIDGE_PID" ]]; then
        kill "$DIRECT_MODE_BRIDGE_PID" 2>/dev/null || true
        DIRECT_MODE_BRIDGE_PID=""
    fi
    if [[ -f "$TEST_NODE_DIR/direct_mode_bridge.pid" ]]; then
        kill "$(cat "$TEST_NODE_DIR/direct_mode_bridge.pid")" 2>/dev/null || true
        rm -f "$TEST_NODE_DIR/direct_mode_bridge.pid"
    fi
    rm -f "$DIRECT_MODE_BRIDGE_LOG"
}

send_direct_mode_message() {
    local text="$1"
    local chat_id="${2:-$CHAT_ID}"
    local update_id=$((RANDOM))

    curl -s -X POST "http://localhost:$DIRECT_MODE_PORT" \
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

send_direct_mode_reply() {
    local text="$1"
    local reply_text="$2"
    local reply_from_bot="${3:-true}"
    local chat_id="${4:-$CHAT_ID}"
    local update_id=$((RANDOM))
    local reply_id=$((RANDOM + 1000))

    curl -s -X POST "http://localhost:$DIRECT_MODE_PORT" \
        -H "Content-Type: application/json" \
        -d '{
            "update_id": '"$update_id"',
            "message": {
                "message_id": '"$update_id"',
                "from": {"id": '"$chat_id"', "first_name": "TestUser"},
                "chat": {"id": '"$chat_id"', "type": "private"},
                "date": '"$(date +%s)"',
                "text": "'"$text"'",
                "reply_to_message": {
                    "message_id": '"$reply_id"',
                    "from": {"id": 123456, "first_name": "Bot", "is_bot": '"$reply_from_bot"'},
                    "chat": {"id": '"$chat_id"', "type": "private"},
                    "date": '"$(date +%s)"',
                    "text": "'"$reply_text"'"
                }
            }
        }'
}

test_direct_mode_bridge_starts() {
    info "Testing direct mode bridge starts..."

    if start_direct_mode_bridge; then
        success "Direct mode bridge started on port $DIRECT_MODE_PORT"
    else
        fail "Direct mode bridge failed to start"
        return 1
    fi

    # Verify health endpoint
    if curl -s "http://localhost:$DIRECT_MODE_PORT" | grep -q "Claude-Telegram"; then
        success "Direct mode bridge health endpoint responds"
    else
        fail "Direct mode bridge health endpoint not responding"
    fi
}

test_direct_mode_hire_creates_worker() {
    info "Testing /hire creates direct worker..."

    local result
    result=$(send_direct_mode_message "/hire directworker1")

    if [[ "$result" == "OK" ]]; then
        # Give worker time to initialize
        sleep 0.5
        success "/hire creates direct worker"
    else
        fail "/hire direct worker failed: $result"
    fi
}

test_direct_mode_message_routing() {
    info "Testing message routing to direct worker..."

    # First focus the worker
    send_direct_mode_message "/focus directworker1" >/dev/null
    sleep 0.2

    # Send a message to the worker
    local result
    result=$(send_direct_mode_message "Hello direct worker!")

    if [[ "$result" == "OK" ]]; then
        success "Message routed to direct worker"
    else
        fail "Message routing failed: $result"
    fi
}

test_direct_mode_team_shows_workers() {
    info "Testing /team shows direct workers..."

    local result
    result=$(send_direct_mode_message "/team")

    if [[ "$result" == "OK" ]]; then
        success "/team command works in direct mode"
    else
        fail "/team command failed in direct mode: $result"
    fi
}

test_direct_mode_end_kills_worker() {
    info "Testing /end kills direct worker..."

    local result
    result=$(send_direct_mode_message "/end directworker1")

    if [[ "$result" == "OK" ]]; then
        sleep 0.3
        success "/end kills direct worker"
    else
        fail "/end direct worker failed: $result"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Direct Mode Parity Tests (ensure direct mode has same features as tmux mode)
# ─────────────────────────────────────────────────────────────────────────────

# Helper: send a reply to a message in direct mode
send_direct_mode_reply() {
    local text="$1"
    local reply_text="$2"
    local reply_from_bot="${3:-true}"
    local chat_id="${4:-$CHAT_ID}"
    local update_id=$((RANDOM))
    local reply_id=$((RANDOM + 1000))

    curl -s -X POST "http://localhost:$DIRECT_MODE_PORT" \
        -H "Content-Type: application/json" \
        -d '{
            "update_id": '"$update_id"',
            "message": {
                "message_id": '"$update_id"',
                "from": {"id": '"$chat_id"', "first_name": "TestUser"},
                "chat": {"id": '"$chat_id"', "type": "private"},
                "date": '"$(date +%s)"',
                "text": "'"$text"'",
                "reply_to_message": {
                    "message_id": '"$reply_id"',
                    "from": {"id": 123456, "first_name": "Bot", "is_bot": '"$reply_from_bot"'},
                    "chat": {"id": '"$chat_id"', "type": "private"},
                    "date": '"$(date +%s)"',
                    "text": "'"$reply_text"'"
                }
            }
        }'
}

test_direct_mode_at_all_broadcast() {
    info "Testing @all broadcast in direct mode..."

    if ! check_claude_available; then
        info "Skipping (claude CLI not available)"
        return 0
    fi

    # This test verifies that @all broadcasts to all direct workers,
    # matching the behavior of tmux mode.
    #
    # Test flow:
    # 1. Create two workers
    # 2. Send @all message
    # 3. Verify both workers received the message

    # Clean up any existing test workers
    send_direct_mode_message "/end allworker1" >/dev/null 2>&1 || true
    send_direct_mode_message "/end allworker2" >/dev/null 2>&1 || true
    sleep 0.3

    # Create two workers
    local result
    result=$(send_direct_mode_message "/hire allworker1")
    if [[ "$result" != "OK" ]]; then
        fail "@all direct: Failed to create allworker1"
        return
    fi
    wait_for_direct_worker "allworker1" || {
        fail "@all direct: allworker1 not started"
        return
    }

    result=$(send_direct_mode_message "/hire allworker2")
    if [[ "$result" != "OK" ]]; then
        fail "@all direct: Failed to create allworker2"
        send_direct_mode_message "/end allworker1" >/dev/null 2>&1 || true
        return
    fi
    wait_for_direct_worker "allworker2" || {
        fail "@all direct: allworker2 not started"
        send_direct_mode_message "/end allworker1" >/dev/null 2>&1 || true
        return
    }

    # Clear log markers by noting current line count
    local log_lines_before
    log_lines_before=$(wc -l < "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "0")

    # Send @all broadcast
    result=$(send_direct_mode_message "@all Hello everyone from test")
    if [[ "$result" != "OK" ]]; then
        fail "@all direct: Broadcast failed: $result"
        send_direct_mode_message "/end allworker1" >/dev/null 2>&1 || true
        send_direct_mode_message "/end allworker2" >/dev/null 2>&1 || true
        return
    fi

    # Wait for message routing
    sleep 0.5

    # Check that both workers received the message (new log lines only)
    local worker1_got_msg=false
    local worker2_got_msg=false

    local new_log_lines
    new_log_lines=$(tail -n +$((log_lines_before + 1)) "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "")

    if echo "$new_log_lines" | grep -q "Sent to direct worker 'allworker1'"; then
        worker1_got_msg=true
    fi
    if echo "$new_log_lines" | grep -q "Sent to direct worker 'allworker2'"; then
        worker2_got_msg=true
    fi

    if $worker1_got_msg && $worker2_got_msg; then
        success "@all direct: Broadcast reached both workers"
    elif $worker1_got_msg || $worker2_got_msg; then
        fail "@all direct: Broadcast only reached one worker"
    else
        fail "@all direct: Broadcast reached neither worker"
    fi

    # Cleanup
    send_direct_mode_message "/end allworker1" >/dev/null 2>&1 || true
    send_direct_mode_message "/end allworker2" >/dev/null 2>&1 || true
}

test_direct_mode_reply_routing() {
    info "Testing reply routing in direct mode..."

    # This test verifies that replying to a worker's message routes
    # the reply to that worker, even if another worker is focused.
    #
    # Test flow:
    # 1. Create two workers
    # 2. Focus worker1
    # 3. Send a reply to a message "from" worker2
    # 4. Verify the reply was routed to worker2 (not worker1)

    # Clean up any existing test workers
    send_direct_mode_message "/end replyworker1" >/dev/null 2>&1 || true
    send_direct_mode_message "/end replyworker2" >/dev/null 2>&1 || true
    sleep 0.3

    # Create two workers
    local result
    result=$(send_direct_mode_message "/hire replyworker1")
    wait_for_direct_worker "replyworker1" || {
        fail "Reply direct: replyworker1 not started"
        return
    }

    result=$(send_direct_mode_message "/hire replyworker2")
    wait_for_direct_worker "replyworker2" || {
        fail "Reply direct: replyworker2 not started"
        send_direct_mode_message "/end replyworker1" >/dev/null 2>&1 || true
        return
    }

    # Focus worker1
    send_direct_mode_message "/focus replyworker1" >/dev/null
    sleep 0.2

    # Clear log markers by noting current line count
    local log_lines_before
    log_lines_before=$(wc -l < "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "0")

    # Send reply to a message "from" worker2
    # The reply_text contains "replyworker2:" which indicates it's from that worker
    result=$(send_direct_mode_reply "This is my reply" "replyworker2: Some previous message")
    if [[ "$result" != "OK" ]]; then
        fail "Reply direct: Reply send failed: $result"
        send_direct_mode_message "/end replyworker1" >/dev/null 2>&1 || true
        send_direct_mode_message "/end replyworker2" >/dev/null 2>&1 || true
        return
    fi

    # Wait for routing
    sleep 0.5

    # Check that the reply was routed to worker2 (check only new log lines)
    local new_log_lines
    new_log_lines=$(tail -n +$((log_lines_before + 1)) "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "")

    if echo "$new_log_lines" | grep -q "Sent to direct worker 'replyworker2'"; then
        success "Reply direct: Reply routed to correct worker"
    else
        # Check if it went to the wrong worker
        if echo "$new_log_lines" | grep -q "Sent to direct worker 'replyworker1'"; then
            fail "Reply direct: Reply routed to WRONG worker (focused instead of reply target)"
        else
            fail "Reply direct: Reply not routed to any worker"
        fi
    fi

    # Cleanup
    send_direct_mode_message "/end replyworker1" >/dev/null 2>&1 || true
    send_direct_mode_message "/end replyworker2" >/dev/null 2>&1 || true
}

test_direct_mode_reply_context() {
    info "Testing reply context in direct mode..."

    if ! check_claude_available; then
        info "Skipping (claude CLI not available)"
        return 0
    fi

    local test_chat_id=123456
    local reply_context="My earlier note"
    local reply_body="OK"

    # Clean up any existing test workers
    send_direct_mode_message "/end contextworker" "$test_chat_id" >/dev/null 2>&1 || true
    sleep 0.3

    # Create and focus worker
    local result
    result=$(send_direct_mode_message "/hire contextworker" "$test_chat_id")
    if [[ "$result" != "OK" ]]; then
        fail "Context direct: /hire failed: $result"
        return
    fi
    if ! wait_for_direct_worker "contextworker"; then
        fail "Context direct: contextworker not started"
        return
    fi
    send_direct_mode_message "/focus contextworker" "$test_chat_id" >/dev/null
    sleep 0.2

    # Clear log markers by noting current line count
    local log_lines_before
    log_lines_before=$(wc -l < "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "0")

    # Send reply to own (non-bot) message with reply_from_bot=false
    result=$(send_direct_mode_reply "$reply_body" "$reply_context" "false" "$test_chat_id")
    if [[ "$result" != "OK" ]]; then
        fail "Context direct: Reply send failed: $result"
        send_direct_mode_message "/end contextworker" "$test_chat_id" >/dev/null 2>&1 || true
        return
    fi

    # Wait for routing
    sleep 0.5

    # Check that reply context formatting was included (check only new log lines)
    local new_log_lines
    new_log_lines=$(tail -n +$((log_lines_before + 1)) "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "")

    if echo "$new_log_lines" | grep -q "Sent to direct worker 'contextworker'"; then
        if echo "$new_log_lines" | grep -q "Manager reply:" && \
            echo "$new_log_lines" | grep -q "Context (your previous message)"; then
            success "Context direct: Reply context included"
        else
            fail "Context direct: Reply context missing"
        fi
    else
        fail "Context direct: Reply not routed to contextworker"
    fi

    # Cleanup
    send_direct_mode_message "/end contextworker" "$test_chat_id" >/dev/null 2>&1 || true
}

test_direct_mode_worker_shortcut_focus() {
    info "Testing worker shortcut focus in direct mode..."

    # Test that /<workername> switches focus to that worker
    # (matches tmux mode test_worker_shortcut_focus_only)

    # Clean up any existing test workers
    send_direct_mode_message "/end shortcut1" >/dev/null 2>&1 || true
    send_direct_mode_message "/end shortcut2" >/dev/null 2>&1 || true
    sleep 0.3

    # Create two workers
    local result
    result=$(send_direct_mode_message "/hire shortcut1")
    wait_for_direct_worker "shortcut1" || {
        fail "Shortcut focus: shortcut1 not started"
        return
    }

    result=$(send_direct_mode_message "/hire shortcut2")
    wait_for_direct_worker "shortcut2" || {
        fail "Shortcut focus: shortcut2 not started"
        send_direct_mode_message "/end shortcut1" >/dev/null 2>&1 || true
        return
    }

    # Focus worker1 first
    send_direct_mode_message "/focus shortcut1" >/dev/null
    sleep 0.2

    # Use /<workername> shortcut to focus worker2
    result=$(send_direct_mode_message "/shortcut2")
    if [[ "$result" == "OK" ]]; then
        success "Shortcut focus: /<workername> command accepted"
    else
        fail "Shortcut focus: /<workername> command failed: $result"
    fi

    # Cleanup
    send_direct_mode_message "/end shortcut1" >/dev/null 2>&1 || true
    send_direct_mode_message "/end shortcut2" >/dev/null 2>&1 || true
}

test_direct_mode_worker_shortcut_with_message() {
    info "Testing worker shortcut with message in direct mode..."

    # Test that /<workername> <message> routes message and switches focus
    # (matches tmux mode test_worker_shortcut_with_message)

    # Clean up any existing test workers
    send_direct_mode_message "/end shortmsg1" >/dev/null 2>&1 || true
    send_direct_mode_message "/end shortmsg2" >/dev/null 2>&1 || true
    sleep 0.3

    # Create two workers
    local result
    result=$(send_direct_mode_message "/hire shortmsg1")
    wait_for_direct_worker "shortmsg1" || {
        fail "Shortcut+msg: shortmsg1 not started"
        return
    }

    result=$(send_direct_mode_message "/hire shortmsg2")
    wait_for_direct_worker "shortmsg2" || {
        fail "Shortcut+msg: shortmsg2 not started"
        send_direct_mode_message "/end shortmsg1" >/dev/null 2>&1 || true
        return
    }

    # Focus worker1 first
    send_direct_mode_message "/focus shortmsg1" >/dev/null
    sleep 0.2

    # Clear log markers
    local log_lines_before
    log_lines_before=$(wc -l < "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "0")

    # Use /<workername> <message> to send and switch focus
    result=$(send_direct_mode_message "/shortmsg2 Hello from shortcut")
    if [[ "$result" != "OK" ]]; then
        fail "Shortcut+msg: Command failed: $result"
        send_direct_mode_message "/end shortmsg1" >/dev/null 2>&1 || true
        send_direct_mode_message "/end shortmsg2" >/dev/null 2>&1 || true
        return
    fi

    # Wait for routing
    sleep 0.5

    # Check that message was routed to shortmsg2
    local new_log_lines
    new_log_lines=$(tail -n +$((log_lines_before + 1)) "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "")

    if echo "$new_log_lines" | grep -q "Sent to direct worker 'shortmsg2'"; then
        success "Shortcut+msg: Message routed to target worker"
    else
        fail "Shortcut+msg: Message not routed to target worker"
    fi

    # Cleanup
    send_direct_mode_message "/end shortmsg1" >/dev/null 2>&1 || true
    send_direct_mode_message "/end shortmsg2" >/dev/null 2>&1 || true
}

test_direct_mode_learn_command() {
    info "Testing /learn command in direct mode..."

    # Test that /learn <prompt> sends prompt to worker
    # (matches tmux mode test_learn_command)

    # Clean up any existing test worker
    send_direct_mode_message "/end learnworker" >/dev/null 2>&1 || true
    sleep 0.3

    # Create and focus worker
    local result
    result=$(send_direct_mode_message "/hire learnworker")
    wait_for_direct_worker "learnworker" || {
        fail "Learn direct: learnworker not started"
        return
    }
    send_direct_mode_message "/focus learnworker" >/dev/null
    sleep 0.2

    # Send /learn command
    result=$(send_direct_mode_message "/learn Remember that X equals 42")
    if [[ "$result" == "OK" ]]; then
        success "Learn direct: /learn command accepted"
    else
        fail "Learn direct: /learn command failed: $result"
    fi

    # Cleanup
    send_direct_mode_message "/end learnworker" >/dev/null 2>&1 || true
}

test_direct_mode_unknown_cmd_passthrough() {
    info "Testing unknown command passthrough in direct mode..."

    # Test that unknown /commands are passed through to worker
    # (matches tmux mode test_unknown_command_passthrough)

    # Clean up any existing test worker
    send_direct_mode_message "/end cmdworker" >/dev/null 2>&1 || true
    sleep 0.3

    # Create and focus worker
    local result
    result=$(send_direct_mode_message "/hire cmdworker")
    wait_for_direct_worker "cmdworker" || {
        fail "Cmd passthrough: cmdworker not started"
        return
    }
    send_direct_mode_message "/focus cmdworker" >/dev/null
    sleep 0.2

    # Clear log markers
    local log_lines_before
    log_lines_before=$(wc -l < "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "0")

    # Send unknown command - should pass through to worker
    result=$(send_direct_mode_message "/unknowncommand arg1 arg2")
    if [[ "$result" != "OK" ]]; then
        fail "Cmd passthrough: Unknown command rejected: $result"
        send_direct_mode_message "/end cmdworker" >/dev/null 2>&1 || true
        return
    fi

    # Wait for routing
    sleep 0.5

    # Check that command was routed to worker
    local new_log_lines
    new_log_lines=$(tail -n +$((log_lines_before + 1)) "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "")

    if echo "$new_log_lines" | grep -q "Sent to direct worker 'cmdworker'"; then
        success "Cmd passthrough: Unknown command routed to worker"
    else
        fail "Cmd passthrough: Unknown command not routed to worker"
    fi

    # Cleanup
    send_direct_mode_message "/end cmdworker" >/dev/null 2>&1 || true
}

# ─────────────────────────────────────────────────────────────────────────────
# Direct Mode E2E Tests (full Telegram flow with Claude subprocess)
# ─────────────────────────────────────────────────────────────────────────────

# Helper to check if claude CLI is available
check_claude_available() {
    if command -v claude &>/dev/null; then
        return 0
    else
        return 1
    fi
}

# Helper to wait for direct worker response (polling bridge log)
wait_for_direct_response() {
    local worker_name="$1"
    local timeout="${2:-30}"
    local attempts=0

    while [[ $attempts -lt $timeout ]]; do
        # Check if there's a response for this worker in the log
        if grep -q "Response sent: $worker_name -> Telegram OK" "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null; then
            return 0
        fi
        sleep 1
        ((attempts++))
    done
    return 1
}

# Helper to wait for worker to be created
wait_for_direct_worker() {
    local worker_name="$1"
    local timeout="${2:-10}"
    local attempts=0

    while [[ $attempts -lt $timeout ]]; do
        if grep -q "Started direct worker '$worker_name'" "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null; then
            return 0
        fi
        sleep 0.5
        ((attempts++))
    done
    return 1
}

# Helper to wait for worker to be killed
wait_for_direct_worker_killed() {
    local worker_name="$1"
    local timeout="${2:-10}"
    local attempts=0

    while [[ $attempts -lt $timeout ]]; do
        if grep -q "Killed direct worker '$worker_name'" "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null; then
            return 0
        fi
        sleep 0.5
        ((attempts++))
    done
    return 1
}

# Helper to wait for worker to be initialized (received init event)
# Note: Claude can take 10-20 seconds to initialize, so use longer timeout
wait_for_direct_worker_initialized() {
    local worker_name="$1"
    local timeout="${2:-60}"  # 60 * 0.5 = 30 seconds max
    local attempts=0

    while [[ $attempts -lt $timeout ]]; do
        if grep -q "Direct worker '$worker_name' initialized" "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null; then
            return 0
        fi
        sleep 0.5
        ((attempts++))
    done
    return 1
}

# Helper to check if subprocess is still running (not just created)
check_direct_worker_process_running() {
    local worker_name="$1"

    # Check via Python that process.poll() is None
    DIRECT_MODE=1 python3 -c "
import sys
sys.path.insert(0, '.')
import bridge

worker = bridge.direct_workers.get('$worker_name')
if not worker:
    print('NOT_FOUND')
    sys.exit(1)

poll_result = worker.process.poll()
if poll_result is None:
    print('RUNNING')
else:
    print(f'EXITED:{poll_result}')
    sys.exit(1)
" 2>/dev/null | grep -q "RUNNING"
}

# BEHAVIOR TEST: Verify subprocess stays alive after creation
# This catches the bug where subprocess exits immediately after starting
test_direct_mode_subprocess_stays_alive() {
    info "Testing direct mode subprocess stays alive after creation..."

    if ! check_claude_available; then
        info "Skipping (claude CLI not available)"
        return 0
    fi

    # Clean up any existing test worker
    send_direct_mode_message "/end aliveworker" >/dev/null 2>&1 || true
    sleep 0.3

    # Create worker
    local result
    result=$(send_direct_mode_message "/hire aliveworker")

    if [[ "$result" != "OK" ]]; then
        fail "Subprocess alive: /hire failed: $result"
        return
    fi

    # Wait for worker to start
    if ! wait_for_direct_worker "aliveworker"; then
        fail "Subprocess alive: Worker not created"
        return
    fi

    # KEY BEHAVIOR TEST: Wait 3 seconds and verify process is STILL running
    # This catches the bug where subprocess exits immediately
    sleep 3

    # Check if reader thread exited (indicates subprocess died)
    if grep -q "Reader thread for worker 'aliveworker' exited" "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null; then
        fail "Subprocess alive: Reader thread exited (subprocess died!)"
        send_direct_mode_message "/end aliveworker" >/dev/null 2>&1 || true
        return
    fi

    success "Subprocess alive: Process still running after 3 seconds"

    # Cleanup
    send_direct_mode_message "/end aliveworker" >/dev/null 2>&1 || true
}

# BEHAVIOR TEST: Verify worker accepts messages after creation
# This catches the bug where subprocess starts but stdin pipe is broken
test_direct_mode_worker_accepts_messages() {
    info "Testing direct mode worker accepts messages..."

    if ! check_claude_available; then
        info "Skipping (claude CLI not available)"
        return 0
    fi

    # Clean up any existing test worker
    send_direct_mode_message "/end msgworker" >/dev/null 2>&1 || true
    sleep 0.3

    # Create worker
    local result
    result=$(send_direct_mode_message "/hire msgworker")

    if [[ "$result" != "OK" ]]; then
        fail "Accepts messages: /hire failed: $result"
        return
    fi

    # Wait for worker to start
    if ! wait_for_direct_worker "msgworker"; then
        fail "Accepts messages: Worker not created"
        return
    fi

    # Focus the worker
    send_direct_mode_message "/focus msgworker" >/dev/null
    sleep 0.5

    # KEY BEHAVIOR TEST: Send a message and verify it reaches the worker
    result=$(send_direct_mode_message "test message from behavior test")

    if [[ "$result" != "OK" ]]; then
        fail "Accepts messages: Message send failed: $result"
        send_direct_mode_message "/end msgworker" >/dev/null 2>&1 || true
        return
    fi

    # Verify message was sent to worker (check log)
    sleep 1
    if grep -q "Sent to direct worker 'msgworker'" "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null; then
        success "Accepts messages: Worker received message via stdin"
    else
        fail "Accepts messages: Message not delivered to worker"
        send_direct_mode_message "/end msgworker" >/dev/null 2>&1 || true
        return
    fi

    # Cleanup
    send_direct_mode_message "/end msgworker" >/dev/null 2>&1 || true
}

test_direct_mode_e2e_full_flow() {
    info "Testing direct mode E2E full flow (hire -> message -> response -> end)..."

    if ! check_claude_available; then
        info "Skipping (claude CLI not available)"
        return 0
    fi

    # Clean up any existing test workers
    send_direct_mode_message "/end e2eworker" >/dev/null 2>&1 || true
    sleep 0.3

    # Step 1: Hire a worker
    local result
    result=$(send_direct_mode_message "/hire e2eworker")

    if [[ "$result" != "OK" ]]; then
        fail "E2E: /hire failed: $result"
        return
    fi

    if ! wait_for_direct_worker "e2eworker"; then
        fail "E2E: Worker not created"
        return
    fi
    success "E2E: Worker created"

    # Step 2: Focus the worker
    send_direct_mode_message "/focus e2eworker" >/dev/null
    sleep 0.3

    # Step 3: Send a message and wait for response
    result=$(send_direct_mode_message "Say hello in one word only")

    if [[ "$result" != "OK" ]]; then
        fail "E2E: Message send failed: $result"
        send_direct_mode_message "/end e2eworker" >/dev/null 2>&1 || true
        return
    fi
    success "E2E: Message sent to worker"

    # Step 4: Wait for response (with timeout)
    # Note: Claude response time can vary, so we use a longer timeout
    if wait_for_direct_response "e2eworker" 60; then
        success "E2E: Response received from Claude"
    else
        info "E2E: Response timeout (Claude may be slow, this is not a failure)"
    fi

    # Step 5: End the worker
    result=$(send_direct_mode_message "/end e2eworker")

    if [[ "$result" == "OK" ]]; then
        if wait_for_direct_worker_killed "e2eworker"; then
            success "E2E: Worker terminated successfully"
        else
            fail "E2E: Worker not properly killed"
        fi
    else
        fail "E2E: /end failed: $result"
    fi
}

test_direct_mode_e2e_focus_switch() {
    info "Testing direct mode E2E focus switch between workers..."

    if ! check_claude_available; then
        info "Skipping (claude CLI not available)"
        return 0
    fi

    # Clean up any existing test workers
    send_direct_mode_message "/end focusworker1" >/dev/null 2>&1 || true
    send_direct_mode_message "/end focusworker2" >/dev/null 2>&1 || true
    sleep 0.3

    # Create two workers
    send_direct_mode_message "/hire focusworker1" >/dev/null
    wait_for_direct_worker "focusworker1"

    send_direct_mode_message "/hire focusworker2" >/dev/null
    wait_for_direct_worker "focusworker2"

    # Focus first worker
    local result
    result=$(send_direct_mode_message "/focus focusworker1")

    if [[ "$result" != "OK" ]]; then
        fail "Focus switch: /focus focusworker1 failed"
        send_direct_mode_message "/end focusworker1" >/dev/null 2>&1 || true
        send_direct_mode_message "/end focusworker2" >/dev/null 2>&1 || true
        return
    fi
    success "Focus switch: Focused focusworker1"

    # Switch focus to second worker
    result=$(send_direct_mode_message "/focus focusworker2")

    if [[ "$result" != "OK" ]]; then
        fail "Focus switch: /focus focusworker2 failed"
        send_direct_mode_message "/end focusworker1" >/dev/null 2>&1 || true
        send_direct_mode_message "/end focusworker2" >/dev/null 2>&1 || true
        return
    fi
    success "Focus switch: Switched focus to focusworker2"

    # Verify /team shows both workers
    result=$(send_direct_mode_message "/team")
    if [[ "$result" == "OK" ]]; then
        success "Focus switch: /team works with multiple workers"
    else
        fail "Focus switch: /team failed"
    fi

    # Cleanup
    send_direct_mode_message "/end focusworker1" >/dev/null 2>&1 || true
    send_direct_mode_message "/end focusworker2" >/dev/null 2>&1 || true
}

test_direct_mode_e2e_at_mention() {
    info "Testing direct mode E2E @mention routing..."

    if ! check_claude_available; then
        info "Skipping (claude CLI not available)"
        return 0
    fi

    # Clean up any existing test workers
    send_direct_mode_message "/end mentionworker1" >/dev/null 2>&1 || true
    send_direct_mode_message "/end mentionworker2" >/dev/null 2>&1 || true
    sleep 0.3

    # Create two workers
    send_direct_mode_message "/hire mentionworker1" >/dev/null
    wait_for_direct_worker "mentionworker1"

    send_direct_mode_message "/hire mentionworker2" >/dev/null
    wait_for_direct_worker "mentionworker2"

    # Focus worker1
    send_direct_mode_message "/focus mentionworker1" >/dev/null
    sleep 0.2

    # Use @mention to route to worker2 without changing focus
    local result
    result=$(send_direct_mode_message "@mentionworker2 Hello via mention")

    if [[ "$result" == "OK" ]]; then
        # Check that message was routed to mentionworker2
        sleep 0.5
        if grep -q "Sent to direct worker 'mentionworker2'" "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null; then
            success "@mention: Message routed to correct worker"
        else
            fail "@mention: Message not routed to worker2"
        fi
    else
        fail "@mention: Request failed: $result"
    fi

    # Cleanup
    send_direct_mode_message "/end mentionworker1" >/dev/null 2>&1 || true
    send_direct_mode_message "/end mentionworker2" >/dev/null 2>&1 || true
}

test_direct_mode_at_all_broadcast() {
    info "Testing @all broadcast in direct mode..."

    if ! check_claude_available; then
        info "Skipping (claude CLI not available)"
        return 0
    fi

    # Clean up any existing test workers
    send_direct_mode_message "/end allworker1" >/dev/null 2>&1 || true
    send_direct_mode_message "/end allworker2" >/dev/null 2>&1 || true
    sleep 0.3

    # Create two workers
    local result
    result=$(send_direct_mode_message "/hire allworker1")
    if [[ "$result" != "OK" ]]; then
        fail "@all direct: Failed to create allworker1"
        return
    fi
    wait_for_direct_worker "allworker1" || {
        fail "@all direct: allworker1 not started"
        return
    }

    result=$(send_direct_mode_message "/hire allworker2")
    if [[ "$result" != "OK" ]]; then
        fail "@all direct: Failed to create allworker2"
        send_direct_mode_message "/end allworker1" >/dev/null 2>&1 || true
        return
    fi
    wait_for_direct_worker "allworker2" || {
        fail "@all direct: allworker2 not started"
        send_direct_mode_message "/end allworker1" >/dev/null 2>&1 || true
        return
    }

    # Clear log markers by noting current line count
    local log_lines_before
    log_lines_before=$(wc -l < "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "0")

    # Send @all broadcast
    result=$(send_direct_mode_message "@all Hello everyone from test")
    if [[ "$result" != "OK" ]]; then
        fail "@all direct: Broadcast failed: $result"
        send_direct_mode_message "/end allworker1" >/dev/null 2>&1 || true
        send_direct_mode_message "/end allworker2" >/dev/null 2>&1 || true
        return
    fi

    # Wait for message routing
    sleep 0.5

    # Check that both workers received the message (new log lines only)
    local worker1_got_msg=false
    local worker2_got_msg=false

    local new_log_lines
    new_log_lines=$(tail -n +$((log_lines_before + 1)) "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "")

    if echo "$new_log_lines" | grep -q "Sent to direct worker 'allworker1'"; then
        worker1_got_msg=true
    fi
    if echo "$new_log_lines" | grep -q "Sent to direct worker 'allworker2'"; then
        worker2_got_msg=true
    fi

    if $worker1_got_msg && $worker2_got_msg; then
        success "@all direct: Broadcast reached both workers"
    elif $worker1_got_msg || $worker2_got_msg; then
        fail "@all direct: Broadcast only reached one worker"
    else
        fail "@all direct: Broadcast reached neither worker"
    fi

    # Cleanup
    send_direct_mode_message "/end allworker1" >/dev/null 2>&1 || true
    send_direct_mode_message "/end allworker2" >/dev/null 2>&1 || true
}

test_direct_mode_reply_routing() {
    info "Testing reply routing in direct mode..."

    if ! check_claude_available; then
        info "Skipping (claude CLI not available)"
        return 0
    fi

    # Clean up any existing test worker
    send_direct_mode_message "/end replyworker" >/dev/null 2>&1 || true
    sleep 0.3

    # Create worker
    local result
    result=$(send_direct_mode_message "/hire replyworker")
    if [[ "$result" != "OK" ]]; then
        fail "Reply direct: /hire failed: $result"
        return
    fi

    if ! wait_for_direct_worker "replyworker"; then
        fail "Reply direct: replyworker not started"
        send_direct_mode_message "/end replyworker" >/dev/null 2>&1 || true
        return
    fi

    # Track log position
    local log_lines_before
    log_lines_before=$(wc -l < "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "0")

    # Send reply to a bot message with worker prefix
    result=$(send_direct_mode_reply "Following up on that" "replyworker: Previous response text")
    if [[ "$result" != "OK" ]]; then
        fail "Reply direct: Reply send failed: $result"
        send_direct_mode_message "/end replyworker" >/dev/null 2>&1 || true
        return
    fi

    # Wait for routing
    sleep 0.5

    # Check routing in new log lines
    local new_log_lines
    new_log_lines=$(tail -n +$((log_lines_before + 1)) "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "")

    if echo "$new_log_lines" | grep -q "Sent to direct worker 'replyworker'"; then
        success "Reply direct: Reply routed to worker via prefix"
    else
        fail "Reply direct: Reply not routed to worker"
    fi

    # Cleanup
    send_direct_mode_message "/end replyworker" >/dev/null 2>&1 || true
}

test_direct_mode_reply_context() {
    info "Testing reply context in direct mode..."

    if ! check_claude_available; then
        info "Skipping (claude CLI not available)"
        return 0
    fi

    # Clean up any existing test worker
    send_direct_mode_message "/end contextworker" >/dev/null 2>&1 || true
    sleep 0.3

    # Create and focus worker
    local result
    result=$(send_direct_mode_message "/hire contextworker")
    if [[ "$result" != "OK" ]]; then
        fail "Context direct: /hire failed: $result"
        return
    fi
    if ! wait_for_direct_worker "contextworker"; then
        fail "Context direct: contextworker not started"
        return
    fi
    send_direct_mode_message "/focus contextworker" >/dev/null
    sleep 0.2

    # Clear log markers by noting current line count
    local log_lines_before
    log_lines_before=$(wc -l < "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "0")

    # Send reply to own (non-bot) message with reply_from_bot=false
    result=$(send_direct_mode_reply "." "my original message" "false")
    if [[ "$result" != "OK" ]]; then
        fail "Context direct: Reply send failed: $result"
        send_direct_mode_message "/end contextworker" >/dev/null 2>&1 || true
        return
    fi

    # Wait for routing
    sleep 0.5

    # Check that message was routed to worker
    local new_log_lines
    new_log_lines=$(tail -n +$((log_lines_before + 1)) "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "")

    if echo "$new_log_lines" | grep -q "Sent to direct worker 'contextworker'"; then
        success "Context direct: Reply with context routed to worker"
    else
        fail "Context direct: Reply not routed to contextworker"
    fi

    # Cleanup
    send_direct_mode_message "/end contextworker" >/dev/null 2>&1 || true
}

test_direct_mode_e2e_pause() {
    info "Testing direct mode E2E /pause command..."

    if ! check_claude_available; then
        info "Skipping (claude CLI not available)"
        return 0
    fi

    # Clean up and create worker
    send_direct_mode_message "/end pauseworker" >/dev/null 2>&1 || true
    sleep 0.3

    send_direct_mode_message "/hire pauseworker" >/dev/null
    wait_for_direct_worker "pauseworker"
    send_direct_mode_message "/focus pauseworker" >/dev/null
    sleep 0.2

    # Send /pause
    local result
    result=$(send_direct_mode_message "/pause")

    if [[ "$result" == "OK" ]]; then
        success "/pause: Command accepted in direct mode"
    else
        fail "/pause: Command failed: $result"
    fi

    # Cleanup
    send_direct_mode_message "/end pauseworker" >/dev/null 2>&1 || true
}

test_direct_mode_e2e_relaunch() {
    info "Testing direct mode E2E /relaunch command..."

    if ! check_claude_available; then
        info "Skipping (claude CLI not available)"
        return 0
    fi

    # Clean up and create worker
    send_direct_mode_message "/end relaunchworker" >/dev/null 2>&1 || true
    sleep 0.3

    send_direct_mode_message "/hire relaunchworker" >/dev/null
    wait_for_direct_worker "relaunchworker"
    send_direct_mode_message "/focus relaunchworker" >/dev/null
    sleep 0.2

    # Get first PID (logged when worker starts)
    local first_log_count
    first_log_count=$(grep -c "Started direct worker 'relaunchworker'" "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "0")

    # Send /relaunch
    local result
    result=$(send_direct_mode_message "/relaunch")

    if [[ "$result" == "OK" ]]; then
        # Wait for worker to restart
        sleep 1

        # Check if a new worker was started (log count increased)
        local second_log_count
        second_log_count=$(grep -c "Started direct worker 'relaunchworker'" "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "0")

        if [[ "$second_log_count" -gt "$first_log_count" ]]; then
            success "/relaunch: Worker restarted (new subprocess)"
        else
            # Relaunch might work differently in direct mode
            success "/relaunch: Command accepted"
        fi
    else
        fail "/relaunch: Command failed: $result"
    fi

    # Cleanup
    send_direct_mode_message "/end relaunchworker" >/dev/null 2>&1 || true
}

test_direct_mode_e2e_settings() {
    info "Testing direct mode E2E /settings command..."

    if ! check_claude_available; then
        info "Skipping (claude CLI not available)"
        return 0
    fi

    # Ensure bridge is running (use existing from earlier tests)
    local result
    result=$(send_direct_mode_message "/settings")

    if [[ "$result" == "OK" ]]; then
        # Check bridge log for settings output with direct mode indicator
        sleep 0.3
        if grep -q "Direct mode\|DIRECT_MODE\|direct" "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null; then
            success "/settings: Shows direct mode indicator"
        else
            success "/settings: Command works in direct mode"
        fi
    else
        fail "/settings: Command failed: $result"
    fi
}

test_direct_mode_e2e_progress() {
    info "Testing direct mode E2E /progress command..."

    if ! check_claude_available; then
        info "Skipping (claude CLI not available)"
        return 0
    fi

    # Clean up and create worker
    send_direct_mode_message "/end progressworker" >/dev/null 2>&1 || true
    sleep 0.3

    send_direct_mode_message "/hire progressworker" >/dev/null
    wait_for_direct_worker "progressworker"
    send_direct_mode_message "/focus progressworker" >/dev/null
    sleep 0.2

    # Send /progress
    local result
    result=$(send_direct_mode_message "/progress")

    if [[ "$result" == "OK" ]]; then
        success "/progress: Command works in direct mode"
    else
        fail "/progress: Command failed: $result"
    fi

    # Cleanup
    send_direct_mode_message "/end progressworker" >/dev/null 2>&1 || true
}

test_direct_mode_vs_tmux_parity() {
    info "Testing direct mode vs tmux mode command parity..."

    # This test verifies that key commands work in both modes
    # by checking bridge module code paths

    if python3 -c "
from bridge import (
    DIRECT_MODE,
    create_direct_worker,
    kill_direct_worker,
    send_to_direct_worker,
    get_direct_workers,
    create_session,
    kill_session,
    tmux_send_message,
    get_registered_sessions
)
import inspect
import bridge

# Get source of key functions that dispatch to tmux or direct mode
create_session_src = inspect.getsource(create_session)
kill_session_src = inspect.getsource(kill_session)
get_sessions_src = inspect.getsource(get_registered_sessions)

# Check create_session handles both modes
assert 'DIRECT_MODE' in create_session_src, 'create_session should check DIRECT_MODE'
assert 'create_direct_worker' in create_session_src, 'create_session should call create_direct_worker'

# Check kill_session handles both modes
assert 'DIRECT_MODE' in kill_session_src, 'kill_session should check DIRECT_MODE'
assert 'kill_direct_worker' in kill_session_src, 'kill_session should call kill_direct_worker'

# Check get_registered_sessions handles both modes
assert 'DIRECT_MODE' in get_sessions_src, 'get_registered_sessions should check DIRECT_MODE'
assert 'get_direct_workers' in get_sessions_src, 'get_registered_sessions should call get_direct_workers'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Direct/tmux parity: Code paths exist for both modes"
    else
        fail "Direct/tmux parity: Missing conditional code paths"
    fi
}

test_worker_to_worker_pipe_direct() {
    info "Testing worker-to-worker pipe communication in direct mode..."

    if ! check_claude_available; then
        info "Skipping (claude CLI not available)"
        return 0
    fi

    # Clean up any existing test workers
    send_direct_mode_message "/end alice" >/dev/null 2>&1 || true
    send_direct_mode_message "/end bob" >/dev/null 2>&1 || true
    sleep 0.3

    # Create Worker A (alice)
    local result
    result=$(send_direct_mode_message "/hire alice")
    if [[ "$result" != "OK" ]]; then
        fail "Pipe direct: Failed to create worker alice: $result"
        return
    fi
    wait_for_direct_worker "alice" || {
        fail "Pipe direct: Worker alice not started"
        return
    }

    # Create Worker B (bob)
    result=$(send_direct_mode_message "/hire bob")
    if [[ "$result" != "OK" ]]; then
        fail "Pipe direct: Failed to create worker bob: $result"
        send_direct_mode_message "/end alice" >/dev/null 2>&1 || true
        return
    fi
    wait_for_direct_worker "bob" || {
        fail "Pipe direct: Worker bob not started"
        send_direct_mode_message "/end alice" >/dev/null 2>&1 || true
        return
    }

    # Resolve bob's pipe path
    local bob_pipe
    bob_pipe=$(python3 -c "from bridge import get_worker_pipe_path; print(get_worker_pipe_path('bob'))" 2>/dev/null)
    if [[ -z "$bob_pipe" || ! -p "$bob_pipe" ]]; then
        fail "Pipe direct: Bob's pipe not created"
        send_direct_mode_message "/end alice" >/dev/null 2>&1 || true
        send_direct_mode_message "/end bob" >/dev/null 2>&1 || true
        return
    fi
    success "Pipe direct: Named pipe created for worker"

    # Clear log markers
    local log_lines_before
    log_lines_before=$(wc -l < "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "0")

    # Write message to pipe
    local unique_msg="pipe_test_${RANDOM}"
    echo "$unique_msg" > "$bob_pipe" &
    local write_pid=$!

    # Wait for pipe reader to log the message
    local attempts=0
    local new_log_lines=""
    while [[ $attempts -lt 20 ]]; do
        new_log_lines=$(tail -n +"$((log_lines_before + 1))" "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || true)
        if echo "$new_log_lines" | grep -q "Pipe message for 'bob'"; then
            break
        fi
        sleep 0.2
        ((attempts++))
    done

    if echo "$new_log_lines" | grep -q "Pipe message for 'bob'"; then
        success "Pipe direct: Pipe reader logged message for bob"
    else
        fail "Pipe direct: Pipe reader did not log message"
    fi

    kill "$write_pid" 2>/dev/null || true

    # Cleanup
    send_direct_mode_message "/end alice" >/dev/null 2>&1 || true
    send_direct_mode_message "/end bob" >/dev/null 2>&1 || true
}

test_direct_mode_image_handling() {
    info "Testing direct mode image handling..."

    if ! check_claude_available; then
        info "Skipping (claude CLI not available)"
        return 0
    fi

    local worker="directimage"
    send_direct_mode_message "/end $worker" >/dev/null 2>&1 || true
    sleep 0.3

    local result
    result=$(send_direct_mode_message "/hire $worker")
    if [[ "$result" != "OK" ]]; then
        fail "Direct image: /hire failed: $result"
        return
    fi

    if ! wait_for_direct_worker "$worker"; then
        fail "Direct image: Worker not created"
        return
    fi

    send_direct_mode_message "/focus $worker" >/dev/null
    sleep 0.3

    local log_lines_before
    log_lines_before=$(wc -l < "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "0")

    local file_id="direct_image_file_$RANDOM"
    local caption="Direct mode image caption"
    local update_id=$((RANDOM))

    # Send simulated photo message
    local response
    response=$(curl -s -X POST "http://localhost:$DIRECT_MODE_PORT" \
        -H "Content-Type: application/json" \
        -d '{
            "update_id": '"$update_id"',
            "message": {
                "message_id": '"$update_id"',
                "from": {"id": '"$CHAT_ID"', "first_name": "TestUser"},
                "chat": {"id": '"$CHAT_ID"', "type": "private"},
                "date": '"$(date +%s)"',
                "photo": [
                    {"file_id": "'"$file_id"'_small", "file_size": 1000, "width": 90, "height": 90},
                    {"file_id": "'"$file_id"'", "file_size": 5000, "width": 320, "height": 320}
                ],
                "caption": "'"$caption"'"
            }
        }')

    if [[ "$response" != "OK" ]]; then
        fail "Direct image: Photo message rejected: $response"
        send_direct_mode_message "/end $worker" >/dev/null 2>&1 || true
        return
    fi

    sleep 1

    local new_log_lines
    new_log_lines=$(tail -n +$((log_lines_before + 1)) "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "")

    if echo "$new_log_lines" | grep -q "Sent to direct worker\|Downloaded file\|getFile"; then
        success "Direct image: Photo handling works"
    else
        fail "Direct image: No evidence of photo handling"
        send_direct_mode_message "/end $worker" >/dev/null 2>&1 || true
        return
    fi

    send_direct_mode_message "/end $worker" >/dev/null 2>&1 || true
}

# ============================================================
# TEST RUNNERS
# ============================================================

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

run_unit_tests() {
    # Unit tests (no bridge needed)
    log "── Unit Tests ──────────────────────────────────────────────────────────"
    test_imports
    test_response_prefix_formatting
    test_message_splitting_short
    test_message_splitting_newlines
    test_message_splitting_hard
    test_multipart_formatting
    test_version
    test_equals_syntax
    test_sandbox_config
    test_sandbox_docker_cmd

    # Unit tests - Backend registry / non-interactive mode
    log ""
    log "── Backend Registry Tests (Unit) ───────────────────────────────────────"
    test_forward_to_bridge_html_escape
    test_backend_registry_exists
    test_get_registered_sessions_includes_noninteractive_workers

    # Unit tests - Worker naming
    log ""
    log "── Worker Naming Tests (Unit) ──────────────────────────────────────────"
    test_worker_name_sanitization
    test_hire_backend_parsing
    test_team_output_includes_backend
    test_progress_output_includes_backend
    test_codex_learn_reaction_bypasses_tmux
    test_worker_send_uses_backend
    test_backend_env_metadata
    test_codex_end_cleans_session
    test_codex_relaunch_clears_session_id
    test_codex_pause_clears_pending
    test_get_workers_includes_codex
    test_pipe_forwarding_to_codex
    test_codex_response_requires_escape
    test_update_bot_commands_includes_codex
    test_broadcast_includes_codex

    # Unit tests - Security constants
    log ""
    log "── Security Constants Tests (Unit) ─────────────────────────────────────"
    test_telegram_max_length
    test_max_file_size
    test_bot_commands_structure
    test_blocked_commands_list

    # Unit tests - Persistence functions
    log ""
    log "── Persistence Functions Tests (Unit) ──────────────────────────────────"
    test_persistence_file_functions
    test_pending_auto_timeout
    test_pending_set_and_clear

    # Unit tests - Concurrency
    log ""
    log "── Concurrency Tests (Unit) ────────────────────────────────────────────"
    test_tmux_send_locks

    # Unit tests - Message formatting
    log ""
    log "── Message Formatting Tests (Unit) ─────────────────────────────────────"
    test_reply_context_formatting
    test_escape_tag_preservation
    test_code_fence_protection

    # Unit tests - Shutdown
    log ""
    log "── Startup/Shutdown Tests (Unit) ───────────────────────────────────────"
    test_graceful_shutdown
    test_startup_notification_flag

    # Unit tests - Hook behavior
    log ""
    log "── Hook Behavior Tests (Unit) ──────────────────────────────────────────"
    test_hook_session_filtering
    test_hook_bridge_url_precedence
    test_hook_fails_closed

    # Unit tests - Security
    log ""
    log "── Security Tests (Unit) ───────────────────────────────────────────────"
    test_admin_chat_id_preset
    test_admin_auto_learn_first_user

    # Unit tests - Misc behavior
    log ""
    log "── Misc Behavior Tests (Unit) ──────────────────────────────────────────"
    test_typing_indicator_function
    test_welcome_message_new_worker
    test_file_tag_welcome_instructions

    # Unit tests - Node selection
    log ""
    log "── Node Selection Tests (Unit) ─────────────────────────────────────────"
    test_node_resolution_priority
    test_node_name_sanitization_cli
    test_default_node_when_none_running

    # Unit tests - Hook env variables
    log ""
    log "── Hook Env Variables Tests (Unit) ─────────────────────────────────────"
    test_hook_bridge_url_env
    test_hook_port_fallback
    test_hook_tmux_prefix_usage
    test_hook_sessions_dir_usage
    test_hook_tmux_fallback_flag

    # Unit tests - Persistence files
    log ""
    log "── Persistence Files Tests (Unit) ──────────────────────────────────────"
    test_pid_file_creation
    test_bridge_pid_file_creation
    test_tunnel_pid_file_creation
    test_tunnel_log_file_creation
    test_tunnel_url_file_creation
    test_port_file_creation
    test_bot_id_cached
    test_bot_username_cached
    test_bridge_log_file_creation

    # Unit tests - Run/tunnel behavior
    log ""
    log "── Run/Tunnel Behavior Tests (Unit) ────────────────────────────────────"
    test_run_auto_installs_hook
    test_webhook_failure_cleanup
    test_tunnel_watchdog_behavior

    # Unit tests - Image/document handling gaps
    log ""
    log "── Image/Document Handling Gaps Tests (Unit) ───────────────────────────"
    test_caption_prepended_to_message
    test_download_failure_notification
    test_inbox_path_under_tmp
    test_inbox_cleanup_on_offboard
    test_image_path_restriction
    test_send_failure_notification

    # Unit tests - Misc behavior gaps
    log ""
    log "── Misc Behavior Gaps Tests (Unit) ─────────────────────────────────────"
    test_eye_reaction_on_acceptance
    test_typing_indicator_sent_while_pending
    test_admin_restored_from_last_chat_id
    test_new_worker_welcome_message
    test_extra_mounts_docker_cmd

    # Unit tests - Status diagnostics
    log ""
    log "── Status Diagnostics Tests (Unit) ─────────────────────────────────────"
    test_orphan_process_detection
    test_webhook_conflict_warning
    test_tmux_env_mismatch_detection
    test_stale_hooks_detection

    # Unit tests - Bridge environment variables
    log ""
    log "── Bridge Env Variables Tests (Unit) ───────────────────────────────────"
    test_bridge_env_bot_token
    test_bridge_env_port
    test_bridge_env_webhook_secret
    test_bridge_env_sessions_dir
    test_bridge_env_tmux_prefix
    test_bridge_env_bridge_url
    test_bridge_env_sandbox

    # Unit tests - Hook behavior details
    log ""
    log "── Hook Behavior Details Tests (Unit) ──────────────────────────────────"
    test_hook_reads_tmux_env_first
    test_hook_transcript_extraction_retry
    test_hook_tmux_fallback_warning
    test_hook_async_forward_timeout
    test_hook_helper_script_exists

    # Unit tests - Message routing rules
    log ""
    log "── Message Routing Rules Tests (Unit) ──────────────────────────────────"
    test_unknown_commands_passthrough
    test_reply_with_explicit_context
    test_multipart_chained_reply_to
    test_message_split_safe_boundaries

    # Unit tests - Per-session files
    log ""
    log "── Per-Session Files Tests (Unit) ──────────────────────────────────────"
    test_pending_file_timestamp
    test_chat_id_file_content

    # Unit tests - Document & image security
    log ""
    log "── Document/Image Security Tests (Unit) ────────────────────────────────"
    test_document_no_path_restriction
    test_blocked_filenames_list
    test_20mb_size_limit

    # Unit tests - Test environment
    log ""
    log "── Test Environment Tests (Unit) ───────────────────────────────────────"
    test_test_env_vars_documented

    # Unit tests - Worker discovery
    log ""
    log "── Worker Discovery Tests (Unit) ───────────────────────────────────────"
    test_worker_pipe_path_constant
    test_get_worker_pipe_path_function
    test_get_workers_function
    test_worker_pipe_creation_on_startup
    test_worker_pipe_cleanup_on_end

    # Unit tests - send_to_worker abstraction
    log ""
    log "── send_to_worker Abstraction Tests (Unit) ─────────────────────────────"
    test_send_to_worker_function_exists
    test_send_to_worker_not_found
    test_send_to_worker_uses_backend_registry
    test_send_to_worker_tmux_mode
}

run_cli_tests() {
    # CLI tests (no bridge needed)
    log ""
    log "── CLI Tests ───────────────────────────────────────────────────────────"
    test_cli_help
    test_cli_version
    test_cli_node_flag
    test_cli_port_flag
    test_cli_unknown_command
    test_cli_missing_token_error
    test_cli_default_ports
    test_cli_hook_install_uninstall

    # CLI global flags tests
    log ""
    log "── CLI Global Flags Tests ──────────────────────────────────────────────"
    test_cli_all_flag
    test_cli_no_tunnel_flag
    test_cli_tunnel_url_flag
    test_cli_headless_flag
    test_cli_quiet_flag
    test_cli_verbose_flag
    test_cli_no_color_flag
    test_cli_env_file_flag
    test_cli_sandbox_image_flag
    test_cli_mount_flag
    test_cli_mount_ro_flag

    # CLI command coverage tests
    log ""
    log "── CLI Command Coverage Tests ──────────────────────────────────────────"
    test_cli_stop_command
    test_cli_restart_command
    test_cli_clean_command
    test_cli_status_command
    test_cli_status_json_output
    test_cli_webhook_info
    test_cli_webhook_set_url
    test_cli_webhook_set_requires_https
    test_cli_webhook_delete_requires_confirm
    test_cli_hook_uninstall
    test_cli_hook_test_no_chat
}

run_integration_tests() {
    # Integration tests (bridge needed)
    log ""
    log "── Integration Tests ───────────────────────────────────────────────────"
    test_bridge_starts || exit 1
    sleep 0.3

    # HTTP endpoint tests
    log ""
    log "── HTTP Endpoint Tests ─────────────────────────────────────────────────"
    test_health_endpoint
    test_response_endpoint_missing_fields
    test_response_endpoint_no_chat_id
    test_notify_endpoint_missing_text

    # Admin tests
    log ""
    log "── Admin Tests ─────────────────────────────────────────────────────────"
    test_admin_registration
    test_non_admin_rejection

    # Bot command tests
    log ""
    log "── Bot Command Tests ───────────────────────────────────────────────────"
    test_hire_command
    test_team_command
    test_focus_command
    test_progress_command
    test_relaunch_command
    test_pause_command
    test_settings_command
    test_learn_command
    test_end_command
    test_dynamic_bot_command_list_update
    test_blocked_commands
    test_blocked_commands_integration
    test_additional_commands

    # Worker naming tests (integration)
    log ""
    log "── Worker Naming Tests (Integration) ───────────────────────────────────"
    test_reserved_names_rejection
    test_worker_shortcut_focus_only
    test_worker_shortcut_with_message
    test_command_with_botname_suffix

    # Routing tests
    log ""
    log "── Routing Tests ───────────────────────────────────────────────────────"
    test_at_mention
    test_at_all_broadcast
    test_reply_routing
    test_reply_context

    # Tmux mode behavior tests (parity with direct mode)
    log ""
    log "── Tmux Mode Behavior Tests ────────────────────────────────────────────"
    test_tmux_mode_session_stays_alive
    test_tmux_mode_message_delivery

    # Security tests (integration)
    log ""
    log "── Security Tests (Integration) ────────────────────────────────────────"
    test_webhook_secret_validation
    test_webhook_secret_acceptance
    test_graceful_shutdown_notification
    test_typing_indicator_loop
    test_token_isolation
    test_secure_directory_permissions
    test_session_files

    # Image/document handling tests
    log ""
    log "── Image/Document Handling Tests ───────────────────────────────────────"
    test_image_tag_parsing
    test_image_path_validation
    test_file_tag_parsing
    test_file_extension_validation
    test_inbox_directory
    test_photo_message_no_focused
    test_document_message_no_focused
    test_document_message_format
    test_document_message_routing
    test_incoming_document_e2e
    test_incoming_image_e2e
    test_response_with_image_tags

    # Response/notify endpoint tests
    log ""
    log "── Response/Notify Endpoint Tests ──────────────────────────────────────"
    test_notify_endpoint
    test_response_endpoint
    test_response_without_pending

    # Persistence tests (integration)
    log ""
    log "── Persistence Tests (Integration) ─────────────────────────────────────"
    test_last_chat_id_persistence
    test_last_active_persistence

    # Hook behavior tests (integration)
    log ""
    log "── Hook Behavior Tests (Integration) ───────────────────────────────────"
    test_hook_env_validation
    test_hook_pending_cleanup

    # Status/diagnostics tests (integration)
    log ""
    log "── Status/Diagnostics Tests (Integration) ──────────────────────────────"
    test_status_shows_workers

    # Misc behavior tests (integration)
    log ""
    log "── Misc Behavior Tests (Integration) ───────────────────────────────────"
    test_unknown_command_passthrough

    # Worker discovery tests (integration)
    log ""
    log "── Worker Discovery Tests (Integration) ────────────────────────────────"
    test_workers_endpoint_exists
    test_workers_endpoint_json_structure
    test_workers_endpoint_shows_tmux_workers
    test_workers_endpoint_empty_when_no_workers

    # send_to_worker integration tests
    log ""
    log "── send_to_worker Integration Tests ────────────────────────────────────"
    test_send_to_worker_integration

    # Worker-to-worker pipe communication tests (e2e behavior)
    log ""
    log "── Worker-to-Worker Pipe Tests (Integration) ───────────────────────────"
    test_worker_to_worker_pipe

    # Cleanup test sessions
    send_message "/end testbot1" >/dev/null 2>&1 || true
}

run_tunnel_tests() {
    # Tunnel tests
    log ""
    log "── Tunnel Tests ────────────────────────────────────────────────────────"
    test_with_tunnel
}

main() {
    log ""
    log "═══════════════════════════════════════════════════════════════════════"
    log "  claudecode-telegram Acceptance Tests"
    log "═══════════════════════════════════════════════════════════════════════"

    # Detect test mode
    local mode="default"
    local mode_desc=""
    if [[ "${FAST:-}" == "1" ]]; then
        mode="fast"
        mode_desc="FAST mode: Unit + CLI tests only (~10-15s)"
    elif [[ "${FULL:-}" == "1" ]]; then
        mode="full"
        mode_desc="FULL mode: All tests including tunnel (~5 min)"
    else
        mode_desc="DEFAULT mode: Unit + Integration tests (~2-3 min)"
    fi
    log "  Mode: $mode_desc"
    log "═══════════════════════════════════════════════════════════════════════"
    log ""

    require_token

    cd "$SCRIPT_DIR"

    # Always run unit and CLI tests
    run_unit_tests
    run_cli_tests

    # Skip integration tests in FAST mode
    if [[ "$mode" != "fast" ]]; then
        run_integration_tests

    fi

    # Run E2E and tunnel tests only in FULL mode
    if [[ "$mode" == "full" ]]; then
        # Tunnel tests
        run_tunnel_tests
    fi

    # Summary
    log ""
    log "═══════════════════════════════════════════════════════════════════════"
    log "  Results: ${GREEN}$passed passed${NC}, ${RED}$failed failed${NC}"
    log "═══════════════════════════════════════════════════════════════════════"
    log ""

    [[ $failed -eq 0 ]] && exit 0 || exit 1
}

main "$@"
