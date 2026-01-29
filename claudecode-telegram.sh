#!/usr/bin/env bash
#
# claudecode-telegram - Bridge Claude Code to Telegram via webhooks
# Multi-node support: use NODE_NAME or --node to target specific nodes
#
set -euo pipefail

VERSION="0.9.6"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─────────────────────────────────────────────────────────────────────────────
# Environment variables (simplified in v0.9.5)
# ─────────────────────────────────────────────────────────────────────────────
# Required:
#   TELEGRAM_BOT_TOKEN  - Your bot token from @BotFather
#
# Optional:
#   ADMIN_CHAT_ID       - Pre-set admin (otherwise auto-learns first user)
#   TUNNEL_URL          - Use existing tunnel instead of starting cloudflared
#
# Internal (auto-derived, don't set manually):
#   PORT, SESSIONS_DIR, TMUX_PREFIX - Derived per node
# ─────────────────────────────────────────────────────────────────────────────

: "${PORT:=8080}"
: "${TUNNEL_URL:=}"

CLAUDE_DIR="$HOME/.claude"
HOOKS_DIR="$CLAUDE_DIR/hooks"
SETTINGS_FILE="$CLAUDE_DIR/settings.json"
NODES_DIR="$CLAUDE_DIR/telegram/nodes"
HOOK_SCRIPT="send-to-telegram.sh"

# CLI flags
VERBOSE=false
QUIET=false
NO_COLOR=false
NO_INPUT=false
JSON_OUTPUT=false
FORCE=false
ALL_NODES=false
NODE_NAME="${NODE_NAME:-}"

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

