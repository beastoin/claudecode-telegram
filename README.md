# Claude Code - Telegram

Run multiple AI workers from Telegram—research, operations, and development in one chat.

<img width="400" alt="image" src="https://github.com/user-attachments/assets/a12cbdbf-cf18-4ba4-8645-08a3a359559a" />

## What This Is

Claude Code - Telegram is a Telegram bot + local bridge that lets you run and coordinate parallel AI workers from one Telegram chat.

In plain English: you message your bot, the bot sends the task to a Claude worker running on your computer, and the worker replies back in Telegram.

What you need:
- A Mac or Linux/Ubuntu computer
- A Telegram account
- An Anthropic account (for API usage)

Important terms (zero assumed knowledge):
- **API key**: a secret key that lets Claude CLI use your Anthropic account.
- **Bot token**: a secret key from Telegram that lets this project control your bot.
- **tmux**: a terminal session manager; this project uses it to keep workers running in the background.
- **Webhook**: a secure URL where Telegram sends new messages so your bot can react instantly.
- **cloudflared**: creates a secure public tunnel so Telegram can reach your computer.

---

## Step-by-Step Setup Guide

If you skip steps, workers can appear to start but fail later. Follow each step in order.

### Part 1: Get Your Accounts Ready (5 min)

#### Step 1: Create a Telegram bot (with BotFather)

Why this matters: this creates your bot identity and gives you the bot token this project needs.

1. Open Telegram and search for `@BotFather`.
2. Open the BotFather chat and press **Start**.
3. Send this command in BotFather:

```bash
/newbot
```

4. BotFather asks for a bot name; send any display name you want.
5. BotFather asks for a username; send a unique username that ends with `bot` (example: `myteamhelper_bot`).
6. Copy the token BotFather sends you.

What you should see in Telegram:
- A BotFather message similar to: `Done! Congratulations on your new bot...`
- A line containing your bot token.

Verification:
- Your token should look like this pattern: `123456789:AAExampleTokenStringHere`.

If this fails:
- Error: `Sorry, this username is already taken.`
- Fix: choose another username and keep the required `bot` suffix.

#### Step 2: Create an Anthropic API key

Why this matters: workers cannot call Claude without your API key.

1. Open `https://console.anthropic.com` and sign in.
2. Open API keys and create a new key.
3. Copy the key immediately.

Verification:
- Your key should start with `sk-ant-`.

If this fails:
- Error: key is missing or does not start with `sk-ant-`.
- Fix: create a new key and copy it again.

Note:
- API usage costs money per request. Check pricing at `https://www.anthropic.com/pricing`.

---

### Part 2: Install Software (10 min)

Choose your platform path and complete every step in that section.

## macOS Setup Path

#### Step 1: Install Homebrew (package manager)

Why this matters: Homebrew makes installing required tools simple and consistent.

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Verification command:

```bash
brew --version
```

You should see output like: `Homebrew 4.x.x`.

If this fails:
- Error: `xcode-select: error: tool 'xcode-select' requires Xcode`
- Fix: run the command below, then retry Step 1.

```bash
xcode-select --install
```

#### Step 2: Install Node.js

Why this matters: Claude CLI is a Node package, so Node.js is required first.

If you prefer a visual installer, you can download Node.js from `https://nodejs.org` instead of using Homebrew.

```bash
brew install node
```

Verification command:

```bash
node --version
```

You should see output like: `v20.11.1` (v18+ is required).

#### Step 3: Install Claude CLI

Why this matters: this is the `claude` command workers actually run.

> [!WARNING]
> Common failure before this step: `npm ERR! code EACCES`.
> If you see this on macOS, install Node via Homebrew first (Step 2), then retry this command.

```bash
npm install -g @anthropic-ai/claude-code
```

Verification command:

```bash
claude --version
```

You should see output like: `1.0.x`.

#### Step 4: Install tmux

Why this matters: tmux keeps multiple workers alive in parallel background sessions.

```bash
brew install tmux
```

Verification command:

```bash
tmux -V
```

You should see output like: `tmux 3.4`.

