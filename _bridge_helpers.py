"""Bridge helper functions — machines, output, learning, codex, checkin."""
from __future__ import annotations
import collections
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, cast
from urllib.parse import urlparse
from core import (
    _log, _LOG_ERROR, _LOG_WARN, _LOG_INFO, _LOG_DEBUG,
    _clock, _subprocess_runner,
    BRIDGE_URL, BRIDGE_PUBLIC_URL, BRIDGE_SSH_TARGET, MACHINES_CONFIG_FILE,
    TIMEOUT_FILE_TRANSFER, TIMEOUT_GIT_OP, TIMEOUT_RSYNC,
    DELAY_BRIEF, DELAY_RESPONSE_GAP,
    DEFAULT_BACKEND, RESTART_COOLDOWN,
    _LEARNING_REMINDER_PATH,
)
from claudecode import (
    _detect_os_family, _find_codex_transcript, _remote_run,
    _LEARNING_REMINDER_TEXT,
    is_claude_running, remote_cache,
    watchdog, learning_reminders, host_health,
    LEARNING_REMINDER_RESPONSE_THRESHOLD, LEARNING_REMINDER_IDLE_HOURS,
    REWIND_TOKENS, PR_REVIEW_TOKENS,
    get_backend, is_valid_backend, list_backends,
)
from telegram import (
    ChatId, MessageId, MessageTransport, ParseMode,
    TelegramApiResponse, TelegramMessageDict, FileValidation,
    send_photo, send_document, send_animation, send_video,
    send_audio, send_voice, send_sticker, send_telegram_message,
    split_message, format_multipart_messages, markdown_to_telegram_html,
    parse_image_tags, parse_file_tags, _parse_media_tags, transport,
    TELEGRAM_MAX_LENGTH, TELEGRAM_RICH_MAX_LENGTH,
    VIDEO_EXTENSIONS, AUDIO_EXTENSIONS, VOICE_EXTENSIONS, STICKER_EXTENSIONS,
    get_manager_chat_id,
)
from markdown_fmt import _pipe_tables_to_html
from _types import (
    MachinePublicDict, MachineHealthDict, MachinesCatalogResponse,
    NodeConfigDict, MentionRouteResult, ParsedWorkerTarget,
    ReminderState, RewindTokenEntry, PrReviewTokenEntry,
    ConnectorMessageLogEntry, CodexTranscriptEntry,
    TmuxSessionDict,
)
class MachineConfigError(ValueError):
    pass
@dataclass
class Machine:
    id: str; ssh_target: str | None; bridge_base_url: str; home_root: str; os_family: str; display_name: str = ""; tailscale_ip: str = ""
    role: str = ""; configured: bool = True
    @property
    def is_local(self) -> bool:
        return self.ssh_target is None
    def public_dict(self) -> MachinePublicDict:
        return {"id": self.id, "display_name": self.display_name or self.id, "ssh_target": self.ssh_target,
            "bridge_base_url": self.bridge_base_url, "home_root": self.home_root, "os_family": self.os_family,
            "tailscale_ip": self.tailscale_ip, "role": self.role, "configured": self.configured}
def _validate_machine_id(machine_id: str) -> str:
    if not isinstance(machine_id, str) or not machine_id: raise MachineConfigError("machine id must be a non-empty string")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", machine_id): raise MachineConfigError(f"invalid machine id: {machine_id!r}")
    return machine_id
def _coerce_optional_str(value: object, field: str, machine_id: str) -> str:
    if value is None: return ""
    if not isinstance(value, str): raise MachineConfigError(f"machine {machine_id!r} field {field!r} must be a string")
    return value
def _implicit_local_machine() -> Machine:
    _id = BRIDGE_SSH_TARGET or "vps"
    return Machine(id=_id, ssh_target=None, bridge_base_url=BRIDGE_URL, home_root=str(Path.home()),
        os_family=_detect_os_family(), display_name=_id.upper(), role="bridge", configured=False)