get_node_config() {
    local node="$1"
    echo "$(get_node_dir "$node")/config.env"
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

load_node_config() {
    local node="$1"
    local config_file
    config_file=$(get_node_config "$node")

    if [[ -f "$config_file" ]]; then
        debug "Loading config from $config_file"
        set -a
        # shellcheck source=/dev/null
        source "$config_file"
        set +a
    fi
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
    local count
    count=$(echo "$running_nodes" | grep -c . || echo 0)

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
        if $NO_INPUT || [[ ! -t 0 ]]; then
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

find_free_port() {
    local start="${1:-8080}"
    for p in $(seq "$start" $((start + 10))); do
        port_in_use "$p" || { echo "$p"; return 0; }
    done
    return 1
}

# Check if a process on a port is our bridge
is_our_bridge() {
    local port="$1"
    local pid
    pid=$(lsof -ti :"$port" 2>/dev/null | head -1)
    [[ -z "$pid" ]] && return 1

    # Check if process command contains bridge.py
    local cmd
    cmd=$(ps -p "$pid" -o args= 2>/dev/null || true)
    [[ "$cmd" == *"bridge.py"* ]]
}

# Handle port conflict with smart recovery
# Returns: 0 = port is now free, 1 = need alternative port
handle_port_conflict() {
    local port="$1"
    local node="$2"

    if ! port_in_use "$port"; then
        return 0  # Port is free
    fi

    # Check if it's our bridge
    if is_our_bridge "$port"; then
        log "Port $port is busy. Looks like an old bridge is still running."
        log "Restarting it for you..."

        local pid
        pid=$(lsof -ti :"$port" 2>/dev/null | head -1)

        # Graceful shutdown first
        kill "$pid" 2>/dev/null || true

        # Wait up to 3 seconds
        local wait=0
        while port_in_use "$port" && [[ $wait -lt 6 ]]; do
            sleep 0.5
            ((wait++))
        done

        # Force kill if still running
        if port_in_use "$port"; then
            kill -9 "$pid" 2>/dev/null || true
            sleep 1
        fi

        if port_in_use "$port"; then
            warn "Could not stop old bridge"
            return 1
        fi

        success "done"
        return 0
    else
        # Not our bridge - another app
        return 1
    fi
}

# Offer alternative port to user
offer_alternative_port() {
    local port="$1"
    local alt_port
    alt_port=$(find_free_port $((port + 1))) || { error "No free port found"; exit 1; }

    log "Port $port is already being used by another app."
    log "I won't stop other apps automatically."

    if ! $NO_INPUT && [[ -t 0 ]]; then
        read -rp "Would you like me to use port $alt_port instead? [Y/n] " confirm
        if [[ "$confirm" =~ ^[Nn] ]]; then
            log "No changes made. Try closing the other app, or run:"
            hint "./claudecode-telegram.sh start --port $alt_port"
            exit 0
        fi
        log "Okay—starting on port $alt_port..."
        echo "$alt_port"
    else
        error "Port $port is already being used by another app."
        hint "Try: ./claudecode-telegram.sh start --port $alt_port"
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
    curl -s -X POST "http://localhost:$port/notify" \
        -H "Content-Type: application/json" \
        -d "{\"text\":\"$message\"}" >/dev/null 2>&1 || true
}

start_tunnel() {
    local port="$1" log_file="$2"
    cloudflared tunnel --url "http://localhost:$port" > "$log_file" 2>&1 &
    echo $!
}

wait_for_tunnel_url() {
    local log_file="$1" timeout="${2:-30}"
    local url="" attempts=0
    while [[ -z "$url" && $attempts -lt $timeout ]]; do
        sleep 1
        url=$(grep -o 'https://[^[:space:]]*\.trycloudflare\.com' "$log_file" 2>/dev/null | head -1 || true)
        ((attempts++))
    done
    echo "$url"
}

is_tunnel_alive() {
    local pid="$1"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

is_tunnel_reachable() {
    local url="$1"
    [[ -n "$url" ]] && curl -s --max-time 5 "$url" >/dev/null 2>&1
}

# ─────────────────────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────────────────────

cmd_start() {
    local node
    node=$(resolve_target_node)
    local port="${PORT:-8080}" host="${HOST:-}"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -p|--port) port="$2"; shift 2;;
            --host) host="$2"; shift 2;;
            *) shift;;
        esac
    done

    # Load node config (may set TELEGRAM_BOT_TOKEN, PORT, etc.)
    load_node_config "$node"
    [[ -n "${PORT:-}" ]] && port="$PORT"

    local token; token=$(require_token)
    check_cmd tmux || { error "tmux not installed"; hint "brew install tmux"; exit 4; }

    # Check if port is available, try smart recovery
    if port_in_use "$port"; then
        if ! handle_port_conflict "$port" "$node"; then
            port=$(offer_alternative_port "$port")
        fi
    fi

    ensure_node_dir "$node"

    local sessions_dir tmux_prefix
    sessions_dir=$(get_node_sessions_dir "$node")
    tmux_prefix=$(get_node_tmux_prefix "$node")

    export TELEGRAM_BOT_TOKEN="$token" PORT="$port" NODE_NAME="$node"
    export SESSIONS_DIR="$sessions_dir" TMUX_PREFIX="$tmux_prefix"

    log "$(bold "Multi-Session Bridge") [$node] on $host:$port"
    log "$(dim "Sessions created via /new <name> from Telegram")"
    log "$(dim "Ctrl+C to stop")"
    exec python3 "$SCRIPT_DIR/bridge.py"
}

