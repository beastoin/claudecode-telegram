#!/usr/bin/env bash
# Deploy STT server to Mac Mini.
#
# Prerequisites on target host:
#   1. ffmpeg installed (brew install ffmpeg)
#   2. transcribe-cli built (~/transcribe.cpp/build/bin/transcribe-cli)
#   3. GGUF model downloaded (~/transcribe.cpp/models/parakeet-unified-en-0.6b/*.gguf)
#
# Usage:
#   ./deploy.sh                          # deploy with launchd (auto-restart)
#   ./deploy.sh --host myhost            # deploy to custom host
#   ./deploy.sh --port 10110             # custom port (default: 10110)
#   ./deploy.sh --stop                   # stop the server
#   ./deploy.sh --status                 # check if running
#   ./deploy.sh --nohup                  # deploy without launchd (manual mode)
#
# Bridge wiring:
#   STT_ENDPOINT=http://<host-ip>:10110/transcribe
#   (default in bridge.py: http://100.126.187.125:10110/transcribe)

set -euo pipefail

HOST="${STT_HOST:-beastoin-agents-f1-mac-mini}"
PORT="${STT_PORT:-10110}"
REMOTE_DIR="~/transcribe.cpp"
PLIST_NAME="com.claudecode.stt"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

usage() {
    echo "Usage: $0 [--host HOST] [--port PORT] [--stop] [--status] [--nohup]"
    exit 1
}

cmd_status() {
    echo "Checking STT server on $HOST:$PORT..."
    if ssh "$HOST" "lsof -iTCP:$PORT -sTCP:LISTEN" 2>/dev/null | grep -q LISTEN; then
        echo "  RUNNING"
        ssh "$HOST" "curl -s http://localhost:$PORT/health 2>/dev/null" && echo
        if ssh "$HOST" "launchctl list $PLIST_NAME" 2>/dev/null | grep -q PID; then
            echo "  Managed by: launchd (auto-restart on crash)"
        else
            echo "  Managed by: manual (nohup)"
        fi
    else
        echo "  NOT RUNNING"
    fi
}

cmd_stop() {
    echo "Stopping STT server on $HOST:$PORT..."
    ssh "$HOST" "launchctl bootout gui/\$(id -u) ~/Library/LaunchAgents/$PLIST_NAME.plist 2>/dev/null || true"
    ssh "$HOST" "lsof -iTCP:$PORT -sTCP:LISTEN -t 2>/dev/null | xargs kill 2>/dev/null || true"
    echo "  Stopped"
}

cmd_deploy() {
    local use_launchd="${1:-true}"
    echo "Deploying STT server to $HOST..."

    # Preflight
    echo "  Checking prerequisites..."
    ssh "$HOST" "command -v ffmpeg >/dev/null" || { echo "  ERROR: ffmpeg not found on $HOST"; exit 1; }
    ssh "$HOST" "test -x $REMOTE_DIR/build/bin/transcribe-cli" || { echo "  ERROR: transcribe-cli not found"; exit 1; }
    ssh "$HOST" "ls $REMOTE_DIR/models/parakeet-unified-en-0.6b/*.gguf >/dev/null 2>&1" || { echo "  ERROR: model not found"; exit 1; }

    # Copy serve.py
    echo "  Copying serve.py..."
    scp -q "$SCRIPT_DIR/serve.py" "$HOST:$REMOTE_DIR/serve.py"

    # Stop existing
    cmd_stop 2>/dev/null

    sleep 1

    if [[ "$use_launchd" == "true" ]]; then
        echo "  Installing launchd service..."
        scp -q "$SCRIPT_DIR/$PLIST_NAME.plist" "$HOST:~/Library/LaunchAgents/$PLIST_NAME.plist"
        ssh "$HOST" "launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/$PLIST_NAME.plist 2>/dev/null || launchctl load ~/Library/LaunchAgents/$PLIST_NAME.plist"
        sleep 3
    else
        echo "  Starting with nohup (no auto-restart)..."
        ssh "$HOST" "nohup python3 $REMOTE_DIR/serve.py --port $PORT > /tmp/stt-server.log 2>&1 &"
        sleep 3
    fi

    # Verify
    if ssh "$HOST" "lsof -iTCP:$PORT -sTCP:LISTEN" 2>/dev/null | grep -q LISTEN; then
        echo "  RUNNING on $HOST:$PORT"
        echo "  Health: $(ssh "$HOST" "curl -s http://localhost:$PORT/health")"
        if [[ "$use_launchd" == "true" ]]; then
            echo "  Managed by: launchd (auto-restart on crash)"
        fi
        echo ""
        echo "Bridge env: STT_ENDPOINT=http://$(ssh "$HOST" "ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null" | tr -d ' '):$PORT/transcribe"
    else
        echo "  ERROR: server failed to start. Check /tmp/stt-server.log on $HOST"
        ssh "$HOST" "tail -10 /tmp/stt-server.log" 2>/dev/null
        exit 1
    fi
}

# Parse args
ACTION="deploy"
USE_LAUNCHD="true"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) HOST="$2"; shift 2;;
        --port) PORT="$2"; shift 2;;
        --stop) ACTION="stop"; shift;;
        --status) ACTION="status"; shift;;
        --nohup) USE_LAUNCHD="false"; shift;;
        -h|--help) usage;;
        *) echo "Unknown arg: $1"; usage;;
    esac
done

case "$ACTION" in
    deploy) cmd_deploy "$USE_LAUNCHD";;
    stop) cmd_stop;;
    status) cmd_status;;
esac
