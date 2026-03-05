#!/bin/bash
set -euo pipefail
SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/$(basename "${BASH_SOURCE[0]}")"
cd /tmp 2>/dev/null || cd /

VERSION="1.1.0"

DRY_RUN=0
VERBOSE=0
USE_COLOR=1
ASSUME_YES=0
NO_SECRETS=0
KEEP_BUILD=0

VM_NAME="secrets-vm"
VM_CPUS=4
VM_MEM=4096
SECRETS_SIZE=512

STATE_ROOT="/var/lib/setup-secure-vm"
BUILD_ROOT="${STATE_ROOT}/build"
VM_ROOT="${STATE_ROOT}/vms"
RUN_ROOT="/run/setup-secure-vm"

VM_STATE_DIR=""
LUKS_IMAGE=""
LUKS_MAPPER=""
RUNTIME_ENV_FILE=""
PID_FILE=""
VM_LOG_FILE=""

TMP_PATHS=()

if [[ -n "${NO_COLOR:-}" || ! -t 2 ]]; then
  USE_COLOR=0
fi

usage() {
  cat <<'EOF_USAGE'
Usage:
  setup-secure-vm.sh [global-flags] <subcommand>

Subcommands:
  fresh       Nuclear clean + full rebuild + start (one command from scratch)
  deps        Install host dependencies (Rust, Python, gcloud, gh, bws)
  build       Build libkrunfw + libkrun + krunvm from source
  create      Create LUKS volume + microVM
  configure   Configure guest proxy daemon, firewall, and CLI tools
  start       Start VM (unlock LUKS, pull Bitwarden secrets, boot proxy daemon)
  stop        Stop VM and cleanup runtime mounts/state
  restart     Stop + start (re-pulls Bitwarden secrets)
  grant       Grant non-root users access to void (e.g. grant claude)
  status      Show VM/runtime status
  test        Run embedded host proxy + daemon security tests
  clean       Nuclear teardown: stop VM, unmount LUKS, delete VM, shred secrets, remove all
  all         Run deps -> build -> create -> configure

Global Flags:
  -h, --help            Show help and exit
  --version             Print version and exit
  --dry-run             Print commands without executing
  --verbose             Print detailed diagnostics
  --no-color            Disable ANSI colors
  --yes                 Auto-confirm destructive operations
  --no-secrets          Skip Bitwarden secrets injection on start (boot VM without secrets)
  --keep-build          Skip removing build artifacts during clean/fresh
  --vm-name NAME        VM name (default: secrets-vm)
  --cpus N              VM CPU count (default: 4)
  --mem N               VM memory in MB (default: 4096)
  --secrets-size N      LUKS image size in MB (default: 512)

Environment (only needed without --no-secrets):
  BWS_ACCESS_TOKEN      Bitwarden Secrets Manager machine account access token
  BWS_PROJECT_ID        Bitwarden Secrets Manager project UUID

Quick Start:

  ── One command from scratch (~30 min first time) ───────────────────

    ./setup-secure-vm.sh --yes --no-secrets fresh

    With Bitwarden secrets:

      BWS_PROJECT_ID=<uuid> BWS_ACCESS_TOKEN=<token> \
        ./setup-secure-vm.sh --yes fresh

  ── Use it ───────────────────────────────────────────────────────────

    void gh pr list                  # GitHub CLI
    void gcloud projects list        # Google Cloud CLI
    void session login github        # Human login portal

    Credentials stay inside the VM. Only stdout/stderr cross the boundary.

  ── Stop / Restart ───────────────────────────────────────────────────

    ./setup-secure-vm.sh --yes stop
    ./setup-secure-vm.sh --yes start

Bitwarden Secrets Manager Setup (one-time):

  This is only needed if you want to inject secrets into the VM at boot.
  Skip this entire section if you use --no-secrets.

  1. OPEN Secrets Manager
     Go to https://vault.bitwarden.com
     Click "Secrets Manager" in the left sidebar
     (If you don't see it, enable it under Organization Settings -> Billing)

  2. CREATE a Project
     Click "Projects" -> "+ New" -> name it (e.g. "secure-vm")
     Copy the Project UUID shown in the URL or details panel
     -> This is your BWS_PROJECT_ID

  3. ADD Secrets to the Project
     Click "Secrets" -> "+ New"
     For each env var you want inside the VM:
       Key   = the env var name   (e.g. GH_TOKEN)
       Value = the secret value   (e.g. ghp_xxxxxxxxxxxx)
     Make sure each secret is assigned to your project

  4. CREATE a Machine Account
     Click "Machine Accounts" -> "+ New" -> name it (e.g. "vm-agent")
     In the machine account, go to "Projects" tab -> add your project

  5. GENERATE an Access Token
     In the machine account, go to "Access Tokens" tab -> "+ Create"
     Copy the token immediately (it won't be shown again)
     -> This is your BWS_ACCESS_TOKEN

  6. VERIFY it works
     bws project list --access-token "<your-token>"
     You should see your project listed

  7. START the VM with secrets
     BWS_PROJECT_ID=<uuid-from-step-2> \
     BWS_ACCESS_TOKEN=<token-from-step-5> \
     ./setup-secure-vm.sh --yes start

Examples:
  ./setup-secure-vm.sh --yes all                                        # full setup
  ./setup-secure-vm.sh --yes --no-secrets start                         # boot without secrets
  ./setup-secure-vm.sh --vm-name secure-dev --cpus 6 --mem 8192 create  # custom VM
  ./setup-secure-vm.sh status                                           # check VM state
  ./setup-secure-vm.sh stop                                             # stop VM
  ./setup-secure-vm.sh clean --yes                                      # remove everything
  void gh pr list                                               # run gh in VM
  void gcloud projects list                                     # run gcloud in VM
  void session list                                             # list browser sessions
EOF_USAGE
}

refresh_paths() {
  VM_STATE_DIR="${VM_ROOT}/${VM_NAME}"
  LUKS_IMAGE="${VM_STATE_DIR}/secrets.img"
  LUKS_MAPPER="${VM_NAME//[^a-zA-Z0-9]/-}-secrets"
  RUNTIME_ENV_FILE="${VM_STATE_DIR}/runtime.env"
  PID_FILE="${VM_STATE_DIR}/krunvm.pid"
  VM_LOG_FILE="${VM_STATE_DIR}/${VM_NAME}.log"
}

color() {
  local code="$1"
  if ((USE_COLOR)); then
    printf '\033[%sm' "$code"
  fi
}

log_info() {
  printf "%s[INFO]%s %s\n" "$(color 32)" "$(color 0)" "$*" >&2
}

log_warn() {
  printf "%s[WARN]%s %s\n" "$(color 33)" "$(color 0)" "$*" >&2
}

log_error() {
  printf "%s[ERROR]%s %s\n" "$(color 31)" "$(color 0)" "$*" >&2
}

log_debug() {
  if ((VERBOSE)); then
    printf "%s[DEBUG]%s %s\n" "$(color 36)" "$(color 0)" "$*" >&2
  fi
}

die() {
  log_error "$*"
  exit 1
}

die_usage() {
  log_error "$*"
  usage >&2
  exit 2
}

track_tmp_path() {
  local p="$1"
  TMP_PATHS+=("$p")
}

cleanup() {
  local p
  for p in "${TMP_PATHS[@]:-}"; do
    [[ -z "$p" ]] && continue
    if [[ -e "$p" ]]; then
      rm -rf -- "$p" || true
    fi
  done
}

on_interrupt() {
  log_warn "Interrupted; stopping immediately."
  exit 130
}

trap cleanup EXIT
trap on_interrupt INT TERM

run() {
  if ((DRY_RUN)); then
    printf "[dry-run]" >&2
    printf " %q" "$@" >&2
    printf "\n" >&2
    return 0
  fi
  if ((VERBOSE)); then
    printf "+" >&2
    printf " %q" "$@" >&2
    printf "\n" >&2
  fi
  "$@"
}

run_bash() {
  local script="$1"
  if ((DRY_RUN)); then
    printf "[dry-run] bash -lc %q\n" "$script" >&2
    return 0
  fi
  if ((VERBOSE)); then
    printf "+ bash -lc %q\n" "$script" >&2
  fi
  bash -lc "$script"
}

need_root() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log_warn "Skipping root check (--dry-run)"
    return 0
  fi
  if [[ "$EUID" -ne 0 ]]; then
    die "This script must run as root."
  fi
}

ensure_numeric() {
  local name="$1"
  local value="$2"
  if ! [[ "$value" =~ ^[0-9]+$ ]] || [[ "$value" -le 0 ]]; then
    die_usage "${name} must be a positive integer: ${value}"
  fi
}

confirm_action() {
  local prompt="$1"
  if ((ASSUME_YES)) || ((DRY_RUN)); then
    return 0
  fi
  if [[ ! -t 0 ]]; then
    die "Refusing destructive operation in non-interactive mode without --yes."
  fi
  printf "%s [type 'yes' to continue]: " "$prompt" >&2
  local answer=""
  read -r answer
  if [[ "$answer" != "yes" ]]; then
    die "Operation cancelled."
  fi
}

ensure_dir() {
  run mkdir -p "$1"
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

require_commands() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log_warn "Skipping command check (--dry-run): $*"
    return 0
  fi
  local missing=()
  local cmd
  for cmd in "$@"; do
    if ! command_exists "$cmd"; then
      missing+=("$cmd")
    fi
  done
  if ((${#missing[@]} > 0)); then
    die "Missing required command(s): ${missing[*]}"
  fi
}

ensure_state_dirs() {
  ensure_dir "$STATE_ROOT"
  ensure_dir "$BUILD_ROOT"
  ensure_dir "$VM_ROOT"
  ensure_dir "$RUN_ROOT"
  ensure_dir "$VM_STATE_DIR"
}

install_apt_packages() {
  local pkgs=("$@")
  run sudo apt-get update
  run sudo apt-get install -y "${pkgs[@]}"
}

install_rust() {
  if command_exists cargo; then
    log_info "Rust is already installed."
    return
  fi
  log_info "Installing Rust toolchain via rustup..."
  run_bash "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y"
}

install_nodesource_nodejs() {
  if command_exists node && command_exists npm; then
    log_info "Node.js and npm are already installed."
    return
  fi
  log_info "Installing Node.js LTS from NodeSource..."
  run_bash "curl -fsSL https://deb.nodesource.com/setup_lts.x | bash -"
  run apt-get install -y nodejs
}

install_gh_cli() {
  if command_exists gh; then
    log_info "gh CLI is already installed."
    return
  fi
  log_info "Installing gh CLI..."
  run bash -lc "curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg"
  run chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
  run_bash "echo \"deb [arch=\$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main\" > /etc/apt/sources.list.d/github-cli.list"
  run apt-get update
  run apt-get install -y gh
}

install_gcloud_cli() {
  if command_exists gcloud; then
    log_info "gcloud CLI is already installed."
    return
  fi
  log_info "Installing gcloud CLI..."
  local installer="${RUN_ROOT}/google-cloud-installer.sh"
  track_tmp_path "$installer"
  run curl -fsSL https://sdk.cloud.google.com -o "$installer"
  if ((DRY_RUN)); then
    printf "[dry-run] bash %q --disable-prompts --install-dir=/usr/local\n" "$installer" >&2
  else
    bash "$installer" --disable-prompts --install-dir=/usr/local
  fi
  if [[ -x /usr/local/google-cloud-sdk/bin/gcloud ]]; then
    run ln -sf /usr/local/google-cloud-sdk/bin/gcloud /usr/local/bin/gcloud
  fi
}

install_bitwarden_cli() {
  if command_exists bws; then
    log_info "Bitwarden Secrets Manager CLI (bws) is already installed."
    return
  fi
  log_info "Installing Bitwarden Secrets Manager CLI (bws)..."
  local arch
  arch="$(uname -m)"
  case "$arch" in
    x86_64)  arch="x86_64" ;;
    aarch64) arch="aarch64" ;;
    *)       log_warn "Unsupported arch $arch for bws binary. Install manually: https://bitwarden.com/help/secrets-manager-cli/"; return ;;
  esac
  local bws_url="https://github.com/bitwarden/sdk-sm/releases/download/bws-v1.0.0/bws-${arch}-unknown-linux-gnu-1.0.0.zip"
  local tmp_zip
  tmp_zip="$(mktemp /tmp/bws.XXXXXX.zip)"
  run curl -fsSL -o "$tmp_zip" "$bws_url"
  run unzip -o "$tmp_zip" -d /usr/local/bin/
  run chmod 0755 /usr/local/bin/bws
  rm -f "$tmp_zip"
  if command_exists bws; then
    log_info "bws installed: $(bws --version 2>/dev/null || echo 'ok')"
  else
    log_warn "'bws' not found after install. See: https://bitwarden.com/help/secrets-manager-cli/"
  fi
}

git_sync_repo() {
  local repo_url="$1"
  local dest="$2"
  git config --global --add safe.directory "$dest" 2>/dev/null || true
  if [[ -d "${dest}/.git" ]]; then
    log_info "Updating $(basename "$dest")..."
    # Reset local changes (e.g. Makefile patches from previous builds)
    git -C "$dest" checkout -- . 2>/dev/null || true
    run git -C "$dest" fetch --all --tags --prune
    run git -C "$dest" pull --ff-only
  else
    log_info "Cloning $(basename "$dest")..."
    run git clone "$repo_url" "$dest"
  fi
}

krunvm_installed() {
  command_exists krunvm || [[ -x "${HOME}/.cargo/bin/krunvm" ]] || [[ -x /root/.cargo/bin/krunvm ]]
}

vm_exists() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    return 0
  fi
  if ! krunvm_installed; then
    return 1
  fi
  local krunvm_bin
  krunvm_bin="$(command -v krunvm 2>/dev/null || true)"
  if [[ -z "$krunvm_bin" ]]; then
    krunvm_bin="/root/.cargo/bin/krunvm"
  fi
  "$krunvm_bin" list 2>/dev/null | awk '{print $1}' | grep -Fxq "$VM_NAME"
}

vm_is_running() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    return 1
  fi
  if ! vm_exists; then
    return 1
  fi
  local krunvm_bin
  krunvm_bin="$(command -v krunvm 2>/dev/null || true)"
  if [[ -z "$krunvm_bin" ]]; then
    krunvm_bin="/root/.cargo/bin/krunvm"
  fi
  "$krunvm_bin" list 2>/dev/null | grep -E "^${VM_NAME}[[:space:]]" | grep -qi "running"
}

ensure_iptables_rule() {
  local chain="$1"
  shift
  if iptables -C "$chain" "$@" >/dev/null 2>&1; then
    return
  fi
  run iptables -A "$chain" "$@"
}

write_proxy_daemon() {
  local target="$1"
  cat >"$target" <<'PY_DAEMON'
#!/usr/bin/env python3
"""
Command proxy daemon — runs as PID 1 inside the guest VM.
Polls /ipc/requests/ for JSON request files, executes allowed commands,
writes JSON responses to /ipc/responses/.

Only gh and gcloud raw commands are allowed, plus structured browser actions.
Credentials are loaded from /run/secrets/env into the daemon's own environment
(never exposed via shared volume).
"""
import ipaddress
import hashlib
import hmac
import json
import logging
import os
import pwd
import re
import secrets
import signal
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import yaml

CONFIG_PATH = Path("/etc/credential-proxy/config.yaml")
LOG_PATH = Path("/var/log/credential-proxy.log")
IPC_DIR = Path("/ipc")
REQUESTS_DIR = IPC_DIR / "requests"
RESPONSES_DIR = IPC_DIR / "responses"
HEARTBEAT_FILE = IPC_DIR / "heartbeat"
SECRETS_ENV = Path("/run/secrets/env")
IPC_KEY_PATH = Path("/run/secrets/ipc.key")
SESSIONS_ROOT = Path("/secrets")
SESSION_DB_FALLBACK = Path("/var/lib/void/.session-meta.json")
LOGIN_PORTAL_SCRIPT = Path("/usr/local/bin/void-login-portal.py")
LOGIN_NODE_SCRIPT = Path("/usr/local/bin/void-login-browser.js")
POLL_INTERVAL = 0.1  # seconds
REQUEST_MAX_AGE_SECONDS = 30

DEFAULT_CONFIG = {
    "allowed_tools": ["gh", "gcloud", "omi"],
    "command_deny_patterns": [
        r"^gh\s+auth\s+token\b",
        r"^gh\s+auth\s+login\b",
        r"^gcloud\s+auth\s+print-access-token\b",
        r"^gcloud\s+auth\s+print-identity-token\b",
        r"^gcloud\s+config\s+set\b",
        r"^gcloud\s+auth\s+login\b",
    ],
    "global_deny_regex": [
        r"(?i)--log-http",
        r"(?i)--verbosity=debug",
        r"(?i)\bsecret\s+get\b.*\b(?:--output|-o|--format)\s*(?:yaml|json)\b",
    ],
    "flag_deny_patterns": [
        r"(?i)(?:^|\s)--body-file(?:=|\s+)\S+",
        r"(?i)(?:^|\s)--jq(?:=|\s+)(?:file://|/|~/|\./|\.\./)\S*",
        r"(?i)(?:^|\s)--template(?:=|\s+)(?:file://|/|~/|\./|\.\./)\S*",
        r"(?i)(?:^|\s)--json(?:=|\s+\S+)?(?:\s+|$).*(?:^|\s)--jq(?:=|\s+\S+)?(?:\s+|$)|"
        r"(?:^|\s)--jq(?:=|\s+\S+)?(?:\s+|$).*(?:^|\s)--json(?:=|\s+\S+)?(?:\s+|$)",
    ],
    "sensitive_output_regex": [
        r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9._-]{8,}\.[A-Za-z0-9._-]{8,}",
        r"AKIA[0-9A-Z]{16}",
        r"ASIA[0-9A-Z]{16}",
        r"glpat-[A-Za-z0-9_-]{20,}",
        r"github_pat_[A-Za-z0-9_]{22,}",
        r"-----BEGIN (?:RSA|EC|OPENSSH|PGP|PRIVATE) KEY-----",
        r"(?i)kubeconfig",
        r"gho_[A-Za-z0-9_]{36}",
        r"ghp_[A-Za-z0-9_]{36}",
        r"ghs_[A-Za-z0-9]{36}",
        r"ghu_[A-Za-z0-9]{36}",
        r"glsa-[A-Za-z0-9-]{20,}",
        r"dop_v1_[a-f0-9]{64}",
        r"op://[^\s]+",
        r"(?i)aws[_-]?session[_-]?token\s*[:=]\s*\S+",
        r"ya29\.[A-Za-z0-9_-]+",
        r"Bearer [A-Za-z0-9._-]+",
        r"Basic [A-Za-z0-9+/=]{20,}",
        r"xox[bpras]-[A-Za-z0-9-]{10,}",
        r"npm_[A-Za-z0-9]{36}",
        r"sk-[A-Za-z0-9]{20,}",
        r"sk-ant-[A-Za-z0-9-]{20,}",
        r"AGE-SECRET-KEY-[A-Z0-9]{59}",
    ],
    "max_browser_sessions": 2,
    "browser_cgroup": "/sys/fs/cgroup/void",
    "login_port_base": 9222,
    "login_port_max": 9322,
    "login_session_timeout": 1800,
    "session_origin_allowlist": {
        "github": ["github.com", "*.github.com"],
        "gmail": ["accounts.google.com", "mail.google.com", "*.google.com"],
    },
    "session_login_urls": {
        "github": "https://github.com/login",
        "gmail": "https://accounts.google.com/",
    },
}

MAX_BROWSER_TIMEOUT = 45
LONG_BROWSER_TIMEOUT = 25
SHORT_BROWSER_TIMEOUT = 10
BROWSER_LONG_ACTIONS = {
    ("open",),
    ("snapshot",),
    ("screenshot",),
    ("wait",),
    ("session", "login"),
}

ALLOWED_BROWSER_COMMANDS = {
    # Navigation
    ("open",): "open_url",
    ("back",): "no_args",
    ("forward",): "no_args",
    ("reload",): "no_args",
    # AI snapshot
    ("snapshot",): "snapshot",
    # Screenshots
    ("screenshot",): "screenshot",
    # Interaction
    ("click",): "selector1",
    ("dblclick",): "selector1",
    ("fill",): "selector_text",
    ("type",): "selector_text",
    ("press",): "key1",
    ("hover",): "selector1",
    ("scroll",): "scroll",
    ("select",): "selector_text",
    ("check",): "selector1",
    ("uncheck",): "selector1",
    ("upload",): "upload",
    ("drag",): "drag",
    # Reading (safe subset)
    ("get", "text"): "selector1",
    ("get", "title"): "no_args",
    ("get", "url"): "no_args",
    ("get", "value"): "selector1",
    ("get", "attr"): "selector_attr",
    ("get", "count"): "selector1",
    ("get", "box"): "selector1",
    # State checks
    ("is", "visible"): "selector1",
    ("is", "enabled"): "selector1",
    ("is", "checked"): "selector1",
    # Waiting
    ("wait",): "wait",
    # Semantic locators
    ("find", "role"): "find_role",
    ("find", "label"): "find_generic",
    ("find", "text"): "find_generic",
    ("find", "placeholder"): "find_generic",
    # Tab management
    ("tab", "list"): "no_args",
    ("tab", "new"): "tab_new",
    ("tab", "close"): "tab_close",
    ("tab", "switch"): "tab_switch",
    # Session management
    ("session", "login"): "session_login",
    ("session", "list"): "no_args",
    ("session", "close"): "session_close",
    # Display settings
    ("set", "viewport"): "viewport",
    ("set", "device"): "device",
    ("set", "media"): "media",
}

FORBIDDEN_TOKENS = {
    "cookie", "storage", "eval", "evaluate", "exec", "script",
    "credential", "header", "route", "intercept", "proxy",
    "console", "errors", "trace", "state", "html",
}

ALLOWED_GH_SUBCOMMANDS = {
    "pr": {"list", "view", "create", "merge", "close", "comment", "review", "diff", "checks", "ready", "edit"},
    "issue": {"list", "view", "create", "close", "comment", "edit", "reopen"},
    "repo": {"view", "list", "clone"},
    "run": {"list", "view", "watch"},
    "release": {"list", "view"},
    "search": {"repos", "issues", "prs", "commits", "code"},
    "status": None,
    "label": {"list", "create"},
}

ALLOWED_GCLOUD_SUBCOMMANDS = {
    ("projects", "list"),
    ("compute", "instances", "list"),
    ("compute", "instances", "describe"),
    ("container", "clusters", "list"),
    ("container", "clusters", "get-credentials"),
    ("run", "services", "list"),
    ("run", "services", "describe"),
    ("run", "jobs", "list"),
    ("logging", "read"),
    ("storage", "ls"),
    ("storage", "cp"),
    ("storage", "buckets", "list"),
    ("storage", "buckets", "describe"),
    ("storage", "buckets", "update"),
    ("auth", "activate-service-account"),
}

OMI_BINARY = Path("/usr/local/bin/omi")
OMI_CHECKSUM_ENV = "OMI_SHA256"

BROWSER_SCRUB_PATTERNS = [
    r'([?&](?:access_token|token|id_token|refresh_token|api[_-]?key|key|sig|signature|auth|authorization|code|client_secret)=)[^&#\s]+',
    r'\bBearer\s+[A-Za-z0-9._~+\-/]+=*',
    r'\bBasic\s+[A-Za-z0-9+/=]{20,}\b',
    r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b',
    r'\bAIza[0-9A-Za-z_-]{35}\b',
    r'\bgh[pousr]_[A-Za-z0-9]{20,}\b',
    r'\bgithub_pat_[A-Za-z0-9_]{22,}\b',
    r'\bxox[bpras]-[A-Za-z0-9-]{10,}\b',
    r'\bnpm_[A-Za-z0-9]{36}\b',
    r'\bsk-[A-Za-z0-9]{20,}\b',
    r'\bsk-ant-[A-Za-z0-9-]{20,}\b',
    r'\bAGE-SECRET-KEY-[A-Z0-9]{59}\b',
]

BROWSER_METADATA_HOSTS = {
    "localhost",
    "metadata.google.internal",
}

active_browser_sessions = set()
session_metadata = {}
login_portals = {}
running = True


def handle_signal(signum, frame):
    global running
    running = False


def load_config():
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}
    merged = dict(DEFAULT_CONFIG)
    merged.update(loaded)
    return merged


def session_db_path():
    if SESSIONS_ROOT.exists() and SESSIONS_ROOT.is_dir():
        return SESSIONS_ROOT / ".void-session-meta.json"
    return SESSION_DB_FALLBACK


def load_session_metadata(logger):
    global session_metadata
    db_path = session_db_path()
    if not db_path.exists():
        session_metadata = {}
        return
    try:
        raw = json.loads(db_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to read session metadata %s: %s", db_path, e)
        session_metadata = {}
        return
    if not isinstance(raw, dict):
        session_metadata = {}
        return
    cleaned = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", key):
            continue
        if isinstance(value, dict):
            cleaned[key] = {
                "created": int(value.get("created", 0) or 0),
                "last_used": int(value.get("last_used", 0) or 0),
            }
    session_metadata = cleaned


def save_session_metadata(logger):
    db_path = session_db_path()
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = db_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(session_metadata, sort_keys=True), encoding="utf-8")
        tmp.replace(db_path)
    except Exception as e:
        logger.warning("Failed to persist session metadata %s: %s", db_path, e)


def touch_session_metadata(session, logger, created_if_missing=False):
    now = int(time.time())
    meta = session_metadata.get(session)
    if not isinstance(meta, dict):
        if not created_if_missing:
            meta = {"created": now, "last_used": now}
        else:
            meta = {"created": now, "last_used": now}
        session_metadata[session] = meta
    else:
        if created_if_missing and not meta.get("created"):
            meta["created"] = now
        meta["last_used"] = now
    save_session_metadata(logger)


def format_ts(ts):
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return "-"
    if ts <= 0:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts))


def resolve_session_allowlist(session, cfg):
    if session == "default":
        return []
    configured = cfg.get("session_origin_allowlist", {})
    if isinstance(configured, dict):
        patterns = configured.get(session)
        if isinstance(patterns, list):
            cleaned = [str(p).strip().lower() for p in patterns if str(p).strip()]
            if cleaned:
                return cleaned

    if session == "github":
        return ["github.com", "*.github.com"]
    if session in {"gmail", "google"}:
        return ["accounts.google.com", "mail.google.com", "*.google.com"]
    if "." in session:
        return [session.lower(), f"*.{session.lower()}"]
    return [f"{session.lower()}.com", f"*.{session.lower()}.com"]


def host_matches_pattern(hostname, pattern):
    host = (hostname or "").lower().strip(".")
    pat = (pattern or "").lower().strip(".")
    if not host or not pat:
        return False
    if pat.startswith("*."):
        suffix = pat[1:]
        return host.endswith(suffix) and host != suffix[1:]
    return host == pat


def session_url_allowed(session, url, cfg):
    if session == "default":
        return True
    try:
        parsed = urlparse(str(url))
    except Exception:
        return False
    if parsed.scheme.lower() != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    allowlist = resolve_session_allowlist(session, cfg)
    if not allowlist:
        return False
    return any(host_matches_pattern(host, pattern) for pattern in allowlist)


def portal_status_path(service):
    return IPC_DIR / f"login-portal-{service}.json"


def service_profile_dir(session):
    if session == "default":
        return Path("/var/lib/void")
    return SESSIONS_ROOT / session / "browser-context"


