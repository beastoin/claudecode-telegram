#!/usr/bin/env bash
#
# claudecode-telegram - Bridge Claude Code to Telegram via webhooks
# Multi-node support: use NODE_NAME or --node to target specific nodes
#
set -euo pipefail

# ============================================================
# CONFIG + GLOBALS
# ============================================================

VERSION="0.50.0"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─────────────────────────────────────────────────────────────────────────────
# Environment variables
# ─────────────────────────────────────────────────────────────────────────────
# Required:
#   TELEGRAM_BOT_TOKEN      - Bot token from @BotFather
#
# Optional:
#   ADMIN_CHAT_ID           - Pre-set admin (otherwise auto-learns first user)
#   TUNNEL_URL              - Use existing tunnel instead of starting cloudflared
#   TELEGRAM_WEBHOOK_SECRET - Webhook verification secret
#
# Derived (auto-set per node, don't set manually):
#   PORT, SESSIONS_DIR, TMUX_PREFIX
# ─────────────────────────────────────────────────────────────────────────────

: "${PORT:=8270}"
: "${TUNNEL_URL:=}"

CLAUDE_DIR="${CLAUDE_DIR:-$HOME/.claude}"
CLAUDE_SETTINGS_FILE="${CLAUDE_SETTINGS_FILE:-$CLAUDE_DIR/settings.json}"
HOOKS_DIR="$CLAUDE_DIR/hooks"
SETTINGS_FILE="$CLAUDE_SETTINGS_FILE"
NODES_DIR="$CLAUDE_DIR/telegram/nodes"
HOOK_SCRIPT="claudecode.sh"

# CLI flags
VERBOSE=false
QUIET=false
NO_COLOR=false
HEADLESS=false
JSON_OUTPUT=false
FORCE=false
ALL_NODES=false
NODE_NAME="${NODE_NAME:-}"
# ============================================================
# OUTPUT + LOGGING
# ============================================================

# ─────────────────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────────────────

_supports_color() { [[ -t 1 ]] && [[ -z "${NO_COLOR:-}" ]] && [[ "${TERM:-}" != "dumb" ]]; }
_color() { if _supports_color && ! $NO_COLOR; then printf "\033[%sm%s\033[0m" "$1" "$2"; else printf "%s" "$2"; fi; }
red()    { _color "31" "$1"; }
green()  { _color "32" "$1"; }
yellow() { _color "33" "$1"; }
bold()   { _color "1" "$1"; }
dim()    { _color "2" "$1"; }

log()     { $QUIET || $JSON_OUTPUT || echo "$@"; }
debug()   { $VERBOSE && echo "$(dim "[debug]") $*" >&2 || true; }
error()   { echo "$(red "error:") $*" >&2; }
success() { $QUIET || $JSON_OUTPUT || echo "$(green "✓") $*"; }
warn()    { $QUIET || echo "$(yellow "warning:") $*" >&2; }
hint()    { $QUIET || $JSON_OUTPUT || echo "$(dim "→") $*"; }

# ============================================================
# NODE MANAGEMENT + RESOLUTION
# ============================================================

# ─────────────────────────────────────────────────────────────────────────────
# Node Management
# ─────────────────────────────────────────────────────────────────────────────

sanitize_node_name() {
    local name="$1"
    # Only allow alphanumeric and hyphens, lowercase
    echo "$name" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9-]//g'
}

get_node_dir() {
    local node="$1"
    echo "$NODES_DIR/$node"
}

get_node_pid_file() {
    local node="$1"
    echo "$(get_node_dir "$node")/pid"
}

get_node_sessions_dir() {
    local node="$1"
    echo "$(get_node_dir "$node")/sessions"
}

get_node_tmux_prefix() {
    local node="$1"
    echo "claude-${node}-"
}

get_default_port() {
    local node="$1"
    case "$node" in
        prod) echo 8271 ;;
        dev)  echo 8272 ;;
        test) echo 8295 ;;
        *)    echo 8270 ;;
    esac
}

ensure_node_dir() {
    local node="$1"
    local node_dir
    node_dir=$(get_node_dir "$node")
    mkdir -p "$node_dir"
    chmod 700 "$node_dir"

    local sessions_dir
    sessions_dir=$(get_node_sessions_dir "$node")
    mkdir -p "$sessions_dir"
    chmod 700 "$sessions_dir"
}

is_node_running() {
    local node="$1"
    local pid_file
    pid_file=$(get_node_pid_file "$node")

    if [[ -f "$pid_file" ]]; then
        local pid
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        # Stale PID file — process is dead, clean up
        rm -f "$pid_file"
    fi
    return 1
}

list_running_nodes() {
    local nodes=()

    if [[ -d "$NODES_DIR" ]]; then
        for node_dir in "$NODES_DIR"/*/; do
            [[ -d "$node_dir" ]] || continue
            local node
            node=$(basename "$node_dir")
            if is_node_running "$node"; then
                nodes+=("$node")
            fi
        done
    fi

    printf '%s\n' "${nodes[@]}"
}

list_all_nodes() {
    local nodes=()

    if [[ -d "$NODES_DIR" ]]; then
        for node_dir in "$NODES_DIR"/*/; do
            [[ -d "$node_dir" ]] || continue
            local node
            node=$(basename "$node_dir")
            nodes+=("$node")
        done
    fi

    printf '%s\n' "${nodes[@]}"
}

# ─────────────────────────────────────────────────────────────────────────────
# Node resolution (auto-detect + prompts)
# ─────────────────────────────────────────────────────────────────────────────

resolve_target_node() {
    # Returns the target node name, or exits with error
    # Priority: --node flag > NODE_NAME env > auto-detect

    if [[ -n "$NODE_NAME" ]]; then
        local sanitized
        sanitized=$(sanitize_node_name "$NODE_NAME")
        if [[ -z "$sanitized" ]]; then
            error "Invalid node name: $NODE_NAME"
            exit 2
        fi
        echo "$sanitized"
        return 0
    fi

    # Auto-detect: check running nodes
    local running_nodes
    running_nodes=$(list_running_nodes)
    local count=0
    [[ -n "$running_nodes" ]] && count=$(echo "$running_nodes" | wc -l | tr -d ' ')

    if [[ $count -eq 0 ]]; then
        # No running nodes - default to "prod" for run command
        echo "prod"
        return 0
    elif [[ $count -eq 1 ]]; then
        # Exactly one running - use it
        echo "$running_nodes"
        return 0
    else
        # Multiple running - need explicit target
        if $HEADLESS || [[ ! -t 0 ]]; then
            error "Multiple nodes running. Specify with --node <name> or --all"
            hint "Running nodes: $(echo "$running_nodes" | tr '\n' ' ')"
            exit 2
        else
            # Interactive: prompt
            log "Multiple nodes running:"
            local i=1
            local node_array=()
            while IFS= read -r node; do
                [[ -n "$node" ]] || continue
                log "  $i) $node"
                node_array+=("$node")
                ((i++))
            done <<< "$running_nodes"
            log "  a) all"

            read -rp "Select node [1-$((i-1))/a]: " choice

            if [[ "$choice" == "a" ]]; then
                ALL_NODES=true
                return 0
            elif [[ "$choice" =~ ^[0-9]+$ ]] && [[ $choice -ge 1 ]] && [[ $choice -lt $i ]]; then
                echo "${node_array[$((choice-1))]}"
                return 0
            else
                error "Invalid choice"
                exit 2
            fi
        fi
    fi
}

# ============================================================
# UTILITY FUNCTIONS
# ============================================================

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

require_token() {
    [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]] || { error "TELEGRAM_BOT_TOKEN not set"; exit 3; }
    echo "$TELEGRAM_BOT_TOKEN"
}

check_cmd() { command -v "$1" &>/dev/null; }

port_in_use() {
    nc -z localhost "$1" 2>/dev/null
}

require_port_free() {
    local port="$1"
    if port_in_use "$port"; then
        error "Port $port is already in use"
        hint "Stop the other process or use: --port <other-port>"
        exit 1
    fi
}

telegram_api() {
    local token="$1" method="$2" data="$3"
    curl -s -X POST "https://api.telegram.org/bot${token}/${method}" \
        -H "Content-Type: application/json" -d "$data"
}

telegram_set_webhook() {
    local token="$1" url="$2"
    if [[ -n "${TELEGRAM_WEBHOOK_SECRET:-}" ]]; then
        curl -s "https://api.telegram.org/bot${token}/setWebhook?url=${url}&secret_token=${TELEGRAM_WEBHOOK_SECRET}"
    else
        curl -s "https://api.telegram.org/bot${token}/setWebhook?url=${url}"
    fi
}

