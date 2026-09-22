#!/usr/bin/env python3
"""Team Memory Ingester — Telegram parsed JSONL → ChromaDB.

Chunks messages by 5-min time windows, embeds with all-MiniLM-L6-v2,
stores in local ChromaDB for semantic search.

Usage:
    python3 ingest.py <parsed-jsonl>                # full re-ingest
    python3 ingest.py <parsed-jsonl> --incremental   # append new messages only
    python3 ingest.py <parsed-jsonl> --full-reindex   # force full re-ingest
"""

import json
import sys
import os
from datetime import datetime
from pathlib import Path

import chromadb

# Import entity index (optional — graceful if not available)
try:
    from entities import EntityIndex
except ImportError:
    try:
        from team_memory.entities import EntityIndex
    except ImportError:
        EntityIndex = None

# --- Config ---
DB_PATH = os.path.expanduser("~/team/rnd/team-memory/db")

# --- Wing/Room classification ---
# Priority order: first match wins for wing. Room is more specific.
WING_RULES = [
    ("omi", [
        "basedhardware/omi", "omi/", "omi-pr", "omi pr", "omi mobile", "omi backend",
        "flutter", "mobile app", "app store", "play store", "google play",
        "codemagic", "deepgram", "/v1/", "plugins", "wearable", "omi_",
        "android", "ios app", "apk", "ipa", "app release",
    ]),
    ("claudecode-telegram", [
        "bridge", "claudecode-telegram", "claudecode_telegram",
        "worker", "tmux", "telegram", "/pilot", "/hire", "/end", "/restart",
        "clawdbot", "team-memory", "/memory", "pr-review",
        "agent-flutter", "agent-swift", "autoloop", "flow-walker",
    ]),
    ("infrastructure", [
        "oauth", "gcp", "gcloud", "google cloud", "ssh", "tailscale",
        "cron", "credential", "ssl", "nginx", "server", "docker",
        "emulator", "adb", "mac mini", "vps", "redis", "grafana",
        "cloudflare", "dns", "github token", "api key",
    ]),
    ("operations", [
        "daily report", "cto report", "weekly report", "monthly report",
        "mrr", "revenue", "subscriber", "billing", "email report",
        "daily ops", "daily task", "ops email", "triage",
        "onboarding", "hiring", "team update",
    ]),
]

ROOM_RULES = {
    "omi": [
        ("prs", ["pr #", "pull/", "pr workflow", "omi-pr", "merged", "pull request"]),
        ("releases", ["release", "app store", "play store", "codemagic", "version",
                       "apk", "ipa", "build"]),
        ("costs", ["cost", "billing", "gemini", "openai", "api cost", "pricing",
                    "budget", "spend"]),
        ("mobile", ["flutter", "mobile", "android", "ios", "app", "ui", "screen"]),
        ("backend", ["backend", "/v1/", "endpoint", "api", "server", "deploy",
                      "database", "postgres", "supabase"]),
    ],
    "claudecode-telegram": [
        ("bridge", ["bridge", "telegram", "routing", "command", "clawdbot"]),
        ("workers", ["worker", "tmux", "session", "/pilot", "/hire", "agent"]),
        ("team-memory", ["team-memory", "/memory", "chromadb", "search", "ingest",
                          "chunk", "embedding"]),
        ("tools", ["pr-review", "agent-flutter", "agent-swift", "autoloop",
                    "flow-walker", "codex"]),
    ],
    "infrastructure": [
        ("oauth", ["oauth", "token", "credential", "gws", "refresh token", "auth"]),
        ("servers", ["server", "vps", "mac mini", "ssh", "tailscale", "port"]),
        ("gcp", ["gcp", "gcloud", "google cloud", "cloud run", "cloud function"]),
        ("ci-cd", ["cron", "github action", "ci", "cd", "pipeline", "deploy"]),
    ],
    "operations": [
        ("daily-report", ["daily report", "daily ops", "daily task"]),
        ("weekly-report", ["weekly report", "cto report", "weekly summary"]),
        ("billing", ["mrr", "revenue", "subscriber", "billing", "payment"]),
    ],
}


