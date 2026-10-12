"""Telegram transport, message formatting, and markdown conversion."""

from __future__ import annotations

import collections
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import hashlib
from html.parser import HTMLParser
import json
import mimetypes
import os
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
from pathlib import Path
from typing import (
    IO,
    Any,
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
    BRIDGE_PUBLIC_URL,
    PORT,
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

if TYPE_CHECKING:
    from claudecode import WorkerStateEntry, TmuxSessionDict

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TELEGRAM_MAX_LENGTH = 4096
TELEGRAM_RICH_MAX_LENGTH = 32768
MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_PHOTO_DIMENSION = 4096
TELEGRAM_PHOTO_MAX_SUM = 10000
TELEGRAM_PHOTO_MAX_DIM = 5000
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".mp4"}
BLOCKED_DOC_EXTENSIONS = {
    ".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".der",
    ".jks", ".keystore", ".kdb", ".pgp", ".gpg", ".asc",
}
BLOCKED_FILENAMES = {
    ".env", ".npmrc", ".pypirc", ".netrc", ".git-credentials",
    "id_rsa", "id_ed25519", "id_dsa", "credentials", "kubeconfig",
}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".flac", ".aac", ".wav"}
VOICE_EXTENSIONS = {".ogg", ".opus", ".oga"}
STICKER_EXTENSIONS = {".tgs"}
CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
TRANSPORT_MODE = os.environ.get("TRANSPORT", "telegram")
LAST_CHAT_ID_FILE = NODE_DIR / "last_chat_id"
LAST_ACTIVE_FILE = NODE_DIR / "last_active"
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
    "/mcp", "/help", "/config", "/model", "/compact", "/cost", "/doctor",
    "/init", "/login", "/logout", "/permissions", "/pr", "/review",
    "/terminal", "/vim", "/approved-tools", "/listen",
]

# ---------------------------------------------------------------------------
# IncomingMessage
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Message helpers
# ---------------------------------------------------------------------------


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


def _build_cwd_change_notice(name: str, old_cwd: str, new_cwd: str, old_sid: str) -> str:
    lines = [f"⚠️ {name}: workspace changed"]
    if old_sid:
        lines.append(f"<b>from:</b> <code>{old_cwd}</code> (session <code>{old_sid[:12]}…</code>)")
    else:
        lines.append(f"<b>from:</b> <code>{old_cwd}</code> (no previous session)")
    lines.append(f"<b>to:</b> <code>{new_cwd}</code> (fresh start)")
    return "\n".join(lines)


class MediaGroupState:
    def __init__(self) -> None:
        self.buffer: dict[str, MediaGroupEntry] = {}
        self.lock: threading.Lock = threading.Lock()


# ---------------------------------------------------------------------------
# File state persistence
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# HTML escaping and sanitization (was markdown_fmt.py)
# ---------------------------------------------------------------------------


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class _TelegramHTMLSanitizer(HTMLParser):
    SAFE_TAGS: frozenset[str] = frozenset(
        {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "blockquote", "span", "tg-emoji", "tg-spoiler"}
    )
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


# ---------------------------------------------------------------------------
# Markdown inline rendering (was markdown_fmt.py)
# ---------------------------------------------------------------------------


def _render_md_inline_plain(children: list[MarkdownToken]) -> str:
    out: list[str] = []
    for tok in children:
        if tok.type in ("text", "code_inline"):
            out.append(tok.content)
        elif tok.type in ("softbreak", "hardbreak"):
            out.append(" ")
        elif tok.type == "image":
            out.append(tok.content or "image")
        elif tok.type in (
            "strong_open", "strong_close", "em_open", "em_close",
            "s_open", "s_close", "link_open", "link_close", "html_inline",
        ):
            pass
        else:
            if tok.content:
                out.append(tok.content)
    return "".join(out)


_INLINE_TAG_MAP = {
    "strong_open": "<b>",
    "strong_close": "</b>",
    "em_open": "<i>",
    "em_close": "</i>",
    "s_open": "<s>",
    "s_close": "</s>",
    "link_close": "</a>",
    "softbreak": "\n",
    "hardbreak": "\n",
}


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
            out.append(f"[{alt}]({src})")
        elif tok.type == "html_inline":
            out.append(_sanitize_telegram_html(tok.content, rejected_open_tags))
        elif tok.content:
            out.append(escape_html(tok.content))
    return "".join(out)


