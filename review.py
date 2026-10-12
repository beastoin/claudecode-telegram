"""Shim: all content merged into bridge.py."""

from bridge import *  # noqa: F401,F403
from bridge import (  # noqa: F401 — underscore names excluded by * import
    _fetch_commit_files_single,
    _fetch_commit_list,
    _fetch_file_context,
    _fetch_full_diff,
    _format_comment_with_context,
    _notify_telegram,
    _parse_full_diff,
    _pr_review_main,
    _REVIEW_CSS,
    _REVIEW_JS,
    _REVIEW_SCHEMA,
    _route_mentions_to_workers,
    _TEMPLATE_DIR,
)

if __name__ == "__main__":
    _pr_review_main()
