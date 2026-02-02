#!/usr/bin/env bash
#
# claudecode-telegram - Bridge Claude Code to Telegram via webhooks
# Multi-node support: use NODE_NAME or --node to target specific nodes
#
set -euo pipefail

VERSION="0.16.2"
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
# Sandbox mode (Docker isolation):
#   SANDBOX_ENABLED         - Set to "1" to run workers in Docker containers (default: 0)
#   SANDBOX_IMAGE           - Docker image name (default: claudecode-telegram:latest)
#   Use --mount/--mount-ro flags for additional mounts (CLI preferred over env)
#
# Derived (auto-set per node, don't set manually):
#   PORT, SESSIONS_DIR, TMUX_PREFIX
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
HEADLESS=false
JSON_OUTPUT=false
FORCE=false
ALL_NODES=false
NODE_NAME="${NODE_NAME:-}"
SANDBOX="${SANDBOX_ENABLED:-0}"  # Default: disabled (sandbox not stable yet)
SANDBOX_MOUNTS=""  # Extra mounts from --mount/--mount-ro flags

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
        prod) echo 8081 ;;
        dev)  echo 8082 ;;
        test) echo 8095 ;;
        *)    echo 8080 ;;
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
cmd_run() {
    local node
    node=$(resolve_target_node)

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

    if ! $no_tunnel && [[ -z "$tunnel_url" ]]; then
        check_cmd cloudflared || { error "cloudflared not installed"; hint "brew install cloudflared (or use --no-tunnel)"; exit 4; }
    fi

    # Check if this node is already running
    if is_node_running "$node"; then
        error "Node '$node' is already running"
        hint "Use: ./claudecode-telegram.sh --node $node restart"
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

    # 1. Install hook if needed (single hook for all nodes, reads env at runtime)
    if [[ ! -f "$HOOKS_DIR/$HOOK_SCRIPT" ]]; then
        log "Installing hook..."
        FORCE=true cmd_hook_install >/dev/null 2>&1 || true
        success "Hook installed"
    else
        log "$(dim "Hook already installed")"
    fi

    log "$(dim "No default session - use /new <name> from Telegram")"

    # Set up env vars for bridge
    export TELEGRAM_BOT_TOKEN="$token" PORT="$port"
    export SESSIONS_DIR="$sessions_dir" TMUX_PREFIX="$tmux_prefix"

    # Sandbox mode env vars
    export SANDBOX_ENABLED="${SANDBOX:-0}"
    export SANDBOX_IMAGE="${SANDBOX_IMAGE:-claudecode-telegram:latest}"
    export SANDBOX_MOUNTS="${SANDBOX_MOUNTS:-}"

    if [[ "$SANDBOX_ENABLED" == "1" ]]; then
        log "$(green "Sandbox mode enabled") - workers run in Docker containers"
        log "$(dim "Default mount: $HOME → /workspace")"
        if [[ -n "$SANDBOX_MOUNTS" ]]; then
            log "$(dim "Extra mounts: $SANDBOX_MOUNTS")"
        fi
        # Check if docker is available
        if ! command -v docker &>/dev/null; then
            warn "Docker not found - falling back to direct execution"
            export SANDBOX_ENABLED=0
        fi
    else
        log "$(yellow "Sandbox mode disabled") - workers run directly (--dangerously-skip-permissions)"
    fi

    local bridge_log="$node_dir/bridge.log"

    # No-tunnel mode: just run bridge in foreground
    if $no_tunnel; then
        log "$(dim "No tunnel (use external tunnel or local testing)")"
        log "$(dim "Ctrl+C to stop")"
        exec python3 -u "$SCRIPT_DIR/bridge.py" 2>&1 | tee -a "$bridge_log"
    fi

    local tunnel_pid=""
    local tunnel_log=""

    # Start tunnel (or use provided URL)
    if [[ -n "$tunnel_url" ]]; then
        log "$(dim "Using provided tunnel URL")"
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

    # Start bridge server in background
    python3 -u "$SCRIPT_DIR/bridge.py" >> "$bridge_log" 2>&1 &
    local bridge_pid=$!
    echo "$bridge_pid" > "$node_dir/bridge.pid"
    echo "$port" > "$node_dir/port"

    # Save bot info for status command
    local bot_info; bot_info=$(telegram_api "$token" "getMe" "{}")
    if echo "$bot_info" | grep -q '"ok":true'; then
        echo "$bot_info" | grep -o '"id":[0-9]*' | head -1 | cut -d: -f2 > "$node_dir/bot_id"
        echo "$bot_info" | grep -o '"username":"[^"]*"' | head -1 | cut -d'"' -f4 > "$node_dir/bot_username"
    fi
    sleep 1

    # Set webhook
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
            error "Webhook setup failed (DNS may still be propagating)"
            hint "Retry manually: ./claudecode-telegram.sh webhook $tunnel_url"
            # Cleanup before exit
            [[ -n "$bridge_pid" ]] && kill "$bridge_pid" 2>/dev/null
            [[ -n "$tunnel_pid" ]] && kill "$tunnel_pid" 2>/dev/null
            rm -f "$node_dir/bridge.pid" "$node_dir/tunnel.pid" "$node_dir/pid"
            exit 1
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
        rm -f "$pid_file" "$node_dir/bridge.pid" "$node_dir/tunnel.pid" "$node_dir/tunnel.log" "$node_dir/tunnel_url" "$node_dir/port" "$node_dir/bot_id" "$node_dir/bot_username"
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

    rm -f "$node_dir/port" "$node_dir/bot_id" "$node_dir/bot_username"

    if [[ $killed -eq 0 ]]; then
        log "Node '$node' was not running"
    else
        success "Node '$node' stopped"
    fi
}

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
    rm -f "$node_dir/bridge.pid" "$node_dir/tunnel.pid" "$node_dir/tunnel.log" "$node_dir/tunnel_url" "$node_dir/port" "$node_dir/bot_id" "$node_dir/bot_username"

    sleep 1

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
            local count=$(echo "$nodes" | wc -w)
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

    [[ -f "$HOOKS_DIR/$HOOK_SCRIPT" ]] && hook_ok=true
    [[ -f "$SETTINGS_FILE" ]] && grep -q "$HOOK_SCRIPT" "$SETTINGS_FILE" 2>/dev/null && settings_ok=true

    # Read bot info from saved files (set at node start) or fall back to env token
    local saved_bot_id="" saved_bot_username=""
    [[ -f "$node_dir/bot_id" ]] && saved_bot_id=$(cat "$node_dir/bot_id")
    [[ -f "$node_dir/bot_username" ]] && saved_bot_username=$(cat "$node_dir/bot_username")

    if [[ -n "$saved_bot_id" && -n "$saved_bot_username" ]]; then
        token_ok=true
        bot_ok=true
        bot_name="$saved_bot_username"
        bot_id="$saved_bot_id"
    elif [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
        token_ok=true
        local r; r=$(telegram_api "$TELEGRAM_BOT_TOKEN" "getMe" "{}")
        if echo "$r" | grep -q '"ok":true'; then
            bot_ok=true
            bot_name=$(echo "$r" | grep -o '"username":"[^"]*"' | cut -d'"' -f4)
            bot_id=$(echo "$r" | grep -o '"id":[0-9]*' | head -1 | cut -d: -f2)
        fi
    fi

    # Get actual webhook from Telegram API (not from local file)
    if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
        local wr; wr=$(telegram_api "$TELEGRAM_BOT_TOKEN" "getWebhookInfo" "{}")
        webhook_url=$(echo "$wr" | grep -o '"url":"[^"]*"' | cut -d'"' -f4)
    elif [[ -n "$saved_bot_id" ]]; then
        # No token available, can't check - show as unknown
        webhook_url=""
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

    # Get settings.json mtime for stale config detection
    local settings_mtime=0
    if [[ -f "$SETTINGS_FILE" ]]; then
        settings_mtime=$(stat -c %Y "$SETTINGS_FILE" 2>/dev/null || stat -f %m "$SETTINGS_FILE" 2>/dev/null || echo 0)
    fi

    if [[ ${#claude_sessions[@]} -gt 0 ]]; then
        log "  sessions: $(green "${#claude_sessions[@]} running")"
        local env_mismatch=false
        local stale_config=false
        for s in "${claude_sessions[@]}"; do
            local session_name="${s#${tmux_prefix}}"
            local issues=""

            # Check tmux env vars match node config
            if $running; then
                local tmux_port tmux_dir tmux_prefix_env
                tmux_port=$(tmux show-environment -t "$s" PORT 2>/dev/null | cut -d= -f2- || true)
                tmux_dir=$(tmux show-environment -t "$s" SESSIONS_DIR 2>/dev/null | cut -d= -f2- || true)
                tmux_prefix_env=$(tmux show-environment -t "$s" TMUX_PREFIX 2>/dev/null | cut -d= -f2- || true)

                [[ -n "$tmux_port" && "$tmux_port" != "$port" ]] && issues+="port "
                [[ -n "$tmux_dir" && "$tmux_dir" != "$sessions_dir" ]] && issues+="dir "
                [[ -n "$tmux_prefix_env" && "$tmux_prefix_env" != "$tmux_prefix" ]] && issues+="prefix "

                if [[ -n "$issues" ]]; then
                    env_mismatch=true
                fi
            fi

            # Check if Claude started before settings.json was modified
            local pane_pid claude_pid claude_start=0
            pane_pid=$(tmux display-message -t "$s" -p '#{pane_pid}' 2>/dev/null || echo "")
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

            if [[ -n "$issues" ]]; then
                log "            - ${session_name} $(red "[${issues% }]")"
            else
                log "            - ${session_name}"
            fi
        done

        if $env_mismatch; then
            log "  $(yellow "⚠ env mismatch: restart node to fix")"
        fi
        if $stale_config; then
            log "  $(yellow "⚠ stale-hooks: restart Claude (/exit) to reload settings.json")"
        fi
    else
        log "  sessions: $(yellow "none")"
    fi

    $hook_ok && log "  hook:     $(green "installed")" || log "  hook:     $(yellow "not installed")"
    if $bot_ok; then
        log "  bot:      $(green "online") (@$bot_name, id:$bot_id)"
    elif $token_ok; then
        log "  bot:      $(red "error")"
    else
        log "  bot:      $(yellow "not configured")"
    fi
    if [[ -n "$webhook_url" ]]; then
        if [[ -n "$tunnel_url" && "$webhook_url" != "$tunnel_url" ]]; then
            log "  webhook:  $(yellow "mismatch") (pointing to different URL)"
            log "            actual:   $webhook_url"
            log "            expected: $tunnel_url"
        else
            log "  webhook:  $(green "set")"
        fi
    elif $token_ok; then
        log "  webhook:  $(yellow "not set")"
    fi
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

    local token; token=$(require_token)

    if ! $FORCE && ! $HEADLESS; then
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
        install)   cmd_hook_install "$@";;
        uninstall) cmd_hook_uninstall "$@";;
        test)      cmd_hook_test;;
        "")        error "Subcommand required"; hint "./claudecode-telegram.sh hook <install|uninstall|test>"; exit 2;;
        *)         error "Unknown: hook $action"; exit 2;;
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

    # Just copy the hook (reads env vars at runtime, set by bridge)
    cp "$src" "$dst" && chmod 755 "$dst"
    success "Hook installed: $dst"

    # Also copy helper script
    local helper_src="$SCRIPT_DIR/hooks/forward-to-bridge.py"
    local helper_dst="$HOOKS_DIR/forward-to-bridge.py"
    if [[ -f "$helper_src" ]]; then
        cp "$helper_src" "$helper_dst" && chmod 755 "$helper_dst"
    fi

    mkdir -p "$CLAUDE_DIR"
    local hook_cmd="$HOME/.claude/hooks/$HOOK_SCRIPT"

    # Add to settings.json
    # Claude Code hooks structure: hooks.Stop[].hooks[] (matcher group with hooks array)
    if [[ -f "$SETTINGS_FILE" ]]; then
        if grep -q "$HOOK_SCRIPT" "$SETTINGS_FILE" 2>/dev/null; then
            log "$(dim "Already in settings.json")"
        elif check_cmd jq; then
            if jq -e '.hooks.Stop[0].hooks' "$SETTINGS_FILE" >/dev/null 2>&1; then
                jq --arg cmd "$hook_cmd" \
                    '.hooks.Stop[0].hooks += [{"type":"command","command":$cmd}]' \
                    "$SETTINGS_FILE" > "$SETTINGS_FILE.tmp" \
                    && mv "$SETTINGS_FILE.tmp" "$SETTINGS_FILE"
            else
                jq --arg cmd "$hook_cmd" \
                    '.hooks.Stop = [{"hooks":[{"type":"command","command":$cmd}]}]' \
                    "$SETTINGS_FILE" > "$SETTINGS_FILE.tmp" \
                    && mv "$SETTINGS_FILE.tmp" "$SETTINGS_FILE"
            fi
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
    log "$(bold "Note:") Single hook for all nodes. Reads config from env vars set by bridge."
}

