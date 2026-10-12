"""Shim: all content merged into bridge.py."""

from bridge import *  # noqa: F401,F403
from bridge import (  # noqa: F401 — underscore names excluded by * import
    _check_incremental,
    _extract_plain_text,
    _indexer_main,
    _paginate,
    _run_transcript,
    _sanitize_fts5_query,
    _setup_db,
    _SKIP_TYPES,
    _transcript_create_tables,
    _transcript_index,
    _transcript_query_entries,
    _transcript_query_search,
    _transcript_query_stats,
    _update_meta,
)

if __name__ == "__main__":
    _indexer_main()
