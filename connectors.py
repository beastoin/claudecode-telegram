from __future__ import annotations
import abc
import base64
import binascii
import json
import os
import re
import subprocess
import tempfile
import threading
from datetime import datetime, timezone, timedelta
from typing import Generic, Optional, Protocol, TypedDict, TypeVar, cast
from _types import (
    GmailHeader,
    GmailBody,
    GmailPayloadPart,
    GmailMessage,
    GmailMessageRef,
    GmailHistoryEntry,
    GmailHistoryResponse,
    GmailProfile,
    GmailAttachmentData,
    GmailMessageListResponse,
    GmailAttachmentInfo,
    GithubUser,
    GithubComment,
    GithubState,
    Attachment,
    ConnectorMetadata,
    IssueContext,
    ConnectorStatus,
    GmailStatus,
    GithubStatus,
)


class MessageCallback(Protocol):
    def __call__(
        self,
        targets: list[str],
        html_text: str,
        plain_text: str,
        attachments: list[Attachment],
        metadata: Optional[ConnectorMetadata] = None,
    ) -> None: ...
class AlertCallback(Protocol):
    def __call__(self, text: str) -> None: ...
class WorkerListCallback(Protocol):
    def __call__(self) -> set[str]: ...


_REPO_SEGMENT = r"[a-zA-Z0-9](?:[a-zA-Z0-9_-]|\.(?!\.))*[a-zA-Z0-9]"
_REPO_PATTERN = re.compile(rf"^(?:{_REPO_SEGMENT}|[a-zA-Z0-9])/(?:{_REPO_SEGMENT}|[a-zA-Z0-9])$")
_MAX_FILENAME_LEN = 200  # Limit attachment filename length
_B64_URLSAFE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
_MAX_MIME_DEPTH = 10  # Max recursion depth for MIME part traversal
_MAX_MIME_PARTS = 100  # Max total MIME parts to process
CONSECUTIVE_FAIL_WARN = 3
CONSECUTIVE_FAIL_REBOOTSTRAP = 5
_MAX_POLL_INTERVAL = 3600
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024  # 25 MB
_MAX_SEEN_IDS = 500
_PRUNE_KEEP = 200
M = TypeVar("M")


def __getattr__(name: str) -> object:
    if name in ("BaseConnector", "GmailConnector", "GitHubConnector"):
        import _connector_impl as _ci

        return getattr(_ci, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
