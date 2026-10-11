"""Telegram transport implementations and utilities."""
from __future__ import annotations
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import uuid
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from collections.abc import Mapping
from typing import Any, cast
from core import (_log, _LOG_ERROR, _LOG_WARN, _LOG_INFO, _LOG_DEBUG,
    _clock, _subprocess_runner, _urlopen, _tg_http_client,
    BRIDGE_PUBLIC_URL, PORT, BOT_TOKEN,
    STT_ENDPOINT, STT_TIMEOUT,
    TIMEOUT_HTTP_API, TIMEOUT_HTTP_DOWNLOAD, TIMEOUT_HTTP_UPLOAD,
    TIMEOUT_TMUX_CHECK, TIMEOUT_FILE_TRANSFER)
from telegram import (MessageTransport, IncomingMessage,
    ChatId, MessageId, ParseMode, TelegramApiResponse,
    FileValidation, TELEGRAM_MAX_LENGTH, TELEGRAM_RICH_MAX_LENGTH,
    _MEDIA_GROUP_WAIT, TRANSPORT_MODE,
    ALLOWED_IMAGE_EXTENSIONS, BLOCKED_DOC_EXTENSIONS, BLOCKED_FILENAMES,
    CODE_FENCE_RE, INLINE_CODE_RE, MAX_FILE_SIZE,
    VIDEO_EXTENSIONS, AUDIO_EXTENSIONS, VOICE_EXTENSIONS, STICKER_EXTENSIONS,
    TELEGRAM_PHOTO_MAX_SUM, TELEGRAM_PHOTO_MAX_DIM, BOT_COMMANDS)
from _types import TelegramApiResponseDict
BLOCKED_FILENAMES = {
    ".env", ".npmrc", ".pypirc", ".netrc", ".git-credentials",
    "id_rsa", "id_ed25519", "id_dsa", "credentials", "kubeconfig", }
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_PHOTO_DIMENSION = 4096
class TelegramAPI:
    def __init__(self, token: str) -> None:
        self.token: str = token
    def api(self, method: str, data: Mapping[str, object] | dict[str, object]) -> TelegramApiResponse:
        if not self.token: return None
        url = f"https://api.telegram.org/bot{self.token}/{method}"; payload = json.dumps(data).encode()
        headers = {"Content-Type": "application/json"}
        try:
            with _tg_http_client.post(url, data=payload, headers=headers,
                                       timeout=TIMEOUT_HTTP_API) as r:
                return cast(TelegramApiResponseDict, json.loads(r.read()))
        except urllib.error.HTTPError as e:
            _log(_LOG_ERROR, "telegram", f"Telegram API error: {e}")
            try:
                raw = e.read(); body = cast(TelegramApiResponseDict, json.loads(raw))
                return body
            except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError, ValueError):
                return {"ok": False, "error_code": e.code, "description": f"HTTP {e.code} (non-JSON body)"}
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            _log(_LOG_ERROR, "telegram", f"Telegram API error: {e}")
            return None
    def set_reaction(self, chat_id: ChatId, message_id: MessageId,
                     reaction: list[dict[str, str]]) -> TelegramApiResponse:
        payload: dict[str, object] = {"chat_id": chat_id, "message_id": message_id, "reaction": reaction}
        return self.api("setMessageReaction", payload)
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
        if parse_mode: payload["parse_mode"] = parse_mode
        if reply_to: payload["reply_to_message_id"] = reply_to
        return telegram_api("sendMessage", payload)
    def send_rich_text(self, chat_id: ChatId, markdown: str,
                       reply_to: MessageId | None = None) -> TelegramApiResponse:
        payload: dict[str, object] = { "chat_id": chat_id, "rich_message": {"markdown": markdown}, }
        if reply_to: payload["reply_to_message_id"] = reply_to
        return telegram_api("sendRichMessage", payload)
    _MEDIA_DISPATCH: dict[str, tuple[str, str, str, bool]] = {
        "photo":     ("sendPhoto",     "photo",     "photo", True),
        "animation": ("sendAnimation", "animation", "photo", False),
        "document":  ("sendDocument",  "document",  "doc", False),
        "video":     ("sendVideo",     "video",     "doc", False),
        "audio":     ("sendAudio",     "audio",     "doc", False),
        "voice":     ("sendVoice",     "voice",     "doc", False), }
    _VALIDATORS = {"photo": lambda p: validate_photo_path(p), "doc": lambda p: validate_document_path(p)}
    def _dispatch_media(self, kind: str, chat_id: ChatId, path: str | Path,
                        caption: str | None = None) -> bool:
        if not BOT_TOKEN: return False
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
        elif kind == "animation": mime_type = "video/mp4" if Path(validated).suffix.lower() == ".mp4" else "image/gif"
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
        if not BOT_TOKEN: return False
        file_path = Path(file_path); data = file_data if file_data is not None else file_path.read_bytes()
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
            data, ]
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
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"} )
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
        if parse_mode: payload["parse_mode"] = parse_mode
        return telegram_api("editMessageText", payload)
    def setup_commands(self, commands: list[dict[str, str]]) -> None:
        telegram_api("setMyCommands", {"commands": commands})
    def download_file(self, file_id: str, session_name: str) -> str | None:
        if not BOT_TOKEN: return None
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
                data=json.dumps({"file_id": file_id}).encode(),
                headers={"Content-Type": "application/json"} )
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
        file_path = str(file_info.get("file_path", "")); file_size = int(file_info.get("file_size", 0))
        if not file_path:
            _log(_LOG_WARN, "telegram", "No file_path in response")
            return None
        if file_size > MAX_FILE_SIZE:
            _log(_LOG_WARN, "telegram", f"File too large: {file_size} > {MAX_FILE_SIZE}")
            return None
        download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        import bridge as _br
        import claudecode as _cc
        inbox = _br.ensure_inbox_dir(session_name); ext = Path(file_path).suffix or ""
        local_filename = f"{uuid.uuid4().hex}{ext}"; local_path = inbox / local_filename
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
            if v is not None: msg += f" {k}={v}"
        _log(_LOG_DEBUG, "local-transport", msg)
        if self._log_file:
            with open(self._log_file, "a") as f: f.write(msg + "\n")
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
    if TRANSPORT_MODE == "local": return LocalTransport()
    return TelegramTransport(BOT_TOKEN)