#### Step 5: Install Python 3

Why this matters: the bridge is written in Python.

```bash
brew install python
```

Verification command:

```bash
python3 --version
```

You should see output like: `Python 3.11.x`.

#### Step 6: Install jq

Why this matters: setup scripts use jq for JSON editing and checks.

```bash
brew install jq
```

Verification command:

```bash
jq --version
```

You should see output like: `jq-1.7`.

#### Step 7: Verify curl

Why this matters: setup and tunnel workflows call `curl` in multiple places.

```bash
curl --version
```

You should see output beginning with `curl`.

#### Step 8: Install cloudflared

Why this matters: cloudflared opens the secure tunnel Telegram needs to reach your machine.

```bash
brew install cloudflared
```

Verification command:

```bash
cloudflared --version
```

You should see output containing: `cloudflared version`.

## Linux/Ubuntu Setup Path

#### Step 1: Update package index

Why this matters: this refreshes available package versions before installing tools.

```bash
sudo apt update
```

Verification command:

```bash
apt-cache policy nodejs
```

You should see package metadata instead of `Unable to locate package`.

#### Step 2: Install Node.js and npm

Why this matters: Claude CLI installation depends on both Node.js and npm.

```bash
sudo apt install -y nodejs npm
```

Verification command:

```bash
node --version
```

You should see `v18` or newer.

If this fails:
- Problem: Ubuntu repo may install an older Node version.
- Fix: run NodeSource setup, then reinstall Node.js.

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
```

```bash
sudo apt install -y nodejs
```

#### Step 3: Install Claude CLI

Why this matters: this provides the `claude` executable used by worker sessions.

> [!WARNING]
> Common failure before this step: `npm ERR! code EACCES`.
> Fix: retry with sudo on Ubuntu.

```bash
sudo npm install -g @anthropic-ai/claude-code
```

Verification command:

```bash
claude --version
```

You should see output like: `1.0.x`.

#### Step 4: Install tmux, jq, curl, and Python 3

Why this matters: these are required by the bridge runtime and setup scripts.

```bash
sudo apt install -y tmux jq curl python3
```

Verification command:

```bash
tmux -V
```

You should see output like: `tmux 3.3a`.

Verification command:

```bash
python3 --version
```

You should see output like: `Python 3.10.x` or newer.

#### Step 5: Verify curl

Why this matters: download and webhook troubleshooting commands use `curl`.

```bash
curl --version
```

You should see output beginning with `curl`.

#### Step 6: Check CPU architecture

Why this matters: cloudflared download URL depends on CPU type.

```bash
uname -m
```

You should see `x86_64` or `aarch64`.

#### Step 7: Download cloudflared (x86_64)

Why this matters: this fetches cloudflared for most Intel/AMD Linux systems.

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /tmp/cloudflared
```

