from __future__ import annotations

# ── Infrastructure from core (no circular dependency) ──────────────────
from core import (
    _str_field, _int_field, _dict_field, _bool_field,
    _log, _LOG_ERROR, _LOG_WARN, _LOG_INFO, _LOG_DEBUG,
    _log_best_effort,
    SubprocessRunner, Clock, MarkdownToken,
    _subprocess_runner, _urlopen, _tg_http_client,
    _clock as _default_clock,
    _RealClock, _RealSubprocessRunner,
    AppContext, get_app_context,
    TunnelConfig,
    VERSION,
    BOT_TOKEN, NODE_NAME, NODE_DIR,
    SESSIONS_DIR, TIMEOUT_HTTP_API, TIMEOUT_HTTP_DOWNLOAD, TIMEOUT_HTTP_UPLOAD,
    TIMEOUT_PROCESS_WAIT, TIMEOUT_FILE_TRANSFER, TIMEOUT_TMUX_CHECK,
    PENDING_TIMEOUT,
    STT_ENDPOINT, STT_TIMEOUT,
    TEAM_DIR,
    ADMIN_CHAT_ID_ENV, admin_chat_id,
    DEFAULT_BACKEND,
)
import collections
from dataclasses import dataclass, field
import enum
import hashlib
import http.client
import os
import json
import socket
import mimetypes
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import uuid
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from collections.abc import Iterable, Mapping
from typing import IO, Callable, Iterator, Literal, NamedTuple, Protocol, TypedDict, TYPE_CHECKING, cast, runtime_checkable

class UrlOpenFn(Protocol):
    def __call__(self, req: urllib.request.Request, *, timeout: float) -> http.client.HTTPResponse: ...

# ── Telegram domain types (owned by this module) ─────────────────────
ChatId = int | str
MessageId = int
ParseMode = Literal["HTML", "MarkdownV2"] | None

class TelegramApiResponseDict(TypedDict, total=False):
    ok: bool
    result: object
    description: str
    error_code: int
TelegramApiResponse = TelegramApiResponseDict | None

class TelegramUser(TypedDict, total=False):
    id: int
    is_bot: bool
    first_name: str
    last_name: str
    username: str

class TelegramChat(TypedDict, total=False):
    id: int
    type: str
    title: str
    username: str

class TelegramPhotoSize(TypedDict, total=False):
    file_id: str
    file_unique_id: str
    width: int
    height: int
    file_size: int

class TelegramDocument(TypedDict, total=False):
    file_id: str
    file_unique_id: str
    file_name: str
    mime_type: str
    file_size: int

class TelegramVoice(TypedDict, total=False):
    file_id: str
    file_unique_id: str
    duration: int
    mime_type: str
    file_size: int

class TelegramVideo(TypedDict, total=False):
    file_id: str
    file_unique_id: str
    width: int
    height: int
    duration: int
    file_name: str
    mime_type: str
    file_size: int

class TelegramAudio(TypedDict, total=False):
    file_id: str
    file_unique_id: str
    duration: int
    performer: str
    title: str
    file_name: str
    mime_type: str
    file_size: int

class TelegramSticker(TypedDict, total=False):
    file_id: str
    file_unique_id: str
    width: int
    height: int
    emoji: str
    type: str
    is_animated: bool
    is_video: bool

class TelegramMessageDict(TypedDict, total=False):
    message_id: int
    chat: TelegramChat
    date: int
    text: str
    photo: list[TelegramPhotoSize]
    document: TelegramDocument
    voice: TelegramVoice
    video: TelegramVideo
    video_note: TelegramVideo
    animation: TelegramDocument
    audio: TelegramAudio
    sticker: TelegramSticker
    reply_to_message: "TelegramMessageDict"
    caption: str
    media_group_id: str
    rich_message: dict[str, str]
TelegramCallbackQuery = TypedDict("TelegramCallbackQuery", {
    "id": str,
    "from": TelegramUser,
    "message": TelegramMessageDict,
    "data": str,
}, total=False)

class TelegramUpdate(TypedDict, total=False):
    update_id: int
    message: TelegramMessageDict
    edited_message: TelegramMessageDict
    callback_query: TelegramCallbackQuery

class FileValidation(NamedTuple):
    ok: bool
    detail: Path | str

class MediaGroupEntry(TypedDict):
    items: list[TelegramMessageDict]
    caption: str
    timer: threading.Timer | None

# ── Cross-module type annotations (TYPE_CHECKING only) ───────────────
if TYPE_CHECKING:
    from claudecode import WorkerStateEntry, TmuxSessionDict

# ── IncomingMessage: parse Telegram update once ──

@dataclass
class IncomingMessage:
    update_id: int = 0
    chat_id: int | None = None
    msg_id: int | None = None
    text: str = ""
    photo: list[TelegramPhotoSize] | None = None
    document: TelegramDocument | None = None
    animation: TelegramDocument | None = None
    audio: TelegramAudio | None = None
    voice: TelegramVoice | None = None
    video: TelegramVideo | None = None
    video_note: TelegramVideo | None = None
    sticker: TelegramSticker | None = None
    has_media: bool = False
    doc_is_image: bool = False
    media_group_id: str | None = None
    reply_to: TelegramMessageDict | None = None
    raw_msg: TelegramMessageDict = field(default_factory=lambda: TelegramMessageDict())

    @classmethod
    def from_update(cls: type["IncomingMessage"], update: TelegramUpdate) -> "IncomingMessage":
        msg = update.get("message", {})
        text = msg.get("text", "") or msg.get("caption", "")
        photo = msg.get("photo")
        document = msg.get("document")
        animation = msg.get("animation")
        audio = msg.get("audio")
        voice = msg.get("voice")
        video = msg.get("video")
        video_note = msg.get("video_note")
        sticker = msg.get("sticker")
        doc_is_image = False
        if document:
            mime_type = document.get("mime_type", "")
            doc_is_image = mime_type.startswith("image/")
        has_media = bool(
            photo or document or animation or video
            or audio or voice or video_note or sticker
        )
        return cls(
            update_id=update.get("update_id", 0),
            chat_id=msg.get("chat", {}).get("id"),
            msg_id=msg.get("message_id"),
            text=text,
            photo=photo,
            document=document,
            animation=animation,
            audio=audio,
            voice=voice,
            video=video,
            video_note=video_note,
            sticker=sticker,
            has_media=has_media,
            doc_is_image=doc_is_image,
            media_group_id=msg.get("media_group_id"),
            reply_to=msg.get("reply_to_message"),
            raw_msg=msg,
        )

def _extract_msg_text(msg: TelegramMessageDict) -> str:
    text = msg.get("text") or msg.get("caption") or ""
    if not text:
        rich = msg.get("rich_message")
        blocks = rich.get("blocks") if rich else None
        if isinstance(blocks, list):
            parts = []
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                bt = b.get("text")
                if not bt:
                    continue
                if isinstance(bt, str):
                    parts.append(bt)
                elif isinstance(bt, list):
                    parts.append("".join(
                        chunk if isinstance(chunk, str) else chunk.get("text", "")
                        for chunk in bt
                    ))
            text = "\n".join(parts)
    return text

def _build_cwd_change_notice(
    name: str,
    old_cwd: str,
    new_cwd: str,
    old_sid: str,
) -> str:
    lines = [f"⚠️ {name}: workspace changed"]
    if old_sid:
        lines.append(f"<b>from:</b> <code>{old_cwd}</code> (session <code>{old_sid[:12]}…</code>)")
    else:
        lines.append(f"<b>from:</b> <code>{old_cwd}</code> (no previous session)")
    lines.append(f"<b>to:</b> <code>{new_cwd}</code> (fresh start)")
    return "\n".join(lines)

# ── Team formatting (pure functions, runtime state injected by caller) ──

def _normalize_activity(raw: str) -> str:
    if not raw:
        return raw
    m = re.match(r'^([A-Z][a-z]+ing)\s*(?:\((.+)\))?\s*$', raw)
    if m:
        verb = m.group(1)
        dur = m.group(2)
        if dur:
            return f"Thinking ({dur})"
        return "Thinking"
    return raw

def _team_attention_summary(watchdog_status: str, activity: str) -> tuple[str, str, int]:
    status = (watchdog_status or "").lower()
    act = (activity or "").lower()

    if "rate limit" in act:
        return "🔴", "rate limit", 0
    if "error" in act or "traceback" in act or "not running" in act or "failed" in act:
        return "🔴", "error", 0
    if "needs input" in status or "needs reply" in status:
        return "🟡", "needs reply", 1
    if "stuck" in status or "no progress" in status:
        return "🔴", "stuck", 0
    if "poisoned" in status or "error loop" in status:
        return "🔴", "error loop", 0
    if "dead" in status or "not responding" in status:
        return "🔴", "stopped", 0
    if "offline" in status:
        return "🔴", "offline", 0
    if "exited" in status or "session ended" in status:
        return "🔴", "session ended", 0

    waiting_signals = (
        "waiting for",
        "awaiting",
        "approval",
        "accept edits",
        "confirm",
        "in plan mode",
    )
    if "working (waiting)" in status or any(sig in act for sig in waiting_signals):
        return "🟡", "needs reply", 1

    return "🟢", "ok", 2