def load_machines_config(path: Path | None = None) -> dict[str, Machine]:
    config_path = Path(path) if path is not None else MACHINES_CONFIG_FILE
    if not config_path.exists(): return {_implicit_local_machine().id: _implicit_local_machine()}
    try: data = cast(NodeConfigDict, json.loads(config_path.read_text()))
    except json.JSONDecodeError as e: raise MachineConfigError(f"{config_path}: invalid JSON: {e}") from e
    except OSError as e: raise MachineConfigError(f"{config_path}: cannot read: {e}") from e
    if not isinstance(data, dict): raise MachineConfigError(f"{config_path}: root must be an object")
    if data.get("version") != 1: raise MachineConfigError(f"{config_path}: version must be 1")
    raw_machines = data.get("machines")
    if not isinstance(raw_machines, dict) or not raw_machines: raise MachineConfigError(f"{config_path}: machines must be a non-empty object")
    machines: dict[str, Machine] = {}; ssh_targets: dict[str, str] = {}; local_count = 0
    for raw_id, raw in raw_machines.items():
        machine_id = _validate_machine_id(raw_id)
        if not isinstance(raw, dict): raise MachineConfigError(f"{config_path}: machine {machine_id!r} must be an object")
        missing = [k for k in ("ssh_target", "bridge_base_url", "home_root", "os_family") if k not in raw]
        if missing: raise MachineConfigError(f"{config_path}: machine {machine_id!r} missing {', '.join(missing)}")
        ssh_target = raw.get("ssh_target")
        if ssh_target is not None and not isinstance(ssh_target, str): raise MachineConfigError(f"{config_path}: machine {machine_id!r} ssh_target must be string or null")
        if ssh_target == "": raise MachineConfigError(f"{config_path}: machine {machine_id!r} ssh_target cannot be empty")
        if ssh_target is None: local_count += 1
        elif ssh_target in ssh_targets:
            raise MachineConfigError(
                f"{config_path}: ssh_target {ssh_target!r} used by both "
                f"{ssh_targets[ssh_target]!r} and {machine_id!r}" )
        else: ssh_targets[ssh_target] = machine_id
        bridge_base_url = _coerce_optional_str(raw.get("bridge_base_url"), "bridge_base_url", machine_id).rstrip("/")
        home_root = _coerce_optional_str(raw.get("home_root"), "home_root", machine_id).rstrip("/")
        os_family = _coerce_optional_str(raw.get("os_family"), "os_family", machine_id)
        if not bridge_base_url: raise MachineConfigError(f"{config_path}: machine {machine_id!r} bridge_base_url cannot be empty")
        if not home_root.startswith("/"): raise MachineConfigError(f"{config_path}: machine {machine_id!r} home_root must be absolute")
        if os_family not in ("linux", "darwin"): raise MachineConfigError(f"{config_path}: machine {machine_id!r} os_family must be linux or darwin")
        machines[machine_id] = Machine(id=machine_id, ssh_target=ssh_target, bridge_base_url=bridge_base_url,
            home_root=home_root, os_family=os_family,
            display_name=_coerce_optional_str(raw.get("display_name", machine_id), "display_name", machine_id),
            tailscale_ip=_coerce_optional_str(raw.get("tailscale_ip", ""), "tailscale_ip", machine_id),
            role=_coerce_optional_str(raw.get("role", ""), "role", machine_id))
    if local_count != 1: raise MachineConfigError(f"{config_path}: exactly one local machine with ssh_target=null is required")
    return machines
def get_machine_catalog(force_reload: bool = False) -> dict[str, Machine]:
    with remote_cache.lock:
        if force_reload or remote_cache.machines is None or remote_cache.machines_path != MACHINES_CONFIG_FILE:
            remote_cache.machines = load_machines_config(MACHINES_CONFIG_FILE); remote_cache.machines_path = MACHINES_CONFIG_FILE
        return dict(remote_cache.machines)
def _machine_for_worker_host(host: str | None, machines: dict[str, Machine]) -> Machine:
    if host is None:
        for machine in machines.values():
            if machine.is_local: return machine
        return _implicit_local_machine()
    for machine in machines.values():
        if machine.ssh_target == host: return machine
    machine_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", host).strip("-") or "unknown"
    return Machine(
        id=machine_id,
        ssh_target=host,
        bridge_base_url=BRIDGE_PUBLIC_URL or "",
        home_root="",
        os_family="",
        display_name=host,
        role="worker-host",
        configured=False, )
def _machine_health(machine: Machine) -> MachineHealthDict:
    host_label = machine.ssh_target or "VPS"
    with watchdog.lock:
        health: MachineHealthDict = {"status": "up" if machine.is_local else "unknown", "down_since": None, "last_error": None,
            "disk": host_health.disk_usage.get(host_label), "memory": host_health.mem_usage.get(host_label), "io": host_health.io_usage.get(host_label)}
        if machine.ssh_target:
            if host_health.down.get(machine.ssh_target, False):
                health["status"] = "down"; health["down_since"] = host_health.down_since.get(machine.ssh_target)
                health["last_error"] = host_health.last_error.get(machine.ssh_target)
            elif (
                machine.ssh_target in host_health.ssh_failures
                or machine.ssh_target in host_health.disk_usage
                or machine.ssh_target in host_health.mem_usage
                or machine.ssh_target in host_health.io_usage ):
                health["status"] = "up"
    return health
