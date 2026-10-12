"""Shim: all content merged into bridge.py."""

from bridge import *  # noqa: F401,F403
from bridge import (  # noqa: F401 — underscore names excluded by * import
    _atomic_write,
    _atomic_write_bytes,
    _atomic_write_text,
    _escape_html,
    _get_header,
    _int_val,
    _pick_strs,
    _safe_json_loads_list,
    _sanitize_filename,
    _str_list_val,
    _str_val,
    _truncate,
    _validate_github_comment,
    _validate_github_state,
    _validate_gmail_attachment_data,
    _validate_gmail_body,
    _validate_gmail_header,
    _validate_gmail_history_response,
    _validate_gmail_message,
    _validate_gmail_message_list_response,
    _validate_gmail_profile,
    _validate_payload_part,
)