def telegram_api(method: str, data: Mapping[str, object]) -> TelegramApiResponse:
    if TRANSPORT_MODE == "local":
        _log(_LOG_INFO, "local-transport", f"telegram_api {method} {str(data)[:100]}")
        return {"ok": True, "result": {"message_id": 1}}
    import telegram as _tg
    if isinstance(_tg.transport, TelegramTransport): return _tg.transport._api.api(method, data)
    return None
def format_file_size(size_bytes: int) -> str:
    if size_bytes < 1024: return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024: return f"{size_bytes / 1024:.1f} KB"
    else: return f"{size_bytes / (1024 * 1024):.1f} MB"
def transcribe_voice(file_path: str, timeout: int | None = None) -> str | None:
    if not STT_ENDPOINT: return None
    if timeout is None: timeout = STT_TIMEOUT
    try:
        file_path_obj = Path(file_path)
        if not file_path_obj.exists(): return None
        boundary = uuid.uuid4().hex; body_parts = []
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
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"} )
        with _urlopen(req, timeout=timeout) as r:
            result = cast(TelegramApiResponseDict, json.loads(r.read())); text = str(result.get("text", "")).strip()
            if text:
                duration = str(result.get("audio_duration_s", "?"))
                _log(_LOG_INFO, "stt", f"STT transcribed: {len(text)} chars from {duration}s audio")
                return text
            return None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        _log(_LOG_ERROR, "bridge", f"STT error (fail-open): {e}")
        return None
def _prepare_photo_for_telegram(photo_path: str | Path) -> tuple[bytes, str]:
    photo_path = Path(photo_path)
    try:
        from PIL import Image
        with Image.open(photo_path) as img:
            w, h = img.size
            needs_resize = ( w + h > TELEGRAM_PHOTO_MAX_SUM or w > TELEGRAM_PHOTO_MAX_DIM or h > TELEGRAM_PHOTO_MAX_DIM
            )
            if needs_resize:
                scale = min(
                    TELEGRAM_PHOTO_MAX_DIM / max(w, 1),
                    TELEGRAM_PHOTO_MAX_DIM / max(h, 1),
                    TELEGRAM_PHOTO_MAX_SUM / max(w + h, 1), )
                new_w = int(w * scale); new_h = int(h * scale)
                resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                import io
                buf = io.BytesIO(); fmt = img.format or ("PNG" if photo_path.suffix.lower() == ".png" else "JPEG")
                if fmt == "PNG" and resized.mode not in ("RGBA", "P", "L", "LA"): resized = resized.convert("RGBA")
                elif fmt == "JPEG" and resized.mode != "RGB": resized = resized.convert("RGB")
                resized.save(buf, format=fmt)
                _log(_LOG_INFO, "telegram", f"Photo auto-resized: {w}x{h} -> {new_w}x{new_h} for Telegram")
                return buf.getvalue(), photo_path.name
        return photo_path.read_bytes(), photo_path.name
    except ImportError: return photo_path.read_bytes(), photo_path.name
def _validate_file_path(path: str | Path, label: str,
                        allowed_exts: set[str] | None = None,
                        blocked_exts: set[str] | None = None,
                        check_blocked_name: bool = False) -> FileValidation:
    p = Path(path)
    if not p.exists(): return FileValidation(False, f"{label} not found: {p}")
    if not p.is_file(): return FileValidation(False, f"Not a file: {p}")
    ext = p.suffix.lower()
    if allowed_exts and ext not in allowed_exts: return FileValidation(False, f"Invalid {label.lower()} extension: {p.suffix}")
    if blocked_exts and ext in blocked_exts: return FileValidation(False, f"Blocked extension (sensitive): {p.suffix}")
    if check_blocked_name and is_blocked_filename(p.name): return FileValidation(False, f"Blocked filename (sensitive): {p.name}")
    if p.stat().st_size > MAX_FILE_SIZE: return FileValidation(False, f"{label} too large: {p.stat().st_size} > {MAX_FILE_SIZE}")
    return FileValidation(True, p)