cmd_run() {
    local node
    node=$(resolve_target_node)
    local port="$PORT"
    local tunnel_url="$TUNNEL_URL"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -p|--port) port="$2"; shift 2;;
            --tunnel-url) tunnel_url="$2"; shift 2;;
            *) shift;;
        esac
    done

    # Load node config
    load_node_config "$node"
    [[ -n "${PORT:-}" ]] && port="$PORT"
    [[ -n "${TUNNEL_URL:-}" ]] && tunnel_url="$TUNNEL_URL"

    local token; token=$(require_token)

    # Check dependencies
    check_cmd tmux || { error "tmux not installed"; hint "brew install tmux"; exit 4; }
    check_cmd python3 || { error "python3 not installed"; exit 4; }

    if [[ -z "$tunnel_url" ]]; then
        check_cmd cloudflared || { error "cloudflared not installed"; hint "brew install cloudflared (or use --tunnel-url)"; exit 4; }
    fi

    # Check if this node is already running
    if is_node_running "$node"; then
        error "Node '$node' is already running"
        hint "Use: ./claudecode-telegram.sh --node $node restart"
        exit 1
    fi

    # Check if port is available, try smart recovery
    if port_in_use "$port"; then
        if ! handle_port_conflict "$port" "$node"; then
            port=$(offer_alternative_port "$port")
        fi
    fi

    ensure_node_dir "$node"

    local node_dir sessions_dir tmux_prefix pid_file
    node_dir=$(get_node_dir "$node")
    sessions_dir=$(get_node_sessions_dir "$node")
    tmux_prefix=$(get_node_tmux_prefix "$node")
    pid_file=$(get_node_pid_file "$node")

    log "$(bold "Starting Claude Code Telegram Bridge v${VERSION}")"
    log "$(bold "Node:") $node"
    log ""

    # 1. Install hook if needed
    if [[ ! -f "$HOOKS_DIR/$HOOK_SCRIPT" ]]; then
        log "Installing hook..."
        FORCE=true cmd_hook_install >/dev/null 2>&1 || true
        success "Hook installed"
    else
        log "$(dim "Hook already installed")"
    fi

    log "$(dim "No default session - use /new <name> from Telegram")"

    local tunnel_pid=""
    local tunnel_log=""

    # 2. Start tunnel (or use provided URL)
    if [[ -n "$tunnel_url" ]]; then
        log "$(dim "Using provided tunnel URL (no cloudflared started)")"
        success "Tunnel: $tunnel_url"
    else
        log "Starting tunnel..."
        tunnel_log="$node_dir/tunnel.log"
        tunnel_pid=$(start_tunnel "$port" "$tunnel_log")
        echo "$tunnel_pid" > "$node_dir/tunnel.pid"

        tunnel_url=$(wait_for_tunnel_url "$tunnel_log" 30)

        if [[ -z "$tunnel_url" ]]; then
            error "Could not get tunnel URL"
            kill "$tunnel_pid" 2>/dev/null || true
            exit 1
        fi

        success "Tunnel: $tunnel_url"
        echo "$tunnel_url" > "$node_dir/tunnel_url"
        sleep 3
    fi

    # 3. Start bridge server in background
    export TELEGRAM_BOT_TOKEN="$token" PORT="$port" NODE_NAME="$node"
    export SESSIONS_DIR="$sessions_dir" TMUX_PREFIX="$tmux_prefix"

    python3 "$SCRIPT_DIR/bridge.py" &
    local bridge_pid=$!
    echo "$bridge_pid" > "$node_dir/bridge.pid"
    echo "$port" > "$node_dir/port"
    sleep 1

    # 4. Set webhook
    local current_webhook=""
    local wr; wr=$(telegram_api "$token" "getWebhookInfo" "{}")
    current_webhook=$(echo "$wr" | grep -o '"url":"[^"]*"' | cut -d'"' -f4)

    if [[ "$current_webhook" == "$tunnel_url" ]]; then
        log "$(dim "Webhook already configured")"
        success "Webhook: $tunnel_url"
    else
        log "Setting webhook..."
        local r ok=false

        for delay in 0 1 2 5 15 30 60; do
            [[ $delay -gt 0 ]] && { log "Webhook not ready, retrying in ${delay}s..."; sleep "$delay"; }
            r=$(telegram_set_webhook "$token" "$tunnel_url")
            if echo "$r" | grep -q '"ok":true'; then
                ok=true
                break
            fi
        done

        log ""
        if $ok; then
            success "Webhook configured"
        else
            warn "Webhook setup failed (DNS may still be propagating)"
            hint "./claudecode-telegram.sh webhook $tunnel_url"
        fi
    fi

    log ""
    log "$(bold "Ready!") Send /new <name> to your bot to create a Claude instance"
    log ""
    log "$(bold "Commands:") /new /use /list /kill /status /stop /restart"
    log "$(dim "Ctrl+C to stop")"
    if [[ -n "$tunnel_pid" ]]; then
        log "$(dim "Tunnel watchdog: enabled (auto-restart on failure)")"
    fi
    log ""

    # Write main PID file
    echo $$ > "$pid_file"
    chmod 600 "$pid_file"
    log "$(dim "PID: $$ ($pid_file)")"

    # Cleanup on exit
    cleanup_and_exit() {
        log ""
        log "Shutting down node '$node'..."
        [[ -n "${tunnel_pid:-}" ]] && kill "$tunnel_pid" 2>/dev/null || true
        [[ -n "${bridge_pid:-}" ]] && kill "$bridge_pid" 2>/dev/null || true
        rm -f "$pid_file" "$node_dir/bridge.pid" "$node_dir/tunnel.pid" "$node_dir/tunnel.log" "$node_dir/tunnel_url" "$node_dir/port"
        exit 0
    }
    trap cleanup_and_exit EXIT INT TERM

    # 5. Watchdog loop
    while true; do
        if ! kill -0 "$bridge_pid" 2>/dev/null; then
            error "Bridge died unexpectedly"
            exit 1
        fi

        if [[ -n "$tunnel_pid" ]]; then
            local tunnel_problem=""
            if ! is_tunnel_alive "$tunnel_pid"; then
                tunnel_problem="process died"
            elif ! is_tunnel_reachable "$tunnel_url"; then
                tunnel_problem="unreachable"
                kill "$tunnel_pid" 2>/dev/null || true
            fi

            if [[ -n "$tunnel_problem" ]]; then
                warn "Tunnel $tunnel_problem, restarting..."
                bridge_notify "$port" "⚠️ Tunnel connection lost. Reconnecting..."

                tunnel_log="$node_dir/tunnel.log"
                tunnel_pid=$(start_tunnel "$port" "$tunnel_log")
                echo "$tunnel_pid" > "$node_dir/tunnel.pid"

                local new_url
                new_url=$(wait_for_tunnel_url "$tunnel_log" 30)

                if [[ -z "$new_url" ]]; then
                    error "Failed to restart tunnel"
                    bridge_notify "$port" "❌ Tunnel restart failed. Bridge may be offline."
                    exit 1
                fi

                tunnel_url="$new_url"
                echo "$tunnel_url" > "$node_dir/tunnel_url"
                success "Tunnel restarted: $tunnel_url"

                local webhook_ok=false
                local webhook_response=""
                for delay in 0 5 15 30 60 90 120 180; do
                    [[ $delay -gt 0 ]] && { log "Waiting ${delay}s for DNS..."; sleep "$delay"; }
                    webhook_response=$(telegram_set_webhook "$token" "$tunnel_url")
                    if echo "$webhook_response" | grep -q '"ok":true'; then
                        webhook_ok=true
                        break
                    fi
                    log "Webhook attempt failed: $webhook_response"
                done

                if $webhook_ok; then
                    success "Webhook updated"
                    bridge_notify "$port" "✅ Tunnel reconnected"
                else
                    warn "Webhook update failed after all retries"
                    log "Last response: $webhook_response"
                    bridge_notify "$port" "⚠️ Tunnel reconnected but webhook update failed. May need manual intervention."
                fi
            fi
        fi

        sleep 10
    done
}

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
            success "Main process stopped (PID $main_pid)"
            rm -f "$pid_file"
            sleep 1
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

    rm -f "$node_dir/port"

    if [[ $killed -eq 0 ]]; then
        log "Node '$node' was not running"
    else
        success "Node '$node' stopped"
    fi
}

