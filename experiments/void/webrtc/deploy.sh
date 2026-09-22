#!/usr/bin/env bash
set -euo pipefail

# Void WebRTC deployment script for triassic-4
# Usage: ./deploy.sh [start|stop|status]

DEPLOY_DIR="/opt/void-webrtc"
PID_DIR="/var/run/void-webrtc"
LOG_DIR="/var/log/void-webrtc"

# Service configuration
DISPLAY_NUM=":99"
SCREEN_WIDTH=720
SCREEN_HEIGHT=1280
SCREEN_FPS=15
RTP_ADDR="127.0.0.1:5004"
CDP_PORT=9222
STREAMD_PORT=8097
GATEWAY_PORT=8096
START_URL="https://www.google.com"
GATEWAY_BIND="${GATEWAY_BIND:-127.0.0.1}"

# Secrets (override via env)
SESSION_SECRET="${SESSION_SECRET:-void-session-secret-$(hostname)}"
TURN_SECRET="${TURN_SECRET:-void-turn-secret-$(hostname)}"
TURN_URL="${TURN_URL:-stun:stun.l.google.com:19302}"
GATEWAY_API_KEY="${GATEWAY_API_KEY:-}"
ALLOWED_ORIGIN="${ALLOWED_ORIGIN:-}"

mkdir -p "$PID_DIR" "$LOG_DIR"

capture_new_pid() {
    local pattern="$1"
    local before_pids="$2"
    local pid_file="$3"

    if [ -f "$pid_file" ]; then
        return
    fi

    local candidate
    while read -r candidate; do
        [ -n "$candidate" ] || continue
        if ! grep -Fxq "$candidate" <<<"$before_pids"; then
            echo "$candidate" > "$pid_file"
            return
        fi
    done < <(pgrep -f "$pattern" 2>/dev/null || true)
}

start() {
    echo "Starting Void WebRTC..."

    # Check dependencies
    for cmd in Xvfb chromium ffmpeg python3; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            echo "ERROR: $cmd not found"
            exit 1
        fi
    done

    if [ ! -f "$DEPLOY_DIR/void-streamd" ]; then
        echo "ERROR: void-streamd binary not found at $DEPLOY_DIR/void-streamd"
        exit 1
    fi

    if [ -z "$GATEWAY_API_KEY" ]; then
        echo "ERROR: GATEWAY_API_KEY is required"
        exit 1
    fi

    # Stop existing if running
    stop 2>/dev/null || true

    local xvfb_before ffmpeg_before
    xvfb_before="$(pgrep -f "Xvfb $DISPLAY_NUM" 2>/dev/null || true)"
    ffmpeg_before="$(pgrep -f "ffmpeg.*x11grab.*$DISPLAY_NUM" 2>/dev/null || true)"

    # 1. Start void-streamd (manages Xvfb + Chromium + ffmpeg + WebRTC)
    echo "  Starting void-streamd..."
    SESSION_SECRET="$SESSION_SECRET" \
    TURN_URL="$TURN_URL" \
    DISPLAY_NUM="$DISPLAY_NUM" \
    SCREEN_WIDTH="$SCREEN_WIDTH" \
    SCREEN_HEIGHT="$SCREEN_HEIGHT" \
    SCREEN_FPS="$SCREEN_FPS" \
    RTP_ADDR="$RTP_ADDR" \
    CDP_PORT="$CDP_PORT" \
    START_URL="$START_URL" \
    STREAMD_ADDR=":$STREAMD_PORT" \
    nohup "$DEPLOY_DIR/void-streamd" \
        > "$LOG_DIR/streamd.log" 2>&1 &
    echo $! > "$PID_DIR/streamd.pid"
    echo "  void-streamd PID: $(cat "$PID_DIR/streamd.pid")"

    # Wait for streamd to be ready
    for i in $(seq 1 20); do
        if curl -sf "http://127.0.0.1:$STREAMD_PORT/health" >/dev/null 2>&1; then
            break
        fi
        sleep 0.5
    done

    # Record child process PIDs started by streamd.
    for i in $(seq 1 20); do
        capture_new_pid "Xvfb $DISPLAY_NUM" "$xvfb_before" "$PID_DIR/xvfb.pid"
        capture_new_pid "ffmpeg.*x11grab.*$DISPLAY_NUM" "$ffmpeg_before" "$PID_DIR/ffmpeg.pid"
        if [ -f "$PID_DIR/xvfb.pid" ] && [ -f "$PID_DIR/ffmpeg.pid" ]; then
            break
        fi
        sleep 0.5
    done

    # 2. Start gateway
    echo "  Starting gateway..."
    SESSION_SECRET="$SESSION_SECRET" \
    TURN_SECRET="$TURN_SECRET" \
    TURN_URL="$TURN_URL" \
    STREAMD_URL="http://127.0.0.1:$STREAMD_PORT" \
    GATEWAY_ADDR="$GATEWAY_BIND:$GATEWAY_PORT" \
    GATEWAY_API_KEY="$GATEWAY_API_KEY" \
    ALLOWED_ORIGIN="$ALLOWED_ORIGIN" \
    CLIENT_DIR="$DEPLOY_DIR/client" \
    nohup python3 "$DEPLOY_DIR/gateway/gateway.py" \
        > "$LOG_DIR/gateway.log" 2>&1 &
    echo $! > "$PID_DIR/gateway.pid"
    echo "  gateway PID: $(cat "$PID_DIR/gateway.pid")"

    sleep 1

    # Verify
    echo ""
    if curl -sf "http://127.0.0.1:$STREAMD_PORT/health" >/dev/null 2>&1; then
        echo "  streamd: OK"
    else
        echo "  streamd: FAILED (check $LOG_DIR/streamd.log)"
    fi

    if curl -sf "http://127.0.0.1:$GATEWAY_PORT/session/status?session_id=test" >/dev/null 2>&1; then
        echo "  gateway: OK"
    else
        echo "  gateway: FAILED (check $LOG_DIR/gateway.log)"
    fi

    echo ""
    echo "Access the browser at: http://$(hostname -I | awk '{print $1}'):$GATEWAY_PORT/"
    echo "Tailscale access:      http://$(cat /etc/hostname 2>/dev/null || hostname):$GATEWAY_PORT/"
    echo ""
}

stop() {
    echo "Stopping Void WebRTC..."
    for svc in gateway streamd xvfb ffmpeg; do
        if [ -f "$PID_DIR/$svc.pid" ]; then
            pid=$(cat "$PID_DIR/$svc.pid")
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid" 2>/dev/null || true
                echo "  Stopped $svc (PID $pid)"
            fi
            rm -f "$PID_DIR/$svc.pid"
        fi
    done
}

status() {
    echo "Void WebRTC Status:"
    for svc in streamd gateway; do
        if [ -f "$PID_DIR/$svc.pid" ]; then
            pid=$(cat "$PID_DIR/$svc.pid")
            if kill -0 "$pid" 2>/dev/null; then
                echo "  $svc: running (PID $pid)"
            else
                echo "  $svc: dead (stale PID $pid)"
            fi
        else
            echo "  $svc: not running"
        fi
    done

    echo ""
    if curl -sf "http://127.0.0.1:$STREAMD_PORT/health" 2>/dev/null; then
        echo ""
    else
        echo "  streamd health: unreachable"
    fi
}

case "${1:-start}" in
    start)  start ;;
    stop)   stop ;;
    status) status ;;
    *)      echo "Usage: $0 [start|stop|status]"; exit 1 ;;
esac
