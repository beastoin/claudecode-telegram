from __future__ import annotations
import re, threading
from pathlib import Path
from urllib.parse import ParseResult
from typing import Callable, NamedTuple, Optional, TypedDict


class ReminderState(TypedDict):
    response_count: int
    last_reminder_ts: float
    last_response_ts: float
    reminder_pending: bool


class WorkerStateEntry(NamedTuple):
    status: str
    reason: str
    since: float


class ParsedWorkerTarget(NamedTuple):
    name: str
    host: str | None


class TmuxActivityResult(NamedTuple):
    activity: str
    context_pct: str | None
    raw_lines: list[str] | None


class AuthorDetection(NamedTuple):
    author: str
    avatar_html: str
    display_text: str


class TmuxSessionDict(TypedDict, total=False):
    tmux: str
    backend: str
    host: str
    protocol: str
    callback_url: str
    version: str
    activity: str
    context_pct: str


class RegistryWorkerDict(TypedDict, total=False):
    backend: str
    chat_id: int | None
    hire_time: int
    host: str
    home_host: str | None
    home_cwd: str | None
    protocol: str
    callback_url: str
    version: str
    tools: dict[str, object]


class RegistryFileDict(TypedDict, total=False):
    version: int
    workers: dict[str, RegistryWorkerDict]


class RewindTokenEntry(TypedDict):
    name: str
    expires_at: float


class PrReviewTokenEntry(TypedDict):
    pr_num: int
    owner: str
    repo: str
    expires_at: float


class ProcStatsEntry(TypedDict):
    cpu: float
    state: str


class QuestionOption(TypedDict):
    num: int
    label: str
    selected: bool


class QuestionDetails(TypedDict):
    header: str
    options: list[QuestionOption]
    selected_num: int


class DiskUsageDict(TypedDict, total=False):
    pct: float
    free_gb: float
    total_gb: float
    ts: float


class MemUsageDict(TypedDict, total=False):
    pct: int | float
    used_gb: float
    total_gb: float
    avail_gb: float
    top_procs: list[dict[str, object]]
    ts: float


class IoUsageDict(TypedDict, total=False):
    iowait_pct: float
    read_iops: int
    write_iops: int
    util_pct: float
    ts: float


class WorktreeItemDict(TypedDict):
    path: str
    size_gb: float


class WorktreeUsageDict(TypedDict):
    total_gb: float
    items: list[WorktreeItemDict]
    ts: float


class CpuHogEntry(TypedDict, total=False):
    pid: int
    cpu: float
    etime_min: int
    cmd: str


class MachineHealthDict(TypedDict, total=False):
    status: str
    down_since: float | None
    last_error: str | None
    disk: DiskUsageDict | None
    memory: MemUsageDict | None
    io: IoUsageDict | None


class CodexTranscriptEntry(TypedDict, total=False):
    role: str
    text: str
    timestamp: str


class GitPushStateResult(TypedDict, total=False):
    orig_sha: str
    orig_branch: str
    staged_files: list[str]
    stash_sha: str | None


class TelegramApiResponseDict(TypedDict, total=False):
    ok: bool
    result: object
    description: str
    error_code: int


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


class WorkerEndpointInfo(TypedDict, total=False):
    name: str
    backend: str
    status: str
    host: str
    tmux: str
    protocol: str
    send_example: str
    machine: str
    address: str
    note: str


class MachinePublicDict(TypedDict, total=False):
    id: str
    display_name: str
    ssh_target: str | None
    bridge_base_url: str
    home_root: str
    os_family: str
    tailscale_ip: str
    role: str
    configured: bool
    access: str
    workers: list[dict[str, str]]
    worker_count: int
    health: MachineHealthDict


class MachinesCatalogResponse(TypedDict):
    version: int
    config_path: str
    caller: str | None
    machines: list[MachinePublicDict]


class ConnectorMessageLogEntry(TypedDict):
    ts: float
    html: str
    plain: str
    targets: list[str]


class ConnectorMetadataDict(TypedDict, total=False):
    number: int
    repo: str


class ConnectorAttachmentDict(TypedDict, total=False):
    path: str
    filename: str
    mimeType: str


class ConnectorStatusDict(TypedDict, total=False):
    name: str
    running: bool
    error: str
    enabled: bool


class MentionRouteResult(TypedDict):
    name: str
    status: str


class HookResponseBody(TypedDict, total=False):
    session: str
    text: str
    source: str
    backend: str
    escape: bool
    session_id: str
    name: str
    worker: str
    to: str
    target: str
    message: str


class HealthAlertBody(TypedDict, total=False):
    worker: str
    issue: str
    transcript_age: int
    node: str


class ForgeRegisterBody(TypedDict, total=False):
    Name: str
    name: str
    Host: str
    host: str
    Version: str
    version: str
    CallbackURL: str
    callback_url: str
    callbackUrl: str
    Tools: dict[str, object]
    tools: dict[str, object]
    note: str
    address: str
    machine: str


class PrActionBody(TypedDict, total=False):
    token: str
    owner: str
    repo: str
    pr_num: int
    body: str
    path: str
    line: int
    commit_id: str
    merge_method: str


class TelegramWebhookBody(TypedDict, total=False):
    update_id: int
    message: TelegramMessageDict
    callback_query: TelegramCallbackQuery
    edited_message: TelegramMessageDict


class NodeConfigDict(TypedDict, total=False):
    admin_chat_id: int
    tunnel: str
    host: str
    port: int
    webhook_secret: str
    connectors: dict[str, object]


