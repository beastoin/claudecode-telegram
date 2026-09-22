"""Team Memory configuration."""

import os
from pathlib import Path

# ChromaDB database path — configurable via env var
DB_PATH = os.environ.get(
    "TEAM_MEMORY_DB",
    str(Path.home() / "team" / "rnd" / "team-memory" / "db"),
)

COLLECTION = "team_memory_telegram"
SUMMARY_COLLECTION = "team_memory_summaries"

# Haiku reranker
HAIKU_MODEL = "claude-haiku-4-5-20251001"
HAIKU_MAX_CANDIDATES = 15
CANDIDATE_TRUNCATE = 500  # chars per candidate in Haiku prompt

# Scoring weights (must sum to 1.0)
SEMANTIC_WEIGHT = 0.7
BM25_WEIGHT = 0.2
TEMPORAL_WEIGHT = 0.1
TEMPORAL_DECAY = 0.05  # higher = faster decay for old results