def ensure_session_profile_dir(session):
    profile_dir = service_profile_dir(session)
    if session != "default" and not SESSIONS_ROOT.exists():
        raise RuntimeError("Encrypted /secrets volume is not mounted")
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError):
        if session == "default":
            profile_dir = Path("/tmp/void-browser-default")
            profile_dir.mkdir(parents=True, exist_ok=True)
        else:
            raise
    # Verify the directory is actually writable (catches LUKS I/O errors)
    try:
        test_file = profile_dir / ".write-test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
    except OSError as e:
        raise RuntimeError(f"Session profile dir not writable: {e}")
    for sub in (".cache", ".config", ".local/share"):
        try:
            (profile_dir / sub).mkdir(parents=True, exist_ok=True)
        except (PermissionError, OSError):
            if session == "default":
                profile_dir = Path("/tmp/void-browser-default")
                (profile_dir / sub).mkdir(parents=True, exist_ok=True)
            else:
                raise
    try:
        pw = pwd.getpwnam("void")
        uid, gid = pw.pw_uid, pw.pw_gid
        root = profile_dir if session == "default" else (SESSIONS_ROOT / session)
        for p in [root, profile_dir, profile_dir / ".cache", profile_dir / ".config", profile_dir / ".local", profile_dir / ".local/share"]:
            if p.exists():
                os.chown(p, uid, gid)
                os.chmod(p, 0o700)
    except Exception:
        pass
    return profile_dir


def port_available(host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def choose_login_port(service, requested_port, cfg):
    host = "127.0.0.1"
    try:
        req = int(requested_port) if requested_port is not None else 0
    except (TypeError, ValueError):
        req = 0
    if req > 0 and req <= 65535 and port_available(host, req):
        return req

    try:
        base = int(cfg.get("login_port_base", 9222))
    except (TypeError, ValueError):
        base = 9222
    try:
        max_port = int(cfg.get("login_port_max", base + 100))
    except (TypeError, ValueError):
        max_port = base + 100
    if max_port < base:
        max_port = base

    spread = max_port - base + 1
    offset = (sum(ord(ch) for ch in service) % spread) if spread > 0 else 0
    ordered = [base + offset]
    ordered.extend(p for p in range(base, max_port + 1) if p != ordered[0])
    for candidate in ordered:
        if port_available(host, candidate):
            return candidate
    return None


def choose_login_url(service, cfg):
    configured = cfg.get("session_login_urls", {})
    if isinstance(configured, dict):
        raw = configured.get(service)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    if service == "github":
        return "https://github.com/login"
    if service in {"gmail", "google"}:
        return "https://accounts.google.com/"
    patterns = resolve_session_allowlist(service, cfg)
    first = next((p for p in patterns if not p.startswith("*.")), "")
    if first:
        return f"https://{first}/"
    return f"https://{service}.com/"


def cleanup_login_portals(logger):
    dead = []
    for service, info in login_portals.items():
        proc = info.get("proc")
        if proc is None:
            dead.append(service)
            continue
        if proc.poll() is None:
            continue
        dead.append(service)
        logger.info("Login portal exited service=%s exit=%s", service, proc.returncode)
    for service in dead:
        login_portals.pop(service, None)


def start_login_portal(session, params, cfg, logger):
    cleanup_login_portals(logger)
    running_info = login_portals.get(session)
    if running_info and running_info.get("proc") and running_info["proc"].poll() is None:
        touch_session_metadata(session, logger, created_if_missing=True)
        return True, running_info.get("url"), None

    if not LOGIN_PORTAL_SCRIPT.exists():
        return False, None, f"Login portal script missing: {LOGIN_PORTAL_SCRIPT}"
    if not LOGIN_NODE_SCRIPT.exists():
        return False, None, f"Login browser bridge missing: {LOGIN_NODE_SCRIPT}"

    try:
        profile_dir = ensure_session_profile_dir(session)
    except RuntimeError as e:
        return False, None, str(e)

    requested_port = params.get("port")
    port = choose_login_port(session, requested_port, cfg)
    if not port:
        return False, None, "No available login portal port in configured range"

    status_file = portal_status_path(session)
    try:
        status_file.unlink(missing_ok=True)
    except Exception:
        pass

    token = secrets.token_urlsafe(18)
    url = choose_login_url(session, cfg)
    allow_hosts = resolve_session_allowlist(session, cfg)
    try:
        max_seconds = int(cfg.get("login_session_timeout", 1800))
    except (TypeError, ValueError):
        max_seconds = 1800
    if max_seconds < 60:
        max_seconds = 60

    cmd = [
        "python3",
        str(LOGIN_PORTAL_SCRIPT),
        "--host",
        "0.0.0.0",
        "--port",
        str(port),
        "--service",
        session,
        "--profile-dir",
        str(profile_dir),
        "--token",
        token,
        "--start-url",
        url,
        "--status-file",
        str(status_file),
        "--node-script",
        str(LOGIN_NODE_SCRIPT),
        "--max-seconds",
        str(max_seconds),
    ]
    for host in allow_hosts:
        cmd.extend(["--allow-host", host])

    env = build_browser_env(session)
    cgroup_path = cfg.get("browser_cgroup", "/sys/fs/cgroup/void")
    preexec = browser_preexec_for_cgroup(cgroup_path)
    portal_log = IPC_DIR / f"login-portal-{session}.log"
    try:
        log_fd = open(portal_log, "w", encoding="utf-8")
    except Exception:
        log_fd = subprocess.DEVNULL
    logger.info("LOGIN_PORTAL cmd=%s", cmd)
    logger.info("LOGIN_PORTAL env_keys=%s", list(env.keys()))
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fd if log_fd != subprocess.DEVNULL else subprocess.DEVNULL,
            stderr=log_fd if log_fd != subprocess.DEVNULL else subprocess.DEVNULL,
            env=env,
            user="void",
            preexec_fn=preexec,
        )
    except Exception as e:
        if log_fd != subprocess.DEVNULL:
            log_fd.close()
        return False, None, f"Failed to launch login portal: {e}"

    ready_url = ""
    ready_error = ""
    deadline = time.time() + 300  # generous for virtiofs import latency
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        if status_file.exists():
            try:
                data = json.loads(status_file.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            if data.get("ready"):
                ready_url = str(data.get("url") or "").strip()
                break
            err = str(data.get("error") or "").strip()
            if err:
                ready_error = err
                break
        time.sleep(0.2)

    if not ready_url:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if not ready_error and status_file.exists():
            try:
                data = json.loads(status_file.read_text(encoding="utf-8"))
                ready_error = str(data.get("error") or "").strip()
            except Exception:
                ready_error = ""
        if not ready_error:
            ready_error = "Login portal failed to become ready"
        # Include portal log in error message for debugging
        if portal_log.exists():
            try:
                log_tail = portal_log.read_text(encoding="utf-8", errors="replace").strip()
                if log_tail:
                    ready_error = f"{ready_error}\nPortal log:\n{log_tail[-2000:]}"
            except Exception:
                pass
        if log_fd != subprocess.DEVNULL:
            try:
                log_fd.close()
            except Exception:
                pass
        return False, None, ready_error

    login_portals[session] = {
        "proc": proc,
        "port": port,
        "url": ready_url,
        "started": int(time.time()),
        "status_file": str(status_file),
    }
    touch_session_metadata(session, logger, created_if_missing=True)
    return True, ready_url, None


def stop_login_portal(session, logger):
    info = login_portals.pop(session, None)
    if not info:
        try:
            portal_status_path(session).unlink(missing_ok=True)
        except Exception:
            pass
        return
    proc = info.get("proc")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    status_file = info.get("status_file")
    if status_file:
        try:
            Path(status_file).unlink(missing_ok=True)
        except Exception:
            pass
    logger.info("Login portal stopped for session=%s", session)


def render_session_table(cfg):
    services = set(session_metadata.keys()) | set(login_portals.keys())
    if SESSIONS_ROOT.exists() and SESSIONS_ROOT.is_dir():
        for child in SESSIONS_ROOT.iterdir():
            if not child.is_dir():
                continue
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", child.name):
                continue
            if (child / "browser-context").exists():
                services.add(child.name)

    rows = [("SERVICE", "CREATED", "LAST_USED", "PORTAL", "ALLOWLIST")]
    for service in sorted(services):
        meta = session_metadata.get(service, {})
        created = format_ts(meta.get("created", 0))
        last_used = format_ts(meta.get("last_used", 0))
        portal_state = "active" if service in login_portals else "-"
        allowlist = ",".join(resolve_session_allowlist(service, cfg))
        rows.append((service, created, last_used, portal_state, allowlist))

    widths = [0, 0, 0, 0, 0]
    for row in rows:
        for idx, col in enumerate(row):
            widths[idx] = max(widths[idx], len(col))
    lines = []
    for idx, row in enumerate(rows):
        lines.append("  ".join(col.ljust(widths[i]) for i, col in enumerate(row)))
        if idx == 0:
            lines.append("  ".join("-" * widths[i] for i in range(len(widths))))
    return "\n".join(lines) + "\n"


def extract_url_from_browser_output(stdout):
    if not stdout:
        return ""
    try:
        payload = json.loads(stdout)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        for key in ("url", "value", "text", "result"):
            val = payload.get(key)
            if isinstance(val, str) and val.startswith("https://"):
                return val
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, str) and item.startswith("https://"):
                return item
            if isinstance(item, dict):
                for key in ("url", "value", "text"):
                    val = item.get(key)
                    if isinstance(val, str) and val.startswith("https://"):
                        return val
    match = re.search(r"https://[^\s\"']+", stdout)
    if match:
        return match.group(0)
    return ""


def build_logger():
    logger = logging.getLogger("command-proxy-daemon")
    logger.setLevel(logging.INFO)
    logger.handlers = []
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(LOG_PATH)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(sh)
    return logger


def load_secrets_env():
    """Load credentials from /run/secrets/env into os.environ."""
    if not SECRETS_ENV.exists():
        return
    with SECRETS_ENV.open("r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                os.environ[key] = value


def load_ipc_key():
    if not IPC_KEY_PATH.exists():
        return None
    try:
        key_hex = IPC_KEY_PATH.read_text(encoding="utf-8").strip()
        if not key_hex:
            return None
        return bytes.fromhex(key_hex)
    except Exception:
        return None


def canonical_json_for_hmac(payload):
    base = dict(payload)
    base.pop("hmac", None)
    return json.dumps(base, sort_keys=True, separators=(",", ":"))


def compute_payload_hmac(payload, key_bytes):
    msg = canonical_json_for_hmac(payload).encode("utf-8")
    return hmac.new(key_bytes, msg, hashlib.sha256).hexdigest()


def verify_signed_payload(payload, key_bytes):
    provided = payload.get("hmac")
    if not isinstance(provided, str):
        return False
    if not re.fullmatch(r"[0-9a-fA-F]{64}", provided):
        return False
    expected = compute_payload_hmac(payload, key_bytes)
    return hmac.compare_digest(provided.lower(), expected.lower())


def check_request_freshness(req):
    ts = req.get("timestamp")
    try:
        ts_value = float(ts)
    except (TypeError, ValueError):
        return False, "Missing or invalid request timestamp"
    now = time.time()
    if ts_value > now + 5:
        return False, "Request timestamp is too far in the future"
    if now - ts_value > REQUEST_MAX_AGE_SECONDS:
        return False, f"Request expired (>{REQUEST_MAX_AGE_SECONDS}s old)"
    return True, None


def scrub_text(text, patterns):
    out = text
    for pattern in patterns:
        out = re.sub(pattern, "[REDACTED]", out)
    return out


def build_command_env():
    """Build a sanitized environment for command execution."""
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": "/root",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        # Pass through credential env vars (loaded from secrets)
        **{k: v for k, v in os.environ.items()
           if k.startswith(("GH_", "GITHUB_", "GCLOUD_", "CLOUDSDK_", "GOOGLE_"))
           or k in ("CLOUDSDK_CONFIG", "CLOUDSDK_CORE_PROJECT")},
    }


def build_browser_env(session="default"):
    """Sanitized env for browser execution with no credential passthrough."""
    profile_dir = ensure_session_profile_dir(session)
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(profile_dir),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "XDG_CACHE_HOME": str(profile_dir / ".cache"),
        "XDG_CONFIG_HOME": str(profile_dir / ".config"),
        "XDG_DATA_HOME": str(profile_dir / ".local/share"),
        "CHROMIUM_FLAGS": "--disable-webrtc",
        "PLAYWRIGHT_CHROMIUM_ARGS": "--disable-webrtc",
        "PLAYWRIGHT_BROWSERS_PATH": "/var/lib/void/.cache/ms-playwright",
    }


def scrub_browser_text(text):
    if not text:
        return text

    def replace(match):
        if match.lastindex:
            return f"{match.group(1)}[REDACTED]"
        return "[REDACTED]"

    out = text
    for pattern in BROWSER_SCRUB_PATTERNS:
        out = re.sub(pattern, replace, out, flags=re.IGNORECASE)
    return out


def scrub_browser_value(value, patterns):
    if isinstance(value, str):
        return scrub_text(scrub_browser_text(value), patterns)
    if isinstance(value, list):
        return [scrub_browser_value(item, patterns) for item in value]
    if isinstance(value, dict):
        return {key: scrub_browser_value(val, patterns) for key, val in value.items()}
    return value


def scrub_browser_output(stdout, patterns):
    if not stdout:
        return ""
    try:
        payload = json.loads(stdout)
    except Exception:
        return scrub_text(scrub_browser_text(stdout), patterns)
    scrubbed = scrub_browser_value(payload, patterns)
    try:
        return json.dumps(scrubbed)
    except Exception:
        return scrub_text(scrub_browser_text(stdout), patterns)


def normalize_browser_action(action):
    if isinstance(action, str):
        parts = [p for p in action.strip().split() if p]
    elif isinstance(action, (list, tuple)):
        parts = [str(p).strip() for p in action if str(p).strip()]
    else:
        parts = []
    return tuple(part.lower() for part in parts)


