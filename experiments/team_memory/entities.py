#!/usr/bin/env python3
"""Entity extraction and SQLite index for team memory knowledge graph.

Extracts structured entities (PRs, issues, agents, decisions, deploys, costs)
from chunk text at ingest time. Stores in SQLite for fast graph traversal
at query time.

Usage:
    # Standalone: extract from existing ChromaDB
    python3 entities.py --from-chromadb

    # Called from ingest.py: extract from chunks
    from entities import EntityIndex
    idx = EntityIndex()
    idx.index_chunks(chunks)
"""

import os
import re
import sqlite3
from pathlib import Path

# --- Config ---
DB_PATH = os.path.expanduser("~/team/rnd/team-memory/entities.db")

AGENT_NAMES = frozenset({
    "chen", "finn", "geni", "hiro", "jin", "kai", "kelvin", "kenji",
    "lee", "luck", "mon", "noa", "ren", "ryo", "sora", "taro", "x", "yuki",
})

# --- Entity extraction regexes ---

# PRs: github.com/.../pull/NNNN, PR #NNNN, #NNNN (4-5 digits)
RE_PR = re.compile(
    r'(?:pull/|PR\s*#?|pr\s*#?)(\d{4,5})'
    r'|#(\d{4,5})(?=\s|[)\].,;:!?]|$)',
    re.IGNORECASE,
)

# Issues: github.com/.../issues/NNNN, issue #NNNN
RE_ISSUE = re.compile(
    r'(?:issues?/|issue\s*#?)(\d{4,5})',
    re.IGNORECASE,
)

# Cost/price: $NNN.NN, $NNK, NNN MRR/ARR
RE_COST = re.compile(
    r'\$(\d[\d,.]+[KkMm]?)'
    r'|(\d[\d,.]+)\s*(?:USD|MRR|ARR)',
    re.IGNORECASE,
)

# Decision patterns: manager approvals/rejections
RE_DECISION = re.compile(
    r'\b(?:approved?|lgtm|merged?|go\s+with|decided?|'
    r'yes\s+do\s+it|let.s\s+use|rejected?|don.t\s+use|'
    r'don.t\s+do|cancel|reverted?|rolled?\s*back)\b',
    re.IGNORECASE,
)

# Deploy/release patterns
RE_DEPLOY = re.compile(
    r'\b(?:deployed?|released?|shipped|pushed?\s+to\s+prod|'
    r'went\s+live|cut\s+(?:a\s+)?release|hotfix)\b'
    r'|v(\d+\.\d+\.\d+)',
    re.IGNORECASE,
)

# GitHub URLs (for repo/owner extraction)
RE_GITHUB_URL = re.compile(
    r'github\.com/([\w.-]+)/([\w.-]+)/(?:pull|issues)/(\d+)',
)

# --- Fact extraction regexes (status changes near PR/issue numbers) ---

# PR status: "merged #6312", "lgtm merged", "PR closed"
RE_PR_MERGED = re.compile(
    r'(?:merged|lgtm.*merge[ds]?|merge\s+(?:this|it|the\s+pr))'
    r'|✅\s*(?:merged|PR\s+merged)',
    re.IGNORECASE,
)
RE_PR_CLOSED = re.compile(
    r'(?:closed?\s+(?:the\s+)?(?:PR|issue|#\d))|(?:PR\s+closed)',
    re.IGNORECASE,
)
RE_PR_APPROVED = re.compile(
    r'(?:approved?\s+(?:the\s+)?(?:PR|#\d))|(?:PR\s+approved)',
    re.IGNORECASE,
)
RE_PR_REVERTED = re.compile(
    r'(?:reverted?|rolled?\s*back|rollback)\s*(?:(?:the\s+)?(?:PR|#\d|changes?))?',
    re.IGNORECASE,
)

