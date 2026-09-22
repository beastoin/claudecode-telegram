# Team Memory — /memory Command Spec

## Overview

Searchable memory over the team's Telegram chat history. Manager sends `/memory <query>` in Telegram and gets a direct answer with source citations.

## Data Flow

```
Telegram Export (JSON)
  → Parser (luck: flatten text, extract metadata, filter noise)
  → Chunker (geni: 5-min time-window grouping)
  → Embedder (ChromaDB default: all-MiniLM-L6-v2, 384-dim)
  → ChromaDB (local persistent storage)
  → Search (lee: semantic + BM25 hybrid + temporal boost)
  → Haiku Reranker (score relevance + extract answer)
  → Telegram Response
```

## Command Interface

```
/memory <query>                     — search all history
/memory <query> --agent <name>      — filter by agent
/memory <query> --days <N>          — last N days only
/memory <query> --from manager      — manager messages only
```

### Response Format (Telegram)

```
🧠 <one-line answer>

📎 Sources:
1. [Apr 8] geni → lee: "Fixed table layout with white-space:nowrap..."
2. [Apr 7] manager → geni: "@geni this format table still not fixed?..."
3. [Mar 30] mon: "Daily ops — TTS cost analysis: Mistral $0.016/1K..."
```

- Answer line: direct answer from Haiku reranker
- Sources: top 3 chunks, truncated to ~100 chars each
- Total response: <500 chars to fit Telegram message comfortably

## Search Pipeline

### Stage 1: ChromaDB Semantic Search (<100ms)
```python
results = collection.query(
    query_texts=[query],
    n_results=20,
    where=filters  # optional: agent, date range, sender
)
```

### Stage 2: BM25 + Temporal Rerank (<10ms)
```python
for each candidate:
    # Keyword overlap (handles PR numbers, issue IDs, exact terms)
    bm25_score = keyword_overlap(query_tokens, doc_tokens)

    # Temporal boost (recent = higher)
    days_ago = (now - chunk_timestamp).days
    time_boost = 1.0 / (1.0 + days_ago * 0.05)

    # Hybrid score
    score = 0.7 * semantic_score + 0.2 * bm25_score + 0.1 * time_boost

# Return top 10
```

### Stage 3: Haiku Rerank + Answer Extraction (~500ms)
Always on for /memory command (quality matters for manager interaction).

**Model**: `claude-haiku-4-5-20251001`

**Prompt**:
```
You are a search reranker for a team chat log. A manager communicates with AI agents via Telegram.

Given a query and 10 candidate chunks, do two things:
1. Score each candidate's relevance to the query (0.0-1.0)
2. Extract a direct one-line answer from the best match

Scoring guide:
- 1.0 = directly answers the query with specific facts
- 0.7 = contains the answer mixed with unrelated content
- 0.3 = mentions related topics but doesn't answer
- 0.0 = irrelevant

Rules:
- The query may contain typos — match intent, not exact spelling
- Agent names: chen, finn, geni, hiro, jin, kai, kelvin, kenji, lee, luck, mon, noa, ren, ryo, sora, taro, x, yuki
- "Thinh" = the manager
- If no candidate answers the query, set answer to "Not found in chat history"
- Include agent name and date in the answer when possible

Query: {query}

Candidates:
{candidates}

Respond with ONLY this JSON (no markdown, no explanation):
{"answer":"one-line answer citing agent and date","rankings":[{"i":0,"s":0.9},{"i":1,"s":0.3}]}
```

**Candidate format** (with neighbor context, truncate match to 500 chars, neighbors to 300 chars):
```
[0] (2026-04-08, thinh) [preceding conversation] prev chunk text...
main chunk text...
[following conversation] next chunk text...

[1] (2026-04-07, beasts) [preceding conversation] prev chunk text...
main chunk text...
```

Each candidate includes 1 preceding + 1 following chunk from ChromaDB (by timestamp).
This gives Haiku conversation flow context to score relevance more accurately.

**Cost**: ~15K input + ~100 output tokens = ~$0.005/query
**Latency**: ~1.1s (neighbor lookup + Haiku call)
**Fallback**: If neighbor lookup or Haiku call fails, return BM25+temporal results without answer extraction.

## Chunking Strategy

### Conversation-Pair Chunking
Two-tier gap threshold preserves question→answer pairs:

1. **gap >= 5 min** (`GAP_THRESHOLD=300s`): always split (different conversation)
2. **gap >= 2 min AND sender changed** (`GAP_SENDER_CHANGE=120s`): split (separate topics)
3. **gap < 2 min**: keep together regardless of sender (conversation exchange)

This captures manager decisions like:
```
kai (beasts): "should we use approach A?"    ← question
manager (Thinh): "yes"                       ← answer  → ONE chunk
```

Without this, "yes" would be a standalone chunk with no semantic value.

- 60.4% of chunks contain both senders (conversation exchanges)
- Concatenate with format: `[2026-04-08T08:00] Thinh: message text`

### Reply Threading
When a message has `reply_to_message_id`, the original message is looked up and prepended as context:
```
[replying to sender: "original message text truncated to 200 chars"]
[2026-04-08T08:00] Thinh: response text
```
- Full message lookup built from ALL messages (not just current batch) for cross-boundary replies
- Deduplication: `seen_reply_ids` set prevents same reply context appearing twice in one chunk
- 597 messages (1.7%) have reply threading — concentrated in decision/instruction patterns
- `reply_to` field preserved in state.json for incremental ingest boundary re-chunking

### Stats (Full Dataset)
- 35,363 messages → 5,247 chunks
- Avg chunk: 1,234 chars, 6.7 messages
- Date range: 2026-01-24 → 2026-04-08 (79 days)
- Agents: all 18 represented
- From distribution: beasts 3,524 chunks (67%), thinh 1,722 chunks (33%)

### Noise Filtering
Skip messages containing only these commands:
`/start, /hire, /end, /settings, /compact, /new, /pilot, /pause, /restart, /progress, /status, /list, /use, /rewind`
Plus routing-only commands: `/lee, /mon, /kenji`, etc.

## ChromaDB Schema

**Collection**: `team_memory_telegram`
**Embedding**: all-MiniLM-L6-v2 (384-dim, cosine distance)

```python
collection.add(
    ids=["tg_{first_msg_id}_{last_msg_id}"],
    documents=["[timestamp] sender: text\n..."],
    metadatas=[{
        "from": "thinh" | "beasts",
        "target_agents": "geni,lee",        # comma-separated
        "timestamp_start": "2026-04-07T08:00:00",
        "timestamp_end": "2026-04-07T08:04:30",
        "msg_count": 5,
        "has_command": False,
    }]
)
```

### Metadata Filtering
```python
# By sender
where={"from": "thinh"}

# By agent
where={"target_agents": {"$contains": "geni"}}

# By date range
where={"timestamp_start": {"$gte": "2026-04-01"}}

# Combined
where={"$and": [{"from": "thinh"}, {"target_agents": {"$contains": "lee"}}]}
```

## Quality Benchmarks (Full Dataset, Semantic Only)

| # | Query | Before | After | Improvement |
|---|-------|--------|-------|-------------|
| 1 | force-graph vs vis.js decision | 0.696 | 0.695 | flat |
| 2 | chen image processing error | 0.447 | 0.283 | +37% |
| 3 | who assigned issue 6382 | 0.613 | 0.584 | +5% |
| 4 | batdongsan OTP problem | 0.658 | 0.588 | +11% |
| 5 | last GWS OAuth renewal | 0.391 | 0.400 | flat |
| 6 | manager on PR 6377 | 0.547 | 0.522 | +5% |
| 7 | omi CLI toolshed owner | 0.459 | 0.427 | +7% |
| 8 | mobile release version error | 0.426 | 0.426 | flat |
| 9 | weekly CTO report | 0.342 | 0.342 | flat |
| 10 | teleporting x back | 0.500 | 0.413 | +17% |

**Before**: time-window only (2,601 chunks). **After**: conversation-pair chunking (5,246 chunks).
**Result**: 6 queries improved, 4 flat, 0 regressions. Biggest win on decision/context queries.
**Expected with BM25 hybrid**: weak → OK (exact-term queries improve)
**Expected with Haiku rerank**: OK → good (relevance scoring fixes false positives)

## Ingest Modes

### Full Ingest
```bash
python3 team_memory/ingest.py /tmp/team-memory-parsed-full.jsonl
# or force full re-ingest:
python3 team_memory/ingest.py /tmp/team-memory-parsed-full.jsonl --full-reindex
```
Drops and recreates the collection. Use for first-time setup or schema changes.

### Incremental Ingest
```bash
python3 team_memory/ingest.py /tmp/team-memory-parsed-full.jsonl --incremental
```
Only processes messages with `id > last_msg_id` from state.json.