def _machine_access(machine: Machine, caller_host: str | None) -> str:
    target_host = machine.ssh_target
    if caller_host == target_host: return "local"
    if target_host is None: return f"ssh {BRIDGE_SSH_TARGET}"
    return f"ssh {target_host}"
def get_machines(caller_from: str | None = None) -> MachinesCatalogResponse:
    import bridge as _b
    machines = get_machine_catalog(); registered = _b.get_registered_sessions()
    caller_info = registered.get(caller_from, {}) if caller_from else {}
    caller_host = caller_info.get("host") if caller_info else (_b.get_worker_host(caller_from) if caller_from else None)
    rows: dict[str, MachinePublicDict] = {}
    for machine in machines.values():
        row = machine.public_dict(); row["access"] = _machine_access(machine, caller_host); row["workers"] = []; row["worker_count"] = 0
        row["health"] = _machine_health(machine); rows[machine.id] = row
    for name, info in registered.items():
        host = info.get("host") if "host" in info else _b.get_worker_host(name); machine = _machine_for_worker_host(host, machines)
        if machine.id not in rows:
            row = machine.public_dict(); row["access"] = _machine_access(machine, caller_host); row["workers"] = []; row["worker_count"] = 0
            row["health"] = _machine_health(machine); rows[machine.id] = row
        rows[machine.id]["workers"].append({"name": name, "backend": info.get("backend", DEFAULT_BACKEND),
            "status": "online" if info.get("tmux") or info.get("callback_url") else "exited"})
        rows[machine.id]["worker_count"] += 1
    return {"version": 1, "config_path": str(MACHINES_CONFIG_FILE), "caller": caller_from or None, "machines": list(rows.values())}
def parse_hire_args(raw: str) -> tuple[str, str]:
    parts = [p for p in (raw or "").split() if p]; backend = DEFAULT_BACKEND; name_parts = []; i = 0
    while i < len(parts):
        part = parts[i]
        if part == "--backend" and i + 1 < len(parts): backend = parts[i + 1]; i += 2; continue
        elif part == "--codex": backend = "codex"
        elif part.startswith("--"): pass
        else: name_parts.append(part)
        i += 1
    if len(name_parts) != 1: return "", backend
    name = name_parts[0]
    for backend_name in list_backends():
        prefix = f"{backend_name}-"
        if name.startswith(prefix): backend = backend_name; name = name[len(prefix):]; break
    if not is_valid_backend(backend): return name, backend
    return name, backend
def _remote_copy(src: str, dst: str, host: str | None = None, direction: str = "push") -> None:
    if not host: shutil.copy2(src, dst)
    elif direction == "push": _subprocess_runner.run(["scp", "-q", src, f"{host}:{dst}"], capture_output=True, timeout=TIMEOUT_FILE_TRANSFER)
    else: _subprocess_runner.run(["scp", "-q", f"{host}:{src}", dst], capture_output=True, timeout=TIMEOUT_FILE_TRANSFER)
def parse_worker_target(target: str) -> ParsedWorkerTarget:
    if "@" in target:
        name, host = target.rsplit("@", 1)
        return ParsedWorkerTarget(name, host)
    return ParsedWorkerTarget(target, None)
def _read_learning_reminder(name: str) -> str:
    try:
        if os.path.isfile(_LEARNING_REMINDER_PATH):
            text = Path(_LEARNING_REMINDER_PATH).read_text().strip()
            if text: return text.replace("{name}", name)
    except OSError as e: _log(_LOG_WARN, "bridge", f"Failed to read learning reminder from {_LEARNING_REMINDER_PATH}: {e}")
    return _LEARNING_REMINDER_TEXT.replace("{name}", name)
def _new_reminder_state() -> ReminderState:
    now = _clock.time()
    return { "response_count": 0, "last_reminder_ts": now, "last_response_ts": now, "reminder_pending": False, }
def _reset_learning_reminder(name: str) -> None:
    with learning_reminders.lock: learning_reminders.state[name] = _new_reminder_state()
def _fire_reminder(name: str, st: ReminderState) -> None:
    import bridge as _b
    st["response_count"] = 0; st["last_reminder_ts"] = _clock.time(); st["reminder_pending"] = True
    _b._task_pool.submit(_send_learning_reminder, name, _read_learning_reminder(name))