If your previous step returned `aarch64`, use this command instead:

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -o /tmp/cloudflared
```

#### Step 8: Install cloudflared binary

Why this matters: this places cloudflared in your executable path.

```bash
sudo install /tmp/cloudflared /usr/local/bin/cloudflared
```

Verification command:

```bash
cloudflared --version
```

You should see output containing: `cloudflared version`.

If this fails:
- Error: `Permission denied` while installing.
- Fix: rerun Step 8 with `sudo` exactly as shown.

---

### Part 3: Configure Authentication (5 min)

#### Step 1: Save your Anthropic API key permanently

Why this matters: workers run in tmux sessions, and tmux must inherit `ANTHROPIC_API_KEY` from your shell startup file.

Important detail: this bridge propagates bridge-specific variables into workers, but it does **not** inject `ANTHROPIC_API_KEY` for you.

> [!WARNING]
> Do **not** create `~/.claude/.credentials.json` for API-key auth.
> It can cause: `OAuth error: Invalid code`.

### macOS (default shell: zsh)

Why this matters: `~/.zshrc` runs every time you open a terminal.

```bash
echo 'export ANTHROPIC_API_KEY="sk-ant-paste-your-real-key-here"' >> ~/.zshrc
```

Replace `sk-ant-paste-your-real-key-here` with your real key from Part 1.

```bash
source ~/.zshrc
```

```bash
echo "$ANTHROPIC_API_KEY"
```

You should see your key printed, starting with `sk-ant-`.

### Linux/Ubuntu (default shell: bash)

Why this matters: `~/.bashrc` runs every time you open a terminal.

```bash
echo 'export ANTHROPIC_API_KEY="sk-ant-paste-your-real-key-here"' >> ~/.bashrc
```

Replace `sk-ant-paste-your-real-key-here` with your real key from Part 1.

```bash
source ~/.bashrc
```

```bash
echo "$ANTHROPIC_API_KEY"
```

You should see your key printed, starting with `sk-ant-`.

If this fails:
- Symptom: command prints a blank line.
- Fix: repeat the `echo 'export ANTHROPIC_API_KEY=...' >> ...` step carefully and reload with `source`.

#### Step 2: Complete Claude CLI first-run wizard once

Why this matters: first-run setup must finish once interactively, or workers can get stuck in setup and never answer.

```bash
claude --dangerously-skip-permissions
```

In the interactive wizard:
1. Pick any theme.
2. Choose `Yes` when asked about custom API key.
3. Confirm permissions bypass.
4. Type `/exit` to close.

Verification command:

```bash
claude --version
```

You should see only a version line (no wizard prompts).

If this fails:
- Error: `OAuth error: Invalid code`.
- Fix: remove the wrong credentials file, then rerun this step.

```bash
rm -f ~/.claude/.credentials.json
```

---

### Part 4: Download and Install claudecode-telegram (2 min)

Choose one option.

#### Option A: Git clone (recommended)

Why this matters: clone gives you easy future updates.

1. Clone the repository.

```bash
git clone https://github.com/beastoin/claudecode-telegram
```

2. Verify key project files are present.

```bash
ls claudecode-telegram/
```

You should see entries including `bridge.py`, `claudecode-telegram.sh`, `hooks`, `DOC.md`, and `test.sh`.

3. Enter the project folder.

```bash
cd claudecode-telegram
```

If this fails:
- Error: `git: command not found`.
- Fix (macOS): run `brew install git`.
- Fix (Ubuntu): run `sudo apt install -y git`.

#### Option B: Tarball (if repo access is restricted)

Why this matters: this works when GitHub clone access is unavailable.

1. Extract the tarball.

```bash
tar xzf claudecode-telegram.tar.gz
```

2. Verify key project files are present.

```bash
ls claudecode-telegram/
```

You should see entries including `bridge.py`, `claudecode-telegram.sh`, `hooks`, `DOC.md`, and `test.sh`.

3. Enter the project folder.

```bash
cd claudecode-telegram
```

---

### Part 5: Start the Bridge (2 min)

#### Step 1: Install Claude hooks

Why this matters: hooks are small scripts that send worker replies from Claude sessions back into Telegram.

```bash
./claudecode-telegram.sh hook install
```

You should see success lines mentioning Stop/SessionStart hooks.

If this fails:
- Error: `jq: command not found`.
- Fix: install jq (Part 2), then rerun this step.

#### Step 2: Set your Telegram bot token for this terminal

Why this matters: the bridge cannot call Telegram without `TELEGRAM_BOT_TOKEN`.

```bash
export TELEGRAM_BOT_TOKEN="123456789:paste-your-real-bot-token-here"
```

Replace the entire value with the token from BotFather.

Verification command:

```bash
echo "$TELEGRAM_BOT_TOKEN"
```

You should see a value with digits, a colon, and a long token string.

If this fails:
- Symptom: output is blank.
- Fix: rerun the `export TELEGRAM_BOT_TOKEN=...` command.

#### Step 3: Run the bridge

Why this matters: this starts bridge + tunnel + webhook so Telegram can reach your workers.

> [!WARNING]
> Common failure before this step: `tmux: command not found`.
> Fix: install tmux first (Part 2), verify with `tmux -V`, then rerun.

```bash
./claudecode-telegram.sh run
```

You should see output similar to:
- `Bridge started on port 8080`
- `Tunnel URL: https://...`
- `Webhook configured`