cmd_restart() {
    if $ALL_NODES; then
        error "--all not supported for restart"
        hint "Restart nodes individually: ./claudecode-telegram.sh --node <name> restart"
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
            sleep 1
        fi
    fi

    # Fallback kills
    [[ -f "$node_dir/bridge.pid" ]] && kill "$(cat "$node_dir/bridge.pid")" 2>/dev/null || true
    [[ -f "$node_dir/tunnel.pid" ]] && kill "$(cat "$node_dir/tunnel.pid")" 2>/dev/null || true
    rm -f "$node_dir/bridge.pid" "$node_dir/tunnel.pid" "$node_dir/tunnel.log" "$node_dir/tunnel_url" "$node_dir/port"

    sleep 1

    # Start fresh
    log ""
    NODE_NAME="$node" cmd_run
}

cmd_status() {
    if $ALL_NODES; then
        # Show all nodes
        local all_nodes
        all_nodes=$(list_all_nodes)

        if [[ -z "$all_nodes" ]]; then
            log "No nodes configured"
            hint "Run: ./claudecode-telegram.sh run"
            return 0
        fi

        log "$(bold "All Nodes")"
        log ""

        while IFS= read -r node; do
            [[ -n "$node" ]] || continue
            show_node_status "$node"
            log ""
        done <<< "$all_nodes"
    else
        local node
        node=$(resolve_target_node)
        show_node_status "$node"
    fi
}

