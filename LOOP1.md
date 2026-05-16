# Bridge Transport Abstraction

## Problem

bridge.py (11K lines) has ~56 direct Telegram API calls scattered throughout. This makes it impossible to:
1. Test forge workers against a real bridge without a Telegram bot
2. Run the bridge in non-Telegram mode (CLI, test, future transports)
3. Swap the messaging transport without touching business logic

## Goal

Extract a `MessageTransport` interface that decouples the bridge's core logic (worker management, command routing, message delivery) from the messaging transport (Telegram). Telegram becomes one implementation. A `LocalTransport` (log-to-stdout, accept HTTP POST) enables testing without Telegram.

## Architecture

```
bridge.py
  ├── Core: WorkerManager, CommandRouter, Handler (HTTP)
  └── transport: MessageTransport (interface)
        ├── TelegramTransport — real Telegram Bot API (production)
        └── LocalTransport — stdout + HTTP POST (testing/forge e2e)
```

### MessageTransport Interface

```python
class MessageTransport:
    """Interface for all outbound messaging from bridge to manager."""

    def send_text(self, chat_id, text, parse_mode=None, reply_to=None) -> dict | None:
        """Send a text message. Returns API response or None."""
        raise NotImplementedError

    def send_photo(self, chat_id, photo_path, caption=None) -> bool:
        """Send an image file. Returns True on success."""
        raise NotImplementedError

    def send_document(self, chat_id, doc_path, caption=None) -> bool:
        """Send a document file. Returns True on success."""
        raise NotImplementedError

    def send_animation(self, chat_id, animation_path, caption=None) -> bool:
        """Send a GIF/MP4 animation. Returns True on success."""
        raise NotImplementedError

    def send_video(self, chat_id, video_path, caption=None) -> bool:
        """Send a video with player. Returns True on success."""
        raise NotImplementedError

    def send_audio(self, chat_id, audio_path, caption=None) -> bool:
        """Send audio with player. Returns True on success."""
        raise NotImplementedError

    def send_voice(self, chat_id, voice_path, caption=None) -> bool:
        """Send voice bubble. Returns True on success."""
        raise NotImplementedError

    def send_sticker(self, chat_id, sticker_path) -> bool:
        """Send a sticker. Returns True on success."""
        raise NotImplementedError

    def send_chat_action(self, chat_id, action) -> None:
        """Send typing indicator or similar."""
        raise NotImplementedError

    def set_reaction(self, chat_id, message_id, reaction) -> None:
        """React to a message."""
        raise NotImplementedError

    def edit_message(self, chat_id, message_id, text, parse_mode=None) -> dict | None:
        """Edit an existing message."""
        raise NotImplementedError

    def setup_commands(self, commands) -> None:
        """Register bot commands (e.g. /hire, /restart)."""
        raise NotImplementedError

    def download_file(self, file_id) -> tuple[str, str] | None:
        """Download a file by ID. Returns (local_path, filename) or None."""
        raise NotImplementedError

    @property
    def name(self) -> str:
        """Transport name for logging."""
        raise NotImplementedError
```

### TelegramTransport

Wraps the existing `TelegramAPI` class and all standalone `send_photo()`, `send_document()`, etc. functions. Moves ALL Telegram-specific code (multipart uploads, file downloads, BOT_TOKEN usage) behind the interface.

### LocalTransport

For testing and forge e2e:
- `send_text()` → print to stdout + append to log file
- `send_photo/document/etc.` → log the path + copy to output dir
- `download_file()` → no-op (no inbound files from Telegram)
- `setup_commands()` → no-op
- Inbound messages arrive via HTTP POST to existing `/` endpoint (same JSON format as Telegram webhook, just without real Telegram)

## Current Telegram Touchpoints (56 calls)

### Category 1: Outbound text (22 calls)
- `telegram_api("sendMessage", ...)` — 10 direct calls
- `send_telegram_message()` — 8 calls (checkin restart notifications)
- `telegram.send_message()` — 3 calls (CommandRouter, startup)
- `telegram_api("editMessageText", ...)` — 1 call

### Category 2: Outbound media (12 calls)
- `send_photo()` — 2 calls (worker response)
- `send_animation()` — 1 call (worker response)
- `send_document()` — 1 call (worker response)
- `send_video()` — 1 call (worker response)
- `send_voice()` — 1 call (worker response)
- `send_audio()` — 1 call (worker response)
- `send_sticker()` — 1 call (worker response)
- Standalone functions with multipart upload — 4 definitions

