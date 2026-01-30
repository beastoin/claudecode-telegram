#!/bin/bash
# Basic tests for claudecode-imessage
# Note: Full integration tests require macOS with Messages.app

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASSED=0
FAILED=0

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

pass() { ((PASSED++)) || true; echo -e "${GREEN}PASS${NC} $1"; }
fail() { ((FAILED++)) || true; echo -e "${RED}FAIL${NC} $1: $2"; }

echo "claudecode-imessage tests"
echo "========================="
echo ""

# Test 1: Python imports
echo "Testing Python imports..."
if python3 -c "import sys; sys.path.insert(0, '$SCRIPT_DIR'); from imessage import db, sender, receiver, watch" 2>/dev/null; then
    pass "Python imports"
else
    fail "Python imports" "Failed to import modules"
fi

# Test 2: Bridge module imports
echo "Testing bridge imports..."
if python3 -c "import sys; sys.path.insert(0, '$SCRIPT_DIR'); import bridge" 2>/dev/null; then
    pass "Bridge imports"
else
    fail "Bridge imports" "Failed to import bridge"
fi

# Test 3: Message splitting
echo "Testing message splitting..."
SPLIT_TEST=$(python3 << EOF
import sys
sys.path.insert(0, '$SCRIPT_DIR')
from imessage.sender import split_message

# Short message - no split
result = split_message("Hello world", 100)
assert len(result) == 1, f"Expected 1 chunk, got {len(result)}"

# Long message - should split
long_text = "A" * 100 + "\n\n" + "B" * 100
result = split_message(long_text, 50)
assert len(result) > 1, f"Expected multiple chunks, got {len(result)}"

print("OK")
EOF
)
if [ "$SPLIT_TEST" = "OK" ]; then
    pass "Message splitting"
else
    fail "Message splitting" "$SPLIT_TEST"
fi

# Test 4: AppleScript escaping
echo "Testing AppleScript escaping..."
ESCAPE_TEST=$(python3 << EOF
import sys
sys.path.insert(0, '$SCRIPT_DIR')
from imessage.sender import escape_applescript_string

# Test quotes
result = escape_applescript_string('Hello "world"')
assert '\\\\"' in result, f"Quotes not escaped: {result}"

# Test backslashes
result = escape_applescript_string('path\\\\to\\\\file')
assert '\\\\\\\\' in result, f"Backslashes not escaped: {result}"

print("OK")
EOF
)
if [ "$ESCAPE_TEST" = "OK" ]; then
    pass "AppleScript escaping"
else
    fail "AppleScript escaping" "$ESCAPE_TEST"
fi

# Test 5: Apple time conversion
echo "Testing Apple time conversion..."
TIME_TEST=$(python3 << EOF
import sys
sys.path.insert(0, '$SCRIPT_DIR')
from imessage.db import apple_time_to_datetime

# Test with nanoseconds since Apple epoch (2001-01-01)
# 2024-01-01 is 23 years later = 725846400 seconds since Apple epoch
# In nanoseconds: 725846400 * 1e9 = 725846400000000000
# But the function adds APPLE_EPOCH_OFFSET internally, so we pass raw nanos

# Use a known value: 0 nanoseconds = 2001-01-01
dt = apple_time_to_datetime(0)
assert dt.year == 2001, f"Wrong year for 0: {dt.year}"

# 1 year in nanoseconds ≈ 31557600000000000
dt = apple_time_to_datetime(31557600000000000)
assert dt.year == 2002, f"Wrong year for 1 year: {dt.year}"

print("OK")
EOF
)
if [ "$TIME_TEST" = "OK" ]; then
    pass "Apple time conversion"
else
    fail "Apple time conversion" "$TIME_TEST"
fi

# Test 6: CLI help
echo "Testing CLI help..."
if "$SCRIPT_DIR/scripts/claudecode-imessage.sh" help >/dev/null 2>&1; then
    pass "CLI help"
else
    fail "CLI help" "Help command failed"
fi

# Test 7: CLI version
echo "Testing CLI version..."
VERSION_OUTPUT=$("$SCRIPT_DIR/scripts/claudecode-imessage.sh" --version 2>&1)
if [[ "$VERSION_OUTPUT" == *"0.1.0"* ]]; then
    pass "CLI version"
else
    fail "CLI version" "Unexpected output: $VERSION_OUTPUT"
fi

# Summary
echo ""
echo "========================="
echo -e "Passed: ${GREEN}$PASSED${NC}"
echo -e "Failed: ${RED}$FAILED${NC}"

if [ $FAILED -gt 0 ]; then
    exit 1
fi

echo ""
echo "Note: Integration tests require macOS with Messages.app"