show_node_status() {
    local node="$1"
    local node_dir tmux_prefix sessions_dir
    node_dir=$(get_node_dir "$node")
    tmux_prefix=$(get_node_tmux_prefix "$node")
    sessions_dir=$(get_node_sessions_dir "$node")

    local running=false port="" tunnel_url=""
    local hook_ok=false settings_ok=false token_ok=false bot_ok=false
    local bot_name="" webhook_url=""
    local claude_sessions=()

    # Check if running
    if is_node_running "$node"; then
        running=true
        [[ -f "$node_dir/port" ]] && port=$(cat "$node_dir/port")
        [[ -f "$node_dir/tunnel_url" ]] && tunnel_url=$(cat "$node_dir/tunnel_url")
    fi

    # Find tmux sessions for this node
    if check_cmd tmux; then
        while IFS= read -r line; do
            [[ -n "$line" ]] && claude_sessions+=("$line")
        done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep "^${tmux_prefix}" || true)
    fi

    # Load node config for token check
    load_node_config "$node"

    [[ -f "$HOOKS_DIR/$HOOK_SCRIPT" ]] && hook_ok=true
    [[ -f "$SETTINGS_FILE" ]] && grep -q "$HOOK_SCRIPT" "$SETTINGS_FILE" 2>/dev/null && settings_ok=true

    if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
        token_ok=true
        local r; r=$(telegram_api "$TELEGRAM_BOT_TOKEN" "getMe" "{}")
        if echo "$r" | grep -q '"ok":true'; then
            bot_ok=true
            bot_name=$(echo "$r" | grep -o '"username":"[^"]*"' | cut -d'"' -f4)
        fi
        local wr; wr=$(telegram_api "$TELEGRAM_BOT_TOKEN" "getWebhookInfo" "{}")
        webhook_url=$(echo "$wr" | grep -o '"url":"[^"]*"' | cut -d'"' -f4)
    fi

    if $JSON_OUTPUT; then
        local sessions_json="[]"
        if [[ ${#claude_sessions[@]} -gt 0 ]]; then
            sessions_json=$(printf '%s\n' "${claude_sessions[@]}" | jq -R . | jq -s .)
        fi
        cat << EOF
{"node":"$node","running":$running,"port":"$port","sessions":$sessions_json,"hook":$hook_ok,"settings":$settings_ok,"token":$token_ok,"bot":"$bot_name","webhook":"$webhook_url"}
EOF
        return
    fi

    log "$(bold "Node: $node") $(if $running; then echo "$(green "[running]")"; else echo "$(yellow "[stopped]")"; fi)"

    if $running; then
        log "  port:     $port"
        [[ -n "$tunnel_url" ]] && log "  tunnel:   $tunnel_url"
    fi

    if [[ ${#claude_sessions[@]} -gt 0 ]]; then
        log "  sessions: $(green "${#claude_sessions[@]} running")"
        for s in "${claude_sessions[@]}"; do
            log "            - ${s#${tmux_prefix}}"
        done
    else
        log "  sessions: $(yellow "none")"
    fi

    $hook_ok && log "  hook:     $(green "installed")" || log "  hook:     $(yellow "not installed")"
    $token_ok && log "  token:    $(green "set") (${TELEGRAM_BOT_TOKEN:0:8}...)" || log "  token:    $(red "missing")"
    $bot_ok && log "  bot:      $(green "online") (@$bot_name)" || { $token_ok && log "  bot:      $(red "error")"; }
    [[ -n "$webhook_url" ]] && log "  webhook:  $(green "set")" || { $token_ok && log "  webhook:  $(yellow "not set")"; }
}

cmd_webhook() {
    local action="${1:-}"
    shift || true

    case "$action" in
        info)   cmd_webhook_info;;
        delete) cmd_webhook_delete;;
        "")     error "URL required"; hint "./claudecode-telegram.sh webhook <url>"; exit 2;;
        *)      cmd_webhook_set "$action";;
    esac
}

cmd_webhook_set() {
    local url="$1"
    [[ "$url" =~ ^https:// ]] || { error "Webhook must use HTTPS"; exit 2; }

    local node
    node=$(resolve_target_node)
    load_node_config "$node"

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
    load_node_config "$node"

    local token; token=$(require_token)
    local r; r=$(telegram_api "$token" "getWebhookInfo" "{}")
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
    load_node_config "$node"

    local token; token=$(require_token)

    if ! $FORCE && ! $NO_INPUT; then
        read -rp "Delete webhook for node '$node'? [y/N] " confirm
        [[ "$confirm" =~ ^[Yy] ]] || { log "Cancelled"; exit 0; }
    fi

    local r; r=$(telegram_api "$token" "deleteWebhook" "{}")
    echo "$r" | grep -q '"ok":true' && success "Webhook deleted" || { error "Failed"; exit 1; }
}

cmd_hook() {
    local action="${1:-}"
    shift || true

    case "$action" in
        install) cmd_hook_install "$@";;
        test)    cmd_hook_test;;
        "")      error "Subcommand required"; hint "./claudecode-telegram.sh hook <install|test>"; exit 2;;
        *)       error "Unknown: hook $action"; exit 2;;
    esac
}

