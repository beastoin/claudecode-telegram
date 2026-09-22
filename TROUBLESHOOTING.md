# Troubleshooting Guide
## Full Troubleshooting Guide

### Quick top-3 checks

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Bot does not respond | Bridge down or wrong admin | Run `./bridge.sh status`. Restart if needed. |
| `👀` but no reply | Worker busy or stuck | Run `/progress`, then `/restart`. |
| `No one assigned` | No focused worker | Run `/team`, then `/focus <name>`. |

### `OAuth error: Invalid code`

Why it happens:
- Claude CLI was not fully initialized with API-key flow. Or `~/.claude/.credentials.json` was created manually.

Fix:

```bash
rm -f ~/.claude/.credentials.json
```

```bash
claude --dangerously-skip-permissions
```

Complete wizard and exit with `/exit`.

### `ModuleNotFoundError: No module named ...`

Why it happens:
- Python 3 is missing or not in PATH.

Fix (macOS):

```bash
brew install python
```

Fix (Ubuntu):

```bash
sudo apt install -y python3
```

Verify:

```bash
python3 --version
```

### `tmux: command not found`

Why it happens:
- tmux is not installed. Workers cannot stay alive without it.

Fix (macOS):

```bash
brew install tmux
```

Fix (Ubuntu):

```bash
sudo apt install -y tmux
```

Verify:

```bash
tmux -V
```

### Worker starts but never responds

Why it happens:
- `ANTHROPIC_API_KEY` is not available inside tmux-created sessions.

Fix:

```bash
echo "$ANTHROPIC_API_KEY"
```

If blank, add the key to your shell startup file (Part 3). Open a new terminal. Then restart the bridge:

```bash
./bridge.sh restart
```

### Bot does not react with `👀`

Why it happens:
- Invalid or missing `TELEGRAM_BOT_TOKEN`. Or webhook misconfiguration. Or bridge is down.

Fix:

```bash
echo "$TELEGRAM_BOT_TOKEN"
```

```bash
./bridge.sh status
```

```bash
./bridge.sh run
```

### `Connection refused`

Why it happens:
- Bridge process is not listening on the expected port.

Fix:

```bash
./bridge.sh run
```

Then verify:

```bash
./bridge.sh status
```

### Tunnel issues (cloudflared)

Common errors:
- `cloudflared: command not found`
- Webhook does not update after startup.

Fix:

```bash
cloudflared --version
```

If command not found, install cloudflared (Part 2). Then rerun the bridge:

```bash
./bridge.sh run
```

### Webhook mismatch or stale webhook

Why it happens:
- Tunnel URL changed but Telegram still points to the old URL.

Fix:

```bash
./bridge.sh webhook info
```

```bash
./bridge.sh webhook delete
```

```bash
./bridge.sh run
```

### Wrong admin account controls the bot

Why it happens:
- The first user to message becomes admin when `ADMIN_CHAT_ID` is not set. On restart, the bridge restores the admin from the persisted `last_chat_id` file.

Fix:

```bash
./bridge.sh clean
```

Then restart. Send the first message from the correct Telegram account.

### Hooks installed but messages not forwarded

Why it happens:
- Hooks were not installed. Or Claude settings are stale.

Fix:

```bash
./bridge.sh hook install
```

Then restart your Claude worker session (`/restart`). Or end and hire the worker again.
