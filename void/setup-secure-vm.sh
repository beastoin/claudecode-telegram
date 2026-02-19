#!/bin/bash
set -euo pipefail
cd /tmp 2>/dev/null || cd /

VERSION="1.0.0"

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
import re
import signal
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
POLL_INTERVAL = 0.1  # seconds
REQUEST_MAX_AGE_SECONDS = 30

DEFAULT_CONFIG = {
    "allowed_tools": ["gh", "gcloud"],
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
}

MAX_BROWSER_TIMEOUT = 45
LONG_BROWSER_TIMEOUT = 25
SHORT_BROWSER_TIMEOUT = 10
BROWSER_LONG_ACTIONS = {
    ("open",),
    ("snapshot",),
    ("screenshot",),
    ("wait",),
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
    ("auth", "activate-service-account"),
}

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


def build_browser_env():
    """Sanitized env for browser execution with no credential passthrough."""
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/var/lib/void",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "XDG_CACHE_HOME": "/var/lib/void/.cache",
        "XDG_CONFIG_HOME": "/var/lib/void/.config",
        "XDG_DATA_HOME": "/var/lib/void/.local/share",
        "CHROMIUM_FLAGS": "--disable-webrtc",
        "PLAYWRIGHT_CHROMIUM_ARGS": "--disable-webrtc",
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
    if action_tuple in {("session", "list"), ("session", "close")}:
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


def check_command(argv, cfg, logger):
    """Validate command against ACL. Returns (ok, error_message)."""
    if not argv:
        return False, "Empty command"

    tool = os.path.basename(argv[0])
    allowed = set(cfg.get("allowed_tools", []))
    if tool not in allowed:
        logger.info("DENIED tool=%s reason=tool_not_allowed", tool)
        return False, f"Tool not allowed: {tool}. Allowed: {', '.join(sorted(allowed))}"

    if tool == "gh":
        sub_ok, sub_err = check_gh_subcommand(argv)
    elif tool == "gcloud":
        sub_ok, sub_err = check_gcloud_subcommand(argv)
    else:
        sub_ok, sub_err = False, f"Unsupported tool: {tool}"

    if not sub_ok:
        logger.info("DENIED tool=%s reason=subcommand_not_allowed detail=%s", tool, sub_err)
        return False, "Command denied by subcommand allowlist"

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
                        env = build_browser_env()
                        cgroup_path = cfg.get("browser_cgroup", "/sys/fs/cgroup/void")
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
                                if action_tuple == ("session", "close"):
                                    active_browser_sessions.discard(browser_session_close_target(session, params))
                                elif action_tuple != ("session", "list"):
                                    active_browser_sessions.add(session)
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
                    env = build_command_env()
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
YAML_PROXY
  chmod 0640 "$target"
}

