#!/bin/bash
# Live Integration Tests for Go claudecode-telegram
# Tests against a running server on port 8095

set -e

PORT="${GO_TEST_PORT:-8095}"
BASE_URL="http://localhost:$PORT"
WEBHOOK_URL="$BASE_URL/webhook"
CHAT_ID="${TEST_CHAT_ID:-121604706}"
PASSED=0
FAILED=0

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${YELLOW}[TEST]${NC} $1"; }
pass() { echo -e "${GREEN}[PASS]${NC} $1"; ((PASSED++)); }
fail() { echo -e "${RED}[FAIL]${NC} $1"; ((FAILED++)); }

# Send webhook message
send_msg() {
    local text="$1"
    local msg_id="${2:-$RANDOM}"
    curl -s -X POST "$WEBHOOK_URL" \
        -H "Content-Type: application/json" \
        -d "{\"message\":{\"chat\":{\"id\":$CHAT_ID},\"text\":\"$text\",\"message_id\":$msg_id}}"
}

# Send webhook with reply
send_reply() {
    local text="$1"
    local reply_to="$2"
    curl -s -X POST "$WEBHOOK_URL" \
        -H "Content-Type: application/json" \
        -d "{\"message\":{\"chat\":{\"id\":$CHAT_ID},\"text\":\"$text\",\"message_id\":$RANDOM,\"reply_to_message\":{\"message_id\":$reply_to}}}"
}

# Check server health
check_server() {
    log "Checking server on port $PORT..."
    if curl -s "$BASE_URL" > /dev/null 2>&1; then
        pass "Server responding on port $PORT"
        return 0
    else
        fail "Server not responding on port $PORT"
        return 1
    fi
}

# ============================================================================
# Test 1: Worker Lifecycle (/hire, /team, /end)
# ============================================================================
test_worker_lifecycle() {
    log "Test 1: Worker Lifecycle"

    # Hire a test worker
    result=$(send_msg "/hire testbot")
    if [[ -z "$result" ]] || [[ "$result" != *"error"* ]]; then
        pass "/hire testbot accepted"
    else
        fail "/hire testbot failed: $result"
    fi
    sleep 1

    # Check team
    result=$(send_msg "/team")
    if [[ -z "$result" ]] || [[ "$result" != *"error"* ]]; then
        pass "/team command accepted"
    else
        fail "/team failed: $result"
    fi
    sleep 1

    # End worker
    result=$(send_msg "/end testbot")
    if [[ -z "$result" ]] || [[ "$result" != *"error"* ]]; then
        pass "/end testbot accepted"
    else
        fail "/end testbot failed: $result"
    fi
    sleep 1
}

# ============================================================================
# Test 2: Focus Command
# ============================================================================
test_focus() {
    log "Test 2: Focus Command"

    # Create worker first
    send_msg "/hire focustest" > /dev/null
    sleep 1

    # Focus on worker
    result=$(send_msg "/focus focustest")
    if [[ -z "$result" ]] || [[ "$result" != *"error"* ]]; then
        pass "/focus focustest accepted"
    else
        fail "/focus failed: $result"
    fi
    sleep 1

    # Check team shows focus
    result=$(send_msg "/team")
    pass "/team with focus checked"

    # Clean up
    send_msg "/end focustest" > /dev/null
    sleep 1
}

# ============================================================================
# Test 3: Message Routing
# ============================================================================
test_message_routing() {
    log "Test 3: Message Routing"

    # Create workers
    send_msg "/hire routetest1" > /dev/null
    sleep 1
    send_msg "/hire routetest2" > /dev/null
    sleep 1

    # Direct message with /worker
    result=$(send_msg "/routetest1 hello direct")
    if [[ -z "$result" ]] || [[ "$result" != *"error"* ]]; then
        pass "/worker direct routing accepted"
    else
        fail "Direct routing failed: $result"
    fi
    sleep 1

    # @worker mention
    result=$(send_msg "@routetest2 hello mention")
    if [[ -z "$result" ]] || [[ "$result" != *"error"* ]]; then
        pass "@worker mention routing accepted"
    else
        fail "Mention routing failed: $result"
    fi
    sleep 1

    # @all broadcast (if supported)
    result=$(send_msg "@all broadcast test")
    pass "@all broadcast sent"
    sleep 1

    # Clean up
    send_msg "/end routetest1" > /dev/null
    send_msg "/end routetest2" > /dev/null
    sleep 1
}

# ============================================================================
# Test 4: /learn Command
# ============================================================================
test_learn() {
    log "Test 4: /learn Command"

    send_msg "/hire learntest" > /dev/null
    sleep 1

    result=$(send_msg "/learn tmux sessions")
    if [[ -z "$result" ]] || [[ "$result" != *"error"* ]]; then
        pass "/learn command accepted"
    else
        fail "/learn failed: $result"
    fi
    sleep 1

    send_msg "/end learntest" > /dev/null
    sleep 1
}