cmd_hook_install() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -f|--force) FORCE=true; shift;;
            *) shift;;
        esac
    done

    local src="$SCRIPT_DIR/hooks/$HOOK_SCRIPT"
    local dst="$HOOKS_DIR/$HOOK_SCRIPT"

    [[ -f "$src" ]] || { error "Hook script not found: $src"; exit 1; }

    mkdir -p "$HOOKS_DIR"

    if [[ -f "$dst" ]] && ! $FORCE; then
        warn "Hook exists: $dst"
        hint "Use --force to overwrite"
        exit 1
    fi

    cp "$src" "$dst" && chmod 755 "$dst"
    success "Hook installed: $dst"

    mkdir -p "$CLAUDE_DIR"
    local hook_cmd="~/.claude/hooks/$HOOK_SCRIPT"

    if [[ -f "$SETTINGS_FILE" ]]; then
        if grep -q "$HOOK_SCRIPT" "$SETTINGS_FILE" 2>/dev/null; then
            log "$(dim "Already in settings.json")"
        elif check_cmd jq; then
            jq --argjson h '{"hooks":[{"type":"command","command":"'"$hook_cmd"'"}]}' \
                '.hooks.Stop += [$h]' "$SETTINGS_FILE" > "$SETTINGS_FILE.tmp" \
                && mv "$SETTINGS_FILE.tmp" "$SETTINGS_FILE"
            success "Updated settings.json"
        else
            warn "Install jq to auto-update settings.json"
        fi
    else
        cat > "$SETTINGS_FILE" << EOF
{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"$hook_cmd"}]}]}}
EOF
        success "Created settings.json"
    fi

    log ""
    log "$(bold "Note:") Hook forwards to bridge - no token needed in Claude session"
}

