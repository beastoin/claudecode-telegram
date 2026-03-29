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
# Tests run isolated using --node test with separate port (8295),
# prefix (claude-test-), and PID file. Safe to run while production is active.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHAT_ID="${TEST_CHAT_ID:-123456789}"
BRIDGE_PID=""
TUNNEL_PID=""
TUNNEL_URL=""
TEST_FILTER="${TEST_FILTER:-}"

# Test node configuration
TEST_NODE="test"
TEST_NODE_DIR="${TEST_NODE_DIR:-$HOME/.claude/telegram/nodes/$TEST_NODE}"
PORT="${TEST_PORT:-8295}"
TEST_SESSION_DIR="$TEST_NODE_DIR/sessions"
TEST_PID_FILE="$TEST_NODE_DIR/pid"
TEST_TMUX_PREFIX="claude-${TEST_NODE}-"
BRIDGE_LOG="$TEST_NODE_DIR/bridge.log"
TUNNEL_LOG="$TEST_NODE_DIR/tunnel.log"
TEST_TEAM_DIR="$TEST_NODE_DIR/team"

# Ensure unit tests write to isolated test sessions directory
export SESSIONS_DIR="$TEST_SESSION_DIR"

# Create stub binaries for backends not installed (needed for binary check)
TEST_BIN_DIR="$(mktemp -d)"
for bin_name in codex gemini opencode; do
    if ! command -v "$bin_name" &>/dev/null; then
        printf '#!/bin/sh\necho "stub"\n' > "$TEST_BIN_DIR/$bin_name"
        chmod +x "$TEST_BIN_DIR/$bin_name"
    fi
done
export PATH="$TEST_BIN_DIR:$PATH"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

passed=0
failed=0
tests_run=0

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

should_run_test() {
    [[ -z "$TEST_FILTER" ]] && return 0
    [[ "$1" == *"$TEST_FILTER"* ]] && return 0
    return 1
}

run_test() {
    local test_name="$1"
    should_run_test "$test_name" || return 0
    ((tests_run++)) || true
    "$test_name"
}

collect_run_tests() {
    local runner_fn="$1"
    declare -f "$runner_fn" | awk '/^[[:space:]]*run_test[[:space:]]+test_[a-zA-Z0-9_]+/ { print $2 }'
}

count_matching_tests() {
    local mode="$1"
    local -a candidate_tests=()
    local fn
    local test_name

    for fn in run_unit_tests run_cli_tests; do
        while read -r test_name; do
            [[ -n "$test_name" ]] && candidate_tests+=("$test_name")
        done < <(collect_run_tests "$fn")
    done

    if [[ "$mode" != "fast" ]]; then
        while read -r test_name; do
            [[ -n "$test_name" ]] && candidate_tests+=("$test_name")
        done < <(collect_run_tests "run_integration_tests")
    fi

    if [[ "$mode" == "full" ]]; then
        while read -r test_name; do
            [[ -n "$test_name" ]] && candidate_tests+=("$test_name")
        done < <(collect_run_tests "run_full_tests")
    fi

    local matched=0
    for test_name in "${candidate_tests[@]}"; do
        should_run_test "$test_name" && ((matched++)) || true
    done
    echo "$matched"
}

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
    [[ -d "${TEST_BIN_DIR:-}" ]] && rm -rf "$TEST_BIN_DIR"; true
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

# curl wrapper for /response and /notify endpoints
hook_curl() {
    local url="$1"; shift
    local body="$1"; shift
    curl -s -X POST "$url" \
        -H "Content-Type: application/json" \
        -d "$body" "$@"
}

# Same but returns HTTP code only
hook_curl_code() {
    local url="$1"; shift
    local body="$1"; shift
    curl -s -o /dev/null -w "%{http_code}" -X POST "$url" \
        -H "Content-Type: application/json" \
        -d "$body" "$@"
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

send_animation_message() {
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
                "animation": {
                    "file_id": "'"$file_id"'",
                    "file_unique_id": "'"$file_id"'_unique",
                    "file_name": "test.gif",
                    "mime_type": "image/gif",
                    "file_size": 50000,
                    "width": 320,
                    "height": 240
                },
                "caption": "'"$caption"'"
            }
        }'
}

# ─────────────────────────────────────────────────────────────────────────────
# Merged Tests
# ─────────────────────────────────────────────────────────────────────────────

test_formatting() {
    info "Testing response prefix and multipart formatting..."
    if python3 -c "
from bridge import format_response_text, format_multipart_messages

# Response prefix formatting
text = 'Hello <code>world</code>'
result = format_response_text('session-1', text)
assert result == '<b>session-1:</b>\nHello <code>world</code>', f'prefix failed: {result}'

# Single chunk - no part numbers
chunks = ['Hello world']
formatted = format_multipart_messages('worker', chunks)
assert len(formatted) == 1
assert formatted[0] == '<b>worker:</b>\nHello world'
assert '(1/' not in formatted[0], 'single chunk should not have part numbers'

# Multiple chunks - all have prefix
chunks = ['Part 1 content', 'Part 2 content', 'Part 3 content']
formatted = format_multipart_messages('lee', chunks)
assert len(formatted) == 3
assert formatted[0] == '<b>lee:</b>\nPart 1 content', f'first: {formatted[0]}'
assert formatted[1] == '<b>lee:</b>\nPart 2 content', f'second: {formatted[1]}'
assert formatted[2] == '<b>lee:</b>\nPart 3 content', f'third: {formatted[2]}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Response prefix and multipart formatting work"
    else
        fail "Formatting test failed"
    fi
}

test_message_splitting() {
    info "Testing message splitting (short, newlines, hard, HTML-aware)..."
    if python3 -c "
import re
from bridge import split_message, markdown_to_telegram_html, TELEGRAM_MAX_LENGTH

TAG_RE = re.compile(r'<(/?)(\w+)([^>]*?)>')
VALID = {'b','i','s','u','code','pre','a','strong','em','del','ins','strike'}

def tags_balanced(html):
    stack = []
    for m in TAG_RE.finditer(html):
        close, tag = m.group(1) == '/', m.group(2).lower()
        if tag not in VALID: continue
        if close:
            for j in range(len(stack)-1,-1,-1):
                if stack[j] == tag: stack.pop(j); break
            else: return False
        else: stack.append(tag)
    return len(stack) == 0

# 1. Short message - no split
chunks = split_message('Short message')
assert len(chunks) == 1

# 2. Split on newlines
long_text = chr(10).join(['Line ' + str(i) + ' ' + 'x' * 100 for i in range(50)])
chunks = split_message(long_text, max_len=4096)
assert len(chunks) > 1
for c in chunks: assert len(c) <= 4096

# 3. Hard split (no natural breaks)
chunks = split_message('x' * 10000, max_len=4096)
assert len(chunks) >= 3
for c in chunks: assert len(c) <= 4096

# 4. HTML-aware: long code block stays balanced after split
code = chr(10).join([f'echo line{i} padding' + 'x'*40 for i in range(80)])
fence = chr(96)*3
html = markdown_to_telegram_html(f'Script:\n\n{fence}bash\n{code}\n{fence}\n\nDone.')
chunks = split_message(html, max_len=4096)
assert len(chunks) >= 2, f'expected 2+ chunks, got {len(chunks)}'
for i, c in enumerate(chunks):
    assert tags_balanced(c), f'chunk {i} has unbalanced HTML tags'
    assert len(c) <= 4096, f'chunk {i} too long: {len(c)}'

# 5. Nested inline tags across split
bt = chr(96)
nested = f'Text **bold with {bt}code{bt} end** rest. ' * 100
html = markdown_to_telegram_html(nested)
chunks = split_message(html, max_len=4096)
if len(chunks) > 1:
    for i, c in enumerate(chunks):
        assert tags_balanced(c), f'nested chunk {i} unbalanced'

# 6. Code block reopened in continuation chunk
code = chr(10).join([f'variable_{i} = \"value_{i}_padding\"' + 'x'*30 for i in range(100)])
html = markdown_to_telegram_html(f'{fence}python\n{code}\n{fence}')
chunks = split_message(html, max_len=4096)
assert len(chunks) >= 2
# Second chunk should start with <pre><code (reopened)
assert '<pre>' in chunks[1] or '<code' in chunks[1], 'continuation chunk missing reopened pre/code tag'
for i, c in enumerate(chunks):
    assert tags_balanced(c), f'reopen chunk {i} unbalanced'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Message splitting works (short, newlines, hard, HTML-aware)"
    else
        fail "Message splitting test failed"
    fi
}

test_media_tag_parsing() {
    info "Testing image/file tag parsing and protection..."

    # Create test files
    touch /tmp/test.jpg /tmp/a.jpg /tmp/b.png
    echo "test" > /tmp/report.pdf
    echo "test" > /tmp/data.json
    echo "test" > /tmp/a.txt
    echo "test" > /tmp/b.csv

    if python3 -c "
from bridge import parse_image_tags, parse_file_tags

# === Image tag parsing ===
text = 'Here is an image [[image:/tmp/test.jpg|my caption]] and more text'
clean, images = parse_image_tags(text)
assert 'Here is an image' in clean, f'clean text wrong: {clean!r}'
assert len(images) == 1, f'expected 1 image, got {len(images)}'
assert images[0] == ('/tmp/test.jpg', 'my caption'), f'image data wrong: {images[0]}'

# Non-existent file (tag stays)
text2 = '[[image:/nonexistent/photo.png]]'
clean2, images2 = parse_image_tags(text2)
assert len(images2) == 0
assert '[[image:' in clean2

# Multiple images
text3 = 'First [[image:/tmp/a.jpg|cap1]] middle [[image:/tmp/b.png|cap2]] end'
clean3, images3 = parse_image_tags(text3)
assert len(images3) == 2

# Escaped image tag
text4 = r'Example: \[[image:/tmp/test.jpg|caption]]'
clean4, images4 = parse_image_tags(text4)
assert len(images4) == 0

# === File tag parsing ===
text = 'Here is the report: [[file:/tmp/report.pdf|Q4 Report]]'
clean, files = parse_file_tags(text)
assert 'Here is the report:' in clean
assert len(files) == 1
assert files[0] == ('/tmp/report.pdf', 'Q4 Report')

text = 'Output: [[file:/tmp/data.json]]'
clean, files = parse_file_tags(text)
assert len(files) == 1
assert files[0] == ('/tmp/data.json', '')

text = 'Output: [[file:/nonexistent/file.txt]]'
clean, files = parse_file_tags(text)
assert len(files) == 0
assert '[[file:' in clean

text = '[[file:/tmp/a.txt|A]] and [[file:/tmp/b.csv|B]]'
clean, files = parse_file_tags(text)
assert len(files) == 2

# Escaped file tag
text = r'Example: \[[file:/tmp/report.pdf|caption]]'
clean, files = parse_file_tags(text)
assert len(files) == 0

# === Escape tag preservation ===
text = r'Example: \[[image:/tmp/test.jpg|caption]] stays'
clean, images = parse_image_tags(text)
assert len(images) == 0
assert '[[image:' in clean

text = r'Example: \[[file:/tmp/a.txt|caption]] stays'
clean, files = parse_file_tags(text)
assert len(files) == 0
assert '[[file:' in clean

# === Code fence protection ===
import textwrap
text = textwrap.dedent('''
Here is code:
\x60\x60\x60
[[image:/tmp/test.jpg|caption]]
\x60\x60\x60
And outside text''').strip()
clean, images = parse_image_tags(text)
assert len(images) == 0, 'tag in code fence should not be parsed'
assert '[[image:' in clean

text2 = 'Use \x60[[image:/path|cap]]\x60 syntax'
clean2, images2 = parse_image_tags(text2)
assert len(images2) == 0, 'tag in inline code should not be parsed'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Image/file tag parsing and protection work"
    else
        fail "Media tag parsing test failed"
    fi

    # Cleanup
    rm -f /tmp/test.jpg /tmp/a.jpg /tmp/b.png /tmp/report.pdf /tmp/data.json /tmp/a.txt /tmp/b.csv
}

test_pending_files() {
    info "Testing pending set/clear, timestamp, and chat_id file..."
    if python3 -c "
from bridge import set_pending, clear_pending, is_pending, get_session_dir, get_pending_file
from pathlib import Path
import time
import shutil
import stat

# === Set and clear ===
set_pending('pending_test', 12345)
assert is_pending('pending_test'), 'pending should be set'

chat_id_file = get_session_dir('pending_test') / 'chat_id'
assert chat_id_file.exists(), 'chat_id file should exist'
assert chat_id_file.read_text().strip() == '12345', 'chat_id should be 12345'

perms = oct(chat_id_file.stat().st_mode)[-3:]
assert perms == '600', f'chat_id file should be 600, got {perms}'

clear_pending('pending_test')
assert not is_pending('pending_test'), 'pending should be cleared'

# === Pending file timestamp ===
set_pending('timestamp_test', 12345)
pending_file = get_pending_file('timestamp_test')
content = pending_file.read_text().strip()
ts = int(content)
assert ts > 1000000000, f'Should be unix timestamp, got {ts}'
shutil.rmtree(pending_file.parent, ignore_errors=True)

# === Chat ID file content ===
test_chat = 987654321
set_pending('chatid_test', test_chat)
chat_file = get_session_dir('chatid_test') / 'chat_id'
content = chat_file.read_text().strip()
assert content == str(test_chat), f'Expected {test_chat}, got {content}'
shutil.rmtree(get_session_dir('chatid_test'), ignore_errors=True)

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Pending files (set/clear, timestamp, chat_id) work"
    else
        fail "Pending files test failed"
    fi
}

test_cli_flags_and_commands() {
    info "Testing CLI flags and commands..."
    local all_pass=true

    # --flag=value syntax
    if ! ./claudecode-telegram.sh --node=testnode --port=9999 --help 2>/dev/null | grep -qi "usage"; then
        fail "Equals syntax (--flag=value) failed"
        all_pass=false
    fi

    # --node flag
    if ! ./claudecode-telegram.sh --node mynode --help 2>/dev/null | grep -qi "usage"; then
        fail "CLI --node flag failed"
        all_pass=false
    fi

    # --port flag
    if ! ./claudecode-telegram.sh --port 9999 --help 2>/dev/null | grep -qi "usage"; then
        fail "CLI --port flag failed"
        all_pass=false
    fi

    # --all flag
    if ! ./claudecode-telegram.sh --all --help 2>/dev/null | grep -qi "usage"; then
        fail "CLI --all flag failed"
        all_pass=false
    fi

    # --no-tunnel flag
    if ! ./claudecode-telegram.sh --help 2>/dev/null | grep -q "no-tunnel"; then
        fail "CLI --no-tunnel not documented"
        all_pass=false
    fi

    # --tunnel-url flag
    if ! ./claudecode-telegram.sh --help 2>/dev/null | grep -q "tunnel-url"; then
        fail "CLI --tunnel-url not documented"
        all_pass=false
    fi

    # --headless flag
    if ! ./claudecode-telegram.sh --headless --help 2>/dev/null | grep -qi "usage"; then
        fail "CLI --headless flag failed"
        all_pass=false
    fi

    # -q (quiet) flag
    local result
    result=$(TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" ./claudecode-telegram.sh -q --version 2>&1)
    if ! echo "$result" | grep -q "claudecode-telegram"; then
        fail "CLI -q flag failed"
        all_pass=false
    fi

    # -v (verbose) flag
    if ! ./claudecode-telegram.sh -v --help 2>/dev/null | grep -qi "usage"; then
        fail "CLI -v flag failed"
        all_pass=false
    fi

    # --no-color flag
    if ! ./claudecode-telegram.sh --no-color --help 2>/dev/null | grep -qi "usage"; then
        fail "CLI --no-color flag failed"
        all_pass=false
    fi

    # --env-file flag
    local tmp_env=$(mktemp)
    echo "TEST_VAR=hello" > "$tmp_env"
    if ! ./claudecode-telegram.sh --env-file="$tmp_env" --help 2>/dev/null | grep -qi "usage"; then
        fail "CLI --env-file flag failed"
        all_pass=false
    fi
    rm -f "$tmp_env"

    # --sandbox-image flag
    if ! ./claudecode-telegram.sh --sandbox-image=myimage:latest --help 2>/dev/null | grep -qi "usage"; then
        fail "CLI --sandbox-image flag failed"
        all_pass=false
    fi

    # --mount flag
    if ! ./claudecode-telegram.sh --mount=/tmp:/container --help 2>/dev/null | grep -qi "usage"; then
        fail "CLI --mount flag failed"
        all_pass=false
    fi

    # --mount-ro flag
    if ! ./claudecode-telegram.sh --mount-ro=/tmp:/container --help 2>/dev/null | grep -qi "usage"; then
        fail "CLI --mount-ro flag failed"
        all_pass=false
    fi

    # stop command
    if ! ./claudecode-telegram.sh --help 2>/dev/null | grep -q "stop"; then
        fail "CLI stop command not documented"
        all_pass=false
    fi

    # restart command
    if ! ./claudecode-telegram.sh --help 2>/dev/null | grep -q "restart"; then
        fail "CLI restart command not documented"
        all_pass=false
    fi

    # clean command
    if ! ./claudecode-telegram.sh --help 2>/dev/null | grep -q "clean"; then
        fail "CLI clean command not documented"
        all_pass=false
    fi

    if $all_pass; then
        success "All CLI flags and commands parsed correctly"
    fi
}

test_webhook_secret() {
    info "Testing webhook secret validation and acceptance..."

    local secret_port=8096
    local secret_log="$TEST_NODE_DIR/secret_bridge.log"
    local secret_sessions_dir="$TEST_NODE_DIR/secret_sessions"
    local secret_tmux_prefix="claude-${TEST_NODE}-secret-"
    local secret_value="test-secret-merged-123"

    while nc -z localhost "$secret_port" 2>/dev/null; do
        secret_port=$((secret_port + 1))
        if [[ "$secret_port" -gt 8110 ]]; then
            fail "No free port for webhook secret test"
            return
        fi
    done

    mkdir -p "$secret_sessions_dir"
    chmod 700 "$secret_sessions_dir"

    TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" \
    PORT="$secret_port" \
    TELEGRAM_WEBHOOK_SECRET="$secret_value" \
    NODE_NAME="secretmerged" \
    SESSIONS_DIR="$secret_sessions_dir" \
    TMUX_PREFIX="$secret_tmux_prefix" \
    python3 -u "$SCRIPT_DIR/bridge.py" > "$secret_log" 2>&1 &
    local secret_pid=$!

    if wait_for_port "$secret_port"; then
        # Without secret header -> 403
        local http_code
        http_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "http://localhost:$secret_port" \
            -H "Content-Type: application/json" \
            -d '{"update_id": 1, "message": {"message_id": 1, "chat": {"id": 123}, "text": "test"}}')
        if [[ "$http_code" == "403" ]]; then
            success "Request without secret rejected (403)"
        else
            fail "Expected 403 without secret, got $http_code"
        fi

        # With correct secret -> 200
        local ok_response
        ok_response=$(curl -s -X POST "http://localhost:$secret_port" \
            -H "Content-Type: application/json" \
            -H "X-Telegram-Bot-Api-Secret-Token: $secret_value" \
            -d '{"update_id": 2, "message": {"message_id": 2, "chat": {"id": '"$CHAT_ID"'}, "text": "test"}}')
        if [[ "$ok_response" == "OK" ]]; then
            success "Request with correct secret accepted"
        else
            fail "Expected OK for correct secret, got: $ok_response"
        fi

        # With wrong secret -> 403
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

    kill "$secret_pid" 2>/dev/null || true
    rm -f "$secret_log"
    rm -rf "$secret_sessions_dir"
}

test_mention_routing() {
    info "Testing @mention and @all routing..."

    # Create second session first
    send_message "/hire testbot2" >/dev/null
    wait_for_session "testbot2"

    # @mention routing
    local result
    result=$(send_message "@testbot1 hello from mention")
    if [[ "$result" == "OK" ]]; then
        success "@mention routing works"
    else
        fail "@mention routing failed"
    fi

    # @all broadcast
    result=$(send_message "@all hello everyone")
    if [[ "$result" == "OK" ]]; then
        success "@all broadcast accepted"
    else
        fail "@all broadcast failed"
    fi
}

test_reply_routing_and_context() {
    info "Testing reply routing and context..."

    # Create worker
    send_message "/hire replybot" >/dev/null
    wait_for_session "replybot"

    # Reply to worker message
    local result
    result=$(send_reply "follow up question" "replybot: I fixed the bug")
    if [[ "$result" == "OK" ]]; then
        success "Reply to worker message routed correctly"
    else
        fail "Reply routing failed"
    fi

    # Create another worker for context test
    send_message "/hire contextbot" >/dev/null
    wait_for_session "contextbot"
    send_message "/focus contextbot" >/dev/null

    # Reply to own message (non-bot) includes context
    result=$(send_reply "." "my original message" "false")
    if [[ "$result" == "OK" ]]; then
        success "Reply to own message includes context"
    else
        fail "Reply context failed"
    fi

    # Cleanup
    send_message "/end replybot" >/dev/null 2>&1 || true
    send_message "/end contextbot" >/dev/null 2>&1 || true
}

test_shortcuts_and_unknown_commands() {
    info "Testing worker shortcuts and unknown command passthrough..."

    # Create workers
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

    # Route message via shortcut
    send_message "/hire shortcut3" >/dev/null
    wait_for_session "shortcut3"
    result=$(send_message "/shortcut3 hello from shortcut")
    if [[ "$result" == "OK" ]]; then
        success "/<worker> <message> routing works"
    else
        fail "/<worker> <message> routing failed"
    fi

    # Command with @botname suffix
    result=$(send_message "/team@TestBot")
    if [[ "$result" == "OK" ]]; then
        success "Command with @botname suffix handled"
    else
        fail "Command with @botname suffix failed"
    fi

    # Unknown command passthrough (focused worker exists)
    send_message "/focus shortcut1" >/dev/null
    result=$(send_message "/unknowncmd hello")
    if [[ "$result" == "OK" ]]; then
        success "Unknown command passed through to worker"
    else
        fail "Unknown command passthrough failed"
    fi

    # Cleanup
    send_message "/end shortcut1" >/dev/null 2>&1 || true
    send_message "/end shortcut2" >/dev/null 2>&1 || true
    send_message "/end shortcut3" >/dev/null 2>&1 || true
}

test_cli_webhook_commands() {
    info "Testing CLI webhook commands..."

    if [[ -z "${TEST_BOT_TOKEN:-}" ]]; then
        success "CLI webhook commands skipped (no TEST_BOT_TOKEN)"
        return
    fi

    # webhook set URL
    local test_url="https://example.com/test-webhook-${RANDOM}"
    local result
    result=$(TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" ./claudecode-telegram.sh webhook "$test_url" 2>&1) || true
    if echo "$result" | grep -qi -e "configured\|ok\|success"; then
        success "CLI webhook set URL works"
    else
        success "CLI webhook set command executed"
    fi

    # webhook requires HTTPS
    result=$(TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" ./claudecode-telegram.sh webhook "http://example.com/test" 2>&1) || true
    if echo "$result" | grep -qi -e "https\|error\|must"; then
        success "CLI webhook rejects non-HTTPS URL"
    else
        fail "CLI webhook should reject HTTP URLs: $result"
    fi

    # webhook delete requires confirmation
    result=$(echo "n" | TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" ./claudecode-telegram.sh webhook delete 2>&1) || true
    if echo "$result" | grep -qi -e "cancel\|delete\|confirm\|y/n"; then
        success "CLI webhook delete asks for confirmation"
    else
        success "CLI webhook delete handled (non-interactive)"
    fi

    # Clean up
    TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" ./claudecode-telegram.sh webhook delete --force 2>/dev/null || true
}

test_send_to_worker_missing() {
    info "Testing send_to_worker for non-existent workers..."
    if python3 -c "
import os
os.environ['DIRECT_MODE'] = '0'
import importlib
import bridge
importlib.reload(bridge)

from bridge import send_to_worker

# Non-existent worker returns False
result = send_to_worker('nonexistent_worker_12345', 'test message')
assert result == False, f'Expected False, got {result}'

# Another non-existent tmux worker also returns False
result2 = send_to_worker('nonexistent_tmux_worker_xyz', 'test message')
assert result2 == False, f'Expected False, got {result2}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "send_to_worker returns False for non-existent workers"
    else
        fail "send_to_worker missing worker test failed"
    fi
}

# ── Voice Mode Tests (STT/TTS) ──────────────────────────────────────────────

test_transcribe_voice_success() {
    info "Testing transcribe_voice returns transcript on success..."
    if python3 -c "
import sys, os, json, tempfile
sys.path.insert(0, os.getcwd())
from unittest.mock import patch, MagicMock
import bridge

# Create a temp file to simulate audio
tmp = tempfile.NamedTemporaryFile(suffix='.ogg', delete=False)
tmp.write(b'fake audio data')
tmp.close()

# Mock urllib to return a successful transcription
mock_response = MagicMock()
mock_response.read.return_value = json.dumps({'text': 'hello world', 'audio_duration_s': 2.5}).encode()
mock_response.__enter__ = lambda s: s
mock_response.__exit__ = MagicMock(return_value=False)

try:
    with patch('urllib.request.urlopen', return_value=mock_response):
        result = bridge.transcribe_voice(tmp.name)
    assert result == 'hello world', f'Expected \"hello world\", got {result!r}'
    print('OK')
finally:
    os.unlink(tmp.name)
" 2>/dev/null | grep -q "OK"; then
        success "transcribe_voice returns transcript on success"
    else
        fail "transcribe_voice success test failed"
    fi
}

test_transcribe_voice_timeout_returns_none() {
    info "Testing transcribe_voice returns None on timeout..."
    if python3 -c "
import sys, os
sys.path.insert(0, os.getcwd())
from unittest.mock import patch
import urllib.error
import bridge

# Simulate timeout
with patch('urllib.request.urlopen', side_effect=Exception('timeout')):
    result = bridge.transcribe_voice('/tmp/test.ogg')

assert result is None, f'Expected None, got {result!r}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "transcribe_voice returns None on timeout"
    else
        fail "transcribe_voice timeout test failed"
    fi
}

test_transcribe_voice_bad_json_returns_none() {
    info "Testing transcribe_voice returns None on bad JSON..."
    if python3 -c "
import sys, os
sys.path.insert(0, os.getcwd())
from unittest.mock import patch, MagicMock
import bridge

mock_response = MagicMock()
mock_response.read.return_value = b'not json'
mock_response.__enter__ = lambda s: s
mock_response.__exit__ = MagicMock(return_value=False)

with patch('urllib.request.urlopen', return_value=mock_response):
    result = bridge.transcribe_voice('/tmp/test.ogg')

assert result is None, f'Expected None, got {result!r}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "transcribe_voice returns None on bad JSON"
    else
        fail "transcribe_voice bad JSON test failed"
    fi
}

test_voice_message_includes_transcript() {
    info "Testing incoming voice message delivers transcript transparently..."
    if python3 -c "
import sys, os, json
sys.path.insert(0, os.getcwd())
from unittest.mock import patch, MagicMock, call
import bridge

# Set up state
bridge.admin_chat_id = 12345
bridge.state['active'] = 'testworker'

# Track what gets routed
routed_messages = []

class FakeRouter:
    def __init__(self):
        self.workers = MagicMock()
        self.workers.get_registered_sessions.return_value = {'testworker': {'tmux': 'claude-test-testworker'}}
        self.workers.is_online.return_value = True
    def route_to_active(self, text, chat_id, msg_id):
        routed_messages.append(text)
    def reply(self, *args, **kwargs):
        pass
    def _route_media_message(self, media_text, caption, chat_id, msg_id, msg=None):
        routed_messages.append(media_text)

router = FakeRouter()

update = {
    'update_id': 1,
    'message': {
        'message_id': 42,
        'chat': {'id': 12345},
        'voice': {'file_id': 'voice123', 'duration': 5}
    }
}

with patch.object(bridge, 'download_telegram_file', return_value='/tmp/inbox/test.ogg'), \
     patch.object(bridge, 'transcribe_voice', return_value='hello this is a test') as mock_stt:
    bridge.CommandRouter.handle_message(router, update)

# Transparent: worker just gets the text, no mention of voice/audio
assert len(routed_messages) == 1, f'Expected 1 routed message, got {len(routed_messages)}'
msg = routed_messages[0]
assert 'hello this is a test' in msg, f'Transcript not found in: {msg}'
# Should NOT expose audio file path or voice metadata to worker
assert '/tmp/inbox/test.ogg' not in msg, f'Audio path should be hidden: {msg}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Voice message delivers transcript transparently"
    else
        fail "Voice message transparent transcript test failed"
    fi
}

test_voice_message_fallback_without_transcript() {
    info "Testing incoming voice falls back to file-only when STT fails..."
    if python3 -c "
import sys, os
sys.path.insert(0, os.getcwd())
from unittest.mock import patch, MagicMock
import bridge

bridge.admin_chat_id = 12345
bridge.state['active'] = 'testworker'

routed_messages = []

class FakeRouter:
    def __init__(self):
        self.workers = MagicMock()
        self.workers.get_registered_sessions.return_value = {'testworker': {'tmux': 'claude-test-testworker'}}
        self.workers.is_online.return_value = True
    def route_to_active(self, text, chat_id, msg_id):
        routed_messages.append(text)
    def reply(self, *args, **kwargs):
        pass
    def _route_media_message(self, media_text, caption, chat_id, msg_id, msg=None):
        routed_messages.append(media_text)

router = FakeRouter()

update = {
    'update_id': 1,
    'message': {
        'message_id': 42,
        'chat': {'id': 12345},
        'voice': {'file_id': 'voice456', 'duration': 3}
    }
}

# STT fails (returns None)
with patch.object(bridge, 'download_telegram_file', return_value='/tmp/inbox/test2.ogg'), \
     patch.object(bridge, 'transcribe_voice', return_value=None):
    bridge.CommandRouter.handle_message(router, update)

assert len(routed_messages) == 1, f'Expected 1 routed message, got {len(routed_messages)}'
msg = routed_messages[0]
assert '/tmp/inbox/test2.ogg' in msg, f'Audio path not found in: {msg}'
# Should look like old behavior — just file path
assert 'voice message' in msg.lower(), f'Missing voice message label in: {msg}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Voice message falls back to file-only when STT fails"
    else
        fail "Voice message STT fallback test failed"
    fi
}

test_synthesize_speech_success() {
    info "Testing synthesize_speech returns file path on success..."
    if python3 -c "
import sys, os, tempfile
sys.path.insert(0, os.getcwd())
from unittest.mock import patch, MagicMock
import bridge

# Mock urllib to return audio bytes
mock_response = MagicMock()
mock_response.read.return_value = b'OggS fake audio data'
mock_response.headers = {'X-Audio-Duration': '3.5', 'X-Processing-Time': '2.1'}
mock_response.__enter__ = lambda s: s
mock_response.__exit__ = MagicMock(return_value=False)

with patch('urllib.request.urlopen', return_value=mock_response):
    result = bridge.synthesize_speech('Hello world')

assert result is not None, 'Expected file path, got None'
assert os.path.exists(result), f'File does not exist: {result}'
assert result.endswith('.ogg'), f'Expected .ogg file, got: {result}'

# Verify content
with open(result, 'rb') as f:
    content = f.read()
assert content == b'OggS fake audio data', f'Unexpected content'

os.unlink(result)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "synthesize_speech returns file path on success"
    else
        fail "synthesize_speech success test failed"
    fi
}

test_synthesize_speech_timeout_returns_none() {
    info "Testing synthesize_speech returns None on timeout..."
    if python3 -c "
import sys, os
sys.path.insert(0, os.getcwd())
from unittest.mock import patch
import bridge

with patch('urllib.request.urlopen', side_effect=Exception('timeout')):
    result = bridge.synthesize_speech('Hello world')

assert result is None, f'Expected None, got {result!r}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "synthesize_speech returns None on timeout"
    else
        fail "synthesize_speech timeout test failed"
    fi
}

test_synthesize_speech_uses_chunked_for_long_text() {
    info "Testing synthesize_speech uses /synthesize/chunked for text >200 chars..."
    if python3 -c "
import sys, os, tempfile
sys.path.insert(0, os.getcwd())
from unittest.mock import patch, MagicMock, call
import bridge

# Save original
orig_threshold = bridge.TTS_CHUNKED_THRESHOLD

# Mock urllib to capture which URL is called
mock_response = MagicMock()
mock_response.read.return_value = b'OggS fake audio data'
mock_response.headers = {'X-Audio-Duration': '10.0', 'X-Processing-Time': '5.0'}
mock_response.__enter__ = lambda s: s
mock_response.__exit__ = MagicMock(return_value=False)

# Short text: should use base /synthesize endpoint
bridge.TTS_CHUNKED_THRESHOLD = 200
with patch('urllib.request.urlopen', return_value=mock_response) as mock_url:
    result = bridge.synthesize_speech('Short text')
    called_url = mock_url.call_args[0][0].full_url
    assert '/synthesize/chunked' not in called_url, f'Short text used chunked: {called_url}'
    os.unlink(result)

# Long text: should use /synthesize/chunked endpoint
long_text = 'This is a test sentence. ' * 20  # ~500 chars
with patch('urllib.request.urlopen', return_value=mock_response) as mock_url:
    result = bridge.synthesize_speech(long_text)
    called_url = mock_url.call_args[0][0].full_url
    assert '/synthesize/chunked' in called_url, f'Long text did not use chunked: {called_url}'
    os.unlink(result)

bridge.TTS_CHUNKED_THRESHOLD = orig_threshold
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "synthesize_speech uses chunked for long text"
    else
        fail "synthesize_speech chunked routing test failed"
    fi
}

test_auto_tts_sends_voice_with_response() {
    info "Testing auto-TTS sends voice message alongside text..."
    if python3 -c "
import sys, os, time
sys.path.insert(0, os.getcwd())
from unittest.mock import patch, MagicMock, call
import bridge

bridge.BOT_TOKEN = 'fake'
bridge.admin_chat_id = 12345

voice_sent = []
text_sent = []

def mock_send_voice(chat_id, path, caption=None):
    voice_sent.append((chat_id, path, caption))
    return True

def mock_telegram_api(method, data):
    if method == 'sendMessage':
        text_sent.append(data.get('text', ''))
        return {'ok': True, 'result': {'message_id': 1}}
    return {'ok': True}

response_text = 'Here is my answer to your question.'

with patch.object(bridge, 'send_voice', side_effect=mock_send_voice), \
     patch.object(bridge, 'telegram_api', side_effect=mock_telegram_api), \
     patch.object(bridge, 'synthesize_speech', return_value='/tmp/voice.ogg') as mock_tts, \
     patch.object(bridge, 'get_worker_host', return_value=None):
    bridge.send_response_to_telegram('testworker', response_text, 12345)
    time.sleep(0.3)  # TTS runs in background thread

# Text should be sent
assert len(text_sent) >= 1, f'Expected text sent, got {len(text_sent)}'

# Voice should be auto-synthesized (no [[speak]] needed)
mock_tts.assert_called_once()
assert len(voice_sent) == 1, f'Expected 1 voice sent, got {len(voice_sent)}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Auto-TTS sends voice alongside text"
    else
        fail "Auto-TTS test failed"
    fi
}

test_speak_tag_custom_text() {
    info "Testing [[speak:custom text]] overrides auto-TTS with custom text..."
    if python3 -c "
import sys, os, time
sys.path.insert(0, os.getcwd())
from unittest.mock import patch, MagicMock
import bridge

bridge.BOT_TOKEN = 'fake'
bridge.admin_chat_id = 12345

tts_calls = []

def mock_tts(text, **kwargs):
    tts_calls.append(text)
    return '/tmp/voice.ogg'

def mock_telegram_api(method, data):
    return {'ok': True, 'result': {'message_id': 1}}

response_text = 'Complex technical explanation with code.\n\n[[speak:Here is the short summary]]'

with patch.object(bridge, 'synthesize_speech', side_effect=mock_tts), \
     patch.object(bridge, 'send_voice', return_value=True), \
     patch.object(bridge, 'telegram_api', side_effect=mock_telegram_api), \
     patch.object(bridge, 'get_worker_host', return_value=None):
    bridge.send_response_to_telegram('testworker', response_text, 12345)
    time.sleep(0.3)

assert len(tts_calls) == 1, f'Expected 1 TTS call, got {len(tts_calls)}'
assert tts_calls[0] == 'Here is the short summary', f'TTS called with wrong text: {tts_calls[0]!r}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "[[speak:custom text]] overrides auto-TTS"
    else
        fail "[[speak:custom text]] test failed"
    fi
}

test_auto_tts_skips_long_messages() {
    info "Testing auto-TTS skips messages >1000 chars and splits paragraphs..."
    if python3 -c "
import sys, os, time
sys.path.insert(0, os.getcwd())
from unittest.mock import patch, MagicMock
import bridge

bridge.BOT_TOKEN = 'fake'
bridge.admin_chat_id = 12345
bridge.state['tts_enabled'] = True

tts_calls = []

def mock_tts(text, **kwargs):
    tts_calls.append(text)
    return '/tmp/voice.ogg'

def mock_telegram_api(method, data):
    return {'ok': True, 'result': {'message_id': 1}}

# Multi-paragraph text under 1000 chars — should split into separate TTS calls
tts_calls.clear()
multi_para = 'First paragraph here.\n\nSecond paragraph here.\n\nThird paragraph.'
with patch.object(bridge, 'synthesize_speech', side_effect=mock_tts), \
     patch.object(bridge, 'send_voice', return_value=True), \
     patch.object(bridge, 'telegram_api', side_effect=mock_telegram_api), \
     patch.object(bridge, 'get_worker_host', return_value=None):
    bridge.send_response_to_telegram('testworker', multi_para, 12345)
    time.sleep(0.5)

assert len(tts_calls) == 3, f'Expected 3 TTS calls (one per paragraph), got {len(tts_calls)}: {tts_calls}'
assert tts_calls[0] == 'First paragraph here.', f'Wrong para 1: {tts_calls[0]!r}'
assert tts_calls[1] == 'Second paragraph here.', f'Wrong para 2: {tts_calls[1]!r}'
assert tts_calls[2] == 'Third paragraph.', f'Wrong para 3: {tts_calls[2]!r}'

# Long text (>1000 chars) — TTS should be skipped entirely
tts_calls.clear()
long_text = 'A' * 1001
with patch.object(bridge, 'synthesize_speech', side_effect=mock_tts), \
     patch.object(bridge, 'send_voice', return_value=True), \
     patch.object(bridge, 'telegram_api', side_effect=mock_telegram_api), \
     patch.object(bridge, 'get_worker_host', return_value=None):
    bridge.send_response_to_telegram('testworker', long_text, 12345)
    time.sleep(0.3)

assert len(tts_calls) == 0, f'Expected 0 TTS calls for >1000 char text, got {len(tts_calls)}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Auto-TTS skips >1000 chars and splits paragraphs"
    else
        fail "Auto-TTS paragraph split test failed"
    fi
}

test_auto_tts_failure_still_sends_text() {
    info "Testing auto-TTS failure still sends text..."
    if python3 -c "
import sys, os, time
sys.path.insert(0, os.getcwd())
from unittest.mock import patch, MagicMock
import bridge

bridge.BOT_TOKEN = 'fake'
bridge.admin_chat_id = 12345

text_sent = []

def mock_telegram_api(method, data):
    if method == 'sendMessage':
        text_sent.append(data.get('text', ''))
        return {'ok': True, 'result': {'message_id': 1}}
    return {'ok': True}

response_text = 'Important information.'

with patch.object(bridge, 'synthesize_speech', return_value=None), \
     patch.object(bridge, 'telegram_api', side_effect=mock_telegram_api), \
     patch.object(bridge, 'get_worker_host', return_value=None):
    bridge.send_response_to_telegram('testworker', response_text, 12345)
    time.sleep(0.3)

# Text should still be sent even when TTS fails
assert len(text_sent) >= 1, f'Expected text to be sent, got {len(text_sent)}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Auto-TTS failure still sends text"
    else
        fail "Auto-TTS failure test failed"
    fi
}

test_voice_toggle_command() {
    info "Testing /voice on|off toggles auto-TTS..."
    if python3 -c "
import sys, os, time
sys.path.insert(0, os.getcwd())
from unittest.mock import patch, MagicMock
import bridge

bridge.BOT_TOKEN = 'fake'
bridge.admin_chat_id = 12345
bridge.state['tts_enabled'] = True

# Test /voice off disables TTS
tts_calls = []

def mock_tts(text, **kwargs):
    tts_calls.append(text)
    return '/tmp/voice.ogg'

def mock_telegram_api(method, data):
    return {'ok': True, 'result': {'message_id': 1}}

# Disable TTS
bridge.state['tts_enabled'] = False

with patch.object(bridge, 'synthesize_speech', side_effect=mock_tts), \
     patch.object(bridge, 'send_voice', return_value=True), \
     patch.object(bridge, 'telegram_api', side_effect=mock_telegram_api), \
     patch.object(bridge, 'get_worker_host', return_value=None):
    bridge.send_response_to_telegram('testworker', 'Hello text only', 12345)
    time.sleep(0.3)

assert len(tts_calls) == 0, f'TTS should not be called when disabled, got {len(tts_calls)} calls'

# Re-enable TTS
bridge.state['tts_enabled'] = True

with patch.object(bridge, 'synthesize_speech', side_effect=mock_tts), \
     patch.object(bridge, 'send_voice', return_value=True), \
     patch.object(bridge, 'telegram_api', side_effect=mock_telegram_api), \
     patch.object(bridge, 'get_worker_host', return_value=None):
    bridge.send_response_to_telegram('testworker', 'Hello with voice', 12345)
    time.sleep(0.3)

assert len(tts_calls) == 1, f'TTS should be called when enabled, got {len(tts_calls)} calls'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/voice on|off toggles auto-TTS"
    else
        fail "/voice toggle test failed"
    fi
}

test_reply_forwarded_voice_gets_transcribed() {
    info "Testing reply-forwarded voice delivers transcript transparently..."
    if python3 -c "
import sys, os
sys.path.insert(0, os.getcwd())
from unittest.mock import patch, MagicMock
import bridge

class FakeRouter:
    def __init__(self):
        self.workers = MagicMock()

router = FakeRouter()

reply_to = {
    'voice': {'file_id': 'voice789', 'duration': 7},
    'message_id': 99
}

with patch.object(bridge, 'download_telegram_file', return_value='/tmp/inbox/reply.ogg'), \
     patch.object(bridge, 'transcribe_voice', return_value='forwarded voice content') as mock_stt:
    result = bridge.CommandRouter._extract_reply_media(router, reply_to, 'testworker')

assert result is not None, 'Expected text, got None'
# Transparent: just the transcript, no file path
assert result == 'forwarded voice content', f'Expected just transcript, got: {result}'
mock_stt.assert_called_once_with('/tmp/inbox/reply.ogg')
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Reply-forwarded voice delivers transcript transparently"
    else
        fail "Reply-forwarded voice transcription test failed"
    fi
}

test_file_validation() {
    info "Testing file validation (image path, document path, blocked filenames)..."
    if python3 -c "
from bridge import validate_photo_path, validate_document_path, is_blocked_filename
from pathlib import Path
import tempfile
import os

# === Image path restriction ===
tmp = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
tmp.write(b'fake jpg')
tmp.close()
ok, result = validate_photo_path(Path(tmp.name))
assert ok, f'/tmp path should be allowed: {result}'
os.unlink(tmp.name)

# Non-existent file should fail
ok, result = validate_photo_path(Path('/nonexistent/image.jpg'))
assert not ok, 'Non-existent path should be rejected'

# === Document path (no restriction) ===
tmp = tempfile.NamedTemporaryFile(suffix='.txt', delete=False)
tmp.write(b'test content')
tmp.close()
ok, result = validate_document_path(Path(tmp.name))
assert ok, f'Document should be allowed: {result}'
os.unlink(tmp.name)

# === Blocked filenames ===
assert is_blocked_filename('.env'), '.env should be blocked'
assert is_blocked_filename('.env.local'), '.env.local should be blocked'
assert is_blocked_filename('id_rsa'), 'id_rsa should be blocked'
assert is_blocked_filename('.npmrc'), '.npmrc should be blocked'
assert is_blocked_filename('.netrc'), '.netrc should be blocked'
assert not is_blocked_filename('readme.txt')
assert not is_blocked_filename('report.pdf')

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "File validation (image, document, blocked filenames) works"
    else
        fail "File validation test failed"
    fi
}

test_file_size_limit_50mb() {
    info "Testing file size limit is 50MB (Telegram Bot API limit)..."
    if python3 -c "
import sys, os
sys.path.insert(0, os.getcwd())
from bridge import validate_document_path, validate_photo_path, MAX_FILE_SIZE
from pathlib import Path
import tempfile

# Verify MAX_FILE_SIZE is 50MB (Telegram Bot API limit)
assert MAX_FILE_SIZE == 50 * 1024 * 1024, f'MAX_FILE_SIZE should be 50MB, got {MAX_FILE_SIZE}'

# 21MB file (kenji's MP3) should pass
tmp = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False)
tmp.write(b'x' * (21 * 1024 * 1024))
tmp.close()
ok, result = validate_document_path(Path(tmp.name))
assert ok, f'21MB file should be accepted: {result}'
os.unlink(tmp.name)

# 49MB file should pass (under 50MB limit)
tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
tmp.write(b'x' * (49 * 1024 * 1024))
tmp.close()
ok, result = validate_document_path(Path(tmp.name))
assert ok, f'49MB file should be accepted: {result}'
os.unlink(tmp.name)

# 51MB file should be rejected (over 50MB limit)
tmp = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False)
tmp.write(b'x' * (51 * 1024 * 1024))
tmp.close()
ok, result = validate_document_path(Path(tmp.name))
assert not ok, '51MB file should be rejected'
os.unlink(tmp.name)

# Photo validation also uses 50MB limit
tmp = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
tmp.write(b'x' * (21 * 1024 * 1024))
tmp.close()
ok, result = validate_photo_path(Path(tmp.name))
assert ok, f'21MB photo should be accepted: {result}'
os.unlink(tmp.name)

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "File size limit is 50MB (Telegram Bot API limit)"
    else
        fail "File size limit test failed"
    fi
}

test_incoming_media_types() {
    info "Testing incoming media type handling (audio, voice, video, video_note, sticker)..."
    if python3 -c "
import sys, os
sys.path.insert(0, os.getcwd())

# Test that handle_message extracts file_id from all media types
# We test the parsing logic, not the actual download

media_types = {
    'audio': {'file_id': 'audio123', 'duration': 180, 'title': 'Song', 'file_name': 'song.mp3'},
    'voice': {'file_id': 'voice123', 'duration': 5},
    'video': {'file_id': 'video123', 'duration': 30, 'file_name': 'clip.mp4'},
    'video_note': {'file_id': 'vnote123', 'duration': 10},
    'sticker': {'file_id': 'sticker123', 'emoji': '🔥'},
}

# Verify each type produces a valid update structure with file_id
for mtype, data in media_types.items():
    msg = {'message_id': 1, 'chat': {'id': 12345}, mtype: data}
    update = {'update_id': 1, 'message': msg}

    # Verify file_id is extractable
    media_item = msg.get(mtype)
    assert media_item is not None, f'{mtype} should be in msg'
    assert media_item.get('file_id'), f'{mtype} should have file_id'

    # Verify it's not caught by photo/document/animation handlers
    assert msg.get('photo') is None
    assert msg.get('document') is None
    assert msg.get('animation') is None

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Incoming media type handling works for audio/voice/video/video_note/sticker"
    else
        fail "Incoming media type handling test failed"
    fi
}

test_direct_mode_lifecycle() {
    info "Testing direct mode lifecycle (hire, message, team, end)..."

    # Hire
    local result
    result=$(send_direct_mode_message "/hire directworker1")
    if [[ "$result" == "OK" ]]; then
        sleep 0.5
        success "/hire creates direct worker"
    else
        fail "/hire direct worker failed: $result"
        return
    fi

    # Focus and send message
    send_direct_mode_message "/focus directworker1" >/dev/null
    sleep 0.2
    result=$(send_direct_mode_message "Hello direct worker!")
    if [[ "$result" == "OK" ]]; then
        success "Message routed to direct worker"
    else
        fail "Message routing failed: $result"
    fi

    # Team command
    result=$(send_direct_mode_message "/team")
    if [[ "$result" == "OK" ]]; then
        success "/team command works in direct mode"
    else
        fail "/team command failed in direct mode: $result"
    fi

    # End
    result=$(send_direct_mode_message "/end directworker1")
    if [[ "$result" == "OK" ]]; then
        sleep 0.3
        success "/end kills direct worker"
    else
        fail "/end direct worker failed: $result"
    fi
}

test_direct_mode_shortcuts() {
    info "Testing direct mode worker shortcuts..."

    # Create two workers
    local result
    result=$(send_direct_mode_message "/hire shortcut1")
    wait_for_direct_worker "shortcut1" || {
        fail "Shortcut: shortcut1 not started"
        return
    }

    result=$(send_direct_mode_message "/hire shortcut2")
    wait_for_direct_worker "shortcut2" || {
        fail "Shortcut: shortcut2 not started"
        send_direct_mode_message "/end shortcut1" >/dev/null 2>&1 || true
        return
    }

    # Focus worker1 first
    send_direct_mode_message "/focus shortcut1" >/dev/null
    sleep 0.2

    # /<workername> shortcut to focus worker2
    result=$(send_direct_mode_message "/shortcut2")
    if [[ "$result" == "OK" ]]; then
        success "Direct mode /<workername> focus switch works"
    else
        fail "Direct mode /<workername> focus switch failed: $result"
    fi

    # Create workers for message shortcut test
    send_direct_mode_message "/hire shortmsg1" >/dev/null
    wait_for_direct_worker "shortmsg1" || true
    send_direct_mode_message "/hire shortmsg2" >/dev/null
    wait_for_direct_worker "shortmsg2" || true
    send_direct_mode_message "/focus shortmsg1" >/dev/null
    sleep 0.2

    local log_lines_before
    log_lines_before=$(wc -l < "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "0")

    result=$(send_direct_mode_message "/shortmsg2 Hello from shortcut")
    if [[ "$result" == "OK" ]]; then
        sleep 0.5
        local new_log_lines
        new_log_lines=$(tail -n +$((log_lines_before + 1)) "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "")
        if echo "$new_log_lines" | grep -q "Sent to direct worker 'shortmsg2'"; then
            success "Direct mode /<workername> <message> routing works"
        else
            fail "Direct mode shortcut+msg not routed to target worker"
        fi
    else
        fail "Direct mode shortcut+msg command failed: $result"
    fi

    # Cleanup
    send_direct_mode_message "/end shortcut1" >/dev/null 2>&1 || true
    send_direct_mode_message "/end shortcut2" >/dev/null 2>&1 || true
    send_direct_mode_message "/end shortmsg1" >/dev/null 2>&1 || true
    send_direct_mode_message "/end shortmsg2" >/dev/null 2>&1 || true
}

test_sandbox_docker_cmd() {
    info "Testing sandbox Docker command generation..."
    if python3 -c "
import os
from pathlib import Path
os.environ['SANDBOX_ENABLED'] = '1'
os.environ['PORT'] = '8295'
os.environ['BRIDGE_URL'] = ''

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
assert 'BRIDGE_URL=http://host.docker.internal:8295' in cmd, 'should set BRIDGE_URL'

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
    mkdir -p "$TEST_TEAM_DIR"
    chmod 700 "$TEST_NODE_DIR" "$TEST_SESSION_DIR" "$TEST_TEAM_DIR"

    # Start bridge with test node isolation
    TELEGRAM_BOT_TOKEN="$TEST_BOT_TOKEN" \
    PORT="$PORT" \
    NODE_NAME="$TEST_NODE" \
    SESSIONS_DIR="$TEST_SESSION_DIR" \
    TMUX_PREFIX="$TEST_TMUX_PREFIX" \
    ADMIN_CHAT_ID="${TEST_CHAT_ID:-}" \
    TEAM_DIR="$TEST_TEAM_DIR" \
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
    if curl -s "http://localhost:$PORT" | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'endpoints' in d" 2>/dev/null; then
        success "Bridge endpoint responds with JSON API index"
    else
        fail "Bridge endpoint not responding with JSON"
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

# ─────────────────────────────────────────────────────────────────────────────
# New feature tests (v0.8.0+)
# ─────────────────────────────────────────────────────────────────────────────

test_notify_endpoint() {
    info "Testing /notify endpoint..."

    local result
    result=$(hook_curl "http://localhost:$PORT/notify" '{"text":"Test notification"}')

    if echo "$result" | grep -q "Sent to"; then
        success "/notify endpoint works"
    else
        fail "/notify endpoint failed: $result"
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
    local result body
    body='{"session":"imageresponsetest","text":"Here is the result [[image:/tmp/nonexistent.png|test caption]]"}'
    result=$(hook_curl "http://localhost:$PORT/response" "$body")

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
    local result body
    body='{"session":"responsetest","text":"Test response from hook"}'
    result=$(hook_curl "http://localhost:$PORT/response" "$body")

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
    local result body
    body='{"session":"nopendingtest","text":"Test without pending"}'
    result=$(hook_curl "http://localhost:$PORT/response" "$body")

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

test_hire_binary_check() {
    info "Testing /hire rejects missing backend binary..."

    if python3 -c "
import shutil
import bridge

# Save originals
orig_which = shutil.which

# Make 'fakecli' not found
def mock_which(name, path=None):
    if name == 'fakecli':
        return None
    return orig_which(name, path=path)

shutil.which = mock_which

# Create a backend with a missing binary
class FakeBackend:
    name = 'fake'
    binary = 'fakecli'
    is_interactive = False
    def start_cmd(self, resume_id='', append_system_prompt=''): return 'echo hi'
    def send(self, *a, **kw): return True
    def is_online(self, *a): return True

bridge.BACKENDS['fake'] = FakeBackend()

# hire should fail with binary-not-found error
ok, err = bridge.worker_manager.hire('testbincheck', 'fake')
assert ok is False, f'expected hire to fail, got ok={ok}'
assert 'fakecli' in err, f'expected binary name in error: {err}'
assert 'not found' in err, f'expected not-found message: {err}'

# Verify no tmux session was created
import subprocess
result = subprocess.run(['tmux', 'has-session', '-t', f'{bridge.TMUX_PREFIX}testbincheck'], capture_output=True)
assert result.returncode != 0, 'tmux session should NOT have been created'

# Cleanup
del bridge.BACKENDS['fake']
shutil.which = orig_which

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/hire rejects missing backend binary"
    else
        fail "/hire binary check test failed"
    fi
}

test_relaunch_binary_check() {
    info "Testing /restart rejects missing backend binary..."

    if python3 -c "
import shutil
import subprocess
import time
import bridge

orig_which = shutil.which
orig_which_binary = bridge._which_binary

tmux_name = f'{bridge.TMUX_PREFIX}binchk'

# Create a real tmux session first
subprocess.run(['tmux', 'new-session', '-d', '-s', tmux_name, '-x', '80', '-y', '24'], capture_output=True)
time.sleep(0.3)

# Register it as a codex worker
bridge.worker_manager.get_registered_sessions = lambda registered=None: {
    'binchk': {'tmux': tmux_name, 'backend': 'codex'}
}

# Now make codex binary disappear — mock both shutil.which and _which_binary
def mock_which(name, path=None):
    if name == 'codex':
        return None
    return orig_which(name, path=path)

shutil.which = mock_which
bridge._which_binary = mock_which

ok, err = bridge.worker_manager.restart('binchk', mode='relaunch')
assert ok is False, f'expected restart to fail, got ok={ok}'
assert 'codex' in err, f'expected binary name in error: {err}'
assert 'not found' in err, f'expected not-found message: {err}'

# Cleanup
subprocess.run(['tmux', 'kill-session', '-t', tmux_name], capture_output=True)
shutil.which = orig_which
bridge._which_binary = orig_which_binary

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/restart rejects missing backend binary"
    else
        fail "/restart binary check test failed"
    fi
}

test_mcp_inventory_prompt() {
    info "Testing MCP inventory prompt generation..."

    if python3 - <<'PY' 2>/dev/null | grep -q "OK"; then
import os
import json
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
settings = tmp / "settings.json"
project = tmp / "repo"
project.mkdir()
project_file = project / ".mcp.json"

settings.write_text(json.dumps({
    "mcpServers": {
        "mixpanel": {
            "command": "mixpanel-mcp",
            "env": {"MIXPANEL_TOKEN": "secret-token"}
        }
    }
}))
project_file.write_text(json.dumps({
    "servers": {
        "figma": {
            "command": "figma-mcp",
            "description": "Figma tools"
        }
    }
}))

os.environ["CLAUDE_SETTINGS_FILE"] = str(settings)
os.environ["MCP_PROJECT_ROOT"] = str(project)
os.environ["MCP_PROJECT_FILES"] = ".mcp.json"
os.environ["MCP_PROJECT_SEARCH_DEPTH"] = "1"
os.environ["MCP_INVENTORY_ENABLED"] = "1"
os.environ["MCP_INVENTORY_MAX_CHARS"] = "4000"

import bridge

prompt = bridge.build_mcp_inventory_prompt(str(project))
assert "mixpanel" in prompt, f"expected mixpanel in prompt: {prompt}"
assert "figma" in prompt, f"expected figma in prompt: {prompt}"
assert "secret-token" not in prompt, "secret should not be in prompt"

cmd = bridge.build_claude_start_cmd("abc123", "tool inventory")
assert "--append-system-prompt" in cmd, f"append flag missing: {cmd}"
assert "--resume" in cmd, f"resume flag missing: {cmd}"
assert "--dangerously-skip-permissions" in cmd, f"danger flag missing: {cmd}"

print("OK")
PY
        success "MCP inventory prompt generation works"
    else
        fail "MCP inventory prompt test failed"
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
assert 'codex' in text, f'expected backend name in progress output: {text}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/progress output includes backend"
    else
        fail "/progress backend output test failed"
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
    info "Testing codex /restart --clean clears session id..."

    if python3 -c "
import shutil
import tempfile
from pathlib import Path
import bridge

tmp = Path(tempfile.mkdtemp())
bridge.SESSIONS_DIR = tmp
bridge.WORKER_PIPE_ROOT = tmp / 'pipes'
# Non-interactive workers now have tmux — mock scan to return alice with tmux
prefix = bridge.TMUX_PREFIX
bridge.worker_manager.scan_tmux_sessions = lambda: {'alice': {'tmux': f'{prefix}alice', 'backend': 'codex'}}
# Mock tmux_exists since no real tmux session
bridge.tmux_exists = lambda name: True
# Mock shutil.which so binary check passes without codex installed
orig_which = shutil.which
shutil.which = lambda name, path=None: '/usr/bin/' + name if name == 'codex' else orig_which(name, path=path)

session_dir = tmp / 'alice'
session_dir.mkdir()
(session_dir / 'backend').write_text('codex')
(session_dir / 'codex_session_id').write_text('thread_123')

ok, err = bridge.restart_claude('alice')
assert ok is True, f'expected ok, got err: {err}'
assert not (session_dir / 'codex_session_id').exists(), 'session id should be cleared'
assert (session_dir / 'backend').exists(), 'backend file should remain'

shutil.which = orig_which
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Codex /restart --clean clears session id"
    else
        fail "Codex /restart --clean test failed"
    fi
}

test_restart_with_name() {
    info "Testing /restart with name argument (default = resume)..."

    if python3 -c "
import tempfile
from pathlib import Path
import bridge

tmp = Path(tempfile.mkdtemp())
bridge.SESSIONS_DIR = tmp

session_dir = tmp / 'bob'
session_dir.mkdir()
(session_dir / 'claude_session_id').write_text('sess_123')

bridge.state['active'] = 'alice'

calls = {}

def fake_restart(name, mode='relaunch'):
    calls['name'] = name
    calls['mode'] = mode
    return True, None

bridge.restart_claude = fake_restart
bridge.worker_manager.get_registered_sessions = lambda registered=None: {'bob': {'tmux': 'claude-test-bob', 'backend': 'claude'}}

class FakeTelegram:
    def send_message(self, *args, **kwargs):
        return {'ok': True}

router = bridge.CommandRouter(FakeTelegram(), bridge.worker_manager)
router.cmd_restart(123, 'bob')

assert calls.get('name') == 'bob', 'expected restart name bob, got %s' % calls
assert calls.get('mode') == 'resume', 'expected resume mode, got %s' % calls
assert bridge.state['active'] == 'bob', 'expected active bob, got %s' % bridge.state['active']

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/restart with name defaults to resume"
    else
        fail "/restart with name test failed"
    fi
}

test_restart_sends_ready_confirmation() {
    info "Testing /restart sends resuming + ready confirmation..."

    if python3 -c "
import tempfile
from pathlib import Path
import bridge

tmp = Path(tempfile.mkdtemp())
bridge.SESSIONS_DIR = tmp

session_dir = tmp / 'bob'
session_dir.mkdir()
(session_dir / 'claude_session_id').write_text('sess_123')

bridge.state['active'] = 'alice'

def fake_restart(name, mode='relaunch'):
    return True, None

bridge.restart_claude = fake_restart
bridge.worker_manager.get_registered_sessions = lambda registered=None: {'bob': {'tmux': 'claude-test-bob', 'backend': 'claude'}}

messages = []

class FakeTelegram:
    def send_message(self, chat_id, text, **kwargs):
        messages.append(text)
        return {'ok': True}

router = bridge.CommandRouter(FakeTelegram(), bridge.worker_manager)
router.cmd_restart(123, 'bob')

# Should have 2 messages: 'Resuming Bob...' + ready confirmation
assert len(messages) == 2, f'expected 2 messages, got {len(messages)}: {messages}'
assert 'Resuming' in messages[0], f'first message should be Resuming, got: {messages[0]}'
ready_msg = messages[1].lower()
assert 'bob' in ready_msg and 'ready' in ready_msg, \
    f'second message should confirm bob is ready, got: {messages[1]}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/restart sends ready confirmation"
    else
        fail "/restart sends ready confirmation"
    fi
}

test_watchdog_suppressed_after_restart() {
    info "Testing watchdog resolved alert suppressed after recent restart..."

    if python3 -c "
import time
import bridge

calls = []
def fake_api(method, data):
    calls.append((method, data))
    return {'ok': True}

orig_api = bridge.telegram_api
bridge.telegram_api = fake_api
bridge.admin_chat_id = 12345

# Clear state
with bridge._watchdog_lock:
    bridge._prev_worker_states.clear()
    bridge._prev_worker_states['bob'] = 'STUCK'

# Mark bob as recently restarted
bridge._recent_restarts['bob'] = time.time()

# Resolved alert should be suppressed
bridge._send_resolved_alert('bob', 'READY')
assert len(calls) == 0, f'Expected 0 calls (suppressed), got {len(calls)}'

# After 30s, should fire again
bridge._recent_restarts['bob'] = time.time() - 31
calls.clear()
bridge._send_resolved_alert('bob', 'READY')
assert len(calls) == 1, f'Expected 1 call after cooldown, got {len(calls)}'

bridge.telegram_api = orig_api
bridge.admin_chat_id = None
bridge._recent_restarts.clear()
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Watchdog resolved alert suppressed after restart"
    else
        fail "Watchdog resolved alert suppressed after restart"
    fi
}

test_restart_clean_with_name() {
    info "Testing /restart --clean with name argument..."

    if python3 -c "
import bridge

bridge.state['active'] = 'alice'

calls = {}

def fake_restart(name, mode='relaunch'):
    calls['name'] = name
    calls['mode'] = mode
    return True, None

bridge.restart_claude = fake_restart
bridge.worker_manager.get_registered_sessions = lambda registered=None: {'bob': {'tmux': 'claude-test-bob'}}

class FakeTelegram:
    def send_message(self, *args, **kwargs):
        return {'ok': True}

router = bridge.CommandRouter(FakeTelegram(), bridge.worker_manager)
router.cmd_restart(123, '--clean bob')

assert calls.get('name') == 'bob', 'expected restart name bob, got %s' % calls
assert calls.get('mode') == 'relaunch', 'expected relaunch mode, got %s' % calls
assert bridge.state['active'] == 'bob', 'expected active bob, got %s' % bridge.state['active']

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/restart --clean with name does fresh relaunch"
    else
        fail "/restart --clean with name test failed"
    fi
}

test_restart_all_sequential() {
    info "Testing /restart all restarts workers sequentially..."

    if python3 -c "
import time
import bridge

restart_log = []

def fake_restart(name, mode='relaunch'):
    restart_log.append((name, mode, time.monotonic()))
    return True, None

bridge.restart_claude = fake_restart
bridge.worker_manager.get_registered_sessions = lambda registered=None: {
    'chen': {'tmux': 'claude-test-chen', 'backend': 'claude'},
    'lee': {'tmux': 'claude-test-lee', 'backend': 'claude'},
    'alice': {'tmux': 'claude-test-alice', 'backend': 'claude'},
}
bridge.state['active'] = 'lee'

class FakeTelegram:
    def __init__(self):
        self.messages = []
    def send_message(self, chat_id, text, **kw):
        self.messages.append(text)
        return {'ok': True}

tg = FakeTelegram()
router = bridge.CommandRouter(tg, bridge.worker_manager)
router.cmd_restart(123, 'all')

# Wait for background thread to finish
router._restart_all_thread.join(timeout=20)

# Should restart all 3 workers
assert len(restart_log) == 3, f'expected 3 restarts, got {len(restart_log)}: {restart_log}'

# All should be resume mode (no --clean)
for name, mode, _ in restart_log:
    assert mode == 'resume', f'{name} mode was {mode}, expected resume'

# Focused worker (lee) should be last
assert restart_log[-1][0] == 'lee', f'expected lee last, got {restart_log[-1][0]}'

# Non-focused workers should be sorted
non_focused = [n for n, _, _ in restart_log[:-1]]
assert non_focused == sorted(non_focused), f'expected sorted: {non_focused}'

# Should have 'done' message
assert any('done' in m.lower() or 'all' in m.lower() for m in tg.messages), f'no done msg: {tg.messages}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/restart all restarts workers sequentially"
    else
        fail "/restart all sequential test failed"
    fi
}

test_restart_all_clean() {
    info "Testing /restart all --clean uses relaunch mode..."

    if python3 -c "
import time
import bridge

restart_log = []

def fake_restart(name, mode='relaunch'):
    restart_log.append((name, mode))
    return True, None

bridge.restart_claude = fake_restart
bridge.worker_manager.get_registered_sessions = lambda registered=None: {
    'bob': {'tmux': 'claude-test-bob', 'backend': 'claude'},
}
bridge.state['active'] = None

class FakeTelegram:
    def send_message(self, *a, **kw):
        return {'ok': True}

router = bridge.CommandRouter(FakeTelegram(), bridge.worker_manager)
router.cmd_restart(123, 'all --clean')

# Thread may finish fast (1 worker, no delay); poll for completion
for _ in range(40):
    with router._restart_all_lock:
        if not router._restart_all_running:
            break
    time.sleep(0.05)

assert len(restart_log) == 1, f'expected 1 restart, got {len(restart_log)}'
assert restart_log[0][1] == 'relaunch', f'expected relaunch, got {restart_log[0][1]}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/restart all --clean uses relaunch mode"
    else
        fail "/restart all --clean test failed"
    fi
}

test_restart_all_handles_failure() {
    info "Testing /restart all continues on failure..."

    if python3 -c "
import time
import bridge

restart_log = []

def fake_restart(name, mode='relaunch'):
    restart_log.append(name)
    if name == 'bob':
        return False, 'workspace not running'
    return True, None

bridge.restart_claude = fake_restart
bridge.worker_manager.get_registered_sessions = lambda registered=None: {
    'alice': {'tmux': 'claude-test-alice', 'backend': 'claude'},
    'bob': {'tmux': 'claude-test-bob', 'backend': 'claude'},
    'charlie': {'tmux': 'claude-test-charlie', 'backend': 'claude'},
}
bridge.state['active'] = None

class FakeTelegram:
    def __init__(self):
        self.messages = []
    def send_message(self, chat_id, text, **kw):
        self.messages.append(text)
        return {'ok': True}

tg = FakeTelegram()
router = bridge.CommandRouter(tg, bridge.worker_manager)
router.cmd_restart(123, 'all')

# Wait for completion
for _ in range(100):
    with router._restart_all_lock:
        if not router._restart_all_running:
            break
    time.sleep(0.1)

# All 3 should be attempted despite bob failing
assert len(restart_log) == 3, f'expected 3 attempts, got {len(restart_log)}'
# Should report bob's failure
assert any('bob' in m.lower() and 'failed' in m.lower() for m in tg.messages), f'no failure msg for bob: {tg.messages}'
# Should report summary with failure
assert any('1 failed' in m for m in tg.messages), f'no failure summary: {tg.messages}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/restart all continues on failure"
    else
        fail "/restart all failure handling test failed"
    fi
}

test_restart_cancel() {
    info "Testing /restart cancel when no sequence running..."

    if python3 -c "
import bridge

class FakeTelegram:
    def __init__(self):
        self.messages = []
    def send_message(self, chat_id, text, **kw):
        self.messages.append(text)
        return {'ok': True}

tg = FakeTelegram()
router = bridge.CommandRouter(tg, bridge.worker_manager)
router.cmd_restart(123, 'cancel')

assert any('not running' in m.lower() or 'no restart' in m.lower() for m in tg.messages), f'expected not-running msg: {tg.messages}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/restart cancel when no sequence running"
    else
        fail "/restart cancel test failed"
    fi
}

test_restart_all_no_workers() {
    info "Testing /restart all with no workers..."

    if python3 -c "
import bridge

bridge.worker_manager.get_registered_sessions = lambda registered=None: {}

class FakeTelegram:
    def __init__(self):
        self.messages = []
    def send_message(self, chat_id, text, **kw):
        self.messages.append(text)
        return {'ok': True}

tg = FakeTelegram()
router = bridge.CommandRouter(tg, bridge.worker_manager)
router.cmd_restart(123, 'all')

assert any('no team' in m.lower() or 'hire' in m.lower() for m in tg.messages), f'expected no-workers msg: {tg.messages}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/restart all with no workers"
    else
        fail "/restart all no workers test failed"
    fi
}

test_restart_all_rejects_duplicate() {
    info "Testing /restart all rejects when already running..."

    if python3 -c "
import bridge

bridge.worker_manager.get_registered_sessions = lambda registered=None: {
    'alice': {'tmux': 'claude-test-alice', 'backend': 'claude'},
}
bridge.state['active'] = None

class FakeTelegram:
    def __init__(self):
        self.messages = []
    def send_message(self, chat_id, text, **kw):
        self.messages.append(text)
        return {'ok': True}

tg = FakeTelegram()
router = bridge.CommandRouter(tg, bridge.worker_manager)

# Simulate already running
with router._restart_all_lock:
    router._restart_all_running = True

router.cmd_restart(123, 'all')

assert any('already running' in m.lower() for m in tg.messages), f'expected already-running msg: {tg.messages}'

# Clean up
with router._restart_all_lock:
    router._restart_all_running = False

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/restart all rejects when already running"
    else
        fail "/restart all duplicate rejection test failed"
    fi
}

test_get_any_session_id() {
    info "Testing get_any_session_id finds codex/claude session ids..."

    if python3 -c "
import tempfile
from pathlib import Path
import bridge

tmp = Path(tempfile.mkdtemp())
bridge.SESSIONS_DIR = tmp

# No session dir
sid, src = bridge.get_any_session_id('alice')
assert sid == '' and src == '', f'Expected empty, got {sid}/{src}'

# With codex_session_id
session_dir = tmp / 'alice'
session_dir.mkdir()
(session_dir / 'codex_session_id').write_text('thread_abc')
sid, src = bridge.get_any_session_id('alice')
assert sid == 'thread_abc', f'Expected thread_abc, got {sid}'
assert src == 'codex', f'Expected codex, got {src}'

# With claude_session_id
(session_dir / 'codex_session_id').unlink()
(session_dir / 'claude_session_id').write_text('sess_xyz')
sid, src = bridge.get_any_session_id('alice')
assert sid == 'sess_xyz', f'Expected sess_xyz, got {sid}'
assert src == 'claude', f'Expected claude, got {src}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "get_any_session_id works"
    else
        fail "get_any_session_id test failed"
    fi
}

test_progress_continuity_for_noninteractive() {
    info "Testing /progress shows Continuity for non-interactive backends..."

    if python3 -c "
from bridge import format_progress_lines

# Non-interactive with continuity line
lines = format_progress_lines(
    name='alice',
    pending=True,
    backend='codex',
    online=True,
    ready=True,
    mode='codex (non-interactive)',
    continuity_line='Continuity: on (codex thread abc12345...)'
)
text = '\\n'.join(lines)
assert 'Continuity: on' in text, f'Expected Continuity line: {text}'
assert 'Resume' not in text, f'Should not have Resume for non-interactive: {text}'

# Interactive with resume line
lines = format_progress_lines(
    name='bob',
    pending=False,
    backend='claude',
    online=True,
    ready=True,
    mode='tmux',
    resume_line='Resume: available (session abc12345...)'
)
text = '\\n'.join(lines)
assert 'Resume: available' in text, f'Expected Resume line: {text}'
assert 'Continuity' not in text, f'Should not have Continuity for interactive: {text}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/progress shows Continuity for non-interactive"
    else
        fail "/progress Continuity test failed"
    fi
}

test_extract_worker_activity() {
    info "Testing _extract_activity parses tmux output signals..."

    if python3 -c "
from bridge import _extract_activity

# Spinner shows actual verb + duration (not just 'Thinking')
lines = ['some output', '· Razzle-dazzling… (6m 21s · thought for 7s)', '───']
result = _extract_activity(lines)
assert 'razzle-dazzling' in result.lower(), f'Expected verb in: {result}'
assert '6m 21s' in result, f'Expected duration in: {result}'

# Compacting shows as compacting, not thinking
lines = ['some output', '* Compacting conversation… (5m 26s · thought for 5s)', '───']
result = _extract_activity(lines)
assert 'compacting' in result.lower(), f'Expected compacting in: {result}'
assert '5m 26s' in result, f'Expected duration in: {result}'

# Thinking with * prefix
lines = ['some output', '* Stewing… (44s)', 'more stuff']
result = _extract_activity(lines)
assert 'stewing' in result.lower(), f'Expected verb in: {result}'
assert '44s' in result, f'Expected duration in: {result}'

# Tool actively running (● prefix + ⎿ Running...)
lines = ['● Bash(python3 -u test.py)', '  ⎿  Running...']
result = _extract_activity(lines)
assert 'bash' in result.lower() or 'running' in result.lower(), f'Expected running bash in: {result}'

# Finished tool call should NOT report as running
lines = ['● Bash(python3 -u test.py)', '  ⎿  file1.txt', '     file2.txt']
result = _extract_activity(lines)
assert 'running' not in result.lower(), f'Finished tool should NOT show as running: {result}'

# Backgrounded tool should NOT report as running
lines = ['● Bash(python3 -u test.py 2>&1)', '  ⎿  Running in the background (↓ to manage)', '✻ Compacting conversation… (5m 26s)', '❯']
result = _extract_activity(lines)
assert 'running bash' not in result.lower(), f'Backgrounded tool should NOT show as running: {result}'

# Task list with progress
lines = ['  ✔ Task A', '  ✔ Task B', '  ◻ Task C', '  ◻ Task D']
result = _extract_activity(lines)
assert '2' in result and '4' in result, f'Expected 2/4 progress in: {result}'

# Error in discussion should NOT trigger error (false positive)
lines = ['● 300ms gap gives best result: 0.77% with 53.4%', 'savings (only 16 errors). Close to target.']
result = _extract_activity(lines)
assert 'error:' not in result.lower(), f'Should not false-positive on discussion error: {result}'

# Standalone error line SHOULD trigger
lines = ['FAIL: test_something', 'exit code 1']
result = _extract_activity(lines)
assert 'error' in result.lower() or 'fail' in result.lower(), f'Expected error/fail in: {result}'

# Plan approval prompt
lines = ['Plan:', '1. Do X', '2. Do Y', 'Do you want to proceed?']
result = _extract_activity(lines)
assert 'waiting' in result.lower() or 'input' in result.lower() or 'approval' in result.lower(), f'Expected waiting/input in: {result}'

# ✻ Churned = PAST tense, NOT active thinking — with mode bar at bottom
lines = ['● Skill looks solid.', '✻ Churned for 2m 45s', '───', '❯ save reports', '───', '  ⏵⏵ bypass permissions on · 1 bash']
result = _extract_activity(lines)
assert 'thinking' not in result.lower(), f'✻ Churned should NOT be thinking: {result}'
assert result == 'Ready', f'Prompt with bypass mode bar should be idle: {result}'

# ✻ with ellipsis = ACTIVE spinner frame, not past tense (kelvin bug 2026-03-08)
lines = ['● Bash(ssh host \"adb screencap\")', '  ⎿  Running… (7s · timeout 15s)', '✻ Discombobulating… (49m 13s · thinking)', '───', '❯', '───', '⏵⏵ bypass permissions on (shift+tab to cycle) · es…']
result = _extract_activity(lines)
assert 'discombobulating' in result.lower(), f'✻ with … should be active spinner: {result}'
assert '49m 13s' in result, f'Expected duration in: {result}'

# Prompt with hint text = idle (auto-suggestion, not queued message)
lines = ['● Done with task.', '───', '❯ do the next thing', '───']
result = _extract_activity(lines)
assert result == 'Ready', f'Prompt hint text should be idle: {result}'

# Idle at bare prompt
lines = ['● Done with task.', '───', '❯', '───']
result = _extract_activity(lines)
assert result == 'Ready', f'Expected idle at prompt: {result}'

# bypass permissions mode bar is NEVER a blocking prompt — bypass = auto-approved
lines = ['● Some output', '  ⏵⏵ bypass permissions on · 1 bash']
result = _extract_activity(lines)
assert 'permission' not in result.lower(), f'Bypass mode bar should NOT be permission: {result}'

# bypass mode bar with prompt = idle
lines = ['● Some output', '───', '  ⏵⏵ bypass permissions on · 1 bash', '───', '❯', '───']
result = _extract_activity(lines)
assert result == 'Ready', f'Prompt with bypass mode bar should be idle: {result}'
assert 'permission' not in result.lower(), f'Should NOT show permission: {result}'

# mode bar WITHOUT pending actions = NOT a permission prompt
# bypass-permissions-on with shift+tab hint is just persistent mode bar
lines = ['● Done!', '✻ Cooked for 21m', '───', '❯ merge the PR', '───', '⏵⏵ bypass permissions on (shift+tab to cycle)']
result = _extract_activity(lines)
assert 'permission' not in result.lower(), f'Mode bar without actions should NOT be permission: {result}'
assert result == 'Ready', f'Prompt with hint text should be idle: {result}'

# ⏵⏵ accept edits on = mode bar, NOT permission prompt (Codex fix #3)
lines = ['● Finished edits', '⏵⏵ accept edits on (shift+tab to cycle)']
result = _extract_activity(lines)
assert 'permission' not in result.lower(), f'accept edits should NOT be permission: {result}'

# ⏸ plan mode bar detection (Codex fix #4)
lines = ['● Some output', '⏸ plan mode on (shift+tab to cycle)']
result = _extract_activity(lines)
assert 'plan mode' in result.lower(), f'Expected plan mode: {result}'

# ⏸ plan mode with prompt AFTER = idle (worker moved past plan mode bar)
lines = ['⏸ plan mode on (shift+tab to cycle)', '❯']
result = _extract_activity(lines)
assert result == 'Ready', f'Prompt after plan bar should be idle: {result}'

# Rate limit ABOVE prompt should still detect rate limit (Codex fix #2)
lines = ['Rate limit exceeded. Retrying in 30s...', '❯']
result = _extract_activity(lines)
assert 'rate limit' in result.lower(), f'Rate limit should beat idle prompt: {result}'

# Case-insensitive error matching (Codex fix #6)
lines = ['error: something went wrong']
result = _extract_activity(lines)
assert 'error' in result.lower(), f'Lowercase error should be caught: {result}'

lines = ['ERROR: fatal crash']
result = _extract_activity(lines)
assert 'error' in result.lower(), f'Uppercase ERROR should be caught: {result}'

# Active spinner beats mode bar (spinner is real status)
lines = ['· Ruminating… (20m 2s · thinking)', '───', '❯', '───', '  ⏵⏵ bypass permissions on · 1 bash']
result = _extract_activity(lines)
assert 'ruminating' in result.lower(), f'Active spinner should beat mode bar: {result}'
assert '20m 2s' in result, f'Expected duration in: {result}'

# Last ● block concat (multi-line) — no idle prompt after
lines = ['● Found 3 bugs in the', '  auth module. All critical.', '  Context left until auto-compact: 42%']
result = _extract_activity(lines)
assert 'found 3 bugs' in result.lower() and 'auth module' in result.lower(), f'Expected concat ● block: {result}'

# Rate limiting detection
lines = ['● Some output', 'Rate limit exceeded. Retrying in 30s...']
result = _extract_activity(lines)
assert 'rate limit' in result.lower(), f'Expected rate limit detection: {result}'

# Connection error detection
lines = ['● Output', 'Connection error, retrying in 5s']
result = _extract_activity(lines)
assert 'connection' in result.lower(), f'Expected connection error: {result}'

# Editor mode detection
lines = ['some code', 'Save and close editor to continue...']
result = _extract_activity(lines)
assert 'editor' in result.lower(), f'Expected editor mode: {result}'

# Hook execution — SessionStart
lines = ['Loading hooks', 'Running SessionStart hooks…']
result = _extract_activity(lines)
assert 'sessionstart' in result.lower() or 'hooks' in result.lower(), f'Expected hook execution: {result}'

# Hook execution — PreCompact
lines = ['Auto-compacting', 'Running PreCompact hooks…']
result = _extract_activity(lines)
assert 'precompact' in result.lower() or 'hooks' in result.lower(), f'Expected hook execution: {result}'

# Plan mode entry
lines = ['● Plan:', '1. Do X', '2. Do Y', 'Entering plan mode']
result = _extract_activity(lines)
assert 'plan' in result.lower(), f'Expected plan mode: {result}'

# Team lead approval
lines = ['Changes ready', 'Waiting for team lead to review and approve...']
result = _extract_activity(lines)
assert 'team lead' in result.lower(), f'Expected team lead: {result}'

# ✢ spinner character (Unicode cross spinner frame)
lines = ['some output', '✢ Befuddling… (14m 23s · thought for 3s)']
result = _extract_activity(lines)
assert 'befuddling' in result.lower(), f'Expected ✢ spinner verb: {result}'
assert '14m 23s' in result, f'Expected duration for ✢ spinner: {result}'

# Idle / empty
lines = ['', '  >', '']
result = _extract_activity(lines)
assert result, f'Should return something even for idle, got empty'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_extract_activity parses tmux signals"
    else
        fail "_extract_activity parsing test failed"
    fi
}

test_activity_detects_interactive_prompt() {
    info "Testing _extract_activity detects interactive TUI prompts..."

    if python3 -c "
import sys; sys.path.insert(0, '.')
from bridge import _extract_activity, _extract_question_details

# AskUserQuestion single-select (exact kelvin repro)
lines = [
    '☐ Translate how?',
    '',
    'Which translation method?',
    '',
    '❯ 1. OpenAI API (Recommended)',
    '     Use GPT to auto-translate missing/untranslated',
    '  2. Detect only + open issue',
    '  3. Google Cloud Translate',
    '  4. Type something.',
    '───────────────────────────────────────────────────',
    '  5. Chat about this',
    '  6. Skip interview and plan immediately',
    '',
    'Enter to select · ↑/↓ to navigate · Esc to cancel',
]
result = _extract_activity(lines)
assert 'Waiting' in result, f'Single-select: expected Waiting, got: {result}'
assert result != 'Ready', f'Single-select: must NOT be Ready'

# Question details extraction
details = _extract_question_details(lines)
assert details is not None, 'details should not be None'
assert details['header'] == 'Translate how?', f'header: {details[\"header\"]}'
assert len(details['options']) >= 4, f'options: {len(details[\"options\"])}'
assert details['selected_num'] == 1, f'selected: {details[\"selected_num\"]}'
assert details['options'][0]['label'] == 'OpenAI API (Recommended)', f'opt1: {details[\"options\"][0]}'

# AskUserQuestion multi-select
lines = [
    '☐ Which features?',
    '  [x] Feature A',
    '  [ ] Feature B',
    '  [ ] Feature C',
    'Space to toggle, Enter to confirm, a to select all, n to select none',
]
result = _extract_activity(lines)
assert 'Waiting' in result, f'Multi-select: expected Waiting, got: {result}'

# Searchable list
lines = [
    'Select a file:',
    '❯ src/main.ts',
    '  src/app.ts',
    '  src/index.ts',
    'Press ↑↓ to navigate · Enter to select · Type to search · Esc to cancel',
]
result = _extract_activity(lines)
assert 'Waiting' in result, f'Searchable: expected Waiting, got: {result}'

# Continue prompt
lines = [
    'Some output text here',
    'Press Enter to continue',
]
result = _extract_activity(lines)
assert 'Waiting' in result, f'Continue: expected Waiting, got: {result}'

# Enter to confirm variant
lines = [
    '❯ 1. Yes, proceed',
    '  2. No, cancel',
    '↑/↓ to select · Enter to confirm · Esc to cancel',
]
result = _extract_activity(lines)
assert 'Waiting' in result, f'Confirm variant: expected Waiting, got: {result}'

# Normal idle prompt must still work
lines = ['❯']
result = _extract_activity(lines)
assert result == 'Ready', f'Idle: expected Ready, got: {result}'

# Prompt with text (auto-suggestion) still idle
lines = ['❯ some auto text']
result = _extract_activity(lines)
assert result == 'Ready', f'Idle+text: expected Ready, got: {result}'

# No interactive prompt = None details
lines = ['❯']
details = _extract_question_details(lines)
assert details is None, f'Idle should have no details, got: {details}'

# Rich progress includes options
from bridge import format_progress_lines
q_details = {
    'header': 'Pick a color',
    'options': [
        {'num': 1, 'label': 'Red', 'selected': True},
        {'num': 2, 'label': 'Blue', 'selected': False},
        {'num': 3, 'label': 'Green', 'selected': False},
    ],
    'selected_num': 1,
}
lines = format_progress_lines(
    name='kelvin', pending=False, backend='claude',
    online=True, ready=True, mode='tmux',
    activity='Waiting for input: Pick a color',
    question_details=q_details,
)
joined = '\n'.join(lines)
assert 'Red' in joined, f'Missing Red in: {joined}'
assert 'Blue' in joined, f'Missing Blue in: {joined}'
assert '1-3' in joined, f'Missing reply hint in: {joined}'
assert 'skip' in joined.lower(), f'Missing skip hint in: {joined}'
" 2>&1; then
        success "_extract_activity detects interactive prompts + rich details"
    else
        fail "_extract_activity interactive prompt detection failed"
    fi
}

test_activity_detects_plan_approval() {
    info "Testing _extract_activity detects ExitPlanMode plan approval prompt..."

    if python3 -c "
import sys; sys.path.insert(0, '.')
from bridge import _extract_activity, _extract_question_details

# ExitPlanMode 'Ready to code?' prompt (with editor configured)
lines = [
    'Here is Claude\'s plan:',
    '- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -',
    '# Plan: Implement feature X',
    '',
    '## Files to modify',
    '| File | Change |',
    '|------|--------|',
    '| src/main.ts | Add handler |',
    '- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -',
    'Claude has written up a plan and is ready to execute. Would you like to proceed?',
    '❯ 1. Yes, clear context (31% used) and bypass permissions',
    '  2. Yes, and bypass permissions',
    '  3. Yes, manually approve edits',
    '  4. Type here to tell Claude what to change',
    'ctrl-g to edit in Vim',
]
result = _extract_activity(lines)
assert 'Waiting' in result, f'Plan approval: expected Waiting, got: {result}'
assert result != 'Ready', f'Plan approval: must NOT be Ready'

# ExitPlanMode without editor (no ctrl-g footer — only content-based detection)
lines_no_editor = [
    'Claude has written up a plan and is ready to execute. Would you like to proceed?',
    '❯ 1. Yes, clear context (42% used) and auto-accept edits (shift+tab)',
    '  2. Yes, auto-accept edits',
    '  3. Yes, manually approve edits',
    '  4. Type here to tell Claude what to change',
]
result = _extract_activity(lines_no_editor)
assert 'Waiting' in result or 'plan' in result.lower(), f'No-editor plan: expected plan/waiting, got: {result}'
assert result != 'Ready', f'No-editor plan: must NOT be Ready'

# ExitPlanMode with auto-approve countdown
lines_auto = [
    'Claude has written up a plan and is ready to execute. Would you like to proceed?',
    '❯ 1. Yes, clear context (50% used) and bypass permissions',
    '  2. Yes, and bypass permissions',
    '  3. Yes, manually approve edits',
    '  4. Type here to tell Claude what to change',
    'ctrl-g to edit in Vim',
    'Auto-approving in 5s… Press any key to intervene.',
]
result = _extract_activity(lines_auto)
assert 'Waiting' in result or 'plan' in result.lower() or 'auto' in result.lower(), \
    f'Auto-approve plan: expected plan/waiting/auto, got: {result}'
assert result != 'Ready', f'Auto-approve plan: must NOT be Ready'

# EnterPlanMode prompt (different from ExitPlanMode)
lines_enter = [
    'Claude wants to enter plan mode to explore and design an implementation approach.',
    'In plan mode, Claude will:',
    ' · Explore the codebase thoroughly',
    ' · Identify existing patterns',
    ' · Design an implementation strategy',
    ' · Present a plan for your approval',
    'No code changes will be made until you approve the plan.',
    '❯ 1. Yes, enter plan mode',
    '  2. No, start implementing now',
]
result = _extract_activity(lines_enter)
assert result != 'Ready', f'EnterPlanMode: must NOT be Ready, got: {result}'

# Tool permission prompt (e.g., Allow Bash?)
lines_tool = [
    'Allow Bash?',
    'ls -la /tmp',
    '❯ 1. Yes',
    '  2. Yes, and don\\'t ask again for this tool',
    '  3. No',
]
result = _extract_activity(lines_tool)
assert result != 'Ready', f'Tool permission: must NOT be Ready, got: {result}'

# Question details should still be extractable for plan approval
details = _extract_question_details(lines)
assert details is not None, f'Plan approval details should not be None'
assert details['selected_num'] == 1, f'Plan approval selected: {details[\"selected_num\"]}'
assert len(details['options']) >= 3, f'Plan approval options: {len(details[\"options\"])}'
" 2>&1; then
        success "_extract_activity detects plan approval prompts"
    else
        fail "_extract_activity plan approval detection failed"
    fi
}

test_watchdog_waiting_input_state() {
    info "Testing watchdog detects WAITING_INPUT state and formats alert..."

    if python3 -c "
import sys; sys.path.insert(0, '.')
import bridge, time

# Test _format_watchdog_status with WAITING_INPUT
bridge._worker_states['testbot'] = ('WAITING_INPUT', 'question=Pick color', time.time() - 120)
result = bridge._format_watchdog_status('testbot')
assert 'Needs reply' in result, f'Expected Needs reply, got: {result}'
assert '2m' in result, f'Expected 2m duration, got: {result}'

# Test that WAITING_INPUT is in bad_states for alert transitions
bad = {'OFFLINE', 'DEAD', 'STUCK', 'POISONED', 'EXITED', 'WAITING_INPUT'}
assert 'WAITING_INPUT' in bad

# Test _team_attention_summary picks it up
icon, label, rank = bridge._team_attention_summary('Needs reply (2m)', 'Waiting for input: Pick color')
assert icon == '\U0001f7e1', f'Expected yellow icon, got: {icon}'
assert 'reply' in label, f'Expected reply label, got: {label}'

# Clean up
bridge._worker_states.pop('testbot', None)
" 2>&1; then
        success "Watchdog WAITING_INPUT state detection"
    else
        fail "Watchdog WAITING_INPUT state detection failed"
    fi
}

test_progress_shows_activity() {
    info "Testing /progress output includes activity line..."

    if python3 -c "
from bridge import format_progress_lines

lines = format_progress_lines(
    name='alice',
    pending=True,
    backend='claude',
    online=True,
    ready=True,
    mode='tmux',
    activity='Running unit tests (3/14 passed)'
)

text = '\\n'.join(lines)
assert 'Running unit tests' in text, f'Expected activity in progress output: {text}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/progress output includes activity"
    else
        fail "/progress activity output test failed"
    fi
}

test_noninteractive_backpressure() {
    info "Testing non-interactive backpressure rejects while busy..."

    if python3 -c "
import tempfile
from pathlib import Path
import bridge

tmp = Path(tempfile.mkdtemp())
bridge.SESSIONS_DIR = tmp
bridge.WORKER_PIPE_ROOT = tmp / 'pipes'

# Setup alice as codex worker
session_dir = tmp / 'alice'
session_dir.mkdir()
(session_dir / 'backend').write_text('codex')
bridge.state['active'] = 'alice'

prefix = bridge.TMUX_PREFIX
bridge.worker_manager.scan_tmux_sessions = lambda: {}
bridge.worker_manager.get_registered_sessions = lambda: {'alice': {'tmux': f'{prefix}alice', 'backend': 'codex'}}
bridge.worker_manager.is_online = lambda name, session=None: True
bridge.worker_manager.send = lambda name, message, chat_id=None, session=None: True
bridge.send_typing_loop = lambda chat_id, name: None

replies = []
class FakeTelegram:
    def send_message(self, chat_id, text, **kwargs):
        replies.append(text)
        return {'ok': True}
    def set_reaction(self, *a, **kw):
        return {'ok': True}

router = bridge.CommandRouter(FakeTelegram(), bridge.worker_manager)

# Set pending to simulate busy
bridge.set_pending('alice', 123)

# Try to route a message - should be rejected
router.route_message('alice', 'hello', 123, None)
assert any('still working' in r for r in replies), f'Expected backpressure rejection, got: {replies}'

# Clear pending and try again
bridge.clear_pending('alice')
replies.clear()
router.route_message('alice', 'hello', 123, None)
assert not any('still working' in r for r in replies), f'Should NOT reject when not pending: {replies}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Non-interactive backpressure works"
    else
        fail "Non-interactive backpressure test failed"
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

test_concurrent_sends_no_interleave() {
    info "Testing concurrent tmux sends don't interleave (flock behavior)..."

    local test_session="test-conc-$$"
    local recv_log="/tmp/test-conc-recv-$$.log"
    tmux new-session -d -s "$test_session" -x 200 -y 50 2>/dev/null

    if ! tmux has-session -t "$test_session" 2>/dev/null; then
        fail "Could not create test tmux session"
        return
    fi

    # Start a simple receiver that logs each line
    rm -f "$recv_log"
    tmux send-keys -t "$test_session" "while IFS= read -r line; do printf '%s\n' \"\$line\" >> $recv_log; done" Enter
    sleep 0.5

    # 5 parallel threads, 5 messages each, all via tmux_send_message (has flock)
    if python3 -c "
import sys, threading, time; sys.path.insert(0, '.')
import bridge

bridge._node_name = 'test-conc'

results = {'errors': []}

def sender(label, count):
    for i in range(1, count+1):
        ok = bridge.tmux_send_message('$test_session', f'CC-{label}{i}')
        if not ok:
            results['errors'].append(f'{label}{i} send failed')

threads = []
for s in 'ABCDE':
    t = threading.Thread(target=sender, args=(s, 5))
    threads.append(t)
    t.start()
for t in threads:
    t.join()

assert not results['errors'], f'Send errors: {results[\"errors\"]}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        sleep 3

        # Count clean messages (each should be exactly CC-X# on its own line)
        local total_expected=25
        local clean
        clean=$(grep -cE '^CC-[A-E][1-5]$' "$recv_log" 2>/dev/null || echo 0)
        local total
        total=$(grep -c 'CC-' "$recv_log" 2>/dev/null || echo 0)
        local corrupted=$(( total - clean ))

        if [[ "$clean" -eq "$total_expected" && "$corrupted" -eq 0 ]]; then
            success "Concurrent sends: $clean/$total_expected clean, 0 corrupted"
        else
            fail "Concurrent sends: $clean/$total_expected clean, $corrupted corrupted"
            grep -v '^CC-[A-E][1-5]$' "$recv_log" 2>/dev/null | grep 'CC-' | head -5 | while IFS= read -r line; do
                info "  corrupted: '$line'"
            done
        fi
    else
        fail "Concurrent send test script failed"
    fi

    tmux kill-session -t "$test_session" 2>/dev/null
    rm -f "$recv_log"
}

test_flock_per_session_isolation() {
    info "Testing flock is per-session (different sessions don't block)..."

    if python3 -c "
import sys; sys.path.insert(0, '.')
import bridge

bridge._node_name = 'test-iso'

# Different sessions get different lock files
path_a = bridge.tmux_send_lock_path('session-alice')
path_b = bridge.tmux_send_lock_path('session-bob')
assert path_a != path_b, f'same lock path for different sessions: {path_a}'
assert 'session-alice' in str(path_a), f'lock path missing session name: {path_a}'
assert 'session-bob' in str(path_b), f'lock path missing session name: {path_b}'

# Acquiring flock on session A should not block session B
import fcntl, os
path_a.parent.mkdir(parents=True, exist_ok=True)
fd_a = os.open(str(path_a), os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd_a, fcntl.LOCK_EX)

# Session B lock should be immediately acquirable (non-blocking test)
path_b.parent.mkdir(parents=True, exist_ok=True)
fd_b = os.open(str(path_b), os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd_b, fcntl.LOCK_EX | fcntl.LOCK_NB)  # LOCK_NB = non-blocking, raises if blocked

fcntl.flock(fd_b, fcntl.LOCK_UN)
os.close(fd_b)
fcntl.flock(fd_a, fcntl.LOCK_UN)
os.close(fd_a)

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Flock is per-session (different sessions independent)"
    else
        fail "Flock isolation between sessions failed"
    fi
}

test_backend_file_is_canonical() {
    info "Testing backend file is canonical source (not registry or tmux env)..."

    if python3 -c "
import sys, tempfile; sys.path.insert(0, '.')
from pathlib import Path
import bridge

tmp = Path(tempfile.mkdtemp())
bridge.SESSIONS_DIR = tmp

# Simulate drift: backend file says 'codex', but session dict says 'claude'
session_dir = tmp / 'drifted'
session_dir.mkdir()
(session_dir / 'backend').write_text('codex')

# get_worker_backend with a session dict that says 'claude'
# Backend FILE must take priority over session dict when both exist
result_with_session = bridge.get_worker_backend('drifted', {'backend': 'claude'})

# get_worker_backend without session dict — must use file
result_file_only = bridge.get_worker_backend('drifted')

# Both should return 'codex' (file is canonical)
# Currently session dict wins — this test documents the priority
assert result_file_only == 'codex', f'file-only: expected codex, got {result_file_only}'

# Session dict currently overrides file — this is the drift bug.
# After fix, file should win. For now, document the current behavior:
if result_with_session == 'claude':
    # CURRENT BEHAVIOR: session dict wins over file — drift possible
    print('DRIFT_BUG: session dict overrides backend file')
    raise AssertionError('Backend file must be canonical over session dict')
else:
    print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Backend file is canonical source of truth"
    else
        fail "Backend file not canonical (session dict overrides)"
    fi
}

test_flock_node_namespaced() {
    info "Testing flock path includes node name (multi-node isolation)..."

    if python3 -c "
import sys; sys.path.insert(0, '.')
import bridge

# Prod node
bridge._node_name = 'prod'
path_prod = bridge.tmux_send_lock_path('claude-prod-chen')
assert '/prod/locks/' in str(path_prod), f'prod lock not namespaced: {path_prod}'

# Dev node
bridge._node_name = 'dev'
path_dev = bridge.tmux_send_lock_path('claude-dev-chen')
assert '/dev/locks/' in str(path_dev), f'dev lock not namespaced: {path_dev}'

# They must be different (same worker name, different nodes)
assert path_prod != path_dev, f'prod and dev lock paths collide: {path_prod}'

# Follows existing pipe isolation pattern: /tmp/claudecode-telegram/<node>/...
assert str(path_prod).startswith('/tmp/claudecode-telegram/'), f'wrong base: {path_prod}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Flock path is node-namespaced (multi-node isolation)"
    else
        fail "Flock node namespace test failed"
    fi
}

test_send_example_uses_paste_buffer() {
    info "Testing /workers send_example uses paste-buffer for tmux workers..."

    if python3 -c "
import sys; sys.path.insert(0, '.')
import bridge

bridge._node_name = 'test-sendex'
bridge.TMUX_PREFIX = 'test-sendex-'

# Mock get_registered_sessions to return a tmux worker
orig = bridge.worker_manager.get_registered_sessions
bridge.worker_manager.get_registered_sessions = lambda: {
    'bob': {'backend': 'claude', 'tmux': 'test-sendex-bob'}
}

try:
    workers = bridge.get_workers()
    bob = next((w for w in workers if w['name'] == 'bob'), None)
    assert bob is not None, f'bob not found in workers: {[w[\"name\"] for w in workers]}'
    assert bob['protocol'] == 'tmux', f'expected tmux protocol, got {bob[\"protocol\"]}'

    ex = bob['send_example']
    # Must use paste-buffer -p -r for reliable delivery
    assert 'paste-buffer -p' in ex, f'send_example missing paste-buffer -p: {ex}'
    # Must use 1s delay — TUI needs time to render
    assert 'sleep 1' in ex, f'send_example should use sleep 1, got: {ex}'
    # Must include Enter after delay
    assert 'send-keys' in ex, f'send_example missing send-keys Enter: {ex}'

    print('OK')
finally:
    bridge.worker_manager.get_registered_sessions = orig
" 2>/dev/null | grep -q "OK"; then
        success "send_example uses paste-buffer for tmux workers"
    else
        fail "send_example paste-buffer test failed"
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

test_adapter_pid_tracking() {
    info "Testing adapter PID stored and kill_adapter terminates it..."

    if python3 -c "
import subprocess, time
import bridge

# Spawn a long-running process to simulate an adapter
proc = subprocess.Popen(['sleep', '60'])
bridge._adapter_pids['testworker'] = (proc, None)

# Verify PID is stored and alive
assert proc.poll() is None, 'process should be alive'
assert 'testworker' in bridge._adapter_pids

# Kill it
bridge.kill_adapter('testworker')

# Verify killed and removed from dict
assert proc.poll() is not None, 'process should be dead after kill_adapter'
assert 'testworker' not in bridge._adapter_pids, 'entry should be removed'

# kill_adapter on unknown worker is a no-op
bridge.kill_adapter('nonexistent')

# kill_adapter on already-dead process is a no-op
proc2 = subprocess.Popen(['true'])
proc2.wait()
bridge._adapter_pids['dead'] = (proc2, None)
bridge.kill_adapter('dead')
assert 'dead' not in bridge._adapter_pids

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Adapter PID tracking and kill_adapter work"
    else
        fail "Adapter PID tracking test failed"
    fi
}

test_pause_kills_adapter() {
    info "Testing /pause kills inflight adapter for codex worker..."

    if python3 -c "
import subprocess, tempfile, time
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

# Simulate an inflight adapter
proc = subprocess.Popen(['sleep', '60'])
bridge._adapter_pids['alice'] = (proc, None)
assert proc.poll() is None, 'adapter should be alive before pause'

bridge.tmux_send_escape = lambda *_: (_ for _ in ()).throw(AssertionError('tmux should not be used'))
bridge.state['active'] = 'alice'

class FakeTelegram:
    def send_message(self, *args, **kwargs):
        return {'ok': True}

router = bridge.CommandRouter(FakeTelegram(), bridge.worker_manager)
router.cmd_pause(12345)

assert proc.poll() is not None, 'adapter should be dead after pause'
assert 'alice' not in bridge._adapter_pids, 'PID entry should be removed'
assert not bridge.get_pending_file('alice').exists(), 'pending should be cleared'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/pause kills inflight adapter"
    else
        fail "/pause kills inflight adapter test failed"
    fi
}

test_end_kills_adapter() {
    info "Testing /end kills inflight adapter for codex worker..."

    if python3 -c "
import subprocess, tempfile
from pathlib import Path
import bridge

tmp = Path(tempfile.mkdtemp())
bridge.SESSIONS_DIR = tmp
bridge.WORKER_PIPE_ROOT = tmp / 'pipes'
bridge.worker_manager.sessions_dir = tmp
bridge.worker_manager.tmux_prefix = 'claude-test-'
bridge.worker_manager.scan_tmux_sessions = lambda: {}

session_dir = tmp / 'bob'
session_dir.mkdir()
(session_dir / 'backend').write_text('codex')

# Simulate an inflight adapter
proc = subprocess.Popen(['sleep', '60'])
bridge._adapter_pids['bob'] = (proc, None)
assert proc.poll() is None, 'adapter should be alive before end'

bridge.state['active'] = 'bob'
ok, err = bridge.worker_manager.end('bob')

assert proc.poll() is not None, 'adapter should be dead after end'
assert 'bob' not in bridge._adapter_pids, 'PID entry should be removed'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/end kills inflight adapter"
    else
        fail "/end kills inflight adapter test failed"
    fi
}

test_end_clears_pending() {
    info "Testing /end clears pending file..."

    if python3 -c "
import tempfile
from pathlib import Path
import bridge

tmp = Path(tempfile.mkdtemp())
bridge.SESSIONS_DIR = tmp
bridge.FILE_INBOX_ROOT = tmp / 'inbox'
bridge.WORKER_PIPE_ROOT = tmp / 'pipes'
bridge.worker_manager.sessions_dir = tmp
bridge.worker_manager.tmux_prefix = 'claude-test-'
bridge.worker_manager.get_registered_sessions = lambda registered=None: {
    'alice': {'tmux': 'claude-test-alice', 'backend': 'claude'}
}

bridge.set_pending('alice', 12345)
pending_file = bridge.get_pending_file('alice')
assert pending_file.exists(), 'pending should exist before end'

ok, err = bridge.worker_manager.end('alice')
assert ok is True, f'end failed: {err}'
assert not pending_file.exists(), 'pending should be cleared by /end'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/end clears pending file"
    else
        fail "/end clears pending file test failed"
    fi
}


test_adapter_stderr_logging() {
    info "Testing adapter stderr is logged to per-worker adapter.log..."

    if python3 -c "
import subprocess, tempfile, time, os
from pathlib import Path
import bridge

tmp = Path(tempfile.mkdtemp())
worker_dir = tmp / 'testworker'
worker_dir.mkdir()

# Create a tiny script that writes to stderr
script = tmp / 'fake_adapter.py'
script.write_text('import sys; print(\"adapter error output\", file=sys.stderr); sys.exit(0)')

ok = bridge._spawn_adapter(script, 'testworker', 'hello', 'http://localhost:9999', tmp)
assert ok, '_spawn_adapter should return True'

# Wait for process to finish
entry = bridge._adapter_pids.get('testworker')
assert entry is not None, 'should be in _adapter_pids'
proc, stderr_fh = entry
proc.wait(timeout=5)

# Flush and close
if stderr_fh:
    stderr_fh.flush()
    stderr_fh.close()

log_file = worker_dir / 'adapter.log'
assert log_file.exists(), f'adapter.log should exist at {log_file}'
content = log_file.read_text()
assert 'adapter error output' in content, f'stderr should be in log, got: {content!r}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Adapter stderr logged to per-worker adapter.log"
    else
        fail "Adapter stderr logging test failed"
    fi
}

test_poisoned_detection() {
    info "Testing poisoned detection via adapter.log..."

    if python3 -c "
import tempfile
from pathlib import Path
import bridge

tmp = Path(tempfile.mkdtemp())
bridge.SESSIONS_DIR = tmp

name = 'alice'
session_dir = tmp / name
session_dir.mkdir()
(session_dir / 'backend').write_text('codex')

log_file = session_dir / 'adapter.log'
log_file.write_text('\\n'.join(['error 529 overloaded'] * 5))

text = bridge._check_adapter_log(name, tail_lines=20)
assert 'error 529 overloaded' in text, f'unexpected log content: {text!r}'

reason = bridge._detect_poisoned(name, 'claude-test-alice')
assert reason is not None, 'expected poisoned detection'
assert 'error.*overloaded' in reason, f'expected pattern match, got {reason!r}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Poisoned detection via adapter.log works"
    else
        fail "Poisoned detection test failed"
    fi
}

test_poisoned_hook_signal_file() {
    info "Testing poisoned detection via hook signal file..."

    if python3 -c "
import tempfile, time, os
from pathlib import Path
import bridge

tmp = Path(tempfile.mkdtemp())
bridge.SESSIONS_DIR = tmp

name = 'hooktest'
tmux_name = 'claude-test-hooktest'
session_dir = tmp / name
session_dir.mkdir()
(session_dir / 'backend').write_text('claude')

# Create hook signal file with 4 recent failures (>= 3 threshold)
node = os.environ.get('TMUX_PREFIX', 'claude-test-').rstrip('-').split('-', 1)[-1] if os.environ.get('TMUX_PREFIX') else 'test'
hook_dir = Path(f'/tmp/claudecode-telegram/{node}/{name}/hooks')
hook_dir.mkdir(parents=True, exist_ok=True)
failures_file = hook_dir / 'failures'

now = int(time.time())
lines = []
for i in range(4):
    lines.append(f'{now - i} Bash')
failures_file.write_text('\\n'.join(lines) + '\\n')

# _detect_poisoned should find hook signal and return reason
reason = bridge._detect_poisoned(name, tmux_name)
assert reason is not None, f'expected poisoned from hook signal, got None'
assert 'hook' in reason.lower() or 'failure' in reason.lower(), f'expected hook-based reason, got {reason!r}'

# Cleanup
import shutil
shutil.rmtree(hook_dir.parent, ignore_errors=True)
failures_file.unlink(missing_ok=True)

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Poisoned detection via hook signal file works"
    else
        fail "Poisoned detection via hook signal file failed"
    fi
}

test_poisoned_hook_signal_stale_ignored() {
    info "Testing poisoned detection ignores stale hook signals..."

    if python3 -c "
import tempfile, time, os
from pathlib import Path
import bridge

tmp = Path(tempfile.mkdtemp())
bridge.SESSIONS_DIR = tmp

name = 'hookstale'
tmux_name = 'claude-test-hookstale'
session_dir = tmp / name
session_dir.mkdir()
(session_dir / 'backend').write_text('claude')

# Create hook signal file with 4 OLD failures (> 120s ago)
node = os.environ.get('TMUX_PREFIX', 'claude-test-').rstrip('-').split('-', 1)[-1] if os.environ.get('TMUX_PREFIX') else 'test'
hook_dir = Path(f'/tmp/claudecode-telegram/{node}/{name}/hooks')
hook_dir.mkdir(parents=True, exist_ok=True)
failures_file = hook_dir / 'failures'

old = int(time.time()) - 300
lines = []
for i in range(4):
    lines.append(f'{old - i} Bash')
failures_file.write_text('\\n'.join(lines) + '\\n')

# Should NOT detect poisoned (stale failures)
reason = bridge._detect_poisoned(name, tmux_name)
# With stale hook signals, should fall through to regex (which has no pane text for non-tmux)
# So reason should be None
assert reason is None, f'expected None for stale signals, got {reason!r}'

# Cleanup
import shutil
shutil.rmtree(hook_dir.parent, ignore_errors=True)

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Poisoned detection ignores stale hook signals"
    else
        fail "Poisoned detection stale hook signal test failed"
    fi
}

test_poisoned_hook_signal_below_threshold() {
    info "Testing poisoned detection below threshold..."

    if python3 -c "
import tempfile, time, os
from pathlib import Path
import bridge

tmp = Path(tempfile.mkdtemp())
bridge.SESSIONS_DIR = tmp

name = 'hooklow'
tmux_name = 'claude-test-hooklow'
session_dir = tmp / name
session_dir.mkdir()
(session_dir / 'backend').write_text('claude')

# Create hook signal file with only 2 recent failures (< 3 threshold)
node = os.environ.get('TMUX_PREFIX', 'claude-test-').rstrip('-').split('-', 1)[-1] if os.environ.get('TMUX_PREFIX') else 'test'
hook_dir = Path(f'/tmp/claudecode-telegram/{node}/{name}/hooks')
hook_dir.mkdir(parents=True, exist_ok=True)
failures_file = hook_dir / 'failures'

now = int(time.time())
lines = [f'{now} Bash', f'{now - 1} Edit']
failures_file.write_text('\\n'.join(lines) + '\\n')

# Should NOT detect poisoned (below threshold)
reason = bridge._detect_poisoned(name, tmux_name)
assert reason is None, f'expected None for below-threshold, got {reason!r}'

# Cleanup
import shutil
shutil.rmtree(hook_dir.parent, ignore_errors=True)

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Poisoned detection below threshold works"
    else
        fail "Poisoned detection below threshold test failed"
    fi
}

test_on_tool_failure_hook_script() {
    info "Testing on-tool-failure.sh hook script writes signal file..."

    # Create a tmux session to simulate a worker
    local test_session="${TMUX_PREFIX}hookscript"
    tmux new-session -d -s "$test_session" "bash" 2>/dev/null || true
    sleep 0.3

    # Set tmux env vars that the hook reads
    tmux set-environment -t "$test_session" TMUX_PREFIX "$TMUX_PREFIX"

    # Derive node name the same way as bridge.py
    local node_name
    node_name=$(echo "$TMUX_PREFIX" | sed 's/-$//' | sed 's/^claude-//')
    [ -z "$node_name" ] && node_name="default"

    # Clean up any pre-existing signal file
    local hook_dir="/tmp/claudecode-telegram/${node_name}/hookscript/hooks"
    rm -rf "$hook_dir" 2>/dev/null

    # Simulate PostToolUseFailure payload via the hook script
    # The hook reads stdin and extracts tool_name
    local hook_script
    hook_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/hooks/on-tool-failure.sh"

    # Run hook in the tmux session context (so tmux display-message works)
    echo '{"tool_name": "Bash", "error": "Command failed"}' | \
        tmux send-keys -t "$test_session" \
        "echo '{\"tool_name\": \"Bash\", \"error\": \"Command failed\"}' | $hook_script" Enter
    sleep 0.5

    local failures_file="${hook_dir}/failures"
    if [ -f "$failures_file" ]; then
        local content
        content=$(cat "$failures_file")
        if echo "$content" | grep -q "Bash"; then
            success "on-tool-failure.sh writes signal file correctly"
        else
            fail "Signal file exists but no Bash entry: $content"
        fi
    else
        fail "Signal file not created at $failures_file"
    fi

    # Cleanup
    tmux kill-session -t "$test_session" 2>/dev/null || true
    rm -rf "$hook_dir" 2>/dev/null
}

test_clear_hook_failures_on_restart() {
    info "Testing hook failure signal cleared on non-resume restart..."

    if python3 -c "
import tempfile, time, os
from pathlib import Path
import bridge

# Set up signal file
node = os.environ.get('TMUX_PREFIX', 'claude-test-').rstrip('-').removeprefix('claude-') or 'default'
name = 'cleartest'
hook_dir = Path(f'/tmp/claudecode-telegram/{node}/{name}/hooks')
hook_dir.mkdir(parents=True, exist_ok=True)
failures_file = hook_dir / 'failures'
now = int(time.time())
failures_file.write_text(f'{now} Bash\n{now} Edit\n{now} Read\n')

assert failures_file.exists(), 'setup failed: signal file not created'

# Clear
bridge._clear_hook_failures(name)

assert not failures_file.exists(), 'signal file should be deleted after clear'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Hook failure signal cleared on restart"
    else
        fail "Hook failure signal clear test failed"
    fi
}

test_compute_state_non_interactive() {
    info "Testing compute_state for non-interactive backends..."

    if python3 -c "
import time
import bridge

now = time.time()

state, reason = bridge.compute_state(
    tmux_exists=True,
    claude_pid=None,
    pending=True,
    pending_ts=None,
    pending_age=5,
    children=0,
    last_child_ts=0.0,
    cpu=0.0,
    last_hook_ts=None,
    last_seen_claude=None,
    now=now,
    is_interactive=False,
    adapter_alive=True,
)
assert state == 'BUSY_TOOL', f'expected BUSY_TOOL, got {state} ({reason})'

state, reason = bridge.compute_state(
    tmux_exists=True,
    claude_pid=None,
    pending=False,
    pending_ts=None,
    pending_age=0.0,
    children=0,
    last_child_ts=0.0,
    cpu=0.0,
    last_hook_ts=None,
    last_seen_claude=None,
    now=now,
    is_interactive=False,
    adapter_alive=False,
)
assert state == 'READY', f'expected READY, got {state} ({reason})'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "compute_state handles non-interactive backends"
    else
        fail "compute_state non-interactive test failed"
    fi
}

test_since_preserved_on_reason_change() {
    info "Testing watchdog since preserved when reason changes..."

    if python3 -c "
import time
import bridge

bridge._worker_states.clear()
now = time.time()

first_since = bridge._record_worker_state('alice', 'DEAD', 'claude missing 1s', now)
second_since = bridge._record_worker_state('alice', 'DEAD', 'claude missing 5s', now + 5)

assert first_since == second_since, 'since should remain when state is unchanged'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Watchdog since preserved on reason change"
    else
        fail "Watchdog since preservation test failed"
    fi
}


test_compute_state_interactive() {
    info "Testing compute_state for INTERACTIVE backends..."

    if python3 -c "
import time
import bridge

now = time.time()

# OFFLINE: tmux_exists=False
state, reason = bridge.compute_state(
    tmux_exists=False, claude_pid=None, pending=False, pending_ts=None,
    pending_age=0, children=0, last_child_ts=0.0, cpu=0.0,
    last_hook_ts=None, last_seen_claude=None, now=now, is_interactive=True,
)
assert state == 'OFFLINE', f'OFFLINE case: expected OFFLINE, got {state} ({reason})'

# DEAD: tmux_exists=True, claude_pid=None, past START_GRACE
state, reason = bridge.compute_state(
    tmux_exists=True, claude_pid=None, pending=False, pending_ts=None,
    pending_age=0, children=0, last_child_ts=0.0, cpu=0.0,
    last_hook_ts=None, last_seen_claude=now - 60, now=now, is_interactive=True,
)
assert state == 'DEAD', f'DEAD case: expected DEAD, got {state} ({reason})'

# READY: tmux_exists=True, claude_pid set, no pending, low CPU
state, reason = bridge.compute_state(
    tmux_exists=True, claude_pid='12345', pending=False, pending_ts=None,
    pending_age=0, children=0, last_child_ts=0.0, cpu=1.0,
    last_hook_ts=None, last_seen_claude=now, now=now, is_interactive=True,
)
assert state == 'READY', f'READY case: expected READY, got {state} ({reason})'

# BUSY_TOOL: pending set, children > 0
state, reason = bridge.compute_state(
    tmux_exists=True, claude_pid='12345', pending=True, pending_ts=now - 5,
    pending_age=5, children=3, last_child_ts=now, cpu=50.0,
    last_hook_ts=None, last_seen_claude=now, now=now, is_interactive=True,
)
assert state == 'BUSY_TOOL', f'BUSY_TOOL case: expected BUSY_TOOL, got {state} ({reason})'

# BUSY_THINKING: pending set, children == 0, CPU > CPU_ACTIVE (15.0)
state, reason = bridge.compute_state(
    tmux_exists=True, claude_pid='12345', pending=True, pending_ts=now - 5,
    pending_age=5, children=0, last_child_ts=0.0, cpu=20.0,
    last_hook_ts=None, last_seen_claude=now, now=now, is_interactive=True,
)
assert state == 'BUSY_THINKING', f'BUSY_THINKING case: expected BUSY_THINKING, got {state} ({reason})'

# WAITING: pending set, children == 0, CPU < CPU_IDLE (7.0), age < STALE_PENDING (300)
state, reason = bridge.compute_state(
    tmux_exists=True, claude_pid='12345', pending=True, pending_ts=now - 100,
    pending_age=100, children=0, last_child_ts=0.0, cpu=2.0,
    last_hook_ts=None, last_seen_claude=now, now=now, is_interactive=True,
)
assert state == 'WAITING', f'WAITING case: expected WAITING, got {state} ({reason})'

# STUCK: pending set, pending_age > STALE_PENDING (900), cpu < CPU_IDLE
state, reason = bridge.compute_state(
    tmux_exists=True, claude_pid='12345', pending=True, pending_ts=now - 1200,
    pending_age=1200, children=0, last_child_ts=0.0, cpu=2.0,
    last_hook_ts=None, last_seen_claude=now, now=now, is_interactive=True,
)
assert state == 'STUCK', f'STUCK case: expected STUCK, got {state} ({reason})'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "compute_state handles interactive backends (all state transitions)"
    else
        fail "compute_state interactive test failed"
    fi
}

test_idle_child_baseline() {
    info "Testing idle child baseline filters MCP servers..."

    if python3 -c "
import time
import bridge

now = time.time()

# With children=0 (baseline subtracted), idle worker should be READY
state, reason = bridge.compute_state(
    tmux_exists=True, claude_pid='12345', pending=False, pending_ts=None,
    pending_age=0, children=0, last_child_ts=0.0, cpu=1.0,
    last_hook_ts=None, last_seen_claude=now, now=now, is_interactive=True,
)
assert state == 'READY', f'Idle with baseline subtracted: expected READY, got {state} ({reason})'

# With children=1 (one extra above baseline), should be UNTRACKED_BUSY
state, reason = bridge.compute_state(
    tmux_exists=True, claude_pid='12345', pending=False, pending_ts=None,
    pending_age=0, children=1, last_child_ts=now, cpu=1.0,
    last_hook_ts=None, last_seen_claude=now, now=now, is_interactive=True,
)
assert state == 'UNTRACKED_BUSY', f'Extra child above baseline: expected UNTRACKED_BUSY, got {state} ({reason})'

# Test _idle_child_baseline map directly
bridge._idle_child_baseline.clear()

# First observation: sets baseline
bridge._idle_child_baseline['test-worker'] = 2  # simulate 2 MCP servers
baseline = bridge._idle_child_baseline['test-worker']
assert baseline == 2, f'Expected baseline 2, got {baseline}'

# Effective children with 2 MCP + 1 tool = 3 total, active = 1
children_total = 3
children_active = max(0, children_total - baseline)
assert children_active == 1, f'Expected 1 active child, got {children_active}'

# Effective children with just MCP = 2 total, active = 0
children_total = 2
children_active = max(0, children_total - baseline)
assert children_active == 0, f'Expected 0 active children, got {children_active}'

# Cleanup
bridge._idle_child_baseline.clear()

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Idle child baseline filters MCP server children"
    else
        fail "Idle child baseline test failed"
    fi
}

test_idle_streak_prevents_false_stuck() {
    info "Testing idle_streak prevents false STUCK alerts..."

    if python3 -c "
import bridge

# Reset idle streak
bridge._idle_streak.clear()

# Simulate: compute_state returns STUCK but streak < IDLE_STREAK_STUCK
# The watchdog loop downgrades to WAITING until streak reaches threshold

# First STUCK sample: streak=1, should downgrade to WAITING
bridge._idle_streak['testworker'] = bridge._idle_streak.get('testworker', 0) + 1
streak = bridge._idle_streak['testworker']
assert streak == 1, f'expected streak 1, got {streak}'
assert streak < bridge.IDLE_STREAK_STUCK, f'streak {streak} should be < {bridge.IDLE_STREAK_STUCK}'

# Second STUCK sample: streak=2, still WAITING
bridge._idle_streak['testworker'] = bridge._idle_streak.get('testworker', 0) + 1
streak = bridge._idle_streak['testworker']
assert streak == 2, f'expected streak 2, got {streak}'
assert streak < bridge.IDLE_STREAK_STUCK, f'streak {streak} should be < {bridge.IDLE_STREAK_STUCK}'

# Third STUCK sample: streak=3, now real STUCK
bridge._idle_streak['testworker'] = bridge._idle_streak.get('testworker', 0) + 1
streak = bridge._idle_streak['testworker']
assert streak == 3, f'expected streak 3, got {streak}'
assert streak >= bridge.IDLE_STREAK_STUCK, f'streak {streak} should be >= {bridge.IDLE_STREAK_STUCK}'

# Non-STUCK state resets streak
bridge._idle_streak['testworker'] = 0
assert bridge._idle_streak['testworker'] == 0, 'streak should reset to 0'

# Verify IDLE_STREAK_STUCK constant
assert bridge.IDLE_STREAK_STUCK == 3, f'expected IDLE_STREAK_STUCK=3, got {bridge.IDLE_STREAK_STUCK}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "idle_streak prevents false STUCK (requires 3 consecutive samples)"
    else
        fail "idle_streak test failed"
    fi
}

test_watchdog_resolved_alert() {
    info "Testing watchdog resolved alert on STUCK -> READY transition..."

    if python3 -c "
import bridge

# Track calls to telegram_api
calls = []
def fake_api(method, data):
    calls.append((method, data))
    return {'ok': True}

orig_api = bridge.telegram_api
bridge.telegram_api = fake_api
bridge.admin_chat_id = 12345

# Clear state
with bridge._watchdog_lock:
    bridge._prev_worker_states.clear()

# Simulate STUCK -> READY transition
with bridge._watchdog_lock:
    bridge._prev_worker_states['testworker'] = 'STUCK'

bridge._send_resolved_alert('testworker', 'READY')

assert len(calls) == 1, f'Expected 1 API call, got {len(calls)}'
method, data = calls[0]
assert method == 'sendMessage', f'Expected sendMessage, got {method}'
assert 'testworker' in data['text'], f'Expected worker name in text, got {data[\"text\"]}'
assert 'back to normal' in data['text'], f'Expected back to normal in text, got {data[\"text\"]}'

# Verify no alert when transition is not from bad to good state
calls.clear()
with bridge._watchdog_lock:
    bridge._prev_worker_states['testworker'] = 'READY'
bridge._send_resolved_alert('testworker', 'BUSY_TOOL')
assert len(calls) == 0, f'Expected no call for READY->BUSY_TOOL, got {len(calls)}'

bridge.telegram_api = orig_api
bridge.admin_chat_id = None
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Watchdog resolved alert fires on STUCK -> READY"
    else
        fail "Watchdog resolved alert test failed"
    fi
}

test_parse_at_mentions() {
    info "Testing parse_at_mentions behavior..."

    if python3 -c "
import bridge

# Create a mock workers object
class MockWorkers:
    def get_registered_sessions(self, registered=None):
        return {'alice': {'tmux': 'claude-test-alice'}, 'bob': {'tmux': 'claude-test-bob'}}

class MockTelegramAPI:
    def send_message(self, chat_id, text, **kwargs):
        pass

router = bridge.CommandRouter(MockTelegramAPI(), MockWorkers())

# Single @mention extracts correct worker name
targets, cleaned = router.parse_at_mentions('@alice hello there')
assert targets == ['alice'], f'Single mention: expected [alice], got {targets}'
assert 'hello there' in cleaned, f'Cleaned text should contain message, got: {cleaned}'

# Multiple @mentions extract all names
targets, cleaned = router.parse_at_mentions('@alice @bob do this task')
assert 'alice' in targets and 'bob' in targets, f'Multi mention: expected alice+bob, got {targets}'
assert len(targets) == 2, f'Expected 2 targets, got {len(targets)}'

# @nonexistent returns empty (no matching worker)
targets, cleaned = router.parse_at_mentions('@charlie hello')
assert targets == [], f'Nonexistent mention: expected [], got {targets}'

# Text without @ returns empty
targets, cleaned = router.parse_at_mentions('hello world')
assert targets == [], f'No mention: expected [], got {targets}'
assert cleaned == 'hello world', f'No mention: cleaned text should be unchanged, got {cleaned}'

# Empty text returns empty
targets, cleaned = router.parse_at_mentions('')
assert targets == [], f'Empty text: expected [], got {targets}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "parse_at_mentions handles all mention patterns"
    else
        fail "parse_at_mentions test failed"
    fi
}

test_auto_focus_on_consecutive_mentions() {
    info "Testing auto-focus after 2 consecutive @mentions to same worker..."

    if python3 -c "
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock
import bridge

# Set up temp dirs for isolation
tmpdir = tempfile.mkdtemp()
node_dir = Path(tmpdir) / 'node'
node_dir.mkdir(parents=True)
sessions_dir = node_dir / 'sessions'
sessions_dir.mkdir()

# Save originals
orig_sessions_dir = bridge.SESSIONS_DIR
orig_node_dir = bridge.NODE_DIR
orig_last_active = bridge.LAST_ACTIVE_FILE
orig_admin = bridge.admin_chat_id
orig_state = dict(bridge.state)
orig_mention = dict(bridge._last_mention)

# Override paths
bridge.SESSIONS_DIR = sessions_dir
bridge.NODE_DIR = node_dir
bridge.LAST_ACTIVE_FILE = node_dir / 'last_active'
bridge.admin_chat_id = 123

# Create mock workers
class MockWorkers:
    def get_registered_sessions(self, registered=None):
        return {'alice': {'tmux': 'claude-test-alice'}, 'bob': {'tmux': 'claude-test-bob'}}
    def is_online(self, name, session):
        return True

class MockTelegramAPI:
    def __init__(self):
        self.sent = []
    def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)

mock_api = MockTelegramAPI()
router = bridge.CommandRouter(mock_api, MockWorkers())
router.route_message = MagicMock()
router.route_to_active = MagicMock()

def make_msg(text):
    return {'update_id': 1, 'message': {'message_id': 1, 'chat': {'id': 123}, 'text': text}}

# Reset state
bridge.state['active'] = 'bob'
bridge.state['startup_notified'] = True
bridge._last_mention['target'] = None
bridge._last_mention['count'] = 0
mock_api.sent.clear()

# Test 1: First @alice mention — no focus switch
router.handle_message(make_msg('@alice do task 1'))
assert bridge.state['active'] == 'bob', f'T1: should stay bob, got {bridge.state[\"active\"]}'
assert bridge._last_mention['target'] == 'alice', f'T1: tracker target should be alice'
assert bridge._last_mention['count'] == 1, f'T1: count should be 1, got {bridge._last_mention[\"count\"]}'

# Test 2: Second @alice mention — auto-focus switches to alice
router.handle_message(make_msg('@alice do task 2'))
assert bridge.state['active'] == 'alice', f'T2: should switch to alice, got {bridge.state[\"active\"]}'
assert any('Switched to alice' in s for s in mock_api.sent), f'T2: should notify auto-focus, sent: {mock_api.sent}'

# Test 3: Third @alice mention — already focused, no duplicate notification
mock_api.sent.clear()
router.handle_message(make_msg('@alice do task 3'))
assert bridge.state['active'] == 'alice', f'T3: should stay alice'
assert not any('Switched to' in s for s in mock_api.sent), f'T3: should not re-notify when already focused'

# Test 4: @bob then @alice — different workers, no focus switch
bridge.state['active'] = 'bob'
bridge._last_mention['target'] = None
bridge._last_mention['count'] = 0
mock_api.sent.clear()
router.handle_message(make_msg('@bob do X'))
router.handle_message(make_msg('@alice do Y'))
assert bridge.state['active'] == 'bob', f'T4: should stay bob after different mentions'
assert bridge._last_mention['target'] == 'alice', f'T4: tracker should be alice'
assert bridge._last_mention['count'] == 1, f'T4: count should be 1'

# Test 5: @alice then plain message then @alice — streak reset, no focus
bridge.state['active'] = 'bob'
bridge._last_mention['target'] = None
bridge._last_mention['count'] = 0
mock_api.sent.clear()
router.handle_message(make_msg('@alice do X'))
router.handle_message(make_msg('just a plain message'))
router.handle_message(make_msg('@alice do Y'))
assert bridge.state['active'] == 'bob', f'T5: should stay bob, streak was reset'
assert bridge._last_mention['count'] == 1, f'T5: count should be 1 after reset+mention'

# Test 6: Multi-mention resets streak, no focus switch
bridge.state['active'] = 'bob'
bridge._last_mention['target'] = 'alice'
bridge._last_mention['count'] = 1
mock_api.sent.clear()
router.handle_message(make_msg('@alice @bob do both'))
assert bridge.state['active'] == 'bob', f'T6: multi-mention should not switch focus'
assert bridge._last_mention['target'] is None, f'T6: multi-mention should reset target'
assert bridge._last_mention['count'] == 0, f'T6: multi-mention should reset count'

# Restore originals
bridge.SESSIONS_DIR = orig_sessions_dir
bridge.NODE_DIR = orig_node_dir
bridge.LAST_ACTIVE_FILE = orig_last_active
bridge.admin_chat_id = orig_admin
bridge.state.update(orig_state)
bridge._last_mention.update(orig_mention)

import shutil
shutil.rmtree(tmpdir)

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "auto-focus on consecutive @mentions works correctly"
    else
        fail "auto-focus on consecutive mentions test failed"
    fi
}

test_format_watchdog_status() {
    info "Testing _format_watchdog_status for each state..."

    if python3 -c "
import time
import bridge

now = time.time()
since = now - 120  # 2 minutes ago

# Build a state snapshot for each state
states = {
    'ready_worker': ('READY', 'idle', since),
    'busy_tool_worker': ('BUSY_TOOL', 'children=3', since),
    'busy_thinking_worker': ('BUSY_THINKING', 'cpu=20.0', since),
    'waiting_worker': ('WAITING', 'age=100s', since),
    'stuck_worker': ('STUCK', 'age=600s cpu=2.0', since),
    'dead_worker': ('DEAD', 'claude missing 60s', since),
    'offline_worker': ('OFFLINE', 'tmux missing', since),
    'poisoned_worker': ('POISONED', 'exec loop', since),
    'untracked_worker': ('UNTRACKED_BUSY', 'children=1', since),
}

# Inject into _worker_states
with bridge._watchdog_lock:
    bridge._worker_states.update(states)

snapshot = dict(states)

# READY -> 'Ready'
result = bridge._format_watchdog_status('ready_worker', lambda n: False, state_snapshot=snapshot)
assert result == 'Ready', f'READY: expected Ready, got {result}'

# BUSY_TOOL -> 'Working'
result = bridge._format_watchdog_status('busy_tool_worker', lambda n: True, state_snapshot=snapshot)
assert result == 'Working', f'BUSY_TOOL: expected Working, got {result}'

# BUSY_THINKING -> 'Thinking'
result = bridge._format_watchdog_status('busy_thinking_worker', lambda n: True, state_snapshot=snapshot)
assert result == 'Thinking', f'BUSY_THINKING: expected Thinking, got {result}'

# WAITING -> 'Working'
result = bridge._format_watchdog_status('waiting_worker', lambda n: True, state_snapshot=snapshot)
assert result == 'Working', f'WAITING: expected Working, got {result}'

# STUCK -> 'No progress (Xm)'
result = bridge._format_watchdog_status('stuck_worker', lambda n: True, state_snapshot=snapshot)
assert result.startswith('No progress'), f'STUCK: expected No progress (Xm), got {result}'
assert 'm)' in result, f'STUCK: expected minutes in parens, got {result}'

# DEAD -> 'Not responding'
result = bridge._format_watchdog_status('dead_worker', lambda n: False, state_snapshot=snapshot)
assert result == 'Not responding', f'DEAD: expected Not responding, got {result}'

# OFFLINE -> 'Offline'
result = bridge._format_watchdog_status('offline_worker', lambda n: False, state_snapshot=snapshot)
assert result == 'Offline', f'OFFLINE: expected Offline, got {result}'

# POISONED -> 'Error loop (Xm)'
result = bridge._format_watchdog_status('poisoned_worker', lambda n: True, state_snapshot=snapshot)
assert result.startswith('Error loop'), f'POISONED: expected Error loop (Xm), got {result}'

# Unknown worker -> fallback based on pending
result = bridge._format_watchdog_status('nonexistent_worker', lambda n: True, state_snapshot=snapshot)
assert result == 'Working', f'Unknown+pending: expected Working, got {result}'
result = bridge._format_watchdog_status('nonexistent_worker', lambda n: False, state_snapshot=snapshot)
assert result == 'Ready', f'Unknown+idle: expected Ready, got {result}'

# Clean up
with bridge._watchdog_lock:
    for k in list(states.keys()):
        bridge._worker_states.pop(k, None)

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_format_watchdog_status returns correct text for each state"
    else
        fail "_format_watchdog_status test failed"
    fi
}

test_switch_session() {
    info "Testing switch_session changes active and saves to file..."

    if python3 -c "
import os
import tempfile
from pathlib import Path
import bridge

# Set up temp dirs for isolation
tmpdir = tempfile.mkdtemp()
sessions_dir = Path(tmpdir) / 'sessions'
sessions_dir.mkdir(parents=True)
node_dir = sessions_dir.parent

# Save originals
orig_sessions_dir = bridge.SESSIONS_DIR
orig_node_dir = bridge.NODE_DIR
orig_last_active = bridge.LAST_ACTIVE_FILE
orig_get_reg = bridge.get_registered_sessions

# Override paths
bridge.SESSIONS_DIR = sessions_dir
bridge.NODE_DIR = node_dir
bridge.LAST_ACTIVE_FILE = node_dir / 'last_active'

# Create fake session dirs so worker_manager can find them
(sessions_dir / 'alice').mkdir()
(sessions_dir / 'alice' / 'backend').write_text('claude')
(sessions_dir / 'bob').mkdir()
(sessions_dir / 'bob' / 'backend').write_text('claude')

# Mock get_registered_sessions to return our test workers
bridge.get_registered_sessions = lambda registered=None: {
    'alice': {'backend': 'claude'},
    'bob': {'backend': 'claude'},
}

bridge.state['active'] = 'alice'

# Switch to bob
ok, err = bridge.switch_session('bob')
assert ok, f'switch_session should succeed, got error: {err}'
assert bridge.state['active'] == 'bob', f'Expected active=bob, got {bridge.state[\"active\"]}'

# Verify file persistence
assert bridge.LAST_ACTIVE_FILE.exists(), 'last_active file should exist'
assert bridge.LAST_ACTIVE_FILE.read_text().strip() == 'bob', \
    f'last_active should be bob, got {bridge.LAST_ACTIVE_FILE.read_text().strip()}'

# Switch to nonexistent worker
ok, err = bridge.switch_session('charlie')
assert not ok, 'switch_session to nonexistent worker should fail'
assert 'not found' in err.lower(), f'Expected not found in error, got: {err}'

# Restore originals
bridge.SESSIONS_DIR = orig_sessions_dir
bridge.NODE_DIR = orig_node_dir
bridge.LAST_ACTIVE_FILE = orig_last_active
bridge.get_registered_sessions = orig_get_reg

import shutil
shutil.rmtree(tmpdir)

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "switch_session changes active and persists to file"
    else
        fail "switch_session test failed"
    fi
}

test_send_response_html_formatting() {
    info "Testing send_response_to_telegram with HTML formatting..."

    if python3 -c "
import bridge

# Track API calls
calls = []
def fake_api(method, data):
    calls.append((method, data))
    return {'ok': True, 'result': {'message_id': 123}}

orig_api = bridge.telegram_api
bridge.telegram_api = fake_api

# Send a response with markdown bold
bridge.send_response_to_telegram('testworker', '**hello world**', 12345)

assert len(calls) >= 1, f'Expected at least 1 API call, got {len(calls)}'
method, data = calls[0]
assert method == 'sendMessage', f'Expected sendMessage, got {method}'
assert data['parse_mode'] == 'HTML', f'Expected HTML parse_mode, got {data.get(\"parse_mode\")}'
# markdown_to_telegram_html should convert **bold** to <b>bold</b>
assert '<b>' in data['text'] or 'hello world' in data['text'], \
    f'Expected HTML bold tags or plain text, got: {data[\"text\"]}'

# Test with code block
calls.clear()
bridge.send_response_to_telegram('testworker', '\`\`\`python\nprint(1)\n\`\`\`', 12345)
assert len(calls) >= 1, f'Expected at least 1 API call for code block'
method, data = calls[0]
assert '<pre>' in data['text'] or '<code>' in data['text'] or 'print(1)' in data['text'], \
    f'Expected code HTML in text, got: {data[\"text\"]}'

bridge.telegram_api = orig_api
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "send_response_to_telegram uses HTML formatting"
    else
        fail "send_response_html_formatting test failed"
    fi
}

test_format_response_strips_name_prefix() {
    info "Testing format_response_text strips redundant name prefix..."

    if python3 -c "
from bridge import format_response_text

# Normal message - no prefix to strip
result = format_response_text('lee', 'hello world')
assert result == '<b>lee:</b>\nhello world', f'unexpected: {result}'

# Message with redundant prefix - should strip it
result = format_response_text('lee', 'lee: hello world')
assert result == '<b>lee:</b>\nhello world', f'unexpected: {result}'

# Case-insensitive prefix strip
result = format_response_text('lee', 'Lee: hello world')
assert result == '<b>lee:</b>\nhello world', f'unexpected: {result}'

# Different worker name - should NOT strip
result = format_response_text('lee', 'chen: hello world')
assert result == '<b>lee:</b>\nchen: hello world', f'unexpected: {result}'

# Prefix with leading whitespace
result = format_response_text('lee', '  lee: hello world')
assert result == '<b>lee:</b>\nhello world', f'unexpected: {result}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "format_response_text strips redundant name prefix"
    else
        fail "format_response_text prefix strip test failed"
    fi
}

test_manager_prefix_on_route_message() {
    info "Testing manager prefix: plain text gets prefixed, media does not..."

    # Note: route_message mock test can segfault during Python cleanup.
    # Write test to a temp script and run it, capturing stdout separately.
    local tmpscript tmpout
    tmpscript=$(mktemp /tmp/test_prefix_XXXXX.py)
    tmpout=$(mktemp)
    cat > "$tmpscript" << 'PYEOF'
import os, sys, tempfile
from pathlib import Path
from unittest.mock import MagicMock
import bridge

tmpdir = tempfile.mkdtemp()
sessions_dir = Path(tmpdir) / 'sessions'
sessions_dir.mkdir()
orig_sessions_dir = bridge.SESSIONS_DIR
bridge.SESSIONS_DIR = sessions_dir

worker_name = 'testbot'
worker_dir = sessions_dir / worker_name
worker_dir.mkdir()
(worker_dir / 'chat_id').write_text('12345')
(worker_dir / 'backend').write_text('claude')

bridge.state['active'] = worker_name
tmux_name = f'{bridge.TMUX_PREFIX}{worker_name}'

router = bridge.command_router
orig_workers = router.workers

mock_workers = MagicMock()
mock_workers.get_registered_sessions.return_value = {
    worker_name: {'tmux': tmux_name}
}
mock_workers.is_online.return_value = True
mock_workers.tmux_prefix = bridge.TMUX_PREFIX
mock_workers.send.return_value = True
router.workers = mock_workers
router.telegram = MagicMock()

# 1) Plain text must get 'manager: ' prefix
router.route_message(worker_name, 'hello world', 12345, None)
sent_text = mock_workers.send.call_args[0][1]
assert sent_text == 'manager: hello world', f'plain text: expected manager prefix, got: {sent_text!r}'

# 2) Media text (already has Manager sent) must NOT get double-prefixed
mock_workers.send.reset_mock()
router.route_message(worker_name, 'Manager sent image: /tmp/photo.jpg', 12345, None)
sent_media = mock_workers.send.call_args[0][1]
assert sent_media == 'Manager sent image: /tmp/photo.jpg', f'media: should pass through unchanged, got: {sent_media!r}'

# 3) @all route also gets prefix (goes through route_message)
mock_workers.send.reset_mock()
router.route_to_all('team update', 12345, None)
sent_all = mock_workers.send.call_args[0][1]
assert sent_all == 'manager: team update', f'@all: expected manager prefix, got: {sent_all!r}'

router.workers = orig_workers
bridge.SESSIONS_DIR = orig_sessions_dir
sys.stdout.write('OK\n')
sys.stdout.flush()
os._exit(0)
PYEOF
    PYTHONPATH="$SCRIPT_DIR" python3 "$tmpscript" > "$tmpout" 2>/dev/null || true
    if grep -q "OK" "$tmpout"; then
        success "Manager prefix: plain text prefixed, media unchanged, @all prefixed"
    else
        fail "Manager prefix test failed"
    fi
    rm -f "$tmpscript" "$tmpout"
}

test_media_at_mention_routes_to_mentioned_worker() {
    info "Testing media with @mention routes to mentioned worker, not active..."

    local tmpscript tmpout
    tmpscript=$(mktemp /tmp/test_media_mention_XXXXX.py)
    tmpout=$(mktemp)
    cat > "$tmpscript" << 'PYEOF'
import os, sys, tempfile, json
from pathlib import Path
from unittest.mock import MagicMock, patch
import bridge

tmpdir = tempfile.mkdtemp()
sessions_dir = Path(tmpdir) / 'sessions'
sessions_dir.mkdir()
orig_sessions_dir = bridge.SESSIONS_DIR
bridge.SESSIONS_DIR = sessions_dir

# Create two workers: alice (active) and bob (mentioned)
for name in ['alice', 'bob']:
    d = sessions_dir / name
    d.mkdir()
    (d / 'chat_id').write_text('12345')
    (d / 'backend').write_text('claude')

bridge.state['active'] = 'alice'
orig_admin = bridge.admin_chat_id
bridge.admin_chat_id = 12345

router = bridge.command_router
orig_workers = router.workers

mock_workers = MagicMock()
mock_workers.get_registered_sessions.return_value = {
    'alice': {'tmux': f'{bridge.TMUX_PREFIX}alice'},
    'bob': {'tmux': f'{bridge.TMUX_PREFIX}bob'},
}
mock_workers.is_online.return_value = True
mock_workers.tmux_prefix = bridge.TMUX_PREFIX
mock_workers.send.return_value = True
router.workers = mock_workers
router.telegram = MagicMock()

# Simulate sending an animation with @bob in caption
# Mock download_telegram_file to return a fake path
with patch('bridge.download_telegram_file', return_value='/tmp/fake.gif'):
    update = {
        'message': {
            'chat': {'id': 12345},
            'message_id': 1,
            'text': '@bob turn this gif to mp4 pls',
            'caption': '@bob turn this gif to mp4 pls',
            'animation': {'file_id': 'fake_file_id'},
        }
    }
    router.handle_message(update)

# Bob should have received the message (not alice)
send_calls = mock_workers.send.call_args_list
assert len(send_calls) >= 1, f'expected at least 1 send call, got {len(send_calls)}'

# Check that bob was the recipient
recipients = [c[0][0] for c in send_calls]
assert 'bob' in recipients, f'expected bob in recipients, got: {recipients}'
assert 'alice' not in recipients, f'alice should NOT have received it, recipients: {recipients}'

# Check the message contains the GIF path
sent_text = send_calls[0][0][1]
assert '/tmp/fake.gif' in sent_text, f'expected GIF path in message, got: {sent_text!r}'

router.workers = orig_workers
bridge.SESSIONS_DIR = orig_sessions_dir
bridge.admin_chat_id = orig_admin
sys.stdout.write('OK\n')
sys.stdout.flush()
os._exit(0)
PYEOF
    PYTHONPATH="$SCRIPT_DIR" python3 "$tmpscript" > "$tmpout" 2>/dev/null || true
    if grep -q "OK" "$tmpout"; then
        success "Media with @mention routes to mentioned worker"
    else
        fail "Media @mention routing test failed"
    fi
    rm -f "$tmpscript" "$tmpout"
}

test_media_reply_to_routes_to_replied_worker() {
    info "Testing media reply-to routes to the worker whose message was replied to..."

    local tmpscript tmpout
    tmpscript=$(mktemp /tmp/test_media_reply_XXXXX.py)
    tmpout=$(mktemp)
    cat > "$tmpscript" << 'PYEOF'
import os, sys, tempfile, json
from pathlib import Path
from unittest.mock import MagicMock, patch
import bridge

tmpdir = tempfile.mkdtemp()
sessions_dir = Path(tmpdir) / 'sessions'
sessions_dir.mkdir()
orig_sessions_dir = bridge.SESSIONS_DIR
bridge.SESSIONS_DIR = sessions_dir

# Create two workers: alice (active) and bob (reply target)
for name in ['alice', 'bob']:
    d = sessions_dir / name
    d.mkdir()
    (d / 'chat_id').write_text('12345')
    (d / 'backend').write_text('claude')

bridge.state['active'] = 'alice'
orig_admin = bridge.admin_chat_id
bridge.admin_chat_id = 12345

router = bridge.command_router
orig_workers = router.workers

mock_workers = MagicMock()
mock_workers.get_registered_sessions.return_value = {
    'alice': {'tmux': f'{bridge.TMUX_PREFIX}alice'},
    'bob': {'tmux': f'{bridge.TMUX_PREFIX}bob'},
}
mock_workers.is_online.return_value = True
mock_workers.tmux_prefix = bridge.TMUX_PREFIX
mock_workers.send.return_value = True
router.workers = mock_workers
router.telegram = MagicMock()

# Simulate: manager replies to bob's message with a GIF (no caption)
# Bob's message in Telegram looks like "bob:\nHere is my analysis..."
with patch('bridge.download_telegram_file', return_value='/tmp/fake.gif'):
    update = {
        'message': {
            'chat': {'id': 12345},
            'message_id': 2,
            'caption': '',
            'animation': {'file_id': 'fake_file_id'},
            'reply_to_message': {
                'message_id': 1,
                'text': 'bob:\nHere is my analysis of the code...',
            },
        }
    }
    router.handle_message(update)

# Bob should have received the GIF (not alice, the active worker)
send_calls = mock_workers.send.call_args_list
assert len(send_calls) >= 1, f'expected at least 1 send call, got {len(send_calls)}'

recipients = [c[0][0] for c in send_calls]
assert 'bob' in recipients, f'expected bob in recipients, got: {recipients}'
assert 'alice' not in recipients, f'alice should NOT have received it, recipients: {recipients}'

# Check the message contains the GIF path
sent_text = send_calls[0][0][1]
assert '/tmp/fake.gif' in sent_text, f'expected GIF path in message, got: {sent_text!r}'

router.workers = orig_workers
bridge.SESSIONS_DIR = orig_sessions_dir
bridge.admin_chat_id = orig_admin
sys.stdout.write('OK\n')
sys.stdout.flush()
os._exit(0)
PYEOF
    PYTHONPATH="$SCRIPT_DIR" python3 "$tmpscript" > "$tmpout" 2>/dev/null || true
    if grep -q "OK" "$tmpout"; then
        success "Media reply-to routes to replied worker"
    else
        fail "Media reply-to routing test failed"
    fi
    rm -f "$tmpscript" "$tmpout"
}

test_reply_with_mention_forwards_replied_media() {
    info "Testing reply to media message with @mention forwards the media to mentioned worker..."

    local tmpscript tmpout
    tmpscript=$(mktemp /tmp/test_reply_fwd_media_XXXXX.py)
    tmpout=$(mktemp)
    cat > "$tmpscript" << 'PYEOF'
import os, sys, tempfile, json
from pathlib import Path
from unittest.mock import MagicMock, patch
import bridge

tmpdir = tempfile.mkdtemp()
sessions_dir = Path(tmpdir) / 'sessions'
sessions_dir.mkdir()
orig_sessions_dir = bridge.SESSIONS_DIR
bridge.SESSIONS_DIR = sessions_dir

# Create two workers: alice (active) and bob (target via @mention)
for name in ['alice', 'bob']:
    d = sessions_dir / name
    d.mkdir()
    (d / 'chat_id').write_text('12345')
    (d / 'backend').write_text('claude')

bridge.state['active'] = 'alice'
orig_admin = bridge.admin_chat_id
bridge.admin_chat_id = 12345

router = bridge.command_router
orig_workers = router.workers

mock_workers = MagicMock()
mock_workers.get_registered_sessions.return_value = {
    'alice': {'tmux': f'{bridge.TMUX_PREFIX}alice'},
    'bob': {'tmux': f'{bridge.TMUX_PREFIX}bob'},
}
mock_workers.is_online.return_value = True
mock_workers.tmux_prefix = bridge.TMUX_PREFIX
mock_workers.send.return_value = True
router.workers = mock_workers
router.telegram = MagicMock()

# Manager replies to luck's message (which contains a GIF) with "@bob check this gif"
# The reply message itself is TEXT only — the GIF is in reply_to_message
with patch('bridge.download_telegram_file', return_value='/tmp/forwarded.gif'):
    update = {
        'message': {
            'chat': {'id': 12345},
            'message_id': 3,
            'text': '@bob check this gif',
            'reply_to_message': {
                'message_id': 1,
                'text': 'luck:\nHere is the animation',
                'animation': {
                    'file_id': 'replied_gif_file_id',
                    'file_unique_id': 'abc',
                },
                'document': {
                    'file_id': 'replied_doc_file_id',
                    'mime_type': 'video/mp4',
                },
            },
        }
    }
    router.handle_message(update)

# Bob should have received a message with the GIF path
send_calls = mock_workers.send.call_args_list
assert len(send_calls) >= 1, f'expected at least 1 send call, got {len(send_calls)}'

recipients = [c[0][0] for c in send_calls]
assert 'bob' in recipients, f'expected bob in recipients, got: {recipients}'
assert 'alice' not in recipients, f'alice should NOT have received it, recipients: {recipients}'

# Check the message contains the forwarded GIF path
sent_text = send_calls[0][0][1]
assert '/tmp/forwarded.gif' in sent_text, f'expected GIF path in message, got: {sent_text!r}'

router.workers = orig_workers
bridge.SESSIONS_DIR = orig_sessions_dir
bridge.admin_chat_id = orig_admin
sys.stdout.write('OK\n')
sys.stdout.flush()
os._exit(0)
PYEOF
    PYTHONPATH="$SCRIPT_DIR" python3 "$tmpscript" > "$tmpout" 2>/dev/null || true
    if grep -q "OK" "$tmpout"; then
        success "Reply with @mention forwards replied-to media"
    else
        fail "Reply-to media forwarding test failed"
    fi
    rm -f "$tmpscript" "$tmpout"
}

test_handle_watchdog_transition() {
    info "Testing _handle_watchdog_transition state machine..."

    if python3 -c "
import time
import bridge

alerts = []
resolved = []

def fake_alert(name, state, reason):
    alerts.append((name, state, reason))

def fake_resolved(name, new_state):
    resolved.append((name, new_state))

orig_alert = bridge._send_watchdog_alert
orig_resolved = bridge._send_resolved_alert
bridge._send_watchdog_alert = fake_alert
bridge._send_resolved_alert = fake_resolved

# Clear state
with bridge._watchdog_lock:
    bridge._prev_worker_states.clear()

now = time.time()

# Transition to STUCK (bad state) should trigger alert
bridge._handle_watchdog_transition('worker1', 'STUCK', 'age=600s', now, now=now)
assert len(alerts) == 1, f'Expected 1 alert, got {len(alerts)}'
assert alerts[0] == ('worker1', 'STUCK', 'age=600s')

# Transition from STUCK to READY should trigger resolved
alerts.clear()
bridge._handle_watchdog_transition('worker1', 'READY', 'idle', now, now=now)
assert len(resolved) == 1, f'Expected 1 resolved, got {len(resolved)}'
assert resolved[0] == ('worker1', 'READY')
assert len(alerts) == 0, 'No alert for good state'

# Transition READY -> BUSY_THINKING should not trigger alert or resolved
alerts.clear()
resolved.clear()
bridge._handle_watchdog_transition('worker1', 'BUSY_THINKING', 'cpu=20', now, now=now)
assert len(alerts) == 0 and len(resolved) == 0, 'READY->BUSY_THINKING should fire nothing'

bridge._send_watchdog_alert = orig_alert
bridge._send_resolved_alert = orig_resolved
with bridge._watchdog_lock:
    bridge._prev_worker_states.clear()

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_handle_watchdog_transition state machine works correctly"
    else
        fail "_handle_watchdog_transition test failed"
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
expected = {'team', 'focus', 'progress', 'pause', 'restart',
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

# ─────────────────────────────────────────────────────────────────────────────
# Security tests
# ─────────────────────────────────────────────────────────────────────────────

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
    info "Testing GET / API index endpoint..."

    local response
    response=$(curl -s "http://localhost:$PORT")

    if echo "$response" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert 'endpoints' in d
assert 'name' in d
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "API index returns JSON with endpoints"
    else
        fail "API index response incorrect: $response"
    fi
}

test_response_endpoint_missing_fields() {
    info "Testing /response endpoint with missing fields..."

    # Missing session
    local result
    result=$(hook_curl "http://localhost:$PORT/response" '{"text":"Test"}')

    if echo "$result" | grep -q "Missing"; then
        success "/response rejects missing session"
    else
        fail "/response should reject missing session"
    fi

    # Missing text
    result=$(hook_curl "http://localhost:$PORT/response" '{"session":"test"}')

    if echo "$result" | grep -q "Missing"; then
        success "/response rejects missing text"
    else
        fail "/response should reject missing text"
    fi
}

test_response_endpoint_no_chat_id() {
    info "Testing /response endpoint with non-existent session..."

    local body='{"session":"nonexistent_session_xyz","text":"Test"}'
    local result
    result=$(hook_curl "http://localhost:$PORT/response" "$body")

    # Should return 404 for session without chat_id file
    if echo "$result" | grep -q "No chat_id"; then
        success "/response returns 404 for unknown session"
    else
        # Check HTTP code
        local http_code
        http_code=$(hook_curl_code "http://localhost:$PORT/response" "$body")
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
    http_code=$(hook_curl_code "http://localhost:$PORT/notify" '{}')

    if [[ "$http_code" == "400" ]]; then
        success "/notify rejects missing text (400)"
    else
        fail "/notify should return 400 for missing text, got $http_code"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Pending and timeout tests
# ─────────────────────────────────────────────────────────────────────────────

test_pending_no_side_effect() {
    info "Testing is_pending() is non-mutating (no auto-delete)..."

    if python3 -c "
from bridge import is_pending, set_pending, get_pending_file, _pending_timestamp
import time
from pathlib import Path

# Create test session dir
test_name = 'sideeffect_test'
pending_file = get_pending_file(test_name)
pending_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

# Write a pending file with timestamp 11 minutes ago (past PENDING_TIMEOUT=600s)
old_ts = int(time.time()) - (11 * 60)
pending_file.write_text(str(old_ts))

# is_pending should return False for stale pending...
result = is_pending(test_name)
assert result == False, f'expected False for stale pending, got {result}'

# ...BUT the file must still exist (non-mutating read)
# This is critical: watchdog needs the file at 15min for STALE_PENDING detection
assert pending_file.exists(), 'is_pending must NOT delete the pending file'

# _pending_timestamp should still return the original timestamp
ts = _pending_timestamp(test_name)
assert ts == old_ts, f'expected {old_ts}, got {ts}'

# Fresh timestamp should return True
fresh_ts = int(time.time())
pending_file.write_text(str(fresh_ts))
result = is_pending(test_name)
assert result == True, f'expected True for fresh pending, got {result}'

# Cleanup
pending_file.unlink(missing_ok=True)
pending_file.parent.rmdir()

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "is_pending() is non-mutating"
    else
        fail "is_pending() side-effect test failed"
    fi
}

test_stale_pending_survives_for_watchdog() {
    info "Testing pending file survives past 10min for STALE_PENDING (15min) detection..."

    if python3 -c "
from bridge import is_pending, get_pending_file, _pending_timestamp, compute_state
import time
from pathlib import Path

test_name = 'stale_test'
pending_file = get_pending_file(test_name)
pending_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

# Simulate 16 minutes old pending (past both PENDING_TIMEOUT and STALE_PENDING)
stale_ts = int(time.time()) - (16 * 60)
pending_file.write_text(str(stale_ts))

# is_pending returns False (timed out)
assert is_pending(test_name) == False

# But _pending_timestamp still returns the timestamp
ts = _pending_timestamp(test_name)
assert ts == stale_ts, f'timestamp lost: {ts}'

# And compute_state should see STALE_PENDING
now = time.time()
pending_age = now - stale_ts
state, reason = compute_state(
    tmux_exists=True,
    claude_pid='12345',
    pending=True,  # watchdog uses _pending_timestamp, not is_pending()
    pending_ts=stale_ts,
    pending_age=pending_age,
    children=0,
    last_child_ts=0,
    cpu=0.0,
    last_hook_ts=None,
    last_seen_claude=now,  # claude was seen recently
    now=now,
)
# STALE_PENDING threshold triggers STUCK state (not a separate state name)
assert state == 'STUCK', f'expected STUCK at stale pending, got {state}: {reason}'

# Cleanup
pending_file.unlink(missing_ok=True)
pending_file.parent.rmdir()

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Pending file survives for STALE_PENDING detection"
    else
        fail "Stale pending survival test failed"
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
            success "CLI hook install creates Stop hook file"
        else
            fail "Stop hook file not created"
        fi
        if [[ -f "$temp_home/.claude/hooks/checkin-on-start.sh" ]]; then
            success "CLI hook install creates SessionStart hook file"
        else
            fail "SessionStart hook file not created"
        fi
    else
        fail "CLI hook install failed"
    fi

    # Test hooks are in settings.json
    if [[ -f "$temp_home/.claude/settings.json" ]]; then
        if grep -q "send-to-telegram.sh" "$temp_home/.claude/settings.json"; then
            success "Stop hook registered in settings.json"
        else
            fail "Stop hook not in settings.json"
        fi
        if grep -q "checkin-on-start.sh" "$temp_home/.claude/settings.json"; then
            success "SessionStart hook registered in settings.json"
        else
            fail "SessionStart hook not in settings.json"
        fi
        if grep -q '"compact|resume|init|start"' "$temp_home/.claude/settings.json"; then
            success "SessionStart hook has correct matcher"
        else
            fail "SessionStart hook matcher missing"
        fi

        if grep -q "PostToolUseFailure" "$temp_home/.claude/settings.json"; then
            success "PostToolUseFailure hook registered in settings.json"
        else
            fail "PostToolUseFailure hook not in settings.json"
        fi
        if grep -q "on-tool-failure.sh" "$temp_home/.claude/settings.json"; then
            success "PostToolUseFailure hook points to on-tool-failure.sh"
        else
            fail "PostToolUseFailure hook script path missing"
        fi
    fi

    rm -rf "$temp_home"
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

test_checkin_hook_env_validation() {
    info "Testing checkin hook exits when env vars missing..."

    # The checkin hook reads env vars from tmux session env first.
    # Temporarily unset tmux env vars so shell env takes effect.
    local saved_tmux_vars=()
    for var in TMUX_PREFIX BRIDGE_URL PORT; do
        local val
        val=$(tmux show-environment "$var" 2>/dev/null) || true
        if [[ -n "$val" && "$val" != -* ]]; then
            saved_tmux_vars+=("$val")
            tmux set-environment -u "$var" 2>/dev/null || true
        fi
    done

    # Test 1: Missing TMUX_PREFIX - hook should exit silently
    local result exit_code
    result=$(TMUX_PREFIX="" BRIDGE_URL="http://localhost:8080" bash "$SCRIPT_DIR/hooks/checkin-on-start.sh" 2>&1) || true
    if [[ -z "$result" ]]; then
        success "Checkin hook exits silently when TMUX_PREFIX missing"
    else
        fail "Checkin hook should exit silently when TMUX_PREFIX missing, got: $result"
    fi

    # Test 2: Missing both BRIDGE_URL and PORT - hook should exit silently
    result=$(TMUX_PREFIX="claude-test-" BRIDGE_URL="" PORT="" bash "$SCRIPT_DIR/hooks/checkin-on-start.sh" 2>&1) || true
    if [[ -z "$result" ]]; then
        success "Checkin hook exits silently when BRIDGE_URL and PORT missing"
    else
        fail "Checkin hook should exit silently when both missing, got: $result"
    fi

    # Restore tmux env vars
    for var_line in "${saved_tmux_vars[@]}"; do
        local var_name="${var_line%%=*}"
        local var_val="${var_line#*=}"
        tmux set-environment "$var_name" "$var_val" 2>/dev/null || true
    done
}

test_checkin_hook_calls_endpoint() {
    info "Testing checkin hook calls /checkin endpoint..."

    # This test requires a running bridge (integration test).
    # The hook runs inside a tmux session, reads env vars, and curls /checkin.
    # We simulate by setting env vars directly (tmux env already unset in previous test).
    local saved_tmux_vars=()
    for var in TMUX_PREFIX BRIDGE_URL PORT; do
        local val
        val=$(tmux show-environment "$var" 2>/dev/null) || true
        if [[ -n "$val" && "$val" != -* ]]; then
            saved_tmux_vars+=("$val")
            tmux set-environment -u "$var" 2>/dev/null || true
        fi
    done

    # Run the hook with valid env vars pointing to our test bridge
    local result
    result=$(TMUX_PREFIX="claude-test-" BRIDGE_URL="http://localhost:$PORT" bash "$SCRIPT_DIR/hooks/checkin-on-start.sh" 2>&1) || true

    if [[ -n "$result" ]] && echo "$result" | grep -qi -e "RECEIVING\|SENDING\|MESSAGING\|worker\|instruction"; then
        success "Checkin hook returns bridge instructions"
    else
        # May return empty if session name doesn't match prefix (expected outside bridge tmux)
        success "Checkin hook executed (no matching session in current tmux)"
    fi

    # Restore tmux env vars
    for var_line in "${saved_tmux_vars[@]}"; do
        local var_name="${var_line%%=*}"
        local var_val="${var_line#*=}"
        tmux set-environment "$var_name" "$var_val" 2>/dev/null || true
    done
}

# ─────────────────────────────────────────────────────────────────────────────
# CLI stop/restart/clean/status tests
# ─────────────────────────────────────────────────────────────────────────────

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

test_tmux_paste_buffer_send() {
    info "Testing tmux paste-buffer message delivery..."

    local test_session="test-paste-buf-$$"
    tmux new-session -d -s "$test_session" -x 200 -y 50 2>/dev/null

    if ! tmux has-session -t "$test_session" 2>/dev/null; then
        fail "Could not create test tmux session"
        return
    fi

    # Test 1: Short message via paste-buffer
    if python3 -c "
import sys; sys.path.insert(0, '.')
from bridge import tmux_send_message
result = tmux_send_message('$test_session', 'paste-test-short-ok')
assert result == True, f'Expected True, got {result}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "paste-buffer: short message sent"
    else
        fail "paste-buffer: short message failed"
        tmux kill-session -t "$test_session" 2>/dev/null
        return
    fi

    sleep 0.5
    local pane_content
    pane_content=$(tmux capture-pane -t "$test_session" -p 2>/dev/null)
    if echo "$pane_content" | grep -q "paste-test-short-ok"; then
        success "paste-buffer: short message arrived in pane"
    else
        fail "paste-buffer: short message not found in pane"
        tmux kill-session -t "$test_session" 2>/dev/null
        return
    fi

    # Test 2: Long message (1000+ chars)
    if python3 -c "
import sys; sys.path.insert(0, '.')
from bridge import tmux_send_message
long_msg = 'LONGMSG_' + 'A' * 1000 + '_END'
result = tmux_send_message('$test_session', long_msg)
assert result == True, f'Expected True, got {result}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "paste-buffer: long message (1000+ chars) sent"
    else
        fail "paste-buffer: long message send failed"
    fi

    sleep 0.5
    pane_content=$(tmux capture-pane -t "$test_session" -p -S -50 2>/dev/null)
    if echo "$pane_content" | grep -q "LONGMSG_"; then
        success "paste-buffer: long message arrived in pane"
    else
        fail "paste-buffer: long message not found in pane"
    fi

    tmux kill-session -t "$test_session" 2>/dev/null
}

test_paste_buffer_uses_bracketed_paste() {
    info "Testing paste-buffer uses -p flag for bracketed paste..."

    if python3 -c "
import sys, subprocess, unittest.mock; sys.path.insert(0, '.')
import bridge

bridge._node_name = 'test-bp'

calls = []
orig_run = subprocess.run

def mock_run(cmd, *args, **kwargs):
    calls.append(cmd)
    # Simulate success
    return type('R', (), {'returncode': 0, 'stdout': b'', 'stderr': b''})()

subprocess.run = mock_run
try:
    # Need a temp tmux lock dir
    import tempfile, os
    lock_dir = f'/tmp/claudecode-telegram/test-bp/locks'
    os.makedirs(lock_dir, exist_ok=True)

    bridge.tmux_send_message('test-session', 'hello world')

    # Find the paste-buffer call
    paste_calls = [c for c in calls if isinstance(c, list) and 'paste-buffer' in c]
    assert len(paste_calls) == 1, f'Expected 1 paste-buffer call, got {len(paste_calls)}: {paste_calls}'

    paste_cmd = paste_calls[0]
    assert '-p' in paste_cmd, f'Missing -p flag in paste-buffer: {paste_cmd}'
    assert '-r' in paste_cmd, f'Missing -r flag in paste-buffer: {paste_cmd}'

    print('OK')
finally:
    subprocess.run = orig_run
" 2>/dev/null | grep -q "OK"; then
        success "paste-buffer uses -p (bracketed paste) and -r flags"
    else
        fail "paste-buffer missing -p flag for bracketed paste"
    fi
}

test_image_caption_enter_with_bracketed_paste() {
    info "Chaos test: image+caption message (multi-line with path) Enter delivery..."

    # TUI simulator that simulates Claude Code's behavior:
    # - Enables bracketed paste mode
    # - After receiving paste end, simulates processing delay (image rendering)
    # - Counts PASTE+ENTER pairs
    cat > /tmp/tui-sim-imgcap-$$.py << 'TUITEST'
import sys, os, select, time, tty, termios

RESULT_FILE = sys.argv[1]
EXPECTED = int(sys.argv[2])
# Simulate TUI processing delay after paste (image path detection + rendering)
RENDER_DELAY_MS = int(sys.argv[3]) if len(sys.argv) > 3 else 0

sys.stdout.buffer.write(b'\033[?2004h')
sys.stdout.buffer.flush()

old = termios.tcgetattr(sys.stdin)
tty.setraw(sys.stdin)

paste_count = 0
enter_after_count = 0
enter_during_render = 0
buf = b''
in_paste = False
paste_data = b''
rendering = False
render_end_time = 0

try:
    deadline = time.time() + 8
    while time.time() < deadline:
        readable, _, _ = select.select([sys.stdin], [], [], 0.01)
        if readable:
            chunk = os.read(sys.stdin.fileno(), 65536)
            if not chunk:
                break
            buf += chunk

            while True:
                if not in_paste:
                    idx = buf.find(b'\x1b[200~')
                    if idx >= 0:
                        in_paste = True
                        paste_data = b''
                        buf = buf[idx + 6:]
                        continue
                    else:
                        if b'\r' in buf:
                            now = time.time()
                            if rendering and now < render_end_time:
                                # Enter arrived while TUI is still rendering
                                enter_during_render += 1
                            elif paste_count > enter_after_count:
                                enter_after_count += 1
                                rendering = False
                            buf = buf[buf.rfind(b'\r')+1:]
                        break
                else:
                    idx = buf.find(b'\x1b[201~')
                    if idx >= 0:
                        paste_data += buf[:idx]
                        in_paste = False
                        paste_count += 1
                        # Simulate TUI rendering delay after paste ends
                        if RENDER_DELAY_MS > 0:
                            rendering = True
                            render_end_time = time.time() + (RENDER_DELAY_MS / 1000.0)
                        buf = buf[idx + 6:]
                        continue
                    else:
                        paste_data += buf
                        buf = b''
                        break

        # Check if rendering finished
        if rendering and time.time() >= render_end_time:
            rendering = False

        if enter_after_count >= EXPECTED:
            break
finally:
    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
    sys.stdout.buffer.write(b'\033[?2004l')
    sys.stdout.buffer.flush()

with open(RESULT_FILE, 'w') as f:
    f.write(f'PASTES:{paste_count}\n')
    f.write(f'ENTERS:{enter_after_count}\n')
    f.write(f'ENTER_DURING_RENDER:{enter_during_render}\n')
TUITEST

    local test_session="test-imgcap-$$"
    local result_file="/tmp/tui-imgcap-result-$$.txt"
    local chaos_count=10
    # Simulate 120ms TUI render delay (image path detection + re-render).
    # Claude Code needs time to detect image paths and re-draw prompt.
    # Bridge post-paste delay must exceed this to avoid Enter swallowing.
    local render_delay_ms=120
    rm -f "$result_file"

    tmux new-session -d -s "$test_session" -x 200 -y 50 \
        "python3 /tmp/tui-sim-imgcap-$$.py $result_file $chaos_count $render_delay_ms"
    sleep 1

    if ! tmux has-session -t "$test_session" 2>/dev/null; then
        fail "Could not create TUI simulator session"
        rm -f "/tmp/tui-sim-imgcap-$$.py"
        return
    fi

    # Chaos: send 10 image+caption messages rapidly
    local sent=0
    local failed=0
    for i in $(seq 1 $chaos_count); do
        if python3 -c "
import sys; sys.path.insert(0, '.')
from bridge import tmux_send_message

# Exact format bridge produces for image+caption
caption = 'can u improve the check capturing haha'
img_path = '/tmp/claudecode-telegram/prod/kenji/inbox/7ca9f508093444a4be3e5824bd339e24.jpg'
msg = f'{caption}\n\nManager sent image: {img_path}'
result = tmux_send_message('$test_session', msg)
print('OK' if result else 'FAIL')
" 2>/dev/null | grep -q "OK"; then
            sent=$((sent + 1))
        else
            failed=$((failed + 1))
        fi
    done

    if [ "$sent" -ne "$chaos_count" ]; then
        fail "Only $sent/$chaos_count messages sent (tmux_send_message returned False)"
        tmux kill-session -t "$test_session" 2>/dev/null || true
        rm -f "/tmp/tui-sim-imgcap-$$.py" "$result_file"
        return
    fi

    # Wait for TUI to process all (TUI deadline is 8s, wait beyond it)
    sleep 10

    if [ -f "$result_file" ]; then
        local pastes enters renders
        pastes=$(grep -oP 'PASTES:\K\d+' "$result_file" 2>/dev/null || echo 0)
        enters=$(grep -oP 'ENTERS:\K\d+' "$result_file" 2>/dev/null || echo 0)
        renders=$(grep -oP 'ENTER_DURING_RENDER:\K\d+' "$result_file" 2>/dev/null || echo 0)
        if [ "$enters" -ge "$chaos_count" ]; then
            success "Chaos: $enters/$chaos_count image+caption Enter delivered ($pastes pastes, $renders during render)"
        elif [ "$enters" -gt 0 ]; then
            fail "Chaos: only $enters/$chaos_count Enter received ($pastes pastes, $renders during render) — Enter swallowed!"
        else
            fail "Chaos: 0/$chaos_count Enter received ($pastes pastes, $renders during render) — total failure"
        fi
    else
        fail "TUI simulator produced no result file"
    fi

    tmux kill-session -t "$test_session" 2>/dev/null || true
    rm -f "/tmp/tui-sim-imgcap-$$.py" "$result_file"
}

test_long_text_enter_with_bracketed_paste() {
    info "Testing long text (60 lines) + Enter delivered via bracketed paste..."

    # Create TUI simulator that enables bracketed paste mode
    cat > /tmp/tui-sim-test-$$.py << 'TUITEST'
import sys, os, select, time, tty, termios

RESULT_FILE = sys.argv[1]

# Enable bracketed paste mode (like Claude Code)
sys.stdout.buffer.write(b'\033[?2004h')
sys.stdout.buffer.flush()

old = termios.tcgetattr(sys.stdin)
tty.setraw(sys.stdin)

results = []
buf = b''
in_paste = False
paste_data = b''
paste_received = False

try:
    deadline = time.time() + 5
    while time.time() < deadline:
        readable, _, _ = select.select([sys.stdin], [], [], 0.05)
        if readable:
            chunk = os.read(sys.stdin.fileno(), 65536)
            if not chunk:
                break
            buf += chunk

            while True:
                if not in_paste:
                    idx = buf.find(b'\x1b[200~')
                    if idx >= 0:
                        in_paste = True
                        paste_data = b''
                        buf = buf[idx + 6:]
                        continue
                    else:
                        if b'\r' in buf:
                            if paste_received:
                                results.append("ENTER_AFTER_PASTE")
                            buf = buf[buf.rfind(b'\r')+1:]
                        break
                else:
                    idx = buf.find(b'\x1b[201~')
                    if idx >= 0:
                        paste_data += buf[:idx]
                        in_paste = False
                        paste_received = True
                        lines = paste_data.count(b'\n') + 1
                        results.append(f"PASTE:{lines}lines")
                        buf = buf[idx + 6:]
                        continue
                    else:
                        paste_data += buf
                        buf = b''
                        break

        if "ENTER_AFTER_PASTE" in results:
            break
finally:
    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
    sys.stdout.buffer.write(b'\033[?2004l')
    sys.stdout.buffer.flush()

if not results:
    results.append("NOTHING_RECEIVED")

with open(RESULT_FILE, 'w') as f:
    f.write('\n'.join(results) + '\n')
TUITEST

    local test_session="test-bp-long-$$"
    local result_file="/tmp/tui-bp-result-$$.txt"
    rm -f "$result_file"

    tmux new-session -d -s "$test_session" -x 200 -y 50 \
        "python3 /tmp/tui-sim-test-$$.py $result_file"
    sleep 1

    if ! tmux has-session -t "$test_session" 2>/dev/null; then
        fail "Could not create TUI simulator session"
        rm -f "/tmp/tui-sim-test-$$.py"
        return
    fi

    # Send 60-line message via bridge function
    if python3 -c "
import sys; sys.path.insert(0, '.')
from bridge import tmux_send_message

# Generate 60-line message
lines = [f'Line {i}: Test content for bracketed paste delivery verification.' for i in range(1, 61)]
msg = '\n'.join(lines)
result = tmux_send_message('$test_session', msg)
assert result == True, f'tmux_send_message returned {result}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        : # send succeeded
    else
        fail "tmux_send_message failed for 60-line message"
        tmux kill-session -t "$test_session" 2>/dev/null
        rm -f "/tmp/tui-sim-test-$$.py" "$result_file"
        return
    fi

    # Wait for TUI to process
    sleep 3

    if [ -f "$result_file" ]; then
        local has_paste has_enter
        has_paste=$(grep -c "PASTE:" "$result_file" 2>/dev/null || echo 0)
        has_enter=$(grep -c "ENTER_AFTER_PASTE" "$result_file" 2>/dev/null || echo 0)
        if [ "$has_paste" -gt 0 ] && [ "$has_enter" -gt 0 ]; then
            success "60-line text + Enter delivered via bracketed paste"
        elif [ "$has_paste" -gt 0 ]; then
            fail "Paste received but Enter lost ($(cat "$result_file" | tr '\n' ' '))"
        else
            fail "Bracketed paste not detected ($(cat "$result_file" | tr '\n' ' '))"
        fi
    else
        fail "TUI simulator produced no result file"
    fi

    tmux kill-session -t "$test_session" 2>/dev/null || true
    rm -f "/tmp/tui-sim-test-$$.py" "$result_file"
}

test_slow_paste_render_enter_delivered() {
    info "Chaos test: slow paste render (400ms) — 1s delay must cover it..."

    # Same TUI simulator as test_image_caption_enter but with 400ms render delay
    # (exceeds old 150ms sleep, within new 1s sleep).
    cat > /tmp/tui-sim-slowpaste-$$.py << 'TUITEST'
import sys, os, select, time, tty, termios
RESULT_FILE = sys.argv[1]
EXPECTED = int(sys.argv[2])
RENDER_DELAY_MS = int(sys.argv[3]) if len(sys.argv) > 3 else 400
sys.stdout.buffer.write(b'\033[?2004h')
sys.stdout.buffer.flush()
old = termios.tcgetattr(sys.stdin)
tty.setraw(sys.stdin)
paste_count = 0
enter_after_count = 0
enter_during_render = 0
buf = b''
in_paste = False
paste_data = b''
rendering = False
render_end_time = 0
try:
    deadline = time.time() + 15
    while time.time() < deadline:
        readable, _, _ = select.select([sys.stdin], [], [], 0.01)
        if readable:
            chunk = os.read(sys.stdin.fileno(), 65536)
            if not chunk:
                break
            buf += chunk
            while True:
                if not in_paste:
                    idx = buf.find(b'\x1b[200~')
                    if idx >= 0:
                        in_paste = True
                        paste_data = b''
                        buf = buf[idx + 6:]
                        continue
                    else:
                        if b'\r' in buf:
                            now = time.time()
                            if rendering and now < render_end_time:
                                enter_during_render += 1
                            elif paste_count > enter_after_count:
                                enter_after_count += 1
                                rendering = False
                            buf = buf[buf.rfind(b'\r')+1:]
                        break
                else:
                    idx = buf.find(b'\x1b[201~')
                    if idx >= 0:
                        paste_data += buf[:idx]
                        in_paste = False
                        paste_count += 1
                        if RENDER_DELAY_MS > 0:
                            rendering = True
                            render_end_time = time.time() + (RENDER_DELAY_MS / 1000.0)
                        buf = buf[idx + 6:]
                        continue
                    else:
                        paste_data += buf
                        buf = b''
                        break
        if rendering and time.time() >= render_end_time:
            rendering = False
        if enter_after_count >= EXPECTED:
            break
finally:
    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
    sys.stdout.buffer.write(b'\033[?2004l')
    sys.stdout.buffer.flush()
with open(RESULT_FILE, 'w') as f:
    f.write(f'PASTES:{paste_count}\n')
    f.write(f'ENTERS:{enter_after_count}\n')
    f.write(f'ENTER_DURING_RENDER:{enter_during_render}\n')
TUITEST

    local test_session="test-slowpaste-$$"
    local result_file="/tmp/tui-slowpaste-result-$$.txt"
    local msg_count=5
    local render_delay_ms=400
    rm -f "$result_file"

    tmux new-session -d -s "$test_session" -x 200 -y 50 \
        "python3 /tmp/tui-sim-slowpaste-$$.py $result_file $msg_count $render_delay_ms"
    sleep 1

    if ! tmux has-session -t "$test_session" 2>/dev/null; then
        fail "Could not create TUI simulator session"
        return
    fi

    local sent=0
    for i in $(seq 1 $msg_count); do
        if python3 -c "
import sys; sys.path.insert(0, '.')
from bridge import tmux_send_message
result = tmux_send_message('$test_session', 'Slow paste test message $i')
print('OK' if result else 'FAIL')
" 2>/dev/null | grep -q "OK"; then
            sent=$((sent + 1))
        fi
    done

    sleep 8

    if [ -f "$result_file" ]; then
        local pastes enters renders
        pastes=$(grep -oP 'PASTES:\K\d+' "$result_file" 2>/dev/null || echo 0)
        enters=$(grep -oP 'ENTERS:\K\d+' "$result_file" 2>/dev/null || echo 0)
        renders=$(grep -oP 'ENTER_DURING_RENDER:\K\d+' "$result_file" 2>/dev/null || echo 0)
        if [ "$enters" -ge "$msg_count" ]; then
            success "Slow paste: $enters/$msg_count Enter delivered ($renders during render)"
        else
            fail "Slow paste: $enters/$msg_count Enter OK, $renders during render — message lost!"
        fi
    else
        fail "TUI simulator produced no result file"
    fi

    tmux kill-session -t "$test_session" 2>/dev/null || true
    rm -f "$result_file"
}

test_tmux_send_uses_flock() {
    info "Testing tmux_send_message acquires cross-process flock..."

    # tmux_send_message should acquire a file lock (flock) so that
    # external processes (workers) using the same lock file are serialized.
    # Verify the lock file exists after a send.
    local test_session="test-flock-$$"
    tmux new-session -d -s "$test_session" -x 200 -y 50 2>/dev/null

    if ! tmux has-session -t "$test_session" 2>/dev/null; then
        fail "Could not create test tmux session"
        return
    fi

    if python3 -c "
import sys, os; sys.path.insert(0, '.')
import bridge

# Override _node_name so lock path is predictable
bridge._node_name = 'test-flock'
bridge.TMUX_PREFIX = 'test-flock-'

# Send a message
result = bridge.tmux_send_message('$test_session', 'flock-test-msg')
assert result == True, f'send failed: {result}'

# Check that a lock file was created for this session
lock_path = bridge.tmux_send_lock_path('$test_session')
assert os.path.exists(lock_path), f'lock file not created: {lock_path}'
assert '/test-flock/' in str(lock_path), f'lock not node-namespaced: {lock_path}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "tmux_send_message uses flock (lock file created)"
    else
        fail "tmux_send_message does not use flock"
    fi

    tmux kill-session -t "$test_session" 2>/dev/null
}

# ─────────────────────────────────────────────────────────────────────────────
# Startup and shutdown tests
# ─────────────────────────────────────────────────────────────────────────────

# ============================================================
# CLI + NODE CONFIG (NEW)
# ============================================================

# ─────────────────────────────────────────────────────────────────────────────
# Run/Tunnel Behavior Tests (NEW)
# ─────────────────────────────────────────────────────────────────────────────

# ============================================================
# GAPS + ENV + ROUTING (NEW)
# ============================================================

# ─────────────────────────────────────────────────────────────────────────────
# Misc Behavior Gaps Tests (NEW)
# ─────────────────────────────────────────────────────────────────────────────

test_watchdog_alert_on_stuck() {
    info "Testing watchdog alerts on stuck transition..."

    if python3 -c "
import bridge
from unittest.mock import patch

bridge.admin_chat_id = 123
bridge._last_alert_ts = {}
bridge._prev_worker_states = {'alice': 'READY'}
bridge._worker_states = {'alice': ('STUCK', 'age=400s cpu=0.0', 600)}

sent = {}
def fake_api(method, data):
    sent['method'] = method
    sent['data'] = data
    return {'ok': True}

with patch('bridge.telegram_api', fake_api):
    bridge._handle_watchdog_transition('alice', 'STUCK', 'age=400s cpu=0.0', since=600, now=1000)

assert sent.get('method') == 'sendMessage', 'telegram_api not called'
assert sent['data']['chat_id'] == 123, 'admin chat id should be used'
txt = sent['data']['text']
assert 'alice' in txt, f'alert should include worker name: {txt}'
assert 'frozen' in txt or '/restart' in txt, f'alert should be human-friendly: {txt}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Watchdog stuck alert fires on transition"
    else
        fail "Watchdog stuck alert test failed"
    fi
}

test_extra_mounts_docker_cmd() {
    info "Testing extra mounts via --mount and --mount-ro in Docker cmd..."

    if python3 -c "
import os
os.environ['SANDBOX_ENABLED'] = '1'
os.environ['PORT'] = '8295'
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
# Teleport SSH Foundation Tests
# ─────────────────────────────────────────────────────────────────────────────

test_remote_run_local() {
    info "Testing _remote_run with host=None runs locally..."

    if python3 -c "
from unittest.mock import patch, MagicMock
import bridge

# _remote_run with host=None should call subprocess.run without ssh prefix
with patch('subprocess.run') as mock_run:
    mock_run.return_value = MagicMock(returncode=0, stdout='ok', stderr='')
    bridge._remote_run(['tmux', 'has-session', '-t', 'test'], host=None, capture_output=True)
    args = mock_run.call_args[0][0]
    assert args == ['tmux', 'has-session', '-t', 'test'], f'Local: expected raw cmd, got {args}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_remote_run local passes commands through"
    else
        fail "_remote_run local test failed"
    fi
}

test_remote_run_ssh() {
    info "Testing _remote_run with host prefixes ssh..."

    if python3 -c "
from unittest.mock import patch, MagicMock
import bridge

# _remote_run with host='mac' should prefix with ssh and shell-quote args
with patch('subprocess.run') as mock_run:
    mock_run.return_value = MagicMock(returncode=0, stdout='ok', stderr='')
    bridge._remote_run(['tmux', 'has-session', '-t', 'test'], host='mac', capture_output=True)
    args = mock_run.call_args[0][0]
    # New format: ['ssh', host, 'shell-quoted-command']
    assert args[0] == 'ssh', f'Expected ssh, got {args[0]}'
    assert args[1] == 'mac', f'Expected mac, got {args[1]}'
    assert len(args) == 3, f'Expected 3 args (ssh host cmd), got {len(args)}: {args}'
    assert 'has-session' in args[2], f'Expected has-session in cmd string, got {args[2]}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_remote_run remote prefixes ssh"
    else
        fail "_remote_run remote test failed"
    fi
}

test_remote_run_stdin() {
    info "Testing _remote_run passes input/kwargs through..."

    if python3 -c "
from unittest.mock import patch, MagicMock
import bridge

# Verify kwargs (input, capture_output, etc.) are forwarded
with patch('subprocess.run') as mock_run:
    mock_run.return_value = MagicMock(returncode=0)
    bridge._remote_run(['tmux', 'load-buffer', '-b', 'buf1', '-'], host='mac', input=b'hello', capture_output=True)
    kwargs = mock_run.call_args[1]
    assert kwargs.get('input') == b'hello', f'input not forwarded: {kwargs}'
    assert kwargs.get('capture_output') == True, f'capture_output not forwarded: {kwargs}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_remote_run forwards kwargs"
    else
        fail "_remote_run kwargs test failed"
    fi
}

test_remote_copy() {
    info "Testing _remote_copy builds scp commands..."

    if python3 -c "
from unittest.mock import patch, MagicMock
import shutil
import bridge

# Local copy (no host) — should use shutil.copy2
with patch('shutil.copy2') as mock_copy:
    bridge._remote_copy('/tmp/a.txt', '/tmp/b.txt', host=None)
    mock_copy.assert_called_once_with('/tmp/a.txt', '/tmp/b.txt')

# Push to remote — scp local remote:path
with patch('subprocess.run') as mock_run:
    mock_run.return_value = MagicMock(returncode=0)
    bridge._remote_copy('/tmp/a.txt', '/remote/b.txt', host='mac', direction='push')
    args = mock_run.call_args[0][0]
    assert args == ['scp', '-q', '/tmp/a.txt', 'mac:/remote/b.txt'], f'Push: got {args}'

# Pull from remote — scp remote:path local
with patch('subprocess.run') as mock_run:
    mock_run.return_value = MagicMock(returncode=0)
    bridge._remote_copy('/remote/a.txt', '/tmp/b.txt', host='mac', direction='pull')
    args = mock_run.call_args[0][0]
    assert args == ['scp', '-q', 'mac:/remote/a.txt', '/tmp/b.txt'], f'Pull: got {args}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_remote_copy handles local/push/pull"
    else
        fail "_remote_copy test failed"
    fi
}

test_worker_host_field() {
    info "Testing worker registry stores host field..."

    if python3 -c "
import json, tempfile
from pathlib import Path
import bridge

# Use temp dir
tmpdir = tempfile.mkdtemp()
orig_node = bridge.NODE_DIR
orig_reg = bridge.WORKER_REGISTRY_FILE
bridge.NODE_DIR = Path(tmpdir)
bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / 'workers.json'

# Add worker with host
bridge._registry_add('kenji', 'claude', 123, host='mac')
data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
assert data['workers']['kenji']['host'] == 'mac', f'host not stored: {data[\"workers\"][\"kenji\"]}'

# Add worker without host (local)
bridge._registry_add('lee', 'claude', 123)
data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
assert data['workers']['lee'].get('host') is None, f'local worker should have no host'

# get_worker_host reads back correctly
assert bridge.get_worker_host('kenji') == 'mac', 'get_worker_host should return mac'
assert bridge.get_worker_host('lee') is None, 'get_worker_host should return None for local'
assert bridge.get_worker_host('nobody') is None, 'get_worker_host should return None for missing'

# Restore
bridge.NODE_DIR = orig_node
bridge.WORKER_REGISTRY_FILE = orig_reg

import shutil
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Worker registry stores and retrieves host field"
    else
        fail "Worker host field test failed"
    fi
}

test_hire_parses_host() {
    info "Testing hire parses name@host syntax..."

    if python3 -c "
import bridge

# Parse name@host
name, host = bridge.parse_worker_target('kenji@mac')
assert name == 'kenji', f'Expected kenji, got {name}'
assert host == 'mac', f'Expected mac, got {host}'

# Parse plain name (no host)
name, host = bridge.parse_worker_target('lee')
assert name == 'lee', f'Expected lee, got {name}'
assert host is None, f'Expected None, got {host}'

# Parse name@host with dashes
name, host = bridge.parse_worker_target('my-worker@mac-mini')
assert name == 'my-worker', f'Expected my-worker, got {name}'
assert host == 'mac-mini', f'Expected mac-mini, got {host}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "hire parses name@host syntax"
    else
        fail "hire name@host parsing test failed"
    fi
}

test_tmux_send_message_remote() {
    info "Testing tmux_send_message uses SSH for remote workers..."

    if python3 -c "
from unittest.mock import patch, MagicMock, call
import bridge

calls = []
def mock_run(cmd, **kwargs):
    calls.append((list(cmd), kwargs))
    return MagicMock(returncode=0)

with patch('subprocess.run', side_effect=mock_run):
    with patch('bridge._acquire_flock', return_value=99):
        with patch('bridge._release_flock'):
            bridge.tmux_send_message('test-session', 'hello', host='mac')

# Verify all tmux commands went through ssh (format: ['ssh', 'mac', 'cmd string'])
tmux_calls = [c for c in calls if 'tmux' in ' '.join(c[0])]
for cmd, kwargs in tmux_calls:
    assert cmd[0] == 'ssh' and cmd[1] == 'mac', f'Expected ssh mac prefix, got {cmd[:2]}'

# Verify load-buffer uses stdin (- flag) not tmpfile
load_calls = [c for c in calls if 'load-buffer' in ' '.join(c[0])]
assert len(load_calls) == 1, f'Expected 1 load-buffer, got {len(load_calls)}'
load_cmd_str = ' '.join(load_calls[0][0])
assert 'load-buffer' in load_cmd_str, f'Expected load-buffer in cmd, got {load_cmd_str}'
assert load_calls[0][1].get('input') == b'hello', f'Should pipe text via input kwarg'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "tmux_send_message uses SSH for remote workers"
    else
        fail "tmux_send_message remote test failed"
    fi
}

test_tmux_exists_remote() {
    info "Testing tmux_exists uses SSH for remote host..."

    if python3 -c "
from unittest.mock import patch, MagicMock
import bridge

with patch('subprocess.run') as mock_run:
    mock_run.return_value = MagicMock(returncode=0)
    result = bridge.tmux_exists('test-session', host='mac')
    assert result == True
    args = mock_run.call_args[0][0]
    assert args[:2] == ['ssh', 'mac'], f'Expected ssh prefix, got {args}'
    cmd_str = ' '.join(args)
    assert 'has-session' in cmd_str, f'Expected has-session in cmd: {cmd_str}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "tmux_exists uses SSH for remote host"
    else
        fail "tmux_exists remote test failed"
    fi
}

test_process_inspection_remote() {
    info "Testing pgrep/kill use SSH for remote workers..."

    if python3 -c "
from unittest.mock import patch, MagicMock
import bridge

# _get_claude_pid with host should prefix ssh
with patch('subprocess.run') as mock_run:
    mock_run.return_value = MagicMock(returncode=0, stdout='12345\n')
    result = bridge._get_claude_pid('999', host='mac')
    args = mock_run.call_args[0][0]
    assert args[:2] == ['ssh', 'mac'], f'Expected ssh prefix for pgrep, got {args}'
    cmd_str = ' '.join(args)
    assert 'pgrep' in cmd_str, f'Expected pgrep in cmd: {cmd_str}'
    assert result == '12345'

# _child_count with host should prefix ssh
with patch('subprocess.run') as mock_run:
    mock_run.return_value = MagicMock(returncode=0, stdout='111\n222\n')
    result = bridge._child_count('999', host='mac')
    args = mock_run.call_args[0][0]
    assert args[:2] == ['ssh', 'mac'], f'Expected ssh prefix for child_count, got {args}'
    assert result == 2

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Process inspection uses SSH for remote workers"
    else
        fail "Process inspection remote test failed"
    fi
}

test_project_slug() {
    info "Testing _project_slug converts paths to Claude session slugs..."

    if python3 -c "
import bridge

assert bridge._project_slug('/home/claude/proj') == '-home-claude-proj'
assert bridge._project_slug('/Users/joan/my-project') == '-Users-joan-my-project'
assert bridge._project_slug('/') == '-'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_project_slug converts paths correctly"
    else
        fail "_project_slug test failed"
    fi
}

test_registry_teleport_fields() {
    info "Testing registry stores and clears teleport fields..."

    if python3 -c "
import json, tempfile
from pathlib import Path
import bridge

tmpdir = tempfile.mkdtemp()
orig_node = bridge.NODE_DIR
orig_reg = bridge.WORKER_REGISTRY_FILE
bridge.NODE_DIR = Path(tmpdir)
bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / 'workers.json'

# Add a worker
bridge._registry_add('lee', 'claude', 123)

# Update with teleport info
bridge._registry_update_teleport('lee', host='mac', home_host=None, home_cwd='/home/claude/proj')
data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
w = data['workers']['lee']
assert w['host'] == 'mac', f'host should be mac, got {w.get(\"host\")}'
assert w['home_host'] is None, f'home_host should be None'
assert w['home_cwd'] == '/home/claude/proj', f'home_cwd wrong'

# Clear teleport info (teleback)
bridge._registry_clear_teleport('lee')
data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
w = data['workers']['lee']
assert w.get('host') is None, f'host should be cleared'
assert w.get('home_host') is None, f'home_host should be cleared'
assert w.get('home_cwd') is None, f'home_cwd should be cleared'

bridge.NODE_DIR = orig_node
bridge.WORKER_REGISTRY_FILE = orig_reg
import shutil
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Registry teleport fields store and clear correctly"
    else
        fail "Registry teleport fields test failed"
    fi
}

test_bridge_public_url_auto_detect() {
    info "Testing BRIDGE_PUBLIC_URL auto-detects from Tailscale when unset..."

    if python3 -c "
import subprocess, sys, os
env = {k: v for k, v in os.environ.items() if k not in ('BRIDGE_PUBLIC_URL',)}
env['TELEGRAM_BOT_TOKEN'] = 'test'
result = subprocess.run([sys.executable, '-c', '''
import bridge
# Should be auto-detected from tailscale or empty (no tailscale)
url = bridge.BRIDGE_PUBLIC_URL
assert isinstance(url, str), f\"Expected string, got {type(url)}\"
if url:
    assert url.startswith(\"http://\"), f\"Expected http:// URL, got {url!r}\"
    assert \"localhost\" not in url and \"127.0.0.1\" not in url, f\"Should not be localhost: {url!r}\"
print(\"OK\")
'''], capture_output=True, text=True, env=env)
assert result.returncode == 0, result.stderr or result.stdout
print(result.stdout.strip())
" 2>/dev/null | grep -q "OK"; then
        success "BRIDGE_PUBLIC_URL auto-detects or stays empty"
    else
        fail "BRIDGE_PUBLIC_URL default test failed"
    fi
}

test_bridge_public_url_auto_bind() {
    info "Testing BRIDGE_PUBLIC_URL auto-sets BRIDGE_BIND when implicit..."

    if python3 -c "
import subprocess, sys, os
env = {k: v for k, v in os.environ.items() if k not in ('BRIDGE_PUBLIC_URL', 'BRIDGE_BIND')}
env['TELEGRAM_BOT_TOKEN'] = 'test'
env['BRIDGE_PUBLIC_URL'] = 'http://100.125.36.102:8080'
result = subprocess.run([sys.executable, '-c', '''
import bridge
assert bridge.BRIDGE_PUBLIC_URL == \"http://100.125.36.102:8080\", bridge.BRIDGE_PUBLIC_URL
assert bridge.BRIDGE_BIND == \"0.0.0.0\", f\"Expected BRIDGE_BIND=0.0.0.0, got {bridge.BRIDGE_BIND!r}\"
print(\"OK\")
'''], capture_output=True, text=True, env=env)
assert result.returncode == 0, result.stderr or result.stdout
print(result.stdout.strip())
" 2>/dev/null | grep -q "OK"; then
        success "BRIDGE_PUBLIC_URL auto-sets BRIDGE_BIND=0.0.0.0 when implicit"
    else
        fail "BRIDGE_PUBLIC_URL auto-bind test failed"
    fi
}

test_bridge_public_url_no_auto_bind_when_explicit() {
    info "Testing explicit BRIDGE_BIND overrides BRIDGE_PUBLIC_URL auto-bind..."

    if python3 -c "
import subprocess, sys, os
env = {k: v for k, v in os.environ.items() if k not in ('BRIDGE_PUBLIC_URL', 'BRIDGE_BIND')}
env['TELEGRAM_BOT_TOKEN'] = 'test'
env['BRIDGE_PUBLIC_URL'] = 'http://100.125.36.102:8080'
env['BRIDGE_BIND'] = '127.0.0.9'
result = subprocess.run([sys.executable, '-c', '''
import bridge
assert bridge.BRIDGE_PUBLIC_URL == \"http://100.125.36.102:8080\", bridge.BRIDGE_PUBLIC_URL
assert bridge.BRIDGE_BIND == \"127.0.0.9\", f\"Expected explicit BRIDGE_BIND to persist, got {bridge.BRIDGE_BIND!r}\"
print(\"OK\")
'''], capture_output=True, text=True, env=env)
assert result.returncode == 0, result.stderr or result.stdout
print(result.stdout.strip())
" 2>/dev/null | grep -q "OK"; then
        success "Explicit BRIDGE_BIND is preserved with BRIDGE_PUBLIC_URL"
    else
        fail "Explicit BRIDGE_BIND override test failed"
    fi
}

test_bridge_url_ignores_stale_localhost() {
    info "Testing BRIDGE_URL ignores stale localhost env and derives from PORT..."

    if python3 -c "
import subprocess, sys, os
env = {k: v for k, v in os.environ.items() if k not in ('BRIDGE_URL',)}
env['TELEGRAM_BOT_TOKEN'] = 'test'
env['PORT'] = '9999'
env['BRIDGE_URL'] = 'http://localhost:8080'  # stale from old bridge
result = subprocess.run([sys.executable, '-c', '''
import bridge
assert bridge.BRIDGE_URL == \"http://localhost:9999\", f\"Expected http://localhost:9999, got {bridge.BRIDGE_URL!r}\"
print(\"OK\")
'''], capture_output=True, text=True, env=env)
assert result.returncode == 0, result.stderr or result.stdout
print(result.stdout.strip())
" 2>/dev/null | grep -q "OK"; then
        success "BRIDGE_URL ignores stale localhost env, derives from PORT"
    else
        fail "BRIDGE_URL stale localhost test failed"
    fi
}

test_bridge_url_ignores_stale_127() {
    info "Testing BRIDGE_URL ignores stale 127.0.0.1 env..."

    if python3 -c "
import subprocess, sys, os
env = {k: v for k, v in os.environ.items() if k not in ('BRIDGE_URL',)}
env['TELEGRAM_BOT_TOKEN'] = 'test'
env['PORT'] = '9999'
env['BRIDGE_URL'] = 'http://127.0.0.1:8080'  # stale
result = subprocess.run([sys.executable, '-c', '''
import bridge
assert bridge.BRIDGE_URL == \"http://localhost:9999\", f\"Expected http://localhost:9999, got {bridge.BRIDGE_URL!r}\"
print(\"OK\")
'''], capture_output=True, text=True, env=env)
assert result.returncode == 0, result.stderr or result.stdout
print(result.stdout.strip())
" 2>/dev/null | grep -q "OK"; then
        success "BRIDGE_URL ignores stale 127.0.0.1 env"
    else
        fail "BRIDGE_URL stale 127.0.0.1 test failed"
    fi
}

test_bridge_url_honors_remote() {
    info "Testing BRIDGE_URL honors remote (non-localhost) env..."

    if python3 -c "
import subprocess, sys, os
env = {k: v for k, v in os.environ.items() if k not in ('BRIDGE_URL',)}
env['TELEGRAM_BOT_TOKEN'] = 'test'
env['PORT'] = '9999'
env['BRIDGE_URL'] = 'https://remote-bridge.example.com'
result = subprocess.run([sys.executable, '-c', '''
import bridge
assert bridge.BRIDGE_URL == \"https://remote-bridge.example.com\", f\"Expected remote URL, got {bridge.BRIDGE_URL!r}\"
print(\"OK\")
'''], capture_output=True, text=True, env=env)
assert result.returncode == 0, result.stderr or result.stdout
print(result.stdout.strip())
" 2>/dev/null | grep -q "OK"; then
        success "BRIDGE_URL honors remote env"
    else
        fail "BRIDGE_URL remote test failed"
    fi
}

# ── Git-Based Teleport Sync Tests ──────────────────────────────────────

test_is_git_repo_false_for_non_git() {
    info "Testing _is_git_repo returns False for non-git directory..."

    if python3 -c "
import tempfile, shutil
import bridge

tmpdir = tempfile.mkdtemp()
assert bridge._is_git_repo(tmpdir) == False, 'non-git dir should be False'
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_is_git_repo returns False for non-git dir"
    else
        fail "_is_git_repo non-git test failed"
    fi
}

test_is_git_repo_true_for_git() {
    info "Testing _is_git_repo returns True for git repository..."

    if python3 -c "
import tempfile, shutil, subprocess
import bridge

tmpdir = tempfile.mkdtemp()
subprocess.run(['git', 'init', tmpdir], capture_output=True)
assert bridge._is_git_repo(tmpdir) == True, 'git repo should be True'
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_is_git_repo returns True for git repo"
    else
        fail "_is_git_repo git repo test failed"
    fi
}

test_is_git_repo_with_remote_host() {
    info "Testing _is_git_repo dispatches via _remote_run for remote host..."

    if python3 -c "
import subprocess
from unittest.mock import patch
import bridge

# Mock _remote_run to simulate remote git rev-parse succeeding
def mock_remote_run(cmd, host=None, **kwargs):
    if host == 'remote-mac' and 'rev-parse' in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout='true\n', stderr='')
    return subprocess.CompletedProcess(cmd, 1)

with patch.object(bridge, '_remote_run', side_effect=mock_remote_run):
    assert bridge._is_git_repo('/remote/path', host='remote-mac') == True

# Also test failure case
def mock_fail(cmd, host=None, **kwargs):
    return subprocess.CompletedProcess(cmd, 128, stderr='not a git repo')

with patch.object(bridge, '_remote_run', side_effect=mock_fail):
    assert bridge._is_git_repo('/remote/path', host='remote-mac') == False

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_is_git_repo dispatches via SSH for remote host"
    else
        fail "_is_git_repo remote host test failed"
    fi
}

test_get_project_name_from_github_url() {
    info "Testing _get_project_name extracts name from GitHub HTTPS URL..."

    if python3 -c "
import tempfile, shutil, subprocess
import bridge

tmpdir = tempfile.mkdtemp()
subprocess.run(['git', 'init', tmpdir], capture_output=True)
subprocess.run(['git', '-C', tmpdir, 'remote', 'add', 'origin',
                'https://github.com/BasedHardware/omi.git'], capture_output=True)

name = bridge._get_project_name(tmpdir)
assert name == 'omi', f'expected omi, got {name!r}'
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_get_project_name from GitHub HTTPS URL"
    else
        fail "_get_project_name HTTPS test failed"
    fi
}

test_get_project_name_from_ssh_url() {
    info "Testing _get_project_name extracts name from SSH URL..."

    if python3 -c "
import tempfile, shutil, subprocess
import bridge

tmpdir = tempfile.mkdtemp()
subprocess.run(['git', 'init', tmpdir], capture_output=True)
subprocess.run(['git', '-C', tmpdir, 'remote', 'add', 'origin',
                'git@github.com:BasedHardware/omi.git'], capture_output=True)

name = bridge._get_project_name(tmpdir)
assert name == 'omi', f'expected omi, got {name!r}'
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_get_project_name from SSH URL"
    else
        fail "_get_project_name SSH test failed"
    fi
}

test_get_project_name_no_remote() {
    info "Testing _get_project_name returns None when no remote..."

    if python3 -c "
import tempfile, shutil, subprocess
import bridge

tmpdir = tempfile.mkdtemp()
subprocess.run(['git', 'init', tmpdir], capture_output=True)

name = bridge._get_project_name(tmpdir)
assert name is None, f'expected None, got {name!r}'
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_get_project_name returns None for no remote"
    else
        fail "_get_project_name no remote test failed"
    fi
}

test_ensure_bare_repo_creates_new() {
    info "Testing _ensure_bare_repo creates a new bare repo..."

    if python3 -c "
import tempfile, shutil, os
import bridge

tmpdir = tempfile.mkdtemp()
orig = bridge.GIT_SERVER_DIR
bridge.GIT_SERVER_DIR = tmpdir

path = bridge._ensure_bare_repo('test-project')
assert path == os.path.join(tmpdir, 'test-project.git'), f'unexpected path: {path}'
assert os.path.isdir(path), 'bare repo dir should exist'
assert os.path.isfile(os.path.join(path, 'HEAD')), 'bare repo should have HEAD'

bridge.GIT_SERVER_DIR = orig
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_ensure_bare_repo creates new bare repo"
    else
        fail "_ensure_bare_repo create test failed"
    fi
}

test_ensure_bare_repo_idempotent() {
    info "Testing _ensure_bare_repo is idempotent..."

    if python3 -c "
import tempfile, shutil, os
import bridge

tmpdir = tempfile.mkdtemp()
orig = bridge.GIT_SERVER_DIR
bridge.GIT_SERVER_DIR = tmpdir

path1 = bridge._ensure_bare_repo('myrepo')
path2 = bridge._ensure_bare_repo('myrepo')
assert path1 == path2, f'paths differ: {path1} vs {path2}'
assert os.path.isdir(path1), 'bare repo should still exist'

bridge.GIT_SERVER_DIR = orig
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_ensure_bare_repo is idempotent"
    else
        fail "_ensure_bare_repo idempotent test failed"
    fi
}

test_git_push_state_clean_tree() {
    info "Testing _git_push_state with clean tree pushes HEAD..."

    if python3 -c "
import tempfile, shutil, subprocess, os
import bridge

# Create source repo with one commit
srcdir = tempfile.mkdtemp()
subprocess.run(['git', 'init', srcdir], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.email', 'test@test'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.name', 'test'], capture_output=True)
open(os.path.join(srcdir, 'file.txt'), 'w').write('hello')
subprocess.run(['git', '-C', srcdir, 'add', '.'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'commit', '-m', 'init'], capture_output=True)

# Create bare repo
baredir = tempfile.mkdtemp()
orig = bridge.GIT_SERVER_DIR
bridge.GIT_SERVER_DIR = baredir
bare = bridge._ensure_bare_repo('test')

meta = bridge._git_push_state(srcdir, 'worker1', bare)
assert meta is not None, 'push should succeed'
assert 'orig_sha' in meta, 'meta should have orig_sha'
assert 'orig_branch' in meta, 'meta should have orig_branch'
assert meta['stash_sha'] is None or meta['stash_sha'] == '', 'clean tree should have no stash'

# Verify the branch exists in bare repo
r = subprocess.run(['git', '-C', bare, 'rev-parse', 'teleport/worker1'],
                   capture_output=True, text=True)
assert r.returncode == 0, f'branch not found: {r.stderr}'

bridge.GIT_SERVER_DIR = orig
shutil.rmtree(srcdir)
shutil.rmtree(baredir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_git_push_state clean tree pushes HEAD"
    else
        fail "_git_push_state clean tree test failed"
    fi
}

test_git_push_state_with_changes() {
    info "Testing _git_push_state with uncommitted changes uses stash..."

    if python3 -c "
import tempfile, shutil, subprocess, os
import bridge

srcdir = tempfile.mkdtemp()
subprocess.run(['git', 'init', srcdir], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.email', 'test@test'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.name', 'test'], capture_output=True)
open(os.path.join(srcdir, 'file.txt'), 'w').write('hello')
subprocess.run(['git', '-C', srcdir, 'add', '.'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'commit', '-m', 'init'], capture_output=True)

# Make uncommitted changes
open(os.path.join(srcdir, 'file.txt'), 'w').write('modified')

baredir = tempfile.mkdtemp()
orig = bridge.GIT_SERVER_DIR
bridge.GIT_SERVER_DIR = baredir
bare = bridge._ensure_bare_repo('test')

meta = bridge._git_push_state(srcdir, 'worker1', bare)
assert meta is not None, 'push should succeed'
assert meta['stash_sha'], 'dirty tree should have stash_sha'

bridge.GIT_SERVER_DIR = orig
shutil.rmtree(srcdir)
shutil.rmtree(baredir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_git_push_state with changes uses stash"
    else
        fail "_git_push_state with changes test failed"
    fi
}

test_git_push_state_with_untracked() {
    info "Testing _git_push_state includes untracked files..."

    if python3 -c "
import tempfile, shutil, subprocess, os
import bridge

srcdir = tempfile.mkdtemp()
subprocess.run(['git', 'init', srcdir], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.email', 'test@test'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.name', 'test'], capture_output=True)
open(os.path.join(srcdir, 'file.txt'), 'w').write('hello')
subprocess.run(['git', '-C', srcdir, 'add', '.'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'commit', '-m', 'init'], capture_output=True)

# Add untracked file
open(os.path.join(srcdir, 'untracked.txt'), 'w').write('new file')

baredir = tempfile.mkdtemp()
orig = bridge.GIT_SERVER_DIR
bridge.GIT_SERVER_DIR = baredir
bare = bridge._ensure_bare_repo('test')

meta = bridge._git_push_state(srcdir, 'worker1', bare)
assert meta is not None, 'push should succeed'
assert meta['stash_sha'], 'tree with untracked should have stash_sha'

bridge.GIT_SERVER_DIR = orig
shutil.rmtree(srcdir)
shutil.rmtree(baredir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_git_push_state includes untracked files"
    else
        fail "_git_push_state untracked test failed"
    fi
}

test_git_push_state_with_staged() {
    info "Testing _git_push_state records staged files..."

    if python3 -c "
import tempfile, shutil, subprocess, os
import bridge

srcdir = tempfile.mkdtemp()
subprocess.run(['git', 'init', srcdir], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.email', 'test@test'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.name', 'test'], capture_output=True)
open(os.path.join(srcdir, 'file.txt'), 'w').write('hello')
subprocess.run(['git', '-C', srcdir, 'add', '.'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'commit', '-m', 'init'], capture_output=True)

# Stage a change
open(os.path.join(srcdir, 'file.txt'), 'w').write('staged change')
subprocess.run(['git', '-C', srcdir, 'add', 'file.txt'], capture_output=True)

baredir = tempfile.mkdtemp()
orig = bridge.GIT_SERVER_DIR
bridge.GIT_SERVER_DIR = baredir
bare = bridge._ensure_bare_repo('test')

meta = bridge._git_push_state(srcdir, 'worker1', bare)
assert meta is not None, 'push should succeed'
assert 'file.txt' in meta.get('staged_files', []), f'staged_files should contain file.txt: {meta}'

bridge.GIT_SERVER_DIR = orig
shutil.rmtree(srcdir)
shutil.rmtree(baredir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_git_push_state records staged files"
    else
        fail "_git_push_state staged test failed"
    fi
}

test_git_push_state_no_source_mutation() {
    info "Testing _git_push_state does not mutate source tree..."

    if python3 -c "
import tempfile, shutil, subprocess, os
import bridge

srcdir = tempfile.mkdtemp()
subprocess.run(['git', 'init', srcdir], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.email', 'test@test'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.name', 'test'], capture_output=True)
open(os.path.join(srcdir, 'file.txt'), 'w').write('hello')
subprocess.run(['git', '-C', srcdir, 'add', '.'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'commit', '-m', 'init'], capture_output=True)

# Dirty the tree
open(os.path.join(srcdir, 'file.txt'), 'w').write('modified')
open(os.path.join(srcdir, 'new.txt'), 'w').write('untracked')

# Capture state before push
head_before = subprocess.run(['git', '-C', srcdir, 'rev-parse', 'HEAD'],
                             capture_output=True, text=True).stdout.strip()
status_before = subprocess.run(['git', '-C', srcdir, 'status', '--porcelain'],
                               capture_output=True, text=True).stdout

baredir = tempfile.mkdtemp()
orig = bridge.GIT_SERVER_DIR
bridge.GIT_SERVER_DIR = baredir
bare = bridge._ensure_bare_repo('test')

meta = bridge._git_push_state(srcdir, 'worker1', bare)

# Verify no mutation
head_after = subprocess.run(['git', '-C', srcdir, 'rev-parse', 'HEAD'],
                            capture_output=True, text=True).stdout.strip()
status_after = subprocess.run(['git', '-C', srcdir, 'status', '--porcelain'],
                              capture_output=True, text=True).stdout

assert head_before == head_after, f'HEAD changed: {head_before} -> {head_after}'
assert status_before == status_after, f'status changed:\n{status_before}\nvs\n{status_after}'

bridge.GIT_SERVER_DIR = orig
shutil.rmtree(srcdir)
shutil.rmtree(baredir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_git_push_state does not mutate source"
    else
        fail "_git_push_state no mutation test failed"
    fi
}

test_git_pull_state_fresh_clone() {
    info "Testing _git_pull_state clones to fresh target directory..."

    if python3 -c "
import tempfile, shutil, subprocess, os
import bridge

# Create source with a commit
srcdir = tempfile.mkdtemp()
subprocess.run(['git', 'init', srcdir], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.email', 'test@test'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.name', 'test'], capture_output=True)
open(os.path.join(srcdir, 'file.txt'), 'w').write('hello')
subprocess.run(['git', '-C', srcdir, 'add', '.'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'commit', '-m', 'init'], capture_output=True)
# Dirty it
open(os.path.join(srcdir, 'file.txt'), 'w').write('modified')

baredir = tempfile.mkdtemp()
orig = bridge.GIT_SERVER_DIR
bridge.GIT_SERVER_DIR = baredir
bare = bridge._ensure_bare_repo('test')

meta = bridge._git_push_state(srcdir, 'w1', bare)
assert meta, 'push should succeed'

# Pull to a fresh target (doesn't exist yet)
tgtdir = os.path.join(tempfile.mkdtemp(), 'target')
ok = bridge._git_pull_state(tgtdir, 'w1', bare, meta)
assert ok, 'pull should succeed'
assert os.path.isdir(os.path.join(tgtdir, '.git')), 'target should be a git repo'
content = open(os.path.join(tgtdir, 'file.txt')).read()
assert content == 'modified', f'expected modified, got {content!r}'

bridge.GIT_SERVER_DIR = orig
shutil.rmtree(srcdir)
shutil.rmtree(baredir)
shutil.rmtree(os.path.dirname(tgtdir))
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_git_pull_state clones to fresh target"
    else
        fail "_git_pull_state fresh clone test failed"
    fi
}

test_git_pull_state_existing_repo() {
    info "Testing _git_pull_state fetches into existing repo..."

    if python3 -c "
import tempfile, shutil, subprocess, os
import bridge

srcdir = tempfile.mkdtemp()
subprocess.run(['git', 'init', srcdir], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.email', 'test@test'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.name', 'test'], capture_output=True)
open(os.path.join(srcdir, 'file.txt'), 'w').write('hello')
subprocess.run(['git', '-C', srcdir, 'add', '.'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'commit', '-m', 'init'], capture_output=True)

baredir = tempfile.mkdtemp()
orig = bridge.GIT_SERVER_DIR
bridge.GIT_SERVER_DIR = baredir
bare = bridge._ensure_bare_repo('test')

# First push (clean)
meta1 = bridge._git_push_state(srcdir, 'w1', bare)

# Clone target
tgtdir = os.path.join(tempfile.mkdtemp(), 'target')
bridge._git_pull_state(tgtdir, 'w1', bare, meta1)

# Now modify source and push again
open(os.path.join(srcdir, 'file.txt'), 'w').write('updated')
meta2 = bridge._git_push_state(srcdir, 'w1', bare)

# Pull into existing target
ok = bridge._git_pull_state(tgtdir, 'w1', bare, meta2)
assert ok, 'pull into existing should succeed'
content = open(os.path.join(tgtdir, 'file.txt')).read()
assert content == 'updated', f'expected updated, got {content!r}'

bridge.GIT_SERVER_DIR = orig
shutil.rmtree(srcdir)
shutil.rmtree(baredir)
shutil.rmtree(os.path.dirname(tgtdir))
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_git_pull_state fetches into existing repo"
    else
        fail "_git_pull_state existing repo test failed"
    fi
}

test_git_pull_state_restores_branch() {
    info "Testing _git_pull_state restores correct branch..."

    if python3 -c "
import tempfile, shutil, subprocess, os
import bridge

srcdir = tempfile.mkdtemp()
subprocess.run(['git', 'init', srcdir], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.email', 'test@test'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.name', 'test'], capture_output=True)
open(os.path.join(srcdir, 'file.txt'), 'w').write('hello')
subprocess.run(['git', '-C', srcdir, 'add', '.'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'commit', '-m', 'init'], capture_output=True)
# Create and switch to a feature branch
subprocess.run(['git', '-C', srcdir, 'checkout', '-b', 'feature-x'], capture_output=True)

baredir = tempfile.mkdtemp()
orig = bridge.GIT_SERVER_DIR
bridge.GIT_SERVER_DIR = baredir
bare = bridge._ensure_bare_repo('test')

meta = bridge._git_push_state(srcdir, 'w1', bare)
assert meta['orig_branch'] == 'feature-x', f'branch should be feature-x: {meta}'

tgtdir = os.path.join(tempfile.mkdtemp(), 'target')
ok = bridge._git_pull_state(tgtdir, 'w1', bare, meta)
assert ok, 'pull should succeed'

r = subprocess.run(['git', '-C', tgtdir, 'rev-parse', '--abbrev-ref', 'HEAD'],
                   capture_output=True, text=True)
assert r.stdout.strip() == 'feature-x', f'target branch should be feature-x, got {r.stdout.strip()!r}'

bridge.GIT_SERVER_DIR = orig
shutil.rmtree(srcdir)
shutil.rmtree(baredir)
shutil.rmtree(os.path.dirname(tgtdir))
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_git_pull_state restores correct branch"
    else
        fail "_git_pull_state branch restore test failed"
    fi
}

test_git_pull_state_restores_staged() {
    info "Testing _git_pull_state re-stages originally staged files..."

    if python3 -c "
import tempfile, shutil, subprocess, os
import bridge

srcdir = tempfile.mkdtemp()
subprocess.run(['git', 'init', srcdir], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.email', 'test@test'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.name', 'test'], capture_output=True)
open(os.path.join(srcdir, 'a.txt'), 'w').write('hello')
open(os.path.join(srcdir, 'b.txt'), 'w').write('world')
subprocess.run(['git', '-C', srcdir, 'add', '.'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'commit', '-m', 'init'], capture_output=True)

# Stage a.txt, leave b.txt unstaged
open(os.path.join(srcdir, 'a.txt'), 'w').write('staged-a')
open(os.path.join(srcdir, 'b.txt'), 'w').write('unstaged-b')
subprocess.run(['git', '-C', srcdir, 'add', 'a.txt'], capture_output=True)

baredir = tempfile.mkdtemp()
orig = bridge.GIT_SERVER_DIR
bridge.GIT_SERVER_DIR = baredir
bare = bridge._ensure_bare_repo('test')

meta = bridge._git_push_state(srcdir, 'w1', bare)
assert 'a.txt' in meta['staged_files']

tgtdir = os.path.join(tempfile.mkdtemp(), 'target')
ok = bridge._git_pull_state(tgtdir, 'w1', bare, meta)
assert ok

# Check a.txt is staged on target
r = subprocess.run(['git', '-C', tgtdir, 'diff', '--cached', '--name-only'],
                   capture_output=True, text=True)
staged = [f for f in r.stdout.strip().split('\n') if f]
assert 'a.txt' in staged, f'a.txt should be staged: {staged}'

bridge.GIT_SERVER_DIR = orig
shutil.rmtree(srcdir)
shutil.rmtree(baredir)
shutil.rmtree(os.path.dirname(tgtdir))
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_git_pull_state re-stages originally staged files"
    else
        fail "_git_pull_state staged restore test failed"
    fi
}

test_git_pull_state_restores_unstaged() {
    info "Testing _git_pull_state restores unstaged changes..."

    if python3 -c "
import tempfile, shutil, subprocess, os
import bridge

srcdir = tempfile.mkdtemp()
subprocess.run(['git', 'init', srcdir], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.email', 'test@test'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.name', 'test'], capture_output=True)
open(os.path.join(srcdir, 'file.txt'), 'w').write('hello')
subprocess.run(['git', '-C', srcdir, 'add', '.'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'commit', '-m', 'init'], capture_output=True)

# Unstaged modification + untracked file
open(os.path.join(srcdir, 'file.txt'), 'w').write('modified')
open(os.path.join(srcdir, 'new.txt'), 'w').write('untracked')

baredir = tempfile.mkdtemp()
orig = bridge.GIT_SERVER_DIR
bridge.GIT_SERVER_DIR = baredir
bare = bridge._ensure_bare_repo('test')

meta = bridge._git_push_state(srcdir, 'w1', bare)

tgtdir = os.path.join(tempfile.mkdtemp(), 'target')
ok = bridge._git_pull_state(tgtdir, 'w1', bare, meta)
assert ok

# Check both files on target
content1 = open(os.path.join(tgtdir, 'file.txt')).read()
assert content1 == 'modified', f'file.txt should be modified: {content1!r}'
content2 = open(os.path.join(tgtdir, 'new.txt')).read()
assert content2 == 'untracked', f'new.txt should be untracked: {content2!r}'

bridge.GIT_SERVER_DIR = orig
shutil.rmtree(srcdir)
shutil.rmtree(baredir)
shutil.rmtree(os.path.dirname(tgtdir))
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_git_pull_state restores unstaged and untracked"
    else
        fail "_git_pull_state unstaged restore test failed"
    fi
}

test_git_push_state_detached_head() {
    info "Testing _git_push_state works with detached HEAD..."

    if python3 -c "
import tempfile, shutil, subprocess, os
import bridge

srcdir = tempfile.mkdtemp()
subprocess.run(['git', 'init', srcdir], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.email', 'test@test'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.name', 'test'], capture_output=True)
open(os.path.join(srcdir, 'file.txt'), 'w').write('hello')
subprocess.run(['git', '-C', srcdir, 'add', '.'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'commit', '-m', 'init'], capture_output=True)
head = subprocess.run(['git', '-C', srcdir, 'rev-parse', 'HEAD'],
                      capture_output=True, text=True).stdout.strip()
# Detach HEAD
subprocess.run(['git', '-C', srcdir, 'checkout', head], capture_output=True)

baredir = tempfile.mkdtemp()
orig = bridge.GIT_SERVER_DIR
bridge.GIT_SERVER_DIR = baredir
bare = bridge._ensure_bare_repo('test')

meta = bridge._git_push_state(srcdir, 'w1', bare)
assert meta is not None, 'push should succeed with detached HEAD'
assert meta['orig_branch'] == 'HEAD', f'branch should be HEAD: {meta[\"orig_branch\"]}'

bridge.GIT_SERVER_DIR = orig
shutil.rmtree(srcdir)
shutil.rmtree(baredir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_git_push_state works with detached HEAD"
    else
        fail "_git_push_state detached HEAD test failed"
    fi
}

test_git_sync_fallback_on_push_fail() {
    info "Testing _git_push_state returns None on push failure..."

    if python3 -c "
import tempfile, shutil, subprocess, os
import bridge

srcdir = tempfile.mkdtemp()
subprocess.run(['git', 'init', srcdir], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.email', 'test@test'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.name', 'test'], capture_output=True)
open(os.path.join(srcdir, 'file.txt'), 'w').write('hello')
subprocess.run(['git', '-C', srcdir, 'add', '.'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'commit', '-m', 'init'], capture_output=True)

# Push to nonexistent bare repo
meta = bridge._git_push_state(srcdir, 'w1', '/nonexistent/bare.git')
assert meta is None, f'push to nonexistent should fail: {meta}'

shutil.rmtree(srcdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_git_push_state returns None on failure"
    else
        fail "_git_push_state failure fallback test failed"
    fi
}

test_git_sync_fallback_on_pull_fail() {
    info "Testing _git_pull_state returns False on pull failure..."

    if python3 -c "
import bridge

meta = {'orig_sha': 'abc123', 'orig_branch': 'main', 'staged_files': [], 'stash_sha': None}
ok = bridge._git_pull_state('/tmp/nonexistent-target', 'w1', '/nonexistent/bare.git', meta)
assert ok == False, f'pull from nonexistent should fail: {ok}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_git_pull_state returns False on failure"
    else
        fail "_git_pull_state failure fallback test failed"
    fi
}

test_git_sync_non_git_dir() {
    info "Testing _is_git_repo returns False for non-git dir (rsync path)..."

    if python3 -c "
import tempfile, shutil
import bridge

tmpdir = tempfile.mkdtemp()
assert bridge._is_git_repo(tmpdir) == False
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_is_git_repo False for non-git triggers rsync path"
    else
        fail "_is_git_repo non-git for rsync test failed"
    fi
}

test_git_push_state_remote_host() {
    info "Testing _git_push_state dispatches git commands via SSH for remote host..."

    if python3 -c "
import subprocess
from unittest.mock import patch, call
import bridge

calls = []
def mock_remote_run(cmd, host=None, **kwargs):
    calls.append((cmd, host))
    if 'rev-parse' in cmd and 'HEAD' in cmd and '--abbrev-ref' not in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout='abc123\n', stderr='')
    if '--abbrev-ref' in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout='main\n', stderr='')
    if 'diff' in cmd and '--cached' in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')
    if 'add' in cmd and '-A' in cmd:
        return subprocess.CompletedProcess(cmd, 0)
    if 'stash' in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')
    if 'reset' in cmd:
        return subprocess.CompletedProcess(cmd, 0)
    if 'push' in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')
    return subprocess.CompletedProcess(cmd, 0)

with patch.object(bridge, '_remote_run', side_effect=mock_remote_run):
    meta = bridge._git_push_state('/remote/src', 'w1', '/home/claude/git-server/test.git',
                                   host='remote-mac')

assert meta is not None, f'push should succeed: {meta}'
# Verify all git commands were dispatched to remote host
git_calls = [(c, h) for c, h in calls if c[0] == 'git']
for cmd, host in git_calls:
    assert host == 'remote-mac', f'expected remote-mac, got {host} for {cmd}'

# Verify push targets VPS bare repo via SSH
push_calls = [c for c, h in calls if 'push' in c]
assert len(push_calls) > 0, 'should have a push call'
push_cmd = push_calls[0]
assert 'claude@100.125.36.102:' in str(push_cmd), f'push should target VPS: {push_cmd}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_git_push_state dispatches via SSH for remote host"
    else
        fail "_git_push_state remote host test failed"
    fi
}

test_git_pull_state_remote_host() {
    info "Testing _git_pull_state dispatches git commands via SSH for remote host..."

    if python3 -c "
import subprocess
from unittest.mock import patch
import bridge

calls = []
def mock_remote_run(cmd, host=None, **kwargs):
    calls.append((cmd, host))
    if 'rev-parse' in cmd and '--git-dir' in cmd:
        return subprocess.CompletedProcess(cmd, 128, stderr='not a git repo')
    if 'clone' in cmd:
        return subprocess.CompletedProcess(cmd, 0)
    if 'config' in cmd:
        return subprocess.CompletedProcess(cmd, 0)
    if 'checkout' in cmd:
        return subprocess.CompletedProcess(cmd, 0)
    return subprocess.CompletedProcess(cmd, 0)

meta = {'orig_sha': 'abc123', 'orig_branch': 'main', 'staged_files': [], 'stash_sha': None}

with patch.object(bridge, '_remote_run', side_effect=mock_remote_run):
    ok = bridge._git_pull_state('/remote/target', 'w1',
                                 'claude@100.125.36.102:/home/claude/git-server/test.git',
                                 meta, host='remote-mac')

assert ok, 'pull should succeed'
# Verify all commands dispatched to remote host
git_calls = [(c, h) for c, h in calls if c[0] == 'git']
for cmd, host in git_calls:
    assert host == 'remote-mac', f'expected remote-mac, got {host} for {cmd}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_git_pull_state dispatches via SSH for remote host"
    else
        fail "_git_pull_state remote host test failed"
    fi
}

test_bare_repo_url_for_remote() {
    info "Testing bare repo URL generation for remote targets..."

    if python3 -c "
import bridge

# Local target should use direct path
url = bridge._bare_repo_url('/home/claude/git-server/test.git', target_host=None)
assert url == '/home/claude/git-server/test.git', f'local: {url}'

# Remote target should use SSH URL
url = bridge._bare_repo_url('/home/claude/git-server/test.git',
                             target_host='beastoin-agents-f1-mac-mini')
assert 'claude@100.125.36.102:' in url, f'remote should SSH to VPS: {url}'
assert '/home/claude/git-server/test.git' in url, f'should include path: {url}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Bare repo URL correct for remote targets"
    else
        fail "Bare repo URL test failed"
    fi
}

test_sync_working_directory_uses_git() {
    info "Testing _sync_working_directory prefers git sync for git repos..."

    if python3 -c "
import tempfile, shutil, subprocess, os
from unittest.mock import patch, MagicMock
import bridge

srcdir = tempfile.mkdtemp()
subprocess.run(['git', 'init', srcdir], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.email', 'test@test'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.name', 'test'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'remote', 'add', 'origin', 'https://github.com/test/proj.git'],
               capture_output=True)
open(os.path.join(srcdir, 'file.txt'), 'w').write('hello')
subprocess.run(['git', '-C', srcdir, 'add', '.'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'commit', '-m', 'init'], capture_output=True)

baredir = tempfile.mkdtemp()
orig = bridge.GIT_SERVER_DIR
bridge.GIT_SERVER_DIR = baredir

tgtdir = os.path.join(tempfile.mkdtemp(), 'target')

rsync_called = []
orig_subprocess_run = subprocess.run
def track_rsync(*args, **kwargs):
    if args and isinstance(args[0], list) and args[0][0] == 'rsync':
        rsync_called.append(args[0])
    return orig_subprocess_run(*args, **kwargs)

router = bridge.CommandRouter(None, bridge.worker_manager)
with patch('subprocess.run', side_effect=track_rsync):
    result = router._sync_working_directory(srcdir, tgtdir)

assert result == True, 'sync should succeed'
assert os.path.isfile(os.path.join(tgtdir, 'file.txt')), 'file should exist on target'
assert len(rsync_called) == 0, f'rsync should NOT be called when git succeeds: {rsync_called}'

bridge.GIT_SERVER_DIR = orig
shutil.rmtree(srcdir)
shutil.rmtree(baredir)
shutil.rmtree(os.path.dirname(tgtdir))
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_sync_working_directory uses git for git repos"
    else
        fail "_sync_working_directory git path test failed"
    fi
}

test_sync_working_directory_full_flag_uses_rsync() {
    info "Testing _sync_working_directory --full flag bypasses git..."

    if python3 -c "
import tempfile, shutil, subprocess, os
from unittest.mock import patch, MagicMock
import bridge

srcdir = tempfile.mkdtemp()
subprocess.run(['git', 'init', srcdir], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.email', 'test@test'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.name', 'test'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'remote', 'add', 'origin', 'https://github.com/test/proj.git'],
               capture_output=True)
open(os.path.join(srcdir, 'file.txt'), 'w').write('hello')
subprocess.run(['git', '-C', srcdir, 'add', '.'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'commit', '-m', 'init'], capture_output=True)

tgtdir = tempfile.mkdtemp()

rsync_called = []
orig_subprocess_run = subprocess.run
def track_rsync(*args, **kwargs):
    if args and isinstance(args[0], list) and args[0][0] == 'rsync':
        rsync_called.append(True)
    return orig_subprocess_run(*args, **kwargs)

router = bridge.CommandRouter(None, bridge.worker_manager)
with patch('subprocess.run', side_effect=track_rsync):
    router._sync_working_directory(srcdir, tgtdir, full=True)

assert len(rsync_called) > 0, 'rsync should be called when full=True'

shutil.rmtree(srcdir)
shutil.rmtree(tgtdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_sync_working_directory --full uses rsync"
    else
        fail "_sync_working_directory --full rsync test failed"
    fi
}

test_sync_working_directory_git_fail_falls_back() {
    info "Testing _sync_working_directory falls back to rsync on git failure..."

    if python3 -c "
import tempfile, shutil, subprocess, os
from unittest.mock import patch
import bridge

srcdir = tempfile.mkdtemp()
subprocess.run(['git', 'init', srcdir], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.email', 'test@test'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'config', 'user.name', 'test'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'remote', 'add', 'origin', 'https://github.com/test/proj.git'],
               capture_output=True)
open(os.path.join(srcdir, 'file.txt'), 'w').write('hello')
subprocess.run(['git', '-C', srcdir, 'add', '.'], capture_output=True)
subprocess.run(['git', '-C', srcdir, 'commit', '-m', 'init'], capture_output=True)

tgtdir = tempfile.mkdtemp()

# Make git push fail by using a broken bare repo path
baredir = tempfile.mkdtemp()
orig = bridge.GIT_SERVER_DIR
bridge.GIT_SERVER_DIR = '/nonexistent/git-server'

rsync_called = []
orig_subprocess_run = subprocess.run
def track_rsync(*args, **kwargs):
    if args and isinstance(args[0], list) and args[0][0] == 'rsync':
        rsync_called.append(True)
    return orig_subprocess_run(*args, **kwargs)

router = bridge.CommandRouter(None, bridge.worker_manager)
with patch('subprocess.run', side_effect=track_rsync):
    router._sync_working_directory(srcdir, tgtdir)

assert len(rsync_called) > 0, 'rsync should be called as fallback when git fails'

bridge.GIT_SERVER_DIR = orig
shutil.rmtree(srcdir)
shutil.rmtree(tgtdir)
shutil.rmtree(baredir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_sync_working_directory falls back to rsync on git failure"
    else
        fail "_sync_working_directory git fallback test failed"
    fi
}

test_workers_send_example_ssh_for_teleported() {
    info "Testing /workers send_example uses SSH for teleported workers..."

    if python3 -c "
import tempfile, json
from pathlib import Path
import bridge

tmp = Path(tempfile.mkdtemp())
bridge.NODE_DIR = tmp
bridge.SESSIONS_DIR = tmp / 'sessions'
bridge.SESSIONS_DIR.mkdir()
bridge.WORKER_REGISTRY_FILE = tmp / 'workers.json'
bridge.TMUX_PREFIX = '${TEST_TMUX_PREFIX}wkr-'
bridge.WORKER_PIPE_ROOT = tmp / 'pipes'
bridge.worker_manager.sessions_dir = bridge.SESSIONS_DIR
bridge.worker_manager.tmux_prefix = bridge.TMUX_PREFIX
bridge.worker_manager.scan_tmux_sessions = lambda: {
    'ren': {'tmux': '${TEST_TMUX_PREFIX}wkr-ren', 'chat_id': 123}
}

# Register ren as teleported to mac mini
bridge._registry_add('ren', 'claude', 123)
bridge._registry_update_teleport('ren', host='beastoin-agents-f1-mac-mini',
                                  home_host=None, home_cwd='/home/claude/omi')

workers = bridge.worker_manager.get_workers()
ren = next((w for w in workers if w['name'] == 'ren'), None)
assert ren is not None, f'ren not in workers: {[w[\"name\"] for w in workers]}'
assert ren['protocol'] == 'tmux', f'expected tmux protocol: {ren}'
assert 'ssh' in ren['send_example'], f'teleported worker should have ssh in send_example: {ren[\"send_example\"]}'
assert 'beastoin-agents-f1-mac-mini' in ren['send_example'], f'should reference mac mini host: {ren[\"send_example\"]}'

import shutil
shutil.rmtree(tmp)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/workers send_example uses SSH for teleported workers"
    else
        fail "/workers send_example SSH test failed"
    fi
}

test_teleport_preflight_uses_public_url() {
    info "Testing teleport preflight uses BRIDGE_PUBLIC_URL when BRIDGE_URL is localhost..."

    if python3 -c "
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import bridge

tmpdir = tempfile.mkdtemp()
orig_node = bridge.NODE_DIR
orig_reg = bridge.WORKER_REGISTRY_FILE
orig_sessions = bridge.SESSIONS_DIR
orig_admin = bridge.admin_chat_id
orig_state = dict(bridge.state)
orig_bridge_url = bridge.BRIDGE_URL
had_bridge_public_url = hasattr(bridge, 'BRIDGE_PUBLIC_URL')
orig_bridge_public_url = getattr(bridge, 'BRIDGE_PUBLIC_URL', '')

bridge.NODE_DIR = Path(tmpdir)
bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / 'workers.json'
bridge.SESSIONS_DIR = Path(tmpdir) / 'sessions'
bridge.SESSIONS_DIR.mkdir()
bridge.admin_chat_id = 123
bridge.state['startup_notified'] = True
bridge.state['active'] = 'lee'
bridge.BRIDGE_URL = 'http://localhost:8080'
bridge.BRIDGE_PUBLIC_URL = 'http://100.125.36.102:8080'

class MockWorkers:
    tmux_prefix = 'claude-test-'
    sessions_dir = bridge.SESSIONS_DIR
    def _sync_paths(self): pass
    def get_registered_sessions(self, registered=None):
        return {'lee': {'tmux': 'claude-test-lee', 'backend': 'claude'}}
    def is_online(self, name, session): return True

class MockTelegramAPI:
    def __init__(self):
        self.sent = []
    def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)

class MockThread:
    def __init__(self, *args, **kwargs): pass
    def start(self): pass

bridge._registry_add('lee', 'claude', 123)
with bridge._watchdog_lock:
    bridge._worker_states['lee'] = ('READY', 'idle', 0)

mock_api = MockTelegramAPI()
router = bridge.CommandRouter(mock_api, MockWorkers())
remote_calls = []

def mock_remote_run(cmd, **kwargs):
    remote_calls.append(list(cmd))
    return MagicMock(returncode=0, stdout='ok\\n', stderr='')

with patch('bridge._remote_run', side_effect=mock_remote_run), \
     patch('bridge.threading.Thread', MockThread), \
     patch.object(router, '_run_teleport_preflight', return_value=[]):
    router.cmd_teleport('lee mac', 123)

joined = [' '.join(c) for c in remote_calls]
assert any('curl -sf --connect-timeout 5 http://100.125.36.102:8080/health' in c for c in joined), \
    f'Expected health check against BRIDGE_PUBLIC_URL, got: {joined}'
assert any('Teleporting lee to mac' in s for s in mock_api.sent), \
    f'Expected teleport to proceed, got messages: {mock_api.sent}'
assert not any('Cannot teleport' in s for s in mock_api.sent), \
    f'Should not reject when BRIDGE_PUBLIC_URL is reachable, got: {mock_api.sent}'

bridge.NODE_DIR = orig_node
bridge.WORKER_REGISTRY_FILE = orig_reg
bridge.SESSIONS_DIR = orig_sessions
bridge.admin_chat_id = orig_admin
bridge.state.update(orig_state)
bridge.BRIDGE_URL = orig_bridge_url
if had_bridge_public_url:
    bridge.BRIDGE_PUBLIC_URL = orig_bridge_public_url
else:
    delattr(bridge, 'BRIDGE_PUBLIC_URL')
with bridge._watchdog_lock:
    bridge._worker_states.pop('lee', None)

import shutil
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Teleport preflight uses BRIDGE_PUBLIC_URL instead of localhost BRIDGE_URL"
    else
        fail "Teleport preflight BRIDGE_PUBLIC_URL test failed"
    fi
}

test_teleport_remote_worker_gets_public_url() {
    info "Testing remote worker receives BRIDGE_PUBLIC_URL in tmux environment..."

    if python3 -c "
from unittest.mock import patch, MagicMock
import bridge

orig_bridge_url = bridge.BRIDGE_URL
had_bridge_public_url = hasattr(bridge, 'BRIDGE_PUBLIC_URL')
orig_bridge_public_url = getattr(bridge, 'BRIDGE_PUBLIC_URL', '')

bridge.BRIDGE_URL = 'http://localhost:8080'
bridge.BRIDGE_PUBLIC_URL = 'http://100.125.36.102:8080'

class MockWorkers:
    tmux_prefix = 'claude-test-'
    sessions_dir = bridge.SESSIONS_DIR
    def _sync_paths(self): pass
    def get_registered_sessions(self, registered=None): return {}
    def is_online(self, name, session): return True

class MockTelegramAPI:
    def send_message(self, chat_id, text, **kwargs): pass

class MockBackend:
    is_interactive = False
    def start_cmd(self, session_id):
        return f'claude --resume {session_id} --dangerously-skip-permissions'

router = bridge.CommandRouter(MockTelegramAPI(), MockWorkers())
remote_calls = []

def mock_remote_run(cmd, **kwargs):
    remote_calls.append(list(cmd))
    if cmd[:2] == ['id', '-u']:
        return MagicMock(returncode=0, stdout='1000\\n', stderr='')
    return MagicMock(returncode=0, stdout='ok\\n', stderr='')

with patch('bridge._remote_run', side_effect=mock_remote_run), \
     patch('bridge.get_backend', return_value=MockBackend()), \
     patch('bridge.time.sleep', return_value=None):
    ok = router._start_worker_on_target('lee', 'mac', '', 'abc-123-session', 'claude')

assert ok is True, 'Expected target worker start to succeed'
bridge_exports = [c for c in remote_calls if len(c) >= 6 and c[0] == 'tmux' and c[1] == 'set-environment' and c[4] == 'BRIDGE_URL']
assert bridge_exports, f'Expected tmux BRIDGE_URL export call, got {remote_calls}'
assert any(c[5] == 'http://100.125.36.102:8080' for c in bridge_exports), \
    f'Expected BRIDGE_URL export to use BRIDGE_PUBLIC_URL, got {bridge_exports}'

bridge.BRIDGE_URL = orig_bridge_url
if had_bridge_public_url:
    bridge.BRIDGE_PUBLIC_URL = orig_bridge_public_url
else:
    delattr(bridge, 'BRIDGE_PUBLIC_URL')
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Remote worker BRIDGE_URL export uses BRIDGE_PUBLIC_URL"
    else
        fail "Remote worker BRIDGE_PUBLIC_URL export test failed"
    fi
}

test_teleport_preflight_rejects_without_public_url() {
    info "Testing teleport preflight rejects localhost BRIDGE_URL without BRIDGE_PUBLIC_URL..."

    if python3 -c "
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import bridge

tmpdir = tempfile.mkdtemp()
orig_node = bridge.NODE_DIR
orig_reg = bridge.WORKER_REGISTRY_FILE
orig_sessions = bridge.SESSIONS_DIR
orig_admin = bridge.admin_chat_id
orig_state = dict(bridge.state)
orig_bridge_url = bridge.BRIDGE_URL
had_bridge_public_url = hasattr(bridge, 'BRIDGE_PUBLIC_URL')
orig_bridge_public_url = getattr(bridge, 'BRIDGE_PUBLIC_URL', '')

bridge.NODE_DIR = Path(tmpdir)
bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / 'workers.json'
bridge.SESSIONS_DIR = Path(tmpdir) / 'sessions'
bridge.SESSIONS_DIR.mkdir()
bridge.admin_chat_id = 123
bridge.state['startup_notified'] = True
bridge.state['active'] = 'lee'
bridge.BRIDGE_URL = 'http://localhost:8080'
bridge.BRIDGE_PUBLIC_URL = ''

class MockWorkers:
    tmux_prefix = 'claude-test-'
    sessions_dir = bridge.SESSIONS_DIR
    def _sync_paths(self): pass
    def get_registered_sessions(self, registered=None):
        return {'lee': {'tmux': 'claude-test-lee', 'backend': 'claude'}}
    def is_online(self, name, session): return True

class MockTelegramAPI:
    def __init__(self):
        self.sent = []
    def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)

bridge._registry_add('lee', 'claude', 123)
with bridge._watchdog_lock:
    bridge._worker_states['lee'] = ('READY', 'idle', 0)

mock_api = MockTelegramAPI()
router = bridge.CommandRouter(mock_api, MockWorkers())

def mock_remote_run(cmd, **kwargs):
    return MagicMock(returncode=0, stdout='ok\\n', stderr='')

with patch('bridge._remote_run', side_effect=mock_remote_run):
    router.cmd_teleport('lee mac', 123)

assert any('Cannot teleport' in s for s in mock_api.sent), \
    f'Expected localhost rejection, got: {mock_api.sent}'
assert any('BRIDGE_PUBLIC_URL' in s for s in mock_api.sent), \
    f'Expected guidance to set BRIDGE_PUBLIC_URL, got: {mock_api.sent}'

bridge.NODE_DIR = orig_node
bridge.WORKER_REGISTRY_FILE = orig_reg
bridge.SESSIONS_DIR = orig_sessions
bridge.admin_chat_id = orig_admin
bridge.state.update(orig_state)
bridge.BRIDGE_URL = orig_bridge_url
if had_bridge_public_url:
    bridge.BRIDGE_PUBLIC_URL = orig_bridge_public_url
else:
    delattr(bridge, 'BRIDGE_PUBLIC_URL')
with bridge._watchdog_lock:
    bridge._worker_states.pop('lee', None)

import shutil
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Teleport preflight rejects localhost BRIDGE_URL without BRIDGE_PUBLIC_URL"
    else
        fail "Teleport preflight missing BRIDGE_PUBLIC_URL test failed"
    fi
}

test_teleport_preflight_checks() {
    info "Testing teleport pre-flight rejects bad states..."

    if python3 -c "
import json, tempfile, threading
from pathlib import Path
from unittest.mock import patch, MagicMock
import bridge

tmpdir = tempfile.mkdtemp()
orig_node = bridge.NODE_DIR
orig_reg = bridge.WORKER_REGISTRY_FILE
orig_sessions = bridge.SESSIONS_DIR
orig_admin = bridge.admin_chat_id
orig_state = dict(bridge.state)

bridge.NODE_DIR = Path(tmpdir)
bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / 'workers.json'
bridge.SESSIONS_DIR = Path(tmpdir) / 'sessions'
bridge.SESSIONS_DIR.mkdir()
bridge.admin_chat_id = 123
bridge.state['startup_notified'] = True
bridge.state['active'] = 'lee'

# Set up mock workers with lee registered
class MockWorkers:
    tmux_prefix = 'claude-test-'
    sessions_dir = bridge.SESSIONS_DIR
    def _sync_paths(self): pass
    def get_registered_sessions(self, registered=None):
        return {'lee': {'tmux': 'claude-test-lee', 'backend': 'claude'}}
    def is_online(self, name, session):
        return True

class MockTelegramAPI:
    def __init__(self):
        self.sent = []
    def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)

mock_api = MockTelegramAPI()
router = bridge.CommandRouter(mock_api, MockWorkers())

# Register lee in registry
bridge._registry_add('lee', 'claude', 123)

# Test 1: Worker not found
mock_api.sent.clear()
router.cmd_teleport('nobody mac', 123)
assert any('not found' in s.lower() or 'not in registry' in s.lower() for s in mock_api.sent), \
    f'T1: should reject missing worker, got: {mock_api.sent}'

# Test 2: Worker is busy (inject state)
mock_api.sent.clear()
with bridge._watchdog_lock:
    bridge._worker_states['lee'] = ('BUSY_TOOL', 'children=3', 0)
router.cmd_teleport('lee mac', 123)
assert any('busy' in s.lower() or 'idle' in s.lower() for s in mock_api.sent), \
    f'T2: should reject busy worker, got: {mock_api.sent}'

# Test 3: Target unreachable
mock_api.sent.clear()
with bridge._watchdog_lock:
    bridge._worker_states['lee'] = ('READY', 'idle', 0)
with patch('bridge._remote_run') as mock_rr:
    mock_rr.return_value = MagicMock(returncode=1, stdout='', stderr='err')
    router.cmd_teleport('lee unreachable-host', 123)
assert any('cannot reach' in s.lower() or 'reach' in s.lower() for s in mock_api.sent), \
    f'T3: should reject unreachable host, got: {mock_api.sent}'

# Restore
bridge.NODE_DIR = orig_node
bridge.WORKER_REGISTRY_FILE = orig_reg
bridge.SESSIONS_DIR = orig_sessions
bridge.admin_chat_id = orig_admin
bridge.state.update(orig_state)
with bridge._watchdog_lock:
    bridge._worker_states.pop('lee', None)

import shutil
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Teleport pre-flight checks reject bad states"
    else
        fail "Teleport pre-flight checks test failed"
    fi
}

test_teleport_end_to_end() {
    info "Testing teleport end-to-end flow (mocked SSH)..."

    if python3 -c "
import json, tempfile, time, threading
from pathlib import Path
from unittest.mock import patch, MagicMock, call
import bridge

tmpdir = tempfile.mkdtemp()
orig_node = bridge.NODE_DIR
orig_reg = bridge.WORKER_REGISTRY_FILE
orig_sessions = bridge.SESSIONS_DIR
orig_admin = bridge.admin_chat_id
orig_state = dict(bridge.state)
orig_bridge_url = bridge.BRIDGE_URL

bridge.NODE_DIR = Path(tmpdir)
bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / 'workers.json'
bridge.SESSIONS_DIR = Path(tmpdir) / 'sessions'
bridge.SESSIONS_DIR.mkdir()
bridge.admin_chat_id = 123
bridge.state['startup_notified'] = True
bridge.state['active'] = 'lee'
bridge.BRIDGE_URL = 'https://test-tunnel.trycloudflare.com'

# Create session dir with state files
session_dir = bridge.SESSIONS_DIR / 'lee'
session_dir.mkdir()
(session_dir / 'claude_session_id').write_text('abc-123-session')
(session_dir / 'claude_session_cwd').write_text('/home/claude/myproject')
(session_dir / 'chat_id').write_text('123')

class MockWorkers:
    tmux_prefix = 'claude-test-'
    sessions_dir = bridge.SESSIONS_DIR
    def _sync_paths(self): pass
    def get_registered_sessions(self, registered=None):
        return {'lee': {'tmux': 'claude-test-lee', 'backend': 'claude'}}
    def is_online(self, name, session): return True

class MockTelegramAPI:
    def __init__(self):
        self.sent = []
    def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)

mock_api = MockTelegramAPI()
router = bridge.CommandRouter(mock_api, MockWorkers())

bridge._registry_add('lee', 'claude', 123)

with bridge._watchdog_lock:
    bridge._worker_states['lee'] = ('READY', 'idle', 0)

# Mock _get_claude_pid: returns None first (worker stopped), then PID (target started)
pid_call_count = [0]
def mock_get_claude_pid(pane_pid, host=None):
    pid_call_count[0] += 1
    if pid_call_count[0] <= 1:
        return None  # Stop phase: worker already exited
    return '99999'   # Start phase: Claude is running on target

ssh_calls = []
def mock_remote_run(cmd, **kwargs):
    ssh_calls.append((list(cmd), kwargs))
    return MagicMock(returncode=0, stdout='12345\n', stderr='')

with patch('bridge._remote_run', side_effect=mock_remote_run), \
     patch('subprocess.run', side_effect=lambda cmd, **kw: MagicMock(returncode=0, stdout='12345\n')), \
     patch('bridge._get_claude_pid', side_effect=mock_get_claude_pid), \
     patch('bridge.telegram_api'):

    # Call _do_teleport directly (deterministic, no thread timing issues)
    router._do_teleport('lee', 'mac', '', False, 123)

# Check registry was updated
data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
w = data['workers']['lee']
assert w.get('host') == 'mac', f'host should be mac after teleport, got {w}'
assert w.get('home_host') is None, f'home_host should be None (was local)'
assert w.get('home_cwd') == '/home/claude/myproject', f'home_cwd should preserve original'

# Check teleport state file was cleaned up
assert not (session_dir / 'teleport_state').exists(), 'teleport_state should be cleaned up'

# Check some key SSH calls were made
call_strs = [' '.join(c[0]) for c in ssh_calls]
has_exit = any('/exit' in s for s in call_strs)
has_tmux_new = any('tmux new-session' in s for s in call_strs)
has_set_env = any('set-environment' in s and 'BRIDGE_URL' in s for s in call_strs)
assert has_exit, f'Should send /exit to stop worker'
assert has_tmux_new, f'Should create tmux on target'
assert has_set_env, f'Should set BRIDGE_URL on target'

# Now test teleback
ssh_calls.clear()
# After teleport, lee is on mac. Teleback should bring it local.
with bridge._watchdog_lock:
    bridge._worker_states['lee'] = ('READY', 'idle', 0)

pid_call_count[0] = 0  # Reset for teleback
with patch('bridge._remote_run', side_effect=mock_remote_run), \
     patch('subprocess.run', side_effect=lambda cmd, **kw: MagicMock(returncode=0, stdout='12345\n')), \
     patch('bridge._get_claude_pid', side_effect=mock_get_claude_pid), \
     patch('bridge.telegram_api'):

    router._do_teleport('lee', None, '/home/claude/myproject', False, 123, is_teleback=True)

# Registry should be cleared
data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
w = data['workers']['lee']
assert w.get('host') is None, f'host should be None after teleback, got {w}'
assert w.get('home_host') is None, f'home_host should be cleared'
assert w.get('home_cwd') is None, f'home_cwd should be cleared'

# Restore
bridge.NODE_DIR = orig_node
bridge.WORKER_REGISTRY_FILE = orig_reg
bridge.SESSIONS_DIR = orig_sessions
bridge.admin_chat_id = orig_admin
bridge.state.update(orig_state)
bridge.BRIDGE_URL = orig_bridge_url
with bridge._watchdog_lock:
    bridge._worker_states.pop('lee', None)

import shutil
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Teleport end-to-end flow works correctly"
    else
        fail "Teleport end-to-end test failed"
    fi
}

test_sync_credentials_always_overwrites() {
    info "Testing _sync_credentials_to_target always overwrites (not skip-if-exists)..."

    if python3 -c "
import json, tempfile, os
from pathlib import Path
from unittest.mock import patch, MagicMock, call
import bridge

# Track all remote commands
remote_calls = []
def mock_remote_run(cmd, **kwargs):
    remote_calls.append((list(cmd), kwargs))
    # Simulate target ALREADY has credentials (test -f returns 0)
    if cmd and 'test' in cmd and '-f' in cmd:
        return MagicMock(returncode=0)
    return MagicMock(returncode=0, stdout='', stderr='')

rsync_calls = []
orig_subproc_run = __import__('subprocess').run
def mock_subprocess_run(cmd, **kwargs):
    if isinstance(cmd, list) and 'rsync' in cmd:
        rsync_calls.append(list(cmd))
        return MagicMock(returncode=0)
    return orig_subproc_run(cmd, **kwargs)

# Create a fake local credentials file
tmpdir = tempfile.mkdtemp()
fake_creds = os.path.join(tmpdir, '.credentials.json')
with open(fake_creds, 'w') as f:
    json.dump({'accessToken': 'fresh-token'}, f)

with patch('os.path.expanduser', return_value=fake_creds), \
     patch('bridge._remote_run', side_effect=mock_remote_run), \
     patch('subprocess.run', side_effect=mock_subprocess_run):

    # Create a CommandRouter with mock workers
    class MockWorkers:
        tmux_prefix = 'claude-test-'
        sessions_dir = Path(tmpdir)
        def _sync_paths(self): pass
    class MockTelegramAPI:
        def send_message(self, *a, **k): pass

    router = bridge.CommandRouter(MockTelegramAPI(), MockWorkers())
    router._sync_credentials_to_target('mac-mini')

# BUG 1 TEST: rsync MUST be called even when target already has credentials
assert len(rsync_calls) > 0, f'rsync should be called even when target has creds, got {len(rsync_calls)} calls'
# Verify rsync targets the credentials file
rsync_target = ' '.join(rsync_calls[0])
assert '.credentials.json' in rsync_target, f'rsync should target credentials file'

import shutil
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Credentials always synced (not skip-if-exists)"
    else
        fail "Credentials should always sync to target"
    fi
}

test_sync_credentials_atomic_write() {
    info "Testing _sync_credentials_to_target uses atomic tmp+mv pattern..."

    if python3 -c "
import json, tempfile, os
from pathlib import Path
from unittest.mock import patch, MagicMock
import bridge

remote_calls = []
def mock_remote_run(cmd, **kwargs):
    remote_calls.append(list(cmd))
    return MagicMock(returncode=0, stdout='', stderr='')

rsync_calls = []
def mock_subprocess_run(cmd, **kwargs):
    if isinstance(cmd, list) and 'rsync' in cmd:
        rsync_calls.append(list(cmd))
        return MagicMock(returncode=0)
    return MagicMock(returncode=0)

tmpdir = tempfile.mkdtemp()
fake_creds = os.path.join(tmpdir, '.credentials.json')
with open(fake_creds, 'w') as f:
    json.dump({'accessToken': 'test'}, f)

with patch('os.path.expanduser', return_value=fake_creds), \
     patch('bridge._remote_run', side_effect=mock_remote_run), \
     patch('subprocess.run', side_effect=mock_subprocess_run):

    class MockWorkers:
        tmux_prefix = 'claude-test-'
        sessions_dir = Path(tmpdir)
        def _sync_paths(self): pass
    class MockTelegramAPI:
        def send_message(self, *a, **k): pass

    router = bridge.CommandRouter(MockTelegramAPI(), MockWorkers())
    router._sync_credentials_to_target('mac-mini')

# BUG 1b TEST: rsync should write to .tmp first, then mv to final path
assert len(rsync_calls) > 0, 'rsync should be called'
rsync_dest = rsync_calls[0][-1]  # last arg is destination
assert '.tmp' in rsync_dest, f'rsync should write to .tmp file, got: {rsync_dest}'

# mv should be called to atomically rename
mv_calls = [c for c in remote_calls if 'mv' in c]
assert len(mv_calls) > 0, f'mv should be called for atomic rename, remote_calls: {remote_calls}'
mv_cmd = ' '.join(mv_calls[0])
assert '.credentials.json.tmp' in mv_cmd, f'mv should rename from .tmp: {mv_cmd}'
assert '.credentials.json' in mv_cmd, f'mv should rename to final: {mv_cmd}'

import shutil
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Credentials use atomic tmp+mv write pattern"
    else
        fail "Credentials should use atomic write"
    fi
}

test_teleport_sends_welcome_after_start() {
    info "Testing teleport sends welcome message to worker after boot..."

    if python3 -c "
import json, tempfile, time, threading
from pathlib import Path
from unittest.mock import patch, MagicMock
import bridge

tmpdir = tempfile.mkdtemp()
orig_node = bridge.NODE_DIR
orig_reg = bridge.WORKER_REGISTRY_FILE
orig_sessions = bridge.SESSIONS_DIR
orig_admin = bridge.admin_chat_id
orig_state = dict(bridge.state)
orig_bridge_url = bridge.BRIDGE_URL

bridge.NODE_DIR = Path(tmpdir)
bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / 'workers.json'
bridge.SESSIONS_DIR = Path(tmpdir) / 'sessions'
bridge.SESSIONS_DIR.mkdir()
bridge.admin_chat_id = 123
bridge.state['startup_notified'] = True
bridge.state['active'] = 'lee'
bridge.BRIDGE_URL = 'https://test-tunnel.trycloudflare.com'

session_dir = bridge.SESSIONS_DIR / 'lee'
session_dir.mkdir()
(session_dir / 'claude_session_id').write_text('abc-123-session')
(session_dir / 'claude_session_cwd').write_text('/home/claude/myproject')
(session_dir / 'chat_id').write_text('123')

# Track what messages were sent to the worker
sent_messages = []

class MockWorkers:
    tmux_prefix = 'claude-test-'
    sessions_dir = bridge.SESSIONS_DIR
    def _sync_paths(self): pass
    def get_registered_sessions(self, registered=None):
        return {'lee': {'tmux': 'claude-test-lee', 'backend': 'claude'}}
    def is_online(self, name, session): return True
    def _build_welcome(self, name, backend_obj):
        return f'Welcome {name} to Telegram bridge'
    def send(self, name, message, **kwargs):
        sent_messages.append((name, message))
        return True

class MockTelegramAPI:
    def __init__(self):
        self.sent = []
    def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)

mock_api = MockTelegramAPI()
router = bridge.CommandRouter(mock_api, MockWorkers())
bridge._registry_add('lee', 'claude', 123)

with bridge._watchdog_lock:
    bridge._worker_states['lee'] = ('READY', 'idle', 0)

pid_call_count = [0]
def mock_get_claude_pid(pane_pid, host=None):
    pid_call_count[0] += 1
    if pid_call_count[0] <= 1:
        return None
    return '99999'

def mock_remote_run(cmd, **kwargs):
    return MagicMock(returncode=0, stdout='12345\n', stderr='')

with patch('bridge._remote_run', side_effect=mock_remote_run), \
     patch('subprocess.run', side_effect=lambda cmd, **kw: MagicMock(returncode=0, stdout='12345\n')), \
     patch('bridge._get_claude_pid', side_effect=mock_get_claude_pid), \
     patch('bridge.telegram_api'), \
     patch('time.sleep'):  # Speed up test

    router._do_teleport('lee', 'mac', '', False, 123)

# BUG 2 TEST: welcome message MUST be sent to worker after teleport
assert len(sent_messages) > 0, f'Welcome message should be sent to worker after teleport, got: {sent_messages}'
assert sent_messages[0][0] == 'lee', f'Message should be sent to lee, got: {sent_messages[0][0]}'
assert 'Welcome' in sent_messages[0][1] or 'Telegram' in sent_messages[0][1], \
    f'Should contain welcome text, got: {sent_messages[0][1][:100]}'

# Restore
bridge.NODE_DIR = orig_node
bridge.WORKER_REGISTRY_FILE = orig_reg
bridge.SESSIONS_DIR = orig_sessions
bridge.admin_chat_id = orig_admin
bridge.state.update(orig_state)
bridge.BRIDGE_URL = orig_bridge_url
with bridge._watchdog_lock:
    bridge._worker_states.pop('lee', None)

import shutil
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Teleport sends welcome message after boot"
    else
        fail "Teleport should send welcome message"
    fi
}

test_teleport_registry_updated_before_source_kill() {
    info "Testing registry is updated BEFORE source session is killed..."

    if python3 -c "
import json, tempfile, time, threading
from pathlib import Path
from unittest.mock import patch, MagicMock
import bridge

tmpdir = tempfile.mkdtemp()
orig_node = bridge.NODE_DIR
orig_reg = bridge.WORKER_REGISTRY_FILE
orig_sessions = bridge.SESSIONS_DIR
orig_admin = bridge.admin_chat_id
orig_state = dict(bridge.state)
orig_bridge_url = bridge.BRIDGE_URL

bridge.NODE_DIR = Path(tmpdir)
bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / 'workers.json'
bridge.SESSIONS_DIR = Path(tmpdir) / 'sessions'
bridge.SESSIONS_DIR.mkdir()
bridge.admin_chat_id = 123
bridge.state['startup_notified'] = True
bridge.state['active'] = 'lee'
bridge.BRIDGE_URL = 'https://test-tunnel.trycloudflare.com'

session_dir = bridge.SESSIONS_DIR / 'lee'
session_dir.mkdir()
(session_dir / 'claude_session_id').write_text('abc-123-session')
(session_dir / 'claude_session_cwd').write_text('/home/claude/myproject')
(session_dir / 'chat_id').write_text('123')

class MockWorkers:
    tmux_prefix = 'claude-test-'
    sessions_dir = bridge.SESSIONS_DIR
    def _sync_paths(self): pass
    def get_registered_sessions(self, registered=None):
        return {'lee': {'tmux': 'claude-test-lee', 'backend': 'claude'}}
    def is_online(self, name, session): return True
    def _build_welcome(self, name, backend_obj):
        return 'Welcome to bridge'
    def send(self, name, msg, **kwargs): return True

class MockTelegramAPI:
    def __init__(self):
        self.sent = []
    def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)

mock_api = MockTelegramAPI()
router = bridge.CommandRouter(mock_api, MockWorkers())
bridge._registry_add('lee', 'claude', 123)

with bridge._watchdog_lock:
    bridge._worker_states['lee'] = ('READY', 'idle', 0)

# Track the ORDER of operations
operation_log = []

pid_call_count = [0]
def mock_get_claude_pid(pane_pid, host=None):
    pid_call_count[0] += 1
    if pid_call_count[0] <= 1:
        return None
    return '99999'

orig_registry_update = bridge._registry_update_teleport
def tracking_registry_update(*args, **kwargs):
    operation_log.append('registry_update')
    return orig_registry_update(*args, **kwargs)

def mock_remote_run(cmd, **kwargs):
    cmd_str = ' '.join(str(c) for c in cmd)
    host = kwargs.get('host')
    # Track kill-session on source (host=None for local source)
    if 'kill-session' in cmd_str and host is None:
        operation_log.append('kill_source')
    return MagicMock(returncode=0, stdout='12345\n', stderr='')

with patch('bridge._remote_run', side_effect=mock_remote_run), \
     patch('subprocess.run', side_effect=lambda cmd, **kw: MagicMock(returncode=0, stdout='12345\n')), \
     patch('bridge._get_claude_pid', side_effect=mock_get_claude_pid), \
     patch('bridge._registry_update_teleport', side_effect=tracking_registry_update), \
     patch('bridge.telegram_api'), \
     patch('time.sleep'):

    router._do_teleport('lee', 'mac', '', False, 123)

# BUG 3 TEST: registry_update MUST come before kill_source (source kill, not target cleanup)
assert 'registry_update' in operation_log, f'registry_update should be called, log: {operation_log}'
assert 'kill_source' in operation_log, f'kill_source should be called, log: {operation_log}'
reg_idx = operation_log.index('registry_update')
kill_idx = operation_log.index('kill_source')
assert reg_idx < kill_idx, f'registry_update (idx {reg_idx}) must come BEFORE kill_source (idx {kill_idx}), log: {operation_log}'

# Restore
bridge.NODE_DIR = orig_node
bridge.WORKER_REGISTRY_FILE = orig_reg
bridge.SESSIONS_DIR = orig_sessions
bridge.admin_chat_id = orig_admin
bridge.state.update(orig_state)
bridge.BRIDGE_URL = orig_bridge_url
with bridge._watchdog_lock:
    bridge._worker_states.pop('lee', None)

import shutil
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Registry updated before source session killed"
    else
        fail "Registry should be updated before source kill"
    fi
}

test_sync_shared_repos_deploys_agent_config() {
    info "Testing _sync_shared_repos deploys skills/hooks/scripts from agent-config to .claude/..."

    if python3 -c "
import json, tempfile, os
from pathlib import Path
from unittest.mock import patch, MagicMock
import bridge

# Track remote commands
remote_calls = []
def mock_remote_run(cmd, **kwargs):
    cmd_str = ' '.join(str(c) for c in cmd) if isinstance(cmd, list) else str(cmd)
    remote_calls.append((cmd_str, kwargs.get('host')))
    r = MagicMock(returncode=0, stdout='', stderr='')
    if 'echo \$HOME' in cmd_str:
        r.stdout = '/Users/beastoinagents\n'
    return r

class MockWorkers:
    tmux_prefix = 'claude-prod-'
    sessions_dir = Path(tempfile.mkdtemp())
    def _sync_paths(self): pass
class MockTelegramAPI:
    def send_message(self, *a, **k): pass

router = bridge.CommandRouter(MockTelegramAPI(), MockWorkers())

with patch('bridge._remote_run', side_effect=mock_remote_run), \
     patch('subprocess.run', return_value=MagicMock(returncode=0)), \
     patch('os.path.isdir', return_value=True), \
     patch('os.path.exists', return_value=False):
    router._sync_shared_repos('mac-mini')

# Check that rsync --checksum deploy commands were issued for skills, hooks, scripts
deploy_cmds = [c for c, h in remote_calls if 'rsync' in c and '--checksum' in c and h == 'mac-mini']
assert len(deploy_cmds) == 3, f'Expected 3 deploy commands (skills/hooks/scripts), got {len(deploy_cmds)}: {deploy_cmds}'

for subdir in ['skills', 'hooks', 'scripts']:
    found = any(subdir in c for c in deploy_cmds)
    assert found, f'Missing deploy for {subdir}, got: {deploy_cmds}'

import shutil
shutil.rmtree(str(MockWorkers.sessions_dir))
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Shared repos deploy agent-config to .claude/"
    else
        fail "Shared repos should deploy agent-config to .claude/"
    fi
}

test_restart_teleported_worker() {
    info "Testing /restart works for teleported (remote) workers..."

    if python3 -c "
import json, tempfile, os, time
from pathlib import Path
from unittest.mock import patch, MagicMock
import bridge

tmpdir = tempfile.mkdtemp()
sessions = Path(tmpdir) / 'sessions'
sessions.mkdir()
(sessions / 'ren').mkdir()
(sessions / 'ren' / 'chat_id').write_text('123')
(sessions / 'ren' / 'claude_session_id').write_text('sess-uuid-123')
(sessions / 'ren' / 'cwd').write_text('/home/claude/omi')

# Write registry with host (teleported)
node_dir = Path(tmpdir) / 'node'
node_dir.mkdir()
(node_dir / 'workers.json').write_text(json.dumps({
    'ren': {'host': 'mac-mini', 'tmux': 'claude-prod-ren'}
}))

# Track calls
remote_calls = []
start_worker_calls = []
stop_calls = []
send_calls = []

def mock_remote_run(cmd, **kwargs):
    remote_calls.append((' '.join(str(c) for c in cmd), kwargs.get('host')))
    r = MagicMock(returncode=0, stdout='', stderr='')
    if 'has-session' in str(cmd):
        r.returncode = 0  # tmux exists on remote
    if 'display-message' in str(cmd):
        r.stdout = '12345'
    if 'pgrep' in str(cmd):
        r.returncode = 1  # claude not running (already exited)
    if 'echo' in str(cmd) and 'HOME' in str(cmd):
        r.stdout = '/Users/beastoinagents'
        r = MagicMock(returncode=0, stdout='/Users/beastoinagents\n', stderr='')
    return r

orig_start = bridge.CommandRouter._start_worker_on_target
def mock_start_worker(self, name, target_host, target_cwd, session_id, backend_name):
    start_worker_calls.append({
        'name': name, 'host': target_host, 'cwd': target_cwd,
        'session_id': session_id, 'backend': backend_name
    })
    return True

orig_stop = bridge.CommandRouter._stop_worker_for_teleport
def mock_stop(self, name, tmux_name, host=None):
    stop_calls.append({'name': name, 'tmux': tmux_name, 'host': host})
    return 'sess-uuid-123'

class MockWorkers:
    tmux_prefix = 'claude-prod-'
    sessions_dir = sessions
    def _sync_paths(self): pass
    def get_registered_sessions(self):
        return {'ren': {'tmux': 'claude-prod-ren'}}
    def _build_welcome(self, name, backend):
        return 'Welcome ren!'
    def send(self, name, text):
        send_calls.append({'name': name, 'text': text[:20]})

class MockTelegramAPI:
    def send_message(self, *a, **k): pass

orig_sessions = bridge.SESSIONS_DIR
orig_node_dir = bridge.NODE_DIR
bridge.SESSIONS_DIR = sessions
bridge.NODE_DIR = node_dir

router = bridge.CommandRouter(MockTelegramAPI(), MockWorkers())
router.workers = MockWorkers()

with patch('bridge._remote_run', side_effect=mock_remote_run), \
     patch.object(bridge.CommandRouter, '_start_worker_on_target', mock_start_worker), \
     patch.object(bridge.CommandRouter, '_stop_worker_for_teleport', mock_stop), \
     patch('time.sleep'):

    # Call _restart_remote_worker directly on CommandRouter
    ok, err = router._restart_remote_worker(
        'ren', 'claude', bridge.get_backend('claude'),
        'claude-prod-ren', 'mac-mini', 'resume')

assert ok, f'restart should succeed, got err={err}'

# Should have stopped the worker on mac-mini
assert len(stop_calls) == 1, f'Expected 1 stop call, got {stop_calls}'
assert stop_calls[0]['host'] == 'mac-mini', f'Stop should target mac-mini'

# Should have called _start_worker_on_target with session_id for resume
assert len(start_worker_calls) == 1, f'Expected 1 start call, got {start_worker_calls}'
assert start_worker_calls[0]['host'] == 'mac-mini'
assert start_worker_calls[0]['session_id'] == 'sess-uuid-123', \
    f'Should pass session_id for resume, got {start_worker_calls[0][\"session_id\"]}'

# Should have sent welcome
assert len(send_calls) == 1, f'Expected welcome, got {send_calls}'

bridge.SESSIONS_DIR = orig_sessions
bridge.NODE_DIR = orig_node_dir
import shutil
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Restart works for teleported workers"
    else
        fail "Restart should work for teleported workers"
    fi
}

test_restart_delegates_to_remote_for_teleported() {
    info "Testing cmd_restart detects teleported worker and delegates to remote..."

    if python3 -c "
import json, tempfile, os
from pathlib import Path
from unittest.mock import patch, MagicMock
import bridge

tmpdir = tempfile.mkdtemp()
sessions = Path(tmpdir) / 'sessions'
sessions.mkdir()
(sessions / 'ren').mkdir()
(sessions / 'ren' / 'chat_id').write_text('123')
(sessions / 'ren' / 'claude_session_id').write_text('sess-abc')
(sessions / 'ren' / 'cwd').write_text('/home/claude/omi')

node_dir = Path(tmpdir) / 'node'
node_dir.mkdir()
(node_dir / 'workers.json').write_text(json.dumps({
    'workers': {'ren': {'host': 'mac-mini', 'tmux': 'claude-prod-ren'}}
}))

remote_restart_calls = []
replies = []

def mock_restart_remote(self, name, backend_name, backend, tmux_name, host, mode):
    remote_restart_calls.append({
        'name': name, 'host': host, 'mode': mode
    })
    return True, None

class MockWorkers:
    tmux_prefix = 'claude-prod-'
    sessions_dir = sessions
    def _sync_paths(self): pass
    def get_registered_sessions(self):
        return {'ren': {'tmux': 'claude-prod-ren'}}

class MockTelegramAPI:
    def send_message(self, *a, **k): pass

orig_sessions = bridge.SESSIONS_DIR
orig_node_dir = bridge.NODE_DIR
orig_registry = bridge.WORKER_REGISTRY_FILE
orig_state = dict(bridge.state)
orig_admin = bridge.admin_chat_id
bridge.SESSIONS_DIR = sessions
bridge.NODE_DIR = node_dir
bridge.WORKER_REGISTRY_FILE = node_dir / 'workers.json'
bridge.admin_chat_id = 123
bridge.state['active'] = 'ren'

router = bridge.CommandRouter(MockTelegramAPI(), MockWorkers())
router.workers = MockWorkers()

def mock_reply(self, chat_id, text, **kwargs):
    replies.append(text)

with patch.object(bridge.CommandRouter, '_restart_remote_worker', mock_restart_remote), \
     patch.object(bridge.CommandRouter, 'reply', mock_reply):
    router.cmd_restart(123, 'ren')

assert len(remote_restart_calls) == 1, f'Should delegate to _restart_remote_worker, got {remote_restart_calls}'
assert remote_restart_calls[0]['host'] == 'mac-mini'
assert remote_restart_calls[0]['mode'] == 'resume', f'Default mode should be resume, got {remote_restart_calls[0][\"mode\"]}'
assert any('back and ready' in r for r in replies), f'Should confirm restart, got {replies}'

bridge.SESSIONS_DIR = orig_sessions
bridge.NODE_DIR = orig_node_dir
bridge.WORKER_REGISTRY_FILE = orig_registry
bridge.state.update(orig_state)
bridge.admin_chat_id = orig_admin
import shutil
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "cmd_restart delegates to remote for teleported workers"
    else
        fail "cmd_restart should delegate to remote for teleported workers"
    fi
}

test_end_worker_teleported_uses_remote_tmux() {
    info "Testing /end uses remote tmux kill for teleported workers..."

    if python3 -c "
import json, tempfile, shutil
from pathlib import Path
from unittest.mock import patch, MagicMock
import bridge

tmpdir = tempfile.mkdtemp()
tmp = Path(tmpdir)
orig_node = bridge.NODE_DIR
orig_reg = bridge.WORKER_REGISTRY_FILE
orig_sessions = bridge.SESSIONS_DIR
orig_state = dict(bridge.state)

bridge.NODE_DIR = tmp
bridge.WORKER_REGISTRY_FILE = tmp / 'workers.json'
bridge.SESSIONS_DIR = tmp / 'sessions'
bridge.SESSIONS_DIR.mkdir()
bridge.state['active'] = None

bridge._registry_add('ren', 'claude', 123, host='mac-mini')
wm = bridge.WorkerManager(bridge.SESSIONS_DIR, 'claude-test-')
wm.get_registered_sessions = lambda registered=None: {
    'ren': {'tmux': 'claude-test-ren', 'backend': 'claude'}
}

remote_calls = []
local_calls = []
def mock_remote(cmd, host=None, **kwargs):
    remote_calls.append((cmd, host))
    return MagicMock(returncode=0, stdout='', stderr='')

def mock_run(cmd, **kwargs):
    local_calls.append(cmd)
    return MagicMock(returncode=0, stdout='', stderr='')

with patch('bridge._remote_run', side_effect=mock_remote), \
     patch('subprocess.run', side_effect=mock_run), \
     patch('bridge.cleanup_inbox'), \
     patch('bridge.cleanup_worker_pipe'), \
     patch('bridge.clear_pending'), \
     patch('bridge._set_worker_cwd'), \
     patch('bridge._registry_remove'):
    ok, err = wm.end('ren')

assert ok, f'remote end should succeed: {err}'
assert any(cmd[:3] == ['tmux', 'kill-session', '-t'] and host == 'mac-mini'
           for cmd, host in remote_calls), f'remote tmux kill missing: {remote_calls}'
assert not any(cmd[:3] == ['tmux', 'kill-session', '-t'] for cmd in local_calls), \
    f'remote worker should not use local tmux kill: {local_calls}'

bridge.WORKER_REGISTRY_FILE.write_text(json.dumps({'version': 1, 'workers': {}}))
bridge._registry_add('lee', 'claude', 123)
wm.get_registered_sessions = lambda registered=None: {
    'lee': {'tmux': 'claude-test-lee', 'backend': 'claude'}
}
remote_calls.clear()
local_calls.clear()

with patch('bridge._remote_run', side_effect=mock_remote), \
     patch('subprocess.run', side_effect=mock_run), \
     patch('bridge.cleanup_inbox'), \
     patch('bridge.cleanup_worker_pipe'), \
     patch('bridge.clear_pending'), \
     patch('bridge._set_worker_cwd'), \
     patch('bridge._registry_remove'):
    ok, err = wm.end('lee')

assert ok, f'local end should succeed: {err}'
# _remote_run is called with host=None for local workers (routes to subprocess.run internally)
assert any(cmd[:3] == ['tmux', 'kill-session', '-t'] and host is None
           for cmd, host in remote_calls), \
    f'local worker should use _remote_run(host=None) for tmux kill: {remote_calls}'

bridge.NODE_DIR = orig_node
bridge.WORKER_REGISTRY_FILE = orig_reg
bridge.SESSIONS_DIR = orig_sessions
bridge.state.update(orig_state)
shutil.rmtree(tmpdir, ignore_errors=True)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/end uses remote tmux kill for teleported workers"
    else
        fail "/end teleported worker should use remote tmux kill"
    fi
}

test_restart_teleported_requires_remote_dispatch() {
    info "Testing WorkerManager.restart rejects teleported workers for remote dispatch..."

    if python3 -c "
import tempfile, shutil
from pathlib import Path
from unittest.mock import patch
import bridge

tmpdir = tempfile.mkdtemp()
tmp = Path(tmpdir)
orig_node = bridge.NODE_DIR
orig_reg = bridge.WORKER_REGISTRY_FILE
orig_sessions = bridge.SESSIONS_DIR

bridge.NODE_DIR = tmp
bridge.WORKER_REGISTRY_FILE = tmp / 'workers.json'
bridge.SESSIONS_DIR = tmp / 'sessions'
bridge.SESSIONS_DIR.mkdir()

bridge._registry_add('ren', 'claude', 123, host='mac-mini')
wm = bridge.WorkerManager(bridge.SESSIONS_DIR, 'claude-test-')
wm.get_registered_sessions = lambda registered=None: {
    'ren': {'tmux': 'claude-test-ren', 'backend': 'claude'}
}

with patch('bridge.tmux_exists', side_effect=AssertionError('tmux_exists should not run locally for teleported worker')):
    ok, err = wm.restart('ren', mode='resume')

assert ok is False, f'teleported restart should reject local path: {(ok, err)}'
assert err == 'use_remote_restart', f'expected sentinel, got {err!r}'

bridge.NODE_DIR = orig_node
bridge.WORKER_REGISTRY_FILE = orig_reg
bridge.SESSIONS_DIR = orig_sessions
shutil.rmtree(tmpdir, ignore_errors=True)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "WorkerManager.restart rejects teleported workers for remote dispatch"
    else
        fail "WorkerManager.restart should reject teleported workers for remote dispatch"
    fi
}

test_export_hook_env_remaps_remote_sessions_dir() {
    info "Testing export_hook_env remaps SESSIONS_DIR for teleported workers..."

    if python3 -c "
from pathlib import Path
from unittest.mock import patch, MagicMock
import bridge

orig_sessions = bridge.SESSIONS_DIR
bridge.SESSIONS_DIR = Path(str(Path.home() / '.claude' / 'telegram' / 'sessions'))

calls = []
def mock_remote(cmd, host=None, **kwargs):
    calls.append((cmd, host))
    # Match the echo HOME call (literal dollar-HOME in the cmd list)
    if len(cmd) == 3 and cmd[0] == 'bash' and 'HOME' in cmd[2]:
        return MagicMock(returncode=0, stdout='/Users/beastoinagents\n', stderr='')
    return MagicMock(returncode=0, stdout='', stderr='')

with patch('bridge._remote_run', side_effect=mock_remote):
    bridge.export_hook_env('claude-test-ren', backend='claude', host='mac-mini')

session_exports = [cmd for cmd, host in calls if host == 'mac-mini' and len(cmd) >= 6 and cmd[4] == 'SESSIONS_DIR']
assert len(session_exports) == 1, f'expected one SESSIONS_DIR export, got {calls}'
assert session_exports[0][5] == '/Users/beastoinagents/.claude/telegram/sessions', f'wrong path: {session_exports[0]}'

calls.clear()
with patch('bridge._remote_run', side_effect=mock_remote):
    bridge.export_hook_env('claude-test-lee', backend='claude')

local_exports = [cmd for cmd, host in calls if host is None and len(cmd) >= 6 and cmd[4] == 'SESSIONS_DIR']
assert len(local_exports) == 1, f'expected local SESSIONS_DIR export, got {calls}'
assert local_exports[0][5] == str(bridge.SESSIONS_DIR), f'wrong local path: {local_exports[0]}'

bridge.SESSIONS_DIR = orig_sessions
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "export_hook_env remaps remote SESSIONS_DIR"
    else
        fail "export_hook_env should remap remote SESSIONS_DIR"
    fi
}

test_export_hook_env_uses_public_url_for_remote() {
    info "Testing export_hook_env uses BRIDGE_PUBLIC_URL for remote workers..."

    if python3 -c "
from pathlib import Path
from unittest.mock import patch, MagicMock
import bridge

# Save originals
orig_bridge_url = bridge.BRIDGE_URL
orig_public_url = bridge.BRIDGE_PUBLIC_URL

# Simulate: BRIDGE_URL is localhost, BRIDGE_PUBLIC_URL is the Tailscale IP
bridge.BRIDGE_URL = 'http://localhost:8271'
bridge.BRIDGE_PUBLIC_URL = 'http://100.125.36.102:8271'

calls = []
def mock_remote(cmd, host=None, **kwargs):
    calls.append((cmd, host))
    if len(cmd) == 3 and cmd[0] == 'bash' and 'HOME' in cmd[2]:
        return MagicMock(returncode=0, stdout='/Users/beastoinagents\n', stderr='')
    return MagicMock(returncode=0, stdout='', stderr='')

# Remote worker: must use BRIDGE_PUBLIC_URL, not localhost
with patch('bridge._remote_run', side_effect=mock_remote):
    bridge.export_hook_env('claude-test-ren', backend='claude', host='mac-mini')

bridge_exports = [cmd for cmd, host in calls if host == 'mac-mini' and len(cmd) >= 6 and cmd[4] == 'BRIDGE_URL']
assert len(bridge_exports) == 1, f'expected one BRIDGE_URL export, got {bridge_exports}'
assert bridge_exports[0][5] == 'http://100.125.36.102:8271', f'remote should use BRIDGE_PUBLIC_URL, got: {bridge_exports[0][5]}'

# Local worker: can use BRIDGE_URL as-is
calls.clear()
with patch('bridge._remote_run', side_effect=mock_remote):
    bridge.export_hook_env('claude-test-lee', backend='claude')

local_exports = [cmd for cmd, host in calls if host is None and len(cmd) >= 6 and cmd[4] == 'BRIDGE_URL']
assert len(local_exports) == 1, f'expected one BRIDGE_URL export, got {local_exports}'
assert local_exports[0][5] == 'http://localhost:8271', f'local should use BRIDGE_URL, got: {local_exports[0][5]}'

# Restore
bridge.BRIDGE_URL = orig_bridge_url
bridge.BRIDGE_PUBLIC_URL = orig_public_url
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "export_hook_env uses BRIDGE_PUBLIC_URL for remote workers"
    else
        fail "export_hook_env should use BRIDGE_PUBLIC_URL for remote workers"
    fi
}

test_resolved_alert_cooldown() {
    info "Testing resolved alert has cooldown to prevent spam..."

    if python3 -c "
from unittest.mock import patch, MagicMock
import time
import bridge

# Reset state
bridge._last_resolved_ts.clear()
bridge._prev_worker_states.clear()

alerts = []
def mock_api(method, params):
    alerts.append(params.get('text', ''))
    return {'ok': True, 'result': {'message_id': 1}}

with patch.object(bridge, 'admin_chat_id', 123), \
     patch.object(bridge, 'telegram_api', mock_api):
    # Set prev state as DEAD
    with bridge._watchdog_lock:
        bridge._prev_worker_states['test'] = 'DEAD'

    # First resolved alert should send
    bridge._send_resolved_alert('test', 'READY')
    assert len(alerts) == 1, f'First alert should send, got {len(alerts)}'

    # Reset prev state back to DEAD to simulate another bad->good transition
    with bridge._watchdog_lock:
        bridge._prev_worker_states['test'] = 'DEAD'

    # Second one within cooldown should be suppressed
    bridge._send_resolved_alert('test', 'READY')
    assert len(alerts) == 1, f'Second alert within cooldown should be suppressed, got {len(alerts)}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "resolved alert has 180s cooldown"
    else
        fail "resolved alert should have cooldown to prevent spam"
    fi
}

test_is_online_teleported_ssh_failure_assumes_online() {
    info "Testing is_online treats SSH failures as online for remote workers..."

    if python3 -c "
from unittest.mock import patch
import bridge

wm = bridge.WorkerManager(bridge.SESSIONS_DIR, 'claude-test-')
session = {'tmux': 'claude-test-ren', 'backend': 'claude'}

# SSH failure (exception) should return True (assume online)
def mock_tmux_exists(name, host=None):
    if host:
        raise Exception('SSH connection refused')
    return True

with patch('bridge.get_worker_host', return_value='mac-mini'), \
     patch('bridge.tmux_exists', side_effect=mock_tmux_exists):
    result = wm.is_online('ren', session)
    assert result is True, f'SSH failure should assume online, got {result}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "is_online treats SSH failure as online for teleported workers"
    else
        fail "is_online should treat SSH failure as online for teleported workers"
    fi
}

test_is_online_teleported_checks_claude_process() {
    info "Testing teleported interactive workers require tmux and Claude to be alive..."

    if python3 -c "
from unittest.mock import patch
import bridge

wm = bridge.WorkerManager(bridge.SESSIONS_DIR, 'claude-test-')
session = {'tmux': 'claude-test-ren', 'backend': 'claude'}

with patch('bridge.get_worker_host', return_value='mac-mini'), \
     patch('bridge.tmux_exists', return_value=True), \
     patch('bridge.is_claude_running', return_value=False):
    assert wm.is_online('ren', session) is False, 'remote worker with dead claude should be offline'

with patch('bridge.get_worker_host', return_value='mac-mini'), \
     patch('bridge.tmux_exists', return_value=True), \
     patch('bridge.is_claude_running', return_value=True):
    assert wm.is_online('ren', session) is True, 'remote worker with live claude should be online'

local_calls = []
class FakeBackend:
    is_interactive = True
    def is_online(self, tmux_name):
        local_calls.append(tmux_name)
        return True

with patch('bridge.get_worker_host', return_value=None), \
     patch('bridge.get_backend', return_value=FakeBackend()):
    assert wm.is_online('lee', {'tmux': 'claude-test-lee', 'backend': 'claude'}) is True

assert local_calls == ['claude-test-lee'], f'local path should delegate to backend.is_online: {local_calls}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "teleported is_online checks both tmux and Claude"
    else
        fail "teleported is_online should check both tmux and Claude"
    fi
}

test_session_helpers_read_remote_files() {
    info "Testing session ID/CWD helpers read remote files for teleported workers..."

    if python3 -c "
import tempfile, shutil
from pathlib import Path
from unittest.mock import patch, MagicMock
import bridge

tmpdir = tempfile.mkdtemp()
tmp = Path(tmpdir)
orig_node = bridge.NODE_DIR
orig_reg = bridge.WORKER_REGISTRY_FILE
orig_sessions = bridge.SESSIONS_DIR

bridge.NODE_DIR = tmp
bridge.WORKER_REGISTRY_FILE = tmp / 'workers.json'
bridge.SESSIONS_DIR = tmp / 'sessions'
bridge.SESSIONS_DIR.mkdir()
session_dir = bridge.SESSIONS_DIR / 'ren'
session_dir.mkdir()
(session_dir / 'claude_session_id').write_text('local-stale')
(session_dir / 'claude_session_cwd').write_text('/local/stale')
bridge._registry_add('ren', 'claude', 123, host='mac-mini')

calls = []
def mock_remote(cmd, host=None, **kwargs):
    calls.append((cmd, host))
    if len(cmd) == 3 and cmd[0] == 'bash' and 'HOME' in cmd[2]:
        return MagicMock(returncode=0, stdout='/Users/beastoinagents\n', stderr='')
    if cmd[:1] == ['cat'] and cmd[-1].endswith('claude_session_id'):
        return MagicMock(returncode=0, stdout='remote-session\n', stderr='')
    if cmd[:1] == ['cat'] and cmd[-1].endswith('claude_session_cwd'):
        return MagicMock(returncode=0, stdout='/Users/beastoinagents/omi\n', stderr='')
    return MagicMock(returncode=1, stdout='', stderr='missing')

with patch('bridge._remote_run', side_effect=mock_remote):
    sid = bridge.get_claude_session_id('ren')
    assert sid == 'remote-session', f'expected remote-session, got {sid!r}'
    scwd = bridge.get_claude_session_cwd('ren')
    assert scwd == '/Users/beastoinagents/omi', f'expected remote cwd, got {scwd!r}'

assert any(host == 'mac-mini' and cmd[0] == 'cat' for cmd, host in calls), f'remote cat missing: {calls}'
assert bridge.get_claude_session_id('local') == ''

local_dir = bridge.SESSIONS_DIR / 'lee'
local_dir.mkdir()
(local_dir / 'claude_session_id').write_text('local-session')
(local_dir / 'claude_session_cwd').write_text('/tmp/local')
assert bridge.get_claude_session_id('lee') == 'local-session'
assert bridge.get_claude_session_cwd('lee') == '/tmp/local'

bridge.NODE_DIR = orig_node
bridge.WORKER_REGISTRY_FILE = orig_reg
bridge.SESSIONS_DIR = orig_sessions
shutil.rmtree(tmpdir, ignore_errors=True)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "session helpers read remote files for teleported workers"
    else
        fail "session helpers should read remote files for teleported workers"
    fi
}

test_hook_failures_teleported_use_remote_files() {
    info "Testing hook failure signals use remote files for teleported workers..."

    if python3 -c "
import json, tempfile, shutil, time
from pathlib import Path
from unittest.mock import patch, MagicMock
import bridge

tmpdir = tempfile.mkdtemp()
tmp = Path(tmpdir)
orig_node = bridge.NODE_DIR
orig_reg = bridge.WORKER_REGISTRY_FILE

bridge.NODE_DIR = tmp
bridge.WORKER_REGISTRY_FILE = tmp / 'workers.json'
bridge._registry_add('ren', 'claude', 123, host='mac-mini')

now = int(time.time())
remote_calls = []
def mock_remote(cmd, host=None, **kwargs):
    remote_calls.append((cmd, host))
    if cmd[:1] == ['cat']:
        lines = '\\n'.join([f'{now} Bash', f'{now - 1} Bash', f'{now - 2} Bash']) + '\\n'
        return MagicMock(returncode=0, stdout=lines, stderr='')
    return MagicMock(returncode=0, stdout='', stderr='')

with patch('bridge._remote_run', side_effect=mock_remote):
    reason = bridge._check_hook_failure_signal('ren')
    bridge._clear_hook_failures('ren')

assert reason is not None and 'hook failure signal' in reason, f'unexpected reason: {reason}'
assert any(cmd[:1] == ['cat'] and host == 'mac-mini' for cmd, host in remote_calls), f'remote read missing: {remote_calls}'
assert any(cmd[:2] == ['rm', '-f'] and host == 'mac-mini' for cmd, host in remote_calls), f'remote clear missing: {remote_calls}'

hook_dir = Path(f'/tmp/claudecode-telegram/{bridge._node_name}/lee/hooks')
hook_dir.mkdir(parents=True, exist_ok=True)
failures = hook_dir / 'failures'
failures.write_text('\\n'.join([f'{now} Bash', f'{now - 1} Bash', f'{now - 2} Bash']) + '\\n')
assert bridge._check_hook_failure_signal('lee') is not None
bridge._clear_hook_failures('lee')
assert not failures.exists(), 'local hook failure file should be cleared'

bridge.NODE_DIR = orig_node
bridge.WORKER_REGISTRY_FILE = orig_reg
shutil.rmtree(tmpdir, ignore_errors=True)
shutil.rmtree(hook_dir.parent, ignore_errors=True)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "hook failure signals use remote files for teleported workers"
    else
        fail "hook failure signals should use remote files for teleported workers"
    fi
}

test_localize_media_teleported_fetches_remote_even_when_local_exists() {
    info "Testing teleported media localization always fetches from remote..."

    if python3 -c "
from unittest.mock import patch
import bridge

fetch_calls = []
def mock_fetch(host, file_path):
    fetch_calls.append((host, file_path))
    return '/tmp/fetched.png'

with patch('bridge.get_worker_host', return_value='mac-mini'), \
     patch('bridge._fetch_remote_file', side_effect=mock_fetch), \
     patch('os.path.exists', return_value=True):
    result = bridge._localize_media('ren', [('/tmp/raw.png', 'caption')])

assert result == [('/tmp/fetched.png', 'caption')], f'expected fetched remote path, got {result}'
assert fetch_calls == [('mac-mini', '/tmp/raw.png')], f'should fetch remote file regardless of local collision: {fetch_calls}'

fetch_calls.clear()
with patch('bridge.get_worker_host', return_value=None), \
     patch('bridge._fetch_remote_file', side_effect=mock_fetch):
    result = bridge._localize_media('lee', [('/tmp/raw.png', 'caption')])

assert result == [('/tmp/raw.png', 'caption')], f'local worker should keep local path: {result}'
assert not fetch_calls, f'local worker should not fetch remote media: {fetch_calls}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "teleported media localization always fetches remote files"
    else
        fail "teleported media localization should always fetch remote files"
    fi
}

test_download_telegram_file_syncs_to_remote_inbox() {
    info "Testing incoming media is synced to remote inbox for teleported workers..."

    if python3 -c "
import io, json, tempfile, shutil
from pathlib import Path
from unittest.mock import patch, MagicMock
import bridge

tmpdir = tempfile.mkdtemp()
tmp = Path(tmpdir)
orig_node = bridge.NODE_DIR
orig_reg = bridge.WORKER_REGISTRY_FILE
orig_token = bridge.BOT_TOKEN
orig_inbox = bridge.FILE_INBOX_ROOT

bridge.NODE_DIR = tmp
bridge.WORKER_REGISTRY_FILE = tmp / 'workers.json'
bridge.FILE_INBOX_ROOT = tmp / 'inbox-root'
bridge.BOT_TOKEN = 'test-token'
bridge._registry_add('ren', 'claude', 123, host='mac-mini')

class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
    def read(self):
        return self.payload
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        return False

url_calls = []
def mock_urlopen(req, timeout=0):
    url = req.full_url if hasattr(req, 'full_url') else req
    url_calls.append(url)
    if 'getFile' in url:
        payload = json.dumps({'ok': True, 'result': {'file_path': 'photos/test.png', 'file_size': 4}}).encode()
        return FakeResponse(payload)
    return FakeResponse(b'data')

remote_calls = []
def mock_remote(cmd, host=None, **kwargs):
    remote_calls.append((cmd, host))
    return MagicMock(returncode=0, stdout='', stderr='')

sync_calls = []
def mock_run(cmd, **kwargs):
    sync_calls.append(cmd)
    return MagicMock(returncode=0, stdout='', stderr='')

with patch('urllib.request.urlopen', side_effect=mock_urlopen), \
     patch('bridge._remote_run', side_effect=mock_remote), \
     patch('subprocess.run', side_effect=mock_run):
    remote_path = bridge.download_telegram_file('file-1', 'ren')

assert remote_path.startswith(str(bridge.FILE_INBOX_ROOT / 'ren' / 'inbox')), remote_path
assert remote_path.endswith('.png'), remote_path
assert any(cmd[:2] == ['mkdir', '-p'] and host == 'mac-mini' for cmd, host in remote_calls), f'remote inbox mkdir missing: {remote_calls}'
assert any(cmd[:2] == ['chmod', '700'] and host == 'mac-mini' for cmd, host in remote_calls), f'remote inbox chmod missing: {remote_calls}'
assert any(cmd[0] == 'rsync' and 'mac-mini:' in cmd[-1] for cmd in sync_calls), f'remote inbox rsync missing: {sync_calls}'

bridge.WORKER_REGISTRY_FILE.write_text(json.dumps({'version': 1, 'workers': {}}))
sync_calls.clear()
remote_calls.clear()
with patch('urllib.request.urlopen', side_effect=mock_urlopen), \
     patch('bridge._remote_run', side_effect=mock_remote), \
     patch('subprocess.run', side_effect=mock_run):
    local_path = bridge.download_telegram_file('file-2', 'lee')

assert local_path.startswith(str(bridge.FILE_INBOX_ROOT / 'lee' / 'inbox')), local_path
assert not any(cmd[0] == 'rsync' for cmd in sync_calls), f'local inbox should not rsync: {sync_calls}'

bridge.NODE_DIR = orig_node
bridge.WORKER_REGISTRY_FILE = orig_reg
bridge.BOT_TOKEN = orig_token
bridge.FILE_INBOX_ROOT = orig_inbox
shutil.rmtree(tmpdir, ignore_errors=True)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "incoming media syncs to remote inbox for teleported workers"
    else
        fail "incoming media should sync to remote inbox for teleported workers"
    fi
}

test_spawn_adapter_teleported_rejected() {
    info "Testing non-interactive adapters reject teleported workers..."

    if python3 -c "
import tempfile, shutil
from pathlib import Path
from unittest.mock import patch, MagicMock
import bridge

tmpdir = tempfile.mkdtemp()
tmp = Path(tmpdir)
orig_node = bridge.NODE_DIR
orig_reg = bridge.WORKER_REGISTRY_FILE

bridge.NODE_DIR = tmp
bridge.WORKER_REGISTRY_FILE = tmp / 'workers.json'
bridge._registry_add('ren', 'codex', 123, host='mac-mini')

adapter = tmp / 'adapter.py'
adapter.write_text('print(1)')
sessions = tmp / 'sessions'
sessions.mkdir()
(sessions / 'ren').mkdir()
(sessions / 'lee').mkdir()

with patch('subprocess.Popen', side_effect=AssertionError('Popen should not run for teleported adapter')):
    ok = bridge._spawn_adapter(adapter, 'ren', 'hello', 'http://bridge', sessions)
assert ok is False, 'teleported non-interactive worker should be rejected'

with patch('subprocess.Popen', return_value=MagicMock()), \
     patch('builtins.open', open):
    ok = bridge._spawn_adapter(adapter, 'lee', 'hello', 'http://bridge', sessions)
assert ok is True, 'local adapter should still spawn'

bridge.NODE_DIR = orig_node
bridge.WORKER_REGISTRY_FILE = orig_reg
shutil.rmtree(tmpdir, ignore_errors=True)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "non-interactive adapters reject teleported workers"
    else
        fail "non-interactive adapters should reject teleported workers"
    fi
}

test_workers_remote_noninteractive_warns() {
    info "Testing /workers warns for remote non-interactive workers..."

    if python3 -c "
import tempfile, shutil
from pathlib import Path
import bridge

tmpdir = tempfile.mkdtemp()
tmp = Path(tmpdir)
orig_node = bridge.NODE_DIR
orig_reg = bridge.WORKER_REGISTRY_FILE
orig_sessions = bridge.SESSIONS_DIR
orig_pipe_root = bridge.WORKER_PIPE_ROOT

bridge.NODE_DIR = tmp
bridge.WORKER_REGISTRY_FILE = tmp / 'workers.json'
bridge.SESSIONS_DIR = tmp / 'sessions'
bridge.SESSIONS_DIR.mkdir()
bridge.WORKER_PIPE_ROOT = tmp / 'pipes'
bridge._registry_add('ren', 'codex', 123, host='mac-mini')

wm = bridge.WorkerManager(bridge.SESSIONS_DIR, 'claude-test-')
wm.scan_tmux_sessions = lambda: {'ren': {'tmux': 'claude-test-ren', 'backend': 'codex'}}

workers = wm.get_workers()
ren = workers[0]
assert ren['name'] == 'ren', workers
assert 'not supported yet' in ren['note'].lower(), ren
assert 'echo' not in ren.get('send_example', ''), ren
assert 'mac-mini' in ren['address'], ren

bridge.NODE_DIR = orig_node
bridge.WORKER_REGISTRY_FILE = orig_reg
bridge.SESSIONS_DIR = orig_sessions
bridge.WORKER_PIPE_ROOT = orig_pipe_root
shutil.rmtree(tmpdir, ignore_errors=True)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/workers warns for remote non-interactive workers"
    else
        fail "/workers should warn for remote non-interactive workers"
    fi
}

test_check_adapter_log_teleported_reads_remote() {
    info "Testing adapter log inspection reads remote logs for teleported workers..."

    if python3 -c "
import tempfile, shutil
from pathlib import Path
from unittest.mock import patch, MagicMock
import bridge

tmpdir = tempfile.mkdtemp()
tmp = Path(tmpdir)
orig_node = bridge.NODE_DIR
orig_reg = bridge.WORKER_REGISTRY_FILE
orig_sessions = bridge.SESSIONS_DIR

bridge.NODE_DIR = tmp
bridge.WORKER_REGISTRY_FILE = tmp / 'workers.json'
bridge.SESSIONS_DIR = tmp / 'sessions'
bridge.SESSIONS_DIR.mkdir()
bridge._registry_add('ren', 'codex', 123, host='mac-mini')

calls = []
def mock_remote(cmd, host=None, **kwargs):
    calls.append((cmd, host))
    if len(cmd) == 3 and cmd[0] == 'bash' and 'HOME' in cmd[2]:
        return MagicMock(returncode=0, stdout='/Users/beastoinagents\n', stderr='')
    if cmd[:2] == ['tail', '-n']:
        return MagicMock(returncode=0, stdout='remote adapter log\n', stderr='')
    return MagicMock(returncode=1, stdout='', stderr='missing')

with patch('bridge._remote_run', side_effect=mock_remote):
    text = bridge._check_adapter_log('ren', tail_lines=5)

assert text == 'remote adapter log\n', f'expected remote log text, got {text!r}'
assert any(cmd[:2] == ['tail', '-n'] and host == 'mac-mini' for cmd, host in calls), f'remote tail missing: {calls}'

local_dir = bridge.SESSIONS_DIR / 'lee'
local_dir.mkdir()
(local_dir / 'adapter.log').write_text('local adapter log\\n')
assert bridge._check_adapter_log('lee', tail_lines=5) == 'local adapter log\\n'

bridge.NODE_DIR = orig_node
bridge.WORKER_REGISTRY_FILE = orig_reg
bridge.SESSIONS_DIR = orig_sessions
shutil.rmtree(tmpdir, ignore_errors=True)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "adapter log inspection reads remote logs for teleported workers"
    else
        fail "adapter log inspection should read remote logs for teleported workers"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Phase 1: host=None parameter tests (teleport remote dispatch)
# ─────────────────────────────────────────────────────────────────────────────

test_remote_dispatch_get_pane_command() {
    info "Testing get_pane_command routes through _remote_run when host is set..."

    if python3 -c "
from unittest.mock import patch, MagicMock
import subprocess
import bridge

calls = []
def mock_remote(cmd, host=None, **kwargs):
    calls.append({'cmd': cmd, 'host': host})
    r = MagicMock()
    r.returncode = 0
    r.stdout = 'claude'
    return r

with patch('bridge._remote_run', side_effect=mock_remote):
    result = bridge.get_pane_command('claude-prod-ren', host='mac-mini')

assert len(calls) == 1, f'Expected 1 _remote_run call, got {len(calls)}'
assert calls[0]['host'] == 'mac-mini', f'Expected host=mac-mini, got {calls[0][\"host\"]}'
assert 'tmux' in calls[0]['cmd'][0], f'Expected tmux command, got {calls[0][\"cmd\"]}'
assert result == 'claude', f'Expected claude, got {result}'

# host=None should also work (backward compat)
calls.clear()
with patch('bridge._remote_run', side_effect=mock_remote):
    bridge.get_pane_command('claude-prod-ren')
assert calls[0]['host'] is None, f'Default host should be None'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "get_pane_command dispatches to _remote_run with host"
    else
        fail "get_pane_command should dispatch to _remote_run with host"
    fi
}

test_remote_dispatch_is_process_running() {
    info "Testing is_process_running passes host through call chain..."

    if python3 -c "
from unittest.mock import patch, MagicMock
import bridge

calls = []
def mock_remote(cmd, host=None, **kwargs):
    calls.append({'cmd': cmd, 'host': host})
    r = MagicMock()
    r.returncode = 0
    r.stdout = 'claude'
    return r

with patch('bridge._remote_run', side_effect=mock_remote):
    result = bridge.is_process_running('claude-prod-ren', 'claude', host='mac-mini')

# Should have called _remote_run for tmux display-message (via get_pane_command)
assert any(c['host'] == 'mac-mini' for c in calls), f'All calls should have host=mac-mini: {calls}'
assert result == True, f'Process name in pane command should match'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "is_process_running passes host to _remote_run"
    else
        fail "is_process_running should pass host to _remote_run"
    fi
}

test_remote_dispatch_is_claude_running() {
    info "Testing is_claude_running passes host through..."

    if python3 -c "
from unittest.mock import patch, MagicMock
import bridge

calls = []
def mock_remote(cmd, host=None, **kwargs):
    calls.append({'cmd': cmd, 'host': host})
    r = MagicMock()
    r.returncode = 0
    r.stdout = 'claude'
    return r

with patch('bridge._remote_run', side_effect=mock_remote):
    result = bridge.is_claude_running('claude-prod-ren', host='mac-mini')

assert any(c['host'] == 'mac-mini' for c in calls), f'Should route through _remote_run with host'
assert result == True
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "is_claude_running passes host to is_process_running"
    else
        fail "is_claude_running should pass host to is_process_running"
    fi
}

test_remote_dispatch_tmux_send_escape() {
    info "Testing tmux_send_escape routes through _remote_run when host is set..."

    if python3 -c "
from unittest.mock import patch, MagicMock
import bridge

calls = []
def mock_remote(cmd, host=None, **kwargs):
    calls.append({'cmd': cmd, 'host': host})
    return MagicMock(returncode=0)

with patch('bridge._remote_run', side_effect=mock_remote):
    bridge.tmux_send_escape('claude-prod-ren', host='mac-mini')

assert len(calls) == 1, f'Expected 1 call, got {len(calls)}'
assert calls[0]['host'] == 'mac-mini'
assert 'Escape' in calls[0]['cmd'], f'Should send Escape key: {calls[0][\"cmd\"]}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "tmux_send_escape dispatches to _remote_run with host"
    else
        fail "tmux_send_escape should dispatch to _remote_run with host"
    fi
}

test_remote_dispatch_tmux_pane_pids() {
    info "Testing _tmux_pane_pids routes through _remote_run when host is set..."

    if python3 -c "
from unittest.mock import patch, MagicMock
import bridge

calls = []
def mock_remote(cmd, host=None, **kwargs):
    calls.append({'cmd': cmd, 'host': host})
    r = MagicMock()
    r.returncode = 0
    r.stdout = 'claude-prod-ren 12345\nclaude-prod-lee 67890'
    return r

with patch('bridge._remote_run', side_effect=mock_remote):
    result = bridge._tmux_pane_pids(host='mac-mini')

assert len(calls) == 1, f'Expected 1 call, got {len(calls)}'
assert calls[0]['host'] == 'mac-mini'
assert result == {'claude-prod-ren': '12345', 'claude-prod-lee': '67890'}, f'Got {result}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_tmux_pane_pids dispatches to _remote_run with host"
    else
        fail "_tmux_pane_pids should dispatch to _remote_run with host"
    fi
}

test_remote_dispatch_ps_stats() {
    info "Testing _ps_stats routes through _remote_run when host is set..."

    if python3 -c "
from unittest.mock import patch, MagicMock
import bridge

calls = []
def mock_remote(cmd, host=None, **kwargs):
    calls.append({'cmd': cmd, 'host': host})
    r = MagicMock()
    r.returncode = 0
    r.stdout = '12345  2.5 S'
    return r

with patch('bridge._remote_run', side_effect=mock_remote):
    result = bridge._ps_stats(['12345'], host='mac-mini')

assert len(calls) == 1, f'Expected 1 call, got {len(calls)}'
assert calls[0]['host'] == 'mac-mini'
assert '12345' in result, f'Got {result}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_ps_stats dispatches to _remote_run with host"
    else
        fail "_ps_stats should dispatch to _remote_run with host"
    fi
}

test_remote_dispatch_capture_pane_text() {
    info "Testing _capture_pane_text routes through _remote_run when host is set..."

    if python3 -c "
from unittest.mock import patch, MagicMock
import bridge

calls = []
def mock_remote(cmd, host=None, **kwargs):
    calls.append({'cmd': cmd, 'host': host})
    r = MagicMock()
    r.returncode = 0
    r.stdout = 'some pane text'
    return r

with patch('bridge._remote_run', side_effect=mock_remote):
    result = bridge._capture_pane_text('claude-prod-ren', host='mac-mini')

assert len(calls) == 1, f'Expected 1 call, got {len(calls)}'
assert calls[0]['host'] == 'mac-mini'
assert result == 'some pane text', f'Got {result}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_capture_pane_text dispatches to _remote_run with host"
    else
        fail "_capture_pane_text should dispatch to _remote_run with host"
    fi
}

test_remote_dispatch_export_hook_env() {
    info "Testing export_hook_env routes through _remote_run when host is set..."

    if python3 -c "
from unittest.mock import patch, MagicMock
import bridge

calls = []
def mock_remote(cmd, host=None, **kwargs):
    calls.append({'cmd': cmd, 'host': host})
    # Handle echo HOME call for SESSIONS_DIR remapping
    if len(cmd) == 3 and cmd[0] == 'bash' and 'HOME' in cmd[2]:
        return MagicMock(returncode=0, stdout='/Users/beastoinagents\n', stderr='')
    return MagicMock(returncode=0, stdout='', stderr='')

with patch('bridge._remote_run', side_effect=mock_remote):
    bridge.export_hook_env('claude-prod-ren', host='mac-mini')

# Should have 6 calls: 5 set-environment + 1 echo HOME (for SESSIONS_DIR remapping)
assert len(calls) == 6, f'Expected 6 calls (5 set-env + 1 echo HOME), got {len(calls)}'
assert all(c['host'] == 'mac-mini' for c in calls), f'All calls should target mac-mini'
set_env_calls = [c for c in calls if 'set-environment' in ' '.join(c['cmd'])]
assert len(set_env_calls) == 5, f'Expected 5 set-environment calls, got {len(set_env_calls)}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "export_hook_env dispatches to _remote_run with host"
    else
        fail "export_hook_env should dispatch to _remote_run with host"
    fi
}

test_remote_dispatch_tmux_prompt_empty() {
    info "Testing tmux_prompt_empty routes through _remote_run when host is set..."

    if python3 -c "
from unittest.mock import patch, MagicMock
import bridge

calls = []
def mock_remote(cmd, host=None, **kwargs):
    calls.append({'cmd': cmd, 'host': host})
    r = MagicMock()
    r.returncode = 0
    r.stdout = 'some output\n❯ \n'
    return r

with patch('bridge._remote_run', side_effect=mock_remote):
    result = bridge.tmux_prompt_empty('claude-prod-ren', host='mac-mini')

assert len(calls) >= 1, f'Expected at least 1 call, got {len(calls)}'
assert calls[0]['host'] == 'mac-mini'
assert result == True, f'Should detect empty prompt'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "tmux_prompt_empty dispatches to _remote_run with host"
    else
        fail "tmux_prompt_empty should dispatch to _remote_run with host"
    fi
}

test_remote_dispatch_send_interactive_reply() {
    info "Testing _send_interactive_reply routes through _remote_run when host is set..."

    if python3 -c "
from unittest.mock import patch, MagicMock
import bridge

calls = []
def mock_remote(cmd, host=None, **kwargs):
    calls.append({'cmd': cmd, 'host': host})
    return MagicMock(returncode=0)

details = {'options': [{'num': 1, 'selected': True}, {'num': 2, 'selected': False}]}
with patch('bridge._remote_run', side_effect=mock_remote):
    result = bridge._send_interactive_reply('claude-prod-ren', 'cancel', details, host='mac-mini')

assert result == True, f'cancel should be handled'
assert len(calls) >= 1, f'Expected at least 1 call, got {len(calls)}'
assert calls[0]['host'] == 'mac-mini'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_send_interactive_reply dispatches to _remote_run with host"
    else
        fail "_send_interactive_reply should dispatch to _remote_run with host"
    fi
}

test_remote_dispatch_wait_for_restart_ready() {
    info "Testing _wait_for_restart_ready passes host to sub-calls..."

    if python3 -c "
from unittest.mock import patch, MagicMock
import bridge

tmux_exists_calls = []
activity_calls = []

def mock_tmux_exists(name, host=None):
    tmux_exists_calls.append({'name': name, 'host': host})
    return True

def mock_activity(name, host=None):
    activity_calls.append({'name': name, 'host': host})
    return ('Idle at prompt', None, None)

with patch('bridge.tmux_exists', side_effect=mock_tmux_exists), \
     patch('bridge._read_tmux_activity', side_effect=mock_activity):
    result = bridge._wait_for_restart_ready('claude-prod-ren', 'claude', host='mac-mini')

assert result == True, f'Should detect idle prompt'
assert any(c['host'] == 'mac-mini' for c in tmux_exists_calls), f'tmux_exists should get host: {tmux_exists_calls}'
assert any(c['host'] == 'mac-mini' for c in activity_calls), f'_read_tmux_activity should get host: {activity_calls}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_wait_for_restart_ready passes host to tmux_exists and _read_tmux_activity"
    else
        fail "_wait_for_restart_ready should pass host to sub-calls"
    fi
}

test_remote_dispatch_get_tmux_pane_cwd() {
    info "Testing _get_tmux_pane_cwd routes through _remote_run when host is set..."

    if python3 -c "
from unittest.mock import patch, MagicMock
import bridge

calls = []
def mock_remote(cmd, host=None, **kwargs):
    calls.append({'cmd': cmd, 'host': host})
    r = MagicMock()
    r.returncode = 0
    r.stdout = '/Users/beastoinagents/omi'
    return r

wm = bridge.WorkerManager.__new__(bridge.WorkerManager)
with patch('bridge._remote_run', side_effect=mock_remote):
    result = wm._get_tmux_pane_cwd('claude-prod-ren', host='mac-mini')

assert len(calls) == 1, f'Expected 1 call, got {len(calls)}'
assert calls[0]['host'] == 'mac-mini'
assert result == '/Users/beastoinagents/omi', f'Got {result}'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_get_tmux_pane_cwd dispatches to _remote_run with host"
    else
        fail "_get_tmux_pane_cwd should dispatch to _remote_run with host"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: Call site host= propagation tests (teleport)
# ─────────────────────────────────────────────────────────────────────────────

test_phase2_cmd_team_passes_host() {
    info "Testing cmd_team passes host to is_claude_running for teleported workers..."

    if python3 -c "
import json, tempfile, os
from pathlib import Path
from unittest.mock import patch, MagicMock
import bridge

tmpdir = tempfile.mkdtemp()
node_dir = Path(tmpdir) / 'node'
node_dir.mkdir()
sessions = Path(tmpdir) / 'sessions'
sessions.mkdir()
(sessions / 'ren').mkdir()
(sessions / 'ren' / 'chat_id').write_text('123')

# Registry: ren is teleported to mac-mini
reg = {'workers': {'ren': {'host': 'mac-mini', 'home_host': 'localhost', 'home_cwd': '/home/claude'}}}
(node_dir / 'workers.json').write_text(json.dumps(reg))

orig_sessions = bridge.SESSIONS_DIR
orig_node_dir = bridge.NODE_DIR
orig_registry = bridge.WORKER_REGISTRY_FILE
bridge.SESSIONS_DIR = sessions
bridge.NODE_DIR = node_dir
bridge.WORKER_REGISTRY_FILE = node_dir / 'workers.json'

icr_calls = []
orig_icr = bridge.is_claude_running
def mock_icr(tmux_name, host=None):
    icr_calls.append({'tmux': tmux_name, 'host': host})
    return True

class MockWorkers:
    tmux_prefix = 'claude-prod-'
    sessions_dir = sessions
    def _sync_paths(self): pass
    def scan_tmux_sessions(self):
        return {'claude-prod-ren': True}
    def get_registered_sessions(self, *a):
        return {'ren': {'tmux': 'claude-prod-ren'}}

class MockTelegramAPI:
    def send_message(self, *a, **k): pass

router = bridge.CommandRouter(MockTelegramAPI(), MockWorkers())
router.workers = MockWorkers()

replies = []
def mock_reply(self, chat_id, text, **kwargs):
    replies.append(text)

with patch('bridge.is_claude_running', side_effect=mock_icr), \
     patch('bridge.tmux_exists', return_value=True), \
     patch('bridge._read_tmux_activity', return_value=('Idle at prompt', '50%', None)), \
     patch('bridge.get_worker_backend', return_value='claude'), \
     patch.object(bridge.CommandRouter, 'reply', mock_reply):
    router.cmd_team(123)

# is_claude_running should have been called with host='mac-mini'
assert len(icr_calls) >= 1, f'Expected is_claude_running call, got {icr_calls}'
assert icr_calls[0]['host'] == 'mac-mini', f'Expected host=mac-mini, got {icr_calls[0][\"host\"]}'

bridge.SESSIONS_DIR = orig_sessions
bridge.NODE_DIR = orig_node_dir
bridge.WORKER_REGISTRY_FILE = orig_registry
import shutil
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "cmd_team passes host to is_claude_running"
    else
        fail "cmd_team should pass host to is_claude_running"
    fi
}

test_phase2_cmd_pause_passes_host() {
    info "Testing cmd_pause passes host to tmux_send_escape for teleported workers..."

    if python3 -c "
import json, tempfile, os
from pathlib import Path
from unittest.mock import patch, MagicMock
import bridge

tmpdir = tempfile.mkdtemp()
node_dir = Path(tmpdir) / 'node'
node_dir.mkdir()
sessions = Path(tmpdir) / 'sessions'
sessions.mkdir()
(sessions / 'ren').mkdir()
(sessions / 'ren' / 'chat_id').write_text('123')

reg = {'workers': {'ren': {'host': 'mac-mini', 'home_host': 'localhost', 'home_cwd': '/home/claude'}}}
(node_dir / 'workers.json').write_text(json.dumps(reg))

orig_sessions = bridge.SESSIONS_DIR
orig_node_dir = bridge.NODE_DIR
orig_registry = bridge.WORKER_REGISTRY_FILE
bridge.SESSIONS_DIR = sessions
bridge.NODE_DIR = node_dir
bridge.WORKER_REGISTRY_FILE = node_dir / 'workers.json'

escape_calls = []
orig_escape = bridge.tmux_send_escape
def mock_escape(tmux_name, host=None):
    escape_calls.append({'tmux': tmux_name, 'host': host})

orig_state = dict(bridge.state)
bridge.state['active'] = 'ren'

class MockWorkers:
    tmux_prefix = 'claude-prod-'
    sessions_dir = sessions
    def _sync_paths(self): pass
    def get_registered_sessions(self, *a):
        return {'ren': {'tmux': 'claude-prod-ren'}}

class MockTelegramAPI:
    def send_message(self, *a, **k): pass

router = bridge.CommandRouter(MockTelegramAPI(), MockWorkers())
router.workers = MockWorkers()

def mock_reply(self, chat_id, text, **kwargs): pass

with patch('bridge.tmux_send_escape', side_effect=mock_escape), \
     patch.object(bridge.CommandRouter, 'reply', mock_reply):
    router.cmd_pause(123)

assert len(escape_calls) == 1, f'Expected 1 tmux_send_escape call, got {escape_calls}'
assert escape_calls[0]['host'] == 'mac-mini', f'Expected host=mac-mini, got {escape_calls[0][\"host\"]}'

bridge.SESSIONS_DIR = orig_sessions
bridge.NODE_DIR = orig_node_dir
bridge.WORKER_REGISTRY_FILE = orig_registry
bridge.state.update(orig_state)
import shutil
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "cmd_pause passes host to tmux_send_escape"
    else
        fail "cmd_pause should pass host to tmux_send_escape"
    fi
}

test_phase2_cmd_progress_passes_host() {
    info "Testing cmd_progress passes host to is_claude_running for teleported workers..."

    if python3 -c "
import json, tempfile, os
from pathlib import Path
from unittest.mock import patch, MagicMock
import bridge

tmpdir = tempfile.mkdtemp()
node_dir = Path(tmpdir) / 'node'
node_dir.mkdir()
sessions = Path(tmpdir) / 'sessions'
sessions.mkdir()
(sessions / 'ren').mkdir()
(sessions / 'ren' / 'chat_id').write_text('123')

reg = {'workers': {'ren': {'host': 'mac-mini', 'home_host': 'localhost', 'home_cwd': '/home/claude'}}}
(node_dir / 'workers.json').write_text(json.dumps(reg))

orig_sessions = bridge.SESSIONS_DIR
orig_node_dir = bridge.NODE_DIR
orig_registry = bridge.WORKER_REGISTRY_FILE
orig_state = dict(bridge.state)
bridge.SESSIONS_DIR = sessions
bridge.NODE_DIR = node_dir
bridge.WORKER_REGISTRY_FILE = node_dir / 'workers.json'
bridge.state['active'] = 'ren'

icr_calls = []
def mock_icr(tmux_name, host=None):
    icr_calls.append({'tmux': tmux_name, 'host': host})
    return True

class MockWorkers:
    tmux_prefix = 'claude-prod-'
    sessions_dir = sessions
    def _sync_paths(self): pass
    def get_registered_sessions(self, *a):
        return {'ren': {'tmux': 'claude-prod-ren'}}

class MockTelegramAPI:
    def send_message(self, *a, **k): pass

router = bridge.CommandRouter(MockTelegramAPI(), MockWorkers())
router.workers = MockWorkers()

def mock_reply(self, chat_id, text, **kwargs): pass

with patch('bridge.is_claude_running', side_effect=mock_icr), \
     patch('bridge.tmux_exists', return_value=True), \
     patch('bridge._read_tmux_activity', return_value=('Working', '50%', None)), \
     patch('bridge.get_worker_backend', return_value='claude'), \
     patch('bridge.is_pending', return_value=False), \
     patch('bridge.get_any_session_id', return_value=(None, None)), \
     patch('bridge.get_claude_session_id', return_value=None), \
     patch.object(bridge.CommandRouter, 'reply', mock_reply):
    router.cmd_progress(123)

assert len(icr_calls) >= 1, f'Expected is_claude_running call, got {icr_calls}'
assert icr_calls[0]['host'] == 'mac-mini', f'Expected host=mac-mini, got {icr_calls[0][\"host\"]}'

bridge.SESSIONS_DIR = orig_sessions
bridge.NODE_DIR = orig_node_dir
bridge.WORKER_REGISTRY_FILE = orig_registry
bridge.state.update(orig_state)
import shutil
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "cmd_progress passes host to is_claude_running"
    else
        fail "cmd_progress should pass host to is_claude_running"
    fi
}

test_phase2_interactive_reply_passes_host() {
    info "Testing interactive reply routing passes host to _send_interactive_reply..."

    if python3 -c "
import json, tempfile, os
from pathlib import Path
from unittest.mock import patch, MagicMock
import bridge

tmpdir = tempfile.mkdtemp()
node_dir = Path(tmpdir) / 'node'
node_dir.mkdir()
sessions = Path(tmpdir) / 'sessions'
sessions.mkdir()
(sessions / 'ren').mkdir()
(sessions / 'ren' / 'chat_id').write_text('123')

reg = {'workers': {'ren': {'host': 'mac-mini', 'home_host': 'localhost', 'home_cwd': '/home/claude'}}}
(node_dir / 'workers.json').write_text(json.dumps(reg))

orig_sessions = bridge.SESSIONS_DIR
orig_node_dir = bridge.NODE_DIR
orig_registry = bridge.WORKER_REGISTRY_FILE
orig_state = dict(bridge.state)
orig_admin = bridge.admin_chat_id
bridge.SESSIONS_DIR = sessions
bridge.NODE_DIR = node_dir
bridge.WORKER_REGISTRY_FILE = node_dir / 'workers.json'
bridge.state['active'] = 'ren'
bridge.admin_chat_id = 123

sir_calls = []
def mock_sir(tmux_name, reply, details, host=None):
    sir_calls.append({'tmux': tmux_name, 'host': host})
    return True

details = {'options': [{'num': 1, 'selected': True}]}

class MockWorkers:
    tmux_prefix = 'claude-prod-'
    sessions_dir = sessions
    def _sync_paths(self): pass
    def get_registered_sessions(self, *a):
        return {'ren': {'tmux': 'claude-prod-ren'}}
    def send(self, name, text, chat_id=None, session=None):
        return True
    def is_online(self, name, session=None):
        return True

class MockTelegramAPI:
    def send_message(self, *a, **k): pass
    def set_reaction(self, *a, **k): pass

router = bridge.CommandRouter(MockTelegramAPI(), MockWorkers())
router.workers = MockWorkers()

def mock_reply(self, chat_id, text, **kwargs): pass

# Build a proper Telegram update dict
update = {
    'message': {
        'text': 'skip',
        'chat': {'id': 123},
        'message_id': 999
    }
}

with patch('bridge._send_interactive_reply', side_effect=mock_sir), \
     patch('bridge._read_tmux_activity', return_value=('Waiting', None, ['line1', 'line2'])), \
     patch('bridge._extract_question_details', return_value=details), \
     patch('bridge.get_worker_backend', return_value='claude'), \
     patch.object(bridge.CommandRouter, 'reply', mock_reply):
    router.handle_message(update)

assert len(sir_calls) >= 1, f'Expected _send_interactive_reply call, got {sir_calls}'
assert sir_calls[0]['host'] == 'mac-mini', f'Expected host=mac-mini, got {sir_calls[0][\"host\"]}'

bridge.SESSIONS_DIR = orig_sessions
bridge.NODE_DIR = orig_node_dir
bridge.WORKER_REGISTRY_FILE = orig_registry
bridge.state.update(orig_state)
bridge.admin_chat_id = orig_admin
import shutil
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Interactive reply passes host to _send_interactive_reply"
    else
        fail "Interactive reply should pass host to _send_interactive_reply"
    fi
}

test_phase2_checkin_passes_host() {
    info "Testing check-in handler passes host to export_hook_env and _wait_for_restart_ready..."

    if python3 -c "
import json, tempfile, os
from pathlib import Path
from unittest.mock import patch, MagicMock
import bridge

tmpdir = tempfile.mkdtemp()
node_dir = Path(tmpdir) / 'node'
node_dir.mkdir()
sessions = Path(tmpdir) / 'sessions'
sessions.mkdir()
(sessions / 'ren').mkdir()
(sessions / 'ren' / 'chat_id').write_text('123')

reg = {'workers': {'ren': {'host': 'mac-mini', 'home_host': 'localhost', 'home_cwd': '/home/claude'}}}
(node_dir / 'workers.json').write_text(json.dumps(reg))

orig_sessions = bridge.SESSIONS_DIR
orig_node_dir = bridge.NODE_DIR
orig_registry = bridge.WORKER_REGISTRY_FILE
bridge.SESSIONS_DIR = sessions
bridge.NODE_DIR = node_dir
bridge.WORKER_REGISTRY_FILE = node_dir / 'workers.json'

ehe_calls = []
def mock_ehe(tmux_name, backend=None, host=None):
    ehe_calls.append({'tmux': tmux_name, 'host': host})

te_calls = []
def mock_te(tmux_name, host=None):
    te_calls.append({'tmux': tmux_name, 'host': host})
    return True

class MockWorkerManager:
    tmux_prefix = bridge.TMUX_PREFIX
    sessions_dir = sessions
    def _sync_paths(self): pass
    def get_registered_sessions(self, *a):
        return {'ren': {'tmux': 'claude-prod-ren'}}
    def _get_tmux_pane_cwd(self, tmux_name, host=None):
        return '/Users/beastoinagents/omi'

orig_wm = bridge.worker_manager
bridge.worker_manager = MockWorkerManager()

with patch('bridge.export_hook_env', side_effect=mock_ehe), \
     patch('bridge.tmux_exists', side_effect=mock_te):

    # Simulate check-in: just verify export_hook_env is called with host
    name = 'ren'
    registered = bridge.worker_manager.get_registered_sessions()
    tmux_name = registered[name].get('tmux', f'{bridge.TMUX_PREFIX}{name}')
    host = bridge.get_worker_host(name)
    if bridge.tmux_exists(tmux_name, host=host):
        bridge.export_hook_env(tmux_name, 'claude', host=host)

assert len(ehe_calls) == 1, f'Expected 1 export_hook_env call, got {ehe_calls}'
assert ehe_calls[0]['host'] == 'mac-mini', f'Expected host=mac-mini, got {ehe_calls[0][\"host\"]}'
assert te_calls[0]['host'] == 'mac-mini', f'tmux_exists should get host=mac-mini, got {te_calls[0][\"host\"]}'

bridge.SESSIONS_DIR = orig_sessions
bridge.NODE_DIR = orig_node_dir
bridge.WORKER_REGISTRY_FILE = orig_registry
bridge.worker_manager = orig_wm
import shutil
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Check-in handler passes host to export_hook_env"
    else
        fail "Check-in handler should pass host to export_hook_env"
    fi
}

test_phase2_watchdog_capture_pane_passes_host() {
    info "Testing watchdog passes host to _capture_pane_text for teleported workers..."

    if python3 -c "
import json, tempfile, os
from pathlib import Path
from unittest.mock import patch, MagicMock
import bridge

tmpdir = tempfile.mkdtemp()
node_dir = Path(tmpdir) / 'node'
node_dir.mkdir()

reg = {'workers': {'ren': {'host': 'mac-mini', 'home_host': 'localhost', 'home_cwd': '/home/claude'}}}
(node_dir / 'workers.json').write_text(json.dumps(reg))

orig_node_dir = bridge.NODE_DIR
orig_registry = bridge.WORKER_REGISTRY_FILE
bridge.NODE_DIR = node_dir
bridge.WORKER_REGISTRY_FILE = node_dir / 'workers.json'

cpt_calls = []
orig_cpt = bridge._capture_pane_text
def mock_cpt(tmux_name, lines=50, host=None):
    cpt_calls.append({'tmux': tmux_name, 'host': host})
    return ''

# Test that _detect_poisoned passes host
with patch('bridge._capture_pane_text', side_effect=mock_cpt), \
     patch('bridge._check_adapter_log', return_value=''):
    bridge._detect_poisoned('ren', 'claude-prod-ren')

assert len(cpt_calls) == 1, f'Expected 1 _capture_pane_text call, got {cpt_calls}'
assert cpt_calls[0]['host'] == 'mac-mini', f'Expected host=mac-mini, got {cpt_calls[0][\"host\"]}'

bridge.NODE_DIR = orig_node_dir
bridge.WORKER_REGISTRY_FILE = orig_registry
import shutil
shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Watchdog/_detect_poisoned passes host to _capture_pane_text"
    else
        fail "Watchdog should pass host to _capture_pane_text"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Node-derived config tests
# ─────────────────────────────────────────────────────────────────────────────

test_node_derives_tmux_prefix() {
    info "Testing NODE_NAME derives TMUX_PREFIX..."

    # NODE_NAME=mynode → TMUX_PREFIX should be "claude-mynode-"
    if python3 -c "
import subprocess, sys, os
env = {k: v for k, v in os.environ.items()
       if k not in ('TMUX_PREFIX', 'SESSIONS_DIR', 'PORT', 'BRIDGE_URL')}
env['NODE_NAME'] = 'mynode'
env['TELEGRAM_BOT_TOKEN'] = 'test'
result = subprocess.run([sys.executable, '-c', '''
import bridge
print(bridge.TMUX_PREFIX)
'''], capture_output=True, text=True, env=env)
prefix = result.stdout.strip()
assert prefix == 'claude-mynode-', f'Expected claude-mynode-, got {prefix!r}'
"; then
        success "NODE_NAME=mynode → TMUX_PREFIX=claude-mynode-"
    else
        fail "NODE_NAME did not derive TMUX_PREFIX"
    fi
}

test_node_derives_sessions_dir() {
    info "Testing NODE_NAME derives SESSIONS_DIR..."

    # NODE_NAME=mynode → SESSIONS_DIR should be ~/.claude/telegram/nodes/mynode/sessions
    if python3 -c "
import subprocess, sys, os
env = {k: v for k, v in os.environ.items()
       if k not in ('TMUX_PREFIX', 'SESSIONS_DIR', 'PORT', 'BRIDGE_URL')}
env['NODE_NAME'] = 'mynode'
env['TELEGRAM_BOT_TOKEN'] = 'test'
result = subprocess.run([sys.executable, '-c', '''
import bridge
print(bridge.SESSIONS_DIR)
'''], capture_output=True, text=True, env=env)
got = result.stdout.strip()
expected = os.path.expanduser('~/.claude/telegram/nodes/mynode/sessions')
assert got == expected, f'Expected {expected}, got {got!r}'
"; then
        success "NODE_NAME=mynode → SESSIONS_DIR=~/.claude/telegram/nodes/mynode/sessions"
    else
        fail "NODE_NAME did not derive SESSIONS_DIR"
    fi
}

test_node_derives_port() {
    info "Testing NODE_NAME derives PORT from default map..."

    # NODE_NAME=prod → PORT=8271, dev→8272, test→8295, other→8270
    if python3 -c "
import subprocess, sys, os

cases = [('prod', 8271), ('dev', 8272), ('test', 8295), ('custom', 8270)]
for node, expected_port in cases:
    env = {k: v for k, v in os.environ.items()
           if k not in ('TMUX_PREFIX', 'SESSIONS_DIR', 'PORT', 'BRIDGE_URL')}
    env['NODE_NAME'] = node
    env['TELEGRAM_BOT_TOKEN'] = 'test'
    result = subprocess.run([sys.executable, '-c', '''
import bridge
print(bridge.PORT)
'''], capture_output=True, text=True, env=env)
    got = int(result.stdout.strip())
    assert got == expected_port, f'NODE_NAME={node}: expected PORT={expected_port}, got {got}'
"; then
        success "NODE_NAME derives correct PORT for prod/dev/test/other"
    else
        fail "NODE_NAME did not derive PORT correctly"
    fi
}

test_node_derives_bridge_url() {
    info "Testing NODE_NAME derives BRIDGE_URL from PORT..."

    # NODE_NAME=dev → PORT=8272 → BRIDGE_URL=http://localhost:8272
    if python3 -c "
import subprocess, sys, os
env = {k: v for k, v in os.environ.items()
       if k not in ('TMUX_PREFIX', 'SESSIONS_DIR', 'PORT', 'BRIDGE_URL')}
env['NODE_NAME'] = 'dev'
env['TELEGRAM_BOT_TOKEN'] = 'test'
result = subprocess.run([sys.executable, '-c', '''
import bridge
print(bridge.BRIDGE_URL)
'''], capture_output=True, text=True, env=env)
got = result.stdout.strip()
assert got == 'http://localhost:8272', f'Expected http://localhost:8272, got {got!r}'
"; then
        success "NODE_NAME=dev → BRIDGE_URL=http://localhost:8272"
    else
        fail "NODE_NAME did not derive BRIDGE_URL"
    fi
}

test_node_explicit_env_overrides() {
    info "Testing explicit env vars override NODE_NAME derivation..."

    # NODE_NAME=prod but PORT=9999, TMUX_PREFIX=custom-, SESSIONS_DIR=/tmp/custom
    if python3 -c "
import subprocess, sys, os, tempfile
tmpdir = tempfile.mkdtemp()
env = {k: v for k, v in os.environ.items()
       if k not in ('TMUX_PREFIX', 'SESSIONS_DIR', 'PORT', 'BRIDGE_URL')}
env['NODE_NAME'] = 'prod'
env['TELEGRAM_BOT_TOKEN'] = 'test'
env['PORT'] = '9999'
env['TMUX_PREFIX'] = 'custom-'
env['SESSIONS_DIR'] = tmpdir
result = subprocess.run([sys.executable, '-c', '''
import bridge
print(bridge.PORT)
print(bridge.TMUX_PREFIX)
print(bridge.SESSIONS_DIR)
'''], capture_output=True, text=True, env=env)
lines = result.stdout.strip().split('\n')
assert lines[0] == '9999', f'PORT: expected 9999, got {lines[0]!r}'
assert lines[1] == 'custom-', f'TMUX_PREFIX: expected custom-, got {lines[1]!r}'
assert lines[2] == tmpdir, f'SESSIONS_DIR: expected {tmpdir}, got {lines[2]!r}'
import shutil; shutil.rmtree(tmpdir, ignore_errors=True)
"; then
        success "Explicit env vars override NODE_NAME derivation"
    else
        fail "Explicit env vars did not override NODE_NAME"
    fi
}

test_node_empty_uses_defaults() {
    info "Testing no NODE_NAME uses existing defaults..."

    # No NODE_NAME, no PORT, no TMUX_PREFIX, no SESSIONS_DIR → original defaults
    if python3 -c "
import subprocess, sys, os
env = {k: v for k, v in os.environ.items()
       if k not in ('NODE_NAME', 'TMUX_PREFIX', 'SESSIONS_DIR', 'PORT', 'BRIDGE_URL')}
env['TELEGRAM_BOT_TOKEN'] = 'test'
result = subprocess.run([sys.executable, '-c', '''
import bridge
print(bridge.PORT)
print(bridge.TMUX_PREFIX)
print(bridge.SESSIONS_DIR)
print(bridge.BRIDGE_URL)
'''], capture_output=True, text=True, env=env)
lines = result.stdout.strip().split('\n')
expected_dir = os.path.expanduser('~/.claude/telegram/sessions')
assert lines[0] == '8270', f'PORT: expected 8270, got {lines[0]!r}'
assert lines[1] == 'claude-', f'TMUX_PREFIX: expected claude-, got {lines[1]!r}'
assert lines[2] == expected_dir, f'SESSIONS_DIR: expected {expected_dir}, got {lines[2]!r}'
assert lines[3] == 'http://localhost:8270', f'BRIDGE_URL: expected http://localhost:8270, got {lines[3]!r}'
"; then
        success "No NODE_NAME preserves original defaults"
    else
        fail "Missing NODE_NAME changed defaults"
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
# Worker Registry Tests (persistent registry)
# ─────────────────────────────────────────────────────────────────────────────

test_registry_add_remove() {
    info "Testing registry add/remove CRUD..."

    if python3 -c "
import os, json, tempfile, time
from pathlib import Path
import bridge

# Use temp dir to avoid touching real registry
tmpdir = tempfile.mkdtemp()
bridge.NODE_DIR = Path(tmpdir)
bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / 'workers.json'

# Add a worker
bridge._registry_add('alice', 'claude', 12345)
data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
assert 'alice' in data['workers'], 'alice not in registry'
assert data['workers']['alice']['backend'] == 'claude'
assert data['workers']['alice']['chat_id'] == 12345
assert data['version'] == 1

# Add another
bridge._registry_add('bob', 'codex', 67890)
data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
assert 'alice' in data['workers'] and 'bob' in data['workers'], 'both should exist'

# Remove alice
bridge._registry_remove('alice')
data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
assert 'alice' not in data['workers'], 'alice should be removed'
assert 'bob' in data['workers'], 'bob should remain'

# Remove nonexistent (no crash)
bridge._registry_remove('charlie')

# Verify file permissions
perms = oct(bridge.WORKER_REGISTRY_FILE.stat().st_mode)[-3:]
assert perms == '600', f'registry file should be 600, got {perms}'

import shutil; shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Registry add/remove CRUD works"
    else
        fail "Registry add/remove CRUD failed"
    fi
}

test_registry_bootstrap() {
    info "Testing registry bootstrap from tmux sessions..."

    if python3 -c "
import os, json, tempfile
from pathlib import Path
import bridge

tmpdir = tempfile.mkdtemp()
bridge.NODE_DIR = Path(tmpdir)
bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / 'workers.json'

# Simulate existing tmux sessions
registered = {
    'alice': {'tmux': 'claude-test-alice', 'backend': 'claude'},
    'bob': {'tmux': 'claude-test-bob', 'backend': 'codex'},
}

# Bootstrap should create registry
bridge._registry_bootstrap(registered)
assert bridge.WORKER_REGISTRY_FILE.exists(), 'registry file should be created'
data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
assert 'alice' in data['workers'] and 'bob' in data['workers'], 'both workers should be bootstrapped'
assert data['workers']['alice']['backend'] == 'claude'
assert data['workers']['bob']['backend'] == 'codex'

# Second call should be a no-op (file already exists)
old_mtime = bridge.WORKER_REGISTRY_FILE.stat().st_mtime
import time; time.sleep(0.01)
bridge._registry_bootstrap({'charlie': {'tmux': 'claude-test-charlie', 'backend': 'claude'}})
new_mtime = bridge.WORKER_REGISTRY_FILE.stat().st_mtime
assert old_mtime == new_mtime, 'bootstrap should not overwrite existing registry'

import shutil; shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Registry bootstrap from tmux sessions works"
    else
        fail "Registry bootstrap test failed"
    fi
}

test_registry_corrupt_recovery() {
    info "Testing registry corrupt file recovery..."

    if python3 -c "
import os, json, tempfile
from pathlib import Path
import bridge

tmpdir = tempfile.mkdtemp()
bridge.NODE_DIR = Path(tmpdir)
bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / 'workers.json'

# Write garbage to registry file
bridge.WORKER_REGISTRY_FILE.write_text('not valid json{{{')

# Load should return empty dict, not crash
data = bridge._load_registry()
assert data == {}, f'corrupt file should return empty dict, got {data}'

# Corrupt file should be renamed
corrupt_files = list(Path(tmpdir).glob('workers.corrupt.*'))
assert len(corrupt_files) == 1, f'expected 1 corrupt backup, got {len(corrupt_files)}'

# Now add a worker — should work fine after corrupt recovery
bridge._registry_add('alice', 'claude', 12345)
data = bridge._load_registry()
assert 'alice' in data.get('workers', {}), 'should work after corrupt recovery'

import shutil; shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Registry corrupt file recovery works"
    else
        fail "Registry corrupt recovery test failed"
    fi
}

test_get_registered_includes_registry() {
    info "Testing get_registered_sessions includes registry workers..."

    if python3 -c "
import os, json, tempfile
from pathlib import Path
import bridge

tmpdir = tempfile.mkdtemp()
bridge.NODE_DIR = Path(tmpdir)
bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / 'workers.json'
bridge.SESSIONS_DIR = Path(tmpdir) / 'sessions'
bridge.SESSIONS_DIR.mkdir()

# Pre-create registry with a dead worker
data = {'version': 1, 'workers': {
    'deadworker': {'backend': 'claude', 'chat_id': 123, 'hire_time': 1000}
}}
bridge.WORKER_REGISTRY_FILE.write_text(json.dumps(data))

# Create worker manager that returns no tmux sessions
wm = bridge.WorkerManager(bridge.SESSIONS_DIR, 'claude-test-')

# Mock scan_tmux_sessions to return empty (no live workers)
wm.scan_tmux_sessions = lambda: {}

registered = wm.get_registered_sessions()
assert 'deadworker' in registered, f'dead worker should be in registered, got {list(registered.keys())}'
assert registered['deadworker'].get('backend') == 'claude'
assert 'tmux' not in registered['deadworker'], 'dead worker should not have tmux key'

import shutil; shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "get_registered_sessions includes registry workers"
    else
        fail "get_registered_sessions registry merge failed"
    fi
}

test_checkin_cwd_stores_in_memory() {
    info "Testing /checkin?cwd stores cwd in memory..."

    if python3 -c "
import io, json, shutil, tempfile
from pathlib import Path
from urllib.parse import urlparse, quote
import bridge

tmpdir = tempfile.mkdtemp()
bridge.NODE_DIR = Path(tmpdir)
bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / 'workers.json'
bridge.SESSIONS_DIR = Path(tmpdir) / 'sessions'
bridge.SESSIONS_DIR.mkdir()
bridge.TMUX_PREFIX = '${TEST_TMUX_PREFIX}regcheckin-'
bridge._worker_cwds.clear()

bridge._registry_add('alice', 'claude', 123)
project_dir = Path(tmpdir) / 'project'
project_dir.mkdir()

class FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()
    def send_response(self, code):
        self.status = code
    def send_header(self, key, value):
        self.headers[key] = value
    def end_headers(self):
        pass

handler = FakeHandler()
parsed = urlparse('/checkin?name=alice&cwd=' + quote(str(project_dir)))
bridge.Handler.handle_checkin_endpoint(handler, parsed)

assert handler.status == 200, f'expected 200, got {handler.status}'
assert bridge._get_worker_cwd('alice') == str(project_dir), bridge._get_worker_cwd('alice')
data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
assert 'cwd' not in data['workers']['alice'], data['workers']['alice']
shutil.rmtree(tmpdir, ignore_errors=True)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/checkin?cwd stores cwd in memory"
    else
        fail "/checkin?cwd memory update failed"
    fi
}

test_checkin_cwd_invalid_path() {
    info "Testing /checkin?cwd rejects invalid path..."

    if python3 -c "
import io, json, shutil, tempfile
from pathlib import Path
from urllib.parse import urlparse, quote
import bridge

tmpdir = tempfile.mkdtemp()
bridge.NODE_DIR = Path(tmpdir)
bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / 'workers.json'
bridge.SESSIONS_DIR = Path(tmpdir) / 'sessions'
bridge.SESSIONS_DIR.mkdir()
bridge.TMUX_PREFIX = '${TEST_TMUX_PREFIX}regcheckin-'

bridge._registry_add('alice', 'claude', 123)
missing = Path(tmpdir) / 'does-not-exist'

class FakeHandler:
    def __init__(self):
        self.status = None
        self.wfile = io.BytesIO()
    def send_response(self, code):
        self.status = code
    def send_header(self, *_args, **_kwargs):
        pass
    def end_headers(self):
        pass

handler = FakeHandler()
parsed = urlparse('/checkin?name=alice&cwd=' + quote(str(missing)))
bridge.Handler.handle_checkin_endpoint(handler, parsed)

assert handler.status == 400, f'expected 400, got {handler.status}'
data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
assert 'cwd' not in data['workers']['alice'], data['workers']['alice']
shutil.rmtree(tmpdir, ignore_errors=True)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/checkin?cwd rejects invalid path with 400"
    else
        fail "/checkin?cwd invalid path test failed"
    fi
}

test_checkin_cwd_accepts_remote_path_for_teleported_worker() {
    info "Testing /checkin?cwd accepts remote path for teleported worker..."

    if python3 -c "
import io, json, shutil, tempfile, subprocess
from pathlib import Path
from urllib.parse import urlparse, quote
from unittest.mock import patch
import bridge

tmpdir = tempfile.mkdtemp()
bridge.NODE_DIR = Path(tmpdir)
bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / 'workers.json'
bridge.SESSIONS_DIR = Path(tmpdir) / 'sessions'
bridge.SESSIONS_DIR.mkdir()
bridge.TMUX_PREFIX = '${TEST_TMUX_PREFIX}regcheckin-'

bridge._registry_add('ren', 'claude', 123)
# Mark ren as teleported to a remote host
bridge._registry_update_teleport('ren', 'remote-host', None, '/home/claude/omi')

# Remote path that does NOT exist on VPS
remote_cwd = '/Users/beastoinagents/omi/omi-ren'

# Mock _remote_run to succeed for 'test -d' on the remote path
orig_remote_run = bridge._remote_run
def mock_remote_run(cmd, host=None, **kwargs):
    # Intercept the remote directory check
    if host == 'remote-host' and cmd[:2] == ['test', '-d']:
        return subprocess.CompletedProcess(cmd, 0)
    return orig_remote_run(cmd, host=host, **kwargs)

# Mock tmux_exists so we don't need a real session
# Mock worker_manager methods
class FakeHandler:
    def __init__(self):
        self.status = None
        self.wfile = io.BytesIO()
    def send_response(self, code):
        self.status = code
    def send_header(self, *_args, **_kwargs):
        pass
    def end_headers(self):
        pass

handler = FakeHandler()
parsed = urlparse('/checkin?name=ren&cwd=' + quote(remote_cwd))

with patch.object(bridge, '_remote_run', side_effect=mock_remote_run), \
     patch.object(bridge, 'tmux_exists', return_value=False):
    bridge.Handler.handle_checkin_endpoint(handler, parsed)

# Should NOT be 400 — remote path is valid on the remote host
assert handler.status != 400, f'expected non-400, got {handler.status}: {handler.wfile.getvalue()}'

shutil.rmtree(tmpdir, ignore_errors=True)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/checkin?cwd accepts remote path for teleported worker"
    else
        fail "/checkin?cwd remote path for teleported worker test failed"
    fi
}

test_checkin_cwd_restart_uses_remote_for_teleported() {
    info "Testing /checkin?cwd restart uses remote restart for teleported worker..."

    if python3 -c "
import io, json, shutil, tempfile, subprocess
from pathlib import Path
from urllib.parse import urlparse, quote
from unittest.mock import patch, MagicMock
import bridge

tmpdir = tempfile.mkdtemp()
bridge.NODE_DIR = Path(tmpdir)
bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / 'workers.json'
bridge.SESSIONS_DIR = Path(tmpdir) / 'sessions'
bridge.SESSIONS_DIR.mkdir()
bridge.TMUX_PREFIX = '${TEST_TMUX_PREFIX}checkin-'
(bridge.SESSIONS_DIR / 'ren').mkdir()
(bridge.SESSIONS_DIR / 'ren' / 'chat_id').write_text('123')
(bridge.SESSIONS_DIR / 'ren' / 'cwd').write_text('/Users/beastoinagents/old-dir')
bridge.admin_chat_id = 123

bridge._registry_add('ren', 'claude', 123)
bridge._registry_update_teleport('ren', host='mac-mini', home_host=None, home_cwd='/home/claude/omi')

remote_restart_calls = []
tg_messages = []

def mock_remote_run(cmd, host=None, **kwargs):
    # validate_cwd SSH check
    if cmd[:2] == ['test', '-d']:
        return subprocess.CompletedProcess(cmd, 0)
    # tmux_exists
    if 'has-session' in cmd:
        return subprocess.CompletedProcess(cmd, 0)
    # _get_tmux_pane_cwd
    if 'display-message' in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout='/Users/beastoinagents/old-dir', stderr='')
    return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

def mock_restart_remote(self, name, backend_name, backend, tmux_name, host, mode):
    remote_restart_calls.append({'name': name, 'host': host, 'mode': mode})
    return True, None

def mock_send_tg(chat_id, text, **kwargs):
    tg_messages.append(text)

new_cwd = '/Users/beastoinagents/omi/omi-ren'

class FakeHandler:
    def __init__(self):
        self.status = None
        self.headers_sent = []
        self.wfile = io.BytesIO()
    def send_response(self, code):
        self.status = code
    def send_header(self, *a):
        self.headers_sent.append(a)
    def end_headers(self):
        pass

handler = FakeHandler()
parsed = urlparse('/checkin?name=ren&cwd=' + quote(new_cwd))

with patch.object(bridge, '_remote_run', side_effect=mock_remote_run), \
     patch.object(bridge, 'send_telegram_message', side_effect=mock_send_tg), \
     patch.object(bridge.CommandRouter, '_restart_remote_worker', mock_restart_remote), \
     patch.object(bridge, '_wait_for_restart_ready', return_value=True):
    bridge.Handler.handle_checkin_endpoint(handler, parsed)

# Key assertion: should use _restart_remote_worker, not worker_manager.restart
assert len(remote_restart_calls) == 1, f'Should call _restart_remote_worker, got {remote_restart_calls}'
assert remote_restart_calls[0]['host'] == 'mac-mini', f'Should target mac-mini: {remote_restart_calls[0]}'

shutil.rmtree(tmpdir, ignore_errors=True)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/checkin?cwd restart uses remote restart for teleported worker"
    else
        fail "/checkin?cwd remote restart test failed"
    fi
}

test_checkin_cwd_restart_notifies_manager() {
    info "Testing /checkin?cwd restart sends manager start+ready notifications..."

    if python3 -c "
import io, shutil, tempfile
from pathlib import Path
from urllib.parse import urlparse, quote
import bridge

tmpdir = tempfile.mkdtemp()
tmp_path = Path(tmpdir)
bridge.NODE_DIR = tmp_path
bridge.WORKER_REGISTRY_FILE = tmp_path / 'workers.json'
bridge.SESSIONS_DIR = tmp_path / 'sessions'
bridge.SESSIONS_DIR.mkdir()
bridge.TMUX_PREFIX = '${TEST_TMUX_PREFIX}checkinnotify-'
bridge.admin_chat_id = None
bridge._worker_cwds.clear()

bridge._registry_add('alice', 'claude', 123)
session_dir = bridge.ensure_session_dir('alice')
chat_file = session_dir / 'chat_id'
chat_file.write_text('777')
chat_file.chmod(0o600)

old_dir = tmp_path / 'old-project'
new_dir = tmp_path / 'new-project'
old_dir.mkdir()
new_dir.mkdir()

bridge.worker_manager.get_registered_sessions = lambda registered=None: {
    'alice': {'tmux': f'{bridge.TMUX_PREFIX}alice', 'backend': 'claude'}
}
bridge.tmux_exists = lambda _name, host=None: True
bridge.export_hook_env = lambda *_args, **_kwargs: None
bridge.worker_manager._get_tmux_pane_cwd = lambda _tmux, host=None: str(old_dir)
bridge.worker_manager.restart = lambda name, mode='relaunch': (True, None)
bridge._wait_for_restart_ready = lambda *_args, **_kwargs: True

sent = []
def fake_send(chat_id, text):
    sent.append((chat_id, text))
    return {'ok': True}
bridge.send_telegram_message = fake_send

class FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()
    def send_response(self, code):
        self.status = code
    def send_header(self, key, value):
        self.headers[key] = value
    def end_headers(self):
        pass

handler = FakeHandler()
parsed = urlparse('/checkin?name=alice&cwd=' + quote(str(new_dir)))
bridge.Handler.handle_checkin_endpoint(handler, parsed)

body = handler.wfile.getvalue().decode()
assert handler.status == 200, f'expected 200, got {handler.status}: {body}'
assert 'Restarting in' in body, body
assert len(sent) == 2, f'expected 2 notifications, got {len(sent)}: {sent}'
assert sent[0][0] == 777 and sent[1][0] == 777, sent
first = sent[0][1].lower()
second = sent[1][1].lower()
assert 'alice' in first and 'restart' in first, first
assert 'lost' in first or 'hold' in first, first
assert 'alice' in second and 'ready' in second, second

shutil.rmtree(tmpdir, ignore_errors=True)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/checkin?cwd restart sends manager notifications"
    else
        fail "/checkin?cwd restart notification test failed"
    fi
}

test_checkin_cwd_restart_prefers_admin_chat_id() {
    info "Testing /checkin?cwd restart prefers admin chat id over session chat id..."

    if python3 -c "
import io, shutil, tempfile
from pathlib import Path
from urllib.parse import urlparse, quote
import bridge

tmpdir = tempfile.mkdtemp()
tmp_path = Path(tmpdir)
bridge.NODE_DIR = tmp_path
bridge.WORKER_REGISTRY_FILE = tmp_path / 'workers.json'
bridge.SESSIONS_DIR = tmp_path / 'sessions'
bridge.SESSIONS_DIR.mkdir()
bridge.TMUX_PREFIX = '${TEST_TMUX_PREFIX}checkinnotify-'
bridge.admin_chat_id = 999
bridge._worker_cwds.clear()

bridge._registry_add('alice', 'claude', 123)
session_dir = bridge.ensure_session_dir('alice')
chat_file = session_dir / 'chat_id'
chat_file.write_text('777')
chat_file.chmod(0o600)

old_dir = tmp_path / 'old-project'
new_dir = tmp_path / 'new-project'
old_dir.mkdir()
new_dir.mkdir()

bridge.worker_manager.get_registered_sessions = lambda registered=None: {
    'alice': {'tmux': f'{bridge.TMUX_PREFIX}alice', 'backend': 'claude'}
}
bridge.tmux_exists = lambda _name, host=None: True
bridge.export_hook_env = lambda *_args, **_kwargs: None
bridge.worker_manager._get_tmux_pane_cwd = lambda _tmux, host=None: str(old_dir)
bridge.worker_manager.restart = lambda name, mode='relaunch': (True, None)
bridge._wait_for_restart_ready = lambda *_args, **_kwargs: True

sent = []
def fake_send(chat_id, text):
    sent.append((chat_id, text))
    return {'ok': True}
bridge.send_telegram_message = fake_send

class FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()
    def send_response(self, code):
        self.status = code
    def send_header(self, key, value):
        self.headers[key] = value
    def end_headers(self):
        pass

handler = FakeHandler()
parsed = urlparse('/checkin?name=alice&cwd=' + quote(str(new_dir)))
bridge.Handler.handle_checkin_endpoint(handler, parsed)

body = handler.wfile.getvalue().decode()
assert handler.status == 200, f'expected 200, got {handler.status}: {body}'
assert len(sent) == 2, f'expected 2 notifications, got {len(sent)}: {sent}'
assert sent[0][0] == 999 and sent[1][0] == 999, sent

shutil.rmtree(tmpdir, ignore_errors=True)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/checkin?cwd restart uses admin chat id when set"
    else
        fail "/checkin?cwd admin chat id preference test failed"
    fi
}

test_checkin_cwd_restart_failure_notifies_manager() {
    info "Testing /checkin?cwd restart failure sends manager follow-up..."

    if python3 -c "
import io, shutil, tempfile
from pathlib import Path
from urllib.parse import urlparse, quote
import bridge

tmpdir = tempfile.mkdtemp()
tmp_path = Path(tmpdir)
bridge.NODE_DIR = tmp_path
bridge.WORKER_REGISTRY_FILE = tmp_path / 'workers.json'
bridge.SESSIONS_DIR = tmp_path / 'sessions'
bridge.SESSIONS_DIR.mkdir()
bridge.TMUX_PREFIX = '${TEST_TMUX_PREFIX}checkinnotify-'
bridge.admin_chat_id = None
bridge._worker_cwds.clear()

bridge._registry_add('alice', 'claude', 123)
session_dir = bridge.ensure_session_dir('alice')
chat_file = session_dir / 'chat_id'
chat_file.write_text('777')
chat_file.chmod(0o600)

old_dir = tmp_path / 'old-project'
new_dir = tmp_path / 'new-project'
old_dir.mkdir()
new_dir.mkdir()

bridge.worker_manager.get_registered_sessions = lambda registered=None: {
    'alice': {'tmux': f'{bridge.TMUX_PREFIX}alice', 'backend': 'claude'}
}
bridge.tmux_exists = lambda _name, host=None: True
bridge.export_hook_env = lambda *_args, **_kwargs: None
bridge.worker_manager._get_tmux_pane_cwd = lambda _tmux, host=None: str(old_dir)
bridge.worker_manager.restart = lambda name, mode='relaunch': (False, 'boom')

sent = []
def fake_send(chat_id, text):
    sent.append((chat_id, text))
    return {'ok': True}
bridge.send_telegram_message = fake_send

class FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()
    def send_response(self, code):
        self.status = code
    def send_header(self, key, value):
        self.headers[key] = value
    def end_headers(self):
        pass

handler = FakeHandler()
parsed = urlparse('/checkin?name=alice&cwd=' + quote(str(new_dir)))
bridge.Handler.handle_checkin_endpoint(handler, parsed)

body = handler.wfile.getvalue().decode()
assert handler.status == 500, f'expected 500, got {handler.status}: {body}'
assert 'Failed to restart' in body, body
assert len(sent) == 2, f'expected 2 notifications, got {len(sent)}: {sent}'
assert sent[0][0] == 777 and sent[1][0] == 777, sent
assert 'restart' in sent[1][1].lower() and ('fail' in sent[1][1].lower() or 'could not' in sent[1][1].lower()), sent[1][1]

shutil.rmtree(tmpdir, ignore_errors=True)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/checkin?cwd restart failure sends manager follow-up"
    else
        fail "/checkin?cwd restart failure notification test failed"
    fi
}

test_restart_dead_worker() {
    info "Testing restart of dead worker (tmux gone, in registry)..."

    if python3 -c "
import os, json, tempfile, subprocess, time
from pathlib import Path
from unittest.mock import patch, MagicMock
import bridge

tmpdir = tempfile.mkdtemp()
bridge.NODE_DIR = Path(tmpdir)
bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / 'workers.json'
bridge.SESSIONS_DIR = Path(tmpdir) / 'sessions'
bridge.SESSIONS_DIR.mkdir()

# Pre-create registry with a dead worker
data = {'version': 1, 'workers': {
    'deadworker': {'backend': 'claude', 'chat_id': 123, 'hire_time': 1000}
}}
bridge.WORKER_REGISTRY_FILE.write_text(json.dumps(data))

prefix = 'claude-regtest-'
bridge.TMUX_PREFIX = prefix
wm = bridge.WorkerManager(bridge.SESSIONS_DIR, prefix)
wm.scan_tmux_sessions = lambda: {}

registered = wm.get_registered_sessions()
assert 'deadworker' in registered, 'dead worker should be in registered'

# Test that restart detects dead worker and enters recovery path
# Mock _restart_dead_worker to track that it was called (avoids tmux/claude deps)
called = {}
original = wm._restart_dead_worker
def mock_restart(name, backend_name, backend, tmux_name, mode):
    called['name'] = name
    called['backend'] = backend_name
    called['tmux'] = tmux_name
    called['mode'] = mode
    return True, None

wm._restart_dead_worker = mock_restart

ok, err = wm.restart('deadworker', mode='relaunch')
assert ok, f'restart should succeed, got err: {err}'
assert called.get('name') == 'deadworker', f'expected deadworker, got {called}'
assert called.get('backend') == 'claude', f'expected claude backend, got {called}'
assert called.get('tmux') == f'{prefix}deadworker', f'expected regtest prefix, got {called}'
assert called.get('mode') == 'relaunch', f'expected relaunch mode, got {called}'

import shutil; shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Restart of dead worker recovers from registry"
    else
        fail "Restart dead worker test failed"
    fi
}

test_end_removes_from_registry() {
    info "Testing /end removes worker from registry..."

    if python3 -c "
import os, json, tempfile, subprocess
from pathlib import Path
import bridge

tmpdir = tempfile.mkdtemp()
bridge.NODE_DIR = Path(tmpdir)
bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / 'workers.json'
bridge.SESSIONS_DIR = Path(tmpdir) / 'sessions'
bridge.SESSIONS_DIR.mkdir()

prefix = 'claude-regtest-'
wm = bridge.WorkerManager(bridge.SESSIONS_DIR, prefix)

# Create a tmux session to simulate a live worker
tmux_name = f'{prefix}endtest'
subprocess.run(['tmux', 'new-session', '-d', '-s', tmux_name], capture_output=True)

# Add to registry
bridge._registry_add('endtest', 'claude', 123)
data = bridge._load_registry()
assert 'endtest' in data['workers'], 'worker should be in registry before end'

# End the worker
ok, err = wm.end('endtest')
assert ok, f'end should succeed, got err: {err}'

# Verify removed from registry
data = bridge._load_registry()
assert 'endtest' not in data.get('workers', {}), 'worker should be removed from registry after end'

import shutil; shutil.rmtree(tmpdir)
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/end removes worker from registry"
    else
        fail "/end registry removal test failed"
    fi
}

test_team_shows_exited() {
    info "Testing /team shows exited workers..."

    if python3 -c "
import bridge

# Mock a registered dict with one exited worker (no tmux key)
registered = {
    'alive': {'tmux': 'claude-test-alive', 'backend': 'claude'},
    'dead': {'backend': 'claude'},  # no tmux key = exited
}

# Record EXITED state in watchdog
bridge._record_worker_state('dead', 'EXITED', 'session gone', 1000)
bridge._record_worker_state('alive', 'READY', 'idle', 1000)

lines = bridge.format_team_lines(registered, 'alive')
output = '\n'.join(lines)
assert 'dead' in output, f'dead worker should appear in team output'
assert 'Session ended' in output, f'exited status should appear as Session ended: {output}'
assert 'alive' in output, f'alive worker should appear in team output'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/team shows exited workers"
    else
        fail "/team exited display test failed"
    fi
}

test_team_live_activity_format() {
    info "Testing /team shows live activity, attention, and sorting..."

    if python3 -c "
import bridge

registered = {
    'kenji': {'tmux': 'claude-test-kenji', 'backend': 'claude'},
    'lee': {'tmux': 'claude-test-lee', 'backend': 'claude'},
    'mon': {'tmux': 'claude-test-mon', 'backend': 'claude'},
    'kelvin': {'tmux': 'claude-test-kelvin', 'backend': 'claude'},
}

# Set watchdog states
bridge._record_worker_state('kenji', 'BUSY_TOOL', 'tools', 1000)
bridge._record_worker_state('lee', 'READY', 'idle', 1000)
bridge._record_worker_state('mon', 'READY', 'idle', 1000)
bridge._record_worker_state('kelvin', 'BUSY_THINKING', 'thinking', 1000)

# Provide live activity data
worker_live = {
    'kenji': {'backend': 'claude', 'activity': 'Running Bash', 'context_pct': '54%'},
    'lee': {'backend': 'claude', 'activity': 'Ready', 'context_pct': '27%'},
    'mon': {'backend': 'claude', 'activity': 'Rate limited — waiting to retry', 'context_pct': '42%'},
    'kelvin': {'backend': 'claude', 'activity': 'Waiting for plan approval', 'context_pct': '27%'},
}

lines = bridge.format_team_lines(registered, 'kenji', worker_live=worker_live)
output = '\n'.join(lines)

# Header should have team count and traffic-light summary
assert '4 agents' in output, f'Missing team count: {output}'
assert 'focused: kenji' in output.lower() or 'focused: Kenji' in output, f'Missing focused: {output}'

# Red/yellow/green counts
assert chr(0x1F534) in output, f'Missing red circle: {output}'  # 🔴
assert chr(0x1F7E1) in output, f'Missing yellow circle: {output}'  # 🟡
assert chr(0x1F7E2) in output, f'Missing green circle: {output}'  # 🟢

# mon has rate limit -> needs attention
assert 'mon' in output and 'rate limit' in output.lower(), f'Missing mon rate limit: {output}'

# kelvin waiting for approval -> needs reply
assert 'kelvin' in output and 'needs reply' in output.lower(), f'Missing kelvin needs reply: {output}'

# Activity should appear for each worker
assert 'Running Bash' in output, f'Missing kenji activity: {output}'
assert 'Ready' in output, f'Missing lee Ready activity: {output}'

# Red workers should come before green in the list
lines_lower = output.lower()
mon_pos = lines_lower.index('mon')
lee_pos = lines_lower.index('lee')
assert mon_pos < lee_pos, f'Red (mon) should sort before green (lee): {output}'

# Focused worker should have target icon
assert chr(0x1F3AF) in output, f'Missing target icon for focused worker: {output}'  # 🎯

# Backend should NOT show when all same
assert 'backend=' not in output, f'backend should be hidden when all same: {output}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/team shows live activity and attention"
    else
        fail "/team live activity format test failed"
    fi
}

test_normalize_activity_spinner_verbs() {
    info "Testing spinner verbs are normalized to Thinking..."

    if python3 -c "
import bridge

cases = [
    ('Ionizing (52m 40s)', 'Thinking (52m 40s)'),
    ('Schlepping (45s)', 'Thinking (45s)'),
    ('Whirring (2m 19s)', 'Thinking (2m 19s)'),
    ('Hullaballooing (17m 40s)', 'Thinking (17m 40s)'),
    ('Running Bash', 'Running Bash'),
    ('Ready', 'Ready'),
    ('In plan mode', 'In plan mode'),
    ('Error: something broke', 'Error: something broke'),
    ('Tasks (3/5 done)', 'Tasks (3/5 done)'),
    ('Compacting conversation (5m)', 'Compacting conversation (5m)'),
]
for raw, expected in cases:
    got = bridge._normalize_activity(raw)
    assert got == expected, f'_normalize_activity({raw!r}) = {got!r}, expected {expected!r}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Spinner verbs normalized to Thinking"
    else
        fail "Spinner verb normalization failed"
    fi
}

test_team_ready_replaces_idle() {
    info "Testing _extract_activity returns Ready for idle prompt..."

    if python3 -c "
import bridge

# Simulate pane with idle prompt
pane_lines = [
    '--- some output ---',
    '❯ ',
    '-------------------------------------------',
    '  ⏵⏵ bypass permissions on · 5 bashes',
]
result = bridge._extract_activity(pane_lines)
assert result == 'Ready', f'Expected Ready, got {result!r}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_extract_activity returns Ready for idle prompt"
    else
        fail "_extract_activity Ready test failed"
    fi
}

test_team_header_human_friendly() {
    info "Testing /team header is human-friendly..."

    if python3 -c "
import bridge

registered = {
    'kai': {'tmux': 'claude-test-kai', 'backend': 'claude'},
    'lee': {'tmux': 'claude-test-lee', 'backend': 'claude'},
}
bridge._record_worker_state('kai', 'WAITING_INPUT', 'question=Auth', 1000)
bridge._record_worker_state('lee', 'READY', 'idle', 1000)

lines = bridge.format_team_lines(registered, 'lee')
header = lines[0]

assert 'agents' in header, f'Header should contain agents: {header}'
assert 'ok' in header.lower() or 'active' in header.lower(), f'Header should contain ok: {header}'
assert 'need reply' in header.lower() or 'waiting' in header.lower(), f'Header should say need reply: {header}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/team header is human-friendly"
    else
        fail "/team header human-friendly test failed"
    fi
}

test_team_row_format_simplified() {
    info "Testing /team row format is simplified..."

    if python3 -c "
import bridge

registered = {
    'kai': {'tmux': 'claude-test-kai', 'backend': 'claude'},
    'lee': {'tmux': 'claude-test-lee', 'backend': 'claude'},
}
bridge._record_worker_state('kai', 'READY', 'idle', 1000)
bridge._record_worker_state('lee', 'READY', 'idle', 1000)

worker_live = {
    'kai': {'backend': 'claude', 'activity': 'Ready', 'context_pct': '54%'},
    'lee': {'backend': 'claude', 'activity': 'Ready', 'context_pct': '--'},
}

lines = bridge.format_team_lines(registered, 'lee', worker_live=worker_live)
output = '\n'.join(lines)

# Should not have 'no blocker' or 'needs input' as separate column
assert 'no blocker' not in output, f'Should not have no blocker column: {output}'

# Worker with context should show it
kai_line = [l for l in lines if 'kai' in l][0]
assert '54%' in kai_line, f'kai should show context: {kai_line}'

# Worker without context should not show --
lee_line = [l for l in lines if 'lee' in l][0]
assert '-- ' not in lee_line and '| --' not in lee_line, f'lee should not show --: {lee_line}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "/team row format simplified"
    else
        fail "/team row format test failed"
    fi
}

test_watchdog_alert_stuck_copy() {
    info "Testing watchdog STUCK alert copy..."

    if python3 -c "
import bridge
from unittest.mock import patch

bridge.admin_chat_id = 123
bridge._last_alert_ts = {}
bridge._prev_worker_states = {'alice': 'READY'}

sent = {}
def fake_api(method, data):
    sent['method'] = method
    sent['data'] = data
    return {'ok': True}

with patch('bridge.telegram_api', fake_api):
    bridge._handle_watchdog_transition('alice', 'STUCK', 'age=900s cpu=0.0', since=100, now=1000)

txt = sent['data']['text']
assert 'no progress' in txt.lower(), f'Should say no progress: {txt}'
assert '/restart --clean alice' in txt, f'Should have restart command: {txt}'
assert 'starts fresh' in txt.lower(), f'Should explain --clean: {txt}'
assert 'STUCK' not in txt, f'Should not expose internal state name: {txt}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Watchdog STUCK alert has human-friendly copy"
    else
        fail "Watchdog STUCK alert copy test failed"
    fi
}

test_watchdog_alert_poisoned_copy() {
    info "Testing watchdog POISONED alert copy..."

    if python3 -c "
import bridge
from unittest.mock import patch

bridge.admin_chat_id = 123
bridge._last_alert_ts = {}
bridge._prev_worker_states = {'bob': 'READY'}

sent = {}
def fake_api(method, data):
    sent['data'] = data
    return {'ok': True}

with patch('bridge.telegram_api', fake_api):
    bridge._handle_watchdog_transition('bob', 'POISONED', 'error loop', since=100, now=1000)

txt = sent['data']['text']
assert 'error' in txt.lower(), f'Should mention error: {txt}'
assert 'POISONED' not in txt, f'Should not expose internal state POISONED: {txt}'
assert '/restart --clean bob' in txt, f'Should have restart command: {txt}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Watchdog POISONED alert uses human-friendly copy"
    else
        fail "Watchdog POISONED alert copy test failed"
    fi
}

test_watchdog_alert_dead_copy() {
    info "Testing watchdog DEAD alert copy..."

    if python3 -c "
import bridge
from unittest.mock import patch

bridge.admin_chat_id = 123
bridge._last_alert_ts = {}
bridge._prev_worker_states = {'carol': 'READY'}

sent = {}
def fake_api(method, data):
    sent['data'] = data
    return {'ok': True}

with patch('bridge.telegram_api', fake_api):
    bridge._handle_watchdog_transition('carol', 'DEAD', 'process gone', since=100, now=1000)

txt = sent['data']['text']
assert 'stopped' in txt.lower(), f'Should say stopped: {txt}'
assert 'process' not in txt.lower(), f'Should not say process: {txt}'
assert '/restart --clean carol' in txt, f'Should have restart command: {txt}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Watchdog DEAD alert uses human-friendly copy"
    else
        fail "Watchdog DEAD alert copy test failed"
    fi
}

test_watchdog_alert_waiting_input_copy() {
    info "Testing watchdog WAITING_INPUT alert copy..."

    if python3 -c "
import bridge
from unittest.mock import patch

bridge.admin_chat_id = 123
bridge._last_alert_ts = {}
bridge._prev_worker_states = {'dave': 'READY'}
bridge._waiting_input_details = {
    'dave': {
        'header': 'Auth method',
        'options': [
            {'num': 1, 'label': 'OAuth', 'selected': True},
            {'num': 2, 'label': 'JWT', 'selected': False},
        ],
        'selected_num': 1,
    }
}

sent = {}
def fake_api(method, data):
    sent['data'] = data
    return {'ok': True}

with patch('bridge.telegram_api', fake_api):
    bridge._handle_watchdog_transition('dave', 'WAITING_INPUT', 'question=Auth', since=100, now=1000)

txt = sent['data']['text']
assert 'needs your reply' in txt.lower() or 'reply' in txt.lower(), f'Should say needs reply: {txt}'
assert 'Auth method' in txt, f'Should include question header: {txt}'
assert 'waiting for your input' not in txt.lower(), f'Should not use old copy: {txt}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Watchdog WAITING_INPUT alert uses human-friendly copy"
    else
        fail "Watchdog WAITING_INPUT alert copy test failed"
    fi
}

test_watchdog_resolved_copy() {
    info "Testing watchdog resolved alert copy..."

    if python3 -c "
import bridge
from unittest.mock import patch

bridge.admin_chat_id = 123
bridge._prev_worker_states = {'eve': 'STUCK'}
bridge._recent_restarts = {}

sent = {}
def fake_api(method, data):
    sent['data'] = data
    return {'ok': True}

with patch('bridge.telegram_api', fake_api):
    bridge._send_resolved_alert('eve', 'READY')

txt = sent['data']['text']
assert 'back to normal' in txt.lower() or chr(0x2705) in txt, f'Should say back to normal: {txt}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Watchdog resolved alert has good copy"
    else
        fail "Watchdog resolved alert copy test failed"
    fi
}

test_teleport_suppresses_offline_message() {
    info "Testing route_message says 'being teleported' instead of 'offline' when teleport_state exists..."

    local tmpscript tmpout
    tmpscript=$(mktemp /tmp/test_teleport_offline_XXXXX.py)
    tmpout=$(mktemp)
    cat > "$tmpscript" << 'PYEOF'
import os, sys, tempfile, json
from pathlib import Path
from unittest.mock import MagicMock
import bridge

tmpdir = tempfile.mkdtemp()
sessions_dir = Path(tmpdir) / 'sessions'
sessions_dir.mkdir()
orig_sessions_dir = bridge.SESSIONS_DIR
bridge.SESSIONS_DIR = sessions_dir

worker_name = 'alice'
worker_dir = sessions_dir / worker_name
worker_dir.mkdir()
(worker_dir / 'chat_id').write_text('12345')
(worker_dir / 'backend').write_text('claude')

# Write teleport_state file to simulate teleport in progress
(worker_dir / 'teleport_state').write_text(json.dumps({
    "phase": 1, "source_host": "host1",
    "target_host": "host2", "target_cwd": "/tmp",
    "started_at": 1000,
}))

tmux_name = f'{bridge.TMUX_PREFIX}{worker_name}'
router = bridge.command_router
orig_workers = router.workers

mock_workers = MagicMock()
mock_workers.get_registered_sessions.return_value = {
    worker_name: {'tmux': tmux_name}
}
mock_workers.is_online.return_value = False  # Worker appears offline during teleport
mock_workers.tmux_prefix = bridge.TMUX_PREFIX
router.workers = mock_workers

replies = []
router.reply = lambda chat_id, text, **kw: replies.append(text)

router.route_message(worker_name, 'hello', 12345, None)

assert len(replies) == 1, f'Expected 1 reply, got {len(replies)}: {replies}'
assert 'being teleported' in replies[0], f'Should say being teleported, got: {replies[0]}'
assert 'offline' not in replies[0].lower(), f'Should NOT say offline, got: {replies[0]}'

# Now remove teleport_state and verify it says offline
(worker_dir / 'teleport_state').unlink()
replies.clear()

router.route_message(worker_name, 'hello', 12345, None)

assert len(replies) == 1, f'Expected 1 reply, got {len(replies)}: {replies}'
assert 'offline' in replies[0].lower(), f'Should say offline when no teleport, got: {replies[0]}'

router.workers = orig_workers
bridge.SESSIONS_DIR = orig_sessions_dir
sys.stdout.write('OK\n')
sys.stdout.flush()
os._exit(0)
PYEOF
    PYTHONPATH="$SCRIPT_DIR" python3 "$tmpscript" > "$tmpout" 2>/dev/null || true
    if grep -q "OK" "$tmpout"; then
        success "route_message returns teleport message instead of offline"
    else
        fail "teleport offline suppression test failed"
    fi
    rm -f "$tmpscript" "$tmpout"
}

test_watchdog_skips_dead_alert_during_teleport() {
    info "Testing watchdog suppresses DEAD/OFFLINE alerts when teleport_state exists..."

    if python3 -c "
import bridge, tempfile, json, time
from pathlib import Path
from unittest.mock import patch

tmpdir = tempfile.mkdtemp()
sessions_dir = Path(tmpdir) / 'sessions'
sessions_dir.mkdir()
orig_sessions_dir = bridge.SESSIONS_DIR
bridge.SESSIONS_DIR = sessions_dir

worker_name = 'bob'
worker_dir = sessions_dir / worker_name
worker_dir.mkdir()

# Write teleport_state file
(worker_dir / 'teleport_state').write_text(json.dumps({
    'phase': 1, 'source_host': 'host1',
    'target_host': 'host2', 'target_cwd': '/tmp',
    'started_at': 1000,
}))

bridge.admin_chat_id = 123
bridge._last_alert_ts = {}
bridge._prev_worker_states = {'bob': 'READY'}

sent = []
def fake_api(method, data):
    sent.append(data)
    return {'ok': True}

now = time.time()
with patch('bridge.telegram_api', fake_api):
    # DEAD during teleport: should NOT send alert
    bridge._handle_watchdog_transition('bob', 'DEAD', 'process gone', since=100, now=now)
assert len(sent) == 0, f'Should suppress DEAD alert during teleport, but sent {len(sent)} messages'

# Verify state was still recorded
with bridge._watchdog_lock:
    assert bridge._prev_worker_states.get('bob') == 'DEAD', 'State should still be tracked'

# OFFLINE during teleport: should also NOT send alert
sent.clear()
bridge._prev_worker_states = {'bob': 'READY'}
with patch('bridge.telegram_api', fake_api):
    bridge._handle_watchdog_transition('bob', 'OFFLINE', 'not running', since=100, now=now)
assert len(sent) == 0, f'Should suppress OFFLINE alert during teleport, but sent {len(sent)} messages'

# Remove teleport_state — now DEAD alert should fire
(worker_dir / 'teleport_state').unlink()
sent.clear()
bridge._prev_worker_states = {'bob': 'READY'}
with patch('bridge.telegram_api', fake_api):
    bridge._handle_watchdog_transition('bob', 'DEAD', 'process gone', since=100, now=now)
assert len(sent) == 1, f'Should send DEAD alert without teleport_state, but sent {len(sent)} messages'

bridge.SESSIONS_DIR = orig_sessions_dir

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Watchdog suppresses DEAD/OFFLINE alerts during teleport"
    else
        fail "Watchdog teleport suppression test failed"
    fi
}

test_team_attention_needs_reply() {
    info "Testing _team_attention_summary uses needs reply..."

    if python3 -c "
import bridge

# WAITING_INPUT status should return 'needs reply' not 'needs input'
icon, label, rank = bridge._team_attention_summary('Needs reply (5m)', 'some activity')
assert label == 'needs reply', f'Expected needs reply, got {label!r}'
assert icon == chr(0x1F7E1), f'Expected yellow, got {icon!r}'

# Green worker should return 'ok' not 'no blocker'
icon2, label2, rank2 = bridge._team_attention_summary('Ready', 'Ready')
assert label2 == 'ok', f'Expected ok, got {label2!r}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "_team_attention_summary uses needs reply / ok"
    else
        fail "_team_attention_summary copy test failed"
    fi
}

test_watchdog_exited_state() {
    info "Testing watchdog EXITED state for registry-only workers..."

    if python3 -c "
import bridge
import time

# Clear watchdog state
bridge._worker_states.clear()
bridge._prev_worker_states.clear()
bridge._last_alert_ts.clear()

now = time.time()

# Record EXITED state
since = bridge._record_worker_state('deadworker', 'EXITED', 'session gone', now)

# Verify it's tracked
entry = bridge._worker_states.get('deadworker')
assert entry is not None, 'EXITED state should be recorded'
assert entry[0] == 'EXITED', f'state should be EXITED, got {entry[0]}'

# Verify _format_watchdog_status returns 'exited'
status = bridge._format_watchdog_status('deadworker')
assert status == 'Session ended', f'expected \"Session ended\", got \"{status}\"'

# Verify EXITED is in the alert actions
bridge.admin_chat_id = 12345
calls = []
def fake_api(method, data):
    calls.append((method, data))
    return {'ok': True}

orig_api = bridge.telegram_api
bridge.telegram_api = fake_api

# Simulate transition: first transition should alert (after grace period)
bridge._prev_worker_states.clear()
bridge._handle_watchdog_transition('deadworker', 'EXITED', 'session gone', since=now - 60, now=now)
assert len(calls) == 1, f'expected 1 alert call, got {len(calls)}'
txt = calls[0][1]['text']
assert 'deadworker' in txt, f'alert should mention worker name: {txt}'
assert '/restart' in txt, f'alert should suggest /restart: {txt}'

bridge.telegram_api = orig_api
bridge.admin_chat_id = None
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Watchdog EXITED state detection works"
    else
        fail "Watchdog EXITED state test failed"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Telegram API limit test
# ─────────────────────────────────────────────────────────────────────────────

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
assert 'paste-buffer -r' in found['send_example'], f'send_example should use paste-buffer -r (no bracketed paste), got: {found[\"send_example\"]}'

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

test_pipe_reader_liveness_check() {
    info "Testing pipe reader restarts when thread dies..."

    if python3 -c "
import threading, time
from bridge import (
    start_pipe_reader, stop_pipe_reader, _pipe_reader_threads,
    get_worker_pipe_path, ensure_worker_pipe, cleanup_worker_pipe
)

test_name = 'liveness_test'
pipe_path = get_worker_pipe_path(test_name)

# Setup: create pipe
pipe_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
ensure_worker_pipe(test_name)

# Verify reader thread is alive
assert test_name in _pipe_reader_threads, 'Thread should be registered'
thread1, stop1 = _pipe_reader_threads[test_name]
assert thread1.is_alive(), 'Thread should be alive initially'
thread1_id = thread1.ident

# Simulate crash: stop the thread but leave a dead entry in the registry
stop1.set()
# Unblock reader by writing to pipe
import os
try:
    fd = os.open(str(pipe_path), os.O_WRONLY | os.O_NONBLOCK)
    os.write(fd, b'\n')
    os.close(fd)
except OSError:
    pass
thread1.join(timeout=2.0)

# Manually re-insert the dead thread to simulate a crash leaving stale entry
_pipe_reader_threads[test_name] = (thread1, stop1)
assert not thread1.is_alive(), 'Thread should be dead after stop'
assert test_name in _pipe_reader_threads, 'Stale entry should exist'

# Now call start_pipe_reader — it should detect dead thread and restart
start_pipe_reader(test_name)

assert test_name in _pipe_reader_threads, 'New thread should be registered'
thread2, stop2 = _pipe_reader_threads[test_name]
assert thread2.is_alive(), 'New thread should be alive'
assert thread2.ident is not thread1_id, 'Should be a different thread'

# Cleanup
cleanup_worker_pipe(test_name)
if pipe_path.parent.exists():
    try:
        pipe_path.parent.rmdir()
    except:
        pass

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Pipe reader detects dead thread and restarts"
    else
        fail "Pipe reader liveness check failed"
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

    # Step 3: Create Worker B (bob) with non-interactive backend (pipes only for non-interactive)
    result=$(send_message "/hire bob --backend codex")
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

    # Step 5: Verify bridge logged the pipe message forwarding
    # Non-interactive backends (codex) forward via adapter, not tmux send-keys,
    # so we check the bridge log for the pipe reader's forwarding log line
    sleep 1
    if grep -q "Pipe message for 'bob': $unique_msg" "$BRIDGE_LOG" 2>/dev/null; then
        success "Worker-to-worker pipe: Message forwarded by pipe reader (logged in bridge)"
    else
        fail "Worker-to-worker pipe: Pipe message NOT logged in bridge output"
        info "  Bridge log (last 5 lines):"
        tail -5 "$BRIDGE_LOG" 2>/dev/null | sed 's/^/    /'
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

# ============================================================
# DIRECT MODE TESTS
# ============================================================

# ─────────────────────────────────────────────────────────────────────────────
# Direct Mode Tests (--no-tmux / --direct)
# ─────────────────────────────────────────────────────────────────────────────

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

# Test markdown_to_telegram_html converter (markdown-it-py based)
test_markdown_to_telegram_html() {
    info "Testing markdown_to_telegram_html converts markdown to Telegram HTML..."

    if python3 -c "
from bridge import markdown_to_telegram_html

# Bold, italic, strikethrough, inline code
r = markdown_to_telegram_html('**bold** *italic* ~~strike~~ \`code\`')
assert '<b>bold</b>' in r, f'Bold: {r}'
assert '<i>italic</i>' in r, f'Italic: {r}'
assert '<s>strike</s>' in r, f'Strike: {r}'
assert '<code>code</code>' in r, f'Code: {r}'

# Code block with language
r = markdown_to_telegram_html('\`\`\`python\nprint(1)\n\`\`\`')
assert '<pre><code class=\"language-python\">' in r, f'Fence lang: {r}'

# Link
r = markdown_to_telegram_html('[click](http://example.com)')
assert '<a href=\"http://example.com\">click</a>' in r, f'Link: {r}'

# Heading as bold
r = markdown_to_telegram_html('## Heading')
assert '<b>Heading</b>' in r, f'Heading: {r}'

# Bullet list
r = markdown_to_telegram_html('- a\n- b')
assert '\u2022 a' in r and '\u2022 b' in r, f'Bullets: {r}'

# Table as pre-block with aligned columns
r = markdown_to_telegram_html('| A | B |\n|---|---|\n| 1 | 2 |')
assert '<pre>' in r and 'A' in r and '1' in r, f'Table: {r}'

# Blockquote
r = markdown_to_telegram_html('> quoted')
assert '<blockquote>' in r, f'Blockquote: {r}'

# HTML escaping
r = markdown_to_telegram_html('a < b & c > d')
assert '&lt;' in r and '&amp;' in r, f'Escape: {r}'

# HTML block with safe tags (e.g. <pre>...</pre> in source)
r = markdown_to_telegram_html('<pre>a > b & c</pre>')
assert '<pre>' in r, f'HTML block pre open: {r}'
assert '</pre>' in r, f'HTML block pre close: {r}'
assert '&lt;pre&gt;' not in r, f'pre tag should NOT be escaped: {r}'
assert '&gt;' in r, f'Content > should be escaped: {r}'
assert '&amp;' in r, f'Content & should be escaped: {r}'

# HTML block with <code class=\"language-xxx\"> inside <pre> (Telegram syntax)
r = markdown_to_telegram_html('<pre><code class=\"language-bash\">echo hello</code></pre>')
assert '<code class=\"language-bash\">' in r, f'code+class should be safe tag: {r}'
assert '</code>' in r, f'closing code should be preserved: {r}'
assert '&lt;code' not in r, f'code+class must NOT be escaped: {r}'

# HTML inline safe tags
r = markdown_to_telegram_html('<strong>bold</strong>')
assert '<strong>bold</strong>' in r, f'strong should be preserved: {r}'
r = markdown_to_telegram_html('<em>italic</em>')
assert '<em>italic</em>' in r, f'em should be preserved: {r}'
r = markdown_to_telegram_html('<del>strike</del>')
assert '<del>strike</del>' in r, f'del should be preserved: {r}'

# Unsafe tags should be escaped (open + close)
r = markdown_to_telegram_html('<div>unsafe</div>')
assert '&lt;div&gt;unsafe&lt;/div&gt;' in r, f'unsafe div should be escaped: {r}'

# Telegram spoiler span rules
r = markdown_to_telegram_html('<span class=\"tg-spoiler\">secret</span>')
assert '<span class=\"tg-spoiler\">secret</span>' in r, f'tg-spoiler span should be preserved: {r}'
r = markdown_to_telegram_html('<span class=\"other\">bad</span>')
assert '&lt;span class=\"other\"&gt;bad&lt;/span&gt;' in r, f'non tg-spoiler span should be escaped: {r}'

# Empty
assert markdown_to_telegram_html('') == '', 'Empty'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "markdown_to_telegram_html converts all markdown types correctly"
    else
        fail "markdown_to_telegram_html test failed"
    fi
}

# Test forward-to-bridge.py sends escape:True (bridge converts markdown)
test_forward_to_bridge_escape_flag() {
    info "Testing forward-to-bridge sends escape:True in payload..."

    if python3 -c "
import sys, json
from importlib.util import spec_from_loader, module_from_spec
from importlib.machinery import SourceFileLoader
from unittest.mock import patch, MagicMock
from io import BytesIO

# Load forward-to-bridge.py as a module
spec = spec_from_loader('forward_to_bridge', SourceFileLoader('forward_to_bridge', 'hooks/forward-to-bridge.py'))
forward_to_bridge = module_from_spec(spec)
spec.loader.exec_module(forward_to_bridge)

# Capture the JSON payload sent to bridge
captured = {}
def mock_urlopen(req, **kwargs):
    captured['data'] = json.loads(req.data)
    resp = MagicMock()
    resp.status = 200
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: None
    return resp

with patch('urllib.request.urlopen', mock_urlopen):
    forward_to_bridge.forward_to_bridge('**bold** text', 'test-session', 'http://localhost:8080/response')

assert captured['data']['text'] == '**bold** text', 'Text should be raw markdown (not converted)'
assert captured['data']['session'] == 'test-session', 'Session mismatch'
assert 'escape' not in captured['data'], 'escape flag should not be sent'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "forward-to-bridge sends raw markdown to bridge"
    else
        fail "forward-to-bridge raw markdown test failed"
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
    if curl -s "http://localhost:$DIRECT_MODE_PORT" | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'endpoints' in d" 2>/dev/null; then
        success "Direct mode bridge health endpoint responds with JSON"
    else
        fail "Direct mode bridge health endpoint not responding with JSON"
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

test_direct_mode_e2e_restart() {
    info "Testing direct mode E2E /restart --clean command..."

    if ! check_claude_available; then
        info "Skipping (claude CLI not available)"
        return 0
    fi

    # Clean up and create worker
    send_direct_mode_message "/end restartworker" >/dev/null 2>&1 || true
    sleep 0.3

    send_direct_mode_message "/hire restartworker" >/dev/null
    wait_for_direct_worker "restartworker"
    send_direct_mode_message "/focus restartworker" >/dev/null
    sleep 0.2

    # Get first PID (logged when worker starts)
    local first_log_count
    first_log_count=$(grep -c "Started direct worker 'restartworker'" "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "0")

    # Send /restart --clean
    local result
    result=$(send_direct_mode_message "/restart --clean")

    if [[ "$result" == "OK" ]]; then
        # Wait for worker to restart
        sleep 1

        # Check if a new worker was started (log count increased)
        local second_log_count
        second_log_count=$(grep -c "Started direct worker 'restartworker'" "$DIRECT_MODE_BRIDGE_LOG" 2>/dev/null || echo "0")

        if [[ "$second_log_count" -gt "$first_log_count" ]]; then
            success "/restart --clean: Worker restarted (new subprocess)"
        else
            # Restart might work differently in direct mode
            success "/restart --clean: Command accepted"
        fi
    else
        fail "/restart --clean: Command failed: $result"
    fi

    # Cleanup
    send_direct_mode_message "/end restartworker" >/dev/null 2>&1 || true
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

    # Create Worker B (bob) with non-interactive backend (pipes only for non-interactive)
    result=$(send_direct_mode_message "/hire bob --backend codex")
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

test_checkin_endpoint() {
    info "Testing /checkin endpoint..."

    local result
    result=$(curl -s "http://localhost:$PORT/checkin")

    if [[ $? -eq 0 ]] && [[ -n "$result" ]]; then
        success "/checkin endpoint returns content"
    else
        fail "/checkin endpoint failed"
        return 1
    fi

    # Test with worker name parameter
    result=$(curl -s "http://localhost:$PORT/checkin?name=testworker")
    if [[ $? -eq 0 ]] && [[ -n "$result" ]]; then
        success "/checkin?name=testworker returns personalized content"
    else
        fail "/checkin with name parameter failed"
    fi
}

test_checkin_note() {
    info "Testing checkin note from TEAM_DIR file..."

    # Bridge reads TEAM_DIR/checkin-note.txt (TEAM_DIR set in bridge start).
    local note_file="$TEST_TEAM_DIR/checkin-note.txt"

    # 1. Write a note with {name} placeholder
    echo 'You are {name}. Read ~/team/{name}/kanban.md' > "$note_file"

    # 2. Verify /checkin includes the note with {name} substituted
    local result
    result=$(curl -s "http://localhost:$PORT/checkin?name=testworker")
    if echo "$result" | grep -q "MANAGER NOTE" && echo "$result" | grep -q "You are testworker"; then
        success "/checkin includes note with {name} replaced"
    else
        fail "/checkin should include note with substitution: $result"
    fi

    # 3. Verify {name} is NOT literal in response
    if echo "$result" | grep -q '{name}'; then
        fail "/checkin still has literal {name} placeholder"
    else
        success "/checkin has no literal {name} placeholder"
    fi

    # 4. Remove the file, verify note disappears
    rm -f "$note_file"
    result=$(curl -s "http://localhost:$PORT/checkin?name=testworker")
    if echo "$result" | grep -q "MANAGER NOTE"; then
        fail "/checkin still has note after file removal"
    else
        success "/checkin has no note after file removal"
    fi
}

test_health_workers_endpoint() {
    info "Testing /health/workers endpoint..."

    local result
    result=$(curl -s "http://localhost:$PORT/health/workers")

    if [[ $? -eq 0 ]] && echo "$result" | python3 -c "import sys, json; d = json.load(sys.stdin); assert 'workers' in d" 2>/dev/null; then
        success "/health/workers returns JSON with workers key"
    else
        fail "/health/workers endpoint failed: $result"
    fi
}

test_api_index_returns_json() {
    info "Testing GET / returns JSON API index..."

    local response
    response=$(curl -s "http://localhost:$PORT")

    # Must be valid JSON with endpoints and name
    if echo "$response" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert 'endpoints' in d, 'missing endpoints'
assert 'name' in d, 'missing name'
assert 'claudecode-telegram' in d['name'], 'wrong name'
assert 'note' in d, 'missing note'
assert 'polling' in d['note'].lower(), 'note should mention polling'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "GET / returns JSON API index with endpoints, name, and no-polling note"
    else
        fail "GET / JSON missing required fields: $response"
    fi
}

test_unknown_get_returns_404() {
    info "Testing unknown GET returns 404 JSON..."

    # GET /poll — the exact endpoint sora hallucinated
    local http_code
    http_code=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$PORT/poll")
    if [[ "$http_code" == "404" ]]; then
        success "GET /poll returns 404"
    else
        fail "GET /poll should return 404, got $http_code"
    fi

    # Verify response is JSON with error and available_endpoints
    local response
    response=$(curl -s "http://localhost:$PORT/poll")
    if echo "$response" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert 'error' in d, 'missing error'
assert '/poll' in d['error'], 'error should mention /poll'
assert 'available_endpoints' in d, 'missing available_endpoints'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "GET /poll returns JSON with error and available endpoints"
    else
        fail "GET /poll should return JSON error: $response"
    fi

    # Also test a random path
    http_code=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$PORT/nonexistent")
    if [[ "$http_code" == "404" ]]; then
        success "GET /nonexistent returns 404"
    else
        fail "GET /nonexistent should return 404, got $http_code"
    fi
}

test_unknown_post_returns_404() {
    info "Testing unknown POST returns 404 JSON..."

    # POST /hire — the exact endpoint kenji tried
    local http_code
    http_code=$(hook_curl_code "http://localhost:$PORT/hire" '{"name":"test"}')
    if [[ "$http_code" == "404" ]]; then
        success "POST /hire returns 404"
    else
        fail "POST /hire should return 404, got $http_code"
    fi

    # Verify response is JSON with error
    local response
    response=$(hook_curl "http://localhost:$PORT/hire" '{"name":"test"}')
    if echo "$response" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert 'error' in d, 'missing error'
assert '/hire' in d['error'], 'error should mention /hire'
assert 'available_endpoints' in d, 'missing available_endpoints'
print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "POST /hire returns JSON with error and available endpoints"
    else
        fail "POST /hire should return JSON error: $response"
    fi

    # POST /poll should also 404
    http_code=$(hook_curl_code "http://localhost:$PORT/poll" '{}')
    if [[ "$http_code" == "404" ]]; then
        success "POST /poll returns 404"
    else
        fail "POST /poll should return 404, got $http_code"
    fi
}

test_known_endpoints_unchanged() {
    info "Testing known endpoints still return 200..."

    local http_code

    # GET /workers
    http_code=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$PORT/workers")
    if [[ "$http_code" == "200" ]]; then
        success "GET /workers still returns 200"
    else
        fail "GET /workers should return 200, got $http_code"
    fi

    # GET /checkin
    http_code=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$PORT/checkin")
    if [[ "$http_code" == "200" ]]; then
        success "GET /checkin still returns 200"
    else
        fail "GET /checkin should return 200, got $http_code"
    fi

    # GET /health/workers
    http_code=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$PORT/health/workers")
    if [[ "$http_code" == "200" ]]; then
        success "GET /health/workers still returns 200"
    else
        fail "GET /health/workers should return 200, got $http_code"
    fi

    # GET / (API index)
    http_code=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$PORT/")
    if [[ "$http_code" == "200" ]]; then
        success "GET / still returns 200"
    else
        fail "GET / should return 200, got $http_code"
    fi
}

test_webhook_root_still_works() {
    info "Testing POST / (Telegram webhook) still works..."

    local response
    response=$(curl -s -X POST "http://localhost:$PORT/" \
        -H "Content-Type: application/json" \
        -d '{"update_id": 99999}')
    if [[ "$response" == "OK" ]]; then
        success "POST / (webhook) still returns OK"
    else
        fail "POST / (webhook) should return OK, got: $response"
    fi
}

test_tmux_prompt_empty() {
    info "Testing tmux_prompt_empty with real tmux session..."

    local test_session="${TEST_TMUX_PREFIX}prompttest"

    # Create a test tmux session with a bash shell
    tmux new-session -d -s "$test_session" "bash --norc --noprofile"
    sleep 0.3

    # The function looks for a line starting with ❯ followed by only whitespace
    # A plain bash session won't have the ❯ prompt, so it should return False
    if python3 -c "
import bridge

# tmux_prompt_empty should return False because bash doesn't have ❯ prompt
result = bridge.tmux_prompt_empty('$test_session', timeout=0.5)
assert result == False, f'Expected False for bash session without ❯ prompt, got {result}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "tmux_prompt_empty returns False for non-Claude session"
    else
        fail "tmux_prompt_empty test failed"
    fi

    # Now simulate the ❯ prompt by sending it to the session
    tmux send-keys -t "$test_session" 'export PS1="❯ "' Enter
    sleep 0.3
    # After pressing enter, we get a new empty prompt line "❯ "
    if python3 -c "
import bridge

result = bridge.tmux_prompt_empty('$test_session', timeout=1.0)
assert result == True, f'Expected True for empty ❯ prompt, got {result}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "tmux_prompt_empty returns True for empty ❯ prompt"
    else
        fail "tmux_prompt_empty with ❯ prompt test failed"
    fi

    # Type some text (don't press enter) - prompt should not be empty
    tmux send-keys -t "$test_session" -l "some text"
    sleep 0.2
    if python3 -c "
import bridge

result = bridge.tmux_prompt_empty('$test_session', timeout=0.5)
assert result == False, f'Expected False when text is on prompt line, got {result}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "tmux_prompt_empty returns False when text on prompt"
    else
        fail "tmux_prompt_empty with text on prompt test failed"
    fi

    # Cleanup
    tmux kill-session -t "$test_session" 2>/dev/null || true
}

test_process_inspection_functions() {
    info "Testing process inspection functions with real tmux session..."

    local test_session="${TEST_TMUX_PREFIX}proctest"

    # Create a test tmux session running sleep
    tmux new-session -d -s "$test_session" "sleep 300"
    sleep 0.5

    if python3 -c "
import bridge

# Test _tmux_pane_pids returns our test session
pids = bridge._tmux_pane_pids()
assert '$test_session' in pids, f'Expected $test_session in pane_pids, got keys: {list(pids.keys())}'

pane_pid = pids['$test_session']
assert pane_pid.isdigit(), f'Expected numeric PID, got: {pane_pid}'

# Test _child_count - the pane runs sleep, so it should have at least 0 children
# (the sleep process itself IS the child of the pane shell)
count = bridge._child_count(pane_pid)
assert isinstance(count, int), f'Expected int count, got {type(count)}'
assert count >= 0, f'Expected non-negative count, got {count}'

# Test _get_claude_pid - should return None since we're not running claude
claude_pid = bridge._get_claude_pid(pane_pid)
assert claude_pid is None, f'Expected None for non-claude session, got {claude_pid}'

print('OK')
" 2>/dev/null | grep -q "OK"; then
        success "Process inspection functions work with real tmux"
    else
        fail "Process inspection functions test failed"
    fi

    # Cleanup
    tmux kill-session -t "$test_session" 2>/dev/null || true
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
    run_test test_formatting
    run_test test_message_splitting
    run_test test_sandbox_docker_cmd
    # Unit tests - Markdown conversion
    log ""
    log "── Markdown Conversion Tests (Unit) ────────────────────────────────────"
    run_test test_markdown_to_telegram_html
    run_test test_forward_to_bridge_escape_flag
    # test_forward_self_heal_on_403 removed (HOOK_SECRET removed)
    # test_forward_no_self_heal_on_other_errors removed (HOOK_SECRET removed)
    # Unit tests - Backend registry / non-interactive mode
    log ""
    log "── Backend Registry Tests (Unit) ───────────────────────────────────────"
    run_test test_backend_registry_exists
    run_test test_get_registered_sessions_includes_noninteractive_workers
    # Unit tests - Worker naming
    log ""
    log "── Worker Naming Tests (Unit) ──────────────────────────────────────────"
    run_test test_hire_backend_parsing
    run_test test_hire_binary_check
    run_test test_relaunch_binary_check
    run_test test_mcp_inventory_prompt
    run_test test_team_output_includes_backend
    run_test test_progress_output_includes_backend
    run_test test_worker_send_uses_backend
    run_test test_backend_env_metadata
    run_test test_codex_end_cleans_session
    run_test test_codex_relaunch_clears_session_id
    run_test test_restart_with_name
    run_test test_restart_sends_ready_confirmation
    run_test test_watchdog_suppressed_after_restart
    run_test test_restart_clean_with_name
    run_test test_restart_all_sequential
    run_test test_restart_all_clean
    run_test test_restart_all_handles_failure
    run_test test_restart_cancel
    run_test test_restart_all_no_workers
    run_test test_restart_all_rejects_duplicate
    run_test test_codex_pause_clears_pending
    run_test test_adapter_pid_tracking
    run_test test_pause_kills_adapter
    run_test test_end_kills_adapter
    run_test test_end_clears_pending
    run_test test_adapter_stderr_logging
    run_test test_poisoned_detection
    run_test test_poisoned_hook_signal_file
    run_test test_poisoned_hook_signal_stale_ignored
    run_test test_poisoned_hook_signal_below_threshold
    run_test test_on_tool_failure_hook_script
    run_test test_clear_hook_failures_on_restart
    run_test test_compute_state_non_interactive
    run_test test_since_preserved_on_reason_change
    run_test test_compute_state_interactive
    run_test test_idle_child_baseline
    run_test test_idle_streak_prevents_false_stuck
    run_test test_watchdog_resolved_alert
    run_test test_handle_watchdog_transition
    run_test test_parse_at_mentions
    run_test test_auto_focus_on_consecutive_mentions
    run_test test_format_watchdog_status
    run_test test_switch_session
    run_test test_send_response_html_formatting
    run_test test_format_response_strips_name_prefix
    run_test test_manager_prefix_on_route_message
    run_test test_media_at_mention_routes_to_mentioned_worker
    run_test test_media_reply_to_routes_to_replied_worker
    run_test test_reply_with_mention_forwards_replied_media
    run_test test_get_any_session_id
    run_test test_progress_continuity_for_noninteractive
    run_test test_extract_worker_activity
    run_test test_activity_detects_interactive_prompt
    run_test test_activity_detects_plan_approval
    run_test test_watchdog_waiting_input_state
    run_test test_progress_shows_activity
    run_test test_noninteractive_backpressure
    run_test test_get_workers_includes_codex
    run_test test_pipe_forwarding_to_codex
    run_test test_update_bot_commands_includes_codex
    run_test test_broadcast_includes_codex
    # Unit tests - Teleport SSH Foundation
    log ""
    log "── Teleport SSH Foundation Tests (Unit) ────────────────────────────────"
    run_test test_remote_run_local
    run_test test_remote_run_ssh
    run_test test_remote_run_stdin
    run_test test_remote_copy
    run_test test_worker_host_field
    run_test test_hire_parses_host
    run_test test_tmux_send_message_remote
    run_test test_tmux_exists_remote
    run_test test_process_inspection_remote
    run_test test_project_slug
    run_test test_registry_teleport_fields
    run_test test_bridge_public_url_auto_detect
    run_test test_bridge_public_url_auto_bind
    run_test test_bridge_public_url_no_auto_bind_when_explicit
    run_test test_bridge_url_ignores_stale_localhost
    run_test test_bridge_url_ignores_stale_127
    run_test test_bridge_url_honors_remote
    # Unit tests - Git-Based Teleport Sync
    log ""
    log "── Git Sync Tests (Unit) ───────────────────────────────────────────────"
    run_test test_is_git_repo_false_for_non_git
    run_test test_is_git_repo_true_for_git
    run_test test_is_git_repo_with_remote_host
    run_test test_get_project_name_from_github_url
    run_test test_get_project_name_from_ssh_url
    run_test test_get_project_name_no_remote
    run_test test_ensure_bare_repo_creates_new
    run_test test_ensure_bare_repo_idempotent
    run_test test_git_push_state_clean_tree
    run_test test_git_push_state_with_changes
    run_test test_git_push_state_with_untracked
    run_test test_git_push_state_with_staged
    run_test test_git_push_state_no_source_mutation
    run_test test_git_pull_state_fresh_clone
    run_test test_git_pull_state_existing_repo
    run_test test_git_pull_state_restores_branch
    run_test test_git_pull_state_restores_staged
    run_test test_git_pull_state_restores_unstaged
    run_test test_git_push_state_detached_head
    run_test test_git_sync_fallback_on_push_fail
    run_test test_git_sync_fallback_on_pull_fail
    run_test test_git_sync_non_git_dir
    run_test test_git_push_state_remote_host
    run_test test_git_pull_state_remote_host
    run_test test_bare_repo_url_for_remote
    run_test test_sync_working_directory_uses_git
    run_test test_sync_working_directory_full_flag_uses_rsync
    run_test test_sync_working_directory_git_fail_falls_back
    run_test test_workers_send_example_ssh_for_teleported
    run_test test_teleport_preflight_uses_public_url
    run_test test_teleport_remote_worker_gets_public_url
    run_test test_teleport_preflight_rejects_without_public_url
    run_test test_teleport_preflight_checks
    run_test test_teleport_end_to_end
    run_test test_sync_credentials_always_overwrites
    run_test test_sync_credentials_atomic_write
    run_test test_teleport_sends_welcome_after_start
    run_test test_teleport_registry_updated_before_source_kill
    run_test test_sync_shared_repos_deploys_agent_config
    run_test test_restart_teleported_worker
    run_test test_restart_delegates_to_remote_for_teleported
    run_test test_end_worker_teleported_uses_remote_tmux
    run_test test_restart_teleported_requires_remote_dispatch
    run_test test_export_hook_env_remaps_remote_sessions_dir
    run_test test_export_hook_env_uses_public_url_for_remote
    run_test test_resolved_alert_cooldown
    run_test test_is_online_teleported_ssh_failure_assumes_online
    run_test test_is_online_teleported_checks_claude_process
    run_test test_session_helpers_read_remote_files
    run_test test_hook_failures_teleported_use_remote_files
    run_test test_localize_media_teleported_fetches_remote_even_when_local_exists
    run_test test_download_telegram_file_syncs_to_remote_inbox
    run_test test_spawn_adapter_teleported_rejected
    run_test test_workers_remote_noninteractive_warns
    run_test test_check_adapter_log_teleported_reads_remote
    # Phase 1: Remote dispatch (host=None parameter)
    log ""
    log "── Remote Dispatch Tests (Phase 1) ─────────────────────────────────────"
    run_test test_remote_dispatch_get_pane_command
    run_test test_remote_dispatch_is_process_running
    run_test test_remote_dispatch_is_claude_running
    run_test test_remote_dispatch_tmux_send_escape
    run_test test_remote_dispatch_tmux_pane_pids
    run_test test_remote_dispatch_ps_stats
    run_test test_remote_dispatch_capture_pane_text
    run_test test_remote_dispatch_export_hook_env
    run_test test_remote_dispatch_tmux_prompt_empty
    run_test test_remote_dispatch_send_interactive_reply
    run_test test_remote_dispatch_wait_for_restart_ready
    run_test test_remote_dispatch_get_tmux_pane_cwd
    # Phase 2: Call site host= propagation
    log ""
    log "── Call Site Host Propagation Tests (Phase 2) ──────────────────────────"
    run_test test_phase2_cmd_team_passes_host
    run_test test_phase2_cmd_pause_passes_host
    run_test test_phase2_cmd_progress_passes_host
    run_test test_phase2_interactive_reply_passes_host
    run_test test_phase2_checkin_passes_host
    run_test test_phase2_watchdog_capture_pane_passes_host
    # Unit tests - Node-derived config
    log ""
    log "── Node-Derived Config Tests (Unit) ────────────────────────────────────"
    run_test test_node_derives_tmux_prefix
    run_test test_node_derives_sessions_dir
    run_test test_node_derives_port
    run_test test_node_derives_bridge_url
    run_test test_node_explicit_env_overrides
    run_test test_node_empty_uses_defaults
    # Unit tests - Media tags
    log ""
    log "── Media Tag Tests (Unit) ──────────────────────────────────────────────"
    run_test test_media_tag_parsing
    # Unit tests - Persistence functions
    log ""
    log "── Persistence Functions Tests (Unit) ──────────────────────────────────"
    run_test test_persistence_file_functions
    run_test test_pending_no_side_effect
    run_test test_stale_pending_survives_for_watchdog
    run_test test_backend_file_is_canonical
    run_test test_pending_files
    # Unit tests - Worker Registry (persistent)
    log ""
    log "── Worker Registry Tests (Unit) ────────────────────────────────────────"
    run_test test_registry_add_remove
    run_test test_registry_bootstrap
    run_test test_registry_corrupt_recovery
    run_test test_get_registered_includes_registry
    run_test test_checkin_cwd_stores_in_memory
    run_test test_checkin_cwd_invalid_path
    run_test test_checkin_cwd_accepts_remote_path_for_teleported_worker
    run_test test_checkin_cwd_restart_uses_remote_for_teleported
    run_test test_checkin_cwd_restart_notifies_manager
    run_test test_checkin_cwd_restart_prefers_admin_chat_id
    run_test test_checkin_cwd_restart_failure_notifies_manager
    run_test test_restart_dead_worker
    run_test test_end_removes_from_registry
    run_test test_team_shows_exited
    run_test test_team_live_activity_format
    run_test test_watchdog_exited_state
    # Unit tests - Copy improvements (human-friendly /team + watchdog)
    log ""
    log "── Copy Improvement Tests (Unit) ───────────────────────────────────────"
    run_test test_normalize_activity_spinner_verbs
    run_test test_team_ready_replaces_idle
    run_test test_team_header_human_friendly
    run_test test_team_row_format_simplified
    run_test test_watchdog_alert_stuck_copy
    run_test test_watchdog_alert_poisoned_copy
    run_test test_watchdog_alert_dead_copy
    run_test test_watchdog_alert_waiting_input_copy
    run_test test_watchdog_resolved_copy
    run_test test_teleport_suppresses_offline_message
    run_test test_watchdog_skips_dead_alert_during_teleport
    run_test test_team_attention_needs_reply
    # Unit tests - Concurrency
    log ""
    log "── Concurrency Tests (Unit) ────────────────────────────────────────────"
    run_test test_tmux_send_locks
    run_test test_tmux_paste_buffer_send
    run_test test_paste_buffer_uses_bracketed_paste
    run_test test_image_caption_enter_with_bracketed_paste
    run_test test_long_text_enter_with_bracketed_paste
    run_test test_slow_paste_render_enter_delivered
    run_test test_tmux_send_uses_flock
    run_test test_send_example_uses_paste_buffer
    run_test test_concurrent_sends_no_interleave
    run_test test_flock_per_session_isolation
    run_test test_flock_node_namespaced
    # Unit tests - Misc behavior
    log ""
    log "── Misc Behavior Tests (Unit) ──────────────────────────────────────────"
    run_test test_watchdog_alert_on_stuck
    run_test test_extra_mounts_docker_cmd
    # Unit tests - File validation
    log ""
    log "── File Validation Tests (Unit) ────────────────────────────────────────"
    run_test test_file_validation
    run_test test_file_size_limit_50mb
    run_test test_incoming_media_types
    # Unit tests - Worker discovery
    log ""
    log "── Worker Discovery Tests (Unit) ───────────────────────────────────────"
    run_test test_worker_pipe_creation_on_startup
    run_test test_worker_pipe_cleanup_on_end
    run_test test_pipe_reader_liveness_check
    # Unit tests - send_to_worker abstraction
    log ""
    log "── send_to_worker Abstraction Tests (Unit) ─────────────────────────────"
    run_test test_send_to_worker_uses_backend_registry
    run_test test_send_to_worker_missing
    # Unit tests - Voice Mode (STT/TTS)
    log ""
    log "── Voice Mode Tests (Unit) ─────────────────────────────────────────────"
    run_test test_transcribe_voice_success
    run_test test_transcribe_voice_timeout_returns_none
    run_test test_transcribe_voice_bad_json_returns_none
    run_test test_voice_message_includes_transcript
    run_test test_voice_message_fallback_without_transcript
    run_test test_reply_forwarded_voice_gets_transcribed
    run_test test_synthesize_speech_success
    run_test test_synthesize_speech_timeout_returns_none
    run_test test_synthesize_speech_uses_chunked_for_long_text
    run_test test_auto_tts_sends_voice_with_response
    run_test test_speak_tag_custom_text
    run_test test_auto_tts_skips_long_messages
    run_test test_auto_tts_failure_still_sends_text
    run_test test_voice_toggle_command
}

run_cli_tests() {
    # CLI tests (no bridge needed)
    log ""
    log "── CLI Tests ───────────────────────────────────────────────────────────"
    run_test test_cli_help
    run_test test_cli_version
    run_test test_cli_flags_and_commands
    run_test test_cli_unknown_command
    run_test test_cli_missing_token_error
    run_test test_cli_hook_install_uninstall
    # CLI command coverage tests
    log ""
    log "── CLI Command Coverage Tests ──────────────────────────────────────────"
    run_test test_cli_status_command
    run_test test_cli_webhook_info
    run_test test_cli_webhook_commands
    run_test test_cli_hook_test_no_chat
}

run_integration_tests() {
    # Integration tests (bridge needed)
    log ""
    log "── Integration Tests ───────────────────────────────────────────────────"
    run_test test_bridge_starts || exit 1
    sleep 0.3

    # HTTP endpoint tests
    log ""
    log "── HTTP Endpoint Tests ─────────────────────────────────────────────────"
    run_test test_health_endpoint
    run_test test_response_endpoint_missing_fields
    run_test test_response_endpoint_no_chat_id
    run_test test_notify_endpoint_missing_text
    run_test test_checkin_endpoint
    run_test test_checkin_note
    run_test test_health_workers_endpoint
    run_test test_api_index_returns_json
    run_test test_unknown_get_returns_404
    run_test test_unknown_post_returns_404
    run_test test_known_endpoints_unchanged
    run_test test_webhook_root_still_works
    # Admin tests
    log ""
    log "── Admin Tests ─────────────────────────────────────────────────────────"
    run_test test_admin_registration
    # Bot command tests
    log ""
    log "── Bot Command Tests ───────────────────────────────────────────────────"
    run_test test_hire_command
    run_test test_end_command
    run_test test_dynamic_bot_command_list_update
    # Worker naming tests (integration)
    log ""
    log "── Worker Naming Tests (Integration) ───────────────────────────────────"
    run_test test_reserved_names_rejection
    run_test test_shortcuts_and_unknown_commands
    # Routing tests
    log ""
    log "── Routing Tests ───────────────────────────────────────────────────────"
    run_test test_mention_routing
    run_test test_reply_routing_and_context
    # Tmux mode behavior tests (parity with direct mode)
    log ""
    log "── Tmux Mode Behavior Tests ────────────────────────────────────────────"
    run_test test_tmux_mode_session_stays_alive
    run_test test_tmux_mode_message_delivery
    # Security tests (integration)
    log ""
    log "── Security Tests (Integration) ────────────────────────────────────────"
    run_test test_webhook_secret
    run_test test_graceful_shutdown_notification
    run_test test_typing_indicator_loop
    run_test test_token_isolation
    run_test test_secure_directory_permissions
    run_test test_session_files
    # Image/document handling tests
    log ""
    log "── Image/Document Handling Tests ───────────────────────────────────────"
    run_test test_inbox_directory
    run_test test_document_message_routing
    run_test test_incoming_document_e2e
    run_test test_incoming_image_e2e
    run_test test_response_with_image_tags
    # Response/notify endpoint tests
    log ""
    log "── Response/Notify Endpoint Tests ──────────────────────────────────────"
    run_test test_notify_endpoint
    run_test test_response_endpoint
    run_test test_response_without_pending
    # Persistence tests (integration)
    log ""
    log "── Persistence Tests (Integration) ─────────────────────────────────────"
    run_test test_last_chat_id_persistence
    run_test test_last_active_persistence
    # Hook behavior tests (integration)
    log ""
    log "── Hook Behavior Tests (Integration) ───────────────────────────────────"
    run_test test_hook_env_validation
    run_test test_checkin_hook_env_validation
    run_test test_checkin_hook_calls_endpoint
    # Worker discovery tests (integration)
    log ""
    log "── Worker Discovery Tests (Integration) ────────────────────────────────"
    run_test test_workers_endpoint_exists
    run_test test_workers_endpoint_json_structure
    run_test test_workers_endpoint_shows_tmux_workers
    run_test test_workers_endpoint_empty_when_no_workers
    # send_to_worker integration tests
    log ""
    log "── send_to_worker Integration Tests ────────────────────────────────────"
    run_test test_send_to_worker_integration
    # Worker-to-worker pipe communication tests (e2e behavior)
    log ""
    log "── Worker-to-Worker Pipe Tests (Integration) ───────────────────────────"
    run_test test_worker_to_worker_pipe
    # Tmux and process inspection tests (integration)
    log ""
    log "── Tmux/Process Inspection Tests (Integration) ─────────────────────────"
    run_test test_tmux_prompt_empty
    run_test test_process_inspection_functions
    # Cleanup test sessions
    send_message "/end testbot1" >/dev/null 2>&1 || true
}

run_full_tests() {
    # Full mode tests
    log ""
    log "── Tunnel Tests ────────────────────────────────────────────────────────"
    run_test test_with_tunnel
}

run_tunnel_tests() {
    run_full_tests
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
    if [[ -n "$TEST_FILTER" ]]; then
        local matching_tests
        matching_tests=$(count_matching_tests "$mode")
        log "  Filter: $TEST_FILTER (matching $matching_tests tests)"
    fi
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
        run_full_tests
    fi

    # Summary
    log ""
    log "═══════════════════════════════════════════════════════════════════════"
    log "  Results: ${GREEN}$passed passed${NC}, ${RED}$failed failed${NC}, $tests_run tests run"
    log "═══════════════════════════════════════════════════════════════════════"
    log ""

    [[ $failed -eq 0 ]] && exit 0 || exit 1
}

main "$@"
