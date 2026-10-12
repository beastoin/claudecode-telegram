"""Shim: all content merged into telegram.py."""

from telegram import (  # noqa: F401
    TelegramAPI,
    TelegramTransport,
    LocalTransport,
    _init_transport,
    telegram_api,
    format_file_size,
    transcribe_voice,
    _prepare_photo_for_telegram,
    _validate_file_path,
    validate_photo_path,
    is_blocked_filename,
    validate_document_path,
    _split_protected_segments,
    _collapse_excess_newlines,
    split_message,
    format_multipart_messages,
    update_bot_commands,
)