def validate_photo_path(photo_path: str | Path) -> FileValidation:
    return _validate_file_path(photo_path, "Photo", allowed_exts=ALLOWED_IMAGE_EXTENSIONS)
def is_blocked_filename(filename: str) -> bool:
    name_lower = filename.lower()
    return name_lower in BLOCKED_FILENAMES or name_lower.startswith(".env")
def validate_document_path(doc_path: str | Path) -> FileValidation:
    return _validate_file_path(doc_path, "Document", blocked_exts=BLOCKED_DOC_EXTENSIONS, check_blocked_name=True)
def _split_protected_segments(text: str, pattern: re.Pattern[str]) -> list[tuple[str, bool]]:
    segments = []; last = 0
    for match in pattern.finditer(text):
        if match.start() > last: segments.append((text[last:match.start()], False))
        segments.append((match.group(0), True))
        last = match.end()
    if last < len(text): segments.append((text[last:], False))
    return segments
def _collapse_excess_newlines(text: str) -> str:
    output = []
    for segment, protected in _split_protected_segments(text, CODE_FENCE_RE):
        if protected:
            output.append(segment)
            continue
        for inline_segment, inline_protected in _split_protected_segments(segment, INLINE_CODE_RE):
            if inline_protected: output.append(inline_segment)
            else: output.append(re.sub(r"\n{3,}", "\n\n", inline_segment))
    return "".join(output)
def split_message(text: str, max_len: int=TELEGRAM_MAX_LENGTH) -> list[str]:
    import re
    if len(text) <= max_len: return [text]
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
            is_close = m.group(1) == '/'; tag_name = m.group(2).lower()
            if tag_name not in TRACKED_TAGS: continue
            if is_close:
                for j in range(len(stack) - 1, -1, -1):
                    if stack[j][0] == tag_name:
                        stack.pop(j)
                        break
            else: stack.append((tag_name, m.group(0)))
        return stack
    def _find_split(text: str, budget: int) -> int:
        if budget <= 0: budget = 1
        search = text[:budget]; last_tag_start = search.rfind('<'); last_tag_end = search.rfind('>')
        if last_tag_start > last_tag_end: search = text[:last_tag_start]; budget = last_tag_start
        for sep in ('\n\n', '\n', ' '):
            pos = search.rfind(sep)
            if pos > budget // 3: return pos + 1
        return max(budget, 1)
    chunks = []; remaining = text
    carry_stack: list[tuple[str, str]] = []
    while remaining:
        prefix = _opening_tags(carry_stack); available = max_len - len(prefix)
        if len(prefix) + len(remaining) + len(_closing_tags(carry_stack)) <= max_len:
            suffix = _closing_tags(_scan_tags(prefix + remaining))
            chunks.append(prefix + remaining + suffix)
            break
        budget = available - 100
        if budget < 100: budget = 100
        for _attempt in range(5):
            split_at = _find_split(remaining, budget); chunk_text = remaining[:split_at].rstrip()
            full_chunk = prefix + chunk_text; open_stack = _scan_tags(full_chunk); suffix = _closing_tags(open_stack)
            if len(full_chunk) + len(suffix) <= max_len: break
            overshoot = len(full_chunk) + len(suffix) - max_len; budget = max(budget - overshoot - 20, 100)
        else:
            hard_limit = max_len - len(prefix) - len(suffix) - 10
            if hard_limit < 1: hard_limit = 1
            chunk_text = remaining[:hard_limit].rstrip(); full_chunk = prefix + chunk_text
            open_stack = _scan_tags(full_chunk); suffix = _closing_tags(open_stack); split_at = hard_limit
        chunks.append(full_chunk + suffix)
        carry_stack = open_stack; remaining = remaining[split_at:].lstrip()
        if split_at == 0: remaining = remaining[1:]
    return chunks
def format_multipart_messages(session_name: str, chunks: list[str]) -> list[str]:
    import telegram as _tg  # late import — format_response_text defined after our import
    return [_tg.format_response_text(session_name, chunk) for chunk in chunks]
def update_bot_commands() -> None:
    commands = list(BOT_COMMANDS)
    import bridge as _br
    registered = _br.get_registered_sessions()
    for name in sorted(registered.keys()): commands.append({"command": name, "description": f"Message {name}"})
    import telegram as _tg; _tg.transport.setup_commands(commands)
    worker_count = len(registered)
    _log(_LOG_INFO, "telegram", f"Bot commands updated ({len(BOT_COMMANDS)} + {worker_count} workers)")
