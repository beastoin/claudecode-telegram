"""Team Memory — searchable Telegram chat history via ChromaDB."""

from .search import search
from .config import DB_PATH, COLLECTION

__all__ = ["search", "DB_PATH", "COLLECTION"]