bridge_notify() {
    local port="$1" message="$2"
    local body host
    body=$(printf '{"text":"%s"}' "$message")
    # Use BRIDGE_PUBLIC_URL host if set (bridge may bind Tailscale IP, not localhost)
    host=$(echo "${BRIDGE_PUBLIC_URL:-}" | sed -E 's|https?://([^:/]+).*|\1|')
    host="${host:-localhost}"
    curl -s -X POST "http://${host}:$port/notifications" \
        -H "Content-Type: application/json" \
        -d "$body" >/dev/null 2>&1 || true
}

# Poll fallback: getUpdates long-polling when webhook/tunnel DNS fails
_poll_fallback_pid=""

start_poll_fallback() {
    local token="$1" port="$2"
    [[ -n "$_poll_fallback_pid" ]] && return 0

    telegram_api "$token" "deleteWebhook" "{}" >/dev/null 2>&1 || true
    sleep 1

    python3 -u -c '
import os, time, json, urllib.request, sys
token, bridge = sys.argv[1], sys.argv[2]
offset = 0
print(f"Poll fallback started (bridge={bridge})", flush=True)
while True:
    try:
        url = f"https://api.telegram.org/bot{token}/getUpdates?offset={offset}&timeout=30"
        with urllib.request.urlopen(urllib.request.Request(url), timeout=35) as resp:
            data = json.loads(resp.read())
        if not data.get("ok"):
            time.sleep(1)
            continue
        for update in data.get("result", []):
            uid = update["update_id"]
            try:
                req = urllib.request.Request(
                    f"{bridge}/", data=json.dumps(update).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=5)
            except Exception as e:
                print(f"Forward failed {uid}: {e}", flush=True)
            offset = uid + 1
    except Exception as e:
        print(f"Poll error: {e}", flush=True)
        time.sleep(2)
' "$token" "http://localhost:$port" >> "${node_dir:-/tmp}/poll-fallback.log" 2>&1 &
    _poll_fallback_pid=$!
    log "Poll fallback started (PID $_poll_fallback_pid)"
}

stop_poll_fallback() {
    [[ -z "$_poll_fallback_pid" ]] && return 0
    kill "$_poll_fallback_pid" 2>/dev/null || true
    wait "$_poll_fallback_pid" 2>/dev/null || true
    _poll_fallback_pid=""
    log "Poll fallback stopped"
}

start_tunnel() {
    local port="$1" log_file="$2"
    cloudflared tunnel --url "http://localhost:$port" > "$log_file" 2>&1 &
    echo $!
}

wait_for_tunnel_url() {
    local log_file="$1" timeout="${2:-60}"
    local url="" attempts=0
    while [[ -z "$url" && $attempts -lt $timeout ]]; do
        sleep 1
        url=$(grep -o 'https://[^[:space:]]*\.trycloudflare\.com' "$log_file" 2>/dev/null | grep -v 'api\.trycloudflare\.com' | head -1 || true)
        ((attempts++))
    done
    echo "$url"
}

# Restart tunnel with retry logic (handles Cloudflare rate limiting)
restart_tunnel_with_retry() {
    local port="$1" node_dir="$2" max_attempts="${3:-3}"
    local attempt=1 backoff=5
    local tunnel_pid="" new_url=""

    while [[ $attempt -le $max_attempts ]]; do
        [[ $attempt -gt 1 ]] && log "Tunnel restart attempt $attempt/$max_attempts (waiting ${backoff}s)..." >&2
        [[ $attempt -gt 1 ]] && sleep "$backoff"

        # Kill any existing tunnel process
        local old_pid
        old_pid=$(cat "$node_dir/tunnel.pid" 2>/dev/null || true)
        [[ -n "$old_pid" ]] && kill "$old_pid" 2>/dev/null || true

        # Start new tunnel
        local tunnel_log="$node_dir/tunnel.log"
        : > "$tunnel_log"  # Truncate log
        tunnel_pid=$(start_tunnel "$port" "$tunnel_log")
        echo "$tunnel_pid" > "$node_dir/tunnel.pid"

        # Wait for URL (60s timeout)
        new_url=$(wait_for_tunnel_url "$tunnel_log" 60)

        if [[ -n "$new_url" ]]; then
            echo "$new_url"
            return 0
        fi

        # Failed, kill the process and retry
        kill "$tunnel_pid" 2>/dev/null || true
        ((attempt++))
        ((backoff *= 2))  # Exponential backoff: 5, 10, 20...
    done

    return 1  # All attempts failed
}

is_tunnel_alive() {
    local pid="$1"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

is_tunnel_reachable() {
    local url="$1"
    [[ -n "$url" ]] && curl -s --max-time 10 "$url" >/dev/null 2>&1
}

# ============================================================
# CLI COMMANDS
# ============================================================

# ─────────────────────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# Command: run
# ─────────────────────────────────────────────────────────────────────────────
cmd_run() {
    local node
    node=$(resolve_target_node)

    # Auto-source node env file (connector vars, tokens, etc.)
    local env_file="$HOME/.config/claudecode-telegram/${node}.env"
    if [[ -f "$env_file" ]]; then
        set -a
        # shellcheck source=/dev/null
        source "$env_file"
        set +a
        log "Loaded env from $env_file"
    fi

    # Parse args first (CLI takes precedence over config)
    local port="" tunnel_url="" no_tunnel=false

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -p=*|--port=*)       port="${1#*=}"; shift;;
            -p|--port)           port="$2"; shift 2;;
            --tunnel-url=*)      tunnel_url="${1#*=}"; shift;;
            --tunnel-url)        tunnel_url="$2"; shift 2;;
            --no-tunnel)         no_tunnel=true; shift;;
            --headless)          HEADLESS=true; shift;;
            -q|--quiet)          QUIET=true; shift;;
            -v|--verbose)        VERBOSE=true; shift;;
            *) shift;;
        esac
    done

    # Port: CLI flag > env var > derived from node name
    [[ -z "$port" ]] && port="${PORT:-$(get_default_port "$node")}"
    [[ -z "$tunnel_url" ]] && tunnel_url="${TUNNEL_URL:-}"

    local token; token=$(require_token)

    # Check dependencies
    check_cmd tmux || { error "tmux not installed"; hint "brew install tmux"; exit 4; }
    check_cmd python3 || { error "python3 not installed"; exit 4; }

    # cloudflared check: Python's TunnelManager handles this at runtime,
    # but pre-check here gives a clearer error message on startup.
    if ! $no_tunnel && [[ -z "$tunnel_url" ]]; then
        check_cmd cloudflared || { error "cloudflared not installed"; hint "brew install cloudflared (or use --no-tunnel)"; exit 4; }
    fi

    # Check if this node is already running
    if is_node_running "$node"; then
        error "Node '$node' is already running"
        hint "Use: ./bridge.sh --node $node restart"
        exit 1
    fi

    require_port_free "$port"

    ensure_node_dir "$node"

    local node_dir sessions_dir tmux_prefix pid_file
    node_dir=$(get_node_dir "$node")
    sessions_dir=$(get_node_sessions_dir "$node")
    tmux_prefix=$(get_node_tmux_prefix "$node")
    pid_file=$(get_node_pid_file "$node")

    log "$(bold "Starting Claude Code Telegram Bridge v${VERSION}")"
    log "$(bold "Node:") $node"
    log ""

    # 1. Install hooks if needed (single hook for all nodes, reads env at runtime)
    if [[ ! -f "$HOOKS_DIR/$HOOK_SCRIPT" ]]; then
        log "Installing hooks..."
        FORCE=true cmd_hook_install >/dev/null 2>&1 || true
        success "Hooks installed"
    else
        log "$(dim "Hooks already installed")"
    fi

    log "$(dim "No default session - use /hire <name> from Telegram")"

    # Set up env vars for bridge
    export TELEGRAM_BOT_TOKEN="$token" PORT="$port"
    export SESSIONS_DIR="$sessions_dir" TMUX_PREFIX="$tmux_prefix"
    export CLAUDE_DIR="$CLAUDE_DIR" CLAUDE_SETTINGS_FILE="$CLAUDE_SETTINGS_FILE"

    # Gmail connector env vars (pass through if set)
    [[ -n "${GMAIL_ENABLED:-}" ]] && export GMAIL_ENABLED
    [[ -n "${GMAIL_POLL_INTERVAL:-}" ]] && export GMAIL_POLL_INTERVAL
    [[ -n "${GMAIL_FROM_FILTER:-}" ]] && export GMAIL_FROM_FILTER
    [[ -n "${GMAIL_GWS_BIN:-}" ]] && export GMAIL_GWS_BIN

    local bridge_log="$node_dir/bridge.log"

    # Set tunnel mode env vars for Python's TunnelManager
    if $no_tunnel; then
        export TUNNEL_MODE="none"
        log "$(dim "No tunnel (use external tunnel or local testing)")"
    elif [[ -n "$tunnel_url" ]]; then
        export TUNNEL_MODE="provided"
        export TUNNEL_URL="$tunnel_url"
        log "$(dim "Using provided tunnel URL: $tunnel_url")"
    else
        export TUNNEL_MODE="${TUNNEL_MODE:-poll}"
        if [[ "$TUNNEL_MODE" == "auto" ]]; then
            log "$(dim "Tunnel: auto (cloudflared quick-tunnel)")"
        else
            log "$(dim "Polling: getUpdates long-polling (set TUNNEL_MODE=auto for cloudflared)")"
        fi
    fi

    log ""
    log "$(bold "Ready!") Send /hire <name> to your bot to create a Claude instance"
    log ""
    log "$(bold "Commands:") /hire /focus /team /restart /end"
    log "$(dim "Ctrl+C to stop")"
    log ""

    # Write main PID file
    echo $$ > "$pid_file"
    chmod 600 "$pid_file"
    log "$(dim "PID: $$ ($pid_file)")"

    echo "$port" > "$node_dir/port"

    # Cleanup on exit
    cleanup_and_exit() {
        log ""
        log "Shutting down node '${node:-unknown}'..."
        [[ -n "${pid_file:-}" ]] && rm -f "$pid_file"
        [[ -n "${node_dir:-}" ]] && rm -f "$node_dir/bridge.pid" "$node_dir/tunnel.pid" "$node_dir/tunnel.log" "$node_dir/tunnel_url" "$node_dir/port" "$node_dir/bot_id" "$node_dir/bot_username"
        exit 0
    }
    trap cleanup_and_exit EXIT INT TERM

    # Run bridge in foreground — Python's TunnelManager handles
    # cloudflared, webhook, poll fallback, and watchdog internally.
    python3 -u "$SCRIPT_DIR/bridge.py" 2>&1 | tee -a "$bridge_log"
}

