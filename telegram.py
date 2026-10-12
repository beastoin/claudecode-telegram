from __future__ import annotations
from core import (
    _str_field,
    _int_field,
    _dict_field,
    _bool_field,
    _log,
    _LOG_ERROR,
    _LOG_WARN,
    _LOG_INFO,
    _LOG_DEBUG,
    _log_best_effort,
    SubprocessRunner,
    Clock,
    MarkdownToken,
    _subprocess_runner,
    _urlopen,
    _tg_http_client,
    _clock as _default_clock,
    _RealClock,
    _RealSubprocessRunner,
    AppContext,
    get_app_context,
    TunnelConfig,
    VERSION,
    BOT_TOKEN,
    NODE_NAME,
    NODE_DIR,
    SESSIONS_DIR,
    TIMEOUT_HTTP_API,
    TIMEOUT_HTTP_DOWNLOAD,
    TIMEOUT_HTTP_UPLOAD,
    TIMEOUT_PROCESS_WAIT,
    TIMEOUT_FILE_TRANSFER,
    TIMEOUT_TMUX_CHECK,
    PENDING_TIMEOUT,
    STT_ENDPOINT,
    STT_TIMEOUT,
    TEAM_DIR,
    ADMIN_CHAT_ID_ENV,
    admin_chat_id,
    DEFAULT_BACKEND,
)
import collections
from dataclasses import dataclass, field
import hashlib
import os
import json
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
from typing import (
    IO,
    Callable,
    Iterator,
    Literal,
    NamedTuple,
    Protocol,
    TypedDict,
    TYPE_CHECKING,
    cast,
    runtime_checkable,
)
from _types import (
    TelegramApiResponseDict,
    TelegramUser,
    TelegramChat,
    TelegramPhotoSize,
    TelegramDocument,
    TelegramVoice,
    TelegramVideo,
    TelegramAudio,
    TelegramSticker,
    TelegramMessageDict,
    TelegramUpdate,
    FileValidation,
    MediaGroupEntry,
)

ChatId = int | str
MessageId = int
ParseMode = Literal["HTML", "MarkdownV2"] | None
TelegramApiResponse = TelegramApiResponseDict | None
TelegramCallbackQuery = TypedDict(
    "TelegramCallbackQuery",
    {
        "id": str,
        "from": TelegramUser,
        "message": TelegramMessageDict,
        "data": str,
    },
    total=False,
)
if TYPE_CHECKING:
    from claudecode import WorkerStateEntry, TmuxSessionDict


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
        has_media = bool(photo or document or animation or video or audio or voice or video_note or sticker)
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
                    parts.append("".join(chunk if isinstance(chunk, str) else chunk.get("text", "") for chunk in bt))
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
    "/mcp",
    "/help",
    "/config",
    "/model",
    "/compact",
    "/cost",
    "/doctor",
    "/init",
    "/login",
    "/logout",
    "/permissions",
    "/pr",
    "/review",
    "/terminal",
    "/vim",
    "/approved-tools",
    "/listen",
]


def save_last_chat_id(chat_id: ChatId | None) -> None:
    if chat_id is None:
        return
    try:
        NODE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        _tmp = LAST_CHAT_ID_FILE.with_suffix(".tmp")
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
        _tmp = LAST_ACTIVE_FILE.with_suffix(".tmp")
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
    def name(self) -> str: ...
    def send_text(
        self, chat_id: ChatId, text: str, parse_mode: ParseMode = None, reply_to: MessageId | None = None
    ) -> TelegramApiResponse: ...
    def send_rich_text(
        self, chat_id: ChatId, markdown: str, reply_to: MessageId | None = None
    ) -> TelegramApiResponse: ...
    def send_photo(self, chat_id: ChatId, photo_path: str | Path, caption: str | None = None) -> bool: ...
    def send_document(self, chat_id: ChatId, doc_path: str | Path, caption: str | None = None) -> bool: ...
    def send_animation(self, chat_id: ChatId, animation_path: str | Path, caption: str | None = None) -> bool: ...
    def send_video(self, chat_id: ChatId, video_path: str | Path, caption: str | None = None) -> bool: ...
    def send_audio(self, chat_id: ChatId, audio_path: str | Path, caption: str | None = None) -> bool: ...
    def send_voice(self, chat_id: ChatId, voice_path: str | Path, caption: str | None = None) -> bool: ...
    def send_sticker(self, chat_id: ChatId, sticker_path: str | Path) -> bool: ...
    def send_chat_action(self, chat_id: ChatId, action: str) -> None: ...
    def set_reaction(self, chat_id: ChatId, message_id: MessageId, reaction: list[dict[str, str]]) -> None: ...
    def edit_message(
        self, chat_id: ChatId, message_id: MessageId, text: str, parse_mode: ParseMode = None
    ) -> TelegramApiResponse: ...
    def setup_commands(self, commands: list[dict[str, str]]) -> None: ...
    def download_file(self, file_id: str, session_name: str) -> str | None: ...


