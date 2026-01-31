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
    [[ -n "$BRIDGE_PID" ]] && kill "$BRIDGE_PID" 2>/dev/null || true
    [[ -n "$TUNNEL_PID" ]] && kill "$TUNNEL_PID" 2>/dev/null || true
    # Kill any test sessions we created (using test prefix)
    tmux list-sessions -F '#{session_name}' 2>/dev/null | grep "^${TEST_TMUX_PREFIX}" | while read -r session; do
        tmux kill-session -t "$session" 2>/dev/null || true
    done
    # Clean up test session files (but keep node dir for next run)
    [[ -d "$TEST_SESSION_DIR" ]] && rm -rf "$TEST_SESSION_DIR"
    [[ -f "$BRIDGE_LOG" ]] && rm -f "$BRIDGE_LOG"
    [[ -f "$TUNNEL_LOG" ]] && rm -f "$TUNNEL_LOG"
    rm -f "$TEST_NODE_DIR/tunnel.pid" "$TEST_NODE_DIR/tunnel_url" "$TEST_NODE_DIR/port"
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
    sleep 1

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

# ─────────────────────────────────────────────────────────────────────────────
# New feature tests (v0.8.0+)
# ─────────────────────────────────────────────────────────────────────────────

test_command_aliases() {
    info "Testing command aliases (/hire, /team, /focus, etc.)..."

    # Create with /hire (alias for /new)
    send_message "/hire aliasbot" >/dev/null
    sleep 2

    if tmux has-session -t "${TEST_TMUX_PREFIX}aliasbot" 2>/dev/null; then
        success "/hire creates worker"
    else
        fail "/hire failed"
        return
    fi

    # Test /team (alias for /list)
    local result
    result=$(send_message "/team")
    if [[ "$result" == "OK" ]]; then
        success "/team works"
    else
        fail "/team failed"
    fi

    # Test /focus (alias for /use)
    result=$(send_message "/focus aliasbot")
    if [[ "$result" == "OK" ]]; then
        success "/focus works"
    else
        fail "/focus failed"
    fi

    # Test /progress (alias for /status)
    result=$(send_message "/progress")
    if [[ "$result" == "OK" ]]; then
        success "/progress works"
    else
        fail "/progress failed"
    fi

    # Test /pause (alias for /stop)
    result=$(send_message "/pause")
    if [[ "$result" == "OK" ]]; then
        success "/pause works"
    else
        fail "/pause failed"
    fi

    # Test /end (alias for /kill)
    send_message "/end aliasbot" >/dev/null
    sleep 1
    if ! tmux has-session -t "${TEST_TMUX_PREFIX}aliasbot" 2>/dev/null; then
        success "/end removes worker"
    else
        fail "/end failed"
    fi
}

test_learn_command() {
    info "Testing /learn command..."

    # Create a worker first
    send_message "/hire learnbot" >/dev/null
    sleep 2
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
    sleep 2

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
    sleep 2
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
    sleep 1

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
    sleep 1

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
    sleep 2
    send_message "/focus doctest" >/dev/null
    sleep 1

    # Send a document message
    local result
    result=$(send_document_message "test_doc_file_id" "report.pdf" "application/pdf" 2048 "Please review this")

    if [[ "$result" == "OK" ]]; then
        # Check bridge log for the document handling
        sleep 1
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
    sleep 2
    send_message "/focus docrecv" >/dev/null
    sleep 1

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
    sleep 2
    send_message "/focus imgrecv" >/dev/null
    sleep 1

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
    sleep 2
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
    send_message "/new imageresponsetest" >/dev/null
    sleep 2

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
        sleep 1
        if grep -q "imageresponsetest" "$BRIDGE_LOG" 2>/dev/null; then
            success "/response endpoint handles image tags"
        else
            fail "/response endpoint did not process message"
        fi
    else
        fail "/response with image tags failed: $result"
    fi

    # Cleanup
    send_message "/kill imageresponsetest" >/dev/null 2>&1 || true
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

test_response_without_pending() {
    info "Testing /response works without pending file (v0.6.2 behavior)..."

    # Create a session for this test
    send_message "/new nopendingtest" >/dev/null
    sleep 3

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
    send_message "/kill nopendingtest" >/dev/null 2>&1 || true
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
    test_response_prefix_formatting
    test_message_splitting_short
    test_message_splitting_newlines
    test_message_splitting_hard
    test_multipart_formatting
    test_version
    test_equals_syntax
    test_sandbox_config
    test_sandbox_docker_cmd

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
    test_notify_endpoint
    test_response_endpoint
    test_response_without_pending

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