cmd_hook_test() {
    local node
    node=$(resolve_target_node)
    load_node_config "$node"

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
    echo "$r" | grep -q '"ok":true' && success "Message sent" || { error "Failed"; exit 1; }
}

cmd_setup() {
    local errors=0

    if $NO_INPUT; then
        log "$(bold "Headless Setup")"

        check_cmd tmux  || { error "tmux not installed"; ((errors++)); }
        check_cmd jq    || { error "jq not installed"; ((errors++)); }
        check_cmd curl  || { error "curl not installed"; ((errors++)); }
        check_cmd python3 || { error "python3 not installed"; ((errors++)); }
        [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]] || { error "TELEGRAM_BOT_TOKEN not set"; ((errors++)); }

        [[ $errors -gt 0 ]] && { error "$errors error(s)"; exit 3; }

        local r; r=$(telegram_api "$TELEGRAM_BOT_TOKEN" "getMe" "{}")
        echo "$r" | grep -q '"ok":true' || { error "Invalid token"; exit 3; }
        success "Token valid: @$(echo "$r" | grep -o '"username":"[^"]*"' | cut -d'"' -f4)"

        FORCE=true
        cmd_hook_install

        log "$(bold "Setup complete")"
        log "Run: ./claudecode-telegram.sh run"
    else
        log "$(bold "Setup")"
        log ""

        log "$(bold "Dependencies")"
        check_cmd tmux    && success "tmux"    || { error "tmux (required)"; hint "brew install tmux"; }
        check_cmd jq      && success "jq"      || warn "jq (optional)"
        check_cmd curl    && success "curl"    || error "curl (required)"
        check_cmd python3 && success "python3" || error "python3 (required)"
        check_cmd cloudflared && success "cloudflared" || warn "cloudflared (for tunnel)"
        log ""

        log "$(bold "Configuration")"
        if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
            success "Token: ${TELEGRAM_BOT_TOKEN:0:8}..."
            local r; r=$(telegram_api "$TELEGRAM_BOT_TOKEN" "getMe" "{}")
            echo "$r" | grep -q '"ok":true' \
                && success "Bot: @$(echo "$r" | grep -o '"username":"[^"]*"' | cut -d'"' -f4)" \
                || error "Token invalid"
        else
            warn "TELEGRAM_BOT_TOKEN not set"
        fi
        log ""

        log "$(bold "Hook")"
        [[ -f "$SCRIPT_DIR/hooks/$HOOK_SCRIPT" ]] && { cmd_hook_install 2>/dev/null || true; }
        log ""

        log "$(bold "Nodes")"
        local all_nodes
        all_nodes=$(list_all_nodes)
        if [[ -n "$all_nodes" ]]; then
            while IFS= read -r node; do
                if is_node_running "$node"; then
                    success "$node $(green "[running]")"
                else
                    log "  $node $(yellow "[stopped]")"
                fi
            done <<< "$all_nodes"
        else
            log "  $(dim "No nodes configured")"
        fi
        log ""

        log "$(bold "Quick Start")"
        log "  1. export TELEGRAM_BOT_TOKEN='...'"
        log "  2. ./claudecode-telegram.sh run"
        log "  3. Send /new backend to your bot on Telegram"
        log ""
        log "$(bold "Multi-Node")"
        log "  NODE_NAME=dev ./claudecode-telegram.sh run"
        log "  NODE_NAME=prod ./claudecode-telegram.sh run"
        log "  ./claudecode-telegram.sh --node dev stop"
        log "  ./claudecode-telegram.sh --all status"
    fi
}

cmd_help() {
    cat << 'EOF'
claudecode-telegram - Bridge Claude Code to Telegram (Multi-Node)

USAGE
  ./claudecode-telegram.sh [flags] <command> [args]

QUICK START
  export TELEGRAM_BOT_TOKEN='...'
  ./claudecode-telegram.sh run

MULTI-NODE
  NODE_NAME=prod ./claudecode-telegram.sh run     # Start prod node
  NODE_NAME=dev ./claudecode-telegram.sh run      # Start dev node
  ./claudecode-telegram.sh --node prod stop       # Stop prod only
  ./claudecode-telegram.sh --all status           # Status of all nodes

NODE CONFIGURATION
  Each node can have a config file at ~/.claude/telegram/nodes/<name>/config.env:
    TELEGRAM_BOT_TOKEN=...
    PORT=8080
    ADMIN_CHAT_ID=...

TELEGRAM COMMANDS
  /new <name>       Create new Claude instance
  /use <name>       Switch active Claude
  /list             List all instances
  /kill <name>      Stop and remove instance
  /status           Detailed status
  /stop             Interrupt active Claude
  @name <msg>       One-off message to specific Claude
  <message>         Send to active Claude

SHELL COMMANDS
  run               Start gateway + tunnel
  restart           Restart gateway (preserves tmux sessions)
  start             Start bridge only (manual setup)
  stop              Stop node (bridge, tunnel, tmux sessions)
  setup             Check deps, install hook, validate
  status            Show current status
  webhook <url>     Set Telegram webhook URL
  webhook info      Show current webhook
  webhook delete    Remove webhook
  hook install      Install Claude Code Stop hook
  hook test         Send test message to Telegram

FLAGS
  -h, --help            Show help
  -V, --version         Show version
  -n, --node <name>     Target specific node
  --all                 Target all nodes (stop, status)
  -q, --quiet           Suppress non-error output
  -v, --verbose         Show debug output
  --json                JSON output (status command)
  --no-color            Disable colors
  --no-input            Headless mode (strict, no prompts)
  --env-file <path>     Load environment from file
  -f, --force           Overwrite existing
  -p, --port <port>     Bridge port (default: 8080)
  --tunnel-url <url>    Use pre-configured tunnel URL

ENVIRONMENT
  NODE_NAME               Target node (default: auto-detect or "prod")
  TELEGRAM_BOT_TOKEN      Bot token from @BotFather (required)
  PORT                    Server port (default: 8080)
  TUNNEL_URL              Pre-configured tunnel URL
  TELEGRAM_WEBHOOK_SECRET Webhook verification secret (optional)

DIRECTORY STRUCTURE
  ~/.claude/telegram/nodes/
  ├── prod/
  │   ├── config.env      # Node configuration
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

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --env-file)  set -a; source "$2"; set +a; shift 2;;
            -h|--help)   cmd_help; exit 0;;
            -V|--version) echo "claudecode-telegram $VERSION (beastoin)"; exit 0;;
            -q|--quiet)  QUIET=true; shift;;
            -v|--verbose) VERBOSE=true; shift;;
            --json)      JSON_OUTPUT=true; shift;;
            --no-color)  NO_COLOR=true; shift;;
            --no-input)  NO_INPUT=true; shift;;
            -f|--force)  FORCE=true; shift;;
            -p|--port)   PORT="$2"; shift 2;;
            -n|--node)   NODE_NAME="$2"; shift 2;;
            --all)       ALL_NODES=true; shift;;
            -*)          error "Unknown flag: $1"; exit 2;;
            *)           break;;
        esac
    done

    local cmd="${1:-run}"
    shift || true

    case "$cmd" in
        run)     cmd_run "$@";;
        restart) cmd_restart "$@";;
        start)   cmd_start "$@";;
        stop)    cmd_stop;;
        setup)   cmd_setup;;
        status)  cmd_status;;
        webhook) cmd_webhook "$@";;
        hook)    cmd_hook "$@";;
        help)    cmd_help;;
        *)       error "Unknown command: $cmd"; hint "./claudecode-telegram.sh --help"; exit 2;;
    esac
}

main "$@"
