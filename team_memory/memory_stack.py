#!/usr/bin/env python3
"""
memory_stack.py — 4-Layer Memory Stack for Team Memory
=======================================================

Inspired by MemPalace's layered architecture. Load only what you need.

    Layer 0: Identity       (~100 tokens)   — Always loaded. Agent roster, aliases, projects.
    Layer 1: Essential Story (~500-800)      — Always loaded. Top recent items per wing.
    Layer 2: On-Demand      (~200-500 each)  — Loaded when a wing/room comes up.
    Layer 3: Deep Search    (unlimited)      — Full semantic search via ChromaDB.

Wake-up cost: ~600-900 tokens (L0+L1). Leaves 95%+ of context free.
"""

import os
import json
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import chromadb

try:
    from .config import DB_PATH, COLLECTION, SUMMARY_COLLECTION
except ImportError:
    DB_PATH = os.path.expanduser("~/team/rnd/team-memory/db")
    COLLECTION = "team_memory_telegram"
    SUMMARY_COLLECTION = "team_memory_summaries"

MESSAGES_COLLECTION = "team_memory_messages"


# ---------------------------------------------------------------------------
# Layer 0 — Identity (agent roster, aliases, projects)
# ---------------------------------------------------------------------------

# Hardcoded roster — stable enough to live in code. Update when team changes.
AGENT_ROSTER = {
    "manager": {"aliases": ["thinh", "beasts_manager"], "role": "Manager"},
    "chen": {"aliases": [], "role": "Worker"},
    "finn": {"aliases": [], "role": "Worker"},
    "geni": {"aliases": [], "role": "Researcher"},
    "hiro": {"aliases": [], "role": "Worker"},
    "jin": {"aliases": [], "role": "Worker"},
    "kai": {"aliases": [], "role": "Worker"},
    "kelvin": {"aliases": [], "role": "Worker"},
    "kenji": {"aliases": [], "role": "Worker"},
    "lee": {"aliases": [], "role": "Worker"},
    "luck": {"aliases": [], "role": "Coordinator"},
    "mon": {"aliases": [], "role": "Worker"},
    "noa": {"aliases": [], "role": "Worker"},
    "ren": {"aliases": [], "role": "Worker"},
    "ryo": {"aliases": [], "role": "Worker"},
    "sora": {"aliases": [], "role": "Worker"},
    "taro": {"aliases": [], "role": "Worker"},
    "x": {"aliases": [], "role": "Worker"},
    "yuki": {"aliases": [], "role": "Worker"},
}

PROJECTS = {
    "omi": "AI wearable — Flutter app + Python backend + Next.js web",
    "claudecode-telegram": "Telegram bridge for Claude Code agents",
    "autoloop": "agent-flutter + flow-walker automated testing",
}

WINGS = ["omi", "claudecode-telegram", "infrastructure", "operations", "general"]


class Layer0:
    """~100 tokens. Always loaded. Team identity context."""

    def render(self) -> str:
        lines = ["## L0 — TEAM IDENTITY"]
        lines.append(f"Agents: {', '.join(sorted(AGENT_ROSTER.keys()))} ({len(AGENT_ROSTER)} total)")
        lines.append(f"Projects: {', '.join(PROJECTS.keys())}")
        lines.append(f"Wings: {', '.join(WINGS)}")
        # Sender aliases (beasts → agent name resolution)
        lines.append('Sender "beasts" = bot account; real sender is prefix before ":"')
        lines.append('Sender "thinh" = manager')
        return "\n".join(lines)

    def token_estimate(self) -> int:
        return len(self.render()) // 4


# ---------------------------------------------------------------------------
# Layer 1 — Essential Story (auto-generated from recent top items per wing)
# ---------------------------------------------------------------------------