class _MentionSnapshot(NamedTuple):
    target: str | None
    mention_count: int
    ts: float


class _StateSnapshot(NamedTuple):
    active: str | None
    startup_notified: bool


PostRouteHandler = Callable[..., None]  # (handler, body, match) -> None
GetRouteHandler = Callable[..., None]  # (handler, parsed, match) -> None


class PostRouteResolution(NamedTuple):
    handler: PostRouteHandler | None
    match: re.Match[str] | None


class GetRouteResolution(NamedTuple):
    handler: GetRouteHandler | None
    match: re.Match[str] | None


class GmailHeader(TypedDict, total=False):
    name: str
    value: str


class GmailBody(TypedDict, total=False):
    data: str
    size: int
    attachmentId: str


class GmailPayloadPart(TypedDict, total=False):
    mimeType: str
    filename: str
    body: GmailBody
    headers: list[GmailHeader]
    parts: list[GmailPayloadPart]


class GmailMessage(TypedDict, total=False):
    id: str
    threadId: str
    labelIds: list[str]
    payload: GmailPayloadPart
    historyId: str


class GmailMessageRef(TypedDict, total=False):
    message: GmailMessage


class GmailHistoryEntry(TypedDict, total=False):
    messagesAdded: list[GmailMessageRef]


class GmailHistoryResponse(TypedDict, total=False):
    history: list[GmailHistoryEntry]
    historyId: str


class GmailProfile(TypedDict, total=False):
    emailAddress: str
    historyId: str


class GmailAttachmentData(TypedDict, total=False):
    data: str


class GmailMessageListResponse(TypedDict, total=False):
    messages: list[GmailMessage]


class GmailAttachmentInfo(TypedDict):
    filename: str
    mimeType: str
    size: int
    attachmentId: str


class GithubUser(TypedDict, total=False):
    login: str


class GithubComment(TypedDict, total=False):
    id: int
    body: str
    user: GithubUser
    html_url: str
    issue_url: str
    pull_request_url: str
    issue_num: str
    kind: str


class GithubState(TypedDict):
    last_poll_time: Optional[str]
    seen_ids: list[int]


class Attachment(TypedDict, total=False):
    path: str
    filename: str
    mimeType: str


class ConnectorMetadata(TypedDict, total=False):
    number: Optional[str]
    repo: str
    comment_id: int


class IssueContext(TypedDict):
    number: str
    kind: str
    url: str


class ConnectorStatus(TypedDict):
    name: str
    running: bool
    sender_filter: str
    poll_interval: int
    consecutive_failures: int
    alert_sent: bool


class GmailStatus(TypedDict):
    name: str
    running: bool
    sender_filter: str
    poll_interval: int
    consecutive_failures: int
    alert_sent: bool
    history_id: Optional[str]


class GithubStatus(TypedDict):
    name: str
    running: bool
    sender_filter: str
    poll_interval: int
    consecutive_failures: int
    alert_sent: bool
    repos: list[str]
    last_poll_time: Optional[str]
    seen_ids_count: int


class TranscriptSyncState(TypedDict, total=False):
    status: str
    progress: str
    error: str | None
    path: str | None
    started: float
    pct: int
    remote_size: int


class TranscriptMessageUsage(TypedDict, total=False):
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int


class TranscriptMessageContent(TypedDict, total=False):
    type: str
    text: str
    name: str
    id: str
    input: dict[str, str]
    content: str | list[object]
    is_error: bool


class TranscriptMessage(TypedDict, total=False):
    role: str
    content: str | list[TranscriptMessageContent]
    model: str
    usage: TranscriptMessageUsage


class TranscriptEntry(TypedDict, total=False):
    type: str
    message: TranscriptMessage
    timestamp: str
    version: str
    gitBranch: str
    _idx: int


class TranscriptStatsDict(TypedDict):
    n_user: int
    n_tool: int
    n_edit: int
    lines_add: int
    lines_del: int
    lines_mod: int
    n_files: int
    model: str
    version: str
    git_branch: str
    first_ts: str
    last_ts: str
    input_tokens: int
    output_tokens: int
    duration: str


class ToolResultDict(TypedDict, total=False):
    content: str
    is_error: bool


class _GuestSessionDictRequired(TypedDict):
    name: str
    created_at: str
    expires_at_unix: float
    notified_workers: set[str] | list[str]


class ChannelMemberDict(TypedDict, total=False):
    type: str
    name: str


ChannelMessageDict = TypedDict(
    "ChannelMessageDict",
    {
        "id": str,
        "seq": int,
        "from": str,
        "text": str,
        "ts": int,
    },
)


class ChannelDict(TypedDict):
    id: str
    label: str
    created_at: str
    expires_at_unix: float
    seq: int
    created_by: str
    members: dict[str, ChannelMemberDict]
    messages: list[ChannelMessageDict]


_RelayMessageDictRequired = TypedDict(
    "_RelayMessageDictRequired",
    {
        "message_id": str,
        "direction": str,
        "from": str,
        "to": str,
        "text": str,
        "ts": str,
    },
)


class RelayMessageDict(_RelayMessageDictRequired, total=False):
    sender_name: str


class RelayChannelDict(TypedDict):
    id: str
    label: str
    worker: str
    workers: list[str]
    created_at: str
    expires_at: str
    expires_at_unix: float
    guest_token_hash: str
    reply_token_hash: str
    reply_token: str
    messages: list[RelayMessageDict]
