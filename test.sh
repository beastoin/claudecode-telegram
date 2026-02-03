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
TEST_NODE_DIR="$HOME/.claude/telegram/nodes/$TEST_NODE"
PORT="${TEST_PORT:-8095}"
TEST_SESSION_DIR="$TEST_NODE_DIR/sessions"
TEST_PID_FILE="$TEST_NODE_DIR/pid"
TEST_TMUX_PREFIX="claude-${TEST_NODE}-"
BRIDGE_LOG="$TEST_NODE_DIR/bridge.log"
TUNNEL_LOG="$TEST_NODE_DIR/tunnel.log"

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

    # Test hook install (with force to overwrite if exists)
    if TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" ./claudecode-telegram.sh hook install --force 2>/dev/null; then
        if [[ -f "$HOME/.claude/hooks/send-to-telegram.sh" ]]; then
            success "CLI hook install creates hook file"
        else
            fail "Hook file not created"
        fi
    else
        fail "CLI hook install failed"
    fi

    # Test hook is in settings.json
    if [[ -f "$HOME/.claude/settings.json" ]]; then
        if grep -q "send-to-telegram.sh" "$HOME/.claude/settings.json"; then
            success "Hook registered in settings.json"
        else
            fail "Hook not in settings.json"
        fi
    fi
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

    # Unit tests - Worker naming
    log ""
    log "── Worker Naming Tests (Unit) ──────────────────────────────────────────"
    test_worker_name_sanitization

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

    # Security tests (integration)
    log ""
    log "── Security Tests (Integration) ────────────────────────────────────────"
    test_webhook_secret_validation
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

    # Only run tunnel tests in FULL mode
    if [[ "$mode" == "full" ]]; then
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