TELEGRAM_MAX_LENGTH = 4096
TELEGRAM_RICH_MAX_LENGTH = 32768
MAX_FILE_SIZE = 50 * 1024 * 1024
ALLOWED_IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".gif",
    ".mp4",
}
BLOCKED_DOC_EXTENSIONS = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".crt",
    ".cer",
    ".der",
    ".jks",
    ".keystore",
    ".kdb",
    ".pgp",
    ".gpg",
    ".asc",
}
BLOCKED_FILENAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    "id_rsa",
    "id_ed25519",
    "id_dsa",
    "credentials",
    "kubeconfig",
}
TELEGRAM_PHOTO_MAX_SUM = 10000
TELEGRAM_PHOTO_MAX_DIM = 5000
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".flac", ".aac", ".wav"}
VOICE_EXTENSIONS = {".ogg", ".opus", ".oga"}
STICKER_EXTENSIONS = {".tgs"}
CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
from _telegram_impl import (
    TelegramAPI,
    TelegramTransport,
    LocalTransport,
    _init_transport,
    telegram_api,
    format_file_size,
    transcribe_voice,
    _prepare_photo_for_telegram,
    _validate_file_path,
    validate_photo_path,
    is_blocked_filename,
    validate_document_path,
    _split_protected_segments,
    _collapse_excess_newlines,
    split_message,
    format_multipart_messages,
    update_bot_commands,
)

transport = _init_transport()


def send_telegram_message(chat_id: ChatId, text: str, parse_mode: ParseMode = None) -> TelegramApiResponse:
    return transport.send_text(chat_id, text, parse_mode=parse_mode)


def download_telegram_file(file_id: str, session_name: str | None) -> str | None:
    if session_name is None:
        return None
    return transport.download_file(file_id, session_name)


def send_voice(chat_id: ChatId, path: str, caption: str | None = None) -> bool:
    return transport.send_voice(chat_id, path, caption)


def send_photo(chat_id: ChatId, path: str, caption: str | None = None) -> bool:
    return transport.send_photo(chat_id, path, caption)


def send_animation(chat_id: ChatId, path: str, caption: str | None = None) -> bool:
    return transport.send_animation(chat_id, path, caption)


def send_document(chat_id: ChatId, path: str, caption: str | None = None) -> bool:
    return transport.send_document(chat_id, path, caption)


def send_video(chat_id: ChatId, path: str, caption: str | None = None) -> bool:
    return transport.send_video(chat_id, path, caption)


def send_audio(chat_id: ChatId, path: str, caption: str | None = None) -> bool:
    return transport.send_audio(chat_id, path, caption)


def send_sticker(chat_id: ChatId, path: str) -> bool:
    return transport.send_sticker(chat_id, path)


def _parse_media_tags(
    text: str, tag_name: str, validate_func: Callable[[str | Path], FileValidation]
) -> tuple[str, list[tuple[str | None, str]]]:
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


from markdown_fmt import (  # noqa: E402
    escape_html as escape_html,
    _TelegramHTMLSanitizer as _TelegramHTMLSanitizer,
    _sanitize_telegram_html as _sanitize_telegram_html,
    _render_md_inline_plain as _render_md_inline_plain,
    _render_md_inline_html as _render_md_inline_html,
    _render_table_as_pre as _render_table_as_pre,
    _wrap_plain_tables as _wrap_plain_tables,
    markdown_to_telegram_html as markdown_to_telegram_html,
    _pipe_tables_to_html as _pipe_tables_to_html,
)


def format_response_text(session_name: str, text: str) -> str:
    stripped = text.lstrip()
    prefix = f"{session_name}:"
    if stripped.lower().startswith(prefix.lower()):
        text = stripped[len(prefix) :].lstrip()
    return f"<b>{session_name}:</b>\n{text}"


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