def _check_learning_reminder(name: str) -> None:
    with learning_reminders.lock:
        st = learning_reminders.state.get(name)
        if st is None: st = _new_reminder_state(); learning_reminders.state[name] = st
        st["last_response_ts"] = _clock.time()
        if st.get("reminder_pending"): st["reminder_pending"] = False; st["response_count"] = 1; return
        st["response_count"] = st.get("response_count", 0) + 1
        if st["response_count"] >= LEARNING_REMINDER_RESPONSE_THRESHOLD: _fire_reminder(name, st)
def _scan_idle_workers() -> None:
    try:
        now = _clock.time(); idle_threshold = LEARNING_REMINDER_IDLE_HOURS * 3600; to_fire = []
        with learning_reminders.lock:
            for name, st in learning_reminders.state.items():
                if st.get("reminder_pending"): continue
                if st.get("response_count", 0) <= 1: continue
                idle_seconds = now - st.get("last_response_ts", now)
                since_reminder = now - st.get("last_reminder_ts", now)
                if idle_seconds >= idle_threshold and since_reminder >= idle_threshold: to_fire.append(name)
            for name in to_fire: _fire_reminder(name, learning_reminders.state[name])
    except KeyError as e: _log(_LOG_ERROR, "bridge", f"Learning reminder idle scan error: {e}")
    finally: _schedule_idle_scan()
def _seed_learning_reminder_state(worker_names: Iterable[str]) -> None:
    with learning_reminders.lock:
        for name in worker_names:
            if name not in learning_reminders.state: learning_reminders.state[name] = _new_reminder_state()
def _schedule_idle_scan() -> None:
    learning_reminders.idle_scan_timer = threading.Timer(1800, _scan_idle_workers)
    learning_reminders.idle_scan_timer.name = "idle-scan"
    learning_reminders.idle_scan_timer.daemon = True
    learning_reminders.idle_scan_timer.start()
def _send_learning_reminder(name: str, text: str) -> None:
    import bridge as _b
    try:
        _clock.sleep(DELAY_RESPONSE_GAP)
        if _b.send_to_worker(name, text): _log(_LOG_INFO, "worker", f"Learning reminder sent to {name}")
        else: _log(_LOG_WARN, "bridge", f"Learning reminder: failed to send to {name}")
    except (ConnectionError, OSError, TimeoutError) as e: _log(_LOG_ERROR, "bridge", f"Learning reminder error for {name}: {e}")
class ConnectorRegistry:
    def __init__(self) -> None:
        from typing import Any as _Any
        self.gmail: _Any = None  # type: ignore[explicit-any]
        self.github: _Any = None  # type: ignore[explicit-any]
        self._log: dict[str, collections.deque[ConnectorMessageLogEntry]] = {}
        self._lock: threading.Lock = threading.Lock()
    def stop_all(self) -> None:
        for name, inst in [("gmail", self.gmail), ("github", self.github)]:
            if inst is not None:
                try:
                    inst.stop()
                    print(f"{name.title()} connector stopped")
                except (RuntimeError, OSError) as exc: _log(_LOG_DEBUG, f"shutdown:{name}", f"{type(exc).__name__}: {exc}")
    def log_message(self, tag: str, html_text: str, plain_text: str, targets: list[str]) -> None:
        with self._lock:
            if tag not in self._log: self._log[tag] = collections.deque(maxlen=20)
            self._log[tag].append({
                "ts": _clock.time(),
                "html": html_text,
                "plain": plain_text,
                "targets": targets or [],
            })
    def get_log(self, tag: str) -> list[ConnectorMessageLogEntry]:
        with self._lock: return list(self._log.get(tag, []))
