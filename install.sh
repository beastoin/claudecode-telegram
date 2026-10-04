#!/usr/bin/env bash
#
# install.sh — One-command setup for claudecode-telegram
#
# Downloads a release tarball, installs system dependencies, then delegates
# to bridge.sh setup for token, hooks, and readiness checks.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/beastoin/claudecode-telegram/main/install.sh | bash
#   # or after cloning:
#   ./install.sh
#
set -euo pipefail

# ── Helpers ────────────────────────────────────────────────────────────────

readonly GREEN='\033[0;32m' RED='\033[0;31m' YELLOW='\033[0;33m'
readonly BOLD='\033[1m' DIM='\033[2m' NC='\033[0m'
readonly REPO="beastoin/claudecode-telegram"

ok()   { echo -e "${GREEN}✓${NC} $*"; }
fail() { echo -e "${RED}✗${NC} $*" >&2; }
warn() { echo -e "${YELLOW}○${NC} $*"; }
info() { echo -e "  ${DIM}$*${NC}"; }

detect_os() {
  case "$(uname -s)" in
    Darwin) echo "macos" ;;
    Linux)
      if [[ -f /etc/debian_version ]]; then echo "debian"
      elif [[ -f /etc/redhat-release ]]; then echo "redhat"
      else echo "linux"
      fi ;;
    *) echo "unknown" ;;
  esac
}

# Install a package if missing. Uses brew (macOS) or apt/dnf (Linux).
install_pkg() {
  local cmd="$1" brew_name="${2:-$1}" apt_name="${3:-$1}"

  if command -v "$cmd" &>/dev/null; then
    ok "$cmd (already installed)"
    return 0
  fi

  case "$OS" in
    macos)
      if ! command -v brew &>/dev/null; then
        info "Installing Homebrew..."
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
      fi
      info "brew install $brew_name"
      brew install "$brew_name"
      ;;
    debian)
      info "sudo apt install -y $apt_name"
      sudo apt update -qq && sudo apt install -y "$apt_name"
      ;;
    redhat)
      info "sudo dnf install -y $apt_name"
      sudo dnf install -y "$apt_name"
      ;;
    *)
      fail "$cmd not found — install it manually"
      return 1
      ;;
  esac
  ok "$cmd"
}

# ── Main ───────────────────────────────────────────────────────────────────

main() {
  local OS
  OS=$(detect_os)
  local INSTALL_DIR="${INSTALL_DIR:-$HOME/claudecode-telegram}"

  echo ""
  echo -e "${BOLD}claudecode-telegram — Install${NC}"
  echo ""

  # ── Step 1: System dependencies ──────────────────────────────────────────
  echo -e "${BOLD}Step 1/2${NC} — Installing system dependencies"
  echo ""

  # Python 3.12+
  if command -v python3 &>/dev/null; then
    local pyver
    pyver=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    local major minor
    major=$(echo "$pyver" | cut -d. -f1)
    minor=$(echo "$pyver" | cut -d. -f2)
    if [[ "$major" -ge 3 && "$minor" -ge 12 ]]; then
      ok "Python $pyver"
    else
      warn "Python $pyver found but 3.12+ required"
      case "$OS" in
        macos) install_pkg python3 python@3.12 python3.12 ;;
        debian)
          if command -v add-apt-repository &>/dev/null; then
            info "Adding deadsnakes PPA for Python 3.12..."
            sudo add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null || true
            sudo apt update -qq
            sudo apt install -y python3.12 python3.12-venv
          else
            fail "Python 3.12+ required — install manually"
            info "See https://www.python.org/downloads/"
            exit 1
          fi ;;
        *)
          fail "Python 3.12+ required — install manually"
          info "See https://www.python.org/downloads/"
          exit 1 ;;
      esac
      ok "Python 3.12"
    fi
  else
    install_pkg python3 python@3.12 python3
  fi

  install_pkg tmux
  install_pkg node node nodejs

  # Claude CLI
  if ! command -v claude &>/dev/null; then
    info "npm install -g @anthropic-ai/claude-code"
    npm install -g @anthropic-ai/claude-code
    ok "Claude CLI"
  else
    ok "Claude CLI $(claude --version 2>/dev/null || echo '')"
  fi

  echo ""

  # ── Step 2: Download release ─────────────────────────────────────────────
  echo -e "${BOLD}Step 2/2${NC} — Getting claudecode-telegram"
  echo ""

  if [[ -f "$INSTALL_DIR/bridge.sh" ]]; then
    # Already installed — update in place
    if [[ -d "$INSTALL_DIR/.git" ]] && command -v git &>/dev/null; then
      cd "$INSTALL_DIR"
      if git pull --ff-only origin main 2>/dev/null; then
        ok "Updated via git pull"
      else
        warn "Could not pull latest (working changes?)"
      fi
    else
      ok "Using existing install at $INSTALL_DIR"
      cd "$INSTALL_DIR"
    fi
  else
    # Fresh install — download tarball (no git required)
    local tmp_dir
    tmp_dir=$(mktemp -d)
    trap 'rm -rf "$tmp_dir"' EXIT INT TERM

    local tarball_url="https://github.com/${REPO}/archive/refs/heads/main.tar.gz"
    info "Downloading from $tarball_url"

    local n=0
    until [[ "$n" -ge 3 ]]; do
      curl -C - --retry 3 -sfLo "$tmp_dir/release.tar.gz" "$tarball_url" && break
      n=$((n + 1))
      sleep 5
    done

    if [[ ! -f "$tmp_dir/release.tar.gz" ]]; then
      fail "Download failed after 3 attempts"
      exit 1
    fi

    tar -xf "$tmp_dir/release.tar.gz" -C "$tmp_dir"
    mkdir -p "$INSTALL_DIR"
    # GitHub archives extract to repo-branch/ — move contents to install dir
    mv "$tmp_dir"/claudecode-telegram-main/* "$INSTALL_DIR/"
    ok "Downloaded to $INSTALL_DIR"
    cd "$INSTALL_DIR"
  fi

  echo ""

  # ── Hand off to bridge.sh setup ──────────────────────────────────────────
  # bridge.sh setup handles: prerequisite checks, bot token, hooks, readiness.
  # No logic is duplicated here.
  exec ./bridge.sh setup --start
}

main "$@"