def classify_wing_room(text: str) -> tuple[str, str]:
    """Classify a chunk into wing and room based on keyword rules.

    Returns (wing, room). Falls back to ("general", "") if no match.
    """
    text_lower = text.lower()

    # Classify wing (first match wins — priority order)
    wing = "general"
    for w, keywords in WING_RULES:
        if any(kw in text_lower for kw in keywords):
            wing = w
            break

    # Classify room within wing
    room = ""
    if wing in ROOM_RULES:
        for r, keywords in ROOM_RULES[wing]:
            if any(kw in text_lower for kw in keywords):
                room = r
                break

    return wing, room
COLLECTION_NAME = "team_memory_telegram"
STATE_FILE = os.path.expanduser("~/team/rnd/team-memory/state.json")
GAP_THRESHOLD = 300  # 5 min — absolute boundary, always splits
GAP_SENDER_CHANGE = 120  # 2 min — splits on sender change (separate topics)
MAX_CHUNK_CHARS = 2000
MIN_CHUNK_CHARS = 50
BATCH_SIZE = 200  # ChromaDB batch add size


def load_state() -> dict:
    """Load ingest state (last message ID, last chunk boundary)."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    """Save ingest state."""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_messages(jsonl_path: str) -> list[dict]:
    """Load parsed JSONL messages."""
    messages = []
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if line:
                messages.append(json.loads(line))
    messages.sort(key=lambda m: m["timestamp_unix"])
    return messages


def build_message_lookup(messages: list[dict]) -> dict[int, dict]:
    """Build id → message lookup for reply threading."""
    return {m["id"]: m for m in messages}


def chunk_by_time_window(messages: list[dict], msg_lookup: dict[int, dict] = None) -> list[dict]:
    """Group consecutive messages into conversation-exchange chunks.

    Rules:
    - gap >= 5 min (GAP_THRESHOLD): always split (different conversation)
    - gap >= 2 min (GAP_SENDER_CHANGE) AND sender changed: split (separate topics)
    - gap < 2 min: keep together regardless of sender (conversation exchange)

    This preserves question→answer pairs like:
      kai: "should we use approach A?" → manager: "yes" → one chunk
    """
    if not messages:
        return []

    if msg_lookup is None:
        msg_lookup = build_message_lookup(messages)

    chunks = []
    current_msgs = [messages[0]]

    for msg in messages[1:]:
        prev = current_msgs[-1]
        gap = msg["timestamp_unix"] - prev["timestamp_unix"]
        sender_changed = msg["from"] != prev["from"]

        if gap >= GAP_THRESHOLD:
            # Absolute boundary — always split
            chunk = build_chunk(current_msgs, msg_lookup)
            if chunk:
                chunks.append(chunk)
            current_msgs = [msg]
        elif gap >= GAP_SENDER_CHANGE and sender_changed:
            # Long pause + sender change — separate topic
            chunk = build_chunk(current_msgs, msg_lookup)
            if chunk:
                chunks.append(chunk)
            current_msgs = [msg]
        else:
            current_msgs.append(msg)

    # Last chunk
    chunk = build_chunk(current_msgs, msg_lookup)
    if chunk:
        chunks.append(chunk)

    # Link neighbors: store prev/next chunk IDs in metadata
    for i, chunk in enumerate(chunks):
        chunk["metadata"]["prev_chunk_id"] = chunks[i - 1]["id"] if i > 0 else ""
        chunk["metadata"]["next_chunk_id"] = chunks[i + 1]["id"] if i < len(chunks) - 1 else ""

    return chunks


AGENT_NAMES = {"chen", "finn", "geni", "hiro", "jin", "kai", "kelvin", "kenji",
               "lee", "luck", "mon", "noa", "ren", "ryo", "sora", "taro", "x", "yuki"}


def _clean_sender(msg):
    """Resolve display sender and clean text.

    - "Thinh" → "manager"
    - "beasts" with "agent:" prefix → agent name, strip prefix from text
    - "beasts" without prefix → "beasts"

    Returns (display_sender, cleaned_text).
    """
    sender = msg["from"]
    text = msg["text"].strip()

    if sender.lower() == "thinh":
        return "manager", text

    if sender.lower() == "beasts":
        # Check for "agent:\n" or "agent: " prefix
        first_line = text.split("\n")[0]
        if ":" in first_line[:30]:
            prefix = first_line.split(":")[0].strip().lower()
            if prefix in AGENT_NAMES:
                # Strip the "agent:\n" prefix from text
                text = text[len(first_line) + 1:].strip() if "\n" in text else text[len(first_line.split(":")[0]) + 1:].strip()
                return prefix, text

    return sender.lower(), text


def build_chunk(messages: list[dict], msg_lookup: dict[int, dict] = None) -> dict | None:
    """Build a chunk from a group of messages. Includes reply context."""
    lines = []
    seen_reply_ids = set()  # avoid duplicating reply context within same chunk

    for msg in messages:
        ts = msg["timestamp"][:16]
        sender, text = _clean_sender(msg)

        # Prepend replied-to message text for context
        reply_to = msg.get("reply_to")
        if reply_to and msg_lookup and reply_to not in seen_reply_ids:
            original = msg_lookup.get(reply_to)
            if original:
                orig_sender, orig_text = _clean_sender(original)
                orig_text = orig_text[:200]
                lines.append(f"[replying to {orig_sender}: \"{orig_text}\"]")
                seen_reply_ids.add(reply_to)

        lines.append(f"[{ts}] {sender}: {text}")

    full_text = "\n".join(lines)

    if len(full_text) > MAX_CHUNK_CHARS:
        full_text = full_text[:MAX_CHUNK_CHARS] + "\n...truncated"

    if len(full_text) < MIN_CHUNK_CHARS:
        return None

    all_agents = set()
    for msg in messages:
        for agent in msg.get("target_agents", []):
            if agent:
                all_agents.add(agent)

    from_counts = {}
    for msg in messages:
        clean_name, _ = _clean_sender(msg)
        from_counts[clean_name] = from_counts.get(clean_name, 0) + 1
    primary_from = max(from_counts, key=from_counts.get)

    first_msg = messages[0]
    last_msg = messages[-1]

    # Classify wing and room
    wing, room = classify_wing_room(full_text)

    return {
        "id": f"tg_{first_msg['id']}_{last_msg['id']}",
        "text": full_text,
        "metadata": {
            "from": primary_from.lower(),
            "target_agents": ",".join(sorted(all_agents)) if all_agents else "",
            "timestamp_start": first_msg["timestamp"],
            "timestamp_end": last_msg["timestamp"],
            "msg_count": len(messages),
            "has_command": any(m.get("has_command", False) for m in messages),
            "wing": wing,
            "room": room,
        },
        "_raw_msgs": messages,  # kept for state tracking, stripped before ChromaDB
    }


SUMMARY_COLLECTION = "team_memory_summaries"
MESSAGES_COLLECTION = "team_memory_messages"


def generate_daily_summaries(chunks: list[dict]) -> list[dict]:
    """Generate daily summary documents from chunks. No LLM — pure aggregation.

    Each summary captures: date, active agents, PRs mentioned, decisions made,
    deploys, key topics (wings/rooms), and top chunks by length.
    """
    from collections import defaultdict
    import re

    # Group chunks by date
    by_date = defaultdict(list)
    for c in chunks:
        date = c["metadata"]["timestamp_start"][:10]
        by_date[date].append(c)

    summaries = []
    for date in sorted(by_date.keys()):
        day_chunks = by_date[date]

        # Aggregate stats
        agents = set()
        wings = set()
        rooms = set()
        pr_nums = set()
        total_msgs = 0

        for c in day_chunks:
            agents.add(c["metadata"]["from"])
            for a in c["metadata"].get("target_agents", "").split(","):
                if a:
                    agents.add(a)
            w = c["metadata"].get("wing", "")
            r = c["metadata"].get("room", "")
            if w:
                wings.add(w)
            if r:
                rooms.add(f"{w}/{r}")
            total_msgs += c["metadata"]["msg_count"]

            # Extract PR numbers
            for m in re.finditer(r'(?:PR|#|pull/)(\d{4,5})', c["text"]):
                pr_nums.add(m.group(1))

        # Build summary text
        lines = [f"Daily Summary: {date}"]
        lines.append(f"Messages: {total_msgs}, Chunks: {len(day_chunks)}")
        lines.append(f"Active agents: {', '.join(sorted(agents))}")
        if wings:
            lines.append(f"Topics: {', '.join(sorted(wings))}")
        if rooms:
            lines.append(f"Areas: {', '.join(sorted(rooms))}")
        if pr_nums:
            lines.append(f"PRs discussed: {', '.join('#'+n for n in sorted(pr_nums)[:20])}")
            if len(pr_nums) > 20:
                lines.append(f"  ...and {len(pr_nums)-20} more")

        # Include top 3 longest chunks as key conversations
        top_chunks = sorted(day_chunks, key=lambda c: len(c["text"]), reverse=True)[:3]
        lines.append("\nKey conversations:")
        for i, c in enumerate(top_chunks, 1):
            preview = c["text"][:200].replace("\n", " ")
            lines.append(f"  {i}. [{c['metadata']['from']}] {preview}...")

        summary_text = "\n".join(lines)

        summaries.append({
            "id": f"summary_{date}",
            "text": summary_text,
            "metadata": {
                "type": "daily_summary",
                "date": date,
                "msg_count": total_msgs,
                "chunk_count": len(day_chunks),
                "agents": ",".join(sorted(agents)),
                "wings": ",".join(sorted(wings)),
                "pr_count": len(pr_nums),
            },
        })

    return summaries


def ingest_summaries(summaries: list[dict], db_path: str):
    """Ingest daily summaries into a separate ChromaDB collection."""
    client = chromadb.PersistentClient(path=db_path)

    try:
        client.delete_collection(SUMMARY_COLLECTION)
    except Exception:
        pass

    collection = client.create_collection(
        name=SUMMARY_COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )

    if summaries:
        ids = [s["id"] for s in summaries]
        documents = [s["text"] for s in summaries]
        metadatas = [s["metadata"] for s in summaries]
        collection.add(ids=ids, documents=documents, metadatas=metadatas)

    return collection


def ingest_messages(messages: list[dict], db_path: str, msg_lookup: dict = None):
    """Index every individual message in its own ChromaDB collection.

    Each message gets: sender, timestamp, text, reply context.
    This enables direct recall queries like "who first mentioned X".
    """
    client = chromadb.PersistentClient(path=db_path)

    try:
        client.delete_collection(MESSAGES_COLLECTION)
    except Exception:
        pass

    collection = client.create_collection(
        name=MESSAGES_COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )

    if msg_lookup is None:
        msg_lookup = {m["id"]: m for m in messages}

    ids = []
    documents = []
    metadatas = []

    for msg in messages:
        sender, text = _clean_sender(msg)

        # Skip very short messages (< 10 chars) — "ok", "yes", emoji
        if len(text.strip()) < 10:
            continue

        # Add reply context
        reply_to = msg.get("reply_to")
        reply_text = ""
        if reply_to and reply_to in msg_lookup:
            orig = msg_lookup[reply_to]
            orig_sender, orig_text = _clean_sender(orig)
            reply_text = f"[replying to {orig_sender}: \"{orig_text[:150]}\"] "

        doc = f"{reply_text}{text}"

        # Classify wing/room per message (MemPalace-style metadata)
        wing, room = classify_wing_room(doc)

        ids.append(f"msg_{msg['id']}")
        documents.append(doc)
        metadatas.append({
            "from": sender,
            "timestamp": msg["timestamp"][:16],
            "timestamp_full": msg["timestamp"],
            "target_agents": ",".join(msg.get("target_agents", [])),
            "reply_to": str(reply_to) if reply_to else "",
            "msg_id": msg["id"],
            "wing": wing,
            "room": room,
        })

    # Batch add
    total = len(ids)
    for i in range(0, total, BATCH_SIZE):
        end = min(i + BATCH_SIZE, total)
        collection.add(
            ids=ids[i:end],
            documents=documents[i:end],
            metadatas=metadatas[i:end],
        )
        print(f"  Indexed {end}/{total} messages")

    return collection


def ingest_full(chunks: list[dict], db_path: str):
    """Full re-ingest: drop and recreate collection."""
    client = chromadb.PersistentClient(path=db_path)

    try:
        client.delete_collection(COLLECTION_NAME)
        print(f"  Deleted existing collection '{COLLECTION_NAME}'")
    except Exception:
        pass

    collection = client.create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    _batch_add(collection, chunks)
    return collection


def ingest_incremental(chunks: list[dict], boundary_chunk_id: str | None, db_path: str):
    """Incremental ingest: add new chunks, handle boundary re-chunking."""
    client = chromadb.PersistentClient(path=db_path)

    try:
        collection = client.get_collection(COLLECTION_NAME)
    except Exception:
        print("  No existing collection — falling back to full ingest")
        return ingest_full(chunks, db_path)

    # If we have a boundary chunk to replace, delete the old one
    if boundary_chunk_id:
        try:
            collection.delete(ids=[boundary_chunk_id])
            print(f"  Deleted boundary chunk {boundary_chunk_id} for re-chunking")
        except Exception:
            pass

    # Use upsert to handle any ID collisions gracefully
    _batch_upsert(collection, chunks)
    return collection


def _batch_add(collection, chunks: list[dict]):
    """Batch add chunks to collection."""
    total = len(chunks)
    for i in range(0, total, BATCH_SIZE):
        batch = chunks[i : i + BATCH_SIZE]
        ids = [c["id"] for c in batch]
        documents = [c["text"] for c in batch]
        metadatas = [{k: v for k, v in c["metadata"].items()} for c in batch]
        collection.add(ids=ids, documents=documents, metadatas=metadatas)
        print(f"  Added {min(i + BATCH_SIZE, total)}/{total} chunks")


def _batch_upsert(collection, chunks: list[dict]):
    """Batch upsert chunks to collection."""
    total = len(chunks)
    for i in range(0, total, BATCH_SIZE):
        batch = chunks[i : i + BATCH_SIZE]
        ids = [c["id"] for c in batch]
        documents = [c["text"] for c in batch]
        metadatas = [{k: v for k, v in c["metadata"].items()} for c in batch]
        collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
        print(f"  Upserted {min(i + BATCH_SIZE, total)}/{total} chunks")


def print_stats(chunks: list[dict]):
    """Print chunk statistics."""
    if not chunks:
        print("\n  Stats: no chunks")
        return

    agents = set()
    for c in chunks:
        for a in c["metadata"]["target_agents"].split(","):
            if a:
                agents.add(a)

    from_counts = {}
    for c in chunks:
        f = c["metadata"]["from"]
        from_counts[f] = from_counts.get(f, 0) + 1

    avg_len = sum(len(c["text"]) for c in chunks) / len(chunks)
    avg_msgs = sum(c["metadata"]["msg_count"] for c in chunks) / len(chunks)

    print(f"\n  Stats:")
    print(f"    Total chunks: {len(chunks)}")
    print(f"    Agents mentioned: {len(agents)} ({', '.join(sorted(agents))})")
    print(f"    From distribution: {from_counts}")
    print(f"    Avg chunk length: {avg_len:.0f} chars")
    print(f"    Avg messages/chunk: {avg_msgs:.1f}")
    print(f"    Date range: {chunks[0]['metadata']['timestamp_start']} → {chunks[-1]['metadata']['timestamp_end']}")

    # Wing/room distribution
    wing_counts = {}
    for c in chunks:
        w = c["metadata"].get("wing", "")
        wing_counts[w] = wing_counts.get(w, 0) + 1
    print(f"    Wings: {wing_counts}")


def test_search(collection):
    """Run a quick test search to verify."""
    test_queries = [
        "TTS voice synthesis",
        "OAuth token renewal",
        "PR review viewer",
    ]
    print(f"\n  Quick search test:")
    for q in test_queries:
        results = collection.query(query_texts=[q], n_results=2)
        if results["documents"][0]:
            top = results["documents"][0][0][:100]
            score = results["distances"][0][0] if results["distances"] else "?"
            print(f"    '{q}' → (dist={score:.3f}) {top}...")
        else:
            print(f"    '{q}' → no results")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 ingest.py <parsed-jsonl> [--incremental | --full-reindex]")
        sys.exit(1)

    jsonl_path = sys.argv[1]
    incremental = "--incremental" in sys.argv
    full_reindex = "--full-reindex" in sys.argv

    if not os.path.exists(jsonl_path):
        print(f"Error: {jsonl_path} not found")
        sys.exit(1)

    print(f"[1/9] Loading messages from {jsonl_path}...")
    all_messages = load_messages(jsonl_path)
    print(f"  Loaded {len(all_messages)} messages")

    # Build full message lookup for reply threading (needs ALL messages, not just new)
    msg_lookup = build_message_lookup(all_messages)
    print(f"  Built message lookup ({len(msg_lookup)} entries) for reply threading")

    state = load_state()
    boundary_chunk_id = None

    if incremental and not full_reindex and state.get("last_msg_id"):
        last_id = state["last_msg_id"]
        last_ts = state.get("last_msg_timestamp_unix", 0)
        last_chunk_id = state.get("last_chunk_id")

        # Filter to only new messages (id > last ingested)
        new_messages = [m for m in all_messages if m["id"] > last_id]

        if not new_messages:
            print(f"  No new messages since last ingest (last_msg_id={last_id})")
            sys.exit(0)

        print(f"  Incremental: {len(new_messages)} new messages since msg_id {last_id}")

        # Boundary handling: check if first new message is within 5 min of last ingested
        first_new_ts = new_messages[0]["timestamp_unix"]
        gap = first_new_ts - last_ts

        if gap < GAP_THRESHOLD and state.get("last_chunk_msgs"):
            # Need to re-chunk: merge last chunk's messages with new ones
            boundary_msgs_raw = state["last_chunk_msgs"]
            print(f"  Boundary merge: {len(boundary_msgs_raw)} old msgs + new msgs (gap={gap}s < {GAP_THRESHOLD}s)")
            messages = boundary_msgs_raw + new_messages
            boundary_chunk_id = last_chunk_id
        else:
            print(f"  Clean boundary: gap={gap}s >= {GAP_THRESHOLD}s, no re-chunking needed")
            messages = new_messages
    else:
        if incremental and not state.get("last_msg_id"):
            print("  No previous state — doing full ingest")
        messages = all_messages

    print(f"[2/9] Chunking {len(messages)} messages by {GAP_THRESHOLD}s time windows...")
    chunks = chunk_by_time_window(messages, msg_lookup)

    # Strip _raw_msgs before stats (but keep for state)
    for c in chunks:
        c.get("_raw_msgs")  # just access check
    print_stats(chunks)

    print(f"\n[3/9] Ingesting into ChromaDB at {DB_PATH}...")
    if incremental and not full_reindex and state.get("last_msg_id"):
        collection = ingest_incremental(chunks, boundary_chunk_id, DB_PATH)
        print(f"  Collection '{COLLECTION_NAME}' now has {collection.count()} documents")
    else:
        # Strip _raw_msgs before sending to ChromaDB
        collection = ingest_full(chunks, DB_PATH)
        print(f"  Collection '{COLLECTION_NAME}' ready ({collection.count()} documents)")

    # Save state for next incremental run
    if all_messages:
        last_msg = all_messages[-1]
        last_chunk = chunks[-1] if chunks else None
        new_state = {
            "last_msg_id": last_msg["id"],
            "last_msg_timestamp": last_msg["timestamp"],
            "last_msg_timestamp_unix": last_msg["timestamp_unix"],
            "last_ingest_time": datetime.now().isoformat(),
            "total_messages_ingested": len(all_messages),
            "total_chunks": collection.count() if collection else 0,
            "mode": "incremental" if (incremental and not full_reindex) else "full",
        }
        if last_chunk:
            new_state["last_chunk_id"] = last_chunk["id"]
            # Store raw messages of last chunk for boundary re-chunking
            raw = last_chunk.get("_raw_msgs", [])
            # Keep only essential fields to minimize state file size
            new_state["last_chunk_msgs"] = [
                {
                    "id": m["id"],
                    "timestamp": m["timestamp"],
                    "timestamp_unix": m["timestamp_unix"],
                    "from": m["from"],
                    "text": m["text"],
                    "target_agents": m.get("target_agents", []),
                    "has_command": m.get("has_command", False),
                    "reply_to": m.get("reply_to"),
                }
                for m in raw
            ]
        save_state(new_state)
        print(f"\n[4/9] State saved to {STATE_FILE}")
        print(f"  last_msg_id: {new_state['last_msg_id']}")
        print(f"  last_chunk_id: {new_state.get('last_chunk_id', 'N/A')}")
    else:
        print(f"\n[4/9] No messages — state unchanged")

    # Daily summaries (use all chunks, not just new ones for incremental)
    print(f"\n[5/9] Generating daily summaries...")
    all_chunks_for_summary = chunks if not (incremental and not full_reindex) else chunks
    summaries = generate_daily_summaries(all_chunks_for_summary)
    summary_col = ingest_summaries(summaries, DB_PATH)
    print(f"  Generated {len(summaries)} daily summaries → '{SUMMARY_COLLECTION}' ({summary_col.count()} docs)")

    # Entity index
    if EntityIndex is not None:
        print(f"\n[6/9] Building entity index...")
        entity_idx = EntityIndex()
        if not (incremental and not full_reindex and state.get("last_msg_id")):
            entity_idx.reset()
        entity_idx.index_chunks(chunks)
        stats = entity_idx.stats()
        print(f"  Entities: {stats['entities']}")
        print(f"  Relations: {stats['total_relations']}")
        print(f"  Facts: {stats['total_facts']} ({stats['current_facts']} current, {stats['expired_facts']} expired)")
        entity_idx.close()
    else:
        print(f"\n[6/9] Entity index skipped (entities module not available)")

    print(f"\n[7/9] Wing/room distribution:")
    wing_summary = {}
    for c in chunks:
        w = c["metadata"].get("wing", "general")
        wing_summary[w] = wing_summary.get(w, 0) + 1
    for w, count in sorted(wing_summary.items(), key=lambda x: -x[1]):
        rooms = {}
        for c in chunks:
            if c["metadata"].get("wing") == w and c["metadata"].get("room"):
                r = c["metadata"]["room"]
                rooms[r] = rooms.get(r, 0) + 1
        room_str = ", ".join(f"{r}:{n}" for r, n in sorted(rooms.items(), key=lambda x: -x[1])[:5])
        print(f"  {w}: {count} chunks" + (f" ({room_str})" if room_str else ""))

    # Raw message index
    print(f"\n[8/9] Indexing raw messages ({len(all_messages)} messages)...")
    msg_col = ingest_messages(all_messages, DB_PATH, msg_lookup)
    print(f"  Collection '{MESSAGES_COLLECTION}' ready ({msg_col.count()} documents)")

    print(f"\n[9/9] Verifying...")
    if collection:
        test_search(collection)

    print(f"\n  Done! ChromaDB at {DB_PATH}")


if __name__ == "__main__":
    main()