# ---------------------------------------------------------------------------
# Table rendering (was markdown_fmt.py)
# ---------------------------------------------------------------------------


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
            lines.append("═" * (sum(col_widths) + 2 * (num_cols - 1)))
    return f"<pre>{chr(10).join(lines)}</pre>\n"


_MULTI_SPACE_RE = re.compile(r"\S  +\S.*\S  +\S")


def _wrap_plain_tables(text: str) -> str:
    parts = re.split(r"(<pre>.*?</pre>)", text, flags=re.DOTALL)
    out: list[str] = []
    for part in parts:
        if part.startswith("<pre>"):
            out.append(part)
            continue
        lines = part.split("\n")
        i = 0
        while i < len(lines):
            if _MULTI_SPACE_RE.search(lines[i]):
                run = [lines[i]]
                j = i + 1
                while j < len(lines) and (_MULTI_SPACE_RE.search(lines[j]) or lines[j].strip() == ""):
                    run.append(lines[j])
                    j += 1
                tabular_count = sum(1 for line in run if _MULTI_SPACE_RE.search(line))
                if tabular_count >= 2:
                    while run and run[-1].strip() == "":
                        j -= 1
                        run.pop()
                    raw = []
                    for rl in run:
                        plain = re.sub(r"<[^>]+>", "", rl)
                        plain = plain.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                        raw.append(plain)
                    content = escape_html("\n".join(raw))
                    out.append(f"<pre>{content}</pre>")
                    i = j
                else:
                    out.append(lines[i])
                    i += 1
            else:
                out.append(lines[i])
                i += 1
    return "\n".join(out) if out else text


