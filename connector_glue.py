"""Connector ↔ bridge glue — extracted from bridge.py."""
from __future__ import annotations
import os
import re
import time
from typing import TYPE_CHECKING, Callable, cast
from urllib.parse import urlparse

import core
import health
import telegram
from core import (
    _log, _LOG_ERROR, _LOG_WARN, _LOG_INFO,
    BRIDGE_PUBLIC_URL, NODE_DIR, TIMEOUT_GIT_OP,
)
from telegram import ALLOWED_IMAGE_EXTENSIONS, VIDEO_EXTENSIONS

if TYPE_CHECKING:
    from connectors import ConnectorAttachmentDict, ConnectorMetadataDict, ConnectorStatusDict
    from connectors import GmailConnector, GitHubConnector

def connector_log_message(tag: str, html_text: str, plain_text: str, targets: list[str]) -> None:
    from bridge import connectors
    connectors.log_message(tag, html_text, plain_text, targets)

def connector_render_html(tag: str, current_html: str) -> str:
    import html as html_mod
    from bridge import connectors, _CONNECTOR_CSS
    esc = html_mod.escape; msgs = connectors.get_log(tag); icon = "🔔" if tag == "github" else "📧"
    title = f"{tag.title()} Feed"; blocks = []
    av_letter = tag[0].upper(); av_color = "#8b5cf6" if tag == "github" else "#f59e0b"
    av_svg = f'<div class="u-av"><svg viewBox="0 0 28 28"><rect width="28" height="28" rx="14" fill="{av_color}"/><text x="14" y="18" text-anchor="middle" fill="#fff" font-size="12" font-weight="600">{av_letter}</text></svg></div>'
    tag_title = esc(tag.title())
    for i, m in enumerate(msgs):
        ts = time.strftime("%b %d, %H:%M", time.gmtime(m["ts"]))
        who = ", ".join(m["targets"]) if m["targets"] else "all"
        content = re.sub(r'(https?://\S+)', r'<a href="\1" target="_blank" rel="noopener">\1</a>', m["html"]).replace("\n", "<br>")
        is_latest = (i == len(msgs) - 1); cls = "chat-msg latest" if is_latest else "chat-msg"
        badge = '<span class="badge">Latest</span>' if is_latest else ""
        blocks.append(f'<div class="{cls}">{av_svg}<div class="chat-body"><span class="u-name">{tag_title}</span><span class="ts">{ts}</span><span class="target">→ {esc(who)}</span>{badge}<div class="chat-text">{content}</div></div></div>')
    blocks_html = "\n".join(blocks); updated = time.strftime("%b %d, %H:%M UTC", time.gmtime(core._clock.time()))
    count_text = f'{len(msgs)} recent message{"s" if len(msgs) != 1 else ""}'
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{icon} {title}</title>'
            '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">'
            '<style>' + _CONNECTOR_CSS + f'</style></head><body><div class="wrap"><header><h1>{icon} {title}</h1>'
            f'<div class="meta">{count_text} &middot; Updated {updated}</div></header>'
            f'<div class="thread">{blocks_html}</div></div></body></html>')

def connector_short_summary(tag: str, plain_text: str, serve_url: str | None = None, metadata: ConnectorMetadataDict | None = None) -> str:
    import html as _html
    icon = "🔔" if tag == "github" else "📧"; body = plain_text.strip()
    body = re.sub(r'^manager\s*\(via\s+\w+[^)]*\):\s*', '', body); body = re.sub(r'\[thread:[^\]]+\]\s*', '', body)
    body = " ".join(body.split())
    if len(body) > 200: body = body[:197] + "…"
    ref_match = re.search(r'#(\d+)', plain_text); thread_match = re.search(r'\[thread:([^\]]+)\]', plain_text)
    header = f"{icon} <b>{tag.title()}</b>"
    if ref_match:
        num = ref_match.group(1); repo = (metadata or {}).get("repo", "BasedHardware/omi")
        gh_url = f"https://github.com/{repo}/issues/{num}"; header += f' <a href="{gh_url}">#{num}</a>'
    elif thread_match: header += f" {_html.escape('thread:' + thread_match.group(1))}"
    parts = [header, _html.escape(body)]
    if serve_url: parts.append(f'<a href="{_html.escape(serve_url)}">View full →</a>')
    return "\n".join(parts)

