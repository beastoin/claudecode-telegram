#!/usr/bin/env bash
# External bridge health watchdog — runs via cron every 2 minutes.
# 1. Checks the unified /health endpoint
# 2. Auto-restarts the bridge if dead (high availability)
# 3. Captures diagnostics on failure (fast root cause)
# 4. Alerts via Telegram bot API (bypasses bridge so it works even when bridge is broken)
#
# Install: crontab -e → */2 * * * * /home/claude/claudecode-telegram/bridge-watchdog.sh
set -euo pipefail

NODE="${BRIDGE_NODE:-prod}"
PORT="${BRIDGE_PORT:-8271}"
ALERT_COOLDOWN=300  # seconds between repeated alerts
AUTO_RESTART="${BRIDGE_AUTO_RESTART:-true}"  # set to "false" to disable
MAX_RESTARTS_PER_HOUR=3  # safety cap
STATE_DIR="$HOME/.claude/telegram/nodes/$NODE"
COOLDOWN_FILE="$STATE_DIR/.watchdog-alert-ts"
RESTART_LOG="$STATE_DIR/.watchdog-restarts"
DIAG_DIR="$STATE_DIR/diagnostics"
BRIDGE_DIR="$HOME/claudecode-telegram"

# Load bot token for direct Telegram alerting (bypasses bridge)
ENV_FILE="$HOME/.config/claudecode-telegram/$NODE.env"
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
fi

BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
ADMIN_CHAT="${ADMIN_CHAT_ID:-}"

# Fallback: read admin chat from bridge state
if [[ -z "$ADMIN_CHAT" && -f "$STATE_DIR/last_chat_id" ]]; then
    ADMIN_CHAT=$(cat "$STATE_DIR/last_chat_id" 2>/dev/null || true)
fi

# ---------------------------------------------------------------------------
# Diagnostics — capture system state on failure for fast root-cause analysis
# ---------------------------------------------------------------------------

