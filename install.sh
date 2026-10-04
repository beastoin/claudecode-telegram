#!/usr/bin/env bash
#
# install.sh — One-command setup for claudecode-telegram
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/beastoin/claudecode-telegram/main/install.sh | bash
#   # or after cloning:
#   ./install.sh
#
set -euo pipefail

# ── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'
BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

ok()   { echo -e "${GREEN}✓${NC} $*"; }
fail() { echo -e "${RED}✗${NC} $*"; }
warn() { echo -e "${YELLOW}○${NC} $*"; }
info() { echo -e "  ${DIM}$*${NC}"; }
step() { echo -e "\n${BOLD}$*${NC}\n"; }

# ── Detect OS ───────────────────────────────────────────────────────────────
detect_os() {
    case "$(uname -s)" in
        Darwin) echo "macos" ;;
        Linux)
            if [ -f /etc/debian_version ]; then echo "debian"
            elif [ -f /etc/redhat-release ]; then echo "redhat"
            else echo "linux"
            fi ;;
        *) echo "unknown" ;;
    esac
}

OS=$(detect_os)
INSTALL_DIR="${INSTALL_DIR:-$HOME/claudecode-telegram}"

echo ""
echo -e "${BOLD}claudecode-telegram — Install${NC}"
echo ""

# ── Step 1: Install system dependencies ─────────────────────────────────────
step "Step 1/5 — Installing dependencies"

install_pkg() {
    local name="$1" brew_name="${2:-$1}" apt_name="${3:-$1}"
    if command -v "$name" &>/dev/null; then
        ok "$name (already installed)"
        return 0
    fi
    case "$OS" in
        macos)
            if ! command -v brew &>/dev/null; then
                echo -e "  ${DIM}Installing Homebrew...${NC}"
                /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
            fi
            echo -e "  ${DIM}brew install $brew_name${NC}"
            brew install "$brew_name"
            ;;
        debian)
            echo -e "  ${DIM}sudo apt install -y $apt_name${NC}"
            sudo apt update -qq && sudo apt install -y "$apt_name"
            ;;
        redhat)
            echo -e "  ${DIM}sudo dnf install -y $apt_name${NC}"
            sudo dnf install -y "$apt_name"
            ;;
        *)
            fail "$name not found — install it manually"
            return 1
            ;;
    esac
    ok "$name"
}

# Python 3.12+
install_python() {
    if command -v python3 &>/dev/null; then
        local pyver
        pyver=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        local major minor
        major=$(echo "$pyver" | cut -d. -f1)
        minor=$(echo "$pyver" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 12 ]; then
            ok "Python $pyver"
            return 0
        fi
        warn "Python $pyver found but 3.12+ required"
    fi
    case "$OS" in
        macos)
            if ! command -v brew &>/dev/null; then
                /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
            fi
            info "brew install python@3.12"
            brew install python@3.12
            ;;
        debian)
            # Try deadsnakes PPA on Ubuntu, or build from source on Debian
            if command -v add-apt-repository &>/dev/null; then
                info "Adding deadsnakes PPA for Python 3.12..."
                sudo add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null || true
                sudo apt update -qq
                sudo apt install -y python3.12 python3.12-venv 2>/dev/null || {
                    fail "Python 3.12 not available via apt — install manually"
                    info "See https://www.python.org/downloads/"
                    return 1
                }
            else
                fail "Python 3.12+ required — install manually"
                info "See https://www.python.org/downloads/"
                return 1
            fi
            ;;
        *)
            fail "Python 3.12+ required — install manually"
            info "See https://www.python.org/downloads/"
            return 1
            ;;
    esac
    ok "Python 3.12"
}

install_python
install_pkg tmux tmux tmux

# Node.js
if ! command -v node &>/dev/null; then
    case "$OS" in
        macos)
            info "brew install node"
            brew install node
            ;;
        debian)
            info "Installing Node.js via NodeSource..."
            curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
            sudo apt install -y nodejs
            ;;
        *)
            fail "Node.js not found — install from https://nodejs.org"
            exit 1
            ;;
    esac
    ok "Node.js"
else
    ok "Node.js $(node --version)"
fi

# Claude CLI
if ! command -v claude &>/dev/null; then
    info "npm install -g @anthropic-ai/claude-code"
    npm install -g @anthropic-ai/claude-code
    ok "Claude CLI"
else
    ok "Claude CLI"
fi

# cloudflared (optional)
if command -v cloudflared &>/dev/null; then
    ok "cloudflared"
else
    warn "cloudflared not found (optional — needed for public webhook)"
    case "$OS" in
        macos) info "brew install cloudflared" ;;
        debian) info "See https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/" ;;
    esac
fi

# ── Step 2: Clone or update repo ────────────────────────────────────────────
step "Step 2/5 — Getting claudecode-telegram"

if [ -d "$INSTALL_DIR/.git" ]; then
    ok "Already cloned at $INSTALL_DIR"
    cd "$INSTALL_DIR"
    if git pull --ff-only origin main 2>/dev/null; then
        ok "Updated to latest"
    else
        warn "Could not pull latest (working changes?)"
    fi
elif [ -f "$INSTALL_DIR/bridge.sh" ]; then
    # We're already in the repo (script run locally)
    ok "Using existing directory: $INSTALL_DIR"
    cd "$INSTALL_DIR"
else
    info "git clone https://github.com/beastoin/claudecode-telegram.git $INSTALL_DIR"
    git clone https://github.com/beastoin/claudecode-telegram.git "$INSTALL_DIR"
    ok "Cloned to $INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# ── Step 3: Bot token ──────────────────────────────────────────────────────
step "Step 3/5 — Telegram bot token"

TOKEN=""
ENV_FILE="$HOME/.config/claudecode-telegram/prod.env"

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
    TOKEN="$TELEGRAM_BOT_TOKEN"
    ok "Found in environment"
elif [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE" 2>/dev/null || true
    TOKEN="${TELEGRAM_BOT_TOKEN:-}"
    [ -n "$TOKEN" ] && ok "Found in $ENV_FILE"
fi

if [ -z "$TOKEN" ]; then
    echo "  No bot token found."
    echo ""
    info "Get one from @BotFather on Telegram:"
    info "1. Open Telegram → search @BotFather → /newbot"
    info "2. Pick a name and username"
    info "3. Copy the token (looks like 123456789:AAE...)"
    echo ""
    printf "  Paste your bot token: "
    read -r TOKEN
    echo ""

    if [ -z "$TOKEN" ]; then
        fail "No token provided"
        exit 1
    fi

    if ! echo "$TOKEN" | grep -qE '^[0-9]+:[A-Za-z0-9_-]+$'; then
        fail "Token format looks wrong (expected: 123456789:AAE...)"
        exit 1
    fi
fi

mkdir -p "$(dirname "$ENV_FILE")"
echo "TELEGRAM_BOT_TOKEN=\"$TOKEN\"" > "$ENV_FILE"
chmod 600 "$ENV_FILE"
ok "Token saved to $ENV_FILE"

# ── Step 4: Install hooks ──────────────────────────────────────────────────
step "Step 4/5 — Installing Claude Code hooks"

./bridge.sh hook install --force 2>/dev/null || FORCE=true ./bridge.sh hook install

# ── Step 5: Done ────────────────────────────────────────────────────────────
step "Step 5/5 — Ready! 🎉"

echo "  Start the bridge:"
echo ""
echo -e "    ${BOLD}cd $INSTALL_DIR && ./bridge.sh run${NC}"
echo ""
echo "  Then open Telegram and send /hire myworker to your bot."
echo ""
