#!/bin/bash
# claudecode-imessage CLI
# Manage the iMessage bridge for Claude Code workers.
#
# Usage:
#   ./claudecode-imessage.sh run          Start the bridge
#   ./claudecode-imessage.sh stop         Stop the bridge
#   ./claudecode-imessage.sh status       Show status
#   ./claudecode-imessage.sh hook install Install Claude stop hook
#   ./claudecode-imessage.sh hook remove  Remove Claude stop hook

set -euo pipefail

VERSION="0.1.0"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Configuration
PORT="${IMESSAGE_BRIDGE_PORT:-8083}"
SESSIONS_DIR="${SESSIONS_DIR:-$HOME/.claude/imessage/sessions}"
TMUX_PREFIX="${TMUX_PREFIX:-claude-imessage-}"
PID_FILE="$HOME/.claude/imessage/bridge.pid"
LOG_FILE="$HOME/.claude/imessage/bridge.log"

# Claude Code hook paths
CLAUDE_HOOKS_DIR="$HOME/.claude/hooks"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"
HOOK_NAME="send-to-imessage.sh"

# Colors (if terminal supports)
if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    NC=''
fi

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

usage() {
    cat << EOF
claudecode-imessage v${VERSION}

Usage: $(basename "$0") <command>

Commands:
  run           Start the bridge (foreground)
  start         Start the bridge (background)
  stop          Stop the bridge
  restart       Restart the bridge
  status        Show status
  hook install  Install Claude Code stop hook
  hook remove   Remove Claude Code stop hook
  help          Show this help

Environment:
  IMESSAGE_BRIDGE_PORT    Bridge port (default: 8083)
  IMESSAGE_ALLOWED_HANDLES  Comma-separated allowed senders
  IMESSAGE_POLL_INTERVAL  Seconds between polls (default: 2)
  IMESSAGE_AUTO_LEARN_ADMIN  Auto-learn first sender (0/1)

Example:
  ./claudecode-imessage.sh hook install
  ./claudecode-imessage.sh run

EOF
}

ensure_dirs() {
    mkdir -p "$(dirname "$PID_FILE")"
    mkdir -p "$SESSIONS_DIR"
    chmod 700 "$(dirname "$PID_FILE")"
    chmod 700 "$SESSIONS_DIR"
}

is_running() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

cmd_run() {
    ensure_dirs

    if is_running; then
        log_error "Bridge is already running (PID: $(cat "$PID_FILE"))"
        exit 1
    fi

    # Check port
    if lsof -ti ":$PORT" >/dev/null 2>&1; then
        log_error "Port $PORT is already in use"
        exit 1
    fi

    log_info "Starting claudecode-imessage v${VERSION} on port $PORT"

    # Export config
    export PORT SESSIONS_DIR TMUX_PREFIX

    # Run bridge
    exec python3 "$SCRIPT_DIR/bridge.py"
}

cmd_start() {
    ensure_dirs

    if is_running; then
        log_error "Bridge is already running (PID: $(cat "$PID_FILE"))"
        exit 1
    fi

    # Check port
    if lsof -ti ":$PORT" >/dev/null 2>&1; then
        log_error "Port $PORT is already in use"
        exit 1
    fi

    log_info "Starting claudecode-imessage v${VERSION} in background"

    # Export config
    export PORT SESSIONS_DIR TMUX_PREFIX

    # Run in background
    nohup python3 "$SCRIPT_DIR/bridge.py" >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"

    sleep 1

    if is_running; then
        log_info "Bridge started (PID: $(cat "$PID_FILE"))"
        log_info "Logs: $LOG_FILE"
    else
        log_error "Bridge failed to start. Check $LOG_FILE"
        exit 1
    fi
}

cmd_stop() {
    if ! is_running; then
        log_warn "Bridge is not running"
        return 0
    fi

    local pid
    pid=$(cat "$PID_FILE")
    log_info "Stopping bridge (PID: $pid)"

    kill "$pid" 2>/dev/null || true

    # Wait for stop
    for i in {1..10}; do
        if ! kill -0 "$pid" 2>/dev/null; then
            break
        fi
        sleep 0.5
    done

    # Force kill if still running
    if kill -0 "$pid" 2>/dev/null; then
        log_warn "Force killing bridge"
        kill -9 "$pid" 2>/dev/null || true
    fi

    rm -f "$PID_FILE"
    log_info "Bridge stopped"
}

cmd_restart() {
    cmd_stop
    sleep 1
    cmd_start
}

