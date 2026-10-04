#!/usr/bin/env bash
#
# Server hardening setup.
#
# This script makes a new server safe. It does these four things:
#   1. Firewall  — Allow SSH and Tailscale. Block all other public ports.
#   2. fail2ban  — Block IPs that try too many SSH logins.
#   3. Git hooks — Scan every commit for leaked secrets.
#   4. SSH check — Make sure only key login works. No passwords.
#
# You can run this script more than once. It will not break anything.
# It does NOT restart SSH. You will not lose your connection.
#
# Usage:
#   sudo bash refs/server-hardening/setup.sh

set -euo pipefail

# ── Constants ──────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly SCRIPT_DIR
readonly SSHD_CONFIG="/etc/ssh/sshd_config"
readonly FAIL2BAN_MAX_RETRY=10
readonly FAIL2BAN_FIND_TIME=600   # 10 minutes
readonly FAIL2BAN_BAN_TIME=3600   # 1 hour

# ── Helpers ────────────────────────────────────────────────────────────────

#######################################
# Print an error message to stderr.
# Arguments:
#   Error message string.
#######################################
err() {
  echo "[ERROR] $*" >&2
}

#######################################
# Print a success line.
# Arguments:
#   Message string.
#######################################
ok() {
  echo "  done: $*"
}

#######################################
# Print a warning line.
# Arguments:
#   Message string.
#######################################
warn() {
  echo "  warning: $*"
}

# ── Step functions ─────────────────────────────────────────────────────────

#######################################
# Set up UFW firewall.
# Allow SSH and Tailscale. Block all other ports from public.
#######################################
setup_firewall() {
  echo "── Step 1: Firewall ──"

  apt-get install -y ufw > /dev/null 2>&1 || true

  # Block all incoming traffic by default. Allow all outgoing.
  ufw default deny incoming > /dev/null
  ufw default allow outgoing > /dev/null

  # Always allow SSH. This prevents lockout.
  ufw allow 22/tcp comment 'SSH' > /dev/null

  # Allow all traffic on the Tailscale interface (internal network).
  ufw allow in on tailscale0 comment 'Tailscale internal' > /dev/null

  # Turn on the firewall. --force skips the yes/no prompt.
  ufw --force enable > /dev/null

  ok "Firewall active. SSH and Tailscale allowed. Public ports blocked."
}

#######################################
# Set up fail2ban for SSH.
# Block an IP after too many failed login attempts.
#######################################
setup_fail2ban() {
  echo "── Step 2: fail2ban ──"

  apt-get install -y fail2ban > /dev/null 2>&1

  # Write the SSH jail config.
  # Block an IP for 1 hour after 10 failed attempts in 10 minutes.
  cat > /etc/fail2ban/jail.local << JAIL
[sshd]
enabled = true
port = ssh
filter = sshd
logpath = /var/log/auth.log
maxretry = ${FAIL2BAN_MAX_RETRY}
findtime = ${FAIL2BAN_FIND_TIME}
bantime = ${FAIL2BAN_BAN_TIME}
JAIL

  systemctl enable fail2ban > /dev/null 2>&1
  systemctl restart fail2ban

  ok "fail2ban active. ${FAIL2BAN_MAX_RETRY} failed logins = 1 hour ban."
}

#######################################
# Set up global git hooks.
# Install the pre-commit hook that scans for secrets.
# The hook also runs any repo-level hooks after it finishes.
# Arguments:
#   None. Uses REAL_USER and REAL_HOME from outer scope.
#######################################
setup_git_hooks() {
  local hooks_dir
  local hook_src

  echo "── Step 3: Git hooks ──"

  hooks_dir="${REAL_HOME}/.git-hooks"
  mkdir -p "${hooks_dir}"

  # Find the pre-commit hook source file.
  hook_src="${SCRIPT_DIR}/../git-hooks/pre-commit"

  if [[ ! -f "${hook_src}" ]]; then
    warn "Pre-commit hook not found at ${hook_src}. Skipped."
    return
  fi

  # Copy the hook and set the correct owner.
  cp "${hook_src}" "${hooks_dir}/pre-commit"
  chmod +x "${hooks_dir}/pre-commit"
  chown "${REAL_USER}:${REAL_USER}" "${hooks_dir}/pre-commit"

  # Tell git to use this hooks directory for all repos.
  su - "${REAL_USER}" -c \
    "git config --global core.hooksPath '${hooks_dir}'" 2> /dev/null || true

  ok "Global git hooks installed. Secret scan runs on every commit."
}

#######################################
# Check SSH config for common problems.
# This function only reads the config. It does not change it.
# Returns:
#   Number of issues found (via the issues variable).
#######################################
check_ssh() {
  local issues=0

  echo "── Step 4: SSH check ──"

  # Password login must be off. Only key login should work.
  if ! grep -q "^PasswordAuthentication no" "${SSHD_CONFIG}" 2> /dev/null; then
    warn "PasswordAuthentication is not 'no'. Fix this by hand."
    issues=$((issues + 1))
  else
    ok "Password login is off."
  fi

  # Root must not log in with a password.
  if grep -q "^PermitRootLogin yes" "${SSHD_CONFIG}" 2> /dev/null; then
    warn "PermitRootLogin is 'yes'. Set it to 'no' or 'without-password'."
    issues=$((issues + 1))
  else
    ok "Root login is restricted."
  fi

  return "${issues}"
}

#######################################
# Print a summary of what was set up.
# Arguments:
#   Number of SSH issues found.
#######################################
print_summary() {
  local ssh_issues="$1"

  echo ""
  echo "=== Setup complete ==="
  echo ""
  echo "All services survive a reboot:"
  echo "  - Firewall     (UFW, systemd)"
  echo "  - fail2ban     (systemd)"
  echo "  - Git hooks    (git global config)"
  echo "  - SSH config   (sshd)"
  echo ""
  echo "Check status:"
  echo "  sudo ufw status                    # firewall rules"
  echo "  sudo fail2ban-client status sshd   # banned IPs"
  echo "  git config --global core.hooksPath # hooks path"
  echo ""

  if [[ "${ssh_issues}" -gt 0 ]]; then
    warn "${ssh_issues} SSH problem(s) need a manual fix. See above."
  fi
}

# ── Main ───────────────────────────────────────────────────────────────────

#######################################
# Main entry point.
# Runs all four hardening steps in order.
#######################################
main() {
  # This script must run as root.
  if [[ "$(id -u)" -ne 0 ]]; then
    err "This script needs root. Run: sudo bash $0"
    exit 1
  fi

  # Find the real user (the one who ran sudo).
  readonly REAL_USER="${SUDO_USER:-claude}"
  readonly REAL_HOME
  REAL_HOME="$(eval echo "~${REAL_USER}")"

  echo "=== Server Hardening ==="
  echo ""

  setup_firewall
  setup_fail2ban
  setup_git_hooks

  local ssh_issues=0
  check_ssh || ssh_issues=$?

  print_summary "${ssh_issues}"
}

main "$@"
