#!/usr/bin/env bash
#
# claudecode-telegram - Bridge Claude Code to Telegram via webhooks
#
set -euo pipefail

VERSION="0.3.1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

: "${PORT:=8080}"
: "${HOST:=0.0.0.0}"

CLAUDE_DIR="$HOME/.claude"
HOOKS_DIR="$CLAUDE_DIR/hooks"
SETTINGS_FILE="$CLAUDE_DIR/settings.json"
SESSIONS_DIR="$CLAUDE_DIR/telegram/sessions"
HOOK_SCRIPT="send-to-telegram.sh"

VERBOSE=false
QUIET=false
NO_COLOR=false
NO_INPUT=false
JSON_OUTPUT=false
FORCE=false

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
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

require_token() {
    [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]] || { error "TELEGRAM_BOT_TOKEN not set"; exit 3; }
    echo "$TELEGRAM_BOT_TOKEN"
}

check_cmd() { command -v "$1" &>/dev/null; }

port_in_use() {
    # nc -z is the most reliable cross-platform port check
    nc -z localhost "$1" 2>/dev/null
}

find_free_port() {
    local start="${1:-8080}"
    for p in $(seq "$start" $((start + 10))); do
        port_in_use "$p" || { echo "$p"; return 0; }
    done
    return 1
}

telegram_api() {
    local token="$1" method="$2" data="$3"
    curl -s -X POST "https://api.telegram.org/bot${token}/${method}" \
        -H "Content-Type: application/json" -d "$data"
}

telegram_set_webhook() {
    local token="$1" url="$2"
    # Add secret_token if TELEGRAM_WEBHOOK_SECRET is set (optional security)
    if [[ -n "${TELEGRAM_WEBHOOK_SECRET:-}" ]]; then
        curl -s "https://api.telegram.org/bot${token}/setWebhook?url=${url}&secret_token=${TELEGRAM_WEBHOOK_SECRET}"
    else
        curl -s "https://api.telegram.org/bot${token}/setWebhook?url=${url}"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────────────────────

cmd_start() {
    local port="$PORT" host="$HOST"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -p|--port) port="$2"; shift 2;;
            --host) host="$2"; shift 2;;
            *) shift;;
        esac
    done

    local token; token=$(require_token)
    check_cmd tmux || { error "tmux not installed"; hint "brew install tmux"; exit 4; }

    # Check if port is available
    if port_in_use "$port"; then
        local alt_port
        alt_port=$(find_free_port $((port + 1))) || { error "No free port found"; exit 1; }
        warn "Port $port is already in use"
        if ! $NO_INPUT; then
            read -rp "Use port $alt_port instead? [Y/n] " confirm
            [[ "$confirm" =~ ^[Nn] ]] && { log "Cancelled"; exit 0; }
        else
            log "Using port $alt_port"
        fi
        port="$alt_port"
    fi

    export TELEGRAM_BOT_TOKEN="$token" PORT="$port"
    log "$(bold "Multi-Session Bridge") on $host:$port"
    log "$(dim "Sessions created via /new <name> from Telegram")"
    log "$(dim "Ctrl+C to stop")"
    exec python3 "$SCRIPT_DIR/bridge.py"
}