cmd_status() {
    echo "claudecode-imessage v${VERSION}"
    echo ""

    # Bridge status
    if is_running; then
        echo -e "Bridge: ${GREEN}running${NC} (PID: $(cat "$PID_FILE"))"
    else
        echo -e "Bridge: ${RED}stopped${NC}"
    fi
    echo "Port: $PORT"
    echo "Sessions: $SESSIONS_DIR"

    # Hook status
    if [ -f "$CLAUDE_HOOKS_DIR/$HOOK_NAME" ]; then
        echo -e "Hook: ${GREEN}installed${NC}"
    else
        echo -e "Hook: ${YELLOW}not installed${NC}"
    fi

    # List sessions
    echo ""
    echo "Workers:"
    local sessions
    sessions=$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep "^${TMUX_PREFIX}" || true)
    if [ -n "$sessions" ]; then
        while IFS= read -r session; do
            local name="${session#$TMUX_PREFIX}"
            local status="idle"
            if [ -f "$SESSIONS_DIR/$name/pending" ]; then
                status="busy"
            fi
            echo "  $name ($status)"
        done <<< "$sessions"
    else
        echo "  (none)"
    fi
}

cmd_hook_install() {
    log_info "Installing Claude Code stop hook"

    # Create hooks directory
    mkdir -p "$CLAUDE_HOOKS_DIR"

    # Copy hook script
    cp "$SCRIPT_DIR/hooks/send-to-imessage.sh" "$CLAUDE_HOOKS_DIR/$HOOK_NAME"
    chmod +x "$CLAUDE_HOOKS_DIR/$HOOK_NAME"

    # Update settings.json
    if [ ! -f "$CLAUDE_SETTINGS" ]; then
        echo '{}' > "$CLAUDE_SETTINGS"
    fi

    # Add hook to settings using Python
    python3 << PYEOF
import json
import os

settings_path = os.path.expanduser("$CLAUDE_SETTINGS")
hook_path = os.path.expanduser("$CLAUDE_HOOKS_DIR/$HOOK_NAME")

with open(settings_path, 'r') as f:
    settings = json.load(f)

if 'hooks' not in settings:
    settings['hooks'] = {}

if 'Stop' not in settings['hooks']:
    settings['hooks']['Stop'] = []

# Check if already added
hook_entry = {"type": "command", "command": hook_path}
if hook_entry not in settings['hooks']['Stop']:
    settings['hooks']['Stop'].append(hook_entry)
    with open(settings_path, 'w') as f:
        json.dump(settings, f, indent=2)
    print("Hook added to settings")
else:
    print("Hook already in settings")
PYEOF

    log_info "Hook installed at $CLAUDE_HOOKS_DIR/$HOOK_NAME"
}

cmd_hook_remove() {
    log_info "Removing Claude Code stop hook"

    # Remove hook script
    rm -f "$CLAUDE_HOOKS_DIR/$HOOK_NAME"

    # Remove from settings.json
    if [ -f "$CLAUDE_SETTINGS" ]; then
        python3 << PYEOF
import json
import os

settings_path = os.path.expanduser("$CLAUDE_SETTINGS")
hook_path = os.path.expanduser("$CLAUDE_HOOKS_DIR/$HOOK_NAME")

with open(settings_path, 'r') as f:
    settings = json.load(f)

if 'hooks' in settings and 'Stop' in settings['hooks']:
    hook_entry = {"type": "command", "command": hook_path}
    if hook_entry in settings['hooks']['Stop']:
        settings['hooks']['Stop'].remove(hook_entry)
        with open(settings_path, 'w') as f:
            json.dump(settings, f, indent=2)
        print("Hook removed from settings")
PYEOF
    fi

    log_info "Hook removed"
}

# Main
case "${1:-}" in
    run)
        cmd_run
        ;;
    start)
        cmd_start
        ;;
    stop)
        cmd_stop
        ;;
    restart)
        cmd_restart
        ;;
    status)
        cmd_status
        ;;
    hook)
        case "${2:-}" in
            install)
                cmd_hook_install
                ;;
            remove)
                cmd_hook_remove
                ;;
            *)
                log_error "Usage: $0 hook [install|remove]"
                exit 1
                ;;
        esac
        ;;
    help|--help|-h)
        usage
        ;;
    version|--version|-v)
        echo "claudecode-imessage v${VERSION}"
        ;;
    *)
        usage
        exit 1
        ;;
esac