cmd_hook_uninstall() {
    local hook_file="$HOOKS_DIR/$HOOK_SCRIPT"

    log "Uninstalling hook..."

    # Remove hook file
    if [[ -f "$hook_file" ]]; then
        rm -f "$hook_file"
        success "Removed: $hook_file"
    else
        log "$(dim "Hook file not found: $hook_file")"
    fi

    # Remove helper
    rm -f "$HOOKS_DIR/forward-to-bridge.py"

    # Remove from settings.json
    if [[ -f "$SETTINGS_FILE" ]] && check_cmd jq; then
        if grep -q "$HOOK_SCRIPT" "$SETTINGS_FILE" 2>/dev/null; then
            jq --arg hook "$HOOK_SCRIPT" '
                .hooks.Stop[0].hooks = [.hooks.Stop[0].hooks[] | select(.command | contains($hook) | not)]
            ' "$SETTINGS_FILE" > "$SETTINGS_FILE.tmp" \
                && mv "$SETTINGS_FILE.tmp" "$SETTINGS_FILE"
            success "Removed from settings.json"
        else
            log "$(dim "Not in settings.json")"
        fi
    fi
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
    echo "$r" | grep -q '"ok":true' && success "Message sent" || { error "Failed"; exit 1; }
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
  run               Start bridge + tunnel + webhook
  restart           Restart (preserves tmux sessions)
  stop              Stop node (bridge, tunnel, sessions)
  clean             Reset admin/chat_id (fixes stale config)
  status            Show current status
  webhook <url>     Set Telegram webhook URL
  webhook info      Show current webhook
  webhook delete    Remove webhook
  hook install      Install Claude Code Stop hook
  hook uninstall    Remove hook
  hook test         Send test message to Telegram

FLAGS
  -h, --help            Show help
  -V, --version         Show version
  -n, --node <name>     Target specific node
  --all                 Target all nodes (stop, status)
  -p, --port <port>     Bridge port (default: 8080)
  --no-tunnel           Skip tunnel/webhook (manual setup)
  --tunnel-url <url>    Use existing tunnel URL
  --headless            Non-interactive mode
  -q, --quiet           Suppress non-error output
  -v, --verbose         Debug output
  --json                JSON output (status)
  --no-color            Disable colors
  --env-file <path>     Load env from file
  -f, --force           Overwrite existing
  --sandbox             Run workers in Docker (default: disabled)
  --no-sandbox          Run workers directly (--dangerously-skip-permissions)
  --sandbox-image <img> Docker image (default: claudecode-telegram:latest)
  --mount <path>        Extra mount (host:container or just path)
  --mount-ro <path>     Extra mount, read-only

ENVIRONMENT
  NODE_NAME               Target node (default: auto-detect or "prod")
  TELEGRAM_BOT_TOKEN      Bot token from @BotFather (required)
  PORT                    Server port (default: 8080)
  TUNNEL_URL              Pre-configured tunnel URL
  TELEGRAM_WEBHOOK_SECRET Webhook verification secret (optional)
  SANDBOX_ENABLED         Enable sandbox mode (1/0, default: 0)
  SANDBOX_IMAGE           Docker image for workers

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

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

main() {
    while [[ $# -gt 0 ]]; do
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
            --sandbox)    SANDBOX=1; shift;;
            --no-sandbox) SANDBOX=0; shift;;
            --sandbox-image=*) SANDBOX_IMAGE="${1#*=}"; shift;;
            --sandbox-image)   SANDBOX_IMAGE="$2"; shift 2;;
            --mount=*)    SANDBOX_MOUNTS="${SANDBOX_MOUNTS:+$SANDBOX_MOUNTS,}${1#*=}"; shift;;
            --mount)      SANDBOX_MOUNTS="${SANDBOX_MOUNTS:+$SANDBOX_MOUNTS,}$2"; shift 2;;
            --mount-ro=*) SANDBOX_MOUNTS="${SANDBOX_MOUNTS:+$SANDBOX_MOUNTS,}ro:${1#*=}"; shift;;
            --mount-ro)   SANDBOX_MOUNTS="${SANDBOX_MOUNTS:+$SANDBOX_MOUNTS,}ro:$2"; shift 2;;
            -*)           error "Unknown flag: $1"; exit 2;;
            *)            break;;
        esac
    done

    local cmd="${1:-run}"
    shift || true

    case "$cmd" in
        run)     cmd_run "$@";;
        restart) cmd_restart "$@";;
        stop)    cmd_stop;;
        clean)   cmd_clean;;
        status)  cmd_status;;
        webhook) cmd_webhook "$@";;
        hook)    cmd_hook "$@";;
        help)    cmd_help;;
        *)       error "Unknown command: $cmd"; hint "./claudecode-telegram.sh --help"; exit 2;;
    esac
}

main "$@"