# ---------------------------------------------------------------------------
# Markdown to Telegram HTML (was markdown_fmt.py)
# ---------------------------------------------------------------------------


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
                result.append(f"{indent}• ")
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
            result.append("————\n")
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
    def _parse_row(line: str) -> list[str] | None:
        line = line.strip()
        if line.startswith("|"):
            line = line[1:]
        if line.endswith("|"):
            line = line[:-1]
        return [cell.strip() for cell in line.split("|")]

    def _esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _cell_md(s: str) -> str:
        s = _esc(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"__(.+?)__", r"<b>\1</b>", s)
        s = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", s)
        s = re.sub(r"~~(.+?)~~", r"<s>\1</s>", s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
        return s

    lines = text.split("\n")
    result = []
    i = 0
    in_code_fence = False
    while i < len(lines):
        if re.match(r"^\s*(`{3,}|~{3,})", lines[i]):
            in_code_fence = not in_code_fence
            result.append(lines[i])
            i += 1
            continue
        if (
            not in_code_fence
            and i + 1 < len(lines)
            and "|" in lines[i]
            and re.match(r"^\s*\|[\s:]*-+[\s:]*(\|[\s:]*-+[\s:]*)*\|?\s*$", lines[i + 1])
        ):
            headers = _parse_row(lines[i])
            aligns = []
            for cell in _parse_row(lines[i + 1]) or []:
                cell = cell.strip()
                if cell.startswith(":") and cell.endswith(":"):
                    aligns.append(' style="text-align:center"')
                elif cell.endswith(":"):
                    aligns.append(' style="text-align:right"')
                else:
                    aligns.append("")
            html = ["<table>"]
            html.append("<tr>")
            for j, h in enumerate(headers or []):
                align = aligns[j] if j < len(aligns) else ""
                html.append(f"<th{align}>{_cell_md(h)}</th>")
            html.append("</tr>")
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip().startswith("|"):
                cells = _parse_row(lines[i])
                html.append("<tr>")
                for j, cell in enumerate(cells or []):
                    align = aligns[j] if j < len(aligns) else ""
                    html.append(f"<td{align}>{_cell_md(cell)}</td>")
                html.append("</tr>")
                i += 1
            html.append("</table>")
            result.append("\n".join(html))
        else:
            result.append(lines[i])
            i += 1
    return "\n".join(result)


# ---------------------------------------------------------------------------
# File validation (was _telegram_impl.py)
# ---------------------------------------------------------------------------


def format_file_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


def is_blocked_filename(filename: str) -> bool:
    name_lower = filename.lower()
    return name_lower in BLOCKED_FILENAMES or name_lower.startswith(".env")


def _validate_file_path(
    path: str | Path,
    label: str,
    allowed_exts: set[str] | None = None,
    blocked_exts: set[str] | None = None,
    check_blocked_name: bool = False,
) -> FileValidation:
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


def validate_document_path(doc_path: str | Path) -> FileValidation:
    return _validate_file_path(doc_path, "Document", blocked_exts=BLOCKED_DOC_EXTENSIONS, check_blocked_name=True)


# ---------------------------------------------------------------------------
# Photo preparation (was _telegram_impl.py)
# ---------------------------------------------------------------------------


def _prepare_photo_for_telegram(photo_path: str | Path) -> tuple[bytes, str]:
    photo_path = Path(photo_path)
    try:
        from PIL import Image

        with Image.open(photo_path) as img:
            w, h = img.size
            needs_resize = w + h > TELEGRAM_PHOTO_MAX_SUM or w > TELEGRAM_PHOTO_MAX_DIM or h > TELEGRAM_PHOTO_MAX_DIM
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


# ---------------------------------------------------------------------------
# Voice transcription (was _telegram_impl.py)
# ---------------------------------------------------------------------------


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
            STT_ENDPOINT, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
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


# ---------------------------------------------------------------------------
# Protected segments and message splitting (was _telegram_impl.py)
# ---------------------------------------------------------------------------


def _split_protected_segments(text: str, pattern: re.Pattern[str]) -> list[tuple[str, bool]]:
    segments = []
    last = 0
    for match in pattern.finditer(text):
        if match.start() > last:
            segments.append((text[last : match.start()], False))
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


def split_message(text: str, max_len: int = TELEGRAM_MAX_LENGTH) -> list[str]:
    if len(text) <= max_len:
        return [text]
    TAG_RE = re.compile(r"<(/?)(\w+)([^>]*)>")
    TRACKED_TAGS = frozenset(
        ("b", "i", "s", "u", "code", "pre", "a", "strong", "em", "del", "ins", "strike", "blockquote")
    )

    def _closing_tags(stack: list[tuple[str, str]]) -> str:
        return "".join(f"</{tag}>" for tag, _ in reversed(stack))

    def _opening_tags(stack: list[tuple[str, str]]) -> str:
        return "".join(full for _, full in stack)

    def _scan_tags(text: str) -> list[tuple[str, str]]:
        stack: list[tuple[str, str]] = []
        for m in TAG_RE.finditer(text):
            is_close = m.group(1) == "/"
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
        last_tag_start = search.rfind("<")
        last_tag_end = search.rfind(">")
        if last_tag_start > last_tag_end:
            search = text[:last_tag_start]
            budget = last_tag_start
        for sep in ("\n\n", "\n", " "):
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


# ---------------------------------------------------------------------------
# Transport protocol
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# TelegramAPI (was _telegram_impl.py)
# ---------------------------------------------------------------------------


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
            with _tg_http_client.post(url, data=payload, headers=headers, timeout=TIMEOUT_HTTP_API) as r:
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

    def set_reaction(
        self, chat_id: ChatId, message_id: MessageId, reaction: list[dict[str, str]]
    ) -> TelegramApiResponse:
        payload: dict[str, object] = {"chat_id": chat_id, "message_id": message_id, "reaction": reaction}
        return self.api("setMessageReaction", payload)


# ---------------------------------------------------------------------------
# TelegramTransport (was _telegram_impl.py)
# ---------------------------------------------------------------------------


class TelegramTransport(MessageTransport):
    def __init__(self, token: str) -> None:
        self._api: TelegramAPI = TelegramAPI(token)

    @property
    def name(self) -> str:
        return "telegram"

    def send_text(
        self, chat_id: ChatId, text: str, parse_mode: ParseMode = None, reply_to: MessageId | None = None
    ) -> TelegramApiResponse:
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        return telegram_api("sendMessage", payload)

    def send_rich_text(self, chat_id: ChatId, markdown: str, reply_to: MessageId | None = None) -> TelegramApiResponse:
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "rich_message": {"markdown": markdown},
        }
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        return telegram_api("sendRichMessage", payload)

    _MEDIA_DISPATCH: dict[str, tuple[str, str, str, bool]] = {
        "photo": ("sendPhoto", "photo", "photo", True),
        "animation": ("sendAnimation", "animation", "photo", False),
        "document": ("sendDocument", "document", "doc", False),
        "video": ("sendVideo", "video", "doc", False),
        "audio": ("sendAudio", "audio", "doc", False),
        "voice": ("sendVoice", "voice", "doc", False),
    }
    _VALIDATORS = {"photo": lambda p: validate_photo_path(p), "doc": lambda p: validate_document_path(p)}

    def _dispatch_media(self, kind: str, chat_id: ChatId, path: str | Path, caption: str | None = None) -> bool:
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
        return self._send_media_multipart(
            chat_id, validated, field, api_method, caption, file_data=file_data, filename=filename, mime_type=mime_type
        )

    def send_photo(self, chat_id: ChatId, photo_path: str | Path, caption: str | None = None) -> bool:
        return self._dispatch_media("photo", chat_id, photo_path, caption)

    def send_animation(self, chat_id: ChatId, animation_path: str | Path, caption: str | None = None) -> bool:
        return self._dispatch_media("animation", chat_id, animation_path, caption)

    def send_document(self, chat_id: ChatId, doc_path: str | Path, caption: str | None = None) -> bool:
        return self._dispatch_media("document", chat_id, doc_path, caption)

    def _send_media_multipart(
        self,
        chat_id: ChatId,
        file_path: Path | str,
        field_name: str,
        api_method: str,
        caption: str | None = None,
        file_data: bytes | None = None,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> bool:
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
            body_parts.extend(
                [
                    f"--{boundary}".encode(),
                    b'Content-Disposition: form-data; name="caption"',
                    b"",
                    caption.encode(),
                ]
            )
        body_parts.append(f"--{boundary}--".encode())
        body_parts.append(b"")
        body = b"\r\n".join(body_parts)
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{BOT_TOKEN}/{api_method}",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
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

    def send_video(self, chat_id: ChatId, video_path: str | Path, caption: str | None = None) -> bool:
        return self._dispatch_media("video", chat_id, video_path, caption)

    def send_audio(self, chat_id: ChatId, audio_path: str | Path, caption: str | None = None) -> bool:
        return self._dispatch_media("audio", chat_id, audio_path, caption)

    def send_voice(self, chat_id: ChatId, voice_path: str | Path, caption: str | None = None) -> bool:
        return self._dispatch_media("voice", chat_id, voice_path, caption)

    def send_sticker(self, chat_id: ChatId, sticker_path: str | Path) -> bool:
        sticker_path = Path(sticker_path)
        if not sticker_path.exists() or not sticker_path.is_file():
            _log(_LOG_WARN, "telegram", f"Sticker not found: {sticker_path}")
            return False
        return self._send_media_multipart(chat_id, sticker_path, "sticker", "sendSticker")

    def send_chat_action(self, chat_id: ChatId, action: str) -> None:
        telegram_api("sendChatAction", {"chat_id": chat_id, "action": action})

    def set_reaction(self, chat_id: ChatId, message_id: MessageId, reaction: list[dict[str, str]]) -> None:
        telegram_api("setMessageReaction", {"chat_id": chat_id, "message_id": message_id, "reaction": reaction})

    def edit_message(
        self, chat_id: ChatId, message_id: MessageId, text: str, parse_mode: ParseMode = None
    ) -> TelegramApiResponse:
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
                headers={"Content-Type": "application/json"},
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
                _cc._remote_run(
                    ["mkdir", "-p", remote_inbox], host=host, capture_output=True, timeout=TIMEOUT_TMUX_CHECK
                )
                _cc._remote_run(
                    ["chmod", "700", remote_inbox], host=host, capture_output=True, timeout=TIMEOUT_TMUX_CHECK
                )
                rsync_result = _subprocess_runner.run(
                    ["rsync", "-az", str(local_path), f"{host}:{remote_inbox}/"],
                    capture_output=True,
                    text=True,
                    timeout=TIMEOUT_FILE_TRANSFER,
                )
                if rsync_result.returncode != 0:
                    _log(
                        _LOG_ERROR,
                        "bridge",
                        f"rsync inbound failed (exit {rsync_result.returncode}): {host}:{remote_inbox}/ -> {rsync_result.stderr.strip()}",
                    )
            return str(local_path)
        except (subprocess.SubprocessError, OSError) as e:
            _log(_LOG_ERROR, "bridge", f"Download error: {e}")
            return None


# ---------------------------------------------------------------------------
# LocalTransport (was _telegram_impl.py)
# ---------------------------------------------------------------------------


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

    def send_text(
        self, chat_id: ChatId, text: str, parse_mode: ParseMode = None, reply_to: MessageId | None = None
    ) -> TelegramApiResponse:
        self._log("send_text", chat_id, text=text[:200])
        return self._MSG_OK

    def send_rich_text(self, chat_id: ChatId, markdown: str, reply_to: MessageId | None = None) -> TelegramApiResponse:
        self._log("send_rich_text", chat_id, text=markdown[:200])
        return self._MSG_OK

    def edit_message(
        self, chat_id: ChatId, message_id: MessageId, text: str, parse_mode: ParseMode = None
    ) -> TelegramApiResponse:
        self._log("edit_message", chat_id, message_id=message_id)
        return {"ok": True, "result": {"message_id": message_id}}

    def send_photo(self, chat_id: ChatId, p: str | Path, caption: str | None = None) -> bool:
        self._log("send_photo", chat_id, path=p)
        return True

    def send_document(self, chat_id: ChatId, p: str | Path, caption: str | None = None) -> bool:
        self._log("send_document", chat_id, path=p)
        return True

    def send_animation(self, chat_id: ChatId, p: str | Path, caption: str | None = None) -> bool:
        self._log("send_animation", chat_id, path=p)
        return True

    def send_video(self, chat_id: ChatId, p: str | Path, caption: str | None = None) -> bool:
        self._log("send_video", chat_id, path=p)
        return True

    def send_audio(self, chat_id: ChatId, p: str | Path, caption: str | None = None) -> bool:
        self._log("send_audio", chat_id, path=p)
        return True

    def send_voice(self, chat_id: ChatId, p: str | Path, caption: str | None = None) -> bool:
        self._log("send_voice", chat_id, path=p)
        return True

    def send_sticker(self, chat_id: ChatId, p: str | Path) -> bool:
        self._log("send_sticker", chat_id, path=p)
        return True

    def send_chat_action(self, chat_id: ChatId, action: str) -> None:
        self._log("send_chat_action", chat_id, action=action)

    def set_reaction(self, chat_id: ChatId, message_id: MessageId, reaction: list[dict[str, str]]) -> None:
        self._log("set_reaction", chat_id, message_id=message_id)

    def setup_commands(self, commands: list[dict[str, str]]) -> None:
        self._log("setup_commands", 0, count=len(commands))

    def download_file(self, file_id: str, session_name: str) -> str | None:
        self._log("download_file", 0, file_id=file_id)
        return None


# ---------------------------------------------------------------------------
# Transport initialization and API helper
# ---------------------------------------------------------------------------


def _init_transport() -> MessageTransport:
    if TRANSPORT_MODE == "local":
        return LocalTransport()
    return TelegramTransport(BOT_TOKEN)


def telegram_api(method: str, data: Mapping[str, object]) -> TelegramApiResponse:
    if TRANSPORT_MODE == "local":
        _log(_LOG_INFO, "local-transport", f"telegram_api {method} {str(data)[:100]}")
        return {"ok": True, "result": {"message_id": 1}}
    if isinstance(transport, TelegramTransport):
        return transport._api.api(method, data)
    return None


transport = _init_transport()


# ---------------------------------------------------------------------------
# Convenience send functions
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Media tag parsing
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Response formatting and bot commands
# ---------------------------------------------------------------------------


def format_response_text(session_name: str, text: str) -> str:
    stripped = text.lstrip()
    prefix = f"{session_name}:"
    if stripped.lower().startswith(prefix.lower()):
        text = stripped[len(prefix) :].lstrip()
    return f"<b>{session_name}:</b>\n{text}"


def format_multipart_messages(session_name: str, chunks: list[str]) -> list[str]:
    return [format_response_text(session_name, chunk) for chunk in chunks]


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
