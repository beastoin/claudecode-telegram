"""Shim: all content merged into telegram.py."""

from telegram import (  # noqa: F401
    escape_html,
    _TelegramHTMLSanitizer,
    _sanitize_telegram_html,
    _render_md_inline_plain,
    _render_md_inline_html,
    _render_table_as_pre,
    _wrap_plain_tables,
    markdown_to_telegram_html,
    _pipe_tables_to_html,
)