class Layer1:
    """~500-800 tokens. Always loaded. Recent high-signal items per wing."""

    MAX_ITEMS = 15
    MAX_CHARS = 3200
    LOOKBACK_DAYS = 7  # only include items from last 7 days

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH

    def generate(self, wing: str = None) -> str:
        """Pull recent chunks from ChromaDB, group by wing, format compactly."""
        try:
            client = chromadb.PersistentClient(path=self.db_path)
            col = client.get_collection(COLLECTION)
        except Exception:
            return "## L1 — No team memory found."

        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M")

        # Fetch recent chunks in batches
        _BATCH = 500
        docs, metas, ids = [], [], []
        offset = 0
        while True:
            kwargs = {"include": ["documents", "metadatas"], "limit": _BATCH, "offset": offset}
            if wing:
                kwargs["where"] = {"wing": wing}
            try:
                batch = col.get(**kwargs)
            except Exception:
                break
            batch_docs = batch.get("documents", [])
            batch_metas = batch.get("metadatas", [])
            batch_ids = batch.get("ids", [])
            if not batch_docs:
                break
            docs.extend(batch_docs)
            metas.extend(batch_metas)
            ids.extend(batch_ids)
            offset += len(batch_docs)
            if len(batch_docs) < _BATCH:
                break

        if not docs:
            return "## L1 — No memories yet."

        # Filter to recent + score by recency and message count
        scored = []
        for doc, meta in zip(docs, metas):
            ts = meta.get("timestamp_start", "")
            if ts < cutoff:
                continue
            msg_count = meta.get("msg_count", 1)
            # Score: more messages = more important conversation
            score = msg_count
            scored.append((score, meta, doc))

        if not scored:
            return "## L1 — No recent activity."

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:self.MAX_ITEMS]

        # Group by wing
        by_wing = defaultdict(list)
        for score, meta, doc in top:
            w = meta.get("wing", "general")
            by_wing[w].append((score, meta, doc))

        lines = ["## L1 — ESSENTIAL STORY (last 7 days)"]
        total_len = 0

        for w in WINGS:
            entries = by_wing.get(w, [])
            if not entries:
                continue

            wing_line = f"\n[{w}]"
            lines.append(wing_line)
            total_len += len(wing_line)

            for score, meta, doc in entries:
                room = meta.get("room", "")
                sender = meta.get("from", "?")
                ts = meta.get("timestamp_start", "")[:10]

                snippet = doc.strip().replace("\n", " ")
                if len(snippet) > 200:
                    snippet = snippet[:197] + "..."

                room_tag = f"/{room}" if room else ""
                entry_line = f"  - [{ts}] {sender}{room_tag}: {snippet}"

                if total_len + len(entry_line) > self.MAX_CHARS:
                    lines.append("  ... (more via L3 search)")
                    return "\n".join(lines)

                lines.append(entry_line)
                total_len += len(entry_line)

        # Add any unclassified wings
        for w, entries in by_wing.items():
            if w not in WINGS:
                wing_line = f"\n[{w}]"
                lines.append(wing_line)
                for score, meta, doc in entries:
                    snippet = doc.strip().replace("\n", " ")[:150]
                    lines.append(f"  - {snippet}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Layer 2 — On-Demand (wing/room filtered retrieval)
# ---------------------------------------------------------------------------

class Layer2:
    """~200-500 tokens per retrieval. Loaded when a specific wing/room comes up."""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH

    def retrieve(self, wing: str = None, room: str = None, n_results: int = 10) -> str:
        """Retrieve chunks filtered by wing and/or room."""
        try:
            client = chromadb.PersistentClient(path=self.db_path)
            col = client.get_collection(COLLECTION)
        except Exception:
            return "No team memory found."

        conditions = []
        if wing:
            conditions.append({"wing": wing})
        if room:
            conditions.append({"room": room})

        where = None
        if len(conditions) == 1:
            where = conditions[0]
        elif len(conditions) > 1:
            where = {"$and": conditions}

        kwargs = {"include": ["documents", "metadatas"], "limit": n_results}
        if where:
            kwargs["where"] = where

        try:
            results = col.get(**kwargs)
        except Exception as e:
            return f"Retrieval error: {e}"

        docs = results.get("documents", [])
        metas = results.get("metadatas", [])

        if not docs:
            label = f"wing={wing}" if wing else ""
            if room:
                label += f" room={room}" if label else f"room={room}"
            return f"No chunks found for {label}."

        lines = [f"## L2 — ON-DEMAND ({len(docs)} chunks)"]
        for doc, meta in zip(docs[:n_results], metas[:n_results]):
            room_name = meta.get("room", "?")
            sender = meta.get("from", "?")
            ts = meta.get("timestamp_start", "")[:10]
            snippet = doc.strip().replace("\n", " ")
            if len(snippet) > 300:
                snippet = snippet[:297] + "..."
            lines.append(f"  [{ts}] {sender} ({room_name}): {snippet}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Layer 3 — Deep Search (full semantic search)
# ---------------------------------------------------------------------------

class Layer3:
    """Unlimited depth. Semantic search against all team memory."""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH

    def search(self, query: str, wing: str = None, room: str = None, n_results: int = 5) -> str:
        """Semantic search with optional wing/room filtering."""
        try:
            client = chromadb.PersistentClient(path=self.db_path)
            col = client.get_collection(COLLECTION)
        except Exception:
            return "No team memory found."

        conditions = []
        if wing:
            conditions.append({"wing": wing})
        if room:
            conditions.append({"room": room})

        where = None
        if len(conditions) == 1:
            where = conditions[0]
        elif len(conditions) > 1:
            where = {"$and": conditions}

        kwargs = {
            "query_texts": [query],
            "n_results": n_results,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where

        try:
            results = col.query(**kwargs)
        except Exception as e:
            return f"Search error: {e}"

        docs = results["documents"][0]
        metas = results["metadatas"][0]
        dists = results["distances"][0]

        if not docs:
            return "No results found."

        lines = [f'## L3 — SEARCH RESULTS for "{query}"']
        if wing:
            lines[0] += f" (wing={wing})"
        if room:
            lines[0] += f" (room={room})"

        for i, (doc, meta, dist) in enumerate(zip(docs, metas, dists), 1):
            similarity = round(1 - dist, 3)
            w = meta.get("wing", "?")
            r = meta.get("room", "?")
            sender = meta.get("from", "?")
            ts = meta.get("timestamp_start", "")[:10]

            snippet = doc.strip().replace("\n", " ")
            if len(snippet) > 300:
                snippet = snippet[:297] + "..."

            lines.append(f"  [{i}] {w}/{r} sim={similarity} [{ts}] {sender}")
            lines.append(f"      {snippet}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# MemoryStack — unified interface
# ---------------------------------------------------------------------------

class MemoryStack:
    """
    The full 4-layer stack for team memory.

        stack = MemoryStack()
        print(stack.wake_up())                   # L0 + L1 (~600-900 tokens)
        print(stack.recall(wing="omi"))           # L2 on-demand
        print(stack.search("pricing change"))     # L3 deep search
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH
        self.l0 = Layer0()
        self.l1 = Layer1(self.db_path)
        self.l2 = Layer2(self.db_path)
        self.l3 = Layer3(self.db_path)

    def wake_up(self, wing: str = None) -> str:
        """Generate wake-up text: L0 (identity) + L1 (essential story).

        Typically ~600-900 tokens. Inject into system prompt or first message.
        """
        parts = [self.l0.render(), "", self.l1.generate(wing=wing)]
        return "\n".join(parts)

    def recall(self, wing: str = None, room: str = None, n_results: int = 10) -> str:
        """On-demand L2 retrieval filtered by wing/room."""
        return self.l2.retrieve(wing=wing, room=room, n_results=n_results)

    def search(self, query: str, wing: str = None, room: str = None, n_results: int = 5) -> str:
        """Deep L3 semantic search."""
        return self.l3.search(query, wing=wing, room=room, n_results=n_results)

    def status(self) -> dict:
        """Status of all layers."""
        result = {
            "db_path": self.db_path,
            "L0_identity": {
                "agents": len(AGENT_ROSTER),
                "projects": len(PROJECTS),
                "wings": len(WINGS),
                "tokens": self.l0.token_estimate(),
            },
            "L1_essential": "Auto-generated from top recent chunks per wing",
            "L2_on_demand": "Wing/room filtered retrieval",
            "L3_deep_search": "Full semantic search via ChromaDB",
        }

        try:
            client = chromadb.PersistentClient(path=self.db_path)
            col = client.get_collection(COLLECTION)
            result["total_chunks"] = col.count()

            # Wing distribution
            wings = {}
            _BATCH = 500
            offset = 0
            while True:
                batch = col.get(include=["metadatas"], limit=_BATCH, offset=offset)
                if not batch["metadatas"]:
                    break
                for meta in batch["metadatas"]:
                    w = meta.get("wing", "general")
                    wings[w] = wings.get(w, 0) + 1
                offset += len(batch["metadatas"])
                if len(batch["metadatas"]) < _BATCH:
                    break
            result["wing_distribution"] = wings

            # Raw messages
            try:
                msg_col = client.get_collection(MESSAGES_COLLECTION)
                result["total_messages"] = msg_col.count()
            except Exception:
                result["total_messages"] = 0

            # Summaries
            try:
                sum_col = client.get_collection(SUMMARY_COLLECTION)
                result["total_summaries"] = sum_col.count()
            except Exception:
                result["total_summaries"] = 0

        except Exception:
            result["total_chunks"] = 0

        return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    def usage():
        print("memory_stack.py — 4-Layer Team Memory Stack")
        print()
        print("Usage:")
        print("  python memory_stack.py wake-up              L0 + L1 wake-up text")
        print("  python memory_stack.py wake-up --wing=omi   Wing-filtered wake-up")
        print("  python memory_stack.py recall --wing=omi    L2 on-demand retrieval")
        print("  python memory_stack.py search <query>       L3 deep search")
        print("  python memory_stack.py status               Layer status")
        sys.exit(0)

    if len(sys.argv) < 2:
        usage()

    cmd = sys.argv[1]
    flags = {}
    positional = []
    for arg in sys.argv[2:]:
        if arg.startswith("--") and "=" in arg:
            key, val = arg.split("=", 1)
            flags[key.lstrip("-")] = val
        elif not arg.startswith("--"):
            positional.append(arg)

    stack = MemoryStack()

    if cmd in ("wake-up", "wakeup"):
        text = stack.wake_up(wing=flags.get("wing"))
        tokens = len(text) // 4
        print(f"Wake-up text (~{tokens} tokens):")
        print("=" * 50)
        print(text)

    elif cmd == "recall":
        print(stack.recall(wing=flags.get("wing"), room=flags.get("room")))

    elif cmd == "search":
        query = " ".join(positional) if positional else ""
        if not query:
            print("Usage: python memory_stack.py search <query>")
            sys.exit(1)
        print(stack.search(query, wing=flags.get("wing"), room=flags.get("room")))

    elif cmd == "status":
        print(json.dumps(stack.status(), indent=2))

    else:
        usage()