# ─────────────────────────────────────────────────────────────────────────────
# Command: stop
# ─────────────────────────────────────────────────────────────────────────────
cmd_stop() {
    if $ALL_NODES; then
        # Stop all nodes
        local nodes
        nodes=$(list_running_nodes)
        if [[ -z "$nodes" ]]; then
            log "No nodes running"
            return 0
        fi

        while IFS= read -r node; do
            [[ -n "$node" ]] || continue
            stop_single_node "$node"
        done <<< "$nodes"

        success "All nodes stopped"
    else
        local node
        node=$(resolve_target_node)
        stop_single_node "$node"
    fi
}

stop_single_node() {
    local node="$1"
    local node_dir pid_file tmux_prefix
    node_dir=$(get_node_dir "$node")
    pid_file=$(get_node_pid_file "$node")
    tmux_prefix=$(get_node_tmux_prefix "$node")

    log "Stopping node '$node'..."
    local killed=0

    # Kill main process via PID file
    if [[ -f "$pid_file" ]]; then
        local main_pid
        main_pid=$(cat "$pid_file")
        if kill "$main_pid" 2>/dev/null; then
            ((killed++))
            # Wait for process to actually exit (up to 5s)
            local waited=0
            while kill -0 "$main_pid" 2>/dev/null && [[ $waited -lt 5 ]]; do
                sleep 1
                ((waited++))
            done
            success "Main process stopped (PID $main_pid)"
            rm -f "$pid_file"
        fi
    fi

    # Kill bridge
    if [[ -f "$node_dir/bridge.pid" ]]; then
        local bridge_pid
        bridge_pid=$(cat "$node_dir/bridge.pid")
        if kill "$bridge_pid" 2>/dev/null; then
            ((killed++))
            success "Bridge stopped"
        fi
        rm -f "$node_dir/bridge.pid"
    fi

    # Kill tunnel
    if [[ -f "$node_dir/tunnel.pid" ]]; then
        local tunnel_pid
        tunnel_pid=$(cat "$node_dir/tunnel.pid")
        if kill "$tunnel_pid" 2>/dev/null; then
            ((killed++))
            success "Tunnel stopped"
        fi
        rm -f "$node_dir/tunnel.pid" "$node_dir/tunnel.log" "$node_dir/tunnel_url"
    fi

    # Kill tmux sessions for this node
    local sessions
    sessions=$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep "^${tmux_prefix}" || true)
    if [[ -n "$sessions" ]]; then
        while IFS= read -r session; do
            if tmux kill-session -t "$session" 2>/dev/null; then
                ((killed++))
                success "Killed tmux session '$session'"
            fi
        done <<< "$sessions"
    fi

    rm -f "$node_dir/port" "$node_dir/bot_id" "$node_dir/bot_username"

    if [[ $killed -eq 0 ]]; then
        log "Node '$node' was not running"
    else
        success "Node '$node' stopped"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Command: clean