def _format_watchdog_status(name: str,
                            pending_lookup: 'Callable[[str], bool] | None' = None,
                            state_snapshot: 'dict[str, WorkerStateEntry] | None' = None,
                            clock_now: float | None = None) -> str:
    if pending_lookup is None:
        pending_lookup = lambda _: False  # noqa: E731
    if clock_now is None:
        clock_now = time.time()
    entry = state_snapshot.get(name) if state_snapshot else None
    if not entry:
        return "Working" if pending_lookup(name) else "Ready"

    state, _reason, since = entry
    now = clock_now
    _SIMPLE = {"READY": "Ready", "BUSY_TOOL": "Working", "BUSY_THINKING": "Thinking",
               "WAITING": "Working", "DEAD": "Not responding", "HOST_OFFLINE": "Host offline",
               "OFFLINE": "Offline", "EXITED": "Session ended", "UNTRACKED_BUSY": "Working"}
    if state in _SIMPLE:
        return _SIMPLE[state]
    minutes = max(0, int((now - since) / 60)) if since else 0
    if state == "WAITING_INPUT":
        return f"Needs reply ({minutes}m)"
    if state == "STUCK":
        age_match = re.search(r"age=(\d+)s", _reason) if _reason else None
        if age_match:
            minutes = int(age_match.group(1)) // 60
        return f"No progress ({minutes}m)"
    if state == "POISONED":
        return f"Error loop ({minutes}m)"
    return state.lower()

def format_team_lines(
    registered: 'dict[str, TmuxSessionDict]',
    active: str | None,
    pending_lookup: 'Callable[[str], bool] | None' = None,
    worker_live: 'dict[str, TmuxSessionDict] | dict[str, dict[str, str | None]] | None' = None,
    *,
    state_snapshot: 'dict[str, WorkerStateEntry] | None' = None,
    clock_now: float | None = None,
    normalize_backend_fn: 'Callable[[str | None], str] | None' = None,
) -> list[str]:
    if pending_lookup is None:
        pending_lookup = lambda _: False  # noqa: E731
    if worker_live is None:
        worker_live = {}
    if state_snapshot is None:
        state_snapshot = {}
    if clock_now is None:
        clock_now = time.time()
    if normalize_backend_fn is None:
        normalize_backend_fn = lambda b: b or DEFAULT_BACKEND  # noqa: E731
    backend_values = set()
    for name, session in registered.items():
        live = worker_live.get(name, {})
        backend = normalize_backend_fn(live.get("backend") or session.get("backend"))
        backend_values.add(backend)
    show_backend = len(backend_values) > 1
    rows = []
    counts = {"🔴": 0, "🟡": 0, "🟢": 0}
    for name in sorted(registered.keys()):
        session = registered[name]
        watchdog_status = _format_watchdog_status(name, pending_lookup,
                                                  state_snapshot=state_snapshot,
                                                  clock_now=clock_now)
        live = worker_live.get(name, {})
        backend = normalize_backend_fn(live.get("backend") or session.get("backend"))
        raw_activity = str(live.get("activity") or "").strip()
        if not raw_activity or raw_activity == "Unknown":
            raw_activity = watchdog_status
        activity = _normalize_activity(raw_activity)
        if len(activity) > 42:
            activity = activity[:39].rstrip() + "..."
        context_pct = str(live.get("context_pct") or "").strip()
        icon, blocker, severity_rank = _team_attention_summary(watchdog_status, raw_activity)
        counts[icon] += 1
        name_cell = f"{name} 🎯" if name == active else name
        ctx_part = f" | ctx {context_pct}" if context_pct and context_pct != "--" else ""
        row = f"{icon} {name_cell} — {activity}{ctx_part}"
        if show_backend:
            row += f" | backend={backend}"
        focus_rank = 0 if name == active else 1
        rows.append((severity_rank, focus_rank, name, blocker, row))
    rows.sort(key=lambda item: (item[0], item[1], item[2]))
    attention_rows = [f"{name} ({blocker})" for rank, _focus, name, blocker, _row in rows if rank < 2]
    lines = []
    focused = active or "(none)"
    lines.append(
        f"Team: {len(registered)} agents · focused: {focused} | "
        f"🟢 {counts['🟢']} ok · 🟡 {counts['🟡']} need reply · 🔴 {counts['🔴']} blocked"
    )
    if attention_rows:
        lines.append("Needs your reply: " + ", ".join(attention_rows))
    lines.extend(row for _rank, _focus, _name, _blocker, row in rows)
    return lines

LAST_CHAT_ID_FILE = NODE_DIR / "last_chat_id"
LAST_ACTIVE_FILE = NODE_DIR / "last_active"

class MediaGroupState:

    def __init__(self) -> None:
        self.buffer: dict[str, MediaGroupEntry] = {}
        self.lock: threading.Lock = threading.Lock()
_MEDIA_GROUP_WAIT: float = 0.8
BOT_COMMANDS = [
    {"command": "team", "description": "Show your team"},
    {"command": "focus", "description": "Focus a worker: /focus <name>"},
    {"command": "restart", "description": "Restart worker (--clean for fresh)"},
    {"command": "status", "description": "Bridge status dashboard"},
    {"command": "pilot", "description": "Toggle pilot access: /pilot <name>"},
    {"command": "relay", "description": "Open public channel: /relay <worker>"},
    {"command": "rewind", "description": "Transcript viewer: /rewind <name>"},
    {"command": "pr", "description": "PR review viewer: /pr <github_pr_url>"},
    {"command": "hire", "description": "Hire a worker: /hire <name>"},
    {"command": "end", "description": "Offboard a worker: /end <name>"},
]
BLOCKED_COMMANDS = [
    "/mcp", "/help", "/config", "/model", "/compact", "/cost",
    "/doctor", "/init", "/login", "/logout", "/permissions",
    "/pr", "/review", "/terminal", "/vim", "/approved-tools", "/listen"
]

# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

def save_last_chat_id(chat_id: ChatId | None) -> None:
    if chat_id is None:
        return
    try:
        NODE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        _tmp = LAST_CHAT_ID_FILE.with_suffix('.tmp')
        _tmp.write_text(str(chat_id))
        _tmp.chmod(0o600)
        os.replace(str(_tmp), str(LAST_CHAT_ID_FILE))
    except OSError as e:
        _log(_LOG_WARN, "bridge", f"Failed to save last_chat_id: {e}")

def load_last_chat_id() -> int | None:
    try:
        if LAST_CHAT_ID_FILE.exists():
            chat_id = LAST_CHAT_ID_FILE.read_text().strip()
            if chat_id:
                return int(chat_id)
    except OSError as e:
        _log(_LOG_WARN, "bridge", f"Failed to load last_chat_id: {e}")
    return None

def save_last_active(name: str) -> None:
    try:
        NODE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        _tmp = LAST_ACTIVE_FILE.with_suffix('.tmp')
        _tmp.write_text(name)
        _tmp.chmod(0o600)
        os.replace(str(_tmp), str(LAST_ACTIVE_FILE))
    except OSError as e:
        _log(_LOG_WARN, "bridge", f"Failed to save last_active: {e}")

def load_last_active() -> str | None:
    try:
        if LAST_ACTIVE_FILE.exists():
            name = LAST_ACTIVE_FILE.read_text().strip()
            if name:
                return name
    except OSError as e:
        _log(_LOG_WARN, "bridge", f"Failed to load last_active: {e}")
    return None

TRANSPORT_MODE = os.environ.get("TRANSPORT", "telegram")

@runtime_checkable
class MessageTransport(Protocol):

    @property
    def name(self) -> str:
        ...

    def send_text(self, chat_id: ChatId, text: str,
                  parse_mode: ParseMode = None,
                  reply_to: MessageId | None = None) -> TelegramApiResponse:
        ...

    def send_rich_text(self, chat_id: ChatId, markdown: str,
                       reply_to: MessageId | None = None) -> TelegramApiResponse:
        ...

    def send_photo(self, chat_id: ChatId, photo_path: str | Path,
                   caption: str | None = None) -> bool:
        ...

    def send_document(self, chat_id: ChatId, doc_path: str | Path,
                      caption: str | None = None) -> bool:
        ...

    def send_animation(self, chat_id: ChatId, animation_path: str | Path,
                       caption: str | None = None) -> bool:
        ...

    def send_video(self, chat_id: ChatId, video_path: str | Path,
                   caption: str | None = None) -> bool:
        ...

    def send_audio(self, chat_id: ChatId, audio_path: str | Path,
                   caption: str | None = None) -> bool:
        ...

    def send_voice(self, chat_id: ChatId, voice_path: str | Path,
                   caption: str | None = None) -> bool:
        ...

    def send_sticker(self, chat_id: ChatId, sticker_path: str | Path) -> bool:
        ...

    def send_chat_action(self, chat_id: ChatId, action: str) -> None:
        ...

    def set_reaction(self, chat_id: ChatId, message_id: MessageId,
                     reaction: list[dict[str, str]]) -> None:
        ...

    def edit_message(self, chat_id: ChatId, message_id: MessageId,
                     text: str, parse_mode: ParseMode = None) -> TelegramApiResponse:
        ...

    def setup_commands(self, commands: list[dict[str, str]]) -> None:
        ...

    def download_file(self, file_id: str, session_name: str) -> str | None:
        ...