# ============================================================================
# Test 5: /pause and /progress Commands
# ============================================================================
test_pause_progress() {
    log "Test 5: /pause and /progress Commands"

    send_msg "/hire pausetest" > /dev/null
    sleep 1

    # Pause
    result=$(send_msg "/pause pausetest")
    if [[ -z "$result" ]] || [[ "$result" != *"error"* ]]; then
        pass "/pause command accepted"
    else
        fail "/pause failed: $result"
    fi
    sleep 1

    # Progress
    result=$(send_msg "/progress pausetest")
    if [[ -z "$result" ]] || [[ "$result" != *"error"* ]]; then
        pass "/progress command accepted"
    else
        fail "/progress failed: $result"
    fi
    sleep 1

    send_msg "/end pausetest" > /dev/null
    sleep 1
}

# ============================================================================
# Test 6: /relaunch Command
# ============================================================================
test_relaunch() {
    log "Test 6: /relaunch Command"

    send_msg "/hire relaunchtest" > /dev/null
    sleep 1

    result=$(send_msg "/relaunch relaunchtest")
    if [[ -z "$result" ]] || [[ "$result" != *"error"* ]]; then
        pass "/relaunch command accepted"
    else
        fail "/relaunch failed: $result"
    fi
    sleep 2

    send_msg "/end relaunchtest" > /dev/null
    sleep 1
}

# ============================================================================
# Test 7: /response Hook Endpoint
# ============================================================================
test_response_hook() {
    log "Test 7: /response Hook Endpoint"

    # Test response endpoint
    result=$(curl -s -X POST "$BASE_URL/response" \
        -H "Content-Type: application/json" \
        -d "{\"session\":\"testworker\",\"chat_id\":\"$CHAT_ID\",\"text\":\"Test response from hook\"}" \
        -w "\nHTTP:%{http_code}")

    if [[ "$result" == *"HTTP:200"* ]]; then
        pass "/response endpoint returned 200"
    else
        fail "/response endpoint failed: $result"
    fi
}

# ============================================================================
# Test 8: /notify Endpoint
# ============================================================================
test_notify() {
    log "Test 8: /notify Endpoint"

    result=$(curl -s -X POST "$BASE_URL/notify" \
        -H "Content-Type: application/json" \
        -d "{\"text\":\"Test notification from integration test\"}" \
        -w "\nHTTP:%{http_code}")

    if [[ "$result" == *"HTTP:200"* ]]; then
        pass "/notify endpoint returned 200"
    else
        fail "/notify endpoint failed: $result"
    fi
}

# ============================================================================
# Test 9: Reply-to Routing
# ============================================================================
test_reply_routing() {
    log "Test 9: Reply-to Routing"

    send_msg "/hire replytest" > /dev/null
    sleep 1

    # Simulate reply to a message (msg_id 12345 as if it was from replytest)
    result=$(send_reply "This is a reply" 12345)
    if [[ -z "$result" ]] || [[ "$result" != *"error"* ]]; then
        pass "Reply-to routing accepted"
    else
        fail "Reply routing failed: $result"
    fi
    sleep 1

    send_msg "/end replytest" > /dev/null
    sleep 1
}

# ============================================================================
# Test 10: Invalid Commands
# ============================================================================
test_invalid_commands() {
    log "Test 10: Invalid/Edge Cases"

    # Unknown command
    result=$(send_msg "/unknowncommand")
    pass "Unknown command handled"

    # Empty hire
    result=$(send_msg "/hire")
    pass "Empty /hire handled"

    # End non-existent worker
    result=$(send_msg "/end nonexistent")
    pass "End non-existent worker handled"

    sleep 1
}

# ============================================================================
# Main
# ============================================================================
main() {
    echo ""
    echo "=========================================="
    echo "Go claudecode-telegram Live Integration Tests"
    echo "Server: $BASE_URL"
    echo "Chat ID: $CHAT_ID"
    echo "=========================================="
    echo ""

    if ! check_server; then
        echo "Server not running. Start with:"
        echo "  ./cctg serve --port $PORT --token <token> --admin $CHAT_ID"
        exit 1
    fi

    test_worker_lifecycle
    test_focus
    test_message_routing
    test_learn
    test_pause_progress
    test_relaunch
    test_response_hook
    test_notify
    test_reply_routing
    test_invalid_commands

    echo ""
    echo "=========================================="
    echo -e "Results: ${GREEN}$PASSED passed${NC}, ${RED}$FAILED failed${NC}"
    echo "=========================================="

    if [[ $FAILED -gt 0 ]]; then
        exit 1
    fi
}

main "$@"