def flatten_values(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from flatten_values(v)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from flatten_values(item)
    elif value is not None:
        yield str(value)


def browser_contains_forbidden_tokens(values):
    for raw in flatten_values(values):
        low = raw.lower()
        for token in FORBIDDEN_TOKENS:
            if token in low:
                return token
    return None


def browser_timeout_for_action(action, requested_timeout):
    action_tuple = normalize_browser_action(action)
    timeout = LONG_BROWSER_TIMEOUT if action_tuple in BROWSER_LONG_ACTIONS else SHORT_BROWSER_TIMEOUT
    try:
        requested = int(requested_timeout)
    except (TypeError, ValueError):
        requested = timeout
    if requested > 0:
        timeout = min(timeout, requested)
    return max(1, min(timeout, MAX_BROWSER_TIMEOUT))


def parse_browser_args(params):
    args = params.get("args", [])
    if args is None:
        return []
    if not isinstance(args, list):
        return None
    return [str(arg) for arg in args]


def validate_browser_url(url):
    if not isinstance(url, str) or not url.strip():
        return "open requires a non-empty url"
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() != "https":
        return "Only https:// URLs are allowed"
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return "URL must include a hostname"
    if hostname in BROWSER_METADATA_HOSTS or hostname.endswith(".local"):
        return f"Denied URL hostname: {hostname}"
    try:
        ip = ipaddress.ip_address(hostname)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return f"Denied URL address: {hostname}"
    except ValueError:
        pass
    return None


def validate_browser_command(action, params, cfg):
    """
    Validate browser action and compile it to a safe argv.
    Returns (ok, argv, error_message).
    """
    action_tuple = normalize_browser_action(action)
    if action_tuple not in ALLOWED_BROWSER_COMMANDS:
        return False, None, f"Browser action not allowed: {' '.join(action_tuple) or str(action)}"

    session = str(params.get("session", "default")).strip() or "default"
    if not re.match(r"^[A-Za-z0-9._-]{1,64}$", session):
        return False, None, "Invalid browser session name"

    schema = ALLOWED_BROWSER_COMMANDS[action_tuple]
    args = parse_browser_args(params)
    if args is None:
        return False, None, "Browser params.args must be a list"

    compiled_args = []
    if schema == "no_args":
        if args:
            return False, None, "Action does not accept arguments"
    elif schema == "open_url":
        url = params.get("url")
        if url is None and len(args) == 1:
            url = args[0]
        elif url is None:
            return False, None, "open requires exactly one URL"
        url_error = validate_browser_url(url)
        if url_error:
            return False, None, url_error
        compiled_args = [str(url)]
    elif schema in {"selector1", "key1", "find_generic", "tab_switch", "device", "media"}:
        if len(args) != 1:
            return False, None, "Action requires exactly one argument"
        compiled_args = args
    elif schema in {"selector_text", "selector_attr", "drag"}:
        if len(args) != 2:
            return False, None, "Action requires exactly two arguments"
        compiled_args = args
    elif schema == "upload":
        if len(args) < 2:
            return False, None, "upload requires selector and file path(s)"
        compiled_args = args
    elif schema == "scroll":
        if len(args) not in (1, 2):
            return False, None, "scroll requires one or two arguments"
        compiled_args = args
    elif schema == "wait":
        if not args:
            return False, None, "wait requires arguments"
        for arg in args:
            low = arg.lower()
            if low == "--fn" or low.startswith("--fn="):
                return False, None, "wait --fn is denied by policy"
            if low == "--download" or low.startswith("--download="):
                return False, None, "wait --download is denied by policy"
        compiled_args = args
    elif schema == "find_role":
        if len(args) < 1:
            return False, None, "find role requires at least one argument"
        compiled_args = args
    elif schema in {"tab_new", "tab_close", "session_close"}:
        if len(args) > 1:
            return False, None, "Action accepts at most one argument"
        compiled_args = args
    elif schema == "session_login":
        if len(args) > 1:
            return False, None, "session login accepts at most one optional URL"
        compiled_args = args
    elif schema == "viewport":
        if len(args) not in (1, 2):
            return False, None, "set viewport requires width/height"
        compiled_args = args
    elif schema == "snapshot" or schema == "screenshot":
        if len(args) > 1:
            return False, None, "Action accepts at most one argument"
        compiled_args = args
    else:
        return False, None, "Unsupported browser action schema"

    forbidden = browser_contains_forbidden_tokens({
        "action": action_tuple,
        "session": session,
        "args": compiled_args,
    })
    if forbidden:
        return False, None, f"Forbidden token detected: {forbidden}"

    if session != "default":
        if schema == "open_url":
            target_url = compiled_args[0] if compiled_args else ""
            if not session_url_allowed(session, target_url, cfg):
                return False, None, f"Session '{session}' URL denied by origin allowlist"
        elif schema == "tab_new" and compiled_args:
            target_url = compiled_args[0]
            url_error = validate_browser_url(target_url)
            if url_error:
                return False, None, url_error
            if not session_url_allowed(session, target_url, cfg):
                return False, None, f"Session '{session}' URL denied by origin allowlist"

    argv = ["agent-browser", "--json", "--session", session, *action_tuple, *compiled_args]
    return True, argv, None


def browser_preexec_for_cgroup(cgroup_path):
    if not cgroup_path:
        return None
    procs_path = Path(cgroup_path) / "cgroup.procs"

    def _preexec():
        try:
            with procs_path.open("w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except Exception:
            pass

    return _preexec


def browser_session_allowed(action_tuple, session, cfg):
    if action_tuple in {("session", "login"), ("session", "list"), ("session", "close")}:
        return True, None
    if session in active_browser_sessions:
        return True, None
    try:
        max_sessions = int(cfg.get("max_browser_sessions", 2))
    except (TypeError, ValueError):
        max_sessions = 2
    if max_sessions < 1:
        max_sessions = 1
    if len(active_browser_sessions) >= max_sessions:
        return False, f"Browser session limit exceeded ({len(active_browser_sessions)}/{max_sessions})"
    return True, None


def browser_session_close_target(session, params):
    args = parse_browser_args(params) or []
    if args:
        return str(args[0])
    name = params.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return session


def enforce_session_origin_for_action(action_tuple, session, cfg, env, cgroup_path):
    if session == "default":
        return True, None
    if action_tuple in {
        ("session", "login"),
        ("session", "list"),
        ("session", "close"),
        ("open",),
        ("tab", "new"),
        ("get", "url"),
    }:
        return True, None
    preexec = browser_preexec_for_cgroup(cgroup_path)
    try:
        probe = subprocess.run(
            ["agent-browser", "--json", "--session", session, "get", "url"],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=6,
            env=env,
            user="void",
            preexec_fn=preexec,
        )
    except Exception:
        return False, f"Session '{session}' origin policy check failed"
    if probe.returncode != 0:
        return False, f"Session '{session}' origin policy check failed"
    current_url = extract_url_from_browser_output(probe.stdout)
    if not current_url:
        return False, f"Session '{session}' origin policy denied (unknown current URL)"
    if not session_url_allowed(session, current_url, cfg):
        return False, f"Session '{session}' origin policy denied current URL: {current_url}"
    return True, None


def check_gh_subcommand(argv):
    if len(argv) < 2:
        return False, "gh subcommand is required"
    top = argv[1]
    allowed_second = ALLOWED_GH_SUBCOMMANDS.get(top)
    if allowed_second is None and top != "status":
        return False, f"gh command group not allowed: {top}"
    if top == "status":
        return True, None
    if len(argv) < 3:
        return False, f"gh {top} subcommand is required"
    second = argv[2]
    if second not in allowed_second:
        return False, f"gh {top} subcommand not allowed: {second}"
    return True, None


def gcloud_non_flag_tokens(argv):
    parts = []
    for tok in argv[1:]:
        if tok == "--":
            break
        if tok.startswith("-"):
            continue
        parts.append(tok)
    return parts


def gcloud_is_storage_cp_download(argv):
    tokens = argv[1:]
    saw_storage = False
    saw_cp = False
    tail = []
    for tok in tokens:
        if not saw_storage:
            if tok == "storage":
                saw_storage = True
            continue
        if not saw_cp:
            if tok == "cp":
                saw_cp = True
            continue
        tail.append(tok)

    operands = [tok for tok in tail if tok and not tok.startswith("-")]
    if len(operands) < 2:
        return False, "gcloud storage cp requires source and destination"
    sources = operands[:-1]
    destination = operands[-1]
    if destination.startswith("gs://"):
        return False, "gcloud storage cp upload denied by policy"
    if not all(src.startswith("gs://") for src in sources):
        return False, "gcloud storage cp only supports gs:// sources (download-only)"
    return True, None


def check_gcloud_subcommand(argv):
    parts = gcloud_non_flag_tokens(argv)
    if not parts:
        return False, "gcloud subcommand is required"

    if len(parts) >= 2 and tuple(parts[:2]) == ("storage", "cp"):
        return gcloud_is_storage_cp_download(argv)

    for allowed_cmd in ALLOWED_GCLOUD_SUBCOMMANDS:
        if allowed_cmd == ("storage", "cp"):
            continue
        if len(parts) >= len(allowed_cmd) and tuple(parts[:len(allowed_cmd)]) == allowed_cmd:
            return True, None

    return False, f"gcloud subcommand not allowed: {' '.join(parts[:3])}"


def check_omi(argv, logger):
    """Validate omi binary checksum. Returns (ok, error_message)."""
    if len(argv) < 2:
        return False, "omi subcommand is required"

    if not OMI_BINARY.is_file():
        return False, "omi binary not installed"

    actual_hash = hashlib.sha256(OMI_BINARY.read_bytes()).hexdigest()
    expected_hash = os.environ.get(OMI_CHECKSUM_ENV, "")

    if not expected_hash:
        logger.info("DENIED tool=omi reason=missing_checksum")
        return False, "no checksum configured for omi"

    if not hmac.compare_digest(actual_hash, expected_hash):
        logger.info("DENIED tool=omi reason=checksum_mismatch")
        return False, "omi binary checksum mismatch"

    return True, None


def check_command(argv, cfg, logger):
    """Validate command against ACL. Returns (ok, error_message)."""
    if not argv:
        return False, "Empty command"

    tool = os.path.basename(argv[0])
    allowed = set(cfg.get("allowed_tools", []))
    if tool not in allowed:
        logger.info("DENIED tool=%s reason=tool_not_allowed", tool)
        return False, f"Tool not allowed: {tool}. Allowed: {', '.join(sorted(allowed))}"

    if tool == "omi":
        # Omi has its own checksum-based validation; skip
        # flag/command/global deny patterns (it's a Go binary,
        # not gh/gcloud CLI).
        return check_omi(argv, logger)

    if tool == "gh":
        sub_ok, sub_err = check_gh_subcommand(argv)
    elif tool == "gcloud":
        sub_ok, sub_err = check_gcloud_subcommand(argv)
    else:
        sub_ok, sub_err = False, f"Unsupported tool: {tool}"

    if not sub_ok:
        logger.info("DENIED tool=%s reason=subcommand_not_allowed detail=%s", tool, sub_err)
        return False, "Command denied by subcommand allowlist"

    # Check flag-level deny patterns on arg tail.
    arg_tail = " ".join(argv[1:])
    for pattern in cfg.get("flag_deny_patterns", []):
        if re.search(pattern, arg_tail):
            logger.info("DENIED tool=%s reason=flag_deny_pattern pattern=%s", tool, pattern)
            return False, "Command denied by flag policy"

    # Check command deny patterns
    cmd_str = " ".join(argv)
    for pattern in cfg.get("command_deny_patterns", []):
        if re.search(pattern, cmd_str):
            logger.info("DENIED tool=%s reason=command_deny_pattern pattern=%s", tool, pattern)
            return False, "Command denied by security policy"

    # Check global deny regex
    for regex in cfg.get("global_deny_regex", []):
        if re.search(regex, cmd_str):
            logger.info("DENIED tool=%s reason=global_deny_regex regex=%s", tool, regex)
            return False, "Command denied by global policy"

    return True, None


def process_request(req_path, cfg, logger, ipc_key):
    """Process a single request file and write response."""
    try:
        with open(req_path, "r") as f:
            req = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error("Failed to read request %s: %s", req_path, e)
        return

    req_id = str(req.get("id", req_path.stem))

    if not verify_signed_payload(req, ipc_key):
        logger.info("DENIED id=%s reason=invalid_hmac", req_id)
        response = {
            "id": req_id,
            "exit_code": 1,
            "stdout": "",
            "stderr": "Request authentication failed",
            "error": "auth",
        }
    else:
        fresh_ok, fresh_err = check_request_freshness(req)
        if not fresh_ok:
            logger.info("DENIED id=%s reason=expired_request detail=%s", req_id, fresh_err)
            response = {
                "id": req_id,
                "exit_code": 1,
                "stdout": "",
                "stderr": fresh_err,
                "error": "expired",
            }
        else:
            request_tool = req.get("tool")
            if request_tool == "browser":
                action = req.get("action", "")
                session = str(req.get("session", "default")).strip() or "default"
                params = req.get("params", {})
                if not isinstance(params, dict):
                    params = {}
                params = dict(params)
                params["session"] = session
                timeout = browser_timeout_for_action(action, req.get("timeout"))
                action_tuple = normalize_browser_action(action)

                logger.info("REQUEST id=%s tool=browser action=%s session=%s", req_id, " ".join(action_tuple), session)
                if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", session):
                    response = {
                        "id": req_id,
                        "exit_code": 1,
                        "stdout": "",
                        "stderr": "Invalid browser session name",
                        "error": "denied",
                    }
                elif action_tuple == ("session", "list"):
                    cleanup_login_portals(logger)
                    response = {
                        "id": req_id,
                        "exit_code": 0,
                        "stdout": render_session_table(cfg),
                        "stderr": "",
                        "error": None,
                    }
                elif action_tuple == ("session", "login"):
                    args = parse_browser_args(params) or []
                    if args:
                        login_url = args[0]
                        url_error = validate_browser_url(login_url)
                        if url_error:
                            response = {
                                "id": req_id,
                                "exit_code": 1,
                                "stdout": "",
                                "stderr": url_error,
                                "error": "denied",
                            }
                        elif not session_url_allowed(session, login_url, cfg):
                            response = {
                                "id": req_id,
                                "exit_code": 1,
                                "stdout": "",
                                "stderr": f"Session '{session}' URL denied by origin allowlist",
                                "error": "denied",
                            }
                        else:
                            params["url"] = login_url
                            portal_ok, portal_url, portal_err = start_login_portal(session, params, cfg, logger)
                            if not portal_ok:
                                response = {
                                    "id": req_id,
                                    "exit_code": 1,
                                    "stdout": "",
                                    "stderr": portal_err or "Failed to start login portal",
                                    "error": "internal",
                                }
                            else:
                                response = {
                                    "id": req_id,
                                    "exit_code": 0,
                                    "stdout": (
                                        f"Login portal ready for session '{session}'.\n"
                                        f"Open: {portal_url}\n"
                                        "Click Done in the portal when finished.\n"
                                    ),
                                    "stderr": "",
                                    "error": None,
                                }
                    else:
                        portal_ok, portal_url, portal_err = start_login_portal(session, params, cfg, logger)
                        if not portal_ok:
                            response = {
                                "id": req_id,
                                "exit_code": 1,
                                "stdout": "",
                                "stderr": portal_err or "Failed to start login portal",
                                "error": "internal",
                            }
                        else:
                            response = {
                                "id": req_id,
                                "exit_code": 0,
                                "stdout": (
                                    f"Login portal ready for session '{session}'.\n"
                                    f"Open: {portal_url}\n"
                                    "Click Done in the portal when finished.\n"
                                ),
                                "stderr": "",
                                "error": None,
                            }
                elif action_tuple == ("session", "close"):
                    target = browser_session_close_target(session, params)
                    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", target):
                        response = {
                            "id": req_id,
                            "exit_code": 1,
                            "stdout": "",
                            "stderr": "Invalid browser session name",
                            "error": "denied",
                        }
                    else:
                        stop_login_portal(target, logger)
                        scrub_patterns = cfg.get("sensitive_output_regex", [])
                        close_stderr = ""
                        try:
                            env = build_browser_env(target)
                            cgroup_path = cfg.get("browser_cgroup", "/sys/fs/cgroup/void")
                            preexec = browser_preexec_for_cgroup(cgroup_path)
                            probe = subprocess.run(
                                ["agent-browser", "--json", "--session", target, "session", "close", target],
                                shell=False,
                                check=False,
                                capture_output=True,
                                text=True,
                                timeout=10,
                                env=env,
                                user="void",
                                preexec_fn=preexec,
                            )
                            close_stderr = scrub_text(scrub_browser_text(probe.stderr), scrub_patterns)
                        except Exception:
                            close_stderr = ""

                        profile = service_profile_dir(target)
                        try:
                            if profile.exists():
                                shutil.rmtree(profile, ignore_errors=True)
                            if profile.parent.exists():
                                profile.parent.rmdir()
                        except Exception:
                            pass

                        active_browser_sessions.discard(target)
                        session_metadata.pop(target, None)
                        save_session_metadata(logger)
                        stderr_out = close_stderr.strip()
                        if stderr_out:
                            stderr_out = f"{stderr_out}\n"
                        response = {
                            "id": req_id,
                            "exit_code": 0,
                            "stdout": f"Closed session '{target}' and wiped stored browser context.\n",
                            "stderr": stderr_out,
                            "error": None,
                        }
                else:
                    ok, argv, error_msg = validate_browser_command(action, params, cfg)
                    if not ok:
                        response = {
                            "id": req_id,
                            "exit_code": 1,
                            "stdout": "",
                            "stderr": error_msg,
                            "error": "denied",
                        }
                    else:
                        session_ok, session_error = browser_session_allowed(action_tuple, session, cfg)
                        if not session_ok:
                            logger.info("DENIED id=%s tool=browser reason=session_limit", req_id)
                            response = {
                                "id": req_id,
                                "exit_code": 1,
                                "stdout": "",
                                "stderr": session_error,
                                "error": "denied",
                            }
                        else:
                            try:
                                env = build_browser_env(session)
                            except RuntimeError as e:
                                response = {
                                    "id": req_id,
                                    "exit_code": 1,
                                    "stdout": "",
                                    "stderr": str(e),
                                    "error": "denied",
                                }
                            else:
                                cgroup_path = cfg.get("browser_cgroup", "/sys/fs/cgroup/void")
                                origin_ok, origin_err = enforce_session_origin_for_action(
                                    action_tuple, session, cfg, env, cgroup_path
                                )
                                if not origin_ok:
                                    response = {
                                        "id": req_id,
                                        "exit_code": 1,
                                        "stdout": "",
                                        "stderr": origin_err,
                                        "error": "denied",
                                    }
                                else:
                                    preexec = browser_preexec_for_cgroup(cgroup_path)
                                    try:
                                        proc = subprocess.run(
                                            argv,
                                            shell=False,
                                            check=False,
                                            capture_output=True,
                                            text=True,
                                            timeout=min(timeout, MAX_BROWSER_TIMEOUT),
                                            env=env,
                                            user="void",
                                            preexec_fn=preexec,
                                        )
                                        scrub_patterns = cfg.get("sensitive_output_regex", [])
                                        response = {
                                            "id": req_id,
                                            "exit_code": proc.returncode,
                                            "stdout": scrub_browser_output(proc.stdout, scrub_patterns),
                                            "stderr": scrub_text(scrub_browser_text(proc.stderr), scrub_patterns),
                                            "error": None,
                                        }
                                        if proc.returncode == 0:
                                            active_browser_sessions.add(session)
                                            touch_session_metadata(session, logger, created_if_missing=True)
                                        logger.info("COMPLETED id=%s tool=browser exit_code=%d", req_id, proc.returncode)
                                    except subprocess.TimeoutExpired:
                                        response = {
                                            "id": req_id,
                                            "exit_code": 124,
                                            "stdout": "",
                                            "stderr": f"Browser command timed out after {timeout}s",
                                            "error": "timeout",
                                        }
                                        logger.info("TIMEOUT id=%s tool=browser timeout=%d", req_id, timeout)
                                    except FileNotFoundError:
                                        response = {
                                            "id": req_id,
                                            "exit_code": 127,
                                            "stdout": "",
                                            "stderr": "Command not found: agent-browser",
                                            "error": "not_found",
                                        }
                                        logger.info("NOT_FOUND id=%s tool=browser", req_id)
                                    except Exception as e:
                                        response = {
                                            "id": req_id,
                                            "exit_code": 1,
                                            "stdout": "",
                                            "stderr": f"Browser execution failed: {e}",
                                            "error": "internal",
                                        }
                                        logger.error("EXCEPTION id=%s tool=browser error=%s", req_id, e)
            else:
                argv = req.get("argv", [])
                timeout = min(req.get("timeout", 60), 300)  # cap at 5 minutes
                logger.info("REQUEST id=%s argv=%s", req_id, argv[:3])  # log first 3 args only

                if argv and argv[0] == "__status__":
                    response = {
                        "id": req_id,
                        "exit_code": 0,
                        "stdout": "ok\n",
                        "stderr": "",
                        "error": None,
                    }
                    logger.info("COMPLETED id=%s tool=__status__ exit_code=0", req_id)
                else:
                    ok, error_msg = check_command(argv, cfg, logger)
                    if not ok:
                        response = {
                            "id": req_id,
                            "exit_code": 1,
                            "stdout": "",
                            "stderr": error_msg,
                            "error": "denied",
                        }
                    else:
                        tool = os.path.basename(argv[0])
                        response = None

                        # Omi: re-verify checksum (TOCTOU defense),
                        # then rewrite argv[0] to the verified binary path.
                        if tool == "omi":
                            omi_ok, omi_err = check_omi(argv, logger)
                            if not omi_ok:
                                response = {
                                    "id": req_id,
                                    "exit_code": 1,
                                    "stdout": "",
                                    "stderr": omi_err,
                                    "error": "denied",
                                }
                            else:
                                argv = [str(OMI_BINARY)] + argv[1:]

                        if not response:
                            env = build_command_env()
                            # Strip OMI_SHA256 from subprocess env (defense in depth)
                            env.pop(OMI_CHECKSUM_ENV, None)
                            try:
                                proc = subprocess.run(
                                    argv,
                                    shell=False,
                                    check=False,
                                    capture_output=True,
                                    text=True,
                                    timeout=timeout,
                                    env=env,
                                )
                                scrub_patterns = cfg.get("sensitive_output_regex", [])
                                response = {
                                    "id": req_id,
                                    "exit_code": proc.returncode,
                                    "stdout": scrub_text(proc.stdout, scrub_patterns),
                                    "stderr": scrub_text(proc.stderr, scrub_patterns),
                                    "error": None,
                                }
                                logger.info("COMPLETED id=%s tool=%s exit_code=%d", req_id, tool, proc.returncode)
                            except subprocess.TimeoutExpired:
                                response = {
                                    "id": req_id,
                                    "exit_code": 124,
                                    "stdout": "",
                                    "stderr": f"Command timed out after {timeout}s",
                                    "error": "timeout",
                                }
                                logger.info("TIMEOUT id=%s tool=%s timeout=%d", req_id, tool, timeout)
                            except FileNotFoundError:
                                response = {
                                    "id": req_id,
                                    "exit_code": 127,
                                    "stdout": "",
                                    "stderr": f"Command not found: {argv[0]}",
                                    "error": "not_found",
                                }
                                logger.info("NOT_FOUND id=%s tool=%s", req_id, tool)
                            except Exception as e:
                                response = {
                                    "id": req_id,
                                    "exit_code": 1,
                                    "stdout": "",
                                    "stderr": "Command execution failed",
                                    "error": "internal",
                                }
                                logger.error("EXCEPTION id=%s error=%s", req_id, e)

    response.setdefault("id", req_id)
    response["timestamp"] = int(time.time())
    response["hmac"] = compute_payload_hmac(response, ipc_key)

    # Atomic write: write .tmp, rename to .json
    resp_path = RESPONSES_DIR / f"{req_path.stem}.json"
    tmp_path = RESPONSES_DIR / f"{req_path.stem}.tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(response, f)
        tmp_path.rename(resp_path)
    except IOError as e:
        logger.error("Failed to write response for %s: %s", req_id, e)

    # Remove processed request
    try:
        req_path.unlink()
    except IOError:
        pass


def main():
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    logger = build_logger()
    logger.info("Command proxy daemon starting (PID %d)", os.getpid())

    # Load credentials into daemon env
    load_secrets_env()

    cfg = load_config()
    logger.info("Allowed tools: %s", cfg.get("allowed_tools", []))
    load_session_metadata(logger)

    ipc_key = load_ipc_key()
    if not ipc_key:
        logger.error("IPC key missing or invalid at %s", IPC_KEY_PATH)
        return 1

    # Ensure IPC directories exist
    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    RESPONSES_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("Polling %s for requests...", REQUESTS_DIR)
    heartbeat_counter = 0

    while running:
        # Process any pending requests
        try:
            for req_file in sorted(REQUESTS_DIR.glob("*.json")):
                if not running:
                    break
                process_request(req_file, cfg, logger, ipc_key)
            cleanup_login_portals(logger)
        except Exception as e:
            logger.error("Poll loop error: %s", e)

        # Write heartbeat every ~5 seconds (50 iterations * 0.1s)
        heartbeat_counter += 1
        if heartbeat_counter >= 50:
            heartbeat_counter = 0
            try:
                HEARTBEAT_FILE.write_text(str(int(time.time())))
            except IOError:
                pass

        time.sleep(POLL_INTERVAL)

    for session_name in list(login_portals.keys()):
        stop_login_portal(session_name, logger)
    logger.info("Daemon shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
PY_DAEMON
  chmod 0755 "$target"
}

write_proxy_config() {
  local target="$1"
  cat >"$target" <<'YAML_PROXY'
allowed_tools:
  - gh
  - gcloud
  - omi
command_deny_patterns:
  - '^gh\s+auth\s+token\b'
  - '^gh\s+auth\s+login\b'
  - '^gcloud\s+auth\s+print-access-token\b'
  - '^gcloud\s+auth\s+print-identity-token\b'
  - '^gcloud\s+config\s+set\b'
  - '^gcloud\s+auth\s+login\b'
global_deny_regex:
  - '(?i)--log-http'
  - '(?i)--verbosity=debug'
  - '(?i)\bsecret\s+get\b.*\b(?:--output|-o|--format)\s*(?:yaml|json)\b'
flag_deny_patterns:
  - '(?i)(?:^|\s)--body-file(?:=|\s+)\S+'
  - '(?i)(?:^|\s)--jq(?:=|\s+)(?:file://|/|~/|\./|\.\./)\S*'
  - '(?i)(?:^|\s)--template(?:=|\s+)(?:file://|/|~/|\./|\.\./)\S*'
  - '(?i)(?:^|\s)--json(?:=|\s+\S+)?(?:\s+|$).*(?:^|\s)--jq(?:=|\s+\S+)?(?:\s+|$)|(?:^|\s)--jq(?:=|\s+\S+)?(?:\s+|$).*(?:^|\s)--json(?:=|\s+\S+)?(?:\s+|$)'
sensitive_output_regex:
  - 'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9._-]{8,}\.[A-Za-z0-9._-]{8,}'
  - 'AKIA[0-9A-Z]{16}'
  - 'ASIA[0-9A-Z]{16}'
  - 'glpat-[A-Za-z0-9_-]{20,}'
  - 'github_pat_[A-Za-z0-9_]{22,}'
  - '-----BEGIN (?:RSA|EC|OPENSSH|PGP|PRIVATE) KEY-----'
  - '(?i)kubeconfig'
  - 'gho_[A-Za-z0-9_]{36}'
  - 'ghp_[A-Za-z0-9_]{36}'
  - 'ghs_[A-Za-z0-9]{36}'
  - 'ghu_[A-Za-z0-9]{36}'
  - 'glsa-[A-Za-z0-9-]{20,}'
  - 'dop_v1_[a-f0-9]{64}'
  - 'op://[^\s]+'
  - '(?i)aws[_-]?session[_-]?token\s*[:=]\s*\S+'
  - 'ya29\.[A-Za-z0-9_-]+'
  - 'Bearer [A-Za-z0-9._-]+'
  - 'Basic [A-Za-z0-9+/=]{20,}'
  - 'xox[bpras]-[A-Za-z0-9-]{10,}'
  - 'npm_[A-Za-z0-9]{36}'
  - 'sk-[A-Za-z0-9]{20,}'
  - 'sk-ant-[A-Za-z0-9-]{20,}'
  - 'AGE-SECRET-KEY-[A-Z0-9]{59}'
max_browser_sessions: 2
browser_cgroup: /sys/fs/cgroup/void
login_port_base: 9222
login_port_max: 9322
login_session_timeout: 1800
session_origin_allowlist:
  github:
    - github.com
    - '*.github.com'
  gmail:
    - accounts.google.com
    - mail.google.com
    - '*.google.com'
session_login_urls:
  github: https://github.com/login
  gmail: https://accounts.google.com/
YAML_PROXY
  chmod 0640 "$target"
}

write_login_browser_bridge_script() {
  local target="$1"
  cat >"$target" <<'LOGIN_BRIDGE_JS'
#!/usr/bin/env python3
"""
Deprecated login bridge stub.

The login portal now launches Chromium directly via CDP and does not use this
script anymore. Keep this file as a compatibility placeholder because daemon
startup still checks that the path exists.
"""
import argparse
import sys


def main():
    parser = argparse.ArgumentParser(description="void-login-browser bridge stub")
    parser.add_argument("--profile-dir")
    parser.add_argument("--start-url")
    parser.add_argument("--comm-dir")
    parser.parse_args()
    sys.stderr.write("void-login-browser bridge is deprecated; portal uses direct CDP\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
LOGIN_BRIDGE_JS
  chmod 0755 "$target"
}

write_login_portal_script() {
  local target="$1"
  cat >"$target" <<'LOGIN_PORTAL_PY'
#!/usr/bin/env python3
import sys
import time as _time
_portal_start = _time.monotonic()
def _plog(msg):
    elapsed = _time.monotonic() - _portal_start
    print(f"[portal +{elapsed:.1f}s] {msg}", flush=True)
_plog("python started, importing stdlib...")
import argparse
import asyncio
import contextlib
import json
import os
import re
import signal
import threading
import time
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlparse
_plog("stdlib imported, importing websockets...")
import websockets
_plog("all imports done")

HTML_PAGE = """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Void Human Login Mode</title>
  <style>
    * { box-sizing: border-box; }
    body { margin: 0; font-family: ui-sans-serif, sans-serif; background: #0f172a; color: #e2e8f0; }
    #top { display: flex; gap: 8px; align-items: center; padding: 10px; background: #111827; position: sticky; top: 0; z-index: 2; }
    #status { font-size: 12px; opacity: 0.95; min-width: 120px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    #done { border: 0; padding: 8px 12px; border-radius: 6px; background: #22c55e; color: #052e16; font-weight: 600; cursor: pointer; }
    #nav { border: 0; padding: 8px 12px; border-radius: 6px; background: #38bdf8; color: #082f49; font-weight: 600; cursor: pointer; }
    #url { flex: 1; border: 1px solid #334155; border-radius: 6px; padding: 8px; background: #0f172a; color: #e2e8f0; min-width: 120px; }
    #viewport-wrap { display: flex; justify-content: center; padding: 10px; }
    #viewport {
      width: min(96vw, 1280px);
      height: auto;
      border: 1px solid #334155;
      background: #020617;
      user-select: none;
      touch-action: none;
      -webkit-touch-callout: none;
      -webkit-user-select: none;
      outline: none;
    }
    #hint { padding: 0 12px 12px; font-size: 12px; color: #93c5fd; }
    #ime {
      position: fixed;
      left: -9999px;
      bottom: 0;
      width: 1px;
      height: 1px;
      opacity: 0;
      pointer-events: none;
    }
  </style>
</head>
<body>
  <div id="top">
    <button id="done">Done</button>
    <button id="nav">Go</button>
    <input id="url" type="text" autocomplete="off" spellcheck="false" />
    <div id="status">connecting...</div>
  </div>
  <div id="viewport-wrap"><img id="viewport" alt="Browser viewport" /></div>
  <textarea id="ime" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false"></textarea>
  <div id="hint">Click to interact. Keyboard input is forwarded to the VM browser. Press Done when login completes.</div>
  <script>
    const statusEl = document.getElementById("status");
    const img = document.getElementById("viewport");
    const doneBtn = document.getElementById("done");
    const navBtn = document.getElementById("nav");
    const urlInput = document.getElementById("url");
    const ime = document.getElementById("ime");
    const qp = new URLSearchParams(location.search);
    const token = qp.get("token") || "";
    const wsPath = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws?token=${encodeURIComponent(token)}`;
    const screenshotUrl = `${location.origin}/screenshot?token=${encodeURIComponent(token)}`;
    let ws = null;
    let reconnectDelay = 250;
    let reconnectTimer = null;
    let suppressClickUntil = 0;
    const activePointers = new Map();
    let frameW = 0;
    let frameH = 0;
    let pollTimer = null;
    let pollActive = false;

    function setStatus(text) {
      statusEl.textContent = text || "";
    }

    function send(obj) {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(obj));
      }
    }

    function connect() {
      if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
      ws = new WebSocket(wsPath);
      ws.onopen = () => {
        reconnectDelay = 250;
        setStatus("connected");
      };
      ws.onclose = () => {
        setStatus("reconnecting...");
        if (reconnectTimer) clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connect, reconnectDelay);
        reconnectDelay = Math.min(4000, Math.floor(reconnectDelay * 1.7));
      };
      ws.onerror = () => setStatus("connection error");
      ws.onmessage = onMessage;
    }

    function onMessage(ev) {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "notice") {
          setStatus(msg.message || "notice");
        }
      } catch (_) {}
    }

    function startScreenshotPoll() {
      if (pollActive) return;
      pollActive = true;
      pollScreenshot();
    }

    function stopScreenshotPoll() {
      pollActive = false;
      if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    }

    function pollScreenshot() {
      if (!pollActive) return;
      const t0 = Date.now();
      fetch(screenshotUrl)
        .then(r => { if (!r.ok) throw new Error(r.status); return r.blob(); })
        .then(blob => {
          const objectUrl = URL.createObjectURL(blob);
          const prev = img.src;
          img.src = objectUrl;
          if (prev && prev.startsWith("blob:")) URL.revokeObjectURL(prev);
          setStatus("live");
          const elapsed = Date.now() - t0;
          const delay = Math.max(50, 300 - elapsed);
          pollTimer = setTimeout(pollScreenshot, delay);
        })
        .catch((err) => {
          setStatus("screenshot: " + err.message);
          pollTimer = setTimeout(pollScreenshot, 1000);
        });
    }

    connect();
    startScreenshotPoll();

    doneBtn.addEventListener("click", () => send({ type: "done" }));
    navBtn.addEventListener("click", () => {
      const nextUrl = (urlInput.value || "").trim();
      if (nextUrl) send({ type: "goto", url: nextUrl });
    });
    urlInput.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        const nextUrl = (urlInput.value || "").trim();
        if (nextUrl) send({ type: "goto", url: nextUrl });
      }
    });

    function focusIme() {
      try { ime.focus({ preventScroll: true }); } catch (_) { ime.focus(); }
    }

    function clientToImagePoint(cx, cy) {
      const rect = img.getBoundingClientRect();
      if (!img.naturalWidth || !img.naturalHeight || rect.width <= 0 || rect.height <= 0) return;
      const sx = img.naturalWidth / rect.width;
      const sy = img.naturalHeight / rect.height;
      return {
        x: Math.round((cx - rect.left) * sx),
        y: Math.round((cy - rect.top) * sy)
      };
    }

    function buttonName(button) {
      if (button === 2) return "right";
      if (button === 1) return "middle";
      return "left";
    }

    function sendMouse(eventType, ev) {
      const pt = clientToImagePoint(ev.clientX, ev.clientY);
      if (!pt) return;
      send({
        type: "mouse",
        event: eventType,
        x: pt.x,
        y: pt.y,
        button: buttonName(ev.button),
        buttons: ev.buttons || 0,
        ctrlKey: ev.ctrlKey,
        shiftKey: ev.shiftKey,
        altKey: ev.altKey,
        metaKey: ev.metaKey
      });
    }

    function sendTouch(eventType, ev) {
      const pt = clientToImagePoint(ev.clientX, ev.clientY);
      if (!pt) return;
      send({
        type: "touch",
        event: eventType,
        pointerId: ev.pointerId || 1,
        x: pt.x,
        y: pt.y
      });
    }

    img.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      suppressClickUntil = Date.now() + 400;
      activePointers.set(ev.pointerId, ev.pointerType);
      focusIme();
      try { img.setPointerCapture(ev.pointerId); } catch (_) {}
      if (ev.pointerType === "touch") {
        sendTouch("start", ev);
      } else {
        sendMouse("move", ev);
        sendMouse("down", ev);
      }
    }, { passive: false });

    img.addEventListener("pointermove", (ev) => {
      if (!activePointers.has(ev.pointerId)) return;
      ev.preventDefault();
      if (ev.pointerType === "touch") {
        sendTouch("move", ev);
      } else {
        sendMouse("move", ev);
      }
    }, { passive: false });

    function endPointer(ev, kind) {
      if (!activePointers.has(ev.pointerId)) return;
      ev.preventDefault();
      activePointers.delete(ev.pointerId);
      if (kind === "cancel") {
        if (ev.pointerType === "touch") sendTouch("cancel", ev);
      } else if (ev.pointerType === "touch") {
        sendTouch("end", ev);
      } else {
        sendMouse("up", ev);
      }
      try { img.releasePointerCapture(ev.pointerId); } catch (_) {}
    }

    img.addEventListener("pointerup", (ev) => endPointer(ev, "up"), { passive: false });
    img.addEventListener("pointercancel", (ev) => endPointer(ev, "cancel"), { passive: false });

    img.addEventListener("click", (ev) => {
      if (Date.now() < suppressClickUntil) return;
      const pt = clientToImagePoint(ev.clientX, ev.clientY);
      if (!pt) return;
      focusIme();
      send({ type: "click", x: pt.x, y: pt.y, button: "left" });
    });

    img.addEventListener("wheel", (ev) => {
      ev.preventDefault();
      const pt = clientToImagePoint(ev.clientX, ev.clientY);
      send({
        type: "scroll",
        dx: Math.round(ev.deltaX),
        dy: Math.round(ev.deltaY),
        x: pt ? pt.x : Math.round(frameW / 2),
        y: pt ? pt.y : Math.round(frameH / 2)
      });
    }, { passive: false });

    function sendKey(ev) {
      send({
        type: "key",
        key: ev.key,
        code: ev.code,
        ctrlKey: ev.ctrlKey,
        shiftKey: ev.shiftKey,
        altKey: ev.altKey,
        metaKey: ev.metaKey,
        repeat: ev.repeat
      });
    }

    function isControlKey(ev) {
      if (ev.ctrlKey || ev.metaKey || ev.altKey) return true;
      const k = ev.key || "";
      if (k.length !== 1) return true;
      return false;
    }

    window.addEventListener("keydown", (ev) => {
      if (document.activeElement === urlInput) return;
      if (document.activeElement === ime && !isControlKey(ev)) return;
      ev.preventDefault();
      sendKey(ev);
    });

    ime.addEventListener("keydown", (ev) => {
      if (!isControlKey(ev)) return;
      ev.preventDefault();
      sendKey(ev);
    });

    ime.addEventListener("input", () => {
      const text = ime.value || "";
      if (text) send({ type: "type", text });
      ime.value = "";
    });

    window.addEventListener("paste", (ev) => {
      const text = ev.clipboardData ? ev.clipboardData.getData("text") : "";
      if (!text) return;
      ev.preventDefault();
      send({ type: "type", text });
    });

    window.addEventListener("focus", () => focusIme());
    focusIme();
  </script>
</body>
</html>
"""

LOGIN_HINTS = ("login", "signin", "sign-in", "oauth", "auth", "consent", "challenge")
DEVTOOLS_RE = re.compile(r"DevTools listening on\s+(ws://\S+)")
DEFAULT_VIEWPORT_W = 1280
DEFAULT_VIEWPORT_H = 800


class CDPError(RuntimeError):
    pass


class CDPDisconnected(CDPError):
    pass


def host_matches_pattern(hostname, pattern):
    host = (hostname or "").lower().strip(".")
    pat = (pattern or "").lower().strip(".")
    if not host or not pat:
        return False
    if pat.startswith("*."):
        suffix = pat[1:]
        return host.endswith(suffix) and host != suffix[1:]
    return host == pat


def url_host_allowed(url, allow_hosts):
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme.lower() != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return any(host_matches_pattern(host, pat) for pat in allow_hosts)


def looks_like_login_url(url):
    low = (url or "").lower()
    return any(token in low for token in LOGIN_HINTS)


def as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def as_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def clamp(value, low, high):
    return max(low, min(high, value))


def write_status(path, payload):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def normalize_navigation_url(raw_url):
    url = str(raw_url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url.lstrip("/")
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    if parsed.scheme.lower() not in {"http", "https"}:
        return ""
    if not parsed.hostname:
        return ""
    return url


def find_chromium_binary():
    root = Path("/var/lib/void/.cache/ms-playwright")
    if root.exists():
        # Prefer headless shell (renders screenshots correctly) over full chrome
        for name in ("chrome-headless-shell", "chrome"):
            matches = sorted(root.rglob(name))
            for candidate in matches:
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)
    for fallback in ("/usr/bin/chromium", "/usr/bin/chromium-browser"):
        if os.path.isfile(fallback) and os.access(fallback, os.X_OK):
            return fallback
    raise RuntimeError("chrome/chrome-headless-shell not found under /var/lib/void/.cache/ms-playwright/")


class CDPConnection:
    """CDP over pipe (--remote-debugging-pipe).

    Chrome uses fd 3 (read) and fd 4 (write) for pipe-based CDP.
    Messages are JSON separated by null bytes (\\x00).
    Uses a blocking thread reader (krunvm asyncio pipe readers don't work).
    """

    def __init__(self, read_fd, write_fd):
        self._read_fd = read_fd    # fd to read Chrome responses from
        self._write_fd = write_fd  # fd to write Chrome commands to
        self._next_id = 0
        self._pending = {}
        self._reader_thread = None
        self._send_lock = asyncio.Lock()
        self.events = asyncio.Queue(maxsize=2048)
        self._closed = asyncio.Event()
        self._loop = None

    @property
    def closed(self):
        return self._closed.is_set()

    async def connect(self):
        self._loop = asyncio.get_running_loop()
        _plog("CDP pipe: starting blocking reader thread")
        self._reader_thread = threading.Thread(target=self._blocking_reader, daemon=True)
        self._reader_thread.start()

    def _blocking_reader(self):
        """Blocking read from Chrome's CDP pipe in a separate thread."""
        buf = b""
        _msg_count = 0
        try:
            while True:
                chunk = os.read(self._read_fd, 65536)
                if not chunk:
                    _plog("CDP reader: pipe EOF")
                    break
                buf += chunk
                while b"\0" in buf:
                    msg_bytes, buf = buf.split(b"\0", 1)
                    if not msg_bytes:
                        continue
                    try:
                        msg = json.loads(msg_bytes)
                    except Exception:
                        continue
                    _msg_count += 1
                    method = msg.get("method", "")
                    if _msg_count <= 30:
                        preview = str(msg)[:200] if not method else f"method={method}"
                        _plog(f"CDP reader msg #{_msg_count}: {preview}")
                    if "id" in msg:
                        call_id = msg.get("id")
                        fut = self._pending.pop(call_id, None)
                        if fut and not fut.done():
                            if "error" in msg:
                                err = msg.get("error", {})
                                message = err.get("message") if isinstance(err, dict) else str(err)
                                self._loop.call_soon_threadsafe(
                                    fut.set_exception, CDPError(message or "CDP call failed"))
                            else:
                                self._loop.call_soon_threadsafe(
                                    fut.set_result, msg.get("result", {}))
                        continue
                    # Event message
                    try:
                        self.events.put_nowait(msg)
                    except asyncio.QueueFull:
                        with contextlib.suppress(asyncio.QueueEmpty):
                            self.events.get_nowait()
                        with contextlib.suppress(asyncio.QueueFull):
                            self.events.put_nowait(msg)
        except Exception as exc:
            self._fail_pending(CDPDisconnected(str(exc)))
        finally:
            self._loop.call_soon_threadsafe(self._closed.set)
            self._fail_pending(CDPDisconnected("CDP pipe closed"))

    def _fail_pending(self, exc):
        for fut in list(self._pending.values()):
            if not fut.done():
                try:
                    self._loop.call_soon_threadsafe(fut.set_exception, exc)
                except Exception:
                    pass
        self._pending.clear()

    async def call(self, method, params=None, session_id=None, timeout=10):
        if self.closed:
            raise CDPDisconnected("CDP pipe is not connected")
        payload = {"method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        loop = asyncio.get_running_loop()
        async with self._send_lock:
            self._next_id += 1
            call_id = self._next_id
            payload["id"] = call_id
            fut = loop.create_future()
            self._pending[call_id] = fut
            try:
                data = json.dumps(payload).encode() + b"\0"
                os.write(self._write_fd, data)
            except Exception as exc:
                self._pending.pop(call_id, None)
                if not fut.done():
                    fut.set_exception(CDPDisconnected(str(exc)))
                raise CDPDisconnected(str(exc)) from exc
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(call_id, None)
            if not fut.done():
                fut.cancel()
            raise CDPError(f"CDP timeout on {method}") from exc

    async def close(self):
        if self._closed.is_set():
            return
        self._closed.set()
        self._fail_pending(CDPDisconnected("CDP pipe closed"))
        with contextlib.suppress(Exception):
            os.close(self._write_fd)
        with contextlib.suppress(Exception):
            os.close(self._read_fd)


class ChromiumCDP:
    def __init__(self, profile_dir, start_url):
        self.profile_dir = profile_dir
        self.start_url = start_url
        self.chromium_path = ""
        self.proc = None
        self.cdp = None
        self.page_session_id = ""
        self.target_id = ""
        self.current_url = ""
        self.current_title = ""
        self.viewport_width = DEFAULT_VIEWPORT_W
        self.viewport_height = DEFAULT_VIEWPORT_H
        self._active_touches = {}
        self._stderr_task = None
        self._stderr_lines = []
        self._devtools_ws_url = ""
        self._event_task = None
        self._location_task = None
        self._frame_queue = asyncio.Queue(maxsize=2)
        self._latest_frame = None
        self._first_frame_event = asyncio.Event()
        self._last_frame_ts = 0.0
        self._detached = False
        self._screenshot_task = None
        self._restart_lock = asyncio.Lock()

    async def start(self):
        self.chromium_path = find_chromium_binary()
        os.makedirs(self.profile_dir, mode=0o700, exist_ok=True)
        self._first_frame_event = asyncio.Event()
        self._devtools_ws_url = ""
        self._detached = False
        self._active_touches.clear()
        self.current_url = ""
        self.current_title = ""
        self._last_frame_ts = 0.0
        while True:
            try:
                self._frame_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            await self._start_once(no_sandbox=False)
        except Exception as first_error:
            err_text = "\n".join(self._stderr_lines[-40:]).lower()
            sandbox_markers = ("no usable sandbox", "setuid sandbox", "namespace sandbox")
            if any(marker in err_text for marker in sandbox_markers):
                if os.getuid() == 0:
                    _plog("chromium sandbox unavailable while running as root; retrying with --no-sandbox")
                    await self.stop()
                    await self._start_once(no_sandbox=True)
                else:
                    await self.stop()
                    raise RuntimeError("chromium sandbox unavailable; refusing --no-sandbox for non-root user") from first_error
            else:
                await self.stop()
                raise first_error

    async def _start_once(self, no_sandbox):
        self._stderr_lines = []
        self._devtools_ws_url = ""
        pipe_reader, pipe_write_fd = await self._launch_chromium(no_sandbox=no_sandbox)
        self.cdp = CDPConnection(pipe_reader, pipe_write_fd)
        await self.cdp.connect()
        _plog("CDP pipe connected, initializing page session...")
        await self._init_page_session()
        _plog(f"page session ready: target={self.target_id} session={self.page_session_id}")
        self._event_task = asyncio.create_task(self._event_loop())
        self._location_task = asyncio.create_task(self._location_loop())
        if self.start_url:
            _plog(f"navigating to {self.start_url}")
            await self.goto(self.start_url)
            _plog("navigation sent, waiting for page to load...")
            await asyncio.sleep(3)  # Let the page render before capturing
        # Start screenshot polling AFTER navigation
        self._screenshot_task = asyncio.create_task(self._screenshot_loop())
        _plog("screenshot polling started, waiting for first frame...")
        await asyncio.wait_for(self._first_frame_event.wait(), timeout=30)
        _plog("first frame received!")
        await self._refresh_location()

    async def _launch_chromium(self, no_sandbox):
        # chrome-headless-shell doesn't need --headless; full chrome does
        is_headless_shell = "headless-shell" in self.chromium_path
        cmd = [
            self.chromium_path,
        ]
        if not is_headless_shell:
            cmd.append("--headless=new")
        cmd += [
            "--remote-debugging-pipe",
            f"--user-data-dir={self.profile_dir}",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-sync",
            "--disable-popup-blocking",
            "--disable-breakpad",
            "--metrics-recording-only",
            "--no-first-run",
            "--no-default-browser-check",
            "--password-store=basic",
            "--use-mock-keychain",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-webrtc",
            f"--window-size={DEFAULT_VIEWPORT_W},{DEFAULT_VIEWPORT_H}",
            "about:blank",
        ]
        if no_sandbox and os.getuid() == 0:
            _plog("WARNING: Chromium sandbox disabled (--no-sandbox) because process is running as root")
            cmd.append("--no-sandbox")
        _plog(f"launching chromium (pipe mode): {self.chromium_path}")
        # Chrome reads from fd 3, writes to fd 4 (from Chrome's POV).
        # We create two pipes: parent_write→chrome_read(3), chrome_write(4)→parent_read.
        chrome_read_fd, parent_write_fd = os.pipe()   # parent writes commands
        parent_read_fd, chrome_write_fd = os.pipe()    # parent reads responses
        _plog(f"pipe fds: chrome_read={chrome_read_fd} parent_write={parent_write_fd} "
              f"parent_read={parent_read_fd} chrome_write={chrome_write_fd}")

        def _pass_fds():
            """In child: dup pipes to fd 3 and 4 for Chrome."""
            if chrome_read_fd != 3:
                os.dup2(chrome_read_fd, 3)
            if chrome_write_fd != 4:
                os.dup2(chrome_write_fd, 4)
            # Close originals that aren't 3 or 4
            for fd in (chrome_read_fd, parent_write_fd, parent_read_fd, chrome_write_fd):
                if fd > 4:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

        import subprocess as _sp
        proc = _sp.Popen(
            cmd,
            stdin=_sp.DEVNULL,
            stdout=_sp.DEVNULL,
            stderr=_sp.PIPE,
            close_fds=False,
            preexec_fn=_pass_fds,
        )
        # Close child-side fds in parent
        os.close(chrome_read_fd)
        os.close(chrome_write_fd)

        self.proc = proc
        self._stderr_task = asyncio.create_task(self._stderr_reader())

        # Give Chrome a moment to start, then check it's alive
        await asyncio.sleep(0.5)
        rc = proc.poll()
        if rc is not None:
            stderr_out = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
            raise RuntimeError(f"Chromium exited immediately (code {rc}): {stderr_out[:2000]}")
        _plog("chromium launched and alive, CDP pipe ready")
        return parent_read_fd, parent_write_fd

    async def _stderr_reader(self):
        if not self.proc or not self.proc.stderr:
            return
        loop = asyncio.get_running_loop()
        try:
            while True:
                line = await loop.run_in_executor(None, self.proc.stderr.readline)
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self._stderr_lines.append(text)
                    if len(self._stderr_lines) > 400:
                        self._stderr_lines = self._stderr_lines[-200:]
                    _plog(f"[chromium] {text}")
        except Exception as exc:
            _plog(f"stderr reader error: {exc}")

    async def _init_page_session(self):
        target = await self.cdp.call("Target.createTarget", {"url": "about:blank"}, timeout=8)
        self.target_id = str(target.get("targetId") or "")
        if not self.target_id:
            raise RuntimeError("CDP Target.createTarget returned empty targetId")
        attached = await self.cdp.call(
            "Target.attachToTarget",
            {"targetId": self.target_id, "flatten": True},
            timeout=8,
        )
        self.page_session_id = str(attached.get("sessionId") or "")
        if not self.page_session_id:
            raise RuntimeError("CDP Target.attachToTarget returned empty sessionId")
        await self._cdp_call("Page.enable", timeout=8)
        await self._cdp_call("Runtime.enable", timeout=8)
        await self._cdp_call("Network.enable", timeout=8)
        await self._cdp_call("Page.setLifecycleEventsEnabled", {"enabled": True}, timeout=8)

    async def _cdp_call(self, method, params=None, timeout=8):
        if not self.cdp or not self.page_session_id:
            raise RuntimeError("CDP session not ready")
        return await self.cdp.call(
            method,
            params=params or {},
            session_id=self.page_session_id,
            timeout=timeout,
        )

    async def _event_loop(self):
        _plog("event loop started")
        _event_count = 0
        try:
            while self.cdp and not self.cdp.closed:
                event = await self.cdp.events.get()
                _event_count += 1
                method = str(event.get("method") or "")
                params = event.get("params") or {}
                event_session = str(event.get("sessionId") or "")
                if _event_count <= 20:
                    _plog(f"event #{_event_count}: method={method} session={event_session[:12]}...")
                if method == "Target.detachedFromTarget":
                    if str(params.get("sessionId") or "") == self.page_session_id:
                        self._detached = True
                    continue
                if event_session and event_session != self.page_session_id:
                    continue
                if method == "Page.screencastFrame":
                    frame_data = str(params.get("data") or "")
                    frame_session = params.get("sessionId")
                    meta = params.get("metadata") or {}
                    self.viewport_width = max(1, as_int(meta.get("deviceWidth"), self.viewport_width))
                    self.viewport_height = max(1, as_int(meta.get("deviceHeight"), self.viewport_height))
                    self._last_frame_ts = time.time()
                    frame = {
                        "format": "jpeg",
                        "data": frame_data,
                        "width": self.viewport_width,
                        "height": self.viewport_height,
                        "ts": self._last_frame_ts,
                    }
                    self._latest_frame = frame
                    if not self._first_frame_event.is_set():
                        self._first_frame_event.set()
                    if self._frame_queue.full():
                        with contextlib.suppress(asyncio.QueueEmpty):
                            self._frame_queue.get_nowait()
                    with contextlib.suppress(asyncio.QueueFull):
                        self._frame_queue.put_nowait(frame)
                    if frame_session is not None:
                        asyncio.create_task(self._ack_screencast_frame(frame_session))
                elif method == "Page.frameNavigated":
                    frame = params.get("frame") or {}
                    url = str(frame.get("url") or "")
                    if url:
                        self.current_url = url
                elif method == "Page.navigatedWithinDocument":
                    url = str(params.get("url") or "")
                    if url:
                        self.current_url = url
        except Exception as exc:
            _plog(f"CDP event loop stopped: {exc}")

    async def _ack_screencast_frame(self, frame_session_id):
        if not self.cdp or self.cdp.closed:
            return
        with contextlib.suppress(Exception):
            await self._cdp_call(
                "Page.screencastFrameAck",
                {"sessionId": frame_session_id},
                timeout=2,
            )

    async def _location_loop(self):
        while True:
            try:
                await self._refresh_location()
            except Exception:
                if not self.cdp or self.cdp.closed:
                    return
            await asyncio.sleep(0.35)

    async def _refresh_location(self):
        result = await self._cdp_call(
            "Runtime.evaluate",
            {
                "expression": "(() => ({url: location.href || '', title: document.title || ''}))()",
                "returnByValue": True,
                "awaitPromise": False,
            },
            timeout=4,
        )
        value = ((result.get("result") or {}).get("value") or {})
        if isinstance(value, dict):
            url = str(value.get("url") or "")
            title = str(value.get("title") or "")
            if url:
                self.current_url = url
            self.current_title = title

    async def _screenshot_loop(self):
        """Poll Page.captureScreenshot in a loop (screencast doesn't work in headless)."""
        _plog("screenshot polling loop started")
        while True:
            try:
                if not self.cdp or self.cdp.closed:
                    return
                result = await self._cdp_call(
                    "Page.captureScreenshot",
                    {
                        "format": "jpeg",
                        "quality": 65,
                    },
                    timeout=8,
                )
                data = str(result.get("data") or "")
                if data:
                    self._last_frame_ts = time.time()
                    frame = {
                        "format": "jpeg",
                        "data": data,
                        "width": self.viewport_width,
                        "height": self.viewport_height,
                        "ts": self._last_frame_ts,
                    }
                    self._latest_frame = frame
                    if not self._first_frame_event.is_set():
                        _plog(f"first screenshot captured ({len(data)} bytes)")
                        self._first_frame_event.set()
                    if self._frame_queue.full():
                        with contextlib.suppress(asyncio.QueueEmpty):
                            self._frame_queue.get_nowait()
                    with contextlib.suppress(asyncio.QueueFull):
                        self._frame_queue.put_nowait(frame)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if not self.cdp or self.cdp.closed:
                    return
                _plog(f"screenshot error: {exc}")
            await asyncio.sleep(0.3)

    def unhealthy(self):
        if self.proc is None:
            return True
        if self.proc.poll() is not None:
            return True
        if not self.cdp or self.cdp.closed:
            return True
        if self._detached:
            return True
        if self._first_frame_event.is_set() and (time.time() - self._last_frame_ts > 8):
            return True
        return False

    async def restart(self):
        async with self._restart_lock:
            await self.stop()
            await self.start()

    def latest_frame_message(self):
        if not self._latest_frame:
            return None
        return {
            "type": "frame",
            "url": self.current_url,
            "title": self.current_title,
            "width": self._latest_frame.get("width", self.viewport_width),
            "height": self._latest_frame.get("height", self.viewport_height),
            "image": f"data:image/{self._latest_frame.get('format', 'jpeg')};base64,{self._latest_frame.get('data', '')}",
        }

    async def next_frame_message(self, timeout=0.25):
        frame = await asyncio.wait_for(self._frame_queue.get(), timeout=timeout)
        return {
            "type": "frame",
            "url": self.current_url,
            "title": self.current_title,
            "width": frame.get("width", self.viewport_width),
            "height": frame.get("height", self.viewport_height),
            "image": f"data:image/{frame.get('format', 'jpeg')};base64,{frame.get('data', '')}",
        }

    def _resolve_coords(self, x, y):
        max_x = max(1, self.viewport_width - 1)
        max_y = max(1, self.viewport_height - 1)
        nx = clamp(as_int(x, 0), 0, max_x)
        ny = clamp(as_int(y, 0), 0, max_y)
        return nx, ny

    def _modifiers(self, payload):
        mods = 0
        if bool(payload.get("altKey")):
            mods |= 1
        if bool(payload.get("ctrlKey")):
            mods |= 2
        if bool(payload.get("metaKey")):
            mods |= 4
        if bool(payload.get("shiftKey")):
            mods |= 8
        return mods

    async def click(self, x, y, button="left", payload=None):
        mods = self._modifiers(payload or {})
        await self.mouse("move", x, y, button=button, modifiers=mods, buttons=0)
        await self.mouse("down", x, y, button=button, modifiers=mods)
        await self.mouse("up", x, y, button=button, modifiers=mods)

    async def mouse(self, event_type, x, y, button="left", buttons=None, modifiers=0):
        event_map = {"move": "mouseMoved", "down": "mousePressed", "up": "mouseReleased"}
        cdp_type = event_map.get(str(event_type or "").lower())
        if not cdp_type:
            return
        btn = str(button or "left").lower()
        if btn not in {"left", "right", "middle", "back", "forward", "none"}:
            btn = "left"
        mx, my = self._resolve_coords(x, y)
        if buttons is None:
            button_mask = {"left": 1, "right": 2, "middle": 4}.get(btn, 0)
        else:
            button_mask = as_int(buttons, 0)
        params = {
            "type": cdp_type,
            "x": mx,
            "y": my,
            "button": btn if btn != "none" else "left",
            "buttons": button_mask,
            "modifiers": modifiers,
            "clickCount": 1,
        }
        await self._cdp_call("Input.dispatchMouseEvent", params, timeout=4)

    async def scroll(self, dx, dy, x=None, y=None, payload=None):
        sx, sy = self._resolve_coords(
            x if x is not None else self.viewport_width // 2,
            y if y is not None else self.viewport_height // 2,
        )
        params = {
            "type": "mouseWheel",
            "x": sx,
            "y": sy,
            "deltaX": as_float(dx, 0),
            "deltaY": as_float(dy, 0),
            "modifiers": self._modifiers(payload or {}),
        }
        await self._cdp_call("Input.dispatchMouseEvent", params, timeout=4)

    async def touch(self, event_type, pointer_id, x, y):
        pid = as_int(pointer_id, 1)
        if pid < 1:
            pid = 1
        tx, ty = self._resolve_coords(x, y)
        kind = str(event_type or "").lower()
        if kind == "start":
            self._active_touches[pid] = (tx, ty)
            cdp_kind = "touchStart"
        elif kind == "move":
            self._active_touches[pid] = (tx, ty)
            cdp_kind = "touchMove"
        elif kind == "end":
            self._active_touches.pop(pid, None)
            cdp_kind = "touchEnd"
        elif kind == "cancel":
            self._active_touches.clear()
            cdp_kind = "touchCancel"
        else:
            return
        points = [
            {"x": px, "y": py, "radiusX": 1, "radiusY": 1, "force": 1, "id": tid}
            for tid, (px, py) in sorted(self._active_touches.items(), key=lambda item: item[0])
        ]
        await self._cdp_call(
            "Input.dispatchTouchEvent",
            {"type": cdp_kind, "touchPoints": points},
            timeout=4,
        )

    async def insert_text(self, text):
        raw = str(text or "")
        if not raw:
            return
        await self._cdp_call("Input.insertText", {"text": raw}, timeout=4)

    async def key_press(self, payload):
        key = str(payload.get("key") or "")
        code = str(payload.get("code") or "")
        if not key:
            return
        if key == "Unidentified":
            return
        modifiers = self._modifiers(payload)
        key_alias = {
            "Esc": "Escape",
            "Del": "Delete",
            "Left": "ArrowLeft",
            "Right": "ArrowRight",
            "Up": "ArrowUp",
            "Down": "ArrowDown",
            "Spacebar": " ",
        }
        key = key_alias.get(key, key)
        printable = len(key) == 1 and not (modifiers & 0b0111)
        if printable:
            await self.insert_text(key)
            return
        vk_map = {
            "Backspace": 8,
            "Tab": 9,
            "Enter": 13,
            "Shift": 16,
            "Control": 17,
            "Alt": 18,
            "Pause": 19,
            "CapsLock": 20,
            "Escape": 27,
            " ": 32,
            "PageUp": 33,
            "PageDown": 34,
            "End": 35,
            "Home": 36,
            "ArrowLeft": 37,
            "ArrowUp": 38,
            "ArrowRight": 39,
            "ArrowDown": 40,
            "Insert": 45,
            "Delete": 46,
            "Meta": 91,
            "ContextMenu": 93,
        }
        vk = vk_map.get(key, 0)
        if not vk:
            if code.startswith("Key") and len(code) == 4:
                vk = ord(code[-1].upper())
            elif code.startswith("Digit") and len(code) == 6:
                vk = ord(code[-1])
            elif len(key) == 1:
                vk = ord(key.upper())
        base = {
            "key": key,
            "code": code,
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
            "modifiers": modifiers,
            "autoRepeat": bool(payload.get("repeat")),
            "isKeypad": False,
            "isSystemKey": False,
        }
        await self._cdp_call("Input.dispatchKeyEvent", dict(base, type="rawKeyDown"), timeout=4)
        await self._cdp_call("Input.dispatchKeyEvent", dict(base, type="keyUp"), timeout=4)

    async def goto(self, url):
        target_url = normalize_navigation_url(url)
        if not target_url:
            raise RuntimeError("invalid_url")
        # Send navigation without waiting — krunvm network can be very slow
        payload = {"method": "Page.navigate", "params": {"url": target_url}}
        if self.page_session_id:
            payload["sessionId"] = self.page_session_id
        async with self.cdp._send_lock:
            self.cdp._next_id += 1
            payload["id"] = self.cdp._next_id
            data = json.dumps(payload).encode() + b"\0"
            os.write(self.cdp._write_fd, data)
        _plog(f"navigate sent (fire-and-forget): {target_url}")

    async def close(self):
        await self.stop()

    async def stop(self):
        for task_attr in ("_event_task", "_location_task", "_screenshot_task"):
            task = getattr(self, task_attr, None)
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                setattr(self, task_attr, None)
        if self.cdp:
            with contextlib.suppress(Exception):
                await self.cdp.close()
        self.cdp = None
        self.page_session_id = ""
        self.target_id = ""
        if self.proc and self.proc.poll() is None:
            with contextlib.suppress(Exception):
                self.proc.terminate()
            loop = asyncio.get_running_loop()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(loop.run_in_executor(None, self.proc.wait), timeout=4)
            if self.proc.poll() is None:
                with contextlib.suppress(Exception):
                    self.proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(loop.run_in_executor(None, self.proc.wait), timeout=2)
        self.proc = None
        if self._stderr_task:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task
            self._stderr_task = None


async def run(args, external_stop):
    started = time.time()
    stop_event = asyncio.Event()
    stop_reason = {"value": "done"}
    allow_hosts = [h.strip() for h in args.allow_host if h.strip()]
    clients = set()
    last_client_ts = {"value": time.time()}

    status_path = Path(args.status_file)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    write_status(status_path, {"ready": False})

    _ = args.node_script  # kept for CLI compatibility

    browser = ChromiumCDP(args.profile_dir, args.start_url)
    ready_written = False

    try:
        await browser.start()
    except Exception as e:
        write_status(status_path, {"ready": False, "error": str(e)})
        raise

    initial_url = browser.current_url or args.start_url

    async def broadcast(payload):
        if not clients:
            return
        raw = json.dumps(payload)
        dead = []
        for client in list(clients):
            try:
                await client.send(raw)
            except Exception:
                dead.append(client)
        for item in dead:
            clients.discard(item)

    async def send_notice(websocket, message):
        try:
            await websocket.send(json.dumps({"type": "notice", "message": message}))
        except Exception:
            pass

    async def handle_client_message(websocket, payload):
        msg_type = str(payload.get("type") or "").lower()
        if msg_type == "done":
            stop_reason["value"] = "manual_done"
            stop_event.set()
            return
        if msg_type == "goto":
            raw_url = str(payload.get("url") or "").strip()
            next_url = normalize_navigation_url(raw_url)
            if not next_url:
                await send_notice(websocket, "invalid url")
                return
            if allow_hosts and not url_host_allowed(next_url, allow_hosts):
                denied_host = (urlparse(next_url).hostname or "unknown").lower()
                await send_notice(websocket, f"HTTP 403 forbidden: host not in allowlist ({denied_host})")
                return
            await browser.goto(next_url)
            return
        if msg_type == "click":
            await browser.click(
                payload.get("x", 0),
                payload.get("y", 0),
                button=payload.get("button", "left"),
                payload=payload,
            )
            return
        if msg_type == "mouse":
            await browser.mouse(
                payload.get("event", ""),
                payload.get("x", 0),
                payload.get("y", 0),
                button=payload.get("button", "left"),
                buttons=payload.get("buttons", 0),
                modifiers=browser._modifiers(payload),
            )
            return
        if msg_type == "touch":
            await browser.touch(
                payload.get("event", ""),
                payload.get("pointerId", 1),
                payload.get("x", 0),
                payload.get("y", 0),
            )
            return
        if msg_type == "scroll":
            await browser.scroll(
                payload.get("dx", 0),
                payload.get("dy", 0),
                x=payload.get("x"),
                y=payload.get("y"),
                payload=payload,
            )
            return
        if msg_type == "key":
            await browser.key_press(payload)
            return
        if msg_type == "type":
            await browser.insert_text(payload.get("text", ""))
            return
        if msg_type == "ping":
            await send_notice(websocket, "pong")
            return
        await send_notice(websocket, f"unknown command: {msg_type}")

    async def ws_handler(websocket, path=None):
        ws_path = path if path is not None else getattr(websocket, "path", "")
        parsed = urlparse(ws_path)
        if parsed.path != "/ws":
            await websocket.close(code=4404, reason="not found")
            return
        token = parse_qs(parsed.query).get("token", [""])[0]
        if token != args.token:
            await websocket.close(code=4403, reason="forbidden")
            return
        clients.add(websocket)
        last_client_ts["value"] = time.time()
        await send_notice(websocket, "connected")
        latest = browser.latest_frame_message()
        if latest:
            with contextlib.suppress(Exception):
                await websocket.send(json.dumps(latest))
        try:
            async for message in websocket:
                last_client_ts["value"] = time.time()
                try:
                    payload = json.loads(message)
                except Exception:
                    await send_notice(websocket, "invalid json")
                    continue
                try:
                    await asyncio.wait_for(handle_client_message(websocket, payload), timeout=8)
                except asyncio.TimeoutError:
                    await send_notice(websocket, "command timeout")
                except Exception as exc:
                    _plog(f"command failure: {exc}")
                    await send_notice(websocket, f"command failed: {exc}")
        finally:
            clients.discard(websocket)

    async def process_request(path, headers):
        parsed = urlparse(path)
        if parsed.path == "/health":
            body = b"ok\n"
            return HTTPStatus.OK, [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))], body
        if parsed.path in {"/", "/index.html"}:
            body = HTML_PAGE.encode("utf-8")
            return HTTPStatus.OK, [
                ("Content-Type", "text/html; charset=utf-8"),
                ("Cache-Control", "no-store"),
                ("Content-Length", str(len(body))),
            ], body
        if parsed.path == "/ws":
            return None
        if parsed.path == "/screenshot":
            qs = parse_qs(parsed.query)
            t = qs.get("token", [""])[0]
            if t != args.token:
                body = b"forbidden"
                return HTTPStatus.FORBIDDEN, [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))], body
            frame = browser.latest_frame_message()
            if not frame:
                body = b"no frame"
                return HTTPStatus.SERVICE_UNAVAILABLE, [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))], body
            img_str = frame.get("image", "")
            if "," in img_str:
                img_str = img_str.split(",", 1)[1]
            import base64 as _b64
            img_bytes = _b64.b64decode(img_str)
            last_client_ts["value"] = time.time()
            return HTTPStatus.OK, [
                ("Content-Type", "image/jpeg"),
                ("Content-Length", str(len(img_bytes))),
                ("Cache-Control", "no-store"),
            ], img_bytes
        body = b"not found\n"
        return HTTPStatus.NOT_FOUND, [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))], body

    stable_counter = 0
    stable_url = ""
    restart_failures = 0

    try:
        async with websockets.serve(
            ws_handler,
            args.host,
            args.port,
            process_request=process_request,
            max_size=2 * 1024 * 1024,
            compression=None,
        ):
            portal_url = f"http://{args.host}:{args.port}/?token={args.token}"
            write_status(
                status_path,
                {
                    "ready": True,
                    "url": portal_url,
                    "service": args.service,
                    "pid": os.getpid(),
                    "started": int(time.time()),
                    "initial_url": initial_url,
                },
            )
            ready_written = True

            while not stop_event.is_set():
                now = time.time()
                if external_stop.is_set():
                    stop_reason["value"] = "signal"
                    break
                if now - started > args.max_seconds:
                    stop_reason["value"] = "timeout"
                    break
                if not clients and now - last_client_ts["value"] > 600:
                    stop_reason["value"] = "idle"
                    break

                if browser.unhealthy():
                    restart_failures += 1
                    await broadcast({"type": "notice", "message": "browser disconnected, reconnecting..."})
                    try:
                        await browser.restart()
                        restart_failures = 0
                        stable_counter = 0
                        stable_url = ""
                        await broadcast({"type": "notice", "message": "browser reconnected"})
                    except Exception as exc:
                        _plog(f"browser restart failed: {exc}")
                        await broadcast({"type": "notice", "message": f"browser restart failed: {exc}"})
                        if restart_failures >= 5:
                            stop_reason["value"] = "browser_failed"
                            break
                        await asyncio.sleep(min(6, restart_failures))
                        continue

                try:
                    frame_msg = await browser.next_frame_message(timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                except Exception as exc:
                    _plog(f"frame read failure: {exc}")
                    await asyncio.sleep(0.2)
                    continue

                await broadcast(frame_msg)

                current_url = str(frame_msg.get("url") or "")
                host_ok = url_host_allowed(current_url, allow_hosts) if allow_hosts else False
                if host_ok and current_url != args.start_url and not looks_like_login_url(current_url):
                    if current_url == stable_url:
                        stable_counter += 1
                    else:
                        stable_url = current_url
                        stable_counter = 1
                else:
                    stable_counter = 0
                    stable_url = ""
                if stable_counter >= 4:
                    stop_reason["value"] = "auto_complete"
                    break
    except Exception as exc:
        if not ready_written:
            write_status(status_path, {"ready": False, "error": str(exc)})
        raise
    finally:
        await browser.close()
        if ready_written:
            write_status(
                status_path,
                {
                    "ready": False,
                    "closed": True,
                    "reason": stop_reason["value"],
                    "service": args.service,
                    "stopped": int(time.time()),
                },
            )


def parse_args():
    parser = argparse.ArgumentParser(description="Void human login portal")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--profile-dir", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--start-url", required=True)
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--node-script", required=True)
    parser.add_argument("--allow-host", action="append", default=[])
    parser.add_argument("--max-seconds", type=int, default=1800)
    return parser.parse_args()


