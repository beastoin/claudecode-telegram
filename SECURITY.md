# Security Hardening
## Security Hardening (Optional)

The bridge includes built-in security defaults. These optional steps add defense-in-depth for production deployments.

### Already enabled by default

- **Default localhost binding**: Bridge binds to `127.0.0.1` by default. Only cloudflared (on the same machine) can reach it. When `BRIDGE_PUBLIC_URL` is set (Tailscale deployments), the bridge auto-binds to `0.0.0.0`. Override manually with `BRIDGE_BIND`.
- **Webhook secret**: Set `TELEGRAM_WEBHOOK_SECRET` to verify incoming Telegram webhooks on `POST /`. This protects only the Telegram webhook endpoint. Other bridge endpoints rely on bind/network isolation.
- **Token isolation**: Workers never see `TELEGRAM_BOT_TOKEN`. Responses flow through the bridge. The bridge holds the token.

### Recommended system-level hardening

#### 1. Run bridge under a dedicated Unix user

This prevents workers from reading bridge environment (including the bot token) via `/proc`.

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin bridge-user
```

Run the bridge as `bridge-user` and workers as your normal user. Workers cannot read `/proc/<bridge-pid>/environ`.

#### 2. Hide process information between users

This prevents any user from listing other users' processes and reading their environment.

```bash
sudo mount -o remount,hidepid=2 /proc
```

To make it permanent, add to `/etc/fstab`:

```text
proc /proc proc defaults,hidepid=2 0 0
```

#### 3. Set ADMIN_CHAT_ID explicitly

This prevents the first random person who finds your bot from becoming admin.

```bash
export ADMIN_CHAT_ID="123456789"
```

Get your chat ID by messaging [@userinfobot](https://t.me/userinfobot) on Telegram.

#### 4. Enable Telegram webhook verification

This ensures only Telegram (not an attacker who discovers your tunnel URL) can send webhooks.

```bash
export TELEGRAM_WEBHOOK_SECRET="$(openssl rand -hex 16)"
```

The bridge passes this to Telegram during webhook setup. It verifies the secret on every incoming request.

