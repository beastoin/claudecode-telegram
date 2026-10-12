"""Shim: all content merged into shared.py."""

from shared import *  # noqa: F401,F403
from shared import (  # noqa: F401 — underscore names excluded by * import
    _GuestSessionDictRequired,
    _MentionSnapshot,
    _RelayMessageDictRequired,
    _StateSnapshot,
)