def connector_export_github(number: int, repo: str) -> str | None:
    import subprocess
    try:
        r = core._subprocess_runner.run(
            ["beast", "github", "export", str(number), "--format", "print", "--serve", "--repo", repo, "--fresh"],
            capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
        if r.returncode == 0:
            for line in r.stderr.splitlines() + r.stdout.splitlines():
                if "http" in line and ("localhost" in line or "serve" in line.lower()):
                    url = line.strip().split()[-1].rstrip("/")
                    if "localhost" in url:
                        host = (urlparse(BRIDGE_PUBLIC_URL).hostname if BRIDGE_PUBLIC_URL else None) or "157.180.48.254"
                        url = url.replace("localhost", host)
                    return url
    except subprocess.SubprocessError as e: _log(_LOG_WARN, "github", f"export failed for #{number}: {e}")
    return None

def connector_on_message(tag: str) -> Callable:
    def handler(targets, html_text, plain_text=None, attachments=None, metadata=None):
        import urllib.error
        from bridge import send_to_worker, _beast_serve_deploy
        if plain_text is None: plain_text = html_text
        connector_log_message(tag, html_text, plain_text, targets)
        if core.admin_chat_id:
            serve_url = None
            if tag == "github" and metadata and metadata.get("number"):
                try:
                    serve_url = connector_export_github(metadata["number"], metadata.get("repo", "BasedHardware/omi"))
                except KeyError as e: _log(_LOG_WARN, tag, f"github export failed: {e}")
            if not serve_url:
                try:
                    page_html = connector_render_html(tag, html_text); tmp_path = f"/tmp/connector-{tag}.html"
                    with open(tmp_path, "w") as f: f.write(page_html)
                    serve_url = _beast_serve_deploy(tmp_path, f"connector-{tag}")
                except OSError as e: _log(_LOG_WARN, tag, f"beast serve failed: {e}")
            summary = connector_short_summary(tag, plain_text, serve_url, metadata)
            try:
                telegram.send_telegram_message(core.admin_chat_id, summary, parse_mode="HTML")
            except OSError:
                try:
                    telegram.send_telegram_message(core.admin_chat_id, plain_text[:300])
                except (urllib.error.URLError, OSError, TimeoutError) as e: _log(_LOG_WARN, tag, f"Telegram send failed: {e}")
            for att in (attachments or []):
                fpath = att.get("path", ""); fname = att.get("filename", "")
                if not fpath or not os.path.isfile(fpath): continue
                ext = os.path.splitext(fname)[1].lower(); caption = f"📧 {fname}"
                if ext in ALLOWED_IMAGE_EXTENSIONS: telegram.send_photo(core.admin_chat_id, fpath, caption)
                elif ext in VIDEO_EXTENSIONS: telegram.send_video(core.admin_chat_id, fpath, caption)
                else: telegram.send_document(core.admin_chat_id, fpath, caption)
                _log(_LOG_INFO, tag, f"attachment -> Telegram: {fname}")
        if targets:
            for name in targets:
                send_to_worker(name, plain_text)
                _log(_LOG_INFO, tag, f"-> {name}: {plain_text[:80]}...")
        else: _log(_LOG_INFO, tag, f"-> Telegram only (no mentions): {plain_text[:80]}...")
    return handler

def connector_get_workers() -> set[str]:
    return set(health.get_registered_sessions().keys())

def connector_on_alert(tag: str) -> Callable:
    def handler(text: str) -> None:
        import urllib.error
        if core.admin_chat_id:
            try:
                telegram.send_telegram_message(core.admin_chat_id, text)
            except (urllib.error.URLError, OSError, TimeoutError) as e: _log(_LOG_WARN, tag, f"Failed to send Telegram alert: {e}")
    return handler

def new_gmail() -> GmailConnector:
    from bridge import GmailConnector as _GmailConnector, GMAIL_GWS_BIN, GMAIL_FROM_FILTER, GMAIL_POLL_INTERVAL
    return _GmailConnector(gws_bin=GMAIL_GWS_BIN, from_filter=GMAIL_FROM_FILTER, poll_interval=GMAIL_POLL_INTERVAL,
        on_message=connector_on_message("gmail"), get_registered_workers=connector_get_workers, on_alert=connector_on_alert("gmail"))

def new_github() -> GitHubConnector:
    from bridge import GitHubConnector as _GitHubConnector, GITHUB_REPOS, GITHUB_FROM_USER, GITHUB_POLL_INTERVAL
    return _GitHubConnector(repo=GITHUB_REPOS, from_user=GITHUB_FROM_USER, poll_interval=GITHUB_POLL_INTERVAL,
        on_message=connector_on_message("github"), get_registered_workers=connector_get_workers,
        on_alert=connector_on_alert("github"), state_file=str(NODE_DIR / "github_state.json"))

def start_connectors() -> tuple[object, object]:
    from bridge import (GMAIL_ENABLED, GmailConnector as _GmailConnector, GMAIL_IMPORT_ERROR,
                        GMAIL_POLL_INTERVAL, GMAIL_FROM_FILTER,
                        GITHUB_ENABLED, GitHubConnector as _GitHubConnector, GITHUB_IMPORT_ERROR,
                        GITHUB_POLL_INTERVAL, GITHUB_FROM_USER, GITHUB_REPOS)
    gmail_inst = None
    if GMAIL_ENABLED and _GmailConnector is not None:
        gmail_inst = new_gmail(); gmail_inst.start()
        print(f"Gmail connector: polling every {GMAIL_POLL_INTERVAL}s for {GMAIL_FROM_FILTER}")
    elif GMAIL_ENABLED and _GmailConnector is None: _log(_LOG_ERROR, "bridge", f"Gmail connector disabled: {GMAIL_IMPORT_ERROR}")
    github_inst = None
    if GITHUB_ENABLED and _GitHubConnector is not None:
        github_inst = new_github(); github_inst.start()
        print(f"GitHub connector: polling every {GITHUB_POLL_INTERVAL}s for {GITHUB_FROM_USER} on {', '.join(GITHUB_REPOS)}")
    elif GITHUB_ENABLED and _GitHubConnector is None: _log(_LOG_ERROR, "bridge", f"GitHub connector disabled: {GITHUB_IMPORT_ERROR}")
    return gmail_inst, github_inst

def restart_connector(name: str) -> tuple[bool, str]:
    from bridge import (GMAIL_ENABLED, GmailConnector as _GmailConnector, GMAIL_IMPORT_ERROR,
                        GITHUB_ENABLED, GitHubConnector as _GitHubConnector, GITHUB_IMPORT_ERROR,
                        connectors)
    _cfg = {"gmail": (GMAIL_ENABLED, _GmailConnector, GMAIL_IMPORT_ERROR, "GMAIL_ENABLED=0", new_gmail),
            "github": (GITHUB_ENABLED, _GitHubConnector, GITHUB_IMPORT_ERROR, "BRIDGE_GHPOLL_ENABLED=0", new_github)}
    entry = _cfg.get(name)
    if not entry: return False, f"Unknown connector: {name} (valid: gmail, github)"
    enabled, cls, import_err, env_name, factory = entry
    if not enabled: return False, f"{name.title()} connector not enabled ({env_name})"
    if cls is None: return False, f"{name.title()} connector import failed: {import_err}"
    old = getattr(connectors, name, None)
    if old is not None: old.stop()
    inst = factory(); setattr(connectors, name, inst)
    ok, msg = inst.restart()
    print(f"{name.title()} connector {'restarted' if ok else 'restart failed'}: {msg}"); return ok, msg

def get_connectors_status() -> dict[str, ConnectorStatusDict]:
    from bridge import GMAIL_ENABLED, GITHUB_ENABLED, connectors
    result: dict[str, ConnectorStatusDict] = {}
    for cname, enabled in [("gmail", GMAIL_ENABLED), ("github", GITHUB_ENABLED)]:
        if not enabled: result[cname] = {"name": cname, "running": False, "enabled": False}
        else:
            inst = getattr(connectors, cname, None)
            result[cname] = cast("ConnectorStatusDict", inst.status()) if inst else {"name": cname, "running": False, "error": "not initialized"}
    return result
