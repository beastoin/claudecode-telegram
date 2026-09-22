# Setup Guide
## Step-by-Step Setup Guide

Follow each step in order. If you skip steps, workers can start but fail later.

### Part 1: Get Your Accounts Ready (5 min)

#### Step 1: Create a Telegram bot (with BotFather)

This step creates your bot identity. It gives you the bot token this project needs.

1. Open Telegram and search for `@BotFather`.
2. Open the BotFather chat and press **Start**.
3. Send this command in BotFather:

```bash
/newbot
```

4. BotFather asks for a bot name. Send any display name you want.
5. BotFather asks for a username. Send a unique username that ends with `bot` (example: `myteamhelper_bot`).
6. Copy the token BotFather sends you.

What you should see in Telegram:
- A BotFather message similar to: `Done! Congratulations on your new bot...`
- A line that contains your bot token.

Verification:
- Your token looks like this: `123456789:AAExampleTokenStringHere`.

If this fails:
- Error: `Sorry, this username is already taken.`
- Fix: choose another username. Keep the required `bot` suffix.

#### Step 2: Create an Anthropic API key

Workers cannot call Claude without your API key.

1. Open `https://console.anthropic.com` and sign in.
2. Open API keys and create a new key.
3. Copy the key immediately.

Verification:
- Your key starts with `sk-ant-`.

If this fails:
- Error: key is missing or does not start with `sk-ant-`.
- Fix: create a new key and copy it again.

Note:
- API usage costs money per request. Check pricing at `https://www.anthropic.com/pricing`.

---

### Part 2: Install Software (10 min)

Choose your platform path. Complete every step in that section.

## macOS Setup Path

#### Step 1: Install Homebrew (package manager)

Homebrew makes installing required tools simple and consistent.

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

Claude CLI is a Node package. Node.js must be installed first.

You can also download Node.js from `https://nodejs.org` instead of Homebrew.

```bash
brew install node
```

Verification command:

```bash
node --version
```

You should see output like: `v20.11.1` (v18+ is required).

#### Step 3: Install Claude CLI

This installs the `claude` command that workers run.

> [!WARNING]
> Common failure before this step: `npm ERR! code EACCES`.
> If you see this on macOS, install Node via Homebrew first (Step 2), then retry.

```bash
npm install -g @anthropic-ai/claude-code
```

Verification command:

```bash
claude --version
```

You should see output like: `1.0.x`.

#### Step 4: Install tmux

tmux keeps multiple workers alive in parallel background sessions.

```bash
brew install tmux
```

Verification command:

```bash
tmux -V
```

You should see output like: `tmux 3.4`.

#### Step 5: Install Python 3

The bridge is written in Python.

```bash
brew install python
```

Verification command:

```bash
python3 --version
```

You should see output like: `Python 3.11.x`.

#### Step 6: Install jq

Setup scripts use jq for JSON editing and checks.

```bash
brew install jq
```

Verification command:

```bash
jq --version
```

You should see output like: `jq-1.7`.

#### Step 7: Verify curl

Setup and tunnel workflows use `curl` in multiple places.

```bash
curl --version
```

You should see output that begins with `curl`.

#### Step 8: Install cloudflared

cloudflared opens the secure tunnel Telegram needs to reach your machine.

```bash
brew install cloudflared
```

Verification command:

```bash
cloudflared --version
```

You should see output that contains: `cloudflared version`.

## Linux/Ubuntu Setup Path

#### Step 1: Update package index

This refreshes available package versions before you install tools.

```bash
sudo apt update
```

Verification command:

```bash
apt-cache policy nodejs
```

You should see package metadata instead of `Unable to locate package`.

#### Step 2: Install Node.js and npm

Claude CLI needs both Node.js and npm.

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

This provides the `claude` executable that worker sessions use.

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

The bridge runtime and setup scripts need these tools.

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

Download and webhook troubleshooting commands use `curl`.

```bash
curl --version
```

You should see output that begins with `curl`.

#### Step 6: Check CPU architecture

The cloudflared download URL depends on CPU type.

```bash
uname -m
```

You should see `x86_64` or `aarch64`.

#### Step 7: Download cloudflared (x86_64)

This fetches cloudflared for most Intel/AMD Linux systems.

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /tmp/cloudflared
```

If your previous step returned `aarch64`, use this command instead:

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -o /tmp/cloudflared
```

#### Step 8: Install cloudflared binary

This places cloudflared in your executable path.

```bash
sudo install /tmp/cloudflared /usr/local/bin/cloudflared
```