# ─────────────────────────────────────────────────────────────────────────────
cmd_clean() {
    local node
    node=$(resolve_target_node)
    local node_dir sessions_dir
    node_dir=$(get_node_dir "$node")
    sessions_dir=$(get_node_sessions_dir "$node")

    log "Cleaning node '$node'..."

    # Remove admin_chat_id file
    if [[ -f "$node_dir/admin_chat_id" ]]; then
        rm -f "$node_dir/admin_chat_id"
        success "Removed admin_chat_id"
    fi

    # Remove chat_id files from all sessions
    local cleaned=0
    if [[ -d "$sessions_dir" ]]; then
        for session_dir in "$sessions_dir"/*/; do
            [[ -d "$session_dir" ]] || continue
            if [[ -f "${session_dir}chat_id" ]]; then
                rm -f "${session_dir}chat_id"
                ((cleaned++))
            fi
        done
    fi

    if [[ $cleaned -gt 0 ]]; then
        success "Removed $cleaned session chat_id file(s)"
    fi

    success "Node '$node' cleaned. Next message will re-register admin."
}

# ─────────────────────────────────────────────────────────────────────────────
# Command: restart
# ─────────────────────────────────────────────────────────────────────────────
cmd_restart() {
    if $ALL_NODES; then
        error "--all not supported for restart"
        hint "Restart nodes individually: ./bridge.sh --node <name> restart"
        exit 2
    fi

    local node
    node=$(resolve_target_node)

    log "Restarting node '$node' (preserving tmux sessions)..."

    # Stop bridge and tunnel only (NOT tmux sessions)
    local node_dir pid_file
    node_dir=$(get_node_dir "$node")
    pid_file=$(get_node_pid_file "$node")

    if [[ -f "$pid_file" ]]; then
        local main_pid
        main_pid=$(cat "$pid_file")
        if kill "$main_pid" 2>/dev/null; then
            success "Main process stopped (PID $main_pid)"
            rm -f "$pid_file"
        fi
    fi

    # Fallback kills
    [[ -f "$node_dir/bridge.pid" ]] && kill "$(cat "$node_dir/bridge.pid")" 2>/dev/null || true
    [[ -f "$node_dir/tunnel.pid" ]] && kill "$(cat "$node_dir/tunnel.pid")" 2>/dev/null || true
    rm -f "$node_dir/bridge.pid" "$node_dir/tunnel.pid" "$node_dir/tunnel.log" "$node_dir/tunnel_url" "$node_dir/port" "$node_dir/bot_id" "$node_dir/bot_username"

    # Wait for port to be free (up to 10s)
    local port
    port="${PORT:-$(get_default_port "$node")}"
    local waited=0
    while ss -tlnp 2>/dev/null | grep -q ":${port} " && [[ $waited -lt 10 ]]; do
        sleep 1
        ((waited++))
    done
    if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
        error "Port $port still in use after 10s — check for orphan processes"
        hint "Run: ./bridge.sh --node $node status"
        exit 1
    fi

    # Start fresh with same args
    log ""
    NODE_NAME="$node" cmd_run "$@"
}

# Detect orphan processes (tunnels/bridges not owned by any node)
detect_orphan_processes() {
    local owned_tunnel_pids=() owned_bridge_pids=()
    local all_nodes

    # Collect all PIDs owned by nodes
    all_nodes=$(list_all_nodes)
    while IFS= read -r node; do
        [[ -n "$node" ]] || continue
        local node_dir; node_dir=$(get_node_dir "$node")
        if [[ -f "$node_dir/tunnel.pid" ]]; then
            local pid; pid=$(cat "$node_dir/tunnel.pid")
            [[ -n "$pid" ]] && owned_tunnel_pids+=("$pid")
        fi
        if [[ -f "$node_dir/bridge.pid" ]]; then
            local pid; pid=$(cat "$node_dir/bridge.pid")
            [[ -n "$pid" ]] && owned_bridge_pids+=("$pid")
        fi
    done <<< "$all_nodes"

    # Find all running cloudflared tunnel processes
    local orphan_tunnels=()
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        local pid port
        pid=$(echo "$line" | awk '{print $1}')
        port=$(echo "$line" | grep -o 'localhost:[0-9]*' | cut -d: -f2 || echo "?")
        # Check if this PID is owned by a node
        local owned=false
        for owned_pid in "${owned_tunnel_pids[@]}"; do
            [[ "$pid" == "$owned_pid" ]] && owned=true && break
        done
        if ! $owned; then
            orphan_tunnels+=("$pid:$port")
        fi
    done < <(pgrep -af "cloudflared tunnel" 2>/dev/null || true)

    # Find all running bridge.py processes
    local orphan_bridges=()
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        local pid; pid=$(echo "$line" | awk '{print $1}')
        # Check if this PID is owned by a node
        local owned=false
        for owned_pid in "${owned_bridge_pids[@]}"; do
            [[ "$pid" == "$owned_pid" ]] && owned=true && break
        done
        if ! $owned; then
            orphan_bridges+=("$pid")
        fi
    done < <(pgrep -af "bridge.py" 2>/dev/null || true)

    # Report orphans
    if [[ ${#orphan_tunnels[@]} -gt 0 || ${#orphan_bridges[@]} -gt 0 ]]; then
        log ""
        log "$(red "⚠ ORPHAN PROCESSES DETECTED")"
        for orphan in "${orphan_tunnels[@]}"; do
            local pid="${orphan%%:*}" port="${orphan##*:}"
            log "  tunnel: PID $pid (port $port) - $(yellow "kill $pid")"
        done
        for pid in "${orphan_bridges[@]}"; do
            log "  bridge: PID $pid - $(yellow "kill $pid")"
        done
        log "  Fix: kill orphan processes or restart node"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Command: status
# ─────────────────────────────────────────────────────────────────────────────
cmd_status() {
    if $ALL_NODES; then
        # Show all nodes
        local all_nodes
        all_nodes=$(list_all_nodes)

        if [[ -z "$all_nodes" ]]; then
            log "No nodes configured"
            hint "Run: ./bridge.sh run"
            return 0
        fi

        log "$(bold "All Nodes")"
        log ""

        # First pass: show status and track running nodes by bot_id
        declare -A bot_nodes  # bot_id -> "node1 node2 ..."

        while IFS= read -r node; do
            [[ -n "$node" ]] || continue
            show_node_status "$node"
            log ""

            if is_node_running "$node"; then
                local node_dir; node_dir=$(get_node_dir "$node")
                local bid=""
                [[ -f "$node_dir/bot_id" ]] && bid=$(cat "$node_dir/bot_id")
                [[ -z "$bid" ]] && bid="unknown"
                bot_nodes["$bid"]+="$node "
            fi
        done <<< "$all_nodes"

        # Warn if multiple nodes running with same bot_id
        for bid in "${!bot_nodes[@]}"; do
            local nodes="${bot_nodes[$bid]}"
            local count
            count=$(echo "$nodes" | wc -w)
            if [[ $count -gt 1 ]]; then
                log "$(red "⚠ CONFLICT: $count nodes running with same bot (id:$bid)")"
                log "  Running: ${nodes% }"
                log "  Only ONE node receives webhook. Others miss messages."
                log "  Fix: Use different TELEGRAM_BOT_TOKEN per node, or stop extras."
            fi
        done
    else
        local node
        node=$(resolve_target_node)
        show_node_status "$node"
    fi

    # Always check for orphan processes
    detect_orphan_processes
}

show_node_status() {
    local node="$1"
    local node_dir tmux_prefix sessions_dir
    node_dir=$(get_node_dir "$node")
    tmux_prefix=$(get_node_tmux_prefix "$node")
    sessions_dir=$(get_node_sessions_dir "$node")

    local running=false port="" tunnel_url=""
    local hook_ok=false settings_ok=false token_ok=false bot_ok=false
    local bot_name="" bot_id="" webhook_url=""
    local claude_sessions=()
    # Comprehensive failure tracking — every silent failure mode gets a flag
    local bridge_http_ok=false  # bridge.py actually responding on port
    local tunnel_alive=false    # cloudflared process running
    local has_chat_id=false     # admin chat_id known (messages have a destination)
    local has_inbound=false     # at least one inbound path (webhook or poll)
    local pid_valid=false       # PID file points to actual bridge.py process
    local pending_updates=0     # webhook queue depth (stuck = messages delayed)
    local poll_running=false    # poll fallback process alive

    # Check if running
    if is_node_running "$node"; then
        running=true
        [[ -f "$node_dir/port" ]] && port=$(cat "$node_dir/port")
        [[ -f "$node_dir/tunnel_url" ]] && tunnel_url=$(cat "$node_dir/tunnel_url")
    fi

    # Validate PID actually belongs to bridge.py (not a recycled PID)
    if $running; then
        local pid_file; pid_file=$(get_node_pid_file "$node")
        local wrapper_pid; wrapper_pid=$(cat "$pid_file" 2>/dev/null || echo "")
        if [[ -n "$wrapper_pid" && -f "/proc/$wrapper_pid/cmdline" ]]; then
            local cmdline; cmdline=$(tr '\0' ' ' < "/proc/$wrapper_pid/cmdline" 2>/dev/null || true)
            if echo "$cmdline" | grep -qE 'bridge\.(sh|py)|python3'; then
                pid_valid=true
            fi
        fi
    fi

    # Bridge HTTP liveness probe — is bridge.py actually accepting connections?
    # Try BRIDGE_PUBLIC_URL first (bridge may bind Tailscale IP, not localhost).
    local bridge_host="localhost"
    if $running && [[ -n "$port" ]]; then
        local probe=""
        if [[ -n "${BRIDGE_PUBLIC_URL:-}" ]]; then
            bridge_host=$(echo "$BRIDGE_PUBLIC_URL" | sed -E 's|https?://([^:/]+).*|\1|')
            probe=$(curl -sf --max-time 3 "http://${bridge_host}:$port/health/workers" 2>/dev/null || echo "")
        fi
        if [[ -z "$probe" ]]; then
            bridge_host="localhost"
            probe=$(curl -sf --max-time 3 "http://localhost:$port/health/workers" 2>/dev/null || echo "")
        fi
        if [[ -n "$probe" ]] && echo "$probe" | grep -q '"workers"'; then
            bridge_http_ok=true
        fi
    fi

    # Check admin chat_id — without this, outbound messages have no destination
    # bridge.py writes to NODE_DIR/last_chat_id (not sessions_dir)
    if [[ -f "$node_dir/last_chat_id" ]]; then
        local chat_id_val; chat_id_val=$(cat "$node_dir/last_chat_id" 2>/dev/null || echo "")
        [[ -n "$chat_id_val" && "$chat_id_val" != "0" ]] && has_chat_id=true
    fi

    # Check tunnel process alive (when tunnel_url exists)
    if [[ -n "$tunnel_url" && -f "$node_dir/tunnel.pid" ]]; then
        local tun_pid; tun_pid=$(cat "$node_dir/tunnel.pid" 2>/dev/null || echo "")
        if [[ -n "$tun_pid" ]] && kill -0 "$tun_pid" 2>/dev/null; then
            tunnel_alive=true
        fi
    fi

    # Check poll fallback via bridge health endpoint (internal Python thread)
    if $bridge_http_ok; then
        local tunnel_health
        tunnel_health=$(curl -sf --max-time 3 "http://${bridge_host}:$port/health/tunnel" 2>/dev/null || echo '{}')
        if echo "$tunnel_health" | grep -q '"polling_active": true\|"polling_active":true'; then
            poll_running=true
        fi
    fi
    # Legacy: also check external poll process (bridge.sh-managed fallback)
    if ! $poll_running && pgrep -af "getUpdates.*bot" 2>/dev/null | grep -q "localhost:${port:-0}"; then
        poll_running=true
    fi

    # Find tmux sessions for this node
    if check_cmd tmux; then
        while IFS= read -r line; do
            [[ -n "$line" ]] && claude_sessions+=("$line")
        done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep "^${tmux_prefix}" || true)
    fi

    [[ -f "$HOOKS_DIR/$HOOK_SCRIPT" ]] && hook_ok=true
    [[ -f "$SETTINGS_FILE" ]] && grep -q "$HOOK_SCRIPT" "$SETTINGS_FILE" 2>/dev/null && settings_ok=true

    # Resolve token: source node env if TELEGRAM_BOT_TOKEN not already set
    local status_token="${TELEGRAM_BOT_TOKEN:-}"
    if [[ -z "$status_token" ]]; then
        local env_file="$HOME/.config/claudecode-telegram/${node}.env"
        if [[ -f "$env_file" ]]; then
            status_token=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$env_file" | head -1 | cut -d= -f2- | tr -d '"' || true)
        fi
    fi

    # Read cached bot info (display fallback when node is stopped)
    local saved_bot_id="" saved_bot_username=""
    [[ -f "$node_dir/bot_id" ]] && saved_bot_id=$(cat "$node_dir/bot_id")
    [[ -f "$node_dir/bot_username" ]] && saved_bot_username=$(cat "$node_dir/bot_username")

    # ALWAYS do a live getMe when we have a token — cached files can't detect
    # a revoked/changed token, which silently breaks all Telegram communication
    local token_live=false
    if [[ -n "$status_token" ]]; then
        token_ok=true
        local r; r=$(telegram_api "$status_token" "getMe" "{}" 2>/dev/null || echo '{}')
        if echo "$r" | grep -q '"ok":true'; then
            token_live=true
            bot_ok=true
            bot_name=$(echo "$r" | grep -o '"username":"[^"]*"' | cut -d'"' -f4)
            bot_id=$(echo "$r" | grep -o '"id":[0-9]*' | head -1 | cut -d: -f2)
        else
            # Token exists but is invalid — critical: bridge can't send to Telegram
            bot_ok=false
            bot_name="${saved_bot_username:-unknown}"
            bot_id="${saved_bot_id:-}"
        fi
    elif [[ -n "$saved_bot_id" && -n "$saved_bot_username" ]]; then
        # No token available, fall back to cached info (display only)
        bot_name="$saved_bot_username"
        bot_id="$saved_bot_id"
    fi

    # Get actual webhook from Telegram API (only possible with a live token)
    local webhook_error=""
    if $token_live; then
        local wr; wr=$(telegram_api "$status_token" "getWebhookInfo" "{}" 2>/dev/null || echo '{}')
        webhook_url=$(echo "$wr" | grep -o '"url":"[^"]*"' | cut -d'"' -f4)
        webhook_error=$(echo "$wr" | grep -o '"last_error_message":"[^"]*"' | cut -d'"' -f4 || true)
        pending_updates=$(echo "$wr" | grep -o '"pending_update_count":[0-9]*' | cut -d: -f2 || echo "0")
        [[ -z "$pending_updates" ]] && pending_updates=0
    fi

    # Determine if any inbound path exists
    if [[ -n "$webhook_url" ]] && [[ -z "$webhook_error" || "$webhook_error" == "null" ]]; then
        has_inbound=true
    fi
    $poll_running && has_inbound=true

    if $JSON_OUTPUT; then
        # Include all workers from bridge API (single source of truth)
        local workers_json="[]"
        if $bridge_http_ok; then
            workers_json=$(curl -sf --max-time 3 "http://${bridge_host}:$port/health/workers" 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(json.dumps(sorted(d.get('workers', {}).keys())))
except: print('[]')
" 2>/dev/null || echo "[]")
        fi
        # Fall back to local tmux sessions if bridge API unavailable
        if [[ "$workers_json" == "[]" ]] && [[ ${#claude_sessions[@]} -gt 0 ]]; then
            workers_json=$(printf '%s\n' "${claude_sessions[@]}" | sed "s/^${tmux_prefix}//" | jq -R . | jq -s .)
        fi
        cat << EOF
{"node":"$node","running":$running,"port":"$port","sessions":$workers_json,"hook":$hook_ok,"settings":$settings_ok,"token":$token_ok,"token_live":$token_live,"bot":"$bot_name","bot_id":"$bot_id","webhook":"$webhook_url","webhook_error":"${webhook_error:-}","bridge_http":$bridge_http_ok,"pid_valid":$pid_valid,"has_chat_id":$has_chat_id,"tunnel_alive":$tunnel_alive,"poll_running":$poll_running,"pending_updates":$pending_updates,"has_inbound":$has_inbound}
EOF
        return
    fi

    log "$(bold "Node: $node") $(if $running; then green "[running]"; else yellow "[stopped]"; fi)"

    if $running; then
        log "  port:     $port"
        [[ -n "$tunnel_url" ]] && log "  tunnel:   $tunnel_url"

        # Bridge HTTP liveness
        if $bridge_http_ok; then
            log "  bridge:   $(green "responding")"
        else
            log "  bridge:   $(red "NOT RESPONDING") — port $port not accepting HTTP"
            log "            bridge.py may have crashed internally"
            log "            fix: ./bridge.sh --node $node restart"
        fi

        # PID validation
        if ! $pid_valid; then
            log "  $(yellow "⚠ PID file may be stale (process is not bridge.sh/bridge.py)")"
        fi
    fi

    # Get settings.json mtime for stale config detection
    local settings_mtime=0
    if [[ -f "$SETTINGS_FILE" ]]; then
        settings_mtime=$(stat -c %Y "$SETTINGS_FILE" 2>/dev/null || stat -f %m "$SETTINGS_FILE" 2>/dev/null || echo 0)
    fi

    # Get all workers from bridge API (single source of truth for all machines)
    local all_workers="" all_worker_names=()
    if $bridge_http_ok; then
        all_workers=$(curl -sf --max-time 3 "http://${bridge_host}:$port/health/workers" 2>/dev/null || echo "")
        if [[ -n "$all_workers" ]]; then
            while IFS= read -r wname; do
                [[ -n "$wname" ]] && all_worker_names+=("$wname")
            done < <(echo "$all_workers" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    for name in sorted(d.get('workers', {}).keys()):
        print(name)
except: pass
" 2>/dev/null || true)
        fi
    fi

    # Fall back to local tmux sessions if bridge API unavailable
    if [[ ${#all_worker_names[@]} -eq 0 ]]; then
        for s in "${claude_sessions[@]}"; do
            all_worker_names+=("${s#"${tmux_prefix}"}")
        done
    fi

    # Build local tmux session lookup for per-worker checks
    local -A local_tmux=()
    for s in "${claude_sessions[@]}"; do
        local_tmux["${s#"${tmux_prefix}"}"]="$s"
    done

    if [[ ${#all_worker_names[@]} -gt 0 ]]; then
        log "  sessions: $(green "${#all_worker_names[@]} running")"

        local env_mismatch=false
        local stale_config=false
        for wname in "${all_worker_names[@]}"; do
            local issues=""
            local tmux_session="${local_tmux[$wname]:-}"

            # Get worker state from bridge API
            local wstate=""
            if [[ -n "$all_workers" ]]; then
                wstate=$(echo "$all_workers" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    w = d.get('workers', {}).get('$wname', {})
    print(w.get('state', ''))
except: pass
" 2>/dev/null || true)
            fi

            # Non-READY state is an issue for any worker (local or remote)
            if [[ -n "$wstate" && "$wstate" != "READY" ]]; then
                issues+="$(echo "$wstate" | tr '[:upper:]' '[:lower:]') "
            fi

            # Detailed checks for workers with local tmux sessions
            if [[ -n "$tmux_session" ]] && $running; then
                local tmux_port tmux_dir tmux_prefix_env tmux_bridge_url
                tmux_port=$(tmux show-environment -t "$tmux_session" PORT 2>/dev/null | cut -d= -f2- || true)
                tmux_dir=$(tmux show-environment -t "$tmux_session" SESSIONS_DIR 2>/dev/null | cut -d= -f2- || true)
                tmux_prefix_env=$(tmux show-environment -t "$tmux_session" TMUX_PREFIX 2>/dev/null | cut -d= -f2- || true)
                tmux_bridge_url=$(tmux show-environment -t "$tmux_session" BRIDGE_URL 2>/dev/null | cut -d= -f2- || true)

                [[ -n "$tmux_port" && "$tmux_port" != "$port" ]] && issues+="port "
                [[ -n "$tmux_dir" && "$tmux_dir" != "$sessions_dir" ]] && issues+="dir "
                [[ -n "$tmux_prefix_env" && "$tmux_prefix_env" != "$tmux_prefix" ]] && issues+="prefix "
                if [[ -n "$tmux_bridge_url" && -n "$port" && ! "$tmux_bridge_url" =~ :${port} ]]; then
                    issues+="bridge_url "
                fi
                echo "$issues" | grep -qE 'port|dir|prefix|bridge_url' && env_mismatch=true

                # Check if Claude started before settings.json was modified
                local pane_pid claude_pid claude_start=0
                pane_pid=$(tmux display-message -t "$tmux_session" -p '#{pane_pid}' 2>/dev/null || echo "")
                if [[ -n "$pane_pid" ]]; then
                    claude_pid=$(pgrep -P "$pane_pid" -f "claude" 2>/dev/null | head -1 || true)
                    if [[ -n "$claude_pid" ]]; then
                        claude_start=$(stat -c %Y "/proc/$claude_pid" 2>/dev/null || echo 0)
                    fi
                fi
                if [[ $settings_mtime -gt 0 && $claude_start -gt 0 && $settings_mtime -gt $claude_start ]]; then
                    issues+="stale-hooks "
                    stale_config=true
                fi
            fi

            if [[ -n "$issues" ]]; then
                log "            - ${wname} $(red "[${issues% }]")"
            else
                log "            - ${wname}"
            fi
        done

        if $env_mismatch; then
            log "  $(yellow "⚠ env mismatch: restart node to fix")"
        fi
        if $stale_config; then
            log "  $(yellow "⚠ stale-hooks: restart Claude (/exit) to reload settings.json")"
        fi
    else
        if $running; then
            log "  sessions: $(yellow "none") — bridge running but no workers to receive messages"
        else
            log "  sessions: $(yellow "none")"
        fi
    fi

    if $hook_ok; then log "  hook:     $(green "installed")"; else log "  hook:     $(yellow "not installed")"; fi
    if ! $settings_ok && $hook_ok; then
        log "  settings: $(yellow "hook not registered in settings.json")"
    fi
    if $bot_ok && $token_live; then
        log "  bot:      $(green "online") (@$bot_name, id:$bot_id)"
    elif $token_ok && ! $token_live; then
        log "  bot:      $(red "TOKEN INVALID") — bridge CANNOT send to Telegram"
        if [[ -n "$bot_name" ]]; then
            log "            cached: @$bot_name (id:$bot_id) — stale, token no longer works"
        fi
        log "            fix: update token in ~/.config/claudecode-telegram/${node}.env and restart"
    elif $token_ok; then
        log "  bot:      $(red "error")"
    else
        log "  bot:      $(yellow "not configured")"
    fi

    # Admin chat_id — without this, outbound messages silently drop
    if $running; then
        if $has_chat_id; then
            log "  chat_id:  $(green "set")"
        else
            log "  chat_id:  $(red "MISSING") — bridge has no Telegram chat to send to"
            log "            fix: send any message to bot from Telegram to register"
        fi
    fi

    # Webhook status
    if [[ -n "$webhook_url" ]]; then
        # Normalize: strip trailing /webhook for comparison (bridge accepts POST on /)
        local norm_webhook="${webhook_url%/webhook}"
        local norm_tunnel="${tunnel_url:-}"
        if [[ -n "$tunnel_url" && "$norm_webhook" != "$norm_tunnel" ]]; then
            log "  webhook:  $(yellow "mismatch") (pointing to different URL)"
            log "            actual:   $webhook_url"
            log "            expected: $tunnel_url"
        else
            log "  webhook:  $(green "set")"
        fi
        if [[ -n "$webhook_error" && "$webhook_error" != "null" ]]; then
            log "  webhook:  $(red "last error: $webhook_error")"
        fi
        if [[ $pending_updates -gt 0 ]]; then
            if [[ $pending_updates -gt 10 ]]; then
                log "  webhook:  $(red "⚠ $pending_updates pending updates") — messages stuck in Telegram queue"
            else
                log "  webhook:  $(yellow "$pending_updates pending updates")"
            fi
        fi
    elif $token_ok; then
        if $poll_running; then
            log "  webhook:  $(yellow "not set") (using poll fallback)"
        else
            log "  webhook:  $(yellow "not set")"
        fi
    fi

    # Tunnel process health (when tunnel_url is configured)
    if $running && [[ -n "$tunnel_url" ]]; then
        if $tunnel_alive; then
            log "  tunnel:   $(green "process alive")"
        else
            log "  tunnel:   $(red "PROCESS DEAD") — cloudflared not running"
            log "            Telegram can't reach bridge. Webhook will fail."
            if $poll_running; then
                log "            poll fallback: $(green "active") (messages still arriving)"
            else
                log "            $(red "NO INBOUND PATH") — messages from Telegram are lost"
                log "            fix: ./bridge.sh --node $node restart"
            fi
        fi
    fi

    # Poll fallback status
    if $running && $poll_running; then
        log "  polling:  $(green "active")"
    fi

    # Final health verdict — flag any combination that causes silent failure
    if $running; then
        if ! $has_inbound && $token_live; then
            if [[ -z "$tunnel_url" ]] && ! $poll_running; then
                log ""
                log "  $(red "✘ NO INBOUND PATH — Telegram messages cannot reach this bridge")"
                log "    Neither webhook nor poll fallback is active."
                log "    fix: ./bridge.sh --node $node restart"
            fi
        fi
        if ! $bridge_http_ok && $token_live; then
            log ""
            log "  $(red "✘ BRIDGE DEAD — HTTP server not responding on port $port")"
            log "    Hooks and webhook will fail. Workers can't communicate."
            log "    fix: ./bridge.sh --node $node restart"
        fi
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Command: webhook
# ─────────────────────────────────────────────────────────────────────────────
cmd_webhook() {
    local action="${1:-}"
    shift || true

    case "$action" in
        info)   cmd_webhook_info;;
        delete) cmd_webhook_delete;;
        "")     error "URL required"; hint "./bridge.sh webhook <url>"; exit 2;;
        *)      cmd_webhook_set "$action";;
    esac
}

# ─────────────────────────────────────────────────────────────────────────────
# Webhook subcommands
# ─────────────────────────────────────────────────────────────────────────────
cmd_webhook_set() {
    local url="$1"
    [[ "$url" =~ ^https:// ]] || { error "Webhook must use HTTPS"; exit 2; }

    local node
    node=$(resolve_target_node)

    local token; token=$(require_token)
    log "Setting webhook for node '$node': $url"

    local r; r=$(telegram_set_webhook "$token" "$url")
    if echo "$r" | grep -q '"ok":true'; then
        success "Webhook configured"
    else
        error "Failed to set webhook"
        echo "$r" >&2
        exit 1
    fi
}

cmd_webhook_info() {
    local node
    node=$(resolve_target_node)

    local token; token=$(require_token)
    local r; r=$(telegram_api "$token" "getWebhookInfo" "{}" 2>/dev/null || true)
    if [[ -z "$r" ]]; then
        warn "Webhook info unavailable (Telegram API error)"
        return 1
    fi
    local url; url=$(echo "$r" | grep -o '"url":"[^"]*"' | cut -d'"' -f4)
    local pending; pending=$(echo "$r" | grep -o '"pending_update_count":[0-9]*' | cut -d: -f2)

    log "Node: $node"
    if [[ -n "$url" ]]; then
        log "URL:     $url"
        log "Pending: ${pending:-0}"
    else
        log "No webhook configured"
    fi
}

cmd_webhook_delete() {
    local node
    node=$(resolve_target_node)

    local token; token=$(require_token)

    if ! $FORCE && ! $HEADLESS; then
        read -rp "Delete webhook for node '$node'? [y/N] " confirm
        [[ "$confirm" =~ ^[Yy] ]] || { log "Cancelled"; exit 0; }
    fi

    local r; r=$(telegram_api "$token" "deleteWebhook" "{}")
    if echo "$r" | grep -q '"ok":true'; then success "Webhook deleted"; else error "Failed"; exit 1; fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Command: hook
# ─────────────────────────────────────────────────────────────────────────────
cmd_hook() {
    local action="${1:-}"
    shift || true

    case "$action" in
        install)   cmd_hook_install "$@";;
        uninstall) cmd_hook_uninstall "$@";;
        test)      cmd_hook_test;;
        "")        error "Subcommand required"; hint "./bridge.sh hook <install|uninstall|test>"; exit 2;;
        *)         error "Unknown: hook $action"; exit 2;;
    esac
}

# ─────────────────────────────────────────────────────────────────────────────
# Hook subcommands
# ─────────────────────────────────────────────────────────────────────────────
cmd_hook_install() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -f|--force) FORCE=true; shift;;
            *) shift;;
        esac
    done

    local src="$SCRIPT_DIR/$HOOK_SCRIPT"
    local dst="$HOOKS_DIR/$HOOK_SCRIPT"

    [[ -f "$src" ]] || { error "Hook script not found: $src"; exit 1; }

    mkdir -p "$HOOKS_DIR"

    if [[ -f "$dst" ]] && ! $FORCE; then
        warn "Hook exists: $dst"
        hint "Use --force to overwrite"
        exit 1
    fi

    # Copy the single hook script (dispatches all events via subcommand)
    cp "$src" "$dst" && chmod 755 "$dst"
    success "Hook installed: $dst"

    mkdir -p "$CLAUDE_DIR"
    local hook_base="$HOME/.claude/hooks/$HOOK_SCRIPT"

    # Write settings.json — all three hook events point to the same script
    if check_cmd jq; then
        local hooks_json
        hooks_json=$(jq -n --arg h "$hook_base" '{
            Stop: [{hooks: [{type: "command", command: ($h + " stop")}]}],
            SessionStart: [{matcher: "compact|resume|init|start", hooks: [{type: "command", command: ($h + " start")}]}],
            PostToolUseFailure: [{hooks: [{type: "command", command: ($h + " tool-failure")}]}]
        }')

        if [[ -f "$SETTINGS_FILE" ]]; then
            jq --argjson hooks "$hooks_json" '.hooks = $hooks' \
                "$SETTINGS_FILE" > "$SETTINGS_FILE.tmp" \
                && mv "$SETTINGS_FILE.tmp" "$SETTINGS_FILE"
        else
            jq -n --argjson hooks "$hooks_json" '{hooks: $hooks}' > "$SETTINGS_FILE"
        fi
        success "Updated settings.json (all hooks → $HOOK_SCRIPT)"
    else
        warn "Install jq to auto-update settings.json"
    fi

    log ""
    log "$(bold "Note:") Single hook for all nodes. Reads config from env vars set by bridge."
}

cmd_hook_uninstall() {
    log "Uninstalling hooks..."

    # Remove main hook script
    local hook_file="$HOOKS_DIR/$HOOK_SCRIPT"
    if [[ -f "$hook_file" ]]; then
        rm -f "$hook_file"
        success "Removed: $hook_file"
    else
        log "$(dim "Hook file not found: $hook_file")"
    fi

    # Remove all hook entries from settings.json
    if [[ -f "$SETTINGS_FILE" ]] && check_cmd jq; then
        jq 'del(.hooks.Stop, .hooks.SessionStart, .hooks.PostToolUseFailure)' \
            "$SETTINGS_FILE" > "$SETTINGS_FILE.tmp" \
            && mv "$SETTINGS_FILE.tmp" "$SETTINGS_FILE"
        success "Removed hooks from settings.json"
    fi

    return 0
}

cmd_hook_test() {
    local node
    node=$(resolve_target_node)

    local token; token=$(require_token)
    local sessions_dir
    sessions_dir=$(get_node_sessions_dir "$node")

    local chat_id=""
    local chat_id_file=""

    if [[ -d "$sessions_dir" ]]; then
        chat_id_file=$(find "$sessions_dir" -name "chat_id" -type f -print0 2>/dev/null | xargs -0 ls -t 2>/dev/null | head -1)
        if [[ -n "$chat_id_file" ]]; then
            chat_id=$(cat "$chat_id_file")
        fi
    fi

    if [[ -z "$chat_id" ]]; then
        error "No chat ID found for node '$node'"
        hint "Send a message to your bot first, then retry"
        exit 1
    fi

    log "Sending test to chat $chat_id (node: $node)..."

    local r; r=$(telegram_api "$token" "sendMessage" "{\"chat_id\":\"$chat_id\",\"text\":\"Test OK from node $node!\"}")
    if echo "$r" | grep -q '"ok":true'; then success "Message sent"; else error "Failed"; exit 1; fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Command: setup — interactive first-run wizard
# ─────────────────────────────────────────────────────────────────────────────
cmd_setup() {
    echo ""
    bold "claudecode-telegram v${VERSION} — Setup"
    echo ""

    # ── Step 1: Check prerequisites ──
    echo "$(bold "Step 1/4") — Checking prerequisites"
    echo ""

    local missing=0 warnings=0

    # Python 3.12+
    if check_cmd python3; then
        local pyver
        pyver=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0")
        local pymajor pyminor
        pymajor=$(echo "$pyver" | cut -d. -f1)
        pyminor=$(echo "$pyver" | cut -d. -f2)
        if [[ "$pymajor" -ge 3 && "$pyminor" -ge 12 ]]; then
            success "Python $pyver"
        else
            echo "$(red "✗") Python $pyver (need 3.12+)"
            missing=$((missing + 1))
        fi
    else
        echo "$(red "✗") Python not found"
        hint "brew install python  (macOS) / apt install python3  (Linux)"
        missing=$((missing + 1))
    fi

    # tmux
    if check_cmd tmux; then
        success "tmux $(tmux -V 2>/dev/null | head -1 | sed 's/tmux //')"
    else
        echo "$(red "✗") tmux not found"
        hint "brew install tmux  (macOS) / apt install tmux  (Linux)"
        missing=$((missing + 1))
    fi

    # Node.js (for Claude CLI)
    if check_cmd node; then
        success "Node.js $(node --version 2>/dev/null)"
    else
        echo "$(red "✗") Node.js not found"
        hint "brew install node  (macOS) / see https://nodejs.org"
        missing=$((missing + 1))
    fi

    # Claude CLI
    if check_cmd claude; then
        success "Claude CLI"
    else
        echo "$(red "✗") Claude CLI not found"
        hint "npm install -g @anthropic-ai/claude-code"
        missing=$((missing + 1))
    fi

    # cloudflared (optional)
    if check_cmd cloudflared; then
        success "cloudflared (tunnel)"
    else
        echo "$(yellow "○") cloudflared not found (optional — needed for public webhook)"
        hint "brew install cloudflared  (or use --no-tunnel with your own URL)"
        warnings=$((warnings + 1))
    fi

    echo ""

    if [[ $missing -gt 0 ]]; then
        error "Missing $missing required tool(s) — install them and re-run ./bridge.sh setup"
        exit 4
    fi

    # ── Step 2: Bot token ──
    echo "$(bold "Step 2/4") — Telegram bot token"
    echo ""

    local token=""
    local node="${NODE_NAME:-prod}"
    local env_file="$HOME/.config/claudecode-telegram/${node}.env"

    # Check existing sources
    if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
        token="$TELEGRAM_BOT_TOKEN"
        success "Found in environment"
    elif [[ -f "$env_file" ]]; then
        # shellcheck disable=SC1090
        source "$env_file" 2>/dev/null || true
        token="${TELEGRAM_BOT_TOKEN:-}"
        if [[ -n "$token" ]]; then
            success "Found in $env_file"
        fi
    fi

    if [[ -z "$token" ]]; then
        echo "  No bot token found."
        echo ""
        echo "  $(dim "Get one from @BotFather on Telegram:")"
        echo "  $(dim "1. Open Telegram → search @BotFather → /newbot")"
        echo "  $(dim "2. Pick a name and username")"
        echo "  $(dim "3. Copy the token (looks like 123456789:AAE...)")"
        echo ""
        printf "  Paste your bot token: "
        read -r token
        echo ""

        if [[ -z "$token" ]]; then
            error "No token provided"
            exit 1
        fi

        # Validate token format
        if ! echo "$token" | grep -qE '^[0-9]+:[A-Za-z0-9_-]+$'; then
            error "Token doesn't look right (expected format: 123456789:AAE...)"
            exit 1
        fi
    fi

    # Save to config
    mkdir -p "$(dirname "$env_file")"
    echo "TELEGRAM_BOT_TOKEN=\"$token\"" > "$env_file"
    chmod 600 "$env_file"
    success "Token saved to $env_file"
    echo ""

    # ── Step 3: Install hooks ──
    echo "$(bold "Step 3/4") — Installing Claude Code hooks"
    echo ""
    FORCE=true cmd_hook_install
    echo ""

    # ── Step 4: Ready ──
    echo "$(bold "Step 4/4") — Ready!"
    echo ""
    echo "  $(green "Setup complete.") Start the bridge with:"
    echo ""
    echo "    $(bold "./bridge.sh run")"
    echo ""
    echo "  Then open Telegram and send $(bold "/hire myworker") to your bot."
    echo ""

    # Offer to start now
    if [[ "${1:-}" == "--start" ]]; then
        cmd_run
    else
        printf "  Start now? [Y/n] "
        read -r answer
        echo ""
        if [[ -z "$answer" || "$answer" =~ ^[Yy] ]]; then
            export TELEGRAM_BOT_TOKEN="$token"
            cmd_run
        fi
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Command: help
# ─────────────────────────────────────────────────────────────────────────────
cmd_help() {
    cat << 'EOF'
claudecode-telegram - Bridge Claude Code to Telegram (Multi-Node)

USAGE
  ./bridge.sh [flags] <command> [args]

QUICK START
  ./bridge.sh setup              # Interactive wizard (first time)
  ./bridge.sh run                # Start the bridge

MULTI-NODE
  NODE_NAME=prod ./bridge.sh run     # Start prod node
  NODE_NAME=dev ./bridge.sh run      # Start dev node
  ./bridge.sh --node prod stop       # Stop prod only
  ./bridge.sh --all status           # Status of all nodes

TELEGRAM COMMANDS
  /hire <name>      Create new Claude instance
  /focus <name>     Switch active Claude
  /team             List all instances
  /end <name>       Stop and remove instance
  /restart          Restart worker (--clean for fresh start)
  @name <msg>       One-off message to specific Claude
  <message>         Send to active Claude

SHELL COMMANDS
  setup             Interactive first-run wizard
  run               Start bridge + tunnel + webhook
  restart           Restart (preserves tmux sessions)
  stop              Stop node (bridge, tunnel, sessions)
  clean             Reset admin/chat_id (fixes stale config)
  status            Show current status
  webhook <url>     Set Telegram webhook URL
  webhook info      Show current webhook
  webhook delete    Remove webhook
  hook install      Install Claude Code hooks (Stop + SessionStart)
  hook uninstall    Remove hooks
  hook test         Send test message to Telegram

FLAGS
  -h, --help            Show help
  -V, --version         Show version
  -n, --node <name>     Target specific node
  --all                 Target all nodes (stop, status)
  -p, --port <port>     Bridge port (default: 8270)
  --no-tunnel           Skip tunnel/webhook (manual setup)
  --tunnel-url <url>    Use existing tunnel URL
  --headless            Non-interactive mode
  -q, --quiet           Suppress non-error output
  -v, --verbose         Debug output
  --json                JSON output (status)
  --no-color            Disable colors
  --env-file <path>     Load env from file
  -f, --force           Overwrite existing
ENVIRONMENT
  NODE_NAME               Target node (default: auto-detect or "prod")
  TELEGRAM_BOT_TOKEN      Bot token from @BotFather (required)
  PORT                    Server port (default: 8270)
  TUNNEL_URL              Pre-configured tunnel URL
  TELEGRAM_WEBHOOK_SECRET Webhook verification secret (optional)

DIRECTORY STRUCTURE
  ~/.claude/telegram/nodes/
  ├── prod/
  │   ├── pid             # Main process PID
  │   ├── bridge.pid      # Bridge process PID
  │   ├── tunnel.pid      # Tunnel process PID
  │   ├── port            # Current port
  │   ├── tunnel_url      # Current tunnel URL
  │   └── sessions/       # Per-session files
  └── dev/
      └── ...

EXIT CODES
  0  Success
  1  Runtime error
  2  Invalid usage
  3  Config error (missing token)
  4  Missing dependency
EOF
}

# ============================================================
# CONNECTOR MANAGEMENT
# ============================================================

cmd_connector() {
    local subcmd="${1:-status}"
    shift || true

    local node
    node=$(resolve_target_node)
    local port
    port=$(get_default_port "$node")

    # Check bridge is running
    if ! curl -sf "http://127.0.0.1:$port/" >/dev/null 2>&1; then
        error "Bridge not running on port $port (node: $node)"
        hint "./bridge.sh --node $node run"
        return 1
    fi

    case "$subcmd" in
        status)
            local result
            if ! result=$(curl -sf "http://127.0.0.1:$port/connectors" 2>&1); then
                error "Failed to reach bridge connectors endpoint"
                return 1
            fi

            log "$(bold "Connectors") (node: $node)"
            log ""

            # Parse JSON and display each connector
            echo "$result" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for name, info in data.items():
    running = info.get('running', False)
    enabled = info.get('enabled', True)
    if not enabled:
        status = '$(dim "disabled")'
    elif running:
        status = '$(green "running")'
    else:
        err = info.get('error', 'stopped')
        status = '$(red "stopped")' + f' ({err})' if err != 'stopped' else '$(red "stopped")'
    line = f'  {name:10s} {status}'
    failures = info.get('consecutive_failures', 0)
    if failures > 0:
        line += f'  failures={failures}'
    sender = info.get('sender_filter', '')
    if sender:
        line += f'  filter={sender}'
    interval = info.get('poll_interval', 0)
    if interval:
        line += f'  interval={interval}s'
    print(line)
" 2>/dev/null || echo "$result" | python3 -m json.tool
            ;;

        restart)
            local name="$1"
            if [[ -z "$name" ]]; then
                error "Usage: ./bridge.sh connector restart <gmail|github>"
                return 1
            fi

            log "Restarting $name connector..."
            local result
            if ! result=$(curl -sf -X POST "http://127.0.0.1:$port/connectors/restarts" \
                -H "Content-Type: application/json" \
                -d "{\"name\": \"$name\"}" 2>&1); then
                error "Failed to restart $name connector"
                echo "$result"
                return 1
            fi

            local ok
            ok=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('ok', False))" 2>/dev/null)
            local msg
            msg=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('message', ''))" 2>/dev/null)

            if [[ "$ok" == "True" ]]; then
                success "$name connector restarted: $msg"
            else
                error "$name connector restart failed: $msg"
                return 1
            fi
            ;;

        *)
            error "Unknown connector command: $subcmd"
            hint "Usage: ./bridge.sh connector status|restart <name>"
            return 1
            ;;
    esac
}

# ============================================================
# ARGUMENT PARSING + DISPATCH
# ============================================================

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

main() {
    # ─────────────────────────────────────────────────────────────────────────────
    # Global flag parsing
    # ─────────────────────────────────────────────────────────────────────────────
    while [[ $# -gt 0 ]]; do
        # shellcheck source=/dev/null
        case "$1" in
            --env-file=*) set -a; source "${1#*=}"; set +a; shift;;
            --env-file)   set -a; source "$2"; set +a; shift 2;;
            -h|--help)    cmd_help; exit 0;;
            -V|--version) echo "claudecode-telegram $VERSION (beastoin)"; exit 0;;
            -q|--quiet)   QUIET=true; shift;;
            -v|--verbose) VERBOSE=true; shift;;
            --json)       JSON_OUTPUT=true; shift;;
            --no-color)   NO_COLOR=true; shift;;
            --headless)   HEADLESS=true; shift;;
            -f|--force)   FORCE=true; shift;;
            -p=*|--port=*) PORT="${1#*=}"; shift;;
            -p|--port)     PORT="$2"; shift 2;;
            -n=*|--node=*) NODE_NAME="${1#*=}"; shift;;
            -n|--node)     NODE_NAME="$2"; shift 2;;
            --all)        ALL_NODES=true; shift;;
            -*)           error "Unknown flag: $1"; exit 2;;
            *)            break;;
        esac
    done

    # ─────────────────────────────────────────────────────────────────────────────
    # Command selection + dispatch
    # ─────────────────────────────────────────────────────────────────────────────
    local cmd="${1:-run}"
    shift || true

    case "$cmd" in
        run)     cmd_run "$@";;
        restart) cmd_restart "$@";;
        stop)    cmd_stop;;
        clean)   cmd_clean;;
        status)  cmd_status;;
        setup)   cmd_setup "$@";;
        webhook) cmd_webhook "$@";;
        hook)      cmd_hook "$@";;
        connector) cmd_connector "$@";;
        help)      cmd_help;;
        *)         error "Unknown command: $cmd"; hint "./bridge.sh --help"; exit 2;;
    esac
}

main "$@"
