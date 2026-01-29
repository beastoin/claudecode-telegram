# C Bridge (minimal deps)

This folder contains a C implementation of the existing `bridge.py` with the same runtime behavior and endpoints.
It is designed to be a drop-in replacement without changing the current project files.

## Dependencies

- libcurl (HTTPS to Telegram)
- libmicrohttpd (HTTP server)
- cJSON (JSON parsing/building)
- pthreads
- tmux

On macOS (Homebrew):

```
brew install curl microhttpd cjson
```

On Debian/Ubuntu:

```
sudo apt-get install libcurl4-openssl-dev libmicrohttpd-dev libcjson-dev
```

## Build

```
make
```

## Run

Same env vars as the Python bridge:

- `TELEGRAM_BOT_TOKEN` (required)
- `PORT` (default: 8080)
- `TELEGRAM_WEBHOOK_SECRET` (optional)
- `ADMIN_CHAT_ID` (optional)
- `SESSIONS_DIR` (default: $HOME/.claude/telegram/sessions)
- `TMUX_PREFIX` (default: claude-)

Example:

```
export TELEGRAM_BOT_TOKEN=... 
./bridge
```

The hook endpoint remains `http://localhost:$PORT/response` and the webhook receiver accepts Telegram updates on any other path.

## Notes

- This is intentionally a single-file C program for minimal maintenance overhead.
- Behavior matches the Python bridge including session management, security model, and image handling.
