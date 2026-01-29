#!/usr/bin/env bash
# Minimal controller for C bridge
set -euo pipefail

VERSION="0.9.5"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_BIN="$SCRIPT_DIR/bridge"
PID_FILE_DEFAULT="$HOME/.claude/claudecode-telegram.pid"
PID_FILE="${PID_FILE:-$PID_FILE_DEFAULT}"

usage() {
  cat <<USAGE
claudecode-telegram (c) v${VERSION}

Usage:
  claudecode-telegram.sh run
  claudecode-telegram.sh start
  claudecode-telegram.sh stop
  claudecode-telegram.sh status
  claudecode-telegram.sh --version
USAGE
}

require_token() {
  if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    echo "error: TELEGRAM_BOT_TOKEN not set" >&2
    exit 1
  fi
}

cmd=${1:-}
case "$cmd" in
  --version|-v)
    echo "claudecode-telegram v${VERSION} (c)"
    exit 0
    ;;
  run)
    require_token
    exec "$BRIDGE_BIN"
    ;;
  start)
    require_token
    if [[ -f "$PID_FILE" ]]; then
      pid=$(cat "$PID_FILE")
      if kill -0 "$pid" 2>/dev/null; then
        echo "already running (pid $pid)"
        exit 0
      fi
    fi
    "$BRIDGE_BIN" >"${BRIDGE_LOG:-/dev/null}" 2>&1 &
    echo $! > "$PID_FILE"
    echo "started (pid $(cat "$PID_FILE"))"
    ;;
  stop)
    if [[ -f "$PID_FILE" ]]; then
      pid=$(cat "$PID_FILE")
      kill "$pid" 2>/dev/null || true
      rm -f "$PID_FILE"
      echo "stopped"
    else
      echo "not running"
    fi
    ;;
  status)
    if [[ -f "$PID_FILE" ]]; then
      pid=$(cat "$PID_FILE")
      if kill -0 "$pid" 2>/dev/null; then
        echo "running (pid $pid)"
        exit 0
      fi
    fi
    echo "not running"
    exit 1
    ;;
  ""|--help|-h)
    usage
    exit 0
    ;;
  *)
    echo "unknown command: $cmd" >&2
    usage
    exit 1
    ;;
esac