**State Tracking** (`~/team/rnd/team-memory/state.json`):
```json
{
  "last_msg_id": 3169596,
  "last_msg_timestamp_unix": 1775622769,
  "last_chunk_id": "tg_3169588_3169596",
  "last_chunk_msgs": [/* raw messages from last chunk */],
  "total_messages_ingested": 35363,
  "total_chunks": 2601
}
```

**Chunk Boundary Handling**: When new messages arrive within 5 min of the last ingested message, the old boundary chunk is deleted and re-chunked with the new messages merged in. This prevents splitting a conversation that spans the ingest boundary.

Example:
- Previous ingest ends at msg 3169596 (11:32:49), last chunk = `tg_3169588_3169596`
- New message arrives at 11:34:50 (gap=121s < 300s threshold)
- Ingester: deletes `tg_3169588_3169596`, merges its 8 messages + new messages, re-chunks
- If gap >= 300s: clean boundary, just adds new chunks without touching old data

### Ingest Schedule

**Manual (current)**:
1. Export Telegram chat from the bot (@beasts)
2. Place at `~/team/exports/ChatExport_<date>-text.json.zip`
3. Run parser: `python3 team_memory/parse.py <export.zip>`
4. Run ingest: `python3 team_memory/ingest.py /tmp/team-memory-parsed-full.jsonl --incremental`

**Automated (Phase 2)**:
- Cron: daily export via Telegram API + parse + incremental ingest
- Schedule: `0 4 * * * cd ~/claudecode-telegram && python3 team_memory/ingest.py --incremental`

## Configuration

```python
# team_memory/config.py
DB_PATH = "~/claudecode-telegram/team_memory/db"
COLLECTION_NAME = "team_memory_telegram"
GAP_THRESHOLD = 300          # 5 min — absolute boundary
GAP_SENDER_CHANGE = 120      # 2 min — split on sender change
MAX_CHUNK_CHARS = 2000
MIN_CHUNK_CHARS = 50
HAIKU_MODEL = "claude-haiku-4-5-20251001"
HAIKU_MAX_CANDIDATES = 10
CANDIDATE_TRUNCATE = 500     # chars per candidate in Haiku prompt
BM25_WEIGHT = 0.2
SEMANTIC_WEIGHT = 0.7
TEMPORAL_WEIGHT = 0.1
TEMPORAL_DECAY = 0.05        # higher = faster decay
```

**API Key**: Uses `ANTHROPIC_API_KEY` from environment (already set for bridge).

## Memory Stack Subcommands

4-layer memory system adding subcommands to `/memory`. CLI also available for direct testing.

### CLI

```bash
python3 -m team_memory.memory_stack status           # chunk counts, wing distribution
python3 -m team_memory.memory_stack wake-up           # L0 identity + L1 essential story
python3 -m team_memory.memory_stack wake-up --wing=omi
python3 -m team_memory.memory_stack recall --wing=omi --room=prs
python3 -m team_memory.memory_stack search who merged PR 6377
```

### Telegram Subcommands

| Command | What it does |
|---------|-------------|
| `/memory status` | Shows chunk counts, message counts, wing distribution |
| `/memory wake-up [wing]` | L0+L1 wake-up text (truncated to 4000 chars) |
| `/memory recall --wing=X [--room=Y]` | L2 on-demand retrieval, filtered by wing/room |
| `/memory <query>` | Existing search (unchanged) |

## File Layout

```
~/claudecode-telegram/team_memory/
  SPEC.md          # this file (includes memory stack spec)
  __init__.py
  config.py        # paths, weights, model config
  memory_stack.py  # 4-layer stack module (status, wake-up, recall)
  ingest.py        # Telegram JSON → ChromaDB
  search.py        # semantic + BM25 + temporal + Haiku
  parse.py         # Telegram export → cleaned JSONL
  db/              # ChromaDB persistent storage
```

## Phase 2: Planned Enhancements

1. **Knowledge Graph** (SQLite facts table): auto-extract entities from chunks (PR merges, assignments, deploys, releases) with `valid_from`/`valid_to` for staleness tracking
2. **JSONL transcript indexing**: add agent session transcripts as second data source
3. **Telegram API export**: automated daily export (no manual zip download)

## Dependencies

- `chromadb>=1.5.0` (includes sentence-transformers, hnswlib)
- `anthropic` (for Haiku reranker, already installed for bridge)
- Python 3.9+