cmd_run() {
    local port="$PORT"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -p|--port) port="$2"; shift 2;;
            *) shift;;
        esac
    done

    local token; token=$(require_token)

    # Check dependencies
    check_cmd tmux || { error "tmux not installed"; hint "brew install tmux"; exit 4; }
    check_cmd cloudflared || { error "cloudflared not installed"; hint "brew install cloudflared"; exit 4; }
    check_cmd python3 || { error "python3 not installed"; exit 4; }

    # Check if port is available
    if port_in_use "$port"; then
        local alt_port
        alt_port=$(find_free_port $((port + 1))) || { error "No free port found"; exit 1; }
        warn "Port $port is already in use"
        if ! $NO_INPUT; then
            read -rp "Use port $alt_port instead? [Y/n] " confirm
            [[ "$confirm" =~ ^[Nn] ]] && { log "Cancelled"; exit 0; }
        else
            log "Using port $alt_port"
        fi
        port="$alt_port"
    fi

    log "$(bold "Starting Claude Code Telegram Bridge (Multi-Session)")"
    log ""

    # 1. Install hook if needed
    if [[ ! -f "$HOOKS_DIR/$HOOK_SCRIPT" ]]; then
        log "Installing hook..."
        FORCE=true cmd_hook_install >/dev/null 2>&1 || true
        success "Hook installed"
    else
        log "$(dim "Hook already installed")"
    fi

    # 2. Multi-session mode: Don't create default session
    # Sessions are created via /new command from Telegram
    log "$(dim "No default session - use /new <name> from Telegram")"

    # 3. Start cloudflared tunnel in background
    log "Starting tunnel..."
    local tunnel_log="/tmp/cloudflared-$$.log"
    cloudflared tunnel --url "http://localhost:$port" > "$tunnel_log" 2>&1 &
    local tunnel_pid=$!

    # Wait for tunnel URL
    local tunnel_url="" attempts=0
    while [[ -z "$tunnel_url" && $attempts -lt 30 ]]; do
        sleep 1
        tunnel_url=$(grep -o 'https://[^[:space:]]*\.trycloudflare\.com' "$tunnel_log" 2>/dev/null | head -1 || true)
        ((++attempts))
    done

    if [[ -z "$tunnel_url" ]]; then
        error "Could not get tunnel URL"
        kill $tunnel_pid 2>/dev/null || true
        exit 1
    fi

    success "Tunnel: $tunnel_url"

    # Give tunnel time to fully establish before webhook setup
    sleep 3

    # 4. Start bridge server in background (must be listening before webhook setup)
    export TELEGRAM_BOT_TOKEN="$token" PORT="$port"
    python3 "$SCRIPT_DIR/bridge.py" &
    local bridge_pid=$!
    sleep 1  # Give server time to start

    # 5. Set webhook (retry with backoff for DNS propagation)
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
        log "$(bold "Ready!") Send /new <name> to your bot to create a Claude instance"
    else
        warn "Webhook setup failed (DNS may still be propagating)"
        log "Bridge and tunnel are still running. Retry manually:"
        hint "./claudecode-telegram.sh webhook $tunnel_url"
    fi
    log ""
    log "$(bold "Commands:") /new /use /list /kill /status /stop"
    log "$(dim "Ctrl+C to stop")"
    log ""

    # Cleanup on exit
    trap 'kill $tunnel_pid $bridge_pid 2>/dev/null; rm -f "$tunnel_log"' EXIT

    # 6. Wait for bridge (bring to foreground)
    wait $bridge_pid
}

cmd_stop() {
    log "Stopping everything..."

    local killed=0

    # Kill bridge
    if pkill -f "bridge.py" 2>/dev/null; then
        ((killed++))
        success "Bridge stopped"
    fi

    # Kill cloudflared tunnel
    if pkill -f "cloudflared tunnel" 2>/dev/null; then
        ((killed++))
        success "Tunnel stopped"
    fi

    # Kill all claude-* tmux sessions
    local sessions
    sessions=$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^claude-' || true)
    if [[ -n "$sessions" ]]; then
        while IFS= read -r session; do
            if tmux kill-session -t "$session" 2>/dev/null; then
                ((killed++))
                success "Killed tmux session '$session'"
            fi
        done <<< "$sessions"
    fi

    # Clean up temp files
    rm -f /tmp/cloudflared-*.log 2>/dev/null

    if [[ $killed -eq 0 ]]; then
        log "Nothing was running"
    else
        success "Cleanup complete"
    fi
}