Verification command:

```bash
cloudflared --version
```

You should see output that contains: `cloudflared version`.

If this fails:
- Error: `Permission denied` while installing.
- Fix: rerun Step 8 with `sudo` exactly as shown.

---

### Part 3: Configure Authentication (5 min)

#### Step 1: Save your Anthropic API key permanently

Workers run in tmux sessions. tmux must inherit `ANTHROPIC_API_KEY` from your shell startup file.

The bridge propagates bridge-specific variables into workers. It does **not** inject `ANTHROPIC_API_KEY` for you.

> [!WARNING]
> Do **not** create `~/.claude/.credentials.json` for API-key auth.
> It can cause: `OAuth error: Invalid code`.

### macOS (default shell: zsh)

`~/.zshrc` runs every time you open a terminal.

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

You should see your key printed. It starts with `sk-ant-`.

### Linux/Ubuntu (default shell: bash)

`~/.bashrc` runs every time you open a terminal.

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

You should see your key printed. It starts with `sk-ant-`.

If this fails:
- Symptom: command prints a blank line.
- Fix: repeat the `echo 'export ANTHROPIC_API_KEY=...' >> ...` step carefully. Then reload with `source`.

#### Step 2: Complete Claude CLI first-run wizard once

First-run setup must finish once interactively. If you skip this, workers can get stuck in setup and never answer.

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

You should see only a version line with no wizard prompts.

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

Clone gives you easy future updates.

1. Clone the repository.

```bash
git clone https://github.com/beastoin/claudecode-telegram
```

2. Verify that key project files are present.

```bash
ls claudecode-telegram/
```

You should see entries that include `bridge.py`, `bridge.sh`, `hooks`, `SPEC.md`, and `test.sh`.

3. Enter the project folder.

```bash
cd claudecode-telegram
```

If this fails:
- Error: `git: command not found`.
- Fix (macOS): run `brew install git`.
- Fix (Ubuntu): run `sudo apt install -y git`.

#### Option B: Tarball (if repo access is restricted)

Use this method when GitHub clone access is unavailable.

1. Extract the tarball.

```bash
tar xzf claudecode-telegram.tar.gz
```

2. Verify that key project files are present.

```bash
ls claudecode-telegram/
```

You should see entries that include `bridge.py`, `bridge.sh`, `hooks`, `SPEC.md`, and `test.sh`.

3. Enter the project folder.

```bash
cd claudecode-telegram
```

---

### Part 5: Start the Bridge (2 min)

#### Step 1: Install Claude hooks

Hooks are small scripts that send worker replies from Claude sessions back into Telegram.

```bash
./bridge.sh hook install
```

You should see success lines that mention Stop/SessionStart hooks.

If this fails:
- Error: `jq: command not found`.
- Fix: install jq (Part 2), then rerun this step.

#### Step 2: Set your Telegram bot token for this terminal

The bridge cannot call Telegram without `TELEGRAM_BOT_TOKEN`.

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

This starts the bridge, tunnel, and webhook so Telegram can reach your workers.

> [!WARNING]
> Common failure before this step: `tmux: command not found`.
> Fix: install tmux first (Part 2), verify with `tmux -V`, then rerun.

```bash
./bridge.sh run
```

You should see output similar to:
- `Multi-Session Bridge on 127.0.0.1:<port>` (port depends on node: prod=8271, dev=8272, default=8270)
- `Tunnel URL: https://...`
- `Webhook configured`

Open a second terminal in the same folder and verify:

```bash
./bridge.sh status
```

You should see node status marked running.

If this fails:
- Error: `Connection refused`.
- Fix: the bridge is not running. Run `./bridge.sh run` again. Keep that terminal open.
- Error: `Webhook setup failed (DNS may still be propagating)`.
- Fix: wait 20-60 seconds. Then rerun `./bridge.sh run`.

---

### Part 6: Start Using It in Telegram (1 min)

#### Step 1: Open your bot chat

All manager and operator actions happen in Telegram.

1. Open Telegram.
2. Search for your bot username from BotFather.
3. Press **Start**.

#### Step 2: Hire your first worker

No one can receive tasks until a worker exists.

Send this in Telegram:

```bash
/hire myworker
```

You should see confirmation that worker `myworker` was created.

If this fails:
- Error: `No one assigned` on later messages.
- Fix: run `/team` then `/focus myworker`.

#### Step 3: Send your first task

This validates end-to-end delivery from Telegram to worker and back.

Send this in Telegram:

```bash
Summarize today's priorities from our latest commit messages.
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