# Deploy with version: "v0.5.4 deployed", "deployed v1.2.3"
RE_DEPLOY_VERSION = re.compile(
    r'v(\d+\.\d+\.\d+)\s+deployed'
    r'|deployed.*v(\d+\.\d+\.\d+)'
    r'|release\s+v(\d+\.\d+\.\d+)',
    re.IGNORECASE,
)

# Cost figures with context: "$37.3K MRR", "$12.4K/day"
RE_COST_FIGURE = re.compile(
    r'\$(\d[\d,.]+[KkMm]?(?:/\w+)?)\s*(?:MRR|ARR|/day|/month|per\s+\w+|revenue)?',
)


class EntityIndex:
    """SQLite-backed entity index for knowledge graph queries."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS entities (
                id          TEXT PRIMARY KEY,   -- e.g. "pr:6312", "agent:lee"
                type        TEXT NOT NULL,      -- pr, issue, agent, cost, deploy, decision
                name        TEXT NOT NULL,      -- display name: "#6312", "lee", "$37.3K"
                first_seen  TEXT,               -- earliest timestamp
                last_seen   TEXT,               -- latest timestamp
                extra       TEXT DEFAULT ''     -- JSON blob for type-specific data
            );

            CREATE TABLE IF NOT EXISTS relations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id   TEXT NOT NULL,      -- FK to entities.id
                chunk_id    TEXT NOT NULL,       -- ChromaDB chunk ID
                rel_type    TEXT NOT NULL,       -- mentioned, authored, decided, deployed
                agent       TEXT DEFAULT '',     -- agent involved (if applicable)
                timestamp   TEXT NOT NULL,       -- chunk timestamp
                FOREIGN KEY (entity_id) REFERENCES entities(id)
            );

            CREATE TABLE IF NOT EXISTS facts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id   TEXT NOT NULL,      -- FK to entities.id
                key         TEXT NOT NULL,       -- "status", "cost", "version", "assignee"
                value       TEXT NOT NULL,       -- "merged", "$12.4K/day", "v0.11.251", "ryo"
                valid_from  TEXT NOT NULL,       -- timestamp when fact became true
                valid_to    TEXT,                -- NULL = still current, set when superseded
                chunk_id    TEXT DEFAULT '',     -- source chunk
                FOREIGN KEY (entity_id) REFERENCES entities(id)
            );

            CREATE INDEX IF NOT EXISTS idx_relations_entity ON relations(entity_id);
            CREATE INDEX IF NOT EXISTS idx_relations_chunk ON relations(chunk_id);
            CREATE INDEX IF NOT EXISTS idx_relations_agent ON relations(agent);
            CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);
            CREATE INDEX IF NOT EXISTS idx_facts_entity ON facts(entity_id);
            CREATE INDEX IF NOT EXISTS idx_facts_current ON facts(entity_id, key, valid_to);
        """)
        self.conn.commit()

    def reset(self):
        """Drop and recreate all tables."""
        self.conn.executescript("""
            DROP TABLE IF EXISTS facts;
            DROP TABLE IF EXISTS relations;
            DROP TABLE IF EXISTS entities;
        """)
        self._create_tables()

    def extract_entities(self, chunk_id: str, text: str, metadata: dict) -> list[dict]:
        """Extract all entities from a single chunk. Returns list of entity dicts."""
        entities = []
        timestamp = metadata.get("timestamp_start", "")
        chunk_from = metadata.get("from", "")

        # --- PRs ---
        seen_prs = set()
        for m in RE_PR.finditer(text):
            num = m.group(1) or m.group(2)
            if num and num not in seen_prs and len(num) >= 4:
                seen_prs.add(num)
                entities.append({
                    "entity_id": f"pr:{num}",
                    "type": "pr",
                    "name": f"#{num}",
                    "rel_type": "mentioned",
                    "agent": chunk_from,
                    "timestamp": timestamp,
                })

        # Dedupe: if same number is both PR and issue, PR wins
        pr_nums = seen_prs.copy()

        # --- Issues ---
        for m in RE_ISSUE.finditer(text):
            num = m.group(1)
            if num and num not in pr_nums:
                entities.append({
                    "entity_id": f"issue:{num}",
                    "type": "issue",
                    "name": f"#{num}",
                    "rel_type": "mentioned",
                    "agent": chunk_from,
                    "timestamp": timestamp,
                })

        # --- Agent mentions ---
        text_lower = text.lower()
        for agent in AGENT_NAMES:
            # Match @agent, agent:, or standalone agent name with word boundary
            if re.search(rf'(?:@{agent}\b|^{agent}:|{agent}:)', text_lower, re.MULTILINE):
                entities.append({
                    "entity_id": f"agent:{agent}",
                    "type": "agent",
                    "name": agent,
                    "rel_type": "mentioned",
                    "agent": chunk_from,
                    "timestamp": timestamp,
                })

        # --- Cost figures ---
        for m in RE_COST.finditer(text):
            value = m.group(1) or m.group(2)
            if value and not value.startswith("0"):
                # Skip trivial values like $0, $1 (likely code/template)
                try:
                    clean = value.replace(",", "").rstrip("KkMm")
                    if float(clean) >= 5:  # skip tiny amounts
                        entities.append({
                            "entity_id": f"cost:{value}",
                            "type": "cost",
                            "name": f"${value}",
                            "rel_type": "mentioned",
                            "agent": chunk_from,
                            "timestamp": timestamp,
                        })
                except ValueError:
                    pass

        # --- Decisions (manager only) ---
        if chunk_from == "manager":
            for m in RE_DECISION.finditer(text):
                keyword = m.group(0).lower().strip()
                # Find what the decision is about — look for PR/issue nearby
                context_window = text[max(0, m.start() - 100):m.end() + 100]
                pr_match = RE_PR.search(context_window)
                subject = f"#{pr_match.group(1) or pr_match.group(2)}" if pr_match else ""

                entities.append({
                    "entity_id": f"decision:{chunk_id}:{keyword}",
                    "type": "decision",
                    "name": f"{keyword} {subject}".strip(),
                    "rel_type": "decided",
                    "agent": chunk_from,
                    "timestamp": timestamp,
                })
                break  # One decision per chunk is enough

        # --- Deploys/releases ---
        for m in RE_DEPLOY.finditer(text):
            version = m.group(1) if m.group(1) else ""
            deploy_name = f"v{version}" if version else m.group(0).strip().lower()
            entities.append({
                "entity_id": f"deploy:{chunk_id}",
                "type": "deploy",
                "name": deploy_name,
                "rel_type": "deployed",
                "agent": chunk_from,
                "timestamp": timestamp,
            })
            break  # One deploy event per chunk

        # --- GitHub URLs (enrich PR/issue entities with repo info) ---
        for m in RE_GITHUB_URL.finditer(text):
            owner, repo, num = m.group(1), m.group(2), m.group(3)
            eid = f"pr:{num}"
            # Update existing entity with repo context
            for e in entities:
                if e["entity_id"] == eid:
                    e["extra"] = f"{owner}/{repo}"
                    break

        # --- Facts extraction (status changes, cost figures) ---
        facts = []

        # PR status changes: find PRs in chunk, detect status keywords nearby
        for pr_num in seen_prs:
            eid = f"pr:{pr_num}"
            if RE_PR_MERGED.search(text):
                facts.append({"entity_id": eid, "key": "status", "value": "merged",
                              "valid_from": timestamp, "chunk_id": chunk_id})
            elif RE_PR_REVERTED.search(text):
                facts.append({"entity_id": eid, "key": "status", "value": "reverted",
                              "valid_from": timestamp, "chunk_id": chunk_id})
            elif RE_PR_CLOSED.search(text):
                facts.append({"entity_id": eid, "key": "status", "value": "closed",
                              "valid_from": timestamp, "chunk_id": chunk_id})
            elif RE_PR_APPROVED.search(text):
                facts.append({"entity_id": eid, "key": "status", "value": "approved",
                              "valid_from": timestamp, "chunk_id": chunk_id})

        # Deploy versions
        for m in RE_DEPLOY_VERSION.finditer(text):
            version = m.group(1) or m.group(2) or m.group(3)
            if version:
                facts.append({"entity_id": f"deploy:v{version}", "key": "version",
                              "value": f"v{version}", "valid_from": timestamp,
                              "chunk_id": chunk_id})

        # Cost figures near entity context
        for m in RE_COST_FIGURE.finditer(text):
            value = m.group(1)
            if value:
                try:
                    clean = value.split("/")[0].replace(",", "").rstrip("KkMm")
                    if float(clean) >= 10:
                        # Find associated entity (PR or topic keyword nearby)
                        ctx = text[max(0, m.start() - 150):m.end() + 50].lower()
                        topic = "general"
                        for keyword in ["gemini", "openai", "gpt", "tts", "mrr", "arr",
                                        "revenue", "billing", "api cost"]:
                            if keyword in ctx:
                                topic = keyword
                                break
                        facts.append({"entity_id": f"cost_topic:{topic}", "key": "amount",
                                      "value": f"${value}", "valid_from": timestamp,
                                      "chunk_id": chunk_id})
                except ValueError:
                    pass

        # Store facts with extracted entities for index_chunk to process
        for e in entities:
            e["_facts"] = []
        if facts:
            # Attach facts to a dummy entry so index_chunk picks them up
            entities.append({"_facts_only": True, "_facts": facts})

        return entities

    def _upsert_fact(self, entity_id: str, key: str, value: str,
                     valid_from: str, chunk_id: str):
        """Insert a fact with automatic staleness management.

        If an existing current fact (valid_to IS NULL) for the same entity+key
        has a different value, close it (set valid_to) before inserting new one.
        """
        # Check for existing current fact with same key
        existing = self.conn.execute("""
            SELECT id, value, valid_from FROM facts
            WHERE entity_id = ? AND key = ? AND valid_to IS NULL
            ORDER BY valid_from DESC LIMIT 1
        """, (entity_id, key)).fetchone()

        if existing:
            old_id, old_value, old_from = existing
            if old_value == value:
                return  # Same fact already current, skip
            if old_from >= valid_from:
                return  # Existing fact is newer or same time, skip
            # Close the old fact
            self.conn.execute("""
                UPDATE facts SET valid_to = ? WHERE id = ?
            """, (valid_from, old_id))

        # Insert new current fact
        self.conn.execute("""
            INSERT INTO facts (entity_id, key, value, valid_from, valid_to, chunk_id)
            VALUES (?, ?, ?, ?, NULL, ?)
        """, (entity_id, key, value, valid_from, chunk_id))

    def index_chunk(self, chunk_id: str, text: str, metadata: dict):
        """Extract and store entities and facts for a single chunk."""
        entities = self.extract_entities(chunk_id, text, metadata)
        for e in entities:
            # Handle facts-only entries
            if e.get("_facts_only"):
                for fact in e.get("_facts", []):
                    self._upsert_fact(
                        fact["entity_id"], fact["key"], fact["value"],
                        fact["valid_from"], fact["chunk_id"],
                    )
                continue

            # Upsert entity
            self.conn.execute("""
                INSERT INTO entities (id, type, name, first_seen, last_seen, extra)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    last_seen = MAX(last_seen, excluded.last_seen),
                    first_seen = MIN(first_seen, excluded.first_seen),
                    extra = CASE WHEN excluded.extra != '' THEN excluded.extra ELSE extra END
            """, (
                e["entity_id"], e["type"], e["name"],
                e["timestamp"], e["timestamp"],
                e.get("extra", ""),
            ))

            # Insert relation
            self.conn.execute("""
                INSERT INTO relations (entity_id, chunk_id, rel_type, agent, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (e["entity_id"], chunk_id, e["rel_type"], e["agent"], e["timestamp"]))

    def index_chunks(self, chunks: list[dict]):
        """Index a batch of chunks. Call commit() after."""
        for chunk in chunks:
            self.index_chunk(
                chunk["id"],
                chunk["text"],
                chunk["metadata"],
            )
        self.conn.commit()

    def delete_chunk(self, chunk_id: str):
        """Remove all relations for a chunk (for re-indexing)."""
        self.conn.execute("DELETE FROM relations WHERE chunk_id = ?", (chunk_id,))
        self.conn.commit()

    def find_related_chunks(self, entity_ids: list[str], exclude_chunks: set[str] = None) -> list[dict]:
        """Find all chunks related to given entities.

        Returns list of {chunk_id, entity_id, entity_name, rel_type, agent, timestamp}
        sorted by timestamp descending.
        """
        if not entity_ids:
            return []

        placeholders = ",".join("?" for _ in entity_ids)
        query = f"""
            SELECT r.chunk_id, r.entity_id, e.name, e.type, r.rel_type, r.agent, r.timestamp
            FROM relations r
            JOIN entities e ON r.entity_id = e.id
            WHERE r.entity_id IN ({placeholders})
            ORDER BY r.timestamp DESC
        """
        rows = self.conn.execute(query, entity_ids).fetchall()

        results = []
        seen = set()
        for chunk_id, eid, name, etype, rel, agent, ts in rows:
            if exclude_chunks and chunk_id in exclude_chunks:
                continue
            if chunk_id not in seen:
                seen.add(chunk_id)
                results.append({
                    "chunk_id": chunk_id,
                    "entity_id": eid,
                    "entity_name": name,
                    "entity_type": etype,
                    "rel_type": rel,
                    "agent": agent,
                    "timestamp": ts,
                })

        return results

    def extract_entities_from_text(self, text: str) -> list[str]:
        """Extract entity IDs from arbitrary text (e.g., a search query or chunk).

        Used at query time to find entities in search results,
        then traverse the graph for related chunks.
        """
        entity_ids = []

        for m in RE_PR.finditer(text):
            num = m.group(1) or m.group(2)
            if num and len(num) >= 4:
                entity_ids.append(f"pr:{num}")

        for m in RE_ISSUE.finditer(text):
            num = m.group(1)
            if num:
                entity_ids.append(f"issue:{num}")

        text_lower = text.lower()
        for agent in AGENT_NAMES:
            if re.search(rf'(?:@{agent}\b|\b{agent}:|\b{agent}\b)', text_lower):
                entity_ids.append(f"agent:{agent}")

        return list(set(entity_ids))

    def get_entity(self, entity_id: str) -> dict | None:
        """Get entity details by ID."""
        row = self.conn.execute(
            "SELECT id, type, name, first_seen, last_seen, extra FROM entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        if row:
            return {
                "id": row[0], "type": row[1], "name": row[2],
                "first_seen": row[3], "last_seen": row[4], "extra": row[5],
            }
        return None

    def get_entity_chunks(self, entity_id: str, limit: int = 50) -> list[str]:
        """Get all chunk IDs for an entity, newest first."""
        rows = self.conn.execute(
            "SELECT chunk_id FROM relations WHERE entity_id = ? ORDER BY timestamp DESC LIMIT ?",
            (entity_id, limit),
        ).fetchall()
        return [r[0] for r in rows]

    def get_current_facts(self, entity_id: str) -> list[dict]:
        """Get all current (non-expired) facts for an entity."""
        rows = self.conn.execute("""
            SELECT key, value, valid_from, chunk_id FROM facts
            WHERE entity_id = ? AND valid_to IS NULL
            ORDER BY valid_from DESC
        """, (entity_id,)).fetchall()
        return [{"key": r[0], "value": r[1], "valid_from": r[2], "chunk_id": r[3]}
                for r in rows]

    def get_fact_history(self, entity_id: str, key: str = None) -> list[dict]:
        """Get full fact history for an entity (all states, including expired)."""
        if key:
            rows = self.conn.execute("""
                SELECT key, value, valid_from, valid_to, chunk_id FROM facts
                WHERE entity_id = ? AND key = ?
                ORDER BY valid_from
            """, (entity_id, key)).fetchall()
        else:
            rows = self.conn.execute("""
                SELECT key, value, valid_from, valid_to, chunk_id FROM facts
                WHERE entity_id = ?
                ORDER BY key, valid_from
            """, (entity_id,)).fetchall()
        return [{"key": r[0], "value": r[1], "valid_from": r[2],
                 "valid_to": r[3], "chunk_id": r[4]} for r in rows]

    def get_entity_card(self, entity_id: str) -> dict | None:
        """Build a compact entity card for Haiku context.

        Returns entity info + current facts + agent involvement + timeline.
        """
        entity = self.get_entity(entity_id)
        if not entity:
            return None

        facts = self.get_current_facts(entity_id)
        history = self.get_fact_history(entity_id)

        # Get agents involved
        agents = self.conn.execute("""
            SELECT DISTINCT agent FROM relations
            WHERE entity_id = ? AND agent != ''
        """, (entity_id,)).fetchall()

        # Get chunk count
        chunk_count = self.conn.execute("""
            SELECT COUNT(DISTINCT chunk_id) FROM relations WHERE entity_id = ?
        """, (entity_id,)).fetchone()[0]

        return {
            "entity": entity,
            "current_facts": facts,
            "fact_history": history,
            "agents": [r[0] for r in agents],
            "chunk_count": chunk_count,
        }

    def format_entity_card(self, entity_id: str) -> str:
        """Format entity card as text for Haiku prompt context."""
        card = self.get_entity_card(entity_id)
        if not card:
            return ""

        e = card["entity"]
        lines = [f"[{e['type'].upper()}] {e['name']} ({e['first_seen'][:10]} → {e['last_seen'][:10]})"]

        if e.get("extra"):
            lines.append(f"  repo: {e['extra']}")

        if card["agents"]:
            lines.append(f"  agents: {', '.join(card['agents'])}")

        lines.append(f"  chunks: {card['chunk_count']}")

        if card["current_facts"]:
            for f in card["current_facts"]:
                lines.append(f"  {f['key']}: {f['value']} (since {f['valid_from'][:10]})")

        if card["fact_history"]:
            history_items = []
            for f in card["fact_history"]:
                if f["valid_to"]:
                    history_items.append(f"{f['value']} ({f['valid_from'][:10]}→{f['valid_to'][:10]})")
            if history_items:
                lines.append(f"  history: {' → '.join(history_items)}")

        return "\n".join(lines)

    @staticmethod
    def _parse_messages_from_chunk(chunk_text: str) -> list[tuple[str, str, str]]:
        """Parse individual messages from chunk text.

        Chunk format: [YYYY-MM-DDTHH:MM] sender: message text
        Messages may span multiple lines until the next [timestamp] header.

        Returns list of (timestamp, sender, text) tuples.
        """
        messages = []
        # Split on message headers: [2026-04-06T09:21] sender:
        parts = re.split(r'(?=\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}\]\s)', chunk_text)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            m = re.match(r'\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})\]\s+([^:]+):\s*(.*)', part, re.DOTALL)
            if m:
                messages.append((m.group(1), m.group(2).strip(), m.group(3).strip()))
        return messages

    def _entity_search_terms(self, entity_id: str) -> list[str]:
        """Get search terms for finding an entity mention in text."""
        entity = self.get_entity(entity_id)
        if not entity:
            return []
        etype = entity["type"]
        name = entity["name"]
        terms = []
        if etype == "pr":
            num = entity_id.split(":")[1]
            terms = [f"#{num}", f"pull/{num}", f"PR {num}", f"PR #{num}", f"pr {num}"]
        elif etype == "issue":
            num = entity_id.split(":")[1]
            terms = [f"#{num}", f"issue {num}", f"issue #{num}", f"issues/{num}"]
        elif etype == "agent":
            agent = entity_id.split(":")[1]
            terms = [f"@{agent}", f"{agent}:"]
        elif etype == "deploy":
            terms = [name]
        elif etype == "cost":
            terms = [name]
        elif etype == "decision":
            terms = [name]
        return terms

    def build_entity_timeline(self, entity_id: str, collection) -> str:
        """Build a full chronological timeline for an entity.

        Parses individual messages from chunk text to get correct
        message-level attribution (sender + timestamp). Only includes
        messages that actually mention the entity.

        Args:
            entity_id: Entity ID (e.g., "pr:6312")
            collection: ChromaDB collection to fetch chunk documents from

        Returns:
            Full timeline text ready for Haiku synthesis.
            Empty string if entity not found or no chunks.
        """
        entity = self.get_entity(entity_id)
        if not entity:
            return ""

        # Get ALL chunk IDs (no limit)
        chunk_ids = self.conn.execute(
            "SELECT DISTINCT chunk_id FROM relations WHERE entity_id = ? ORDER BY timestamp ASC",
            (entity_id,),
        ).fetchall()
        chunk_ids = [r[0] for r in chunk_ids]

        if not chunk_ids:
            return ""

        # Fetch chunk documents from ChromaDB
        try:
            result = collection.get(ids=chunk_ids, include=["documents", "metadatas"])
        except Exception:
            return ""

        # Get search terms for this entity
        search_terms = self._entity_search_terms(entity_id)
        search_terms_lower = [t.lower() for t in search_terms]

        # Parse individual messages from all chunks, filter to those mentioning entity
        all_messages = []  # (timestamp, sender, text, is_mention)
        seen_messages = set()  # deduplicate across overlapping chunks

        for doc in result["documents"]:
            messages = self._parse_messages_from_chunk(doc)
            for ts, sender, text in messages:
                msg_key = (ts, sender)
                if msg_key in seen_messages:
                    continue
                seen_messages.add(msg_key)

                text_lower = text.lower()
                mentions = any(term in text_lower for term in search_terms_lower)
                all_messages.append((ts, sender, text, mentions))

        # Sort chronologically
        all_messages.sort(key=lambda x: x[0])

        # Build entity card header
        card = self.format_entity_card(entity_id)

        # Fix entity card agents: use actual message senders from mention messages
        mention_senders = set()
        for ts, sender, text, is_mention in all_messages:
            if is_mention:
                mention_senders.add(sender.lower())
        # Include known agents who sent mention messages
        active_agents = sorted(mention_senders & AGENT_NAMES)
        # Also include non-agent senders (e.g., "manager")
        other_senders = sorted(mention_senders - AGENT_NAMES)
        all_senders = other_senders + active_agents
        if all_senders:
            card += f"\n  active in discussion: {', '.join(all_senders)}"

        # Separate mentions from context
        mention_msgs = [(ts, s, t) for ts, s, t, m in all_messages if m]
        context_msgs = [(ts, s, t) for ts, s, t, m in all_messages if not m]

        # Build timeline: mention messages get ~200 chars, centered on entity mention
        timeline_lines = []
        for ts, sender, text in mention_msgs:
            condensed = " ".join(text.split())
            if len(condensed) > 200:
                # Find the entity mention and show context around it
                text_lower = condensed.lower()
                best_pos = -1
                for term in search_terms_lower:
                    pos = text_lower.find(term)
                    if pos >= 0:
                        best_pos = pos
                        break
                if best_pos >= 0 and best_pos > 100:
                    # Show window around the mention
                    start = max(0, best_pos - 80)
                    end = min(len(condensed), start + 200)
                    condensed = "..." + condensed[start:end]
                    if end < len(condensed):
                        condensed += "..."
                else:
                    condensed = condensed[:197] + "..."
            timeline_lines.append(f"[{ts}] {sender}: {condensed}")

        # Include context messages only if total stays under budget
        mention_text = "\n".join(timeline_lines)
        if len(mention_text) < 4000 and context_msgs:
            timeline_lines.append("")
            timeline_lines.append(f"--- Context ({len(context_msgs)} msgs) ---")
            budget = 6000 - len(mention_text)
            added = 0
            for ts, sender, text in context_msgs:
                condensed = " ".join(text.split())
                if len(condensed) > 80:
                    condensed = condensed[:77] + "..."
                line = f"[{ts}] {sender}: {condensed}"
                budget -= len(line) + 1
                if budget < 0:
                    remaining = len(context_msgs) - added
                    timeline_lines.append(f"  ...({remaining} more context msgs)")
                    break
                timeline_lines.append(line)
                added += 1

        timeline_text = "\n".join(timeline_lines)

        return (
            f"{card}\n\n"
            f"--- Timeline: {len(mention_msgs)} mentions, "
            f"{len(context_msgs)} context msgs ---\n"
            f"{timeline_text}"
        )

    def stats(self) -> dict:
        """Return index statistics."""
        counts = {}
        for row in self.conn.execute("SELECT type, COUNT(*) FROM entities GROUP BY type"):
            counts[row[0]] = row[1]
        total_relations = self.conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        total_facts = self.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        current_facts = self.conn.execute(
            "SELECT COUNT(*) FROM facts WHERE valid_to IS NULL"
        ).fetchone()[0]
        expired_facts = total_facts - current_facts
        return {
            "entities": counts,
            "total_relations": total_relations,
            "total_facts": total_facts,
            "current_facts": current_facts,
            "expired_facts": expired_facts,
        }

    def close(self):
        self.conn.close()


def main():
    """Standalone: populate entity index from existing ChromaDB."""
    import sys

    from_chromadb = "--from-chromadb" in sys.argv

    if from_chromadb:
        import chromadb
        chromadb_path = os.path.expanduser("~/team/rnd/team-memory/db")
        print(f"Loading chunks from ChromaDB at {chromadb_path}...")
        client = chromadb.PersistentClient(path=chromadb_path)
        col = client.get_collection("team_memory_telegram")
        total = col.count()
        all_data = col.get(include=["documents", "metadatas"], limit=total)

        print(f"Loaded {total} chunks")

        idx = EntityIndex()
        idx.reset()

        for i, (cid, doc, meta) in enumerate(
            zip(all_data["ids"], all_data["documents"], all_data["metadatas"])
        ):
            idx.index_chunk(cid, doc, meta)
            if (i + 1) % 1000 == 0:
                idx.conn.commit()
                print(f"  Indexed {i + 1}/{total} chunks")

        idx.conn.commit()
        stats = idx.stats()
        print(f"\nEntity index built at {DB_PATH}")
        print(f"  Entities: {stats['entities']}")
        print(f"  Relations: {stats['total_relations']}")
        print(f"  Facts: {stats['total_facts']} total, {stats['current_facts']} current, {stats['expired_facts']} expired")

        # Show top entities per type
        for etype in ["pr", "issue", "agent", "decision", "deploy", "cost"]:
            rows = idx.conn.execute("""
                SELECT e.id, e.name, COUNT(r.id) as rel_count
                FROM entities e
                JOIN relations r ON e.id = r.entity_id
                WHERE e.type = ?
                GROUP BY e.id
                ORDER BY rel_count DESC
                LIMIT 5
            """, (etype,)).fetchall()
            if rows:
                print(f"\n  Top {etype}s:")
                for eid, name, count in rows:
                    print(f"    {name}: {count} chunks")

        # Show sample entity cards with facts
        print(f"\n  === Sample Entity Cards ===")
        sample_prs = idx.conn.execute("""
            SELECT DISTINCT entity_id FROM facts
            WHERE entity_id LIKE 'pr:%' LIMIT 5
        """).fetchall()
        for (eid,) in sample_prs:
            card = idx.format_entity_card(eid)
            if card:
                print(f"\n  {card}")

        idx.close()
    else:
        print("Usage: python3 entities.py --from-chromadb")
        sys.exit(1)


if __name__ == "__main__":
    main()