capture_diagnostics() {
    local reason="$1"
    local ts
    ts=$(date +%Y%m%d-%H%M%S)
    local diag_file="$DIAG_DIR/$ts-${reason}.txt"
    mkdir -p "$DIAG_DIR"

    {
        echo "=== Bridge Watchdog Diagnostics ==="
        echo "Time:   $(date -Iseconds)"
        echo "Reason: $reason"
        echo "Node:   $NODE"
        echo "Port:   $PORT"
        echo ""

        echo "--- Bridge processes ---"
        pgrep -fa "bridge.py" 2>/dev/null || echo "(none)"
        echo ""

        echo "--- PID files ---"
        for f in "$STATE_DIR/pid" "$STATE_DIR/bridge.pid"; do
            if [[ -f "$f" ]]; then
                local pid_val
                pid_val=$(cat "$f" 2>/dev/null || echo "?")
                local alive="dead"
                kill -0 "$pid_val" 2>/dev/null && alive="alive"
                echo "$f: $pid_val ($alive)"
            else
                echo "$f: (missing)"
            fi
        done
        echo ""

        echo "--- Port $PORT listeners ---"
        ss -tlnp 2>/dev/null | grep ":$PORT " || echo "(none)"
        echo ""

        echo "--- Bridge log (last 50 lines) ---"
        tail -50 "$STATE_DIR/bridge.log" 2>/dev/null || echo "(no log)"
        echo ""

        echo "--- System resources ---"
        echo "Memory: $(free -h 2>/dev/null | grep Mem || echo '?')"
        echo "Disk:   $(df -h / 2>/dev/null | tail -1 || echo '?')"
        echo "Load:   $(uptime 2>/dev/null || echo '?')"
        echo ""

        echo "--- tmux sessions ---"
        tmux list-sessions 2>/dev/null | head -30 || echo "(none)"
        echo ""

        echo "--- Orphan bridge.py processes ---"
        local main_pid=""
        for f in "$STATE_DIR/pid" "$STATE_DIR/bridge.pid"; do
            [[ -f "$f" ]] && main_pid=$(cat "$f" 2>/dev/null) && break
        done
        pgrep -fa "bridge.py" 2>/dev/null | while read -r line; do
            local pid
            pid=$(echo "$line" | awk '{print $1}')
            if [[ -n "$main_pid" && "$pid" == "$main_pid" ]]; then
                echo "  $line (MAIN)"
            else
                echo "  $line (ORPHAN)"
            fi
        done
        echo ""

        echo "--- Recent dmesg OOM/kill (last 20) ---"
        dmesg --time-format iso 2>/dev/null | grep -iE "oom|kill|bridge" | tail -20 || echo "(none or no access)"

    } > "$diag_file" 2>&1

    # Keep only last 20 diagnostics
    # shellcheck disable=SC2012
    ls -1t "$DIAG_DIR"/*.txt 2>/dev/null | tail -n +21 | xargs rm -f 2>/dev/null || true

    echo "$diag_file"
}

# ---------------------------------------------------------------------------
# Auto-restart — bring bridge back if dead
# ---------------------------------------------------------------------------

check_restart_budget() {
    # Returns 0 if restart is allowed, 1 if budget exhausted
    local now
    now=$(date +%s)
    local one_hour_ago=$(( now - 3600 ))

    # Count restarts in last hour
    local count=0
    if [[ -f "$RESTART_LOG" ]]; then
        while IFS= read -r ts; do
            if (( ts > one_hour_ago )); then
                count=$((count + 1))
            fi
        done < "$RESTART_LOG"
    fi

    if (( count >= MAX_RESTARTS_PER_HOUR )); then
        return 1
    fi
    return 0
}

record_restart() {
    local now
    now=$(date +%s)
    echo "$now" >> "$RESTART_LOG"
    # Trim old entries (keep last 24h)
    if [[ -f "$RESTART_LOG" ]]; then
        local day_ago=$(( now - 86400 ))
        local tmp="$RESTART_LOG.tmp"
        while IFS= read -r ts; do
            if (( ts > day_ago )); then
                echo "$ts"
            fi
        done < "$RESTART_LOG" > "$tmp"
        mv "$tmp" "$RESTART_LOG"
    fi
}

auto_restart_bridge() {
    local reason="$1"

    if [[ "$AUTO_RESTART" != "true" ]]; then
        return 1
    fi

    if ! check_restart_budget; then
        send_alert "Bridge dead ($reason) — restart budget exhausted ($MAX_RESTARTS_PER_HOUR/hour). Manual intervention needed." true
        return 1
    fi

    # Kill any orphan bridge.py processes on our port
    pgrep -f "bridge.py.*--port $PORT" 2>/dev/null | while read -r pid; do
        kill "$pid" 2>/dev/null || true
    done
    sleep 2

    # Also kill anything holding the port
    fuser -k "$PORT/tcp" 2>/dev/null || true
    sleep 1

    # Start bridge via bridge.sh
    cd "$BRIDGE_DIR"
    if [[ -f "$ENV_FILE" ]]; then
        # shellcheck disable=SC1090
        source "$ENV_FILE"
    fi

    setsid env \
        TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}" \
        BRIDGE_PUBLIC_URL="${BRIDGE_PUBLIC_URL:-http://100.125.36.102:$PORT}" \
        python3 -u bridge.py --node "$NODE" --port "$PORT" \
        </dev/null >> "$STATE_DIR/bridge.log" 2>&1 &
    local new_pid=$!
    disown "$new_pid" 2>/dev/null || true

    # Wait for bridge to bind
    local ready=false
    for i in $(seq 1 30); do
        sleep 1
        if curl -sf "http://127.0.0.1:$PORT/health" > /dev/null 2>&1; then
            ready=true
            break
        fi
    done

    record_restart

    if [[ "$ready" == "true" ]]; then
        echo "$(date -Iseconds) AUTO-RESTART: success (reason=$reason, new_pid=$new_pid)" >> "$STATE_DIR/watchdog.log"
        send_alert "✅ Bridge auto-restarted (reason: $reason, PID=$new_pid)" true
        return 0
    else
        echo "$(date -Iseconds) AUTO-RESTART: FAILED (reason=$reason, pid=$new_pid)" >> "$STATE_DIR/watchdog.log"
        send_alert "Bridge auto-restart FAILED ($reason). PID=$new_pid but health check not responding. Manual fix needed." true
        return 1
    fi
}

# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------

send_alert() {
    local msg="$1"
    local bridge_dead="${2:-false}"
    # Check cooldown
    if [[ -f "$COOLDOWN_FILE" ]]; then
        local last_ts
        last_ts=$(cat "$COOLDOWN_FILE" 2>/dev/null || echo 0)
        local now
        now=$(date +%s)
        if (( now - last_ts < ALERT_COOLDOWN )); then
            return 0  # cooldown active
        fi
    fi
    echo "$(date -Iseconds) ALERT: $msg" >> "$STATE_DIR/watchdog.log"
    date +%s > "$COOLDOWN_FILE"
    # Primary: deliver to alert workers via tmux (worker-driven, no manager in loop)
    ALERT_WORKERS="${BRIDGE_ALERT_WORKERS:-}"
    if [[ -n "$ALERT_WORKERS" ]]; then
        local tmux_prefix="${TMUX_PREFIX:-claude-${NODE}-}"
        IFS=',' read -ra workers <<< "$ALERT_WORKERS"
        for w in "${workers[@]}"; do
            w=$(echo "$w" | xargs)  # trim whitespace
            tmux send-keys -t "${tmux_prefix}${w}" "bridge-watchdog: $msg" Enter 2>/dev/null || true
        done
    fi
    # Fallback: Telegram DM to manager ONLY if bridge is dead (last resort)
    if [[ "$bridge_dead" == "true" && -n "$BOT_TOKEN" && -n "$ADMIN_CHAT" ]]; then
        curl -s -m 10 "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
            -d "chat_id=$ADMIN_CHAT" \
            -d "text=🚨 Bridge watchdog: $msg" \
            -d "parse_mode=" > /dev/null 2>&1 || true
    fi
}

send_resolved() {
    local signal="$1"
    ALERT_WORKERS="${BRIDGE_ALERT_WORKERS:-}"
    if [[ -n "$ALERT_WORKERS" ]]; then
        local tmux_prefix="${TMUX_PREFIX:-claude-${NODE}-}"
        IFS=',' read -ra workers <<< "$ALERT_WORKERS"
        for w in "${workers[@]}"; do
            w=$(echo "$w" | xargs)
            tmux send-keys -t "${tmux_prefix}${w}" "bridge-watchdog: ✅ Resolved: $signal" Enter 2>/dev/null || true
        done
    fi
    if [[ -n "$BOT_TOKEN" && -n "$ADMIN_CHAT" ]]; then
        curl -s -m 10 "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
            -d "chat_id=$ADMIN_CHAT" \
            -d "text=✅ Bridge watchdog: Resolved: $signal" \
            -d "parse_mode=" > /dev/null 2>&1 || true
    fi
}

# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

# Check 1: Is the bridge process alive?
BRIDGE_PID=""
for pidfile in "$STATE_DIR/bridge.pid" "$STATE_DIR/pid"; do
    if [[ -f "$pidfile" ]]; then
        pid=$(cat "$pidfile" 2>/dev/null || true)
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            BRIDGE_PID="$pid"
            break
        fi
    fi
done

if [[ -z "$BRIDGE_PID" ]]; then
    # Also check for any bridge.py process on our port (PID file stale)
    BRIDGE_PID=$(pgrep -f "bridge.py.*--port $PORT" 2>/dev/null | head -1 || true)
fi

if [[ -z "$BRIDGE_PID" ]]; then
    DIAG=$(capture_diagnostics "process-dead")
    if auto_restart_bridge "process-dead"; then
        send_resolved "bridge:dead"
    else
        send_alert "🚨 Bridge process not running (node=$NODE). Diagnostics: $DIAG" true
    fi
    exit 1
fi

# Check 2: Hit the unified /health endpoint
HEALTH=$(curl -s -m 5 "http://127.0.0.1:$PORT/health" 2>/dev/null || echo '{"error":"unreachable"}')
if echo "$HEALTH" | grep -q '"error"'; then
    DIAG=$(capture_diagnostics "http-dead")
    if auto_restart_bridge "http-dead"; then
        send_resolved "bridge:http"
    else
        send_alert "🚨 Bridge HTTP not responding on port $PORT (PID=$BRIDGE_PID). Diagnostics: $DIAG" true
    fi
    exit 1
fi

# Check 3: Parse the unified health response
ALL_OK=$(echo "$HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('ok', False))" 2>/dev/null || echo "")

if [[ "$ALL_OK" == "False" ]]; then
    # Check which signals failed, filtering out snoozed alerts
    ALERTS_JSON=$(curl -s -m 3 "http://127.0.0.1:$PORT/alerts" 2>/dev/null || echo '{}')
    DETAILS=$(HEALTH_JSON="$HEALTH" ALERTS_JSON="$ALERTS_JSON" python3 -c "
import os, json
d = json.loads(os.environ['HEALTH_JSON'])
try:
    alerts = json.loads(os.environ.get('ALERTS_JSON', '{}'))
    snoozed = set(alerts.get('snoozed', {}).keys())
except: snoozed = set()
bad = []
for sig, info in d.get('signals', {}).items():
    if not info.get('ok') and sig not in snoozed:
        bad.append(f'{sig}(age={info.get(\"last_ok_ago_s\",\"?\"):.0f}s,err={info.get(\"errors\",0)})')
for name, alive in d.get('threads', {}).items():
    key = f'thread:{name}'
    if not alive and key not in snoozed:
        bad.append(f'thread:{name}=DEAD')
pools = d.get('pools', {})
for pname, pinfo in pools.items():
    pending = pinfo.get('pending', 0)
    mx = pinfo.get('max', 0)
    if pending > mx * 2:
        bad.append(f'{pname}:saturated({pending})')
print(', '.join(bad) if bad else '')
" 2>/dev/null || echo "parse error")
    if [[ -n "$DETAILS" ]]; then
        # Capture diagnostics for degraded health too
        capture_diagnostics "health-degraded" > /dev/null 2>&1
        send_alert "Health degraded: $DETAILS"
        exit 1
    fi
    # All failures are snoozed — log but don't alert
    echo "$(date -Iseconds) OK pid=$BRIDGE_PID (snoozed failures)" >> "$STATE_DIR/watchdog.log"
    exit 0
fi

# All OK — log heartbeat
VERSION=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('version','?'))" 2>/dev/null || echo "?")
UPTIME=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('uptime_s','?'))" 2>/dev/null || echo "?")
WORKERS=$(echo "$HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len([s for s,v in d.get('signals',{}).items()]))" 2>/dev/null || echo "?")
echo "$(date -Iseconds) OK pid=$BRIDGE_PID v=$VERSION uptime=${UPTIME}s signals=$WORKERS" >> "$STATE_DIR/watchdog.log"

# Clear cooldown on healthy state (so next failure alerts immediately)
rm -f "$COOLDOWN_FILE" 2>/dev/null || true

# Rotate watchdog log (keep last 500 lines)
if [[ -f "$STATE_DIR/watchdog.log" ]]; then
    tail -500 "$STATE_DIR/watchdog.log" > "$STATE_DIR/watchdog.log.tmp" 2>/dev/null && \
        mv "$STATE_DIR/watchdog.log.tmp" "$STATE_DIR/watchdog.log" 2>/dev/null || true
fi

# Rotate bridge log (cap at ~5MB)
BRIDGE_LOG="$STATE_DIR/bridge.log"
if [[ -f "$BRIDGE_LOG" ]]; then
    LOG_SIZE=$(stat -c %s "$BRIDGE_LOG" 2>/dev/null || echo 0)
    if (( LOG_SIZE > 5242880 )); then
        cp "$BRIDGE_LOG" "$BRIDGE_LOG.1" 2>/dev/null || true
        truncate -s 0 "$BRIDGE_LOG" 2>/dev/null || true
    fi
fi

# Rotate diagnostics (keep last 20)
if [[ -d "$DIAG_DIR" ]]; then
    # shellcheck disable=SC2012
    ls -1t "$DIAG_DIR"/*.txt 2>/dev/null | tail -n +21 | xargs rm -f 2>/dev/null || true
fi