def main():
    args = parse_args()
    if args.max_seconds < 60:
        args.max_seconds = 60

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    stop = asyncio.Event()

    def _on_signal(*_):
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass

    async def runner():
        task = asyncio.create_task(run(args, stop))
        done, pending = await asyncio.wait(
            {task, asyncio.create_task(stop.wait())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task not in done:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for p in pending:
            p.cancel()

    try:
        loop.run_until_complete(runner())
    finally:
        loop.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"void-login-portal failed: {e}", flush=True)
        raise
LOGIN_PORTAL_PY
  chmod 0755 "$target"
}

write_firewall_script() {
  local target="$1"
  cat >"$target" <<'FW_SCRIPT'
#!/bin/bash
set -euo pipefail

# Debian bookworm defaults to nftables backend. If the kernel lacks
# nf_tables support (e.g., libkrun microVM), fall back to iptables-legacy.
IPT=""
for candidate in iptables iptables-legacy; do
  if command -v "$candidate" >/dev/null 2>&1 && $candidate -L -n >/dev/null 2>&1; then
    IPT="$candidate"
    break
  fi
done

IP6T=""
for candidate in ip6tables ip6tables-legacy; do
  if command -v "$candidate" >/dev/null 2>&1 && $candidate -L -n >/dev/null 2>&1; then
    IP6T="$candidate"
    break
  fi
done

if [[ -z "$IPT" ]]; then
  echo "WARNING: No usable iptables binary found. Skipping IPv4 firewall (kernel may lack netfilter)." >&2
  exit 0
fi

if [[ -z "$IP6T" ]]; then
  echo "WARNING: No usable ip6tables binary found. Skipping IPv6 firewall." >&2
  IP6T="$IPT"  # fall through, best-effort
fi

$IPT -F
$IPT -X
$IPT -P INPUT DROP
$IPT -P FORWARD DROP
$IPT -P OUTPUT DROP

$IPT -A INPUT -i lo -j ACCEPT
$IPT -A OUTPUT -o lo -j ACCEPT
$IPT -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
$IPT -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# Block internal/metadata before allowing internet egress.
$IPT -A OUTPUT -d 169.254.169.254 -j DROP
$IPT -A OUTPUT -d 10.0.0.0/8 -j DROP
$IPT -A OUTPUT -d 172.16.0.0/12 -j DROP
$IPT -A OUTPUT -d 192.168.0.0/16 -j DROP
$IPT -A OUTPUT -d 100.64.0.0/10 -j DROP

# Browser egress policy: UID-scoped whitelist-only HTTPS + DNS to resolvers.
# IMPORTANT: DNS rules MUST come before private IP blocks, because the VM's
# DNS resolver may be on a private IP (e.g., libkrun gateway at 10.0.2.3).
BROWSER_UID="$(id -u void 2>/dev/null || true)"
if [[ -n "$BROWSER_UID" ]]; then
  $IPT -N VOID_BROWSER_EGRESS 2>/dev/null || true
  $IPT -F VOID_BROWSER_EGRESS
  $IPT -A OUTPUT -m owner --uid-owner "$BROWSER_UID" -j VOID_BROWSER_EGRESS
  $IPT -A VOID_BROWSER_EGRESS -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

  # 1. Allow DNS to resolvers FIRST (before private IP blocks)
  while read -r ns; do
    [[ -z "$ns" ]] && continue
    [[ "$ns" == *:* ]] && continue
    $IPT -A VOID_BROWSER_EGRESS -p udp -d "$ns" --dport 53 -j ACCEPT
    $IPT -A VOID_BROWSER_EGRESS -p tcp -d "$ns" --dport 53 -j ACCEPT
  done < <(awk '/^nameserver/{print $2}' /etc/resolv.conf)

  # 2. Block private/reserved ranges (after DNS is allowed)
  $IPT -A VOID_BROWSER_EGRESS -d 169.254.169.254/32 -j REJECT
  for cidr in \
    0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 \
    169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 \
    224.0.0.0/4 240.0.0.0/4; do
    $IPT -A VOID_BROWSER_EGRESS -d "$cidr" -j REJECT
  done

  # 3. Allow HTTPS egress, reject everything else
  $IPT -A VOID_BROWSER_EGRESS -p tcp --dport 443 -j ACCEPT
  $IPT -A VOID_BROWSER_EGRESS -j REJECT
fi

$IPT -A OUTPUT -p udp --dport 53 -j ACCEPT
$IPT -A OUTPUT -p tcp --dport 53 -j ACCEPT
$IPT -A OUTPUT -p tcp --dport 443 -j ACCEPT
$IPT -A OUTPUT -p tcp --dport 80 -j ACCEPT

$IP6T -F
$IP6T -X
$IP6T -P INPUT DROP
$IP6T -P FORWARD DROP
$IP6T -P OUTPUT DROP

$IP6T -A INPUT -i lo -j ACCEPT
$IP6T -A OUTPUT -o lo -j ACCEPT
$IP6T -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
$IP6T -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# Block local and private IPv6 ranges before allowing egress.
$IP6T -A OUTPUT -d ::1/128 -j DROP
$IP6T -A OUTPUT -d ::/128 -j DROP
$IP6T -A OUTPUT -d fc00::/7 -j DROP
$IP6T -A OUTPUT -d fe80::/10 -j DROP
$IP6T -A OUTPUT -d ff00::/8 -j DROP

if [[ -n "$BROWSER_UID" ]]; then
  $IP6T -N VOID_BROWSER_EGRESS6 2>/dev/null || true
  $IP6T -F VOID_BROWSER_EGRESS6
  $IP6T -A OUTPUT -m owner --uid-owner "$BROWSER_UID" -j VOID_BROWSER_EGRESS6
  $IP6T -A VOID_BROWSER_EGRESS6 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

  # Allow DNS to configured IPv6 resolvers first.
  while read -r ns; do
    [[ -z "$ns" ]] && continue
    [[ "$ns" != *:* ]] && continue
    $IP6T -A VOID_BROWSER_EGRESS6 -p udp -d "$ns" --dport 53 -j ACCEPT
    $IP6T -A VOID_BROWSER_EGRESS6 -p tcp -d "$ns" --dport 53 -j ACCEPT
  done < <(awk '/^nameserver/{print $2}' /etc/resolv.conf)

  # Block private/link-local/multicast before allowing HTTPS.
  for cidr6 in ::/128 ::1/128 fc00::/7 fe80::/10 ff00::/8; do
    $IP6T -A VOID_BROWSER_EGRESS6 -d "$cidr6" -j REJECT
  done

  $IP6T -A VOID_BROWSER_EGRESS6 -p tcp --dport 443 -j ACCEPT
  $IP6T -A VOID_BROWSER_EGRESS6 -j REJECT
fi

$IP6T -A OUTPUT -p udp --dport 53 -j ACCEPT
$IP6T -A OUTPUT -p tcp --dport 53 -j ACCEPT
$IP6T -A OUTPUT -p tcp --dport 443 -j ACCEPT
$IP6T -A OUTPUT -p tcp --dport 80 -j ACCEPT
FW_SCRIPT
  chmod 0755 "$target"
}