class TelegramAPI:

    def __init__(self, token: str) -> None:
        self.token: str = token

    def api(self, method: str, data: Mapping[str, object] | dict[str, object]) -> TelegramApiResponse:
        if not self.token:
            return None
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        payload = json.dumps(data).encode()
        headers = {"Content-Type": "application/json"}
        try:
            with _tg_http_client.post(url, data=payload, headers=headers,
                                       timeout=TIMEOUT_HTTP_API) as r:
                return cast(TelegramApiResponseDict, json.loads(r.read()))
        except urllib.error.HTTPError as e:
            _log(_LOG_ERROR, "telegram", f"Telegram API error: {e}")
            try:
                raw = e.read()
                body = cast(TelegramApiResponseDict, json.loads(raw))
                return body
            except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError, ValueError):
                return {"ok": False, "error_code": e.code, "description": f"HTTP {e.code} (non-JSON body)"}
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            _log(_LOG_ERROR, "telegram", f"Telegram API error: {e}")
            return None

    def send_message(self, chat_id: ChatId, text: str, **kwargs: object) -> TelegramApiResponse:
        payload: dict[str, object] = {"chat_id": chat_id, "text": text}
        payload.update(kwargs)
        return self.api("sendMessage", payload)

    def send_rich_message(self, chat_id: ChatId, markdown: str, **kwargs: object) -> TelegramApiResponse:
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "rich_message": {"markdown": markdown},
        }
        payload.update(kwargs)
        return self.api("sendRichMessage", payload)

    def send_photo(self, chat_id: ChatId, photo: str, **kwargs: object) -> TelegramApiResponse:
        payload: dict[str, object] = {"chat_id": chat_id, "photo": photo}
        payload.update(kwargs)
        return self.api("sendPhoto", payload)

    def send_document(self, chat_id: ChatId, document: str, **kwargs: object) -> TelegramApiResponse:
        payload: dict[str, object] = {"chat_id": chat_id, "document": document}
        payload.update(kwargs)
        return self.api("sendDocument", payload)

    def send_animation(self, chat_id: ChatId, animation: str, **kwargs: object) -> TelegramApiResponse:
        payload: dict[str, object] = {"chat_id": chat_id, "animation": animation}
        payload.update(kwargs)
        return self.api("sendAnimation", payload)

    def set_reaction(self, chat_id: ChatId, message_id: MessageId,
                     reaction: list[dict[str, str]]) -> TelegramApiResponse:
        payload: dict[str, object] = {"chat_id": chat_id, "message_id": message_id, "reaction": reaction}
        return self.api("setMessageReaction", payload)

    def send_chat_action(self, chat_id: ChatId, action: str) -> TelegramApiResponse:
        return self.api("sendChatAction", {"chat_id": chat_id, "action": action})

    # ── Webhook management ────────────────────────────────────────────
    def set_webhook(self, url: str, secret_token: str = "") -> TelegramApiResponse:
        payload: dict[str, object] = {"url": url}
        if secret_token:
            payload["secret_token"] = secret_token
        return self.api("setWebhook", payload)

    def delete_webhook(self) -> TelegramApiResponse:
        return self.api("deleteWebhook", {})

    def get_webhook_info(self) -> TelegramApiResponse:
        return self.api("getWebhookInfo", {})

    def get_me(self) -> TelegramApiResponse:
        return self.api("getMe", {})

class TelegramTransport(MessageTransport):

    def __init__(self, token: str) -> None:
        self._api: TelegramAPI = TelegramAPI(token)

    @property
    def name(self) -> str:
        return "telegram"

    def send_text(self, chat_id: ChatId, text: str,
                  parse_mode: ParseMode = None,
                  reply_to: MessageId | None = None) -> TelegramApiResponse:
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        return telegram_api("sendMessage", payload)

    def send_rich_text(self, chat_id: ChatId, markdown: str,
                       reply_to: MessageId | None = None) -> TelegramApiResponse:
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "rich_message": {"markdown": markdown},
        }
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        return telegram_api("sendRichMessage", payload)

    _MEDIA_DISPATCH: dict[str, tuple[str, str, str, bool]] = {
        "photo":     ("sendPhoto",     "photo",     "photo", True),
        "animation": ("sendAnimation", "animation", "photo", False),
        "document":  ("sendDocument",  "document",  "doc", False),
        "video":     ("sendVideo",     "video",     "doc", False),
        "audio":     ("sendAudio",     "audio",     "doc", False),
        "voice":     ("sendVoice",     "voice",     "doc", False),
    }
    _VALIDATORS = {"photo": lambda p: validate_photo_path(p), "doc": lambda p: validate_document_path(p)}

    def _dispatch_media(self, kind: str, chat_id: ChatId, path: str | Path,
                        caption: str | None = None) -> bool:
        if not BOT_TOKEN:
            return False
        api_method, field, vkey, _ = self._MEDIA_DISPATCH[kind]
        ok, validated = self._VALIDATORS[vkey](path)
        if not ok:
            _log(_LOG_WARN, "telegram", validated)
            return False
        file_data: bytes | None = None
        filename: str | None = None
        mime_type: str | None = None
        if kind == "photo":
            file_data, filename = _prepare_photo_for_telegram(validated)
            mime_type = mimetypes.guess_type(str(validated))[0] or "image/jpeg"
        elif kind == "animation":
            mime_type = "video/mp4" if Path(validated).suffix.lower() == ".mp4" else "image/gif"
        return self._send_media_multipart(chat_id, validated, field, api_method, caption,
                                          file_data=file_data, filename=filename, mime_type=mime_type)

    def send_photo(self, chat_id: ChatId, photo_path: str | Path,
                   caption: str | None = None) -> bool:
        return self._dispatch_media("photo", chat_id, photo_path, caption)

    def send_animation(self, chat_id: ChatId, animation_path: str | Path,
                       caption: str | None = None) -> bool:
        return self._dispatch_media("animation", chat_id, animation_path, caption)

    def send_document(self, chat_id: ChatId, doc_path: str | Path,
                      caption: str | None = None) -> bool:
        return self._dispatch_media("document", chat_id, doc_path, caption)

    def _send_media_multipart(self, chat_id: ChatId, file_path: Path | str,
                              field_name: str, api_method: str,
                              caption: str | None = None,
                              file_data: bytes | None = None,
                              filename: str | None = None,
                              mime_type: str | None = None) -> bool:
        if not BOT_TOKEN:
            return False
        file_path = Path(file_path)
        data = file_data if file_data is not None else file_path.read_bytes()
        fname = filename or file_path.name
        ctype = mime_type or mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        boundary = uuid.uuid4().hex
        body_parts: list[bytes] = [
            f"--{boundary}".encode(),
            b'Content-Disposition: form-data; name="chat_id"',
            b"",
            str(chat_id).encode(),
            f"--{boundary}".encode(),
            f'Content-Disposition: form-data; name="{field_name}"; filename="{fname}"'.encode(),
            f"Content-Type: {ctype}".encode(),
            b"",
            data,
        ]
        if caption:
            body_parts.extend([
                f"--{boundary}".encode(),
                b'Content-Disposition: form-data; name="caption"',
                b"",
                caption.encode(),
            ])
        body_parts.append(f"--{boundary}--".encode())
        body_parts.append(b"")
        body = b"\r\n".join(body_parts)
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{BOT_TOKEN}/{api_method}",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
            )
            with _urlopen(req, timeout=TIMEOUT_HTTP_UPLOAD) as r:
                result = cast(TelegramApiResponseDict, json.loads(r.read()))
                if result.get("ok"):
                    _log(_LOG_INFO, "telegram", f"{api_method} sent: {fname}")
                    return True
                else:
                    _log(_LOG_WARN, "bridge", f"{api_method} failed: {result}")
                    return False
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            _log(_LOG_ERROR, "bridge", f"{api_method} error: {e}")
            return False

    def send_video(self, chat_id: ChatId, video_path: str | Path,
                   caption: str | None = None) -> bool:
        return self._dispatch_media("video", chat_id, video_path, caption)

    def send_audio(self, chat_id: ChatId, audio_path: str | Path,
                   caption: str | None = None) -> bool:
        return self._dispatch_media("audio", chat_id, audio_path, caption)

    def send_voice(self, chat_id: ChatId, voice_path: str | Path,
                   caption: str | None = None) -> bool:
        return self._dispatch_media("voice", chat_id, voice_path, caption)

    def send_sticker(self, chat_id: ChatId, sticker_path: str | Path) -> bool:
        sticker_path = Path(sticker_path)
        if not sticker_path.exists() or not sticker_path.is_file():
            _log(_LOG_WARN, "telegram", f"Sticker not found: {sticker_path}")
            return False
        return self._send_media_multipart(chat_id, sticker_path, "sticker", "sendSticker")

    def send_chat_action(self, chat_id: ChatId, action: str) -> None:
        telegram_api("sendChatAction", {"chat_id": chat_id, "action": action})

    def set_reaction(self, chat_id: ChatId, message_id: MessageId,
                     reaction: list[dict[str, str]]) -> None:
        telegram_api("setMessageReaction", {"chat_id": chat_id, "message_id": message_id, "reaction": reaction})

    def edit_message(self, chat_id: ChatId, message_id: MessageId, text: str,
                     parse_mode: ParseMode = None) -> TelegramApiResponse:
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return telegram_api("editMessageText", payload)

    def setup_commands(self, commands: list[dict[str, str]]) -> None:
        telegram_api("setMyCommands", {"commands": commands})

    def download_file(self, file_id: str, session_name: str) -> str | None:
        if not BOT_TOKEN:
            return None
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
                data=json.dumps({"file_id": file_id}).encode(),
                headers={"Content-Type": "application/json"}
            )
            with _urlopen(req, timeout=TIMEOUT_HTTP_DOWNLOAD) as r:
                result = cast(TelegramApiResponseDict, json.loads(r.read()))
                if not result.get("ok"):
                    _log(_LOG_WARN, "bridge", f"getFile failed: {result}")
                    return None
                _file_result = result.get("result", {})
                file_info = _file_result if isinstance(_file_result, dict) else {}
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            _log(_LOG_ERROR, "bridge", f"getFile error: {e}")
            return None
        file_path = str(file_info.get("file_path", ""))
        file_size = int(file_info.get("file_size", 0))
        if not file_path:
            _log(_LOG_WARN, "telegram", "No file_path in response")
            return None
        if file_size > MAX_FILE_SIZE:
            _log(_LOG_WARN, "telegram", f"File too large: {file_size} > {MAX_FILE_SIZE}")
            return None
        download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        import bridge as _br
        import claudecode as _cc
        inbox = _br.ensure_inbox_dir(session_name)
        ext = Path(file_path).suffix or ""
        local_filename = f"{uuid.uuid4().hex}{ext}"
        local_path = inbox / local_filename
        try:
            req = urllib.request.Request(download_url)
            with _urlopen(req, timeout=TIMEOUT_HTTP_UPLOAD) as r:
                content = r.read()
                if len(content) > MAX_FILE_SIZE:
                    _log(_LOG_WARN, "telegram", f"Downloaded file too large: {len(content)}")
                    return None
                local_path.write_bytes(content)
                local_path.chmod(0o600)
            _log(_LOG_INFO, "telegram", f"Downloaded file: {local_path}")
            host = _br.get_worker_host(session_name)
            if host:
                remote_inbox = str(inbox)
                _cc._remote_run(["mkdir", "-p", remote_inbox], host=host, capture_output=True, timeout=TIMEOUT_TMUX_CHECK)
                _cc._remote_run(["chmod", "700", remote_inbox], host=host, capture_output=True, timeout=TIMEOUT_TMUX_CHECK)
                rsync_result = _subprocess_runner.run(
                    ["rsync", "-az", str(local_path), f"{host}:{remote_inbox}/"],
                    capture_output=True, text=True, timeout=TIMEOUT_FILE_TRANSFER)
                if rsync_result.returncode != 0:
                    _log(_LOG_ERROR, "bridge", f"rsync inbound failed (exit {rsync_result.returncode}): {host}:{remote_inbox}/ -> {rsync_result.stderr.strip()}")
            return str(local_path)
        except (subprocess.SubprocessError, OSError) as e:
            _log(_LOG_ERROR, "bridge", f"Download error: {e}")
            return None