def _parse_codex_transcript(path: str, host: str | None = None) -> list[CodexTranscriptEntry]:
    try:
        if host:
            r = _remote_run(["cat", path], host=host, capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
            if r.returncode != 0: return []
            content = r.stdout
        else: content = Path(path).read_text()
    except (subprocess.SubprocessError, OSError): return []
    messages = []
    for line in content.strip().split("\n"):
        if not line.strip(): continue
        try: ev = cast(dict[str, object], json.loads(line))
        except json.JSONDecodeError: continue
        ev_type = str(ev.get("type", "")); raw_payload = ev.get("payload", {})
        payload = cast(dict[str, object], raw_payload) if isinstance(raw_payload, dict) else {}
        ts = str(ev.get("timestamp", ""))
        if ev_type == "response_item":
            item_type = str(payload.get("type", ""))
            if item_type == "message":
                role = str(payload.get("role", "assistant"))
                text_parts: list[str] = []
                raw_content = payload.get("content", [])
                for c in cast(list[dict[str, object]], raw_content) if isinstance(raw_content, list) else []:
                    if isinstance(c, dict) and c.get("type") in ("output_text", "input_text") and c.get("text"): text_parts.append(str(c["text"]))
                text = "\n".join(text_parts).strip()
                if text and role != "developer": messages.append({"role": role, "text": text, "timestamp": ts})
            elif item_type == "function_call":
                name = str(payload.get("name", ""))
                messages.append({"role": "tool_use", "text": f"[{name}]", "timestamp": ts})
            elif item_type == "function_call_output":
                output = str(payload.get("output", ""))
                if output: messages.append({"role": "tool_result", "text": output[:500], "timestamp": ts})
    return cast(list[CodexTranscriptEntry], messages)
def _read_codex_transcript(worker_name: str) -> list[CodexTranscriptEntry]:
    import bridge as _b
    host = _b.get_worker_host(worker_name); path = _find_codex_transcript(worker_name, host=host)
    if not path: return []
    return _parse_codex_transcript(path, host=host)
REWIND_TIMEOUT: int = 24 * 60 * 60
PR_REVIEW_EXTEND: int = 300
class TokenStore:
    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
    def _validate(self, store: dict[str, dict[str, object]], token: str | None, extend: bool, ttl: int) -> dict[str, object] | None:
        if not token: return None
        now = _clock.time()
        with self._lock:
            for k in [k for k, v in store.items() if v["expires_at"] <= now]: del store[k]
            entry = store.get(token)
            if entry and extend: entry["expires_at"] = now + ttl
            return entry
    def add_rewind(self, token: str, name: str, timeout: int = REWIND_TIMEOUT) -> None:
        with self._lock: REWIND_TOKENS[token] = {"name": name, "expires_at": _clock.time() + timeout}
    def validate_rewind(self, token: str | None, extend: bool = True) -> RewindTokenEntry | None:
        return self._validate(REWIND_TOKENS, token, extend, REWIND_TIMEOUT)
    def add_pr_review(self, token: str, pr_num: int, owner: str, repo: str) -> None:
        with self._lock:
            PR_REVIEW_TOKENS[token] = {"pr_num": pr_num, "owner": owner, "repo": repo,
                                       "expires_at": _clock.time() + PR_REVIEW_EXTEND}
    def validate_pr_review(self, token: str | None, extend: bool = True) -> PrReviewTokenEntry | None:
        return self._validate(PR_REVIEW_TOKENS, token, extend, PR_REVIEW_EXTEND)
def _extract_file_id(msg: TelegramMessageDict) -> tuple[str | None, str]:
    if msg.get("animation"): return msg["animation"].get("file_id"), "GIF"
    if msg.get("photo"): return max(msg["photo"], key=lambda p: p.get("file_size", 0)).get("file_id"), "image"
    doc = msg.get("document")
    if doc:
        if doc.get("mime_type", "").startswith("image/"): return doc.get("file_id"), "image"
        return doc.get("file_id"), "file"
    for key, label in [("video","video"),("audio","audio"),("voice","voice"),("video_note","video note"),("sticker","sticker")]:
        if msg.get(key): return msg[key].get("file_id"), label
    return None, "media"
def _format_offline_warning(statuses: list[MentionRouteResult | None]) -> str | None:
    sent_to = [s["name"] for s in statuses if s and s.get("status") == "sent"]
    offline = [s["name"] for s in statuses if s and s.get("status") == "offline"]
    if not offline: return None
    parts = [f"⚠️ {', '.join(offline)} {'is' if len(offline) == 1 else 'are'} offline."]
    if sent_to: parts.append(f"Delivered to {', '.join(sent_to)}.")
    return " ".join(parts)
def _gh_api_call(args: list[str], *, input_data: str | None = None,
                 timeout: int = TIMEOUT_FILE_TRANSFER) -> tuple[bool, str]:
    try:
        kw: dict[str, object] = {"capture_output": True, "text": True, "timeout": timeout}
        if input_data is not None: kw["input"] = input_data
        r = _subprocess_runner.run(["gh", "api"] + args, **kw)
        if r.returncode != 0: return False, r.stderr.strip() or r.stdout.strip()
        return True, r.stdout
    except subprocess.TimeoutExpired: return False, "timeout"
def _fetch_remote_file(host: str, remote_path: str) -> str | None:
    original_name = Path(remote_path).name; tmp_dir = tempfile.mkdtemp(prefix="remote-file-")
    local_path = os.path.join(tmp_dir, original_name)
    try:
        r = _subprocess_runner.run(
            ["rsync", "-az", f"{host}:{remote_path}", local_path],
            capture_output=True, text=True, timeout=TIMEOUT_RSYNC)
        if r.returncode == 0 and os.path.getsize(local_path) > 0: return local_path
        if r.returncode != 0: _log(_LOG_ERROR, "bridge", f"rsync failed (exit {r.returncode}): {host}:{remote_path} -> {r.stderr.strip()}")
    except (subprocess.SubprocessError, OSError) as e: _log(_LOG_WARN, "bridge", f"Remote file fetch failed: {host}:{remote_path} -> {e}")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return None
def _localize_media(name: str, media_list: list[tuple[str | None, str]]) -> list[tuple[str | None, str]]:
    import bridge as _b
    host = _b.get_worker_host(name)
    if not host: return media_list
    result: list[tuple[str | None, str]] = []
    for file_path, caption in media_list:
        if not file_path:
            result.append((file_path, caption))
            continue
        local = _fetch_remote_file(host, file_path)
        if local: result.append((local, caption))
        else:
            _log(_LOG_WARN, "bridge", f"Cannot fetch remote file {host}:{file_path} for {name}")
            result.append((None, f"[Fetch failed: {file_path}]"))
    return result
def _parse_response_media(name: str, text: str) -> tuple[str, list[tuple[str | None, str]], list[tuple[str | None, str]]]:
    import bridge as _b
    host = _b.get_worker_host(name)
    if host:
        _accept_all: Callable[[str | Path], FileValidation] = lambda p: FileValidation(True, Path(p))
        clean_text, images = _parse_media_tags(text, "image", _accept_all)
        clean_text, files = _parse_media_tags(clean_text, "file", _accept_all)
    else:
        clean_text, images = parse_image_tags(text)
        clean_text, files = parse_file_tags(clean_text)
    images = _localize_media(name, images); files = _localize_media(name, files)
    return clean_text, images, files
def _send_text_via_telegram(name: str, clean_text: str, chat_id: int, log_prefix: str) -> None:
    rich_sent = False; rich_failed_at = -1
    rich_chunks: list[str] = []
    prev_msg_id: int | None = None
    if hasattr(transport, 'send_rich_text'):
        rich_text = clean_text.lstrip(); prefix_lower = f"{name}:".lower()
        if rich_text.lower().startswith(prefix_lower): rich_text = rich_text[len(prefix_lower):].lstrip()
        rich_text = _pipe_tables_to_html(rich_text); rich_md = f"**{name}:**\n{rich_text}"
        prefix_reserve = len(name) + 30; rich_chunks = split_message(rich_md, TELEGRAM_RICH_MAX_LENGTH - prefix_reserve)
        rich_sent = True
        for i, chunk in enumerate(rich_chunks):
            if i > 0: chunk = f"**{name}:** _(continued)_\n{chunk}"
            result = transport.send_rich_text( chat_id, chunk, reply_to=prev_msg_id if prev_msg_id else None )
            if result and result.get("ok"):
                _rr = result.get("result", {}); prev_msg_id = _rr.get("message_id") if isinstance(_rr, dict) else None
                if len(rich_chunks) > 1: _log(_LOG_INFO, "telegram", f"{log_prefix} sent (rich): {name} part {i+1}/{len(rich_chunks)} -> Telegram OK")
                else: _log(_LOG_INFO, "telegram", f"{log_prefix} sent (rich): {name} -> Telegram OK")
            else:
                error_code = (result or {}).get("error_code", 0); desc = (result or {}).get("description", "")
                _log(_LOG_ERROR, "bridge", f"{log_prefix} sendRichMessage failed ({error_code}: {desc}), falling back to HTML")
                rich_sent = False; rich_failed_at = i
                break
            if i < len(rich_chunks) - 1: _clock.sleep(DELAY_BRIEF)
    if not rich_sent and rich_failed_at > 0:
        _send_as_html(name, '\n'.join(rich_chunks[rich_failed_at:]), chat_id, log_prefix, prev_msg_id)
        rich_sent = True
    if not rich_sent: _send_as_html(name, clean_text, chat_id, log_prefix)
def _send_as_html(name: str, text: str, chat_id: int, log_prefix: str,
                  prev_msg_id: int | None = None) -> int | None:
    html_text = markdown_to_telegram_html(text); prefix_reserve = len(name) + 30
    chunks = split_message(html_text, TELEGRAM_MAX_LENGTH - prefix_reserve)
    formatted_parts = format_multipart_messages(name, chunks)
    for i, part in enumerate(formatted_parts):
        result = transport.send_text( chat_id, part, parse_mode="HTML", reply_to=prev_msg_id if prev_msg_id else None )
        if result and result.get("ok"):
            _rr = result.get("result", {}); prev_msg_id = _rr.get("message_id") if isinstance(_rr, dict) else None
            if len(formatted_parts) > 1: _log(_LOG_INFO, "telegram", f"{log_prefix} sent: {name} part {i+1}/{len(formatted_parts)} -> Telegram OK")
            else: _log(_LOG_INFO, "telegram", f"{log_prefix} sent: {name} -> Telegram OK")
        else:
            desc = str((result or {}).get("description", ""))
            _log(_LOG_WARN, "bridge", f"{log_prefix} HTML send failed ({desc}), retrying as plain text")
            plain_text = re.sub(r'<[^>]+>', '', part)
            plain_text = plain_text.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
            result = transport.send_text( chat_id, plain_text, reply_to=prev_msg_id if prev_msg_id else None )
            if result and result.get("ok"): _rr = result.get("result", {}); prev_msg_id = _rr.get("message_id") if isinstance(_rr, dict) else None
        if i < len(formatted_parts) - 1: _clock.sleep(DELAY_BRIEF)
    return prev_msg_id
def _send_response_media(name: str, images: list[tuple[str | None, str]], files: list[tuple[str | None, str]], chat_id: int) -> None:
    for img_path, img_caption in images:
        if img_path is None:
            transport.send_text(chat_id, f"{name}: {img_caption}")
            continue
        full_caption = f"{name}: {img_caption}" if img_caption else f"{name}:"
        if Path(img_path).suffix.lower() in (".gif", ".mp4"): sent = send_animation(chat_id, img_path, full_caption)
        else: sent = send_photo(chat_id, img_path, full_caption)
        if sent: _log(_LOG_INFO, "telegram", f"Image sent: {name} -> {img_path}")
        else: transport.send_text(chat_id, f"{name}: [Image failed: {img_path}]")
    for file_path, file_caption in files:
        if file_path is None:
            transport.send_text(chat_id, f"{name}: {file_caption}")
            continue
        full_caption = f"{name}: {file_caption}" if file_caption else f"{name}:"; ext = Path(file_path).suffix.lower()
        if ext in VIDEO_EXTENSIONS: sent = send_video(chat_id, file_path, full_caption)
        elif ext in AUDIO_EXTENSIONS: sent = send_audio(chat_id, file_path, full_caption)
        elif ext in VOICE_EXTENSIONS: sent = send_voice(chat_id, file_path, full_caption)
        elif ext in STICKER_EXTENSIONS: sent = send_sticker(chat_id, file_path)
        else: sent = send_document(chat_id, file_path, full_caption)
        if sent: _log(_LOG_INFO, "telegram", f"File sent: {name} -> {file_path}")
        else: transport.send_text(chat_id, f"{name}: [File failed: {file_path}]")
def send_response_to_telegram(name: str, text: str, chat_id: int, log_prefix: str = "Response") -> None:
    clean_text, images, files = _parse_response_media(name, text)
    if clean_text and len(clean_text.strip()) <= 5:
        _log(_LOG_DEBUG, "telegram", f"{log_prefix} short msg: {name}, text={repr(clean_text)}, "
             f"images={len(images)}, files={len(files)}")
    if clean_text: _send_text_via_telegram(name, clean_text, chat_id, log_prefix)
    _send_response_media(name, images, files, chat_id)
def _beast_serve_deploy(html_path: str, slug: str) -> str | None:
    try:
        r = _subprocess_runner.run(
            ["beast", "serve", "deploy", html_path, "--slug", slug, "--output-json"],
            capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
        if r.returncode == 0:
            data = cast(dict[str, object], json.loads(r.stdout)); url = str(data.get("url", ""))
            if url and "localhost" in url:
                host = urlparse(BRIDGE_PUBLIC_URL).hostname if BRIDGE_PUBLIC_URL else "157.180.48.254"
                url = url.replace("localhost", str(host))
            return url or None
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e: _log(_LOG_WARN, "bridge", f"beast serve deploy failed for {slug}: {e}")
    return None
class _LegacyTransportAdapter(MessageTransport):
    def __init__(self, legacy: Any) -> None: self._legacy = legacy
    @property
    def name(self) -> str: return "legacy-adapter"
    def send_text(self, chat_id: ChatId, text: str, parse_mode: ParseMode = None, reply_to: MessageId | None = None) -> TelegramApiResponse:
        result = self._legacy.send_message(chat_id, text); return result if result else {"ok": True, "result": {"message_id": 1}}
    def send_photo(self, chat_id: ChatId, photo_path: str | Path, caption: str | None = None) -> bool: return False
    def send_document(self, chat_id: ChatId, doc_path: str | Path, caption: str | None = None) -> bool: return False
    def send_animation(self, chat_id: ChatId, animation_path: str | Path, caption: str | None = None) -> bool: return False
    def send_video(self, chat_id: ChatId, video_path: str | Path, caption: str | None = None) -> bool: return False
    def send_audio(self, chat_id: ChatId, audio_path: str | Path, caption: str | None = None) -> bool: return False
    def send_voice(self, chat_id: ChatId, voice_path: str | Path, caption: str | None = None) -> bool: return False
    def send_sticker(self, chat_id: ChatId, sticker_path: str | Path) -> bool: return False
    def send_chat_action(self, chat_id: ChatId, action: str) -> None: pass
    def set_reaction(self, chat_id: ChatId, message_id: MessageId, reaction: list[dict[str, str]]) -> None:
        if hasattr(self._legacy, 'set_reaction'): self._legacy.set_reaction(chat_id, message_id, reaction)
    def edit_message(self, chat_id: ChatId, message_id: MessageId, text: str, parse_mode: ParseMode = None) -> TelegramApiResponse:
        return {"ok": True, "result": {"message_id": message_id}}
    def setup_commands(self, commands: list[dict[str, str]]) -> None: pass
    def download_file(self, file_id: str, session_name: str) -> str | None: return None
def _checkin_can_restart(name: str, tmux_name: str,
                         host: str | None, pane_cwd: str,
                         requested_cwd: str) -> tuple[bool, str]:
    last_restart = watchdog.recent_restarts.get(name, 0); elapsed = _clock.time() - last_restart
    if elapsed < RESTART_COOLDOWN:
        if watchdog.force_restart_pending_cwd.pop(name, False): _log(_LOG_WARN, "checkin", f"{name}: cooldown bypassed (post-force CWD repair)")
        else:
            _log(_LOG_WARN, "checkin", f"{name}: BLOCKED restart (cooldown {elapsed:.0f}s < {RESTART_COOLDOWN}s)")
            return False, (f"Checkin restart blocked: {name} was restarted {elapsed:.0f}s ago "
                           f"(cooldown {RESTART_COOLDOWN}s). CWD mismatch: pane={pane_cwd} vs requested={requested_cwd}")
    if is_claude_running(tmux_name, host=host):
        _log(_LOG_INFO, "checkin", f"{name}: BLOCKED restart (Claude already running in tmux)")
        return False, (f"Checkin restart skipped: {name} has Claude running. "
                       f"CWD mismatch: pane={pane_cwd} vs requested={requested_cwd}")
    with watchdog.restart_lock:
        inflight_ts = watchdog.restart_in_progress.get(name)
        if inflight_ts and _clock.time() - inflight_ts < 120:
            _log(_LOG_INFO, "checkin", f"{name}: BLOCKED restart (in-flight since {_clock.time() - inflight_ts:.0f}s ago)")
            return False, f"Checkin restart blocked: {name} restart already in progress ({_clock.time() - inflight_ts:.0f}s)."
        watchdog.restart_in_progress[name] = _clock.time()
    return True, ""
def _checkin_do_restart(name: str, backend_name: str,
                        tmux_name: str, host: str | None,
                        requested_cwd: str) -> tuple[bool, str]:
    import bridge as _b
    notify_chat_id = get_manager_chat_id(name)
    try:
        if notify_chat_id is not None:
            send_telegram_message(
                notify_chat_id,
                f"{name} is restarting in a new directory. "
                "Messages during restart may be lost.", )
        if host:
            restart_backend = get_backend(backend_name)
            ok, err = _b.command_router._restart_remote_worker(
                name, backend_name, restart_backend, tmux_name, host, "relaunch")
        else: ok, err = _b.worker_manager.restart(name, mode="relaunch")
        watchdog.recent_restarts[name] = _clock.time()
        _log(_LOG_INFO, "checkin", f"{name}: restart result ok={ok}, err={err}")
        if not ok:
            if notify_chat_id is not None:
                send_telegram_message(
                    notify_chat_id,
                    f"{name} could not restart. "
                    f"Run /restart {name} before sending new messages.", )
            return False, err or "restart failed"
        if notify_chat_id is not None:
            if _b._wait_for_restart_ready(tmux_name, backend_name, host=host): send_telegram_message(notify_chat_id, f"{name} is ready. Safe to send messages now.")
            else:
                send_telegram_message(
                    notify_chat_id,
                    f"{name} restarted but is not ready yet. "
                    f"Hold messages for now. If this continues, run /restart {name}.", )
        return True, ""
    finally:
        with watchdog.restart_lock: watchdog.restart_in_progress.pop(name, None)
