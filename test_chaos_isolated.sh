#!/usr/bin/env bash
#
# test_chaos_isolated.sh — Run chaos tests on an isolated machine via beast host
#
# Fundamental isolation: tests run as a different OS user on a different machine,
# so they physically CANNOT see or kill the prod bridge process.
#
# Usage:
#   TEST_BOT_TOKEN='...' ./test_chaos_isolated.sh                 # chaos tests only
#   TEST_BOT_TOKEN='...' TEST_FILTER='' ./test_chaos_isolated.sh  # full test suite
#
# Target machine: triassic-4 (configured in ~/.ssh/config)
# Isolated user:  bridge-test (beast host managed, 4G RAM, 200% CPU)
#
# Prerequisites:
#   - SSH access to triassic-4 as root
#   - beast host setup done on triassic-4
#   - bridge-test user created: beast host create bridge-test --mem 4G --cpu 200%
#   - Python 3.12 at /opt/beast/shared/bin/python3.12
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_HOST="${TARGET_HOST:-triassic-4}"
TARGET_USER="${TARGET_USER:-bridge-test}"
TEST_FILTER="${TEST_FILTER:-test_chaos}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

log()     { echo -e "$@"; }
info()    { log "${YELLOW}→${NC} $1"; }
success() { log "${GREEN}✓${NC} $1"; }
fail()    { log "${RED}✗${NC} $1"; }

# ── Pre-flight ───────────────────────────────────────────────────────────

if [[ -z "${TEST_BOT_TOKEN:-}" ]]; then
    fail "TEST_BOT_TOKEN not set"
    echo "  Usage: TEST_BOT_TOKEN='...' ./test_chaos_isolated.sh"
    exit 1
fi

info "Checking ${TARGET_HOST}..."
if ! ssh "$TARGET_HOST" 'true' 2>/dev/null; then
    fail "Cannot SSH to $TARGET_HOST"
    exit 1
fi

if ! ssh "$TARGET_HOST" "beast host list --output-json 2>/dev/null" | grep -q "$TARGET_USER"; then
    fail "User $TARGET_USER not found on $TARGET_HOST"
    echo "  Create it: ssh $TARGET_HOST beast host create $TARGET_USER --mem 4G --cpu 200%"
    exit 1
fi
success "Connected to ${TARGET_HOST}, user ${TARGET_USER} exists"

# ── Sync code ────────────────────────────────────────────────────────────

TARGET_HOME="/data/beast/home-store/${TARGET_USER}"
TARGET_DIR="${TARGET_HOME}/claudecode-telegram"

info "Syncing project to ${TARGET_HOST}:${TARGET_DIR}..."
rsync -az --delete \
    --exclude 'node_modules' --exclude '.git' --exclude 'build' \
    --exclude 'pilot/node_modules' --exclude 'runs' --exclude 'void' \
    --exclude '__pycache__' --exclude '.pytest_cache' --exclude '.mypy_cache' \
    --exclude '.venv' --exclude '.claude' \
    "$SCRIPT_DIR/" "${TARGET_HOST}:${TARGET_DIR}/" 2>&1
ssh "$TARGET_HOST" "chown -R ${TARGET_USER}:agents ${TARGET_DIR}/" 2>&1
success "Code synced"

# ── Ensure venv ──────────────────────────────────────────────────────────

info "Checking Python venv..."
ssh "$TARGET_HOST" "beast host exec ${TARGET_USER} \
    --env PATH=/opt/beast/shared/bin:/usr/local/bin:/usr/bin:/bin \
    --env HOME=${TARGET_HOME} \
    -- bash -c '
        cd ~/claudecode-telegram
        if [ ! -f .venv/bin/python3 ]; then
            python3.12 -m venv .venv
            .venv/bin/pip install pytest requests --quiet 2>&1
        fi
        .venv/bin/python3 --version
    '" 2>&1 | grep -v 'Running as unit\|Finished with\|Main processes\|Service runtime\|CPU time'
success "Python venv ready"

# ── Run tests ────────────────────────────────────────────────────────────

log ""
log "═══════════════════════════════════════════════════════════════════════"
log "  Isolated Chaos Tests"
log "  Machine: ${TARGET_HOST}  User: ${TARGET_USER}  Filter: ${TEST_FILTER:-all}"
log "  Isolation: different machine + different OS user (beast host)"
log "═══════════════════════════════════════════════════════════════════════"
log ""

EXIT_CODE=0
ssh "$TARGET_HOST" "beast host exec ${TARGET_USER} \
    --env PATH=/opt/beast/shared/bin:/usr/local/bin:/usr/bin:/bin \
    --env TEST_BOT_TOKEN=${TEST_BOT_TOKEN} \
    --env TEST_CHAT_ID=${TEST_CHAT_ID:-121604706} \
    --env TEST_FILTER=${TEST_FILTER} \
    --env HOME=${TARGET_HOME} \
    --env PYTHON=${TARGET_DIR}/.venv/bin/python3 \
    -- timeout 300 bash -c '
        cd ~/claudecode-telegram
        mkdir -p ~/.claude/telegram/nodes/test/sessions ~/.claude/telegram/nodes/test/team
        chmod 700 ~/.claude/telegram/nodes/test ~/.claude/telegram/nodes/test/sessions ~/.claude/telegram/nodes/test/team
        ./test.sh 2>&1
    '" 2>&1 | grep -v 'Running as unit\|Finished with\|Main processes\|Service runtime\|CPU time\|beast host exec' || EXIT_CODE=$?

log ""
log "═══════════════════════════════════════════════════════════════════════"
if [[ $EXIT_CODE -eq 0 ]]; then
    log "  ${GREEN}PASSED — all tests passed in OS-level isolation${NC}"
else
    log "  ${RED}FAILED — exit code: ${EXIT_CODE}${NC}"
fi
log "═══════════════════════════════════════════════════════════════════════"

exit $EXIT_CODE