cmd_status() {
    local hook_ok=false settings_ok=false token_ok=false bot_ok=false
    local bot_name="" webhook_url=""
    local claude_sessions=()

    # Find all claude-* tmux sessions
    if check_cmd tmux; then
        while IFS= read -r line; do
            [[ -n "$line" ]] && claude_sessions+=("$line")
        done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^claude-' || true)
    fi

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
{"sessions":$sessions_json,"hook":$hook_ok,"settings":$settings_ok,"token":$token_ok,"bot":"$bot_name","webhook":"$webhook_url"}
EOF
        return
    fi

    log "$(bold "Status (Multi-Session Mode)")"
    if [[ ${#claude_sessions[@]} -gt 0 ]]; then
        log "  sessions: $(green "${#claude_sessions[@]} running")"
        for s in "${claude_sessions[@]}"; do
            log "            - ${s#claude-}"
        done
    else
        log "  sessions: $(yellow "none") (use /new <name> from Telegram)"
    fi
    $hook_ok && log "  hook:     $(green "installed")" || log "  hook:     $(yellow "not installed")"
    $settings_ok && log "  settings: $(green "configured")" || log "  settings: $(yellow "not configured")"
    $token_ok && log "  token:    $(green "set") (${TELEGRAM_BOT_TOKEN:0:8}...)" || log "  token:    $(red "missing")"
    $bot_ok && log "  bot:      $(green "online") (@$bot_name)" || { $token_ok && log "  bot:      $(red "error")"; }
    [[ -n "$webhook_url" ]] && log "  webhook:  $(green "set") ($webhook_url)" || { $token_ok && log "  webhook:  $(yellow "not set")"; }

    # Suggest next action
    if ! $token_ok; then
        hint "Set TELEGRAM_BOT_TOKEN and run again"
    elif ! $hook_ok; then
        hint "Run: ./claudecode-telegram.sh hook install"
    elif [[ -z "$webhook_url" ]]; then
        hint "Run: ./claudecode-telegram.sh run"
    elif [[ ${#claude_sessions[@]} -eq 0 ]]; then
        hint "Send /new <name> to your bot on Telegram"
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

    local token; token=$(require_token)
    log "Setting webhook: $url"

    local r; r=$(telegram_set_webhook "$token" "$url")
    if echo "$r" | grep -q '"ok":true'; then
        success "Webhook configured"
        hint "Start bridge: ./claudecode-telegram.sh start"
    else
        error "Failed to set webhook"
        echo "$r" >&2
        exit 1
    fi
}

cmd_webhook_info() {
    local token; token=$(require_token)
    local r; r=$(telegram_api "$token" "getWebhookInfo" "{}")
    local url; url=$(echo "$r" | grep -o '"url":"[^"]*"' | cut -d'"' -f4)
    local pending; pending=$(echo "$r" | grep -o '"pending_update_count":[0-9]*' | cut -d: -f2)

    if [[ -n "$url" ]]; then
        log "URL:     $url"
        log "Pending: ${pending:-0}"
    else
        log "No webhook configured"
        hint "./claudecode-telegram.sh webhook <url>"
    fi
}

cmd_webhook_delete() {
    local token; token=$(require_token)

    if ! $FORCE && ! $NO_INPUT; then
        read -rp "Delete webhook? [y/N] " confirm
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

    # Update settings.json
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
    hint "Run bridge with: ./claudecode-telegram.sh run"
}

cmd_hook_test() {
    local token; token=$(require_token)

    # Find the most recent chat_id from any session
    local chat_id=""
    local chat_id_file=""

    # Check multi-session directories first
    if [[ -d "$SESSIONS_DIR" ]]; then
        chat_id_file=$(find "$SESSIONS_DIR" -name "chat_id" -type f -print0 2>/dev/null | xargs -0 ls -t 2>/dev/null | head -1)
        if [[ -n "$chat_id_file" ]]; then
            chat_id=$(cat "$chat_id_file")
        fi
    fi

    # Fallback to legacy file
    if [[ -z "$chat_id" ]] && [[ -f "$CLAUDE_DIR/telegram_chat_id" ]]; then
        chat_id=$(cat "$CLAUDE_DIR/telegram_chat_id")
    fi

    if [[ -z "$chat_id" ]]; then
        error "No chat ID found"
        hint "Send a message to your bot first, then retry"
        exit 1
    fi

    log "Sending test to chat $chat_id..."

    local r; r=$(telegram_api "$token" "sendMessage" "{\"chat_id\":\"$chat_id\",\"text\":\"Test OK!\"}")
    echo "$r" | grep -q '"ok":true' && success "Message sent" || { error "Failed"; exit 1; }
}

cmd_setup() {
    local errors=0

    if $NO_INPUT; then
        # Headless: strict validation
        log "$(bold "Headless Setup (Multi-Session Mode)")"

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
        # Interactive: helpful warnings
        log "$(bold "Setup (Multi-Session Mode)")"
        log ""

        log "$(bold "Dependencies")"
        check_cmd tmux    && success "tmux"    || { error "tmux (required)"; hint "brew install tmux"; }
        check_cmd jq      && success "jq"      || warn "jq (optional, for settings.json)"
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

        log "$(bold "Sessions")"
        local sessions
        sessions=$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^claude-' || true)
        if [[ -n "$sessions" ]]; then
            while IFS= read -r s; do
                success "${s#claude-}"
            done <<< "$sessions"
        else
            log "  $(dim "No claude-* sessions (use /new from Telegram)")"
        fi
        log ""

        log "$(bold "Quick Start (1 command!)")"
        log "  1. export TELEGRAM_BOT_TOKEN='...'"
        log "  2. ./claudecode-telegram.sh run"
        log "  3. Send /new backend to your bot on Telegram"
        log ""
        log "$(bold "Manual Setup (more control)")"
        log "  1. export TELEGRAM_BOT_TOKEN='...'"
        log "  2. ./claudecode-telegram.sh start"
        log "  3. $(dim "(new terminal)") cloudflared tunnel --url http://localhost:8080"
        log "  4. ./claudecode-telegram.sh webhook <tunnel-url>"
    fi
}

cmd_help() {
    cat << 'EOF'
claudecode-telegram - Bridge Claude Code to Telegram (Multi-Session)

USAGE
  ./claudecode-telegram.sh [flags] <command> [args]

QUICK START (1 command!)
  export TELEGRAM_BOT_TOKEN='...'
  ./claudecode-telegram.sh run

  Then from Telegram:
    /new backend     Create Claude instance "backend"
    /new frontend    Create another instance "frontend"
    /use backend     Switch to backend
    /list            See all instances
    @backend <msg>   One-off message to backend

TELEGRAM COMMANDS
  /new <name>       Create new Claude instance (tmux: claude-<name>)
  /use <name>       Switch active Claude
  /list             List all instances (scans tmux)
  /kill <name>      Stop and remove instance
  /status           Detailed status of active Claude
  /stop             Interrupt active Claude (Escape)
  @name <msg>       One-off message to specific Claude
  <message>         Send to active Claude

SHELL COMMANDS
  run               Start gateway + tunnel (no default session)
  start             Start bridge only (manual setup)
  stop              Stop bridge and tunnel, clean up
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
  -q, --quiet           Suppress non-error output
  -v, --verbose         Show debug output
  --json                JSON output (status command)
  --no-color            Disable colors
  --no-input            Headless mode (strict, no prompts)
  --env-file <path>     Load environment from file
  -f, --force           Overwrite existing (hook install, webhook delete)
  -p, --port <port>     Bridge port (default: 8080)

ENVIRONMENT
  TELEGRAM_BOT_TOKEN        Bot token from @BotFather (required)
  PORT                      Server port (default: 8080)
  TELEGRAM_WEBHOOK_SECRET   Webhook verification secret (optional, for extra security)

SECURITY
  - Token is NEVER exposed to Claude sessions (bridge-centric architecture)
  - First user to message becomes admin (auto-learn, stored in RAM)
  - Non-admin users are silently ignored
  - Session files use 0o700/0o600 permissions
  - Optional webhook verification via TELEGRAM_WEBHOOK_SECRET

MULTI-SESSION WORKFLOW
  1. Start gateway:    ./claudecode-telegram.sh run
  2. From Telegram:    /new backend
  3. Send message:     Write unit tests for the API
  4. Create another:   /new frontend
  5. Switch:           /use backend  (or @backend <msg>)
  6. List all:         /list

AUTO-DISCOVERY
  - Existing claude-* tmux sessions are auto-discovered on startup
  - Unregistered sessions (running claude) can be registered via:
    {"name": "your-session-name"}

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
            -V|--version) echo "claudecode-telegram $VERSION"; exit 0;;
            -q|--quiet)  QUIET=true; shift;;
            -v|--verbose) VERBOSE=true; shift;;
            --json)      JSON_OUTPUT=true; shift;;
            --no-color)  NO_COLOR=true; shift;;
            --no-input)  NO_INPUT=true; shift;;
            -f|--force)  FORCE=true; shift;;
            -p|--port)   PORT="$2"; shift 2;;
            -*)          error "Unknown flag: $1"; exit 2;;
            *)           break;;
        esac
    done

    local cmd="${1:-run}"
    shift || true

    case "$cmd" in
        run)     cmd_run "$@";;
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