class LocalTransport(MessageTransport):

    def __init__(self) -> None:
        self._log_file: str = os.environ.get("TRANSPORT_LOG", "")

    @property
    def name(self) -> str:
        return "local"

    def _log(self, method: str, chat_id: ChatId, **kwargs: object) -> None:
        msg = f"{method} chat_id={chat_id}"
        for k, v in kwargs.items():
            if v is not None:
                msg += f" {k}={v}"
        _log(_LOG_DEBUG, "local-transport", msg)
        if self._log_file:
            with open(self._log_file, "a") as f:
                f.write(msg + "\n")
    _MSG_OK: TelegramApiResponse = {"ok": True, "result": {"message_id": 1}}

    def send_text(self, chat_id: ChatId, text: str, parse_mode: ParseMode = None, reply_to: MessageId | None = None) -> TelegramApiResponse:
        self._log("send_text", chat_id, text=text[:200]); return self._MSG_OK
    def send_rich_text(self, chat_id: ChatId, markdown: str, reply_to: MessageId | None = None) -> TelegramApiResponse:
        self._log("send_rich_text", chat_id, text=markdown[:200]); return self._MSG_OK
    def edit_message(self, chat_id: ChatId, message_id: MessageId, text: str, parse_mode: ParseMode = None) -> TelegramApiResponse:
        self._log("edit_message", chat_id, message_id=message_id); return {"ok": True, "result": {"message_id": message_id}}
    def send_photo(self, chat_id: ChatId, p: str | Path, caption: str | None = None) -> bool:
        self._log("send_photo", chat_id, path=p); return True
    def send_document(self, chat_id: ChatId, p: str | Path, caption: str | None = None) -> bool:
        self._log("send_document", chat_id, path=p); return True
    def send_animation(self, chat_id: ChatId, p: str | Path, caption: str | None = None) -> bool:
        self._log("send_animation", chat_id, path=p); return True
    def send_video(self, chat_id: ChatId, p: str | Path, caption: str | None = None) -> bool:
        self._log("send_video", chat_id, path=p); return True
    def send_audio(self, chat_id: ChatId, p: str | Path, caption: str | None = None) -> bool:
        self._log("send_audio", chat_id, path=p); return True
    def send_voice(self, chat_id: ChatId, p: str | Path, caption: str | None = None) -> bool:
        self._log("send_voice", chat_id, path=p); return True
    def send_sticker(self, chat_id: ChatId, p: str | Path) -> bool:
        self._log("send_sticker", chat_id, path=p); return True
    def send_chat_action(self, chat_id: ChatId, action: str) -> None:
        self._log("send_chat_action", chat_id, action=action)
    def set_reaction(self, chat_id: ChatId, message_id: MessageId, reaction: list[dict[str, str]]) -> None:
        self._log("set_reaction", chat_id, message_id=message_id)
    def setup_commands(self, commands: list[dict[str, str]]) -> None:
        self._log("setup_commands", 0, count=len(commands))
    def download_file(self, file_id: str, session_name: str) -> str | None:
        self._log("download_file", 0, file_id=file_id); return None

def _init_transport() -> MessageTransport:
    if TRANSPORT_MODE == "local":
        return LocalTransport()
    return TelegramTransport(BOT_TOKEN)

transport = _init_transport()

def telegram_api(method: str, data: Mapping[str, object]) -> TelegramApiResponse:
    if TRANSPORT_MODE == "local":
        _log(_LOG_INFO, "local-transport", f"telegram_api {method} {str(data)[:100]}")
        return {"ok": True, "result": {"message_id": 1}}
    if isinstance(transport, TelegramTransport):
        return transport._api.api(method, data)
    return None

def send_telegram_message(chat_id: ChatId, text: str,
                          parse_mode: ParseMode = None) -> TelegramApiResponse:
    return transport.send_text(chat_id, text, parse_mode=parse_mode)

def download_telegram_file(file_id: str, session_name: str | None) -> str | None:
    if session_name is None:
        return None
    return transport.download_file(file_id, session_name)

def send_voice(chat_id: ChatId, path: str, caption: str | None = None) -> bool: return transport.send_voice(chat_id, path, caption)
def send_photo(chat_id: ChatId, path: str, caption: str | None = None) -> bool: return transport.send_photo(chat_id, path, caption)
def send_animation(chat_id: ChatId, path: str, caption: str | None = None) -> bool: return transport.send_animation(chat_id, path, caption)
def send_document(chat_id: ChatId, path: str, caption: str | None = None) -> bool: return transport.send_document(chat_id, path, caption)
def send_video(chat_id: ChatId, path: str, caption: str | None = None) -> bool: return transport.send_video(chat_id, path, caption)
def send_audio(chat_id: ChatId, path: str, caption: str | None = None) -> bool: return transport.send_audio(chat_id, path, caption)
def send_sticker(chat_id: ChatId, path: str) -> bool: return transport.send_sticker(chat_id, path)

# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
MAX_FILE_SIZE = 50 * 1024 * 1024
ALLOWED_IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp",
    ".gif", ".mp4",
}
ALLOWED_DOC_EXTENSIONS = {
    ".md", ".txt", ".rst", ".pdf",
    ".json", ".csv", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml",
    ".log", ".sql", ".patch", ".diff",
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".go", ".rs", ".java", ".kt", ".swift",
    ".rb", ".php", ".c", ".cpp", ".h", ".hpp",
    ".sh", ".html", ".css", ".scss",
    ".zip", ".tar", ".gz",
    ".mp3", ".m4a", ".flac", ".aac", ".wav",
    ".ogg", ".opus", ".oga",
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".tgs",
}
BLOCKED_DOC_EXTENSIONS = {
    ".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".der",
    ".jks", ".keystore", ".kdb", ".pgp", ".gpg", ".asc",
}
BLOCKED_FILENAMES = {
    ".env", ".npmrc", ".pypirc", ".netrc", ".git-credentials",
    "id_rsa", "id_ed25519", "id_dsa", "credentials", "kubeconfig",
}

def format_file_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"