Verification command (open a second terminal in the same folder):

```bash
./claudecode-telegram.sh status
```

You should see node status marked running.

If this fails:
- Error: `Connection refused`.
- Fix: the bridge is not running; run `./claudecode-telegram.sh run` again and keep that terminal open.
- Error: `Webhook setup failed (DNS may still be propagating)`.
- Fix: wait 20-60 seconds and rerun `./claudecode-telegram.sh run`.

---

### Part 6: Start Using It in Telegram (1 min)

#### Step 1: Open your bot chat

Why this matters: all manager/operator actions happen in Telegram.

1. Open Telegram.
2. Search for your bot username from BotFather.
3. Press **Start**.

#### Step 2: Hire your first worker

Why this matters: no one can receive tasks until a worker exists.

Send this in Telegram:

```bash
/hire myworker
```

You should see confirmation that worker `myworker` was created.

If this fails:
- Error: `No one assigned` on later messages.
- Fix: run `/team` then `/focus myworker`.

#### Step 3: Send your first task

Why this matters: this validates end-to-end delivery from Telegram to worker and back.

Send this in Telegram:

```bash
Summarize today’s priorities from our latest commit messages.
```

What to expect:
1. Bot reacts with `👀` (delivery confirmed).
2. Worker replies as `myworker: ...`.

If this fails:
- Symptom: no `👀` reaction.
- Fix: verify `TELEGRAM_BOT_TOKEN` and bridge status.
- Symptom: `👀` appears but no reply.
- Fix: run `/progress`, then `/restart` if the worker is stuck.

---

## Daily Use

### For Managers (Telegram only)

1. Ask your operator to complete setup once.
2. Open the bot in Telegram.
3. Send `/hire myworker`.
4. Start assigning work in plain English.

### For Operators (normal startup)

1. Open terminal in `claudecode-telegram`.
2. Export bot token if not already set in your shell profile.
3. Run `./claudecode-telegram.sh run`.
4. Confirm with `./claudecode-telegram.sh status`.

### Real Commands (from our real workflow)

1. `/hire ops`
2. `/hire triage`
3. `/hire research`
4. `/hire frontend`
5. `/hire qa`
6. `/ops Run 5 worker queue: scrape refunds, reconcile invoices, update CRM notes, draft escalation email, and ping @qa for flaky test owners`
7. `/triage Triage the latest GitHub issues; label, close dupes, and summarize top 10 with links`
8. `/research Compare auth flows across api/, web/, and mobile/ repos; highlight inconsistencies + suggested fix`
9. `/frontend Audit the settings UI for missing states and propose copy improvements`
10. `/qa Reproduce the top crash from yesterday and draft a minimal repro`
11. `@ops Coordinate with @frontend on status banner copy; @research share findings with @triage`
12. `/progress`
13. `/progress` (later, when you return)
14. `@ops Post the nightly summary + anything blocked`

You can also drop a screenshot and ask: `What is wrong with this UI?`

## Commands Reference

### Telegram commands

| Command | What it does |
|---------|--------------|
| `/hire <name>` | Add a worker |
| `/focus <name>` | Set who gets your next message |
| `/progress` | See if the focused worker is busy |
| `/team` | List workers + focus |
| `/end <name>` | Remove a worker |
| `/pause` | Interrupt active worker |
| `/restart` | Restart active worker |
| `/restart --clean` | Restart with fresh context |
| `/learn` | Ask focused worker what they learned |
| `@name <msg>` | Send one-off message to a specific worker |
| `<message>` | Send to current focused worker |

Backend selection examples:
- `/hire alice --codex`
- `/hire codex-alice`
- `/hire gemini-worker --gemini`
- `/hire op-worker --opencode`
- `/hire custom --backend <name>`

### Shell commands