write_firewall_script() {
  local target="$1"
  cat >"$target" <<'FW_SCRIPT'
#!/bin/bash
set -euo pipefail

# Debian bookworm defaults to nftables backend. If the kernel lacks
# nf_tables support (e.g., libkrun microVM), fall back to iptables-legacy.
# If neither works (no netfilter in kernel), warn and skip.
IPT=""
for candidate in iptables iptables-legacy; do
  if command -v "$candidate" >/dev/null 2>&1 && $candidate -L -n >/dev/null 2>&1; then
    IPT="$candidate"
    break
  fi
done

if [[ -z "$IPT" ]]; then
  echo "WARNING: Kernel has no netfilter support. Firewall rules NOT applied." >&2
  echo "WARNING: Network isolation relies on the host firewall instead." >&2
  exit 0
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
BROWSER_UID="$(id -u void 2>/dev/null || true)"
if [[ -n "$BROWSER_UID" ]]; then
  $IPT -N VOID_BROWSER_EGRESS 2>/dev/null || true
  $IPT -F VOID_BROWSER_EGRESS
  $IPT -A OUTPUT -m owner --uid-owner "$BROWSER_UID" -j VOID_BROWSER_EGRESS
  $IPT -A VOID_BROWSER_EGRESS -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

  for cidr in \
    0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 \
    169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 \
    224.0.0.0/4 240.0.0.0/4; do
    $IPT -A VOID_BROWSER_EGRESS -d "$cidr" -j REJECT
  done
  $IPT -A VOID_BROWSER_EGRESS -d 169.254.169.254/32 -j REJECT

  while read -r ns; do
    [[ -z "$ns" ]] && continue
    [[ "$ns" == *:* ]] && continue
    $IPT -A VOID_BROWSER_EGRESS -p udp -d "$ns" --dport 53 -j ACCEPT
    $IPT -A VOID_BROWSER_EGRESS -p tcp -d "$ns" --dport 53 -j ACCEPT
  done < <(awk '/^nameserver/{print $2}' /etc/resolv.conf)

  $IPT -A VOID_BROWSER_EGRESS -p tcp --dport 443 -j ACCEPT
  $IPT -A VOID_BROWSER_EGRESS -j REJECT
fi

$IPT -A OUTPUT -p udp --dport 53 -j ACCEPT
$IPT -A OUTPUT -p tcp --dport 53 -j ACCEPT
$IPT -A OUTPUT -p tcp --dport 443 -j ACCEPT
$IPT -A OUTPUT -p tcp --dport 80 -j ACCEPT
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
  python3 python3-yaml sudo jq

install -d -m 0755 /usr/local/bin
install -d -m 0750 /etc/credential-proxy

install -m 0755 /provision/command-proxy-daemon.py /usr/local/bin/command-proxy-daemon
install -m 0640 /provision/config.yaml /etc/credential-proxy/config.yaml
install -m 0755 /provision/firewall.sh /root/firewall.sh

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
  write_proxy_config "${bundle_dir}/config.yaml"
  write_firewall_script "${bundle_dir}/firewall.sh"
  write_guest_provision_script "${bundle_dir}/provision.sh"

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
# Usage: void <gh|gcloud|browse> [args...]

RUNTIME_ENV="${runtime_env}"

usage() {
  echo "Usage: void <gh|gcloud|browse> [args...]" >&2
  echo "" >&2
  echo "Executes gh, gcloud, or browse commands securely inside the VM." >&2
  echo "Credentials never leave the VM boundary." >&2
  exit 2
}

if [[ \$# -lt 1 ]]; then
  usage
fi

tool="\$1"
case "\$tool" in
  gh|gcloud|browse) ;;
  -h|--help) usage ;;
  *) echo "Error: only 'gh', 'gcloud', and 'browse' are allowed (got: \$tool)" >&2; exit 1 ;;
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
)
else
  # Build JSON request for gh/gcloud passthrough mode.
  argv_json=\$(printf '%s\n' "\$@" | python3 -c '
import json, sys
print(json.dumps([line.rstrip("\n") for line in sys.stdin]))
')

  timeout=60
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

request=\$(printf '%s' "\$request" | python3 - "\$IPC_KEY" <<'PY_SIGN_REQUEST'
import hashlib
import hmac
import json
import sys

key_hex = sys.argv[1].strip()
try:
    key = bytes.fromhex(key_hex)
except ValueError:
    print("Error: invalid IPC key in runtime state", file=sys.stderr)
    raise SystemExit(1)

req = json.load(sys.stdin)
payload = dict(req)
payload.pop("hmac", None)
canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
req["hmac"] = hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
print(json.dumps(req))
PY_SIGN_REQUEST
)

# Atomic write: .tmp then rename to .json
echo "\$request" > "\${IPC_DIR}/requests/\${req_id}.tmp"
mv "\${IPC_DIR}/requests/\${req_id}.tmp" "\${IPC_DIR}/requests/\${req_id}.json"

# Poll for response (timeout 90s to allow for command timeout + overhead)
poll_timeout=90
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
SECRETS_DIR=${secrets_dir}
LUKS_MOUNT=${luks_mount}
LUKS_MAPPER=${mapper}
VM_LOG=${log_file}
IPC_DIR=${ipc_dir}
IPC_KEY=${ipc_key}
EOF_RUNTIME
  chmod 0644 "$RUNTIME_ENV_FILE"
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
  local luks_mount=""

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

  fi

  # Create shared IPC directory for host↔guest communication.
  # NOT tracked for cleanup — must persist while VM is running.
  # Cleaned up by cmd_stop() via runtime.env IPC_DIR.
  local ipc_dir
  ipc_dir="$(mktemp -d "${mktemp_base}/vm-ipc.${VM_NAME}.XXXXXX")"
  run mkdir -p "${ipc_dir}/requests" "${ipc_dir}/responses"
  run chmod 0777 "$ipc_dir"
  run chmod 0777 "${ipc_dir}/requests" "${ipc_dir}/responses"

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
    vol_args+=(--volume "${secrets_dir}:/secrets_in" --volume "${luks_mount}:/secrets")
  fi
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
  log_info "Use 'void gh ...', 'void gcloud ...', or 'void browse ...' to run commands."
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

    # runtime.env must be readable
    if [[ -f "$RUNTIME_ENV_FILE" ]]; then
      run chmod 0644 "$RUNTIME_ENV_FILE"
    fi

    # IPC directory must be read/writable
    if [[ -n "$ipc_dir" && -d "$ipc_dir" ]]; then
      run chmod 0777 "$ipc_dir"
      run chmod 0777 "${ipc_dir}/requests" "${ipc_dir}/responses"
    else
      # Scan for any IPC dirs
      local d
      for d in "${RUN_ROOT}"/vm-ipc.${VM_NAME}.*; do
        [[ -d "$d" ]] || continue
        run chmod 0777 "$d" "$d/requests" "$d/responses"
      done
    fi

    log_info "User '${username}' can now run: void gh ... / void gcloud ... / void browse ..."
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

  echo "version=${VERSION}"
  echo "vm_name=${VM_NAME}"
  echo "vm_exists=${vm_present}"
  echo "vm_running=${vm_running}"
  echo "luks_image=${LUKS_IMAGE}"
  echo "luks_mapper=${LUKS_MAPPER}"
  echo "luks_mapper_open=${mapper_open}"
  echo "ipc_active=${ipc_active}"
  echo "daemon_alive=${daemon_alive}"
  echo "ipc_dir=${ipc_dir:-none}"
  echo "runtime_state_file=${RUNTIME_ENV_FILE}"
  echo "vm_log_file=${VM_LOG_FILE}"

  if command_exists krunvm || [[ -x /root/.cargo/bin/krunvm ]]; then
    local krunvm_bin
    krunvm_bin="$(command -v krunvm 2>/dev/null || true)"
    [[ -n "$krunvm_bin" ]] || krunvm_bin="/root/.cargo/bin/krunvm"
    "$krunvm_bin" list || true
  fi
}