def transcribe_voice(file_path: str, timeout: int | None = None) -> str | None:
    if not STT_ENDPOINT:
        return None
    if timeout is None:
        timeout = STT_TIMEOUT

    try:
        file_path_obj = Path(file_path)
        if not file_path_obj.exists():
            return None

        boundary = uuid.uuid4().hex
        body_parts = []
        body_parts.append(f"--{boundary}".encode())
        content_type = mimetypes.guess_type(str(file_path_obj))[0] or "audio/ogg"
        body_parts.append(f'Content-Disposition: form-data; name="file"; filename="{file_path_obj.name}"'.encode())
        body_parts.append(f"Content-Type: {content_type}".encode())
        body_parts.append(b"")
        body_parts.append(file_path_obj.read_bytes())
        body_parts.append(f"--{boundary}--".encode())
        body_parts.append(b"")
        body = b"\r\n".join(body_parts)
        req = urllib.request.Request(
            STT_ENDPOINT,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        with _urlopen(req, timeout=timeout) as r:
            result = cast(TelegramApiResponseDict, json.loads(r.read()))
            text = str(result.get("text", "")).strip()
            if text:
                duration = str(result.get("audio_duration_s", "?"))
                _log(_LOG_INFO, "stt", f"STT transcribed: {len(text)} chars from {duration}s audio")
                return text
            return None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        _log(_LOG_ERROR, "bridge", f"STT error (fail-open): {e}")
        return None

TELEGRAM_PHOTO_MAX_SUM = 10000
TELEGRAM_PHOTO_MAX_DIM = 5000

def _prepare_photo_for_telegram(photo_path: str | Path) -> tuple[bytes, str]:
    photo_path = Path(photo_path)
    try:
        from PIL import Image
        with Image.open(photo_path) as img:
            w, h = img.size
            needs_resize = (
                w + h > TELEGRAM_PHOTO_MAX_SUM or
                w > TELEGRAM_PHOTO_MAX_DIM or
                h > TELEGRAM_PHOTO_MAX_DIM
            )
            if needs_resize:
                scale = min(
                    TELEGRAM_PHOTO_MAX_DIM / max(w, 1),
                    TELEGRAM_PHOTO_MAX_DIM / max(h, 1),
                    TELEGRAM_PHOTO_MAX_SUM / max(w + h, 1),
                )
                new_w = int(w * scale)
                new_h = int(h * scale)
                resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                import io
                buf = io.BytesIO()
                fmt = img.format or ("PNG" if photo_path.suffix.lower() == ".png" else "JPEG")
                if fmt == "PNG" and resized.mode not in ("RGBA", "P", "L", "LA"):
                    resized = resized.convert("RGBA")
                elif fmt == "JPEG" and resized.mode != "RGB":
                    resized = resized.convert("RGB")
                resized.save(buf, format=fmt)
                _log(_LOG_INFO, "telegram", f"Photo auto-resized: {w}x{h} -> {new_w}x{new_h} for Telegram")
                return buf.getvalue(), photo_path.name
        return photo_path.read_bytes(), photo_path.name
    except ImportError:
        return photo_path.read_bytes(), photo_path.name

def _validate_file_path(path: str | Path, label: str,
                        allowed_exts: set[str] | None = None,
                        blocked_exts: set[str] | None = None,
                        check_blocked_name: bool = False) -> FileValidation:
    p = Path(path)
    if not p.exists():
        return FileValidation(False, f"{label} not found: {p}")
    if not p.is_file():
        return FileValidation(False, f"Not a file: {p}")
    ext = p.suffix.lower()
    if allowed_exts and ext not in allowed_exts:
        return FileValidation(False, f"Invalid {label.lower()} extension: {p.suffix}")
    if blocked_exts and ext in blocked_exts:
        return FileValidation(False, f"Blocked extension (sensitive): {p.suffix}")
    if check_blocked_name and is_blocked_filename(p.name):
        return FileValidation(False, f"Blocked filename (sensitive): {p.name}")
    if p.stat().st_size > MAX_FILE_SIZE:
        return FileValidation(False, f"{label} too large: {p.stat().st_size} > {MAX_FILE_SIZE}")
    return FileValidation(True, p)

def validate_photo_path(photo_path: str | Path) -> FileValidation:
    return _validate_file_path(photo_path, "Photo", allowed_exts=ALLOWED_IMAGE_EXTENSIONS)

def is_blocked_filename(filename: str) -> bool:
    name_lower = filename.lower()
    return name_lower in BLOCKED_FILENAMES or name_lower.startswith(".env")

def validate_document_path(doc_path: str | Path) -> FileValidation:
    return _validate_file_path(doc_path, "Document", blocked_exts=BLOCKED_DOC_EXTENSIONS,
                               check_blocked_name=True)
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".flac", ".aac", ".wav"}
VOICE_EXTENSIONS = {".ogg", ".opus", ".oga"}
STICKER_EXTENSIONS = {".tgs"}
CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")

def _split_protected_segments(text: str, pattern: re.Pattern[str]) -> list[tuple[str, bool]]:
    segments = []
    last = 0
    for match in pattern.finditer(text):
        if match.start() > last:
            segments.append((text[last:match.start()], False))
        segments.append((match.group(0), True))
        last = match.end()
    if last < len(text):
        segments.append((text[last:], False))
    return segments

def _collapse_excess_newlines(text: str) -> str:
    output = []
    for segment, protected in _split_protected_segments(text, CODE_FENCE_RE):
        if protected:
            output.append(segment)
            continue
        for inline_segment, inline_protected in _split_protected_segments(segment, INLINE_CODE_RE):
            if inline_protected:
                output.append(inline_segment)
            else:
                output.append(re.sub(r"\n{3,}", "\n\n", inline_segment))
    return "".join(output)

def _parse_media_tags(text: str, tag_name: str, validate_func: Callable[[str | Path], FileValidation]) -> tuple[str, list[tuple[str | None, str]]]:
    pattern = re.compile(rf"(\\)?\[\[{tag_name}:([^\]|]+)(?:\|([^\]]*))?\]\]")
    items = []
    removed = 0

    def replace_tag(match: re.Match[str]) -> str:
        nonlocal removed
        if match.group(1):
            return match.group(0)[1:]
        path = match.group(2).strip()
        caption = (match.group(3) or "").strip()
        ok, _ = validate_func(path)
        if ok:
            items.append((path, caption))
            removed += 1
            return ""
        return match.group(0)

    output = []
    for segment, protected in _split_protected_segments(text, CODE_FENCE_RE):
        if protected:
            output.append(segment)
            continue
        for inline_segment, inline_protected in _split_protected_segments(segment, INLINE_CODE_RE):
            if inline_protected:
                output.append(inline_segment)
            else:
                output.append(pattern.sub(replace_tag, inline_segment))
    clean_text = "".join(output)
    if removed:
        clean_text = _collapse_excess_newlines(clean_text).strip()
    return clean_text, items

def parse_image_tags(text: str) -> tuple[str, list[tuple[str | None, str]]]:
    return _parse_media_tags(text, "image", validate_photo_path)

def parse_file_tags(text: str) -> tuple[str, list[tuple[str | None, str]]]:
    return _parse_media_tags(text, "file", validate_document_path)

def escape_html(text: str) -> str:
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