write_guest_provision_script() {
  local target="$1"
  cat >"$target" <<'PROVISION_SH'
#!/bin/bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends \
  cryptsetup iptables ca-certificates curl gnupg \
  python3 python3-yaml python3-websockets sudo jq

install -d -m 0755 /usr/local/bin
install -d -m 0750 /etc/credential-proxy

install -m 0755 /provision/command-proxy-daemon.py /usr/local/bin/command-proxy-daemon
install -m 0755 /provision/void-login-portal.py /usr/local/bin/void-login-portal.py
install -m 0755 /provision/void-login-browser.js /usr/local/bin/void-login-browser.js
install -m 0640 /provision/config.yaml /etc/credential-proxy/config.yaml
install -m 0755 /provision/firewall.sh /root/firewall.sh

# Compile and install omi binary (checksum-verified tier)
if [ -f /provision/omi.go ]; then
  apt-get install -y --no-install-recommends golang-go
  cd /tmp && CGO_ENABLED=0 go build -o /usr/local/bin/omi /provision/omi.go
  chmod 0755 /usr/local/bin/omi
fi

# Install gh CLI
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
  | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
  > /etc/apt/sources.list.d/github-cli.list
apt-get update
apt-get install -y gh

# Install gcloud CLI
curl -fsSL https://sdk.cloud.google.com -o /tmp/gcloud-install.sh
bash /tmp/gcloud-install.sh --disable-prompts --install-dir=/usr/local
ln -sf /usr/local/google-cloud-sdk/bin/gcloud /usr/local/bin/gcloud
rm -f /tmp/gcloud-install.sh

# Install Node.js 22 LTS (NodeSource)
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
  | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg
echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
  > /etc/apt/sources.list.d/nodesource.list
apt-get update
apt-get install -y --no-install-recommends nodejs

# Browser user isolation (create BEFORE installing Chromium)
if ! id -u void >/dev/null 2>&1; then
  useradd --system --create-home \
    --home-dir /var/lib/void --shell /usr/sbin/nologin void
fi
install -d -o void -g void -m 0700 \
  /var/lib/void/.cache \
  /var/lib/void/.config \
  /var/lib/void/.local/share \
  /var/lib/void/screenshots

# Install browser agent and Chromium dependencies
npm install -g --omit=dev --ignore-scripts agent-browser@0.10.0 || true
# Fix binary permissions (krunvm filesystem may block chmod during npm install)
chmod +x /usr/lib/node_modules/agent-browser/bin/agent-browser-* 2>/dev/null || true
ln -sf /usr/lib/node_modules/agent-browser/bin/agent-browser-linux-x64 /usr/local/bin/agent-browser 2>/dev/null || true
# Step 1: Install system deps (fonts, libs) as root
npx --yes playwright install-deps chromium 2>/dev/null || true
# Step 2: Download Chromium AS void user (needs HOME + cwd set properly)
su -s /bin/sh -c 'cd /var/lib/void && HOME=/var/lib/void npx --yes playwright install chromium' void

swapoff -a || true
sed -i '/[[:space:]]swap[[:space:]]/d' /etc/fstab

/root/firewall.sh
PROVISION_SH
  chmod 0755 "$target"
}

prepare_provision_bundle() {
  local bundle_dir
  local mktemp_base="${RUN_ROOT}"
  [[ "$DRY_RUN" -eq 1 ]] && mktemp_base="/tmp"
  bundle_dir="$(mktemp -d "${mktemp_base}/provision.${VM_NAME}.XXXXXX")"
  track_tmp_path "$bundle_dir"

  write_proxy_daemon "${bundle_dir}/command-proxy-daemon.py"
  write_login_portal_script "${bundle_dir}/void-login-portal.py"
  write_login_browser_bridge_script "${bundle_dir}/void-login-browser.js"
  write_proxy_config "${bundle_dir}/config.yaml"
  write_firewall_script "${bundle_dir}/firewall.sh"
  write_guest_provision_script "${bundle_dir}/provision.sh"

  # Bundle omi source (single Go file, compiled during provision)
  local omi_src
  omi_src="$(dirname "$SCRIPT_PATH")/omi.go"
  if [[ -f "$omi_src" ]]; then
    cp "$omi_src" "${bundle_dir}/omi.go"
  fi

  # POSIX bootstrap: debian:bookworm-slim only has /bin/sh (dash).
  # Install bash first, then hand off to the real provision script.
  cat >"${bundle_dir}/bootstrap.sh" <<'BOOTSTRAP'
#!/bin/sh
set -eu
apt-get update
apt-get install -y bash
exec /bin/bash /provision/provision.sh
BOOTSTRAP
  chmod 0755 "${bundle_dir}/bootstrap.sh"

  printf "%s" "$bundle_dir"
}

write_host_proxy_script() {
  local target="$1"
  local runtime_env="$2"
  cat >"$target" <<PROXY_SCRIPT
#!/bin/bash
set -euo pipefail

# void — Host-side client for the VM command proxy daemon.
# Usage: void <gh|gcloud|browse|session> [args...]

RUNTIME_ENV="${runtime_env}"

usage() {
  echo "Usage: void <gh|gcloud|browse|session|omi> [args...]" >&2
  echo "" >&2
  echo "Tier 1 — CLI passthrough (raw gh/gcloud):" >&2
  echo "  void gh pr list" >&2
  echo "  void gcloud projects list" >&2
  echo "" >&2
  echo "Tier 2 — Checksum-verified scripts (omi infrastructure):" >&2
  echo "  void omi bucket-versioning-set [--dry-run] [--project ID]" >&2
  echo "  Omi scripts are verified against SHA256 checksums stored in Bitwarden." >&2
  echo "" >&2
  echo "Session commands:" >&2
  echo "  void session login <service> [--port N]" >&2
  echo "  void session list" >&2
  echo "  void session close <service>" >&2
  echo "" >&2
  echo "Credentials never leave the VM boundary." >&2
  exit 2
}

if [[ \$# -lt 1 ]]; then
  usage
fi

tool="\$1"
case "\$tool" in
  gh|gcloud|browse|session|omi) ;;
  -h|--help) usage ;;
  *) echo "Error: only 'gh', 'gcloud', 'browse', 'session', and 'omi' are allowed (got: \$tool)" >&2; exit 1 ;;
esac

# Read IPC directory from runtime state
if [[ ! -f "\$RUNTIME_ENV" ]]; then
  echo "Error: VM is not running (no runtime state at \$RUNTIME_ENV)" >&2
  exit 1
fi

IPC_DIR="\$(grep -E '^IPC_DIR=' "\$RUNTIME_ENV" | head -n1 | cut -d'=' -f2-)"
if [[ -z "\$IPC_DIR" || ! -d "\$IPC_DIR" ]]; then
  echo "Error: IPC directory not found. Is the VM running?" >&2
  exit 1
fi

IPC_KEY="\$(grep -E '^IPC_KEY=' "\$RUNTIME_ENV" | head -n1 | cut -d'=' -f2-)"
if [[ -z "\$IPC_KEY" ]]; then
  echo "Error: IPC key missing from runtime state. Restart VM." >&2
  exit 1
fi

maybe_start_local_port_forward() {
  local port="\$1"
  if ! [[ "\$port" =~ ^[0-9]+$ ]]; then
    return 0
  fi
  if ! command -v socat >/dev/null 2>&1; then
    return 0
  fi
  local vm_name
  vm_name="\$(grep -E '^VM_NAME=' "\$RUNTIME_ENV" | head -n1 | cut -d'=' -f2-)"
  [[ -n "\$vm_name" ]] || return 0

  local krunvm_bin=""
  krunvm_bin="\$(command -v krunvm 2>/dev/null || true)"
  if [[ -z "\$krunvm_bin" && -x /root/.cargo/bin/krunvm ]]; then
    krunvm_bin="/root/.cargo/bin/krunvm"
  fi
  [[ -n "\$krunvm_bin" ]] || return 0

  local guest_ip=""
  guest_ip="\$("\$krunvm_bin" ip "\$vm_name" 2>/dev/null | awk 'NF{print \$1; exit}' || true)"
  [[ -n "\$guest_ip" ]] || return 0

  if bash -lc "</dev/tcp/127.0.0.1/\${port}" >/dev/null 2>&1; then
    return 0
  fi

  local pid_file="/tmp/void-session-forward-\${vm_name}-\${port}.pid"
  if [[ -f "\$pid_file" ]]; then
    local old_pid
    old_pid="\$(cat "\$pid_file" 2>/dev/null || true)"
    if [[ -n "\$old_pid" ]] && kill -0 "\$old_pid" >/dev/null 2>&1; then
      return 0
    fi
    rm -f "\$pid_file"
  fi

  nohup socat "TCP-LISTEN:\${port},bind=127.0.0.1,reuseaddr,fork" "TCP:\${guest_ip}:\${port}" \
    >/tmp/void-session-forward-\${vm_name}-\${port}.log 2>&1 &
  echo "\$!" > "\$pid_file"
}

# Generate unique request ID
req_id="req-\$(date +%s)-\$\$-\$RANDOM"