### Category 3: Inbound file download (3 calls)
- `BOT_TOKEN` used in getFile URL — 1
- `BOT_TOKEN` used in download URL — 1
- File download logic — 1 function

### Category 4: Bot setup (3 calls)
- `telegram_api("setMyCommands", ...)` — 1
- `telegram_api("sendChatAction", ...)` — 1
- `telegram.set_reaction()` — 1

### Category 5: Startup/shutdown (2 calls)
- Startup notification — 1
- BOT_TOKEN check in main() — 1

## Implementation Plan

### Increment 1: Define interface + TelegramTransport wrapper ✅
1. ✅ Add `MessageTransport` base class to bridge.py (line 1251)
2. ✅ Create `TelegramTransport` that wraps existing `TelegramAPI` + standalone functions (line 1358)
3. ✅ Create global `transport` variable initialized from env (line 1730)
4. ✅ **No behavior change** — TelegramTransport calls the same code

### Increment 2: Replace outbound text calls ✅
1. ✅ Replace all `telegram_api("sendMessage", ...)` with `transport.send_text()`
2. ✅ Replace `send_telegram_message()` calls with `transport.send_text()`
3. ✅ Replace `telegram.send_message()` calls with `transport.send_text()`
4. ✅ Replace `telegram_api("editMessageText", ...)` with `transport.edit_message()`
5. ✅ Backward-compat wrappers preserved (delegate to transport)
6. ✅ Tests: FAST suite passes (322/4)

### Increment 3: Replace outbound media calls ✅
1. ✅ Move `send_photo()`, `send_document()`, `send_animation()`, `send_video()`, `send_voice()`, `send_audio()`, `send_sticker()` into `TelegramTransport`
2. ✅ Replace all media calls in `_send_response_to_telegram()` with `transport.send_*()`
3. ✅ Standalone media functions preserved as thin wrappers

### Increment 4: Replace inbound file download ✅
1. ✅ Move `_download_telegram_file()` into `TelegramTransport.download_file()`
2. ✅ Replace callers

### Increment 5: Replace bot setup + startup ✅
1. ✅ Move `setup_bot_commands()` to `transport.setup_commands()`
2. ✅ Move startup notification to `transport.send_text()`
3. ✅ Replace `BOT_TOKEN` check with `TRANSPORT_MODE == "telegram"` guard
4. ✅ `_LegacyTransportAdapter` wraps FakeTelegram in tests (line 5877)

### Increment 6: Implement LocalTransport ✅
1. ✅ `LocalTransport` class: logs to stdout + optional TRANSPORT_LOG file (line 1654)
2. ✅ `TRANSPORT=local` env var selects LocalTransport
3. ✅ Bridge starts without BOT_TOKEN when TRANSPORT=local
4. ✅ 5 unit tests (interface, send_text, media, log file, init selection)
5. ✅ POST /register endpoint for forge worker registration

### Increment 7: Forge e2e with real bridge ✅
1. ✅ Start bridge with `TRANSPORT=local` — works, discovers all workers, no BOT_TOKEN
2. ✅ `forge/test-e2e-bridge.sh` — 13 assertions, all pass
3. ✅ Full lifecycle verified: register → tmux session → workdir → watchdog → alive

## Testing

```bash
# Transport unit tests (FAST mode)
TEST_FILTER=test_transport FAST=1 TEST_BOT_TOKEN='fake' ./test.sh
TEST_FILTER=test_local_transport FAST=1 TEST_BOT_TOKEN='fake' ./test.sh

# Full FAST suite (322 passed, 4 pre-existing failures)
FAST=1 TEST_BOT_TOKEN='fake' ./test.sh

# Forge e2e with mock bridge (11 assertions)
./forge/test-e2e.sh

# Forge e2e with real bridge (13 assertions)
./forge/test-e2e-bridge.sh
```

## Files Modified
- **bridge.py** — MessageTransport (line 1251), TelegramTransport (line 1358), LocalTransport (line 1654), _init_transport (line 1724), POST /register endpoint, API_ENDPOINTS updated
- **test.sh** — 5 transport unit tests + 1 integration test (test_forge_register_endpoint)
- **forge/test-e2e-bridge.sh** — NEW: real bridge e2e test (13 assertions)

## Definition of Done
1. ✅ All Telegram API calls go through MessageTransport
2. ✅ `TRANSPORT=local` starts bridge without BOT_TOKEN
3. ✅ Forge e2e test passes against real bridge with LocalTransport
4. ✅ Existing FAST test suite passes (322/4 pre-existing)
5. ✅ Backward-compat preserved: telegram_api(), send_telegram_message(), standalone send_* still work