cmd_test() {
  local test_root
  test_root="$(mktemp -d /tmp/setup-secure-vm-test.XXXXXX)"
  track_tmp_path "$test_root"

  local daemon_py="${test_root}/command-proxy-daemon.py"
  local firewall_sh="${test_root}/firewall.sh"
  local host_proxy="${test_root}/void"
  local runtime_env="${test_root}/runtime.env"

  write_proxy_daemon "$daemon_py"
  write_firewall_script "$firewall_sh"
  write_host_proxy_script "$host_proxy" "$runtime_env"

  python3 - "$daemon_py" "$firewall_sh" "$host_proxy" <<'PY_TEST'
import importlib.util
import logging
import pathlib
import sys

daemon_path = pathlib.Path(sys.argv[1])
firewall_path = pathlib.Path(sys.argv[2])
host_proxy_path = pathlib.Path(sys.argv[3])

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
    and env.get("HOME") == "/var/lib/void"
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

# 18. scrub query tokens
scrubbed = module.scrub_browser_text("https://example.com/?token=secret123&x=1")
check(
    "test_browser_scrubs_query_tokens",
    "secret123" not in scrubbed and "token=[REDACTED]" in scrubbed,
    scrubbed,
)

# 19. session limit enforced
module.active_browser_sessions.clear()
module.active_browser_sessions.add("s1")
module.active_browser_sessions.add("s2")
limit_cfg = dict(cfg)
limit_cfg["max_browser_sessions"] = 2
allowed, msg = module.browser_session_allowed(("open",), "s3", limit_cfg)
check("test_browser_session_limit_enforced", not allowed, msg or "")

# 20. timeout enforced
slow_timeout = module.browser_timeout_for_action("open", 999)
fast_timeout = module.browser_timeout_for_action("click", 999)
check(
    "test_browser_timeout_enforced",
    slow_timeout == 25 and fast_timeout == 10,
    f"slow={slow_timeout} fast={fast_timeout}",
)

# Host proxy regression check for browse wiring
host_proxy = host_proxy_path.read_text(encoding="utf-8")
check(
    "test_host_proxy_allows_browse_tool",
    "gh|gcloud|browse" in host_proxy and "\"tool\": \"browser\"" in host_proxy,
    "browse tool mapping missing",
)

# ── gh/gcloud ACL tests ──────────────────────────────────────────────

# 22. gh allowed
ok, err = module.check_command(["gh", "pr", "list"], cfg, logger)
check("test_gh_pr_list_allowed", ok, err or "")

# 23. gcloud allowed
ok, err = module.check_command(["gcloud", "projects", "list"], cfg, logger)
check("test_gcloud_projects_list_allowed", ok, err or "")

# 24. deny tool not in allowed list
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

# ── gh/gcloud environment tests ──────────────────────────────────────

# 48. command env passes through GH_ vars
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

# 49. command env passes through GOOGLE_ vars
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

# 50. command env does NOT pass random vars
_os.environ["MY_SECRET"] = "leaked"
cmd_env = module.build_command_env()
check(
    "test_command_env_excludes_random_vars",
    "MY_SECRET" not in cmd_env,
    f"MY_SECRET={cmd_env.get('MY_SECRET', '(absent)')}",
)
_os.environ.pop("MY_SECRET", None)

# 51. unsigned requests fail HMAC verification
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

# ── Host proxy tests ─────────────────────────────────────────────────

# 52. host proxy allows gh
check(
    "test_host_proxy_allows_gh",
    "gh|gcloud|browse" in host_proxy,
    "gh not in allowed tools pattern",
)

# 53. host proxy rejects unknown tools
check(
    "test_host_proxy_rejects_unknown_tools",
    "only 'gh', 'gcloud', and 'browse' are allowed" in host_proxy,
    "missing tool rejection message",
)

# 54. host proxy builds argv JSON for gh/gcloud
check(
    "test_host_proxy_builds_argv_json",
    "argv_json" in host_proxy and "json.dumps" in host_proxy,
    "missing argv JSON construction",
)

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