if [[ "\$tool" == "browse" ]]; then
  shift
  session="default"
  browser_args=()
  while [[ \$# -gt 0 ]]; do
    case "\$1" in
      --session)
        if [[ \$# -lt 2 ]]; then
          echo "Error: --session requires a value" >&2
          exit 2
        fi
        session="\$2"
        shift 2
        ;;
      --session=*)
        session="\${1#*=}"
        shift
        ;;
      *)
        browser_args+=("\$1")
        shift
        ;;
    esac
  done

  if [[ -z "\$session" ]]; then
    echo "Error: --session cannot be empty" >&2
    exit 2
  fi

  request=\$(python3 - "\$req_id" "\$session" "\${browser_args[@]}" <<'PY_BROWSER_REQUEST'
import json
import sys
import time


def fail(msg):
    print(f"Error: {msg}", file=sys.stderr)
    raise SystemExit(2)


req_id = sys.argv[1]
session = sys.argv[2]
args = sys.argv[3:]
if not args:
    fail("browse requires a subcommand")

one_word = {
    "open", "back", "forward", "reload", "snapshot", "screenshot",
    "click", "dblclick", "fill", "type", "press", "hover", "scroll",
    "select", "check", "uncheck", "upload", "drag", "wait",
}
two_word = {
    "get": {"text", "title", "url", "value", "attr", "count", "box"},
    "is": {"visible", "enabled", "checked"},
    "find": {"role", "label", "text", "placeholder"},
    "tab": {"list", "new", "close", "switch"},
    "session": {"list", "close"},
    "set": {"viewport", "device", "media"},
}

cmd = args[0]
tail = args[1:]
action = ""
params = {}

if cmd == "open":
    if len(tail) != 1:
        fail("usage: void browse open <https-url>")
    action = "open"
    params = {"url": tail[0], "args": [tail[0]]}
elif cmd in one_word:
    action = cmd
    params = {"args": tail}
elif cmd in two_word:
    if not tail:
        fail(f"usage: void browse {cmd} <subcommand> [...]")
    sub = tail[0]
    if sub not in two_word[cmd]:
        fail(f"unsupported browse action: {cmd} {sub}")
    action = f"{cmd} {sub}"
    params = {"args": tail[1:]}
    if cmd == "session" and sub == "close" and tail[1:]:
        params["name"] = tail[1]
else:
    fail(f"unsupported browse action: {cmd}")

timeout = 25 if action in {"open", "snapshot", "screenshot", "wait"} else 10
request = {
    "id": req_id,
    "tool": "browser",
    "session": session,
    "action": action,
    "params": params,
    "timeout": timeout,
    "timestamp": int(time.time()),
}
print(json.dumps(request))
PY_BROWSER_REQUEST
) || exit \$?
  if [[ -z "\$request" ]]; then
    echo "Error: failed to build browser request" >&2
    exit 2
  fi
elif [[ "\$tool" == "session" ]]; then
  shift
  if [[ \$# -lt 1 ]]; then
    echo "Error: session requires a subcommand (login|list|close)" >&2
    exit 2
  fi
  session_subcmd="\$1"
  shift
  case "\$session_subcmd" in
    login)
      if [[ \$# -lt 1 ]]; then
        echo "Error: usage: void session login <service> [--port N]" >&2
        exit 2
      fi
      service="\$1"
      shift
      port="\${VOID_SESSION_PORT:-9222}"
      while [[ \$# -gt 0 ]]; do
        case "\$1" in
          --port)
            [[ \$# -ge 2 ]] || { echo "Error: --port requires a value" >&2; exit 2; }
            port="\$2"
            shift 2
            ;;
          --port=*)
            port="\${1#*=}"
            shift
            ;;
          *)
            echo "Error: unsupported argument for session login: \$1" >&2
            exit 2
            ;;
        esac
      done
      maybe_start_local_port_forward "\$port"
      request=\$(python3 - "\$req_id" "\$service" "\$port" <<'PY_SESSION_LOGIN'
import json
import re
import sys
import time

req_id = sys.argv[1]
service = sys.argv[2].strip()
port_raw = sys.argv[3].strip()

if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", service):
    print("Error: invalid service name", file=sys.stderr)
    raise SystemExit(2)

port = None
if port_raw:
    try:
        p = int(port_raw)
    except ValueError:
        print("Error: --port must be an integer", file=sys.stderr)
        raise SystemExit(2)
    if p < 1 or p > 65535:
        print("Error: --port must be in 1..65535", file=sys.stderr)
        raise SystemExit(2)
    port = p

params = {"args": []}
if port is not None:
    params["port"] = port

request = {
    "id": req_id,
    "tool": "browser",
    "session": service,
    "action": "session login",
    "params": params,
    "timeout": 30,
    "timestamp": int(time.time()),
}
print(json.dumps(request))
PY_SESSION_LOGIN
) || exit \$?
      ;;
    list)
      if [[ \$# -ne 0 ]]; then
        echo "Error: usage: void session list" >&2
        exit 2
      fi
      request=\$(python3 - "\$req_id" <<'PY_SESSION_LIST'
import json
import sys
import time

req_id = sys.argv[1]
request = {
    "id": req_id,
    "tool": "browser",
    "session": "default",
    "action": "session list",
    "params": {"args": []},
    "timeout": 10,
    "timestamp": int(time.time()),
}
print(json.dumps(request))
PY_SESSION_LIST
) || exit \$?
      ;;
    close)
      if [[ \$# -ne 1 ]]; then
        echo "Error: usage: void session close <service>" >&2
        exit 2
      fi
      service="\$1"
      request=\$(python3 - "\$req_id" "\$service" <<'PY_SESSION_CLOSE'
import json
import re
import sys
import time

req_id = sys.argv[1]
service = sys.argv[2].strip()
if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", service):
    print("Error: invalid service name", file=sys.stderr)
    raise SystemExit(2)

request = {
    "id": req_id,
    "tool": "browser",
    "session": service,
    "action": "session close",
    "params": {"args": [service], "name": service},
    "timeout": 20,
    "timestamp": int(time.time()),
}
print(json.dumps(request))
PY_SESSION_CLOSE
) || exit \$?
      ;;
    *)
      echo "Error: unsupported session subcommand: \$session_subcmd" >&2
      exit 2
      ;;
  esac
else
  # Build JSON request for gh/gcloud/omi passthrough mode.
  argv_json=\$(printf '%s\n' "\$@" | python3 -c '
import json, sys
print(json.dumps([line.rstrip("\n") for line in sys.stdin]))
')

  if [[ "\$tool" == "omi" ]]; then
    timeout=300
  else
    timeout=60
  fi
  request=\$(python3 -c "
import json, time
print(json.dumps({
    'id': '\${req_id}',
    'argv': \${argv_json},
    'timeout': \${timeout},
    'timestamp': int(time.time())
}))
")
fi

if [[ -z "\$request" ]]; then
  echo "Error: empty request — cannot proceed" >&2
  exit 2
fi

request=\$(python3 -c "
import hashlib, hmac, json, sys
key_hex = sys.argv[1].strip()
try:
    key = bytes.fromhex(key_hex)
except ValueError:
    print('Error: invalid IPC key in runtime state', file=sys.stderr)
    raise SystemExit(1)
req = json.loads(sys.argv[2])
payload = dict(req)
payload.pop('hmac', None)
canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
req['hmac'] = hmac.new(key, canonical.encode('utf-8'), hashlib.sha256).hexdigest()
print(json.dumps(req))
" "\$IPC_KEY" "\$request")

# Atomic write: .tmp then rename to .json
echo "\$request" > "\${IPC_DIR}/requests/\${req_id}.tmp"
mv "\${IPC_DIR}/requests/\${req_id}.tmp" "\${IPC_DIR}/requests/\${req_id}.json"

# Poll for response (timeout 360s to allow for virtiofs import latency + portal cold start)
poll_timeout=360
elapsed=0
resp_file="\${IPC_DIR}/responses/\${req_id}.json"

while [[ ! -f "\$resp_file" ]]; do
  sleep 0.2
  elapsed=\$((elapsed + 1))
  if [[ \$elapsed -ge \$((poll_timeout * 5)) ]]; then
    echo "Error: timed out waiting for VM response after \${poll_timeout}s" >&2
    rm -f "\${IPC_DIR}/requests/\${req_id}.json" 2>/dev/null || true
    exit 124
  fi
done

# Parse response: verify auth, print stdout/stderr, exit with the command's exit code
python3 - "\${resp_file}" "\${IPC_KEY}" <<'PY_VERIFY_RESPONSE'
import hashlib
import hmac
import json
import sys

resp_file = sys.argv[1]
key_hex = sys.argv[2].strip()

try:
    key = bytes.fromhex(key_hex)
except ValueError:
    print("Error: invalid IPC key in runtime state", file=sys.stderr)
    raise SystemExit(1)

with open(resp_file, "r", encoding="utf-8") as f:
    resp = json.load(f)

provided = resp.get("hmac")
if not isinstance(provided, str):
    print("Error: missing response authentication", file=sys.stderr)
    raise SystemExit(1)

payload = dict(resp)
payload.pop("hmac", None)
canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
expected = hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()

if not hmac.compare_digest(provided.lower(), expected.lower()):
    print("Error: invalid response authentication", file=sys.stderr)
    raise SystemExit(1)

if resp.get("stdout"):
    sys.stdout.write(resp["stdout"])
if resp.get("stderr"):
    sys.stderr.write(resp["stderr"])
sys.exit(resp.get("exit_code", 1))
PY_VERIFY_RESPONSE
proxy_exit=\$?

# Clean up
rm -f "\${resp_file}" 2>/dev/null || true

exit \$proxy_exit
PROXY_SCRIPT
  chmod 0755 "$target"
}

read_runtime_value() {
  local key="$1"
  if [[ ! -f "$RUNTIME_ENV_FILE" ]]; then
    return 1
  fi
  grep -E "^${key}=" "$RUNTIME_ENV_FILE" | head -n1 | cut -d'=' -f2-
}

write_runtime_state() {
  local secrets_dir="$1"
  local luks_mount="$2"
  local mapper="$3"
  local log_file="$4"
  local ipc_dir="$5"
  local ipc_key="$6"
  if ((DRY_RUN)); then
    printf "[dry-run] write runtime state to %s\n" "$RUNTIME_ENV_FILE" >&2
    return
  fi
  cat >"$RUNTIME_ENV_FILE" <<EOF_RUNTIME
VM_NAME=${VM_NAME}
SECRETS_DIR=${secrets_dir}
LUKS_MOUNT=${luks_mount}
LUKS_MAPPER=${mapper}
VM_LOG=${log_file}
IPC_DIR=${ipc_dir}
IPC_KEY=${ipc_key}
EOF_RUNTIME
  chmod 0640 "$RUNTIME_ENV_FILE"
}

wait_for_port() {
  local host="$1"
  local port="$2"
  local timeout_sec="$3"
  local i
  for ((i=0; i<timeout_sec; i++)); do
    if bash -lc "</dev/tcp/${host}/${port}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

fetch_bitwarden_env() {
  local project_id="$1"
  local out_file="$2"

  require_commands jq

  if [[ "$DRY_RUN" -eq 0 ]] && ! command_exists bws; then
    cat >&2 <<'BWS_HELP'

  'bws' command not found. Install it first:

    bash setup-secure-vm.sh deps        # installs bws + all other deps

  Or install manually:

    curl -fsSL -o /tmp/bws.zip \
      https://github.com/bitwarden/sdk-sm/releases/download/bws-v1.0.0/bws-x86_64-unknown-linux-gnu-1.0.0.zip
    unzip /tmp/bws.zip -d /usr/local/bin/ && chmod +x /usr/local/bin/bws

  Docs: https://bitwarden.com/help/secrets-manager-cli/

BWS_HELP
    exit 1
  fi

  if ((DRY_RUN)); then
    printf "[dry-run] bws secret list %q -o json | jq ... > %q\n" "$project_id" "$out_file" >&2
    return
  fi

  if [[ -z "${BWS_ACCESS_TOKEN:-}" ]]; then
    cat >&2 <<'TOKEN_HELP'

  BWS_ACCESS_TOKEN is required. Generate one in Bitwarden:

    1. Go to https://vault.bitwarden.com -> Secrets Manager
    2. Go to Machine Accounts -> create or select one
    3. Grant it access to your project
    4. Generate an Access Token
    5. Run:

       BWS_ACCESS_TOKEN=<token> BWS_PROJECT_ID=<project-id> bash setup-secure-vm.sh --yes start

TOKEN_HELP
    exit 1
  fi

  # Write secrets: JSON values go to separate files, everything else to KEY=VALUE env file.
  local secrets_dir
  secrets_dir="$(dirname "$out_file")"

  BWS_ACCESS_TOKEN="$BWS_ACCESS_TOKEN" bws secret list "$project_id" -o json \
    | python3 -c "
import json, sys, os
secrets = json.load(sys.stdin)
out_file = '${out_file}'
secrets_dir = '${secrets_dir}'
with open(out_file, 'w') as ef:
    for s in secrets:
        key = s.get('key', '')
        val = s.get('value', '')
        if not key or val is None:
            continue
        stripped = val.strip()
        if stripped.startswith('{') and stripped.endswith('}'):
            # JSON value — write to separate file, reference path in env
            json_file = os.path.join(secrets_dir, key.lower() + '.json')
            with open(json_file, 'w') as jf:
                jf.write(val)
            os.chmod(json_file, 0o600)
            ef.write(f'{key}={json_file}\n')
        else:
            # Simple value — single line
            ef.write(f'{key}={val}\n')
"
}

cmd_deps() {
  ensure_state_dirs

  log_info "Installing host dependencies..."
  install_apt_packages \
    build-essential clang lld patchelf python3 python3-pyelftools \
    libssl-dev libcap-ng-dev pkg-config python3-pip python3-venv \
    git curl jq make ca-certificates gnupg lsb-release \
    cryptsetup iptables \
    flex bison bc libelf-dev libclang-dev asciidoctor buildah

  install_rust
  install_nodesource_nodejs

  if python3 -c "import yaml" 2>/dev/null; then
    log_info "Python yaml module already installed."
  else
    log_info "Installing Python yaml module..."
    run python3 -m pip install --break-system-packages pyyaml
  fi

  install_gcloud_cli
  install_gh_cli
  install_bitwarden_cli

  log_info "Dependencies complete."
}

cmd_build() {
  ensure_state_dirs
  require_commands git make ldconfig

  export PATH="${HOME}/.cargo/bin:/root/.cargo/bin:${PATH}"

  local libkrunfw_dir="${BUILD_ROOT}/libkrunfw"
  local libkrun_dir="${BUILD_ROOT}/libkrun"

  git_sync_repo "https://github.com/containers/libkrunfw" "$libkrunfw_dir"

  # Fix upstream Makefile bug: $(MAKE) $(MAKEFLAGS) passes 'w' (--print-directory)
  # as a target name instead of a flag, causing "No rule to make target 'w'".
  # Remove explicit $(MAKEFLAGS) — Make propagates flags via environment automatically.
  if ((DRY_RUN)); then
    printf "[dry-run] patch libkrunfw Makefile: remove explicit \$(MAKEFLAGS)\n" >&2
  else
    sed -i 's/\$(MAKE) \$(MAKEFLAGS)/\$(MAKE)/g' "${libkrunfw_dir}/Makefile"
  fi

  log_info "Building libkrunfw..."
  if ((DRY_RUN)); then
    run make -C "$libkrunfw_dir" -j"$(nproc)"
  else
    # Close stdin so kernel olddefconfig auto-accepts all NEW options silently.
    make -C "$libkrunfw_dir" -j"$(nproc)" < /dev/null
  fi
  run sudo make -C "$libkrunfw_dir" install

  git_sync_repo "https://github.com/containers/libkrun" "$libkrun_dir"
  log_info "Building libkrun (BLK=1 NET=1)..."
  run make -C "$libkrun_dir" BLK=1 NET=1 -j"$(nproc)"
  run sudo make -C "$libkrun_dir" BLK=1 NET=1 install

  if ((DRY_RUN)); then
    printf "[dry-run] write /etc/ld.so.conf.d/libkrun.conf and run ldconfig\n" >&2
  else
    sudo sh -c 'echo "/usr/local/lib64" > /etc/ld.so.conf.d/libkrun.conf'
  fi
  run sudo ldconfig

  if [[ "$DRY_RUN" -eq 0 ]]; then
    if ! command_exists cargo && [[ ! -x /root/.cargo/bin/cargo ]]; then
      die "cargo not found; run deps first."
    fi
  fi

  local cargo_bin
  cargo_bin="$(command -v cargo 2>/dev/null || echo /root/.cargo/bin/cargo)"

  log_info "Installing krunvm via cargo..."
  export LIBRARY_PATH="/usr/local/lib64:${LIBRARY_PATH:-}"
  export LD_LIBRARY_PATH="/usr/local/lib64:${LD_LIBRARY_PATH:-}"
  run "$cargo_bin" install krunvm --locked

  log_info "Build complete."
}

cmd_create() {
  need_root
  ensure_state_dirs
  require_commands cryptsetup dd mkfs.ext4

  local krunvm_bin
  krunvm_bin="$(command -v krunvm 2>/dev/null || true)"
  if [[ -z "$krunvm_bin" && -x /root/.cargo/bin/krunvm ]]; then
    krunvm_bin="/root/.cargo/bin/krunvm"
  fi
  if [[ -z "$krunvm_bin" && "$DRY_RUN" -eq 0 ]]; then
    die "krunvm not found; run build first."
  fi
  [[ -n "$krunvm_bin" ]] || krunvm_bin="krunvm"

  if [[ ! -f "$LUKS_IMAGE" ]]; then
    log_info "Creating LUKS image at ${LUKS_IMAGE} (${SECRETS_SIZE} MB)..."
    run dd if=/dev/zero of="$LUKS_IMAGE" bs=1M count="$SECRETS_SIZE" status=progress
    local luks_keyfile="${VM_STATE_DIR}/luks.key"
    if ((ASSUME_YES)) || [[ ! -t 0 ]]; then
      log_info "Generating random LUKS key (non-interactive mode)..."
      dd if=/dev/urandom of="$luks_keyfile" bs=512 count=1 2>/dev/null
      chmod 0400 "$luks_keyfile"
      run cryptsetup luksFormat --batch-mode "$LUKS_IMAGE" "$luks_keyfile"
      run cryptsetup open --key-file "$luks_keyfile" "$LUKS_IMAGE" "$LUKS_MAPPER"
    else
      log_warn "You will be prompted for the LUKS passphrase."
      run cryptsetup luksFormat "$LUKS_IMAGE"
      run cryptsetup open "$LUKS_IMAGE" "$LUKS_MAPPER"
    fi
    run mkfs.ext4 -F "/dev/mapper/${LUKS_MAPPER}"
    run cryptsetup close "$LUKS_MAPPER"
  else
    log_info "LUKS image already exists; leaving as-is: ${LUKS_IMAGE}"
  fi

  if vm_exists; then
    log_info "VM '${VM_NAME}' already exists; skipping create."
    return
  fi

  log_info "Creating microVM '${VM_NAME}'..."
  run "$krunvm_bin" create docker.io/debian:bookworm-slim \
    --name "$VM_NAME" \
    --cpus "$VM_CPUS" \
    --mem "$VM_MEM"

  log_info "Create complete."
}

cmd_configure() {
  need_root
  ensure_state_dirs

  local krunvm_bin
  krunvm_bin="$(command -v krunvm 2>/dev/null || true)"
  if [[ -z "$krunvm_bin" && -x /root/.cargo/bin/krunvm ]]; then
    krunvm_bin="/root/.cargo/bin/krunvm"
  fi
  if [[ -z "$krunvm_bin" && "$DRY_RUN" -eq 0 ]]; then
    die "krunvm not found; run build first."
  fi
  [[ -n "$krunvm_bin" ]] || krunvm_bin="krunvm"

  if ! vm_exists; then
    die "VM '${VM_NAME}' does not exist. Run create first."
  fi
  if vm_is_running; then
    die "VM '${VM_NAME}' is running. Stop it before configure."
  fi

  local bundle
  bundle="$(prepare_provision_bundle)"

  log_info "Attaching provisioning bundle to VM..."
  run "$krunvm_bin" changevm "$VM_NAME" --volume "${bundle}:/provision"

  log_info "Running guest provisioning..."
  run "$krunvm_bin" start "$VM_NAME" -- /provision/bootstrap.sh

  # Install Chromium into persistent container layer via buildah.
  # The krunvm provision above runs inside the guest whose overlay is ephemeral,
  # so Chromium installed there is lost on VM restart. buildah run writes to
  # the persistent container layer that survives krunvm stop/start.
  local container_id
  container_id="$(buildah containers -q | head -1)"
  if [[ -n "$container_id" ]]; then
    log_info "Installing Chromium into persistent container layer..."
    run buildah run "$container_id" -- env \
      PLAYWRIGHT_BROWSERS_PATH=/var/lib/void/.cache/ms-playwright \
      npx --yes playwright install chromium
    run buildah run "$container_id" -- chown -R 999:0 /var/lib/void/.cache/ms-playwright
  else
    log_warn "No buildah container found; skipping persistent Chromium install."
  fi

  # Browser cgroup limits are set in the boot script (boot.sh) on each start.
  # No separate configure step needed — cgroups are virtual and reset on reboot.

  log_info "Configure complete."
}

cmd_start() {
  need_root
  ensure_state_dirs

  local krunvm_bin
  krunvm_bin="$(command -v krunvm 2>/dev/null || true)"
  if [[ -z "$krunvm_bin" && -x /root/.cargo/bin/krunvm ]]; then
    krunvm_bin="/root/.cargo/bin/krunvm"
  fi
  if [[ -z "$krunvm_bin" && "$DRY_RUN" -eq 0 ]]; then
    die "krunvm not found; run build first."
  fi
  [[ -n "$krunvm_bin" ]] || krunvm_bin="krunvm"

  if ! vm_exists; then
    die "VM '${VM_NAME}' does not exist. Run create first."
  fi

  if vm_is_running; then
    die "VM '${VM_NAME}' appears to be running already. Use status/stop first."
  fi

  local project_id="${BWS_PROJECT_ID:-}"
  if [[ -z "$project_id" && "$DRY_RUN" -eq 0 && "$NO_SECRETS" -eq 0 ]]; then
    cat >&2 <<'SECRETS_GUIDE'

  BWS_PROJECT_ID is missing. Two options:

  ── Option A: Start WITHOUT secrets (for testing) ──────────────────

    bash setup-secure-vm.sh --yes --no-secrets start

  ── Option B: Start WITH Bitwarden Secrets Manager ─────────────────

    If you already have a project and access token:

      BWS_PROJECT_ID=<project-uuid> \
      BWS_ACCESS_TOKEN=<access-token> \
      bash setup-secure-vm.sh --yes start

    Don't have these yet? One-time setup:

      1. Go to https://vault.bitwarden.com -> Secrets Manager
      2. Projects -> + New -> name it -> copy the Project UUID
      3. Secrets -> + New -> add key/value pairs (e.g. GH_TOKEN=ghp_xxx)
         Assign each secret to your project
      4. Machine Accounts -> + New -> add your project to it
      5. Access Tokens -> + Create -> copy the token (shown only once)
      6. Verify:  bws project list --access-token "<token>"
      7. Run step above with your UUID and token

    Full guide: bash setup-secure-vm.sh --help

  ────────────────────────────────────────────────────────────────────
SECRETS_GUIDE
    exit 1
  fi
  [[ -n "$project_id" ]] || project_id="<BWS_PROJECT_ID>"

  log_info "Disabling host swap..."
  run swapoff -a

  local mktemp_base="${RUN_ROOT}"
  [[ "$DRY_RUN" -eq 1 ]] && mktemp_base="/tmp"

  local secrets_dir=""
  local luks_mount
  luks_mount="$(mktemp -d "${mktemp_base}/luks.${VM_NAME}.XXXXXX")"
  # NOT tracked for EXIT cleanup — must persist while VM is running.
  # Cleaned up by cmd_stop() via runtime.env LUKS_MOUNT.

  if [[ "$DRY_RUN" -eq 1 ]] || ! cryptsetup status "$LUKS_MAPPER" >/dev/null 2>&1; then
    local luks_keyfile="${VM_STATE_DIR}/luks.key"
    if [[ -f "$luks_keyfile" ]]; then
      log_info "Unlocking LUKS image with key file..."
      run cryptsetup open --key-file "$luks_keyfile" "$LUKS_IMAGE" "$LUKS_MAPPER"
    else
      log_warn "Unlocking LUKS image; passphrase required."
      run cryptsetup open "$LUKS_IMAGE" "$LUKS_MAPPER"
    fi
  else
    log_info "LUKS mapper already open: ${LUKS_MAPPER}"
  fi

  run mount "/dev/mapper/${LUKS_MAPPER}" "$luks_mount"

  if ((NO_SECRETS)); then
    log_warn "Skipping Bitwarden secrets injection (--no-secrets)."
    log_warn "VM will boot without secrets. Set them manually inside the VM if needed."
  else
    secrets_dir="$(mktemp -d "${mktemp_base}/vm-secrets.${VM_NAME}.XXXXXX")"
    # NOT tracked for EXIT cleanup — must persist while VM is running.
    # Cleaned up by cmd_stop() via runtime.env SECRETS_DIR.
    run chmod 0700 "$secrets_dir"

    local env_file="${secrets_dir}/env"
    log_info "Fetching Bitwarden secrets into tmpfs-backed runtime directory..."
    fetch_bitwarden_env "$project_id" "$env_file"
    run chmod 0600 "$env_file"
    unset BWS_ACCESS_TOKEN || true
  fi

  # Create shared IPC directory for host↔guest communication.
  # NOT tracked for cleanup — must persist while VM is running.
  # Cleaned up by cmd_stop() via runtime.env IPC_DIR.
  local ipc_dir
  ipc_dir="$(mktemp -d "${mktemp_base}/vm-ipc.${VM_NAME}.XXXXXX")"
  run mkdir -p "${ipc_dir}/requests" "${ipc_dir}/responses"
  run chmod 0770 "$ipc_dir"
  run chmod 0770 "${ipc_dir}/requests" "${ipc_dir}/responses"

  # Write guest boot script to a file (krunvm mangles multi-line -c args).
  local boot_dir
  boot_dir="$(mktemp -d "${mktemp_base}/vm-boot.${VM_NAME}.XXXXXX")"
  track_tmp_path "$boot_dir"

  if ((NO_SECRETS)); then
    cat >"${boot_dir}/boot.sh" <<'GUEST_BOOT_NOSECRETS'
#!/bin/sh
set -eu

if [ -x /root/firewall.sh ]; then
  /root/firewall.sh
fi

swapoff -a 2>/dev/null || true

mkdir -p /run/secrets
if ! mountpoint -q /run/secrets; then
  mount -t tmpfs -o size=50M,mode=0700,nosuid,noexec tmpfs /run/secrets
fi

mkdir -p /ipc/requests /ipc/responses
IPC_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
printf '%s\n' "$IPC_KEY" > /run/secrets/ipc.key
chmod 0600 /run/secrets/ipc.key
printf '%s\n' "$IPC_KEY" > /ipc/ipc.key
chmod 0600 /ipc/ipc.key

if [ -d /sys/fs/cgroup ]; then
  CG=/sys/fs/cgroup/void
  mkdir -p "$CG" 2>/dev/null || true
  echo $((1536*1024*1024)) > "$CG/memory.max" 2>/dev/null || true
  echo $((1280*1024*1024)) > "$CG/memory.high" 2>/dev/null || true
  echo "200000 100000" > "$CG/cpu.max" 2>/dev/null || true
  echo "192" > "$CG/pids.max" 2>/dev/null || true
fi

exec python3 /usr/local/bin/command-proxy-daemon
GUEST_BOOT_NOSECRETS
  else
    cat >"${boot_dir}/boot.sh" <<'GUEST_BOOT'
#!/bin/sh
set -eu

if [ -x /root/firewall.sh ]; then
  /root/firewall.sh
fi

swapoff -a 2>/dev/null || true

mkdir -p /run/secrets
if ! mountpoint -q /run/secrets; then
  mount -t tmpfs -o size=50M,mode=0700,nosuid,noexec tmpfs /run/secrets
fi

mkdir -p /ipc/requests /ipc/responses
IPC_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
printf '%s\n' "$IPC_KEY" > /run/secrets/ipc.key
chmod 0600 /run/secrets/ipc.key
printf '%s\n' "$IPC_KEY" > /ipc/ipc.key
chmod 0600 /ipc/ipc.key

cp /secrets_in/env /run/secrets/env
chmod 0600 /run/secrets/env
umount /secrets_in || true

# Copy any JSON files from secrets_in to /run/secrets
for jf in /secrets_in/*.json; do
  [ -f "$jf" ] || continue
  cp "$jf" /run/secrets/
  chmod 0600 "/run/secrets/$(basename "$jf")"
done

# Auto-activate gcloud service account
SA_JSON="/run/secrets/google_application_credentials_json.json"
if [ -f "$SA_JSON" ]; then
  export GOOGLE_APPLICATION_CREDENTIALS="$SA_JSON"
  sed -i 's|^GOOGLE_APPLICATION_CREDENTIALS_JSON=.*|GOOGLE_APPLICATION_CREDENTIALS='"$SA_JSON"'|' /run/secrets/env
  if command -v gcloud >/dev/null 2>&1; then
    SA_EMAIL=$(python3 -c "import json;print(json.load(open('$SA_JSON'))['client_email'])" 2>/dev/null || true)
    if [ -n "$SA_EMAIL" ]; then
      gcloud auth activate-service-account "$SA_EMAIL" --key-file="$SA_JSON" >/dev/null 2>&1 || true
    fi
  fi
fi

# Auto-activate gh if GH_TOKEN is in secrets
if grep -q '^GH_TOKEN=' /run/secrets/env 2>/dev/null; then
  GH_TOKEN=$(grep '^GH_TOKEN=' /run/secrets/env | sed 's/^GH_TOKEN=//')
  export GH_TOKEN
  if command -v gh >/dev/null 2>&1; then
    gh auth setup-git 2>/dev/null || true
  fi
fi

if [ -d /sys/fs/cgroup ]; then
  CG=/sys/fs/cgroup/void
  mkdir -p "$CG" 2>/dev/null || true
  echo $((1536*1024*1024)) > "$CG/memory.max" 2>/dev/null || true
  echo $((1280*1024*1024)) > "$CG/memory.high" 2>/dev/null || true
  echo "200000 100000" > "$CG/cpu.max" 2>/dev/null || true
  echo "192" > "$CG/pids.max" 2>/dev/null || true
fi

exec python3 /usr/local/bin/command-proxy-daemon
GUEST_BOOT
  fi
  chmod 0755 "${boot_dir}/boot.sh"

  # Attach ALL volumes in a single changevm call.
  # Multiple calls would overwrite previous volume mappings.
  local vol_args=()
  if [[ -n "${secrets_dir:-}" ]]; then
    vol_args+=(--volume "${secrets_dir}:/secrets_in")
  fi
  vol_args+=(--volume "${luks_mount}:/secrets")
  vol_args+=(--volume "${ipc_dir}:/ipc" --volume "${boot_dir}:/boot")

  log_info "Attaching volumes to VM: ${vol_args[*]}"
  run "$krunvm_bin" changevm "$VM_NAME" "${vol_args[@]}"

  local ipc_key="pending"
  write_runtime_state "${secrets_dir:-none}" "${luks_mount:-none}" "$LUKS_MAPPER" "$VM_LOG_FILE" "$ipc_dir" "$ipc_key"

  # Install host-side proxy script
  log_info "Installing void to /usr/local/bin/void..."
  if ((DRY_RUN)); then
    printf "[dry-run] write_host_proxy_script /usr/local/bin/void %s\n" "$RUNTIME_ENV_FILE" >&2
  else
    write_host_proxy_script "/usr/local/bin/void" "$RUNTIME_ENV_FILE"
  fi

  log_info "Starting VM in background..."
  if ((DRY_RUN)); then
    printf "[dry-run] nohup krunvm start %q -- /boot/boot.sh > %q 2>&1 &\n" "$VM_NAME" "$VM_LOG_FILE" >&2
  else
    nohup "$krunvm_bin" start "$VM_NAME" -- /boot/boot.sh >"$VM_LOG_FILE" 2>&1 &
    echo "$!" > "$PID_FILE"
  fi

  if ! ((DRY_RUN)); then
    log_info "Waiting for daemon heartbeat..."
    local heartbeat_file="${ipc_dir}/heartbeat"
    local ipc_key_file="${ipc_dir}/ipc.key"
    local i
    for ((i=0; i<60; i++)); do
      if [[ -f "$heartbeat_file" ]]; then
        log_info "Daemon is alive (heartbeat detected)."
        break
      fi
      sleep 1
    done
    if [[ ! -f "$heartbeat_file" ]]; then
      log_warn "No heartbeat after 60s. Daemon may still be starting. Check ${VM_LOG_FILE}."
    fi

    log_info "Waiting for IPC authentication key..."
    for ((i=0; i<30; i++)); do
      if [[ -s "$ipc_key_file" ]]; then
        ipc_key="$(tr -d '[:space:]' < "$ipc_key_file" 2>/dev/null || true)"
        if [[ "$ipc_key" =~ ^[0-9a-fA-F]{64}$ ]]; then
          break
        fi
      fi
      sleep 1
    done
    if [[ ! "$ipc_key" =~ ^[0-9a-fA-F]{64}$ ]]; then
      die "IPC key unavailable or invalid after boot. Check ${VM_LOG_FILE}."
    fi
  else
    ipc_key="$(printf '0%.0s' {1..64})"
  fi

  write_runtime_state "${secrets_dir:-none}" "${luks_mount:-none}" "$LUKS_MAPPER" "$VM_LOG_FILE" "$ipc_dir" "$ipc_key"

  log_info "VM started successfully."
  log_info "Use 'void gh ...', 'void gcloud ...', 'void browse ...', or 'void session ...'."
}

cmd_stop() {
  need_root
  ensure_state_dirs

  local krunvm_bin
  krunvm_bin="$(command -v krunvm 2>/dev/null || true)"
  if [[ -z "$krunvm_bin" && -x /root/.cargo/bin/krunvm ]]; then
    krunvm_bin="/root/.cargo/bin/krunvm"
  fi
  if [[ -z "$krunvm_bin" && "$DRY_RUN" -eq 0 ]]; then
    die "krunvm not found."
  fi
  [[ -n "$krunvm_bin" ]] || krunvm_bin="krunvm"

  if vm_exists; then
    if vm_is_running; then
      log_info "Stopping VM '${VM_NAME}'..."
      run "$krunvm_bin" stop "$VM_NAME"
    else
      log_info "VM '${VM_NAME}' is not running."
    fi
  else
    log_warn "VM '${VM_NAME}' does not exist."
  fi

  local secrets_dir=""
  local luks_mount=""
  local mapper=""
  local ipc_dir=""

  if [[ -f "$RUNTIME_ENV_FILE" ]]; then
    secrets_dir="$(read_runtime_value "SECRETS_DIR" || true)"
    luks_mount="$(read_runtime_value "LUKS_MOUNT" || true)"
    mapper="$(read_runtime_value "LUKS_MAPPER" || true)"
    ipc_dir="$(read_runtime_value "IPC_DIR" || true)"
  fi

  # Unmount ALL luks mounts (scan /run dir in case runtime.env is stale)
  local mount_path
  for mount_path in "${RUN_ROOT}"/luks.*.*; do
    [[ -d "$mount_path" ]] || continue
    if mountpoint -q "$mount_path" 2>/dev/null; then
      log_info "Unmounting ${mount_path}..."
      umount "$mount_path" 2>/dev/null || umount -l "$mount_path" 2>/dev/null || true
    fi
    rmdir "$mount_path" 2>/dev/null || true
  done
  # Also unmount the specific one from runtime.env if different
  if [[ -n "$luks_mount" && -d "$luks_mount" ]]; then
    if mountpoint -q "$luks_mount" 2>/dev/null; then
      log_info "Unmounting LUKS mount ${luks_mount}..."
      umount "$luks_mount" 2>/dev/null || umount -l "$luks_mount" 2>/dev/null || true
    fi
    rmdir "$luks_mount" 2>/dev/null || true
  fi

  # Close LUKS mapper (try default name if runtime.env doesn't have it)
  mapper="${mapper:-${LUKS_MAPPER}}"
  if cryptsetup status "$mapper" >/dev/null 2>&1; then
    log_info "Closing LUKS mapper ${mapper}..."
    run cryptsetup close "$mapper" || run cryptsetup close --deferred "$mapper" || true
  fi

  if [[ -n "$secrets_dir" && -d "$secrets_dir" ]]; then
    if [[ -f "${secrets_dir}/env" ]]; then
      if command_exists shred; then
        run shred -u "${secrets_dir}/env" || run rm -f "${secrets_dir}/env"
      else
        run rm -f "${secrets_dir}/env"
      fi
    fi
    run rmdir "$secrets_dir" || true
  fi

  if [[ -n "$ipc_dir" && -d "$ipc_dir" ]]; then
    log_info "Cleaning up IPC directory ${ipc_dir}..."
    run rm -rf "$ipc_dir"
  fi

  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1; then
      log_info "Stopping residual krunvm process ${pid}..."
      run kill "$pid"
    fi
    run rm -f "$PID_FILE"
  fi

  # Remove host-side proxy script
  if [[ -f /usr/local/bin/void ]]; then
    run rm -f /usr/local/bin/void
  fi

  run rm -f "$RUNTIME_ENV_FILE"

  log_info "Stop complete."
}

cmd_grant() {
  need_root
  ensure_state_dirs

  if [[ ${#EXTRAS[@]} -lt 1 ]]; then
    die_usage "Usage: setup-secure-vm.sh grant <username> [username...]"
  fi

  local ipc_dir=""
  if [[ -f "$RUNTIME_ENV_FILE" ]]; then
    ipc_dir="$(read_runtime_value "IPC_DIR" || true)"
  fi

  for username in "${EXTRAS[@]}"; do
    if ! id "$username" >/dev/null 2>&1; then
      die "User '$username' does not exist."
    fi

    log_info "Granting void access to user '${username}'..."

    local user_group
    user_group="$(id -gn "$username")"

    # runtime.env must be readable by granted user only
    if [[ -f "$RUNTIME_ENV_FILE" ]]; then
      run chmod 0640 "$RUNTIME_ENV_FILE"
      if command_exists setfacl; then
        run setfacl -m "u:${username}:r" "$RUNTIME_ENV_FILE"
      else
        run chgrp "$user_group" "$RUNTIME_ENV_FILE"
      fi
    fi

    # IPC directory must be read/writable by granted user only
    if [[ -n "$ipc_dir" && -d "$ipc_dir" ]]; then
      run chmod 0770 "$ipc_dir"
      run chmod 0770 "${ipc_dir}/requests" "${ipc_dir}/responses"
      if command_exists setfacl; then
        run setfacl -m "u:${username}:rwx" "$ipc_dir" "${ipc_dir}/requests" "${ipc_dir}/responses"
      else
        run chgrp "$user_group" "$ipc_dir" "${ipc_dir}/requests" "${ipc_dir}/responses"
      fi
    else
      # Scan for any IPC dirs
      local d
      for d in "${RUN_ROOT}"/vm-ipc.${VM_NAME}.*; do
        [[ -d "$d" ]] || continue
        run chmod 0770 "$d" "$d/requests" "$d/responses"
        if command_exists setfacl; then
          run setfacl -m "u:${username}:rwx" "$d" "$d/requests" "$d/responses"
        else
          run chgrp "$user_group" "$d" "$d/requests" "$d/responses"
        fi
      done
    fi

    log_info "User '${username}' can now run: void gh ... / void gcloud ... / void browse ... / void session ..."
  done
}

cmd_status() {
  ensure_state_dirs

  local vm_present="no"
  local vm_running="no"
  local mapper_open="no"
  local ipc_active="no"
  local daemon_alive="no"

  if vm_exists; then
    vm_present="yes"
  fi
  if vm_is_running; then
    vm_running="yes"
  fi
  if cryptsetup status "$LUKS_MAPPER" >/dev/null 2>&1; then
    mapper_open="yes"
  fi

  local ipc_dir=""
  if [[ -f "$RUNTIME_ENV_FILE" ]]; then
    ipc_dir="$(read_runtime_value "IPC_DIR" || true)"
  fi
  if [[ -n "$ipc_dir" && -d "$ipc_dir" ]]; then
    ipc_active="yes"
    # Check daemon heartbeat (should update every ~5s)
    if [[ -f "${ipc_dir}/heartbeat" ]]; then
      local hb_time
      hb_time="$(cat "${ipc_dir}/heartbeat" 2>/dev/null || echo 0)"
      local now
      now="$(date +%s)"
      if [[ $((now - hb_time)) -lt 15 ]]; then
        daemon_alive="yes"
      fi
    fi
  fi

  # ── Basic status ──
  echo "=== Void VM Status ==="
  echo "version=${VERSION}"
  echo "vm_name=${VM_NAME}"
  echo "vm_exists=${vm_present}"
  echo "vm_running=${vm_running}"
  echo "luks_mapper_open=${mapper_open}"
  echo "ipc_active=${ipc_active}"
  echo "daemon_alive=${daemon_alive}"
  echo ""

  # ── Heartbeat detail ──
  if [[ -n "$ipc_dir" && -f "${ipc_dir}/heartbeat" ]]; then
    local hb_time now hb_age
    hb_time="$(cat "${ipc_dir}/heartbeat" 2>/dev/null || echo 0)"
    now="$(date +%s)"
    hb_age=$((now - hb_time))
    echo "=== Heartbeat ==="
    echo "last_heartbeat=$(date -d "@$hb_time" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || date -r "$hb_time" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "$hb_time")"
    echo "age_seconds=${hb_age}"
    echo ""
  fi

  # ── IPC queues ──
  if [[ -n "$ipc_dir" && -d "$ipc_dir" ]]; then
    echo "=== IPC ==="
    echo "ipc_dir=${ipc_dir}"
    echo "pending_requests=$(ls "${ipc_dir}/requests/"*.json 2>/dev/null | wc -l)"
    echo "stored_responses=$(ls "${ipc_dir}/responses/"*.json 2>/dev/null | wc -l)"
    if [[ -f "${ipc_dir}/ipc.key" ]]; then
      echo "ipc_key=present ($(wc -c < "${ipc_dir}/ipc.key" 2>/dev/null || echo 0) bytes)"
    else
      echo "ipc_key=MISSING"
    fi
    echo ""
  fi

  # ── krunvm list ──
  if command_exists krunvm || [[ -x /root/.cargo/bin/krunvm ]]; then
    local krunvm_bin
    krunvm_bin="$(command -v krunvm 2>/dev/null || true)"
    [[ -n "$krunvm_bin" ]] || krunvm_bin="/root/.cargo/bin/krunvm"
    echo "=== krunvm list ==="
    "$krunvm_bin" list 2>&1 || echo "(krunvm list failed)"
    echo ""
  fi

  # ── VM process ──
  echo "=== VM Process ==="
  local vm_pids
  vm_pids="$(pgrep -f "krun.*${VM_NAME}" 2>/dev/null || true)"
  if [[ -n "$vm_pids" ]]; then
    echo "vm_pids=${vm_pids}"
    ps -p "$vm_pids" -o pid,user,vsz,rss,etime,args --no-headers 2>/dev/null || true
  else
    echo "vm_pids=none"
  fi
  echo ""

  # ── DNS check (inside VM via test command) ──
  echo "=== Network (quick probe) ==="
  if [[ "$daemon_alive" == "yes" && -n "$ipc_dir" ]]; then
    # Send an internal status probe command to test daemon request path
    local probe_id="status-probe-$$"
    local probe_req="${ipc_dir}/requests/${probe_id}.json"
    local probe_resp="${ipc_dir}/responses/${probe_id}.json"
    local ipc_key_hex=""
    if [[ -f "${ipc_dir}/ipc.key" ]]; then
      ipc_key_hex="$(cat "${ipc_dir}/ipc.key" 2>/dev/null || true)"
    fi
    # Build signed probe request
    local probe_ts
    probe_ts="$(date +%s)"
    local probe_json="{\"id\":\"${probe_id}\",\"argv\":[\"__status__\"],\"timeout\":10,\"timestamp\":${probe_ts}}"
    if [[ -n "$ipc_key_hex" ]] && command -v python3 >/dev/null 2>&1; then
      local probe_hmac
      probe_hmac="$(python3 -c "
import hmac, hashlib, json
key = bytes.fromhex('${ipc_key_hex}')
payload = json.loads('${probe_json}')
canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
print(hmac.new(key, canonical.encode('utf-8'), hashlib.sha256).hexdigest())
" 2>/dev/null || true)"
      if [[ -n "$probe_hmac" ]]; then
        probe_json="{\"id\":\"${probe_id}\",\"argv\":[\"__status__\"],\"timeout\":10,\"timestamp\":${probe_ts},\"hmac\":\"${probe_hmac}\"}"
      fi
    fi
    echo "$probe_json" > "$probe_req" 2>/dev/null
    # Wait up to 15s for response
    local waited=0
    while [[ ! -f "$probe_resp" && $waited -lt 15 ]]; do
      sleep 1
      waited=$((waited + 1))
    done
    if [[ -f "$probe_resp" ]]; then
      echo "status_probe=ok (${waited}s)"
      local probe_exit
      probe_exit="$(python3 -c "import json;print(json.load(open('${probe_resp}'))['exit_code'])" 2>/dev/null || echo "?")"
      local probe_stdout
      probe_stdout="$(python3 -c "import json;print(json.load(open('${probe_resp}')).get('stdout','')[:200])" 2>/dev/null || echo "")"
      local probe_stderr
      probe_stderr="$(python3 -c "import json;print(json.load(open('${probe_resp}')).get('stderr','')[:200])" 2>/dev/null || echo "")"
      echo "exit_code=${probe_exit}"
      [[ -n "$probe_stdout" ]] && echo "stdout=${probe_stdout}"
      [[ -n "$probe_stderr" ]] && echo "stderr=${probe_stderr}"
      rm -f "$probe_resp"
    else
      echo "status_probe=TIMEOUT (15s)"
    fi
    rm -f "$probe_req"
    echo ""

    # Browser probe
    echo "=== Browser (quick probe) ==="
    local bprobe_id="status-browser-probe-$$"
    local bprobe_req="${ipc_dir}/requests/${bprobe_id}.json"
    local bprobe_resp="${ipc_dir}/responses/${bprobe_id}.json"
    local bprobe_ts
    bprobe_ts="$(date +%s)"
    local bprobe_json="{\"id\":\"${bprobe_id}\",\"tool\":\"browser\",\"action\":\"session list\",\"timeout\":10,\"timestamp\":${bprobe_ts}}"
    if [[ -n "$ipc_key_hex" ]] && command -v python3 >/dev/null 2>&1; then
      local bprobe_hmac
      bprobe_hmac="$(python3 -c "
import hmac, hashlib, json
key = bytes.fromhex('${ipc_key_hex}')
payload = json.loads('${bprobe_json}')
canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
print(hmac.new(key, canonical.encode('utf-8'), hashlib.sha256).hexdigest())
" 2>/dev/null || true)"
      if [[ -n "$bprobe_hmac" ]]; then
        bprobe_json="{\"id\":\"${bprobe_id}\",\"tool\":\"browser\",\"action\":\"session list\",\"timeout\":10,\"timestamp\":${bprobe_ts},\"hmac\":\"${bprobe_hmac}\"}"
      fi
    fi
    echo "$bprobe_json" > "$bprobe_req" 2>/dev/null
    waited=0
    while [[ ! -f "$bprobe_resp" && $waited -lt 15 ]]; do
      sleep 1
      waited=$((waited + 1))
    done
    if [[ -f "$bprobe_resp" ]]; then
      echo "browser_session_probe=ok (${waited}s)"
      local bprobe_exit
      bprobe_exit="$(python3 -c "import json;print(json.load(open('${bprobe_resp}'))['exit_code'])" 2>/dev/null || echo "?")"
      local bprobe_stdout
      bprobe_stdout="$(python3 -c "import json;print(json.load(open('${bprobe_resp}')).get('stdout','')[:200])" 2>/dev/null || echo "")"
      echo "exit_code=${bprobe_exit}"
      [[ -n "$bprobe_stdout" ]] && echo "stdout=${bprobe_stdout}"
      rm -f "$bprobe_resp"
    else
      echo "browser_session_probe=TIMEOUT (15s)"
    fi
    rm -f "$bprobe_req"
    echo ""

    # DNS probe via browser open
    echo "=== DNS (browser open probe) ==="
    local dprobe_id="status-dns-probe-$$"
    local dprobe_req="${ipc_dir}/requests/${dprobe_id}.json"
    local dprobe_resp="${ipc_dir}/responses/${dprobe_id}.json"
    local dprobe_ts
    dprobe_ts="$(date +%s)"
    local dprobe_json="{\"id\":\"${dprobe_id}\",\"tool\":\"browser\",\"action\":\"open\",\"params\":{\"url\":\"https://example.com\"},\"timeout\":15,\"timestamp\":${dprobe_ts}}"
    if [[ -n "$ipc_key_hex" ]] && command -v python3 >/dev/null 2>&1; then
      local dprobe_hmac
      dprobe_hmac="$(python3 -c "
import hmac, hashlib, json
key = bytes.fromhex('${ipc_key_hex}')
payload = json.loads('${dprobe_json}')
canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
print(hmac.new(key, canonical.encode('utf-8'), hashlib.sha256).hexdigest())
" 2>/dev/null || true)"
      if [[ -n "$dprobe_hmac" ]]; then
        dprobe_json="{\"id\":\"${dprobe_id}\",\"tool\":\"browser\",\"action\":\"open\",\"params\":{\"url\":\"https://example.com\"},\"timeout\":15,\"timestamp\":${dprobe_ts},\"hmac\":\"${dprobe_hmac}\"}"
      fi
    fi
    echo "$dprobe_json" > "$dprobe_req" 2>/dev/null
    waited=0
    while [[ ! -f "$dprobe_resp" && $waited -lt 20 ]]; do
      sleep 1
      waited=$((waited + 1))
    done
    if [[ -f "$dprobe_resp" ]]; then
      local dprobe_exit
      dprobe_exit="$(python3 -c "import json;print(json.load(open('${dprobe_resp}'))['exit_code'])" 2>/dev/null || echo "?")"
      local dprobe_stdout
      dprobe_stdout="$(python3 -c "import json;print(json.load(open('${dprobe_resp}')).get('stdout','')[:300])" 2>/dev/null || echo "")"
      local dprobe_stderr
      dprobe_stderr="$(python3 -c "import json;print(json.load(open('${dprobe_resp}')).get('stderr','')[:300])" 2>/dev/null || echo "")"
      if [[ "$dprobe_exit" == "0" ]]; then
        echo "dns_and_browse=OK (${waited}s)"
      else
        echo "dns_and_browse=FAILED (${waited}s, exit_code=${dprobe_exit})"
      fi
      [[ -n "$dprobe_stdout" ]] && echo "stdout=${dprobe_stdout}"
      [[ -n "$dprobe_stderr" ]] && echo "stderr=${dprobe_stderr}"
      rm -f "$dprobe_resp"
    else
      echo "dns_and_browse=TIMEOUT (20s) — likely DNS resolution failure inside VM"
    fi
    rm -f "$dprobe_req"
    echo ""
  else
    echo "daemon not alive — skipping probes"
    echo ""
  fi

  # ── Daemon log tail ──
  echo "=== Daemon Log (last 20 lines) ==="
  if [[ -f "$VM_LOG_FILE" ]]; then
    tail -20 "$VM_LOG_FILE"
  else
    echo "(no log file at ${VM_LOG_FILE})"
  fi
  echo ""

  # ── Runtime env ──
  echo "=== Runtime State ==="
  if [[ -f "$RUNTIME_ENV_FILE" ]]; then
    cat "$RUNTIME_ENV_FILE"
  else
    echo "(no runtime state file)"
  fi
}

cmd_test() {
  local test_root
  test_root="$(mktemp -d /tmp/setup-secure-vm-test.XXXXXX)"
  track_tmp_path "$test_root"

  local daemon_py="${test_root}/command-proxy-daemon.py"
  local login_portal_py="${test_root}/void-login-portal.py"
  local firewall_sh="${test_root}/firewall.sh"
  local host_proxy="${test_root}/void"
  local runtime_env="${test_root}/runtime.env"
  local void_dir
  void_dir="$(dirname "$SCRIPT_PATH")"
  local deploy_sh="${void_dir}/webrtc/deploy.sh"
  local readme_md="${void_dir}/README.md"

  write_proxy_daemon "$daemon_py"
  write_login_portal_script "$login_portal_py"
  write_firewall_script "$firewall_sh"
  write_host_proxy_script "$host_proxy" "$runtime_env"

  python3 - "$daemon_py" "$login_portal_py" "$firewall_sh" "$host_proxy" "$SCRIPT_PATH" "$deploy_sh" "$readme_md" <<'PY_TEST'
import hashlib
import importlib.util
import json
import logging
import os
import pathlib
import re
import subprocess
import sys
import tempfile

daemon_path = pathlib.Path(sys.argv[1])
login_portal_path = pathlib.Path(sys.argv[2])
firewall_path = pathlib.Path(sys.argv[3])
host_proxy_path = pathlib.Path(sys.argv[4])
setup_script_path = pathlib.Path(sys.argv[5])
deploy_script_path = pathlib.Path(sys.argv[6])
readme_path = pathlib.Path(sys.argv[7])
login_portal_source = login_portal_path.read_text(encoding="utf-8")

spec = importlib.util.spec_from_file_location("proxy_daemon", daemon_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

logger = logging.getLogger("setup-secure-vm-test")
logger.addHandler(logging.NullHandler())
cfg = dict(module.DEFAULT_CONFIG)

tests = []
failures = []


def check(name, condition, detail=""):
    tests.append(name)
    if condition:
        print(f"PASS {name}")
    else:
        failures.append((name, detail))
        print(f"FAIL {name}: {detail}")


# 1. allow open https
ok, argv, err = module.validate_browser_command(
    "open", {"session": "default", "url": "https://example.com"}, cfg
)
check("test_browser_allow_open_https", ok and argv and argv[-1] == "https://example.com", err or "")

# 2. deny http scheme
ok, _, err = module.validate_browser_command(
    "open", {"session": "default", "url": "http://example.com"}, cfg
)
check("test_browser_deny_http_scheme", not ok and "https" in (err or "").lower(), err or "")

# 3. deny private IP
ok, _, err = module.validate_browser_command(
    "open", {"session": "default", "url": "https://10.0.0.1"}, cfg
)
check("test_browser_deny_private_ip_url", not ok, err or "")

# 4. deny metadata hostname
ok, _, err = module.validate_browser_command(
    "open", {"session": "default", "url": "https://169.254.169.254"}, cfg
)
check("test_browser_deny_metadata_hostname", not ok, err or "")

# 5. whitelist denies eval
ok, _, err = module.validate_browser_command(
    "evaluate", {"session": "default", "args": ["1+1"]}, cfg
)
check("test_browser_whitelist_denies_eval", not ok, err or "")

# 6. whitelist denies cookies
ok, _, err = module.validate_browser_command(
    "cookies", {"session": "default"}, cfg
)
check("test_browser_whitelist_denies_cookies", not ok, err or "")

# 7. whitelist denies storage
ok, _, err = module.validate_browser_command(
    "localStorage", {"session": "default"}, cfg
)
check("test_browser_whitelist_denies_storage", not ok, err or "")

# 8. whitelist denies state
ok, _, err = module.validate_browser_command(
    "state save", {"session": "default", "args": ["x.json"]}, cfg
)
check("test_browser_whitelist_denies_state", not ok, err or "")

# 9. whitelist denies get html
ok, _, err = module.validate_browser_command(
    "get html", {"session": "default", "args": ["body"]}, cfg
)
check("test_browser_whitelist_denies_get_html", not ok, err or "")

# 10. whitelist denies set headers
ok, _, err = module.validate_browser_command(
    "set headers", {"session": "default", "args": ["{}"]}, cfg
)
check("test_browser_whitelist_denies_set_headers", not ok, err or "")

# 11. whitelist denies set credentials
ok, _, err = module.validate_browser_command(
    "set credentials", {"session": "default", "args": ["u", "p"]}, cfg
)
check("test_browser_whitelist_denies_set_credentials", not ok, err or "")

# 12. wait denies --fn
ok, _, err = module.validate_browser_command(
    "wait", {"session": "default", "args": ["--fn", "true"]}, cfg
)
check("test_browser_wait_denies_fn", not ok, err or "")

# 13. wait denies --download
ok, _, err = module.validate_browser_command(
    "wait", {"session": "default", "args": ["--download"]}, cfg
)
check("test_browser_wait_denies_download", not ok, err or "")

# 14. browser env is sanitized
env = module.build_browser_env()
check(
    "test_browser_env_is_sanitized",
    "GH_TOKEN" not in env
    and "GOOGLE_APPLICATION_CREDENTIALS" not in env
    and env.get("HOME") in {"/var/lib/void", "/tmp/void-browser-default"}
    and env.get("PLAYWRIGHT_CHROMIUM_ARGS") == "--disable-webrtc",
    str(env),
)

# 15. firewall allows 443 in browser chain
fw = firewall_path.read_text(encoding="utf-8")
check(
    "test_browser_firewall_allows_443",
    "VOID_BROWSER_EGRESS" in fw and "--dport 443 -j ACCEPT" in fw,
    "missing browser 443 allow rule",
)

# 16. firewall blocks metadata
check(
    "test_browser_firewall_blocks_metadata",
    "169.254.169.254/32" in fw,
    "missing metadata deny rule",
)

# 17. firewall blocks private ranges
check(
    "test_browser_firewall_blocks_private_ranges",
    "10.0.0.0/8" in fw and "172.16.0.0/12" in fw and "192.168.0.0/16" in fw,
    "missing one or more private range deny rules",
)

# 18. firewall must fail closed if iptables is unavailable
with tempfile.TemporaryDirectory(prefix="void-fw-path.") as fake_path:
    probe = subprocess.run(
        ["/bin/bash", str(firewall_path)],
        env={**os.environ, "PATH": fake_path},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
check(
    "test_firewall_fails_closed_without_iptables",
    probe.returncode != 0,
    f"expected non-zero exit when iptables missing, got {probe.returncode}",
)

# 19. firewall includes IPv6 policy rules
check(
    "test_firewall_has_ipv6_rules",
    "ip6tables" in fw and "::1/128" in fw,
    "missing ip6tables IPv6 rules",
)

# 20. scrub query tokens
scrubbed = module.scrub_browser_text("https://example.com/?token=secret123&x=1")
check(
    "test_browser_scrubs_query_tokens",
    "secret123" not in scrubbed and "token=[REDACTED]" in scrubbed,
    scrubbed,
)

# 21. session limit enforced
module.active_browser_sessions.clear()
module.active_browser_sessions.add("s1")
module.active_browser_sessions.add("s2")
limit_cfg = dict(cfg)
limit_cfg["max_browser_sessions"] = 2
allowed, msg = module.browser_session_allowed(("open",), "s3", limit_cfg)
check("test_browser_session_limit_enforced", not allowed, msg or "")

# 22. timeout enforced
slow_timeout = module.browser_timeout_for_action("open", 999)
fast_timeout = module.browser_timeout_for_action("click", 999)
check(
    "test_browser_timeout_enforced",
    slow_timeout == 25 and fast_timeout == 10,
    f"slow={slow_timeout} fast={fast_timeout}",
)

# 23. session login action is allowed
ok, argv, err = module.validate_browser_command(
    "session login", {"session": "github", "args": []}, cfg
)
check("test_browser_allows_session_login", ok and argv and "session" in argv, err or "")

# 24. per-session origin policy blocks off-domain open
ok, _, err = module.validate_browser_command(
    "open", {"session": "github", "url": "https://example.com"}, cfg
)
check("test_browser_denies_offdomain_session_open", not ok and "allowlist" in (err or "").lower(), err or "")

# 25. login portal goto enforces allow-hosts before navigation
check(
    "test_portal_goto_enforces_allow_hosts",
    bool(re.search(r'if msg_type == "goto":[\s\S]{0,500}url_host_allowed\(next_url,\s*allow_hosts\)', login_portal_source)),
    "goto handler missing allow-host enforcement",
)

# 26. login portal goto returns explicit 403-style rejection for denied hosts
check(
    "test_portal_goto_returns_403_for_denied_host",
    bool(re.search(r'if msg_type == "goto":[\s\S]{0,700}403', login_portal_source)),
    "goto handler missing explicit 403 rejection message",
)

# 27. chromium no-sandbox must be conditional on root
check(
    "test_portal_no_sandbox_only_for_root",
    '"--no-sandbox",' not in login_portal_source
    and bool(re.search(r'if\s+no_sandbox\s+and\s+os\.getuid\(\)\s*==\s*0', login_portal_source)),
    "no-sandbox flag must be gated on os.getuid() == 0",
)

# 28. disabling chromium sandbox must log a clear warning
check(
    "test_portal_logs_warning_when_sandbox_disabled",
    "running as root" in login_portal_source.lower() and "--no-sandbox" in login_portal_source,
    "missing explicit warning when chromium sandbox is disabled",
)

# Host proxy regression check for browse wiring
host_proxy = host_proxy_path.read_text(encoding="utf-8")
check(
    "test_host_proxy_allows_browse_tool",
    "gh|gcloud|browse|session|omi" in host_proxy and "\"tool\": \"browser\"" in host_proxy,
    "browse tool mapping missing",
)

# ── gh/gcloud ACL tests ──────────────────────────────────────────────

# 29. gh allowed
ok, err = module.check_command(["gh", "pr", "list"], cfg, logger)
check("test_gh_pr_list_allowed", ok, err or "")

# 30. gcloud allowed
ok, err = module.check_command(["gcloud", "projects", "list"], cfg, logger)
check("test_gcloud_projects_list_allowed", ok, err or "")

# 31. deny tool not in allowed list
ok, err = module.check_command(["curl", "https://example.com"], cfg, logger)
check("test_deny_tool_not_allowed_curl", not ok and "not allowed" in (err or "").lower(), err or "")

# 25. deny node
ok, err = module.check_command(["node", "-e", "1+1"], cfg, logger)
check("test_deny_tool_not_allowed_node", not ok and "not allowed" in (err or "").lower(), err or "")

# 26. deny python3
ok, err = module.check_command(["python3", "-c", "import os"], cfg, logger)
check("test_deny_tool_not_allowed_python3", not ok and "not allowed" in (err or "").lower(), err or "")

# 27. deny gh auth token (leaks PAT)
ok, err = module.check_command(["gh", "auth", "token"], cfg, logger)
check("test_deny_gh_auth_token", not ok and "denied" in (err or "").lower(), err or "")

# 28. deny gh auth login (would overwrite creds)
ok, err = module.check_command(["gh", "auth", "login"], cfg, logger)
check("test_deny_gh_auth_login", not ok and "denied" in (err or "").lower(), err or "")

# 29. deny gcloud auth print-access-token
ok, err = module.check_command(["gcloud", "auth", "print-access-token"], cfg, logger)
check("test_deny_gcloud_print_access_token", not ok and "denied" in (err or "").lower(), err or "")

# 30. deny gcloud auth print-identity-token
ok, err = module.check_command(["gcloud", "auth", "print-identity-token"], cfg, logger)
check("test_deny_gcloud_print_identity_token", not ok and "denied" in (err or "").lower(), err or "")

# 31. deny gcloud config set (could redirect auth)
ok, err = module.check_command(["gcloud", "config", "set", "core/project", "evil"], cfg, logger)
check("test_deny_gcloud_config_set", not ok and "denied" in (err or "").lower(), err or "")

# 32. deny gcloud auth login
ok, err = module.check_command(["gcloud", "auth", "login"], cfg, logger)
check("test_deny_gcloud_auth_login", not ok and "denied" in (err or "").lower(), err or "")

# 33. deny --log-http (global deny regex — leaks auth headers)
ok, err = module.check_command(["gcloud", "compute", "instances", "list", "--log-http"], cfg, logger)
check("test_deny_gcloud_log_http", not ok and "denied" in (err or "").lower(), err or "")

# 34. deny --verbosity=debug (global deny regex)
ok, err = module.check_command(["gcloud", "compute", "instances", "list", "--verbosity=debug"], cfg, logger)
check("test_deny_gcloud_verbosity_debug", not ok and "denied" in (err or "").lower(), err or "")

# 35. deny empty command
ok, err = module.check_command([], cfg, logger)
check("test_deny_empty_command", not ok, err or "")

# 36. deny gh api (not in allowlist)
ok, err = module.check_command(["gh", "api", "/user"], cfg, logger)
check("test_deny_gh_api_subcommand", not ok and "denied" in (err or "").lower(), err or "")

# 37. deny gh extension install (not in allowlist)
ok, err = module.check_command(["gh", "extension", "install", "owner/ext"], cfg, logger)
check("test_deny_gh_extension_install", not ok and "denied" in (err or "").lower(), err or "")

# 38. deny gcloud secrets access (not in allowlist)
ok, err = module.check_command(["gcloud", "secrets", "versions", "access", "latest"], cfg, logger)
check("test_deny_gcloud_secrets_versions_access", not ok and "denied" in (err or "").lower(), err or "")

# 39. deny gh --body-file flag (reads arbitrary files)
ok, err = module.check_command(
    ["gh", "issue", "create", "--title", "x", "--body-file", "/etc/passwd"],
    cfg,
    logger,
)
check("test_deny_gh_body_file_flag", not ok and "denied" in (err or "").lower(), err or "")

# 40. allow safe inline body for gh issue create
ok, err = module.check_command(
    ["gh", "issue", "create", "--title", "x", "--body", "safe text"],
    cfg,
    logger,
)
check("test_allow_gh_inline_body_flag", ok, err or "")

# 41. deny gh --template with local path
ok, err = module.check_command(
    ["gh", "issue", "create", "--title", "x", "--template", "/tmp/t.md"],
    cfg,
    logger,
)
check("test_deny_gh_template_file_flag", not ok and "denied" in (err or "").lower(), err or "")

# 42. deny --json combined with --jq
ok, err = module.check_command(
    ["gh", "issue", "view", "123", "--json", "body", "--jq", ".body"],
    cfg,
    logger,
)
check("test_deny_gh_json_jq_combo", not ok and "denied" in (err or "").lower(), err or "")

# ── gh/gcloud output scrubbing tests ─────────────────────────────────

scrub_patterns = cfg.get("sensitive_output_regex", [])

# 39. scrub JWT
jwt_text = "token: eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
scrubbed = module.scrub_text(jwt_text, scrub_patterns)
check("test_scrub_jwt_token", "eyJ" not in scrubbed and "[REDACTED]" in scrubbed, scrubbed)

# 40. scrub GitHub PAT
ghp_text = "auth: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijkl"
scrubbed = module.scrub_text(ghp_text, scrub_patterns)
check("test_scrub_github_pat", "ghp_" not in scrubbed and "[REDACTED]" in scrubbed, scrubbed)

# 41. scrub GitHub OAuth token
gho_text = "token=gho_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijkl"
scrubbed = module.scrub_text(gho_text, scrub_patterns)
check("test_scrub_github_oauth_token", "gho_" not in scrubbed and "[REDACTED]" in scrubbed, scrubbed)

# 42. scrub Google access token
ya29_text = "access_token: ya29.a0ARrdaM-something_long"
scrubbed = module.scrub_text(ya29_text, scrub_patterns)
check("test_scrub_google_access_token", "ya29." not in scrubbed and "[REDACTED]" in scrubbed, scrubbed)

# 43. scrub Bearer token
bearer_text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"
scrubbed = module.scrub_text(bearer_text, scrub_patterns)
check("test_scrub_bearer_token", "Bearer eyJ" not in scrubbed and "[REDACTED]" in scrubbed, scrubbed)

# 44. scrub AWS key
aws_text = "aws_key=AKIAIOSFODNN7EXAMPLE"
scrubbed = module.scrub_text(aws_text, scrub_patterns)
check("test_scrub_aws_key", "AKIAIOSFODNN7EXAMPLE" not in scrubbed and "[REDACTED]" in scrubbed, scrubbed)

# 45. scrub PEM private key (PKCS#8 format)
pem_text = "-----BEGIN PRIVATE KEY-----\nMIIE..."
scrubbed = module.scrub_text(pem_text, scrub_patterns)
check("test_scrub_pem_key", "BEGIN PRIVATE KEY" not in scrubbed and "[REDACTED]" in scrubbed, scrubbed)

# 46. scrub fine-grained GitHub PAT
github_pat_text = "token github_pat_abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"
scrubbed = module.scrub_text(github_pat_text, scrub_patterns)
check("test_scrub_github_fine_grained_pat", "github_pat_" not in scrubbed and "[REDACTED]" in scrubbed, scrubbed)

# 47. scrub OpenAI API key format
openai_key_text = "api_key=sk-1234567890ABCDEFGHIJKLMNOPQRST"
scrubbed = module.scrub_text(openai_key_text, scrub_patterns)
check("test_scrub_openai_api_key", "sk-1234567890" not in scrubbed and "[REDACTED]" in scrubbed, scrubbed)

# 48. scrub GitHub server-to-server token
ghs_text = "token=ghs_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
scrubbed = module.scrub_text(ghs_text, scrub_patterns)
check("test_scrub_github_server_token", "ghs_" not in scrubbed and "[REDACTED]" in scrubbed, scrubbed)

# 49. scrub GitHub user-to-server token
ghu_text = "token=ghu_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
scrubbed = module.scrub_text(ghu_text, scrub_patterns)
check("test_scrub_github_user_token", "ghu_" not in scrubbed and "[REDACTED]" in scrubbed, scrubbed)

# 50. scrub GitLab service account token
glsa_text = "glsa-a1b2c3d4e5f6g7h8i9j0k1l2"
scrubbed = module.scrub_text(glsa_text, scrub_patterns)
check("test_scrub_gitlab_service_account_token", "glsa-" not in scrubbed and "[REDACTED]" in scrubbed, scrubbed)

# 51. scrub DigitalOcean token
dop_text = "token=dop_v1_" + "0" * 64
scrubbed = module.scrub_text(dop_text, scrub_patterns)
check("test_scrub_digitalocean_token", "dop_v1_" not in scrubbed and "[REDACTED]" in scrubbed, scrubbed)

# 52. scrub 1Password reference
op_ref_text = "credential=op://vault/item/field"
scrubbed = module.scrub_text(op_ref_text, scrub_patterns)
check("test_scrub_1password_reference", "op://" not in scrubbed and "[REDACTED]" in scrubbed, scrubbed)

# 53. scrub AWS session token key/value
aws_session_text = "AWS_SESSION_TOKEN=IQoJb3JpZ2luX2VjEKf//////////wEaCXVzLXdlc3QtMiJIMEYCIQCl"
scrubbed = module.scrub_text(aws_session_text, scrub_patterns)
check("test_scrub_aws_session_token", "AWS_SESSION_TOKEN=" not in scrubbed and "[REDACTED]" in scrubbed, scrubbed)

# ── gh/gcloud environment tests ──────────────────────────────────────

# 54. command env passes through GH_ vars
import os as _os
_orig_gh = _os.environ.get("GH_TOKEN")
_os.environ["GH_TOKEN"] = "test-value-123"
cmd_env = module.build_command_env()
check(
    "test_command_env_passes_gh_token",
    cmd_env.get("GH_TOKEN") == "test-value-123",
    f"GH_TOKEN={cmd_env.get('GH_TOKEN')}",
)
if _orig_gh is None:
    _os.environ.pop("GH_TOKEN", None)
else:
    _os.environ["GH_TOKEN"] = _orig_gh

# 55. command env passes through GOOGLE_ vars
_orig_gac = _os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
_os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/run/secrets/sa.json"
cmd_env = module.build_command_env()
check(
    "test_command_env_passes_google_creds",
    cmd_env.get("GOOGLE_APPLICATION_CREDENTIALS") == "/run/secrets/sa.json",
    f"GAC={cmd_env.get('GOOGLE_APPLICATION_CREDENTIALS')}",
)
if _orig_gac is None:
    _os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
else:
    _os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _orig_gac

# 56. command env does NOT pass random vars
_os.environ["MY_SECRET"] = "leaked"
cmd_env = module.build_command_env()
check(
    "test_command_env_excludes_random_vars",
    "MY_SECRET" not in cmd_env,
    f"MY_SECRET={cmd_env.get('MY_SECRET', '(absent)')}",
)
_os.environ.pop("MY_SECRET", None)

# 57. unsigned requests fail HMAC verification
import time as _time
unsigned = {
    "id": "unsigned-1",
    "argv": ["gh", "pr", "list"],
    "timeout": 60,
    "timestamp": int(_time.time()),
}
check(
    "test_verify_signed_payload_rejects_unsigned_request",
    not module.verify_signed_payload(unsigned, bytes.fromhex("11" * 32)),
    str(unsigned),
)

# 58. internal status probe succeeds with valid canonical HMAC
import tempfile as _tempfile
from pathlib import Path as _Path

with _tempfile.TemporaryDirectory(prefix="void-status-test.") as _tmp:
    _resp_dir = _Path(_tmp) / "responses"
    _resp_dir.mkdir(parents=True, exist_ok=True)
    _req_path = _Path(_tmp) / "status-ok.json"
    _key = bytes.fromhex("22" * 32)
    _req = {
        "id": "status-ok",
        "argv": ["__status__"],
        "timeout": 5,
        "timestamp": int(_time.time()),
    }
    _req["hmac"] = module.compute_payload_hmac(_req, _key)
    _req_path.write_text(json.dumps(_req), encoding="utf-8")
    _orig_resp_dir = module.RESPONSES_DIR
    module.RESPONSES_DIR = _resp_dir
    try:
        module.process_request(_req_path, cfg, logger, _key)
    finally:
        module.RESPONSES_DIR = _orig_resp_dir
    _resp_path = _resp_dir / "status-ok.json"
    if _resp_path.exists():
        _resp = json.loads(_resp_path.read_text(encoding="utf-8"))
        _ok = _resp.get("exit_code") == 0 and _resp.get("error") in {None, "none"}
        _detail = str(_resp)
    else:
        _ok = False
        _detail = "status response not written"
check("test_status_probe_valid_hmac_succeeds", _ok, _detail)

# 59. status probe fails with invalid HMAC
with _tempfile.TemporaryDirectory(prefix="void-status-test.") as _tmp:
    _resp_dir = _Path(_tmp) / "responses"
    _resp_dir.mkdir(parents=True, exist_ok=True)
    _req_path = _Path(_tmp) / "status-bad.json"
    _key = bytes.fromhex("33" * 32)
    _req = {
        "id": "status-bad",
        "argv": ["__status__"],
        "timeout": 5,
        "timestamp": int(_time.time()),
        "hmac": "0" * 64,
    }
    _req_path.write_text(json.dumps(_req), encoding="utf-8")
    _orig_resp_dir = module.RESPONSES_DIR
    module.RESPONSES_DIR = _resp_dir
    try:
        module.process_request(_req_path, cfg, logger, _key)
    finally:
        module.RESPONSES_DIR = _orig_resp_dir
    _resp_path = _resp_dir / "status-bad.json"
    if _resp_path.exists():
        _resp = json.loads(_resp_path.read_text(encoding="utf-8"))
        _ok = _resp.get("error") == "auth" and _resp.get("exit_code") == 1
        _detail = str(_resp)
    else:
        _ok = False
        _detail = "status response not written"
check("test_status_probe_invalid_hmac_fails", _ok, _detail)

# ── Host proxy tests ─────────────────────────────────────────────────

# 60. host proxy allows gh
check(
    "test_host_proxy_allows_gh",
    "gh|gcloud|browse|session|omi" in host_proxy,
    "gh not in allowed tools pattern",
)

# 61. host proxy rejects unknown tools
check(
    "test_host_proxy_rejects_unknown_tools",
    "only 'gh', 'gcloud', 'browse', 'session', and 'omi' are allowed" in host_proxy,
    "missing tool rejection message",
)

# 62. host proxy builds argv JSON for gh/gcloud
check(
    "test_host_proxy_builds_argv_json",
    "argv_json" in host_proxy and "json.dumps" in host_proxy,
    "missing argv JSON construction",
)

# 63. runtime.env is not world-readable
setup_script = setup_script_path.read_text(encoding="utf-8")
check(
    "test_runtime_env_permissions_restrictive",
    bool(__import__("re").search(r'^\s*(?:run\s+)?chmod 0640 "\$RUNTIME_ENV_FILE"\s*$', setup_script, __import__("re").MULTILINE)),
    "runtime.env chmod must be 0640",
)

# 64. IPC directory perms are not world-writable
check(
    "test_ipc_directory_permissions_restrictive",
    bool(__import__("re").search(r'^\s*(?:run\s+)?chmod 0770 "\$ipc_dir"\s*$', setup_script, __import__("re").MULTILINE))
    and bool(__import__("re").search(r'^\s*(?:run\s+)?chmod 0770 "\$\{ipc_dir\}/requests" "\$\{ipc_dir\}/responses"\s*$', setup_script, __import__("re").MULTILINE)),
    "ipc dir chmod must be 0770",
)

# 65. deploy stop uses PID files instead of pkill
deploy_script = deploy_script_path.read_text(encoding="utf-8")
check(
    "test_deploy_stop_uses_pid_files_not_pkill",
    "pkill -f" not in deploy_script and "$PID_DIR/$svc.pid" in deploy_script,
    "deploy stop must manage processes via PID files only",
)

# ── Omi checksum-verified script tests ──────────────────────────────

# 66. omi is in allowed_tools
check(
    "test_omi_in_allowed_tools",
    "omi" in cfg["allowed_tools"],
    f"allowed_tools={cfg['allowed_tools']}",
)

# 67. omi requires subcommand
ok, err = module.check_command(["omi"], cfg, logger)
check(
    "test_omi_subcommand_required",
    not ok and "subcommand" in (err or "").lower(),
    err or "",
)

# Set up a temp omi binary for checksum tests
_omi_tmp = _tempfile.TemporaryDirectory(prefix="void-omi-test.")
_omi_bin = _Path(_omi_tmp.name) / "omi"
_omi_bin.write_bytes(b"#!/bin/sh\necho hello\n")
_omi_hash = hashlib.sha256(_omi_bin.read_bytes()).hexdigest()

# Temporarily override OMI_BINARY
_orig_omi_bin = module.OMI_BINARY
module.OMI_BINARY = _omi_bin

# 68. missing checksum env var → denied (fail closed)
_os.environ.pop("OMI_SHA256", None)
ok, err = module.check_command(["omi", "bucket-versioning-set"], cfg, logger)
check(
    "test_omi_missing_checksum_denied",
    not ok and "no checksum" in (err or "").lower(),
    err or "",
)

# 69. correct checksum → allowed
_os.environ["OMI_SHA256"] = _omi_hash
ok, err = module.check_command(["omi", "bucket-versioning-set"], cfg, logger)
check(
    "test_omi_correct_checksum_allowed",
    ok and err is None,
    err or "",
)

# 70. wrong checksum → denied
_os.environ["OMI_SHA256"] = "0" * 64
ok, err = module.check_command(["omi", "bucket-versioning-set"], cfg, logger)
check(
    "test_omi_wrong_checksum_denied",
    not ok and "checksum mismatch" in (err or "").lower(),
    err or "",
)

# 71. omi binary not found → denied
module.OMI_BINARY = _Path("/nonexistent/omi")
_os.environ["OMI_SHA256"] = _omi_hash
ok, err = module.check_command(["omi", "bucket-versioning-set"], cfg, logger)
check(
    "test_omi_binary_not_found_denied",
    not ok and "not installed" in (err or "").lower(),
    err or "",
)
module.OMI_BINARY = _omi_bin

# 72. host proxy includes omi in case statement
check(
    "test_host_proxy_includes_omi",
    "gh|gcloud|browse|session|omi" in host_proxy,
    "omi not in host proxy case statement",
)

# 73. omi uses 300s timeout in host proxy
check(
    "test_host_proxy_omi_timeout_300",
    "timeout=300" in host_proxy,
    "omi timeout not set to 300 in host proxy",
)

# 74. OMI_SHA256 NOT leaked to subprocess env
_os.environ["OMI_SHA256"] = _omi_hash
cmd_env = module.build_command_env()
check(
    "test_omi_checksum_not_in_command_env",
    "OMI_SHA256" not in cmd_env,
    f"OMI_SHA256={cmd_env.get('OMI_SHA256', '(absent)')}",
)
_os.environ.pop("OMI_SHA256", None)

# Restore OMI_BINARY
module.OMI_BINARY = _orig_omi_bin
_omi_tmp.cleanup()

if failures:
    print(f"\\n{len(failures)} of {len(tests)} tests failed.")
    for name, detail in failures:
        print(f"- {name}: {detail}")
    raise SystemExit(1)

print(f"\\nAll {len(tests)} tests passed.")
PY_TEST
}

cmd_clean() {
  ensure_state_dirs
  confirm_action "This will destroy the VM, LUKS data, and all runtime state. Continue?"

  log_info "Nuclear clean: removing everything..."

  # ── Step 1: Stop VM if running ─────────────────────────────────────
  local krunvm_bin
  krunvm_bin="$(command -v krunvm 2>/dev/null || echo "${HOME}/.cargo/bin/krunvm")"

  if command_exists krunvm || [[ -x "$krunvm_bin" ]]; then
    if "$krunvm_bin" list 2>/dev/null | awk '{print $1}' | grep -Fxq "$VM_NAME"; then
      log_info "Stopping and deleting VM '${VM_NAME}'..."
      "$krunvm_bin" stop "$VM_NAME" 2>/dev/null || true
      run "$krunvm_bin" delete "$VM_NAME" || true
    fi
  fi

  # Kill any residual krunvm process
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1; then
      run kill "$pid" || true
    fi
    run rm -f "$PID_FILE"
  fi

  # ── Step 2: Unmount all LUKS mounts ────────────────────────────────
  local mount_path
  for mount_path in "${RUN_ROOT}"/luks.*.*; do
    [[ -d "$mount_path" ]] || continue
    if mountpoint -q "$mount_path" 2>/dev/null; then
      log_info "Unmounting ${mount_path}..."
      umount "$mount_path" 2>/dev/null || umount -l "$mount_path" 2>/dev/null || true
    fi
    rm -rf "$mount_path" 2>/dev/null || true
  done

  # ── Step 3: Close LUKS mapper ──────────────────────────────────────
  if cryptsetup status "$LUKS_MAPPER" >/dev/null 2>&1; then
    log_info "Closing LUKS mapper ${LUKS_MAPPER}..."
    run cryptsetup close "$LUKS_MAPPER" || true
  fi

  # ── Step 4: Shred secrets ──────────────────────────────────────────
  local secrets_path
  for secrets_path in "${RUN_ROOT}"/vm-secrets.*.*/env; do
    [[ -f "$secrets_path" ]] || continue
    log_info "Shredding ${secrets_path}..."
    if command_exists shred; then
      shred -u "$secrets_path" 2>/dev/null || rm -f "$secrets_path"
    else
      rm -f "$secrets_path"
    fi
  done

  # ── Step 5: Remove all runtime temp dirs ───────────────────────────
  log_info "Removing all runtime dirs under ${RUN_ROOT}..."
  run rm -rf "${RUN_ROOT}"/provision.*.* "${RUN_ROOT}"/vm-secrets.*.* \
             "${RUN_ROOT}"/vm-ipc.*.* "${RUN_ROOT}"/vm-boot.*.* \
             "${RUN_ROOT}"/luks.*.*

  # ── Step 6: Remove void proxy ──────────────────────────────────────
  if [[ -f /usr/local/bin/void ]]; then
    log_info "Removing /usr/local/bin/void..."
    run rm -f /usr/local/bin/void
  fi

  # ── Step 7: Remove build artifacts (unless --keep-build) ───────────
  if ! ((KEEP_BUILD)); then
    local libkrunfw_dir="${BUILD_ROOT}/libkrunfw"
    if [[ -d "$libkrunfw_dir" ]]; then
      log_info "Cleaning libkrunfw..."
      make -C "$libkrunfw_dir" clean 2>/dev/null || true
      local kdir
      for kdir in "${libkrunfw_dir}"/linux-*/; do
        [[ -d "$kdir" ]] && run rm -rf "$kdir"
      done
    fi

    local libkrun_dir="${BUILD_ROOT}/libkrun"
    if [[ -d "$libkrun_dir" ]]; then
      log_info "Cleaning libkrun..."
      make -C "$libkrun_dir" clean 2>/dev/null || true
    fi
  else
    log_info "Keeping build artifacts (--keep-build)."
  fi

  # ── Step 8: Remove VM state (LUKS image, keys, logs) ───────────────
  if [[ -f "${VM_STATE_DIR}/luks.key" ]]; then
    log_info "Shredding LUKS key..."
    if command_exists shred; then
      shred -u "${VM_STATE_DIR}/luks.key" 2>/dev/null || rm -f "${VM_STATE_DIR}/luks.key"
    else
      rm -f "${VM_STATE_DIR}/luks.key"
    fi
  fi

  if [[ -f "$LUKS_IMAGE" ]]; then
    log_info "Removing LUKS image..."
    run rm -f "$LUKS_IMAGE"
  fi

  run rm -f "$RUNTIME_ENV_FILE"

  if [[ -d "$VM_STATE_DIR" ]]; then
    run rm -rf "$VM_STATE_DIR"
  fi

  log_info "Clean complete. No trace left."
}

cmd_all() {
  cmd_deps
  cmd_build
  cmd_create
  cmd_configure
}

cmd_fresh() {
  cmd_clean
  cmd_deps
  cmd_build
  cmd_create
  cmd_configure
  cmd_start
}

cmd_restart() {
  cmd_stop
  cmd_start
}

parse_args() {
  local arg
  for arg in "$@"; do
    case "$arg" in
      -h|--help)
        usage
        exit 0
        ;;
      --version)
        echo "$VERSION"
        exit 0
        ;;
    esac
  done

  local subcommand=""
  local extras=()

  while (($#)); do
    case "$1" in
      --dry-run)
        DRY_RUN=1
        shift
        ;;
      --verbose)
        VERBOSE=1
        shift
        ;;
      --no-color)
        USE_COLOR=0
        shift
        ;;
      --yes)
        ASSUME_YES=1
        shift
        ;;
      --no-secrets)
        NO_SECRETS=1
        shift
        ;;
      --keep-build)
        KEEP_BUILD=1
        shift
        ;;
      --vm-name)
        [[ $# -ge 2 ]] || die_usage "--vm-name requires a value"
        VM_NAME="$2"
        shift 2
        ;;
      --vm-name=*)
        VM_NAME="${1#*=}"
        shift
        ;;
      --cpus)
        [[ $# -ge 2 ]] || die_usage "--cpus requires a value"
        VM_CPUS="$2"
        shift 2
        ;;
      --cpus=*)
        VM_CPUS="${1#*=}"
        shift
        ;;
      --mem)
        [[ $# -ge 2 ]] || die_usage "--mem requires a value"
        VM_MEM="$2"
        shift 2
        ;;
      --mem=*)
        VM_MEM="${1#*=}"
        shift
        ;;
      --secrets-size)
        [[ $# -ge 2 ]] || die_usage "--secrets-size requires a value"
        SECRETS_SIZE="$2"
        shift 2
        ;;
      --secrets-size=*)
        SECRETS_SIZE="${1#*=}"
        shift
        ;;
      deps|build|create|configure|start|stop|restart|status|test|clean|all|fresh|grant)
        if [[ -z "$subcommand" ]]; then
          subcommand="$1"
        else
          extras+=("$1")
        fi
        shift
        ;;
      --)
        shift
        while (($#)); do
          extras+=("$1")
          shift
        done
        ;;
      -* )
        die_usage "Unknown flag: $1"
        ;;
      *)
        if [[ -z "$subcommand" ]]; then
          subcommand="$1"
        else
          extras+=("$1")
        fi
        shift
        ;;
    esac
  done

  ensure_numeric "--cpus" "$VM_CPUS"
  ensure_numeric "--mem" "$VM_MEM"
  ensure_numeric "--secrets-size" "$SECRETS_SIZE"

  if [[ -z "$VM_NAME" ]]; then
    die_usage "--vm-name cannot be empty"
  fi

  refresh_paths

  if [[ -z "$subcommand" ]]; then
    die_usage "Missing subcommand."
  fi

  if ((${#extras[@]} > 0)) && [[ "$subcommand" != "grant" ]]; then
    die_usage "Unexpected argument(s): ${extras[*]}"
  fi

  PARSED_SUBCOMMAND="$subcommand"
  PARSED_EXTRAS=("${extras[@]+"${extras[@]}"}")
}

main() {
  # Handle --help/--version before full parse
  for arg in "$@"; do
    case "$arg" in
      -h|--help) usage; exit 0 ;;
      --version) echo "$VERSION"; exit 0 ;;
    esac
  done

  parse_args "$@"
  local subcommand="$PARSED_SUBCOMMAND"

  case "$subcommand" in
    deps)
      cmd_deps
      ;;
    build)
      cmd_build
      ;;
    create)
      cmd_create
      ;;
    configure)
      cmd_configure
      ;;
    start)
      cmd_start
      ;;
    stop)
      cmd_stop
      ;;
    status)
      cmd_status
      ;;
    test)
      cmd_test
      ;;
    clean)
      cmd_clean
      ;;
    all)
      cmd_all
      ;;
    fresh)
      cmd_fresh
      ;;
    restart)
      cmd_restart
      ;;
    grant)
      EXTRAS=("${PARSED_EXTRAS[@]+"${PARSED_EXTRAS[@]}"}")
      cmd_grant
      ;;
    *)
      die_usage "Unknown subcommand: ${subcommand}"
      ;;
  esac
}

main "$@"
