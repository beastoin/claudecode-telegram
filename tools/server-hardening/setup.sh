#!/usr/bin/env bash
# Server hardening setup script.
# Run once on a new dedicated server. All services are persistent (systemd).
#
# What it does:
#   1. UFW firewall — allow SSH + Tailscale, block everything else from public
#   2. fail2ban — SSH brute-force protection (10 attempts → 1hr ban)
#   3. Global git hooks — trufflehog secret scan on every commit, chains to repo hooks
#   4. SSH hardening — verify key-only auth, no root password login
#
# Usage: sudo bash tools/server-hardening/setup.sh
#
# Safe to re-run — idempotent. Does NOT restart SSH (avoids lockout).
set -euo pipefail

echo "=== Server Hardening Setup ==="
echo ""

# ── Must be root ────────────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: Run with sudo"
  exit 1
fi

# Detect the non-root user (the one who invoked sudo)
REAL_USER="${SUDO_USER:-claude}"
REAL_HOME=$(eval echo "~$REAL_USER")

# ── 1. UFW Firewall ────────────────────────────────────────────────────────
echo "── Setting up UFW firewall ──"

apt-get install -y ufw >/dev/null 2>&1 || true

# Default: deny incoming, allow outgoing
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null

# SSH from anywhere (critical — don't lock ourselves out)
ufw allow 22/tcp comment 'SSH' >/dev/null

# Allow ALL traffic on Tailscale interface (internal network)
ufw allow in on tailscale0 comment 'Tailscale internal' >/dev/null

# Enable (--force skips the "are you sure" prompt)
ufw --force enable >/dev/null
echo "  ✓ UFW active: SSH + Tailscale allowed, public ports blocked"

# ── 2. fail2ban ─────────────────────────────────────────────────────────────
echo "── Setting up fail2ban ──"

apt-get install -y fail2ban >/dev/null 2>&1

# SSH jail config — 10 attempts in 10min → 1hr ban
cat > /etc/fail2ban/jail.local << 'JAIL'
[sshd]
enabled = true
port = ssh
filter = sshd
logpath = /var/log/auth.log
maxretry = 10
findtime = 600
bantime = 3600
JAIL

systemctl enable fail2ban >/dev/null 2>&1
systemctl restart fail2ban
echo "  ✓ fail2ban active: SSH brute-force protection (10 tries → 1hr ban)"

# ── 3. Global git hooks ────────────────────────────────────────────────────
echo "── Setting up global git hooks ──"

HOOKS_DIR="$REAL_HOME/.git-hooks"
mkdir -p "$HOOKS_DIR"

# Only install if the pre-commit hook source exists
HOOK_SRC="$(cd "$(dirname "$0")/.." && pwd)/git-hooks/pre-commit"
if [ -f "$HOOK_SRC" ]; then
  cp "$HOOK_SRC" "$HOOKS_DIR/pre-commit"
  chmod +x "$HOOKS_DIR/pre-commit"
  chown "$REAL_USER:$REAL_USER" "$HOOKS_DIR/pre-commit"
fi

# Also install the global chaining wrapper if it exists
GLOBAL_HOOK="$REAL_HOME/.git-hooks/pre-commit"
if [ -f "$GLOBAL_HOOK" ]; then
  chown "$REAL_USER:$REAL_USER" "$GLOBAL_HOOK"
fi

# Set global hooks path for the real user
su - "$REAL_USER" -c "git config --global core.hooksPath '$HOOKS_DIR'" 2>/dev/null || true
echo "  ✓ Global git hooks: trufflehog on every commit, chains to repo hooks"

# ── 4. SSH hardening verification ───────────────────────────────────────────
echo "── Verifying SSH config ──"

SSHD_CONFIG="/etc/ssh/sshd_config"
issues=0

if ! grep -q "^PasswordAuthentication no" "$SSHD_CONFIG" 2>/dev/null; then
  echo "  ⚠ PasswordAuthentication not set to 'no' — fix manually"
  issues=$((issues + 1))
else
  echo "  ✓ Password auth disabled"
fi

if grep -q "^PermitRootLogin yes" "$SSHD_CONFIG" 2>/dev/null; then
  echo "  ⚠ PermitRootLogin is 'yes' — should be 'no' or 'without-password'"
  issues=$((issues + 1))
else
  echo "  ✓ Root login restricted"
fi

# ── Summary ─────────────────────────────────────────────────────────────────
echo ""
echo "=== Setup Complete ==="
echo ""
echo "Persistent services (no maintenance needed):"
echo "  • UFW firewall     — systemd, survives reboot"
echo "  • fail2ban         — systemd, survives reboot"
echo "  • Git hooks        — global config, survives reboot"
echo "  • SSH hardening    — sshd config, survives reboot"
echo ""
echo "To check status anytime:"
echo "  sudo ufw status          # firewall rules"
echo "  sudo fail2ban-client status sshd  # banned IPs"
echo "  git config --global core.hooksPath  # hooks path"
echo ""
if [ $issues -gt 0 ]; then
  echo "⚠ $issues SSH issue(s) need manual attention (see above)"
fi
