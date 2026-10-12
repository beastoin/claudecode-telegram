"""Shim: all content merged into bridge.py."""

from bridge import *  # noqa: F401,F403
from bridge import (  # noqa: F401 — underscore names excluded by * import
    _parse_transcript_entries,
    _render_transcript_html,
    _resolve_transcript_path,
    _run_transcript_query,
    _start_transcript_sync,
    _TEMPLATE_DIR,
    _transcript_sync,
    _TRANSCRIPT_CSS,
    _TRANSCRIPT_JS,
)