class _TelegramHTMLSanitizer(HTMLParser):
    SAFE_TAGS: frozenset[str] = frozenset({
        "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
        "code", "pre", "a", "blockquote", "span", "tg-emoji", "tg-spoiler",
    })
    SAFE_ATTRS: dict[str, frozenset[str]] = {
        "a": frozenset({"href"}),
        "code": frozenset({"class"}),
        "blockquote": frozenset({"expandable"}),
        "span": frozenset({"class"}),
        "tg-emoji": frozenset({"emoji-id"}),
    }

    def __init__(self, rejected_open_tags: list[str]) -> None:
        super().__init__(convert_charrefs=False)
        self._out: list[str] = []
        self._rejected_open_tags = rejected_open_tags

    def _escape_attr(self, value: str) -> str:
        return escape_html(value).replace('"', "&quot;")

    def _attrs_are_safe(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        allowed = self.SAFE_ATTRS.get(tag, frozenset())
        seen: set[str] = set()
        for name, value in attrs:
            if name in seen or name not in allowed:
                return False
            seen.add(name)
            if tag == "a" and name == "href":
                if value is None:
                    return False
            elif tag == "code" and name == "class":
                if value is None or not value.startswith("language-"):
                    return False
            elif tag == "blockquote" and name == "expandable":
                if value not in (None, "", "expandable"):
                    return False
            elif tag == "span" and name == "class":
                if value != "tg-spoiler":
                    return False
            elif tag == "tg-emoji" and name == "emoji-id":
                if value is None:
                    return False
        return True

    def _render_start_tag(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        if not attrs:
            return f"<{tag}>"
        rendered = []
        for name, value in attrs:
            if value is None:
                rendered.append(name)
            else:
                rendered.append(f'{name}="{self._escape_attr(value)}"')
        return f"<{tag} {' '.join(rendered)}>"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        accepted = tag in self.SAFE_TAGS and self._attrs_are_safe(tag, attrs)
        if accepted:
            self._out.append(self._render_start_tag(tag, attrs))
        else:
            self._out.append(escape_html(self.get_starttag_text() or f"<{tag}>"))
            self._rejected_open_tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        rejected_match = False
        for idx in range(len(self._rejected_open_tags) - 1, -1, -1):
            if self._rejected_open_tags[idx] == tag:
                rejected_match = True
                del self._rejected_open_tags[idx]
                break
        if tag in self.SAFE_TAGS and not rejected_match:
            self._out.append(f"</{tag}>")
        else:
            self._out.append(escape_html(f"</{tag}>"))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        accepted = tag in self.SAFE_TAGS and self._attrs_are_safe(tag, attrs)
        if accepted:
            start = self._render_start_tag(tag, attrs)
            self._out.append(f"{start[:-1]}/>")
        else:
            self._out.append(escape_html(self.get_starttag_text() or f"<{tag}/>"))

    def handle_data(self, data: str) -> None:
        self._out.append(escape_html(data))

    def handle_entityref(self, name: str) -> None:
        self._out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._out.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self._out.append(escape_html(f"<!--{data}-->"))

    def html(self) -> str:
        return "".join(self._out)

def _sanitize_telegram_html(raw: str, rejected_open_tags: list[str]) -> str:
    if not raw:
        return ""
    sanitizer = _TelegramHTMLSanitizer(rejected_open_tags)
    sanitizer.feed(raw)
    sanitizer.close()
    return sanitizer.html()

def _render_md_inline_plain(children: list[MarkdownToken]) -> str:
    out: list[str] = []
    for tok in children:
        if tok.type in ("text", "code_inline"):
            out.append(tok.content)
        elif tok.type in ("softbreak", "hardbreak"):
            out.append(" ")
        elif tok.type == "image":
            out.append(tok.content or "image")
        elif tok.type in ("strong_open", "strong_close", "em_open", "em_close",
                          "s_open", "s_close", "link_open", "link_close",
                          "html_inline"):
            pass
        else:
            if tok.content:
                out.append(tok.content)
    return "".join(out)

_INLINE_TAG_MAP = {"strong_open": "<b>", "strong_close": "</b>", "em_open": "<i>",
                   "em_close": "</i>", "s_open": "<s>", "s_close": "</s>",
                   "link_close": "</a>", "softbreak": "\n", "hardbreak": "\n"}

def _render_md_inline_html(children: list[MarkdownToken], rejected_open_tags: list[str]) -> str:
    out: list[str] = []
    for tok in children:
        if tok.type == "text":
            out.append(escape_html(tok.content))
        elif tok.type == "code_inline":
            out.append(f"<code>{escape_html(tok.content)}</code>")
        elif tok.type in _INLINE_TAG_MAP:
            out.append(_INLINE_TAG_MAP[tok.type])
        elif tok.type == "link_open":
            href = escape_html((tok.attrs or {}).get("href", ""))
            out.append(f'<a href="{href}">')
        elif tok.type == "image":
            alt = escape_html(tok.content or "image")
            src = escape_html((tok.attrs or {}).get("src", ""))
            out.append(f'[{alt}]({src})')
        elif tok.type == "html_inline":
            out.append(_sanitize_telegram_html(tok.content, rejected_open_tags))
        elif tok.content:
            out.append(escape_html(tok.content))
    return "".join(out)

def _render_table_as_pre(headers: list[str], rows: list[list[str]]) -> str:
    all_rows = [headers] + rows
    if not all_rows or not all_rows[0]:
        return ""
    num_cols = max(len(r) for r in all_rows)
    col_widths = [0] * num_cols
    for row in all_rows:
        for ci, cell in enumerate(row):
            if ci < num_cols:
                col_widths[ci] = max(col_widths[ci], len(cell))
    lines: list[str] = []
    for ri, row in enumerate(all_rows):
        cols = []
        for ci in range(num_cols):
            cell = row[ci] if ci < len(row) else ""
            cols.append(escape_html(cell.ljust(col_widths[ci])))
        lines.append("  ".join(cols).rstrip())
        if ri == 0:
            lines.append("\u2550" * (sum(col_widths) + 2 * (num_cols - 1)))
    return f"<pre>{chr(10).join(lines)}</pre>\n"

_MULTI_SPACE_RE = re.compile(r'\S  +\S.*\S  +\S')

def _wrap_plain_tables(text: str) -> str:
    parts = re.split(r'(<pre>.*?</pre>)', text, flags=re.DOTALL)
    out: list[str] = []
    for part in parts:
        if part.startswith('<pre>'):
            out.append(part)
            continue
        lines = part.split('\n')
        i = 0
        while i < len(lines):
            if _MULTI_SPACE_RE.search(lines[i]):
                run = [lines[i]]
                j = i + 1
                while j < len(lines) and (_MULTI_SPACE_RE.search(lines[j]) or lines[j].strip() == ''):
                    run.append(lines[j])
                    j += 1
                tabular_count = sum(1 for line in run if _MULTI_SPACE_RE.search(line))
                if tabular_count >= 2:
                    while run and run[-1].strip() == '':
                        j -= 1
                        run.pop()
                    raw = []
                    for rl in run:
                        plain = re.sub(r'<[^>]+>', '', rl)
                        plain = plain.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
                        raw.append(plain)
                    content = escape_html('\n'.join(raw))
                    out.append(f'<pre>{content}</pre>')
                    i = j
                else:
                    out.append(lines[i])
                    i += 1
            else:
                out.append(lines[i])
                i += 1
    return '\n'.join(out) if out else text

def markdown_to_telegram_html(text: str) -> str:
    from markdown_it import MarkdownIt
    md = MarkdownIt("commonmark").enable("strikethrough").enable("table")
    tokens = md.parse(text)
    result: list[str] = []
    list_depth = 0
    ordered_counter: list[int] = []
    in_table = False
    table_row: list[str] = []
    table_headers: list[str] = []
    table_rows: list[list[str]] = []
    in_thead = False
    rejected_open_tags: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]

        if tok.type == "paragraph_open":
            pass
        elif tok.type == "paragraph_close":
            if not in_table:
                result.append("\n")
        elif tok.type == "inline":
            if in_table:
                table_row.append(_render_md_inline_plain(cast(list[MarkdownToken], tok.children or [])))
            else:
                result.append(_render_md_inline_html(cast(list[MarkdownToken], tok.children or []), rejected_open_tags))

        elif tok.type == "heading_open":
            result.append("<b>")
        elif tok.type == "heading_close":
            result.append("</b>\n")

        elif tok.type == "fence":
            lang = tok.info.strip() if tok.info else ""
            code = escape_html(tok.content.rstrip("\n"))
            if lang:
                result.append(f'<pre><code class="language-{escape_html(lang)}">{code}</code></pre>\n')
            else:
                result.append(f"<pre>{code}</pre>\n")
        elif tok.type == "code_block":
            code = escape_html(tok.content.rstrip("\n"))
            result.append(f"<pre>{code}</pre>\n")

        elif tok.type == "blockquote_open":
            result.append("<blockquote>")
        elif tok.type == "blockquote_close":
            if result and result[-1].endswith("\n"):
                result[-1] = result[-1][:-1]
            result.append("</blockquote>\n")

        elif tok.type == "bullet_list_open":
            list_depth += 1
        elif tok.type == "bullet_list_close":
            list_depth -= 1
            if list_depth == 0:
                result.append("\n")

        elif tok.type == "ordered_list_open":
            list_depth += 1
            ordered_counter.append(0)
        elif tok.type == "ordered_list_close":
            list_depth -= 1
            ordered_counter.pop()
            if list_depth == 0:
                result.append("\n")

        elif tok.type == "list_item_open":
            indent = "  " * (list_depth - 1)
            if ordered_counter:
                ordered_counter[-1] += 1
                result.append(f"{indent}{ordered_counter[-1]}. ")
            else:
                result.append(f"{indent}\u2022 ")
        elif tok.type == "list_item_close":
            if result and not result[-1].endswith("\n"):
                result.append("\n")

        elif tok.type == "table_open":
            in_table = True
            table_headers = []
            table_rows = []
        elif tok.type == "table_close":
            in_table = False
            pre_block = _render_table_as_pre(table_headers, table_rows)
            if pre_block:
                result.append(pre_block)
            table_headers = []
            table_rows = []
        elif tok.type == "thead_open":
            in_thead = True
        elif tok.type == "thead_close":
            in_thead = False
        elif tok.type in ("tbody_open", "tbody_close"):
            pass
        elif tok.type == "tr_open":
            table_row = []
        elif tok.type == "tr_close":
            if in_thead:
                table_headers = table_row[:]
            else:
                table_rows.append(table_row[:])
            table_row = []
        elif tok.type in ("th_open", "th_close", "td_open", "td_close"):
            pass

        elif tok.type == "hr":
            result.append("\u2014\u2014\u2014\u2014\n")

        elif tok.type == "html_block":
            result.append(_sanitize_telegram_html(tok.content, rejected_open_tags))

        else:
            if tok.content:
                result.append(escape_html(tok.content))
        i += 1
    output = "".join(result).strip()
    while "\n\n\n" in output:
        output = output.replace("\n\n\n", "\n\n")
    return _wrap_plain_tables(output)

def _pipe_tables_to_html(text: str) -> str:
    import re

    def _parse_row(line: str) -> list[str] | None:
        line = line.strip()
        if line.startswith('|'):
            line = line[1:]
        if line.endswith('|'):
            line = line[:-1]
        return [cell.strip() for cell in line.split('|')]

    def _esc(s: str) -> str:
        return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    def _cell_md(s: str) -> str:
        s = _esc(s)
        s = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', s)
        s = re.sub(r'__(.+?)__', r'<b>\1</b>', s)
        s = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', s)
        s = re.sub(r'~~(.+?)~~', r'<s>\1</s>', s)
        s = re.sub(r'`([^`]+)`', r'<code>\1</code>', s)
        s = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', s)
        return s

    lines = text.split('\n')
    result = []
    i = 0
    in_code_fence = False
    while i < len(lines):
        if re.match(r'^\s*(`{3,}|~{3,})', lines[i]):
            in_code_fence = not in_code_fence
            result.append(lines[i])
            i += 1
            continue

        if (not in_code_fence
                and i + 1 < len(lines)
                and '|' in lines[i]
                and re.match(r'^\s*\|[\s:]*-+[\s:]*(\|[\s:]*-+[\s:]*)*\|?\s*$', lines[i + 1])):
            headers = _parse_row(lines[i])
            aligns = []
            for cell in (_parse_row(lines[i + 1]) or []):
                cell = cell.strip()
                if cell.startswith(':') and cell.endswith(':'):
                    aligns.append(' style="text-align:center"')
                elif cell.endswith(':'):
                    aligns.append(' style="text-align:right"')
                else:
                    aligns.append('')
            html = ['<table>']
            html.append('<tr>')
            for j, h in enumerate(headers or []):
                align = aligns[j] if j < len(aligns) else ''
                html.append(f'<th{align}>{_cell_md(h)}</th>')
            html.append('</tr>')
            i += 2
            while i < len(lines) and '|' in lines[i] and lines[i].strip().startswith('|'):
                cells = _parse_row(lines[i])
                html.append('<tr>')
                for j, cell in enumerate(cells or []):
                    align = aligns[j] if j < len(aligns) else ''
                    html.append(f'<td{align}>{_cell_md(cell)}</td>')
                html.append('</tr>')
                i += 1
            html.append('</table>')
            result.append('\n'.join(html))
        else:
            result.append(lines[i])
            i += 1
    return '\n'.join(result)

def format_response_text(session_name: str, text: str) -> str:
    stripped = text.lstrip()
    prefix = f"{session_name}:"
    if stripped.lower().startswith(prefix.lower()):
        text = stripped[len(prefix):].lstrip()
    return f"<b>{session_name}:</b>\n{text}"

# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
TELEGRAM_MAX_LENGTH = 4096
TELEGRAM_RICH_MAX_LENGTH = 32768

def split_message(text: str, max_len: int=TELEGRAM_MAX_LENGTH) -> list[str]:
    import re
    if len(text) <= max_len:
        return [text]

    TAG_RE = re.compile(r'<(/?)(\w+)([^>]*)>')
    TRACKED_TAGS = frozenset(('b', 'i', 's', 'u', 'code', 'pre', 'a',
                              'strong', 'em', 'del', 'ins', 'strike', 'blockquote'))

    def _closing_tags(stack: list[tuple[str, str]]) -> str:
        return "".join(f"</{tag}>" for tag, _ in reversed(stack))

    def _opening_tags(stack: list[tuple[str, str]]) -> str:
        return "".join(full for _, full in stack)

    def _scan_tags(text: str) -> list[tuple[str, str]]:
        stack: list[tuple[str, str]] = []
        for m in TAG_RE.finditer(text):
            is_close = m.group(1) == '/'
            tag_name = m.group(2).lower()
            if tag_name not in TRACKED_TAGS:
                continue
            if is_close:
                for j in range(len(stack) - 1, -1, -1):
                    if stack[j][0] == tag_name:
                        stack.pop(j)
                        break
            else:
                stack.append((tag_name, m.group(0)))
        return stack

    def _find_split(text: str, budget: int) -> int:
        if budget <= 0:
            budget = 1
        search = text[:budget]
        last_tag_start = search.rfind('<')
        last_tag_end = search.rfind('>')
        if last_tag_start > last_tag_end:
            search = text[:last_tag_start]
            budget = last_tag_start

        for sep in ('\n\n', '\n', ' '):
            pos = search.rfind(sep)
            if pos > budget // 3:
                return pos + 1

        return max(budget, 1)

    chunks = []
    remaining = text
    carry_stack: list[tuple[str, str]] = []

    while remaining:
        prefix = _opening_tags(carry_stack)
        available = max_len - len(prefix)

        if len(prefix) + len(remaining) + len(_closing_tags(carry_stack)) <= max_len:
            suffix = _closing_tags(_scan_tags(prefix + remaining))
            chunks.append(prefix + remaining + suffix)
            break

        budget = available - 100
        if budget < 100:
            budget = 100

        for _attempt in range(5):
            split_at = _find_split(remaining, budget)
            chunk_text = remaining[:split_at].rstrip()
            full_chunk = prefix + chunk_text
            open_stack = _scan_tags(full_chunk)
            suffix = _closing_tags(open_stack)

            if len(full_chunk) + len(suffix) <= max_len:
                break
            overshoot = len(full_chunk) + len(suffix) - max_len
            budget = max(budget - overshoot - 20, 100)
        else:
            hard_limit = max_len - len(prefix) - len(suffix) - 10
            if hard_limit < 1:
                hard_limit = 1
            chunk_text = remaining[:hard_limit].rstrip()
            full_chunk = prefix + chunk_text
            open_stack = _scan_tags(full_chunk)
            suffix = _closing_tags(open_stack)
            split_at = hard_limit
        chunks.append(full_chunk + suffix)
        carry_stack = open_stack
        remaining = remaining[split_at:].lstrip()

        if split_at == 0:
            remaining = remaining[1:]
    return chunks

def format_multipart_messages(session_name: str, chunks: list[str]) -> list[str]:
    return [format_response_text(session_name, chunk) for chunk in chunks]

def setup_bot_commands() -> None:
    update_bot_commands()

def update_bot_commands() -> None:
    commands = list(BOT_COMMANDS)
    import bridge as _br
    registered = _br.get_registered_sessions()
    for name in sorted(registered.keys()):
        commands.append({"command": name, "description": f"Message {name}"})
    transport.setup_commands(commands)
    worker_count = len(registered)
    _log(_LOG_INFO, "telegram", f"Bot commands updated ({len(BOT_COMMANDS)} + {worker_count} workers)")

def get_manager_chat_id(name: str) -> ChatId | None:
    if admin_chat_id is not None:
        return admin_chat_id

    import claudecode as _cc
    chat_id_file = _cc.get_chat_id_file(name)
    if not chat_id_file.exists():
        return None

    try:
        value = chat_id_file.read_text().strip()
        return int(value) if value else None
    except OSError as e:
        _log(_LOG_WARN, "bridge", f"Failed to read chat_id for {name}: {e}")
        return None

class TunnelState(enum.Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    RESTARTING = "restarting"
    POLL_FALLBACK = "poll_fallback"
    FAILED = "failed"

class TunnelManager:

    def __init__(
        self,
        config: TunnelConfig,
        bot_token: str,
        port: int,
        node_dir: Path,
        webhook_secret: str = "",
        bind_host: str = "",
        subprocess_runner: SubprocessRunner | None = None,
        clock: Clock | None = None,
        urlopen: "UrlOpenFn | None" = None,
        on_notify: Callable[[str], None] | None = None,
        on_update: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self._config = config
        self._token = bot_token
        self._port = port
        self._node_dir = node_dir
        self._bind_host = bind_host
        self._webhook_secret = webhook_secret
        self._runner = subprocess_runner or _RealSubprocessRunner()
        self._clock = clock or _RealClock()
        self._urlopen: UrlOpenFn = urlopen or cast(UrlOpenFn, _urlopen)
        self._on_notify = on_notify or (lambda _msg: None)
        self._on_update = on_update
        self._lock = threading.Lock()
        self._state = TunnelState.STOPPED
        self._tunnel_url: str = ""
        self._tunnel_proc: subprocess.Popen[str] | None = None
        self._polling_active: bool = False
        self._stop_event = threading.Event()
        self._watchdog_thread: threading.Thread | None = None
        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None

    # ── Public API ────────────────────────────────────────────────────

    def start(self) -> None:
        if self._config.mode == "none":
            _log(_LOG_INFO, "tunnel", "Tunnel disabled (mode=none)")
            return
        self._kill_stale_tunnel()
        self._stop_event.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, name="tunnel-watchdog", daemon=True,
        )
        self._watchdog_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._poll_stop.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5)
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=10)
        self._kill_cloudflared()
        self._state = TunnelState.STOPPED
        _log(_LOG_INFO, "tunnel", "Tunnel manager stopped")

    @property
    def state(self) -> TunnelState:
        return self._state

    @property
    def tunnel_url(self) -> str:
        return self._tunnel_url

    @property
    def polling_active(self) -> bool:
        return self._polling_active

    def status(self) -> dict[str, str | bool]:
        return {
            "mode": self._config.mode,
            "state": self._state.value,
            "tunnel_url": self._tunnel_url,
            "polling_active": self._polling_active,
        }

    # ── Telegram API helpers (use injected urlopen) ────────────────────

    def _telegram_api(self, method: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        data = json.dumps(payload or {}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with self._urlopen(req, timeout=10) as resp:
                result: dict[str, object] = json.loads(resp.read())
                return result
        except Exception:
            return {}

    def _tg_set_webhook(self, webhook_url: str, secret_token: str = "") -> dict[str, object]:
        payload: dict[str, object] = {"url": webhook_url}
        if secret_token:
            payload["secret_token"] = secret_token
        return self._telegram_api("setWebhook", payload)

    def _tg_delete_webhook(self) -> dict[str, object]:
        return self._telegram_api("deleteWebhook")

    def _tg_get_webhook_info(self) -> dict[str, object]:
        return self._telegram_api("getWebhookInfo")

    def _tg_get_me(self) -> dict[str, object]:
        return self._telegram_api("getMe")

    # ── Watchdog loop ─────────────────────────────────────────────────

    def _watchdog_loop(self) -> None:
        self._state = TunnelState.STARTING

        if not self._wait_for_port():
            _log(_LOG_ERROR, "tunnel", f"Bridge did not bind to port {self._port}")
            self._state = TunnelState.FAILED
            return

        if self._config.mode == "poll":
            _log(_LOG_INFO, "tunnel", "Poll mode — using getUpdates long-polling (no tunnel)")
            self._start_poll_fallback()
        elif self._config.mode == "provided":
            self._tunnel_url = self._config.provided_url
            _log(_LOG_INFO, "tunnel", f"Using provided tunnel URL: {self._tunnel_url}")
        elif self._config.mode == "auto":
            url = self._start_cloudflared()
            if url:
                self._tunnel_url = url
                _log(_LOG_INFO, "tunnel", f"Tunnel ready: {self._tunnel_url}")
                self._save_tunnel_url(url)
            else:
                _log(_LOG_WARN, "tunnel", "Tunnel failed to start — using poll fallback")
                self._start_poll_fallback()

        if self._tunnel_url and not self._polling_active:
            if not self._set_webhook_with_retry(self._tunnel_url):
                _log(_LOG_WARN, "tunnel", "Webhook DNS not ready — using poll fallback")
                self._start_poll_fallback()
            else:
                self._state = TunnelState.RUNNING
        self._save_bot_info()
        check_counter = 0
        while not self._stop_event.is_set():
            self._stop_event.wait(self._config.watchdog_interval)
            if self._stop_event.is_set():
                break

            if self._tunnel_proc is not None:
                problem = self._check_tunnel_health()
                if problem == "process died":
                    _log(_LOG_WARN, "tunnel", "Tunnel process died, restarting...")
                    self._on_notify("⚠️ Tunnel process died. Reconnecting...")
                    self._state = TunnelState.RESTARTING
                    new_url = self._restart_with_retry()
                    if new_url:
                        self._tunnel_url = new_url
                        self._save_tunnel_url(new_url)
                        _log(_LOG_INFO, "tunnel", f"Tunnel restarted: {new_url}")
                        self._stop_poll_fallback()
                        if self._set_webhook_with_retry(new_url):
                            self._state = TunnelState.RUNNING
                            self._on_notify("✅ Tunnel reconnected")
                        else:
                            _log(_LOG_WARN, "tunnel", "Webhook DNS not ready after restart")
                            self._start_poll_fallback()
                            self._on_notify("📡 Using poll fallback while DNS propagates...")
                    else:
                        _log(_LOG_WARN, "tunnel", "Tunnel restart failed after retries")
                        self._start_poll_fallback()
                        continue
                elif problem == "unreachable":
                    if not self._polling_active:
                        _log(_LOG_WARN, "tunnel", "Tunnel URL unreachable (DNS?), falling back to polling")
                        self._start_poll_fallback()

            if self._polling_active and self._poll_thread and not self._poll_thread.is_alive():
                _log(_LOG_WARN, "tunnel", "Poll fallback thread died, restarting...")
                self._poll_stop.clear()
                self._start_poll_fallback()
            check_counter = (check_counter + 1) % self._config.webhook_check_cycles
            if check_counter == 0:
                if self._tunnel_url:
                    self._periodic_webhook_check()
                elif self._polling_active and self._tunnel_proc is None and self._config.mode == "auto":
                    _log(_LOG_INFO, "tunnel", "Retrying cloudflared start...")
                    url = self._start_cloudflared()
                    if url:
                        self._tunnel_url = url
                        self._save_tunnel_url(url)
                        _log(_LOG_INFO, "tunnel", f"Tunnel established: {url}")
                        self._stop_poll_fallback()
                        if self._set_webhook_with_retry(url):
                            self._state = TunnelState.RUNNING
                            self._on_notify("✅ Tunnel established (webhook active)")
                        else:
                            _log(_LOG_WARN, "tunnel", "Webhook DNS not ready after tunnel start")
                            self._start_poll_fallback()

    # ── Cloudflared management ────────────────────────────────────────

    def _start_cloudflared(self) -> str:
        log_file = self._node_dir / "tunnel.log"
        log_file.write_text("")

        try:
            proc = self._runner.popen(
                [self._config.cloudflared_binary, "tunnel", "--url", f"http://localhost:{self._port}"],
                stdout=open(log_file, "w"),
                stderr=subprocess.STDOUT,
            )
        except (OSError, FileNotFoundError) as exc:
            _log(_LOG_ERROR, "tunnel", f"Failed to start cloudflared: {exc}")
            return ""

        self._tunnel_proc = proc
        self._save_pid(proc.pid)
        url = self._wait_for_url(log_file, self._config.startup_timeout)
        if not url:
            self._kill_cloudflared()
        return url

    def _wait_for_url(self, log_file: Path, timeout: int) -> str:
        elapsed = 0
        while elapsed < timeout and not self._stop_event.is_set():
            self._clock.sleep(1)
            elapsed += 1
            try:
                text = log_file.read_text()
            except OSError:
                continue
            for line in text.splitlines():
                for word in line.split():
                    if word.startswith("https://") and ".trycloudflare.com" in word and "api.trycloudflare.com" not in word:
                        return word.rstrip("/")
        return ""

    def _kill_cloudflared(self) -> None:
        proc = self._tunnel_proc
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            self._tunnel_proc = None

    def _is_tunnel_alive(self) -> bool:
        proc = self._tunnel_proc
        return proc is not None and proc.poll() is None

    def _is_tunnel_reachable(self) -> bool:
        if not self._tunnel_url:
            return False
        try:
            req = urllib.request.Request(self._tunnel_url, method="HEAD")
            self._urlopen(req, timeout=self._config.reachability_timeout)
            return True
        except urllib.error.HTTPError:
            return True
        except (urllib.error.URLError, OSError, TimeoutError):
            return False

    def _check_tunnel_health(self) -> str:
        if not self._is_tunnel_alive():
            return "process died"
        if not self._is_tunnel_reachable():
            return "unreachable"
        return ""

    def _restart_with_retry(self) -> str:
        backoff = self._config.initial_backoff
        for attempt in range(1, self._config.max_restart_attempts + 1):
            if attempt > 1:
                _log(_LOG_INFO, "tunnel", f"Restart attempt {attempt}/{self._config.max_restart_attempts} (backoff {backoff}s)")
                self._clock.sleep(backoff)
                backoff *= 2
            self._kill_cloudflared()
            url = self._start_cloudflared()
            if url:
                return url

        return ""

    # ── Webhook management ────────────────────────────────────────────

    def _set_webhook_with_retry(self, url: str) -> bool:
        for delay in self._config.webhook_retry_delays:
            if delay > 0:
                _log(_LOG_INFO, "tunnel", f"Webhook not ready, retrying in {delay}s...")
                self._clock.sleep(delay)
            if self._stop_event.is_set():
                return False

            resp = self._tg_set_webhook(url, secret_token=self._webhook_secret)
            if resp.get("ok"):
                _log(_LOG_INFO, "tunnel", "Webhook configured")
                return True

        self._tg_delete_webhook()
        return False

    def _periodic_webhook_check(self) -> None:
        if self._polling_active:
            self._stop_poll_fallback()
            resp = self._tg_set_webhook(self._tunnel_url, secret_token=self._webhook_secret)
            if resp.get("ok"):
                self._state = TunnelState.RUNNING
                _log(_LOG_INFO, "tunnel", "Webhook restored, poll fallback stopped")
                self._on_notify("✅ Webhook restored (DNS resolved)")
            else:
                self._tg_delete_webhook()
                self._start_poll_fallback()
        else:
            info = self._tg_get_webhook_info()
            if info:
                result = info.get("result", {})
                current_url = result.get("url", "") if isinstance(result, dict) else ""
                if not current_url or self._tunnel_url not in current_url:
                    resp = self._tg_set_webhook(self._tunnel_url, secret_token=self._webhook_secret)
                    if resp.get("ok"):
                        _log(_LOG_INFO, "tunnel", "Webhook re-registered (was stale)")
                        self._on_notify("✅ Webhook re-registered")

    # ── Poll fallback ─────────────────────────────────────────────────

    def _start_poll_fallback(self) -> None:
        if self._polling_active:
            return

        self._tg_delete_webhook()
        self._clock.sleep(1)
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="tunnel-poll", daemon=True,
        )
        self._poll_thread.start()
        self._polling_active = True
        self._state = TunnelState.POLL_FALLBACK
        _log(_LOG_INFO, "tunnel", "Poll fallback started")

    def _stop_poll_fallback(self) -> None:
        if not self._polling_active:
            return
        self._poll_stop.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5)
        self._polling_active = False
        _log(_LOG_INFO, "tunnel", "Poll fallback stopped")

    def _poll_loop(self) -> None:
        offset = 0
        _bridge_host = self._bind_host or "127.0.0.1"
        _log(_LOG_INFO, "tunnel:poll", f"Poll loop started (bridge={_bridge_host}:{self._port})")
        while not self._poll_stop.is_set():
            try:
                url = f"https://api.telegram.org/bot{self._token}/getUpdates?offset={offset}&timeout={self._config.poll_timeout}"
                req = urllib.request.Request(url)
                with self._urlopen(req, timeout=self._config.poll_timeout + 5) as resp:
                    data = json.loads(resp.read())

                if not data.get("ok"):
                    self._clock.sleep(1)
                    continue

                for update in data.get("result", []):
                    uid = update.get("update_id", 0)
                    if self._on_update:
                        try:
                            self._on_update(update)
                        except Exception as exc:
                            _log(_LOG_ERROR, "tunnel:poll", f"Forward failed update_id={uid}: {exc}")
                    else:
                        self._forward_to_localhost(update)
                    offset = uid + 1

            except Exception as exc:
                if not self._poll_stop.is_set():
                    _log(_LOG_WARN, "tunnel:poll", f"Poll error: {exc}")
                    self._clock.sleep(self._config.poll_error_delay)

    def _forward_to_localhost(self, update: dict[str, object]) -> None:
        _bridge_host = self._bind_host or "127.0.0.1"
        try:
            req = urllib.request.Request(
                f"http://{_bridge_host}:{self._port}/",
                data=json.dumps(update).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            self._urlopen(req, timeout=5)
        except Exception as exc:
            _log(_LOG_WARN, "tunnel:poll", f"Forward to {_bridge_host} failed: {exc}")

    # ── Port wait ─────────────────────────────────────────────────────

    def _wait_for_port(self) -> bool:
        elapsed = 0
        bind_host = self._bind_host or "127.0.0.1"
        while elapsed < self._config.port_wait_timeout and not self._stop_event.is_set():
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(1)
                    s.connect((bind_host, self._port))
                    _log(_LOG_INFO, "tunnel", f"Bridge ready on {bind_host}:{self._port} ({elapsed}s)")
                    return True
            except (ConnectionRefusedError, OSError):
                pass
            self._clock.sleep(1)
            elapsed += 1
        return False

    # ── File persistence ──────────────────────────────────────────────

    def _save_tunnel_url(self, url: str) -> None:
        try:
            (self._node_dir / "tunnel_url").write_text(url)
        except OSError:
            pass

    def _kill_stale_tunnel(self) -> None:
        pid_file = self._node_dir / "tunnel.pid"
        try:
            pid = int(pid_file.read_text().strip())
        except (OSError, ValueError):
            return
        try:
            os.kill(pid, 9)
            _log(_LOG_WARN, "tunnel", f"Killed stale cloudflared (pid={pid})")
        except (OSError, ProcessLookupError):
            pass
        try:
            pid_file.unlink()
        except OSError:
            pass

    def _save_pid(self, pid: int) -> None:
        try:
            (self._node_dir / "tunnel.pid").write_text(str(pid))
        except OSError:
            pass

    def _save_bot_info(self) -> None:
        try:
            info = self._tg_get_me()
            if info.get("ok"):
                result = info.get("result", {})
                if isinstance(result, dict):
                    bot_id = result.get("id")
                    username = result.get("username", "")
                    if bot_id:
                        (self._node_dir / "bot_id").write_text(str(bot_id))
                    if username:
                        (self._node_dir / "bot_username").write_text(username)
        except Exception:
            pass