| Command | What it does |
|---------|--------------|
| `./claudecode-telegram.sh run` | Start bridge + tunnel + webhook |
| `./claudecode-telegram.sh restart` | Restart node, preserve tmux sessions |
| `./claudecode-telegram.sh stop` | Stop node |
| `./claudecode-telegram.sh clean` | Reset admin/chat ID |
| `./claudecode-telegram.sh status` | Show status |
| `./claudecode-telegram.sh webhook <url>` | Set webhook URL manually |
| `./claudecode-telegram.sh webhook info` | Show webhook details |
| `./claudecode-telegram.sh webhook delete` | Remove webhook |
| `./claudecode-telegram.sh hook install` | Install Claude hooks |
| `./claudecode-telegram.sh hook uninstall` | Remove Claude hooks |
| `./claudecode-telegram.sh hook test` | Send test message to Telegram |

### Common runtime flags

| Flag | Meaning |
|------|---------|
| `--node <name>` | Target one node (example: prod/dev) |
| `--all` | Apply command to all nodes (status/stop) |
| `--port <port>` | Set bridge port |
| `--no-tunnel` | Skip cloudflared + webhook automation |
| `--tunnel-url <url>` | Use an existing tunnel URL |
| `--headless` | Non-interactive mode |
| `--json` | JSON output for status |

## What to Expect (Message Flow)

1. You send a task.
2. Bot reacts with `👀` to confirm delivery.
3. Worker replies later as `worker_name: ...`.

## Full Troubleshooting Guide

### Quick top-3 checks

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Bot doesn't respond | Bridge down or wrong admin | Run `./claudecode-telegram.sh status` and restart if needed |
| `👀` but no reply | Worker busy or stuck | Run `/progress`, then `/restart` |
| `No one assigned` | No focused worker | Run `/team`, then `/focus <name>` |

### `OAuth error: Invalid code`

Why it happens:
- Claude CLI was not fully initialized with API-key flow, or `~/.claude/.credentials.json` was manually created.

Fix:

```bash
rm -f ~/.claude/.credentials.json
```

```bash
claude --dangerously-skip-permissions
```

Then complete wizard and exit with `/exit`.

### `ModuleNotFoundError: No module named ...` or `No module named ...`

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
- tmux is not installed, so workers cannot stay alive.

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

If blank, add the key to your shell startup file (Part 3), open a new terminal, then restart the bridge.

```bash
./claudecode-telegram.sh restart
```

### Bot does not react with `👀`

Why it happens:
- Invalid/missing `TELEGRAM_BOT_TOKEN`, webhook misconfiguration, or bridge down.

Fix:

```bash
echo "$TELEGRAM_BOT_TOKEN"
```

```bash
./claudecode-telegram.sh status
```

```bash
./claudecode-telegram.sh run
```

### `Connection refused`

Why it happens:
- Bridge process is not listening on expected port.

Fix:

```bash
./claudecode-telegram.sh run
```

Then verify:

```bash
./claudecode-telegram.sh status
```

### Tunnel issues (cloudflared)

Common errors:
- `cloudflared: command not found`
- webhook not updating after startup

Fix:

```bash
cloudflared --version
```

If command not found, install cloudflared (Part 2) and rerun bridge.

```bash
./claudecode-telegram.sh run
```

### Webhook mismatch or stale webhook

Why it happens:
- Tunnel URL changed but Telegram still points to old URL.

Fix:

```bash
./claudecode-telegram.sh webhook info
```

```bash
./claudecode-telegram.sh webhook delete
```

```bash
./claudecode-telegram.sh run
```

### Wrong admin account controls the bot

Why it happens:
- First user to message becomes admin when `ADMIN_CHAT_ID` is not set.

Fix:

```bash
./claudecode-telegram.sh clean
```

Then restart and send the first message from the correct Telegram account.

### Hooks installed but messages not forwarded

Why it happens:
- Hooks were not installed or Claude settings are stale.

Fix:

```bash
./claudecode-telegram.sh hook install
```

Then restart your Claude worker session (`/restart`) or end/hire worker again.

## Security Hardening (Optional)

The bridge includes built-in security defaults. These optional steps add defense-in-depth for production deployments.

### Already enabled by default

- **Localhost-only binding**: Bridge binds to `127.0.0.1` — only cloudflared (on the same machine) can reach it. Override with `BRIDGE_BIND=0.0.0.0` if needed.
- **HMAC-signed hook endpoints**: `/response` and `/notify` require `X-Hook-Signature` headers. The bridge generates a per-run secret and exports it to worker sessions automatically. Rogue local processes cannot inject messages without the secret.
- **Webhook secret**: Set `TELEGRAM_WEBHOOK_SECRET` to verify incoming Telegram webhooks. The bridge rejects forged webhook requests.
- **Token isolation**: Workers never see `TELEGRAM_BOT_TOKEN`. Responses flow through the bridge, which holds the token.

### Recommended system-level hardening

#### 1. Run bridge under a dedicated Unix user

Why: prevents workers from reading bridge environment (including the bot token) via `/proc`.

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin bridge-user
```

Run the bridge as `bridge-user` and workers as your normal user. Workers cannot read `/proc/<bridge-pid>/environ`.

#### 2. Hide process information between users

Why: prevents any user from listing other users' processes and reading their environment.

```bash
sudo mount -o remount,hidepid=2 /proc
```

To make it permanent, add to `/etc/fstab`:

```text
proc /proc proc defaults,hidepid=2 0 0
```

#### 3. Set ADMIN_CHAT_ID explicitly

Why: prevents the first random person who finds your bot from becoming admin.

```bash
export ADMIN_CHAT_ID="123456789"
```

Get your chat ID by messaging [@userinfobot](https://t.me/userinfobot) on Telegram.

#### 4. Enable Telegram webhook verification

Why: ensures only Telegram (not an attacker who discovers your tunnel URL) can send webhooks.

```bash
export TELEGRAM_WEBHOOK_SECRET="$(openssl rand -hex 16)"
```

The bridge passes this to Telegram during webhook setup and verifies it on every incoming request.

## Gotchas & Limits

- **Single admin**: First person to message becomes admin unless `ADMIN_CHAT_ID` is set.
- **Focus resets + context persists**: After restart, run `/focus` again. Want a clean slate? `/end <name>` then `/hire <name>`.
- **Telegram limits**: Long replies split after 4096 chars.

## Project Structure

```text
claudecode-telegram/
|-- bridge.py
|-- claudecode-telegram.sh
|-- hooks/
|-- DOC.md
`-- test.sh
```

## Manager Outcomes

- **Throughput while offline.** Run multiple workers in parallel so work continues after hours.
- **Less context tax.** Long-lived workers keep state, so you do not re-explain.
- **One place to coordinate.** Broadcast, delegate, and check status from a single chat.

## Real Results (From Our Team)

- **@chen** triaged 290 issues in one session and tagged priorities + root causes.
- **@geni** did deep research on 2 OSS projects, tracing end-to-end flows and dependencies.
- **Ops manager** keeps 5 workers running; code ships while they are offline.

## Where Data Lives

- **Messages stay in Telegram.** The bridge does not store message history elsewhere.
- **Worker context is the chat.** Each worker continues from the same ongoing Telegram thread.
- **Easy to resume.** Pick up any time from the existing chat history.

## Why This Architecture

- **Fewer places for data to live means lower risk.**
- **Less to secure and less to monitor.**
- **Easier reviews when you need to check what happened.**
- **Fewer moving parts to break.**

## Use Cases

- **Ops:** incident updates, checklists, status notes.
- **Research:** quick briefs, vendor comparisons, market scans.
- **Triage:** sort tickets, label issues, route requests.
- **Support:** draft replies, summarize threads, suggest next steps.

## Compounding Team Knowledge

- Keep a lightweight team memory with two shared files: `~/team/playbook.md` and `~/team/learnings.md`.
- Daily: ops manager asks for learnings/help, team adds quick notes.
- Result: the team gets smarter every day and repeats fewer mistakes.
- **[See our playbook template with real examples](TEMPLATE-PLAYBOOK.md)**

## Credits

Original project by Han Xiao (hanxiao/claudecode-telegram).

## License

MIT
