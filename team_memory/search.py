#!/usr/bin/env python3
"""Search team memory (Telegram chat history) via ChromaDB."""

import argparse
import json
import sys
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from .config import DB_PATH, COLLECTION, SUMMARY_COLLECTION
except ImportError:
    DB_PATH = os.path.expanduser("~/team/rnd/team-memory/db")
    COLLECTION = "team_memory_telegram"
    SUMMARY_COLLECTION = "team_memory_summaries"

MESSAGES_COLLECTION = "team_memory_messages"

# Natural language time expressions → days lookback
TIME_EXPRESSIONS = [
    (r"\btoday\b", 1),
    (r"\byesterday\b", 2),
    (r"\bthis week\b", 7),
    (r"\blast week\b", 14),
    (r"\bthis month\b", 31),
    (r"\blast month\b", 62),
    (r"\b(\d+)\s*days?\s*ago\b", None),  # dynamic
    (r"\brecent(?:ly)?\b", 7),
]


# Wing detection — maps query keywords to ChromaDB wing metadata
WING_KEYWORDS = {
    "omi": ["omi", "mobile", "flutter", "app store", "play store", "codemagic",
            "deepgram", "wearable", "android app", "ios app", "omi pr"],
    "claudecode-telegram": ["bridge", "telegram", "worker", "tmux", "/pilot",
                            "/hire", "team-memory", "/memory", "clawdbot"],
    "infrastructure": ["oauth", "gcp", "gcloud", "ssh", "tailscale", "server",
                       "docker", "emulator", "mac mini", "redis", "grafana"],
    "operations": ["daily report", "cto report", "weekly report", "billing",
                   "mrr", "revenue", "ops email"],
}

# Broad query patterns that benefit from summary search
BROAD_QUERY_PATTERNS = [
    r"\bwhat happened\b", r"\bweekly\b", r"\bsummary\b", r"\boverview\b",
    r"\bthis week\b", r"\blast week\b", r"\bthis month\b", r"\bstatus\b",
    r"\bwhat.+going on\b", r"\bupdate\b",
]


ROOM_KEYWORDS = {
    "omi": {
        "prs": ["pr #", "pull/", "merged", "pull request", "pr workflow"],
        "releases": ["release", "app store", "play store", "codemagic", "version", "build"],
        "costs": ["cost", "billing", "gemini cost", "openai cost", "pricing", "budget", "spend"],
        "mobile": ["flutter", "mobile", "android", "ios", "ui", "screen"],
        "backend": ["backend", "/v1/", "endpoint", "api", "server", "deploy"],
    },
    "claudecode-telegram": {
        "bridge": ["bridge", "telegram", "routing", "command"],
        "workers": ["worker", "tmux", "session", "/pilot", "/hire"],
        "team-memory": ["team-memory", "/memory", "chromadb", "search", "ingest"],
    },
    "infrastructure": {
        "oauth": ["oauth", "token", "credential", "gws", "refresh token"],
        "servers": ["server", "vps", "mac mini", "ssh", "tailscale"],
        "gcp": ["gcp", "gcloud", "google cloud", "cloud run"],
    },
}


def _detect_wing(query):
    """Detect wing from query keywords. Returns wing name or None."""
    import re
    q = query.lower()
    if re.search(r'\b(?:pr\s*#?\d{3,}|#\d{4,})\b', q):
        return "omi"
    for wing, keywords in WING_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            return wing
    return None


def _detect_room(query, wing):
    """Detect room from query keywords within a wing. Returns room name or None."""
    if not wing or wing not in ROOM_KEYWORDS:
        return None
    q = query.lower()
    for room, keywords in ROOM_KEYWORDS[wing].items():
        if any(kw in q for kw in keywords):
            return room
    return None


def _is_broad_query(query):
    """Check if query is broad enough to benefit from summary search."""
    import re
    q = query.lower()
    return any(re.search(p, q) for p in BROAD_QUERY_PATTERNS)


def parse_time_from_query(query):
    """Extract time filter from natural language query. Returns (days, cleaned_query)."""
    import re
    query_lower = query.lower()
    for pattern, days_val in TIME_EXPRESSIONS:
        m = re.search(pattern, query_lower)
        if m:
            if days_val is None:
                # Dynamic: "N days ago"
                days_val = int(m.group(1)) + 1
            # Remove the time expression from query for cleaner semantic search
            cleaned = query[:m.start()] + query[m.end():]
            cleaned = re.sub(r'\s+', ' ', cleaned).strip()
            return days_val, cleaned if cleaned else query
    return None, query


def get_neighbor_chunks(collection, candidate_ids, candidate_metadatas):
    """Fetch neighboring chunks (1 before + 1 after) for each candidate.

    Returns dict: {chunk_id: {"prev": text|None, "next": text|None}}

    Uses prev_chunk_id / next_chunk_id stored in metadata at ingest time.
    Single batch get() call — O(1) per neighbor, no full-collection scan.
    """
    # Collect all neighbor IDs to fetch in one batch
    neighbor_ids = set()
    for meta in candidate_metadatas:
        if meta.get("prev_chunk_id"):
            neighbor_ids.add(meta["prev_chunk_id"])
        if meta.get("next_chunk_id"):
            neighbor_ids.add(meta["next_chunk_id"])

    if not neighbor_ids:
        return {cid: {"prev": None, "next": None} for cid in candidate_ids}

    # Single batch fetch for all neighbors
    nb_data = collection.get(ids=list(neighbor_ids), include=["documents"])
    nb_docs = {cid: doc for cid, doc in zip(nb_data["ids"], nb_data["documents"])}

    # Map back to candidates
    neighbors = {}
    for chunk_id, meta in zip(candidate_ids, candidate_metadatas):
        prev_id = meta.get("prev_chunk_id", "")
        next_id = meta.get("next_chunk_id", "")
        neighbors[chunk_id] = {
            "prev": nb_docs.get(prev_id, "")[:300] if prev_id else None,
            "next": nb_docs.get(next_id, "")[:300] if next_id else None,
        }

    return neighbors


def rerank_with_haiku(query, candidates, n_results=5, collection=None,
                      summary_context="", entity_card_context=""):
    """Rerank candidates using Claude Haiku for relevance scoring."""
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Warning: ANTHROPIC_API_KEY not set, skipping LLM rerank", file=sys.stderr)
        return None

    # Fetch neighbor chunks for context if collection available
    neighbor_ctx = {}
    if collection:
        try:
            candidate_ids = [c.get("_id", "") for c in candidates]
            candidate_metas = [c.get("_meta", {}) for c in candidates]
            neighbor_ctx = get_neighbor_chunks(collection, candidate_ids, candidate_metas)
        except Exception as e:
            print(f"Warning: neighbor lookup failed ({e})", file=sys.stderr)

    # Build chunks for prompt (truncate to 500 chars each)
    chunks = []
    for i, c in enumerate(candidates):
        text = c["text"][:500] + "..." if len(c["text"]) > 500 else c["text"]

        # Add neighbor context if available
        chunk_id = c.get("_id", "")
        nb = neighbor_ctx.get(chunk_id, {})
        parts = []
        if nb.get("prev"):
            parts.append(f"[preceding conversation] {nb['prev']}")
        parts.append(text)
        if nb.get("next"):
            parts.append(f"[following conversation] {nb['next']}")

        combined = "\n".join(parts)
        chunks.append(f"[{i}] ({c['date']}, {c['from']}) {combined}")

    chunks_text = "\n\n".join(chunks)

    system_prompt = (
        "You are a search relevance judge for a team chat memory system. "
        "Given a query and numbered chat chunks, score each chunk's relevance "
        "to the query from 0.0 to 1.0. Also provide a one-sentence answer to "
        "the query based on the chunks.\n\n"
        "Respond ONLY with valid JSON, no markdown:\n"
        '{"answer": "one-sentence answer", "rankings": [{"index": 0, "score": 0.9}, ...]}'
    )

    # Build user message with optional enrichment context
    extra_ctx = []
    if summary_context:
        extra_ctx.append(f"Daily summaries (background context):\n{summary_context}")
    if entity_card_context:
        extra_ctx.append(f"Entity cards:\n{entity_card_context}")
    prefix = "\n\n".join(extra_ctx) + "\n\n" if extra_ctx else ""
    user_msg = f"{prefix}Query: {query}\n\nChunks:\n{chunks_text}"

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )

        raw = response.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        data = json.loads(raw)
        answer = data.get("answer", "")
        rankings = data.get("rankings", [])

        # Apply Haiku scores, filter >= 0.3
        reranked = []
        for r in rankings:
            idx = r.get("index")
            score = r.get("score", 0)
            if idx is not None and 0 <= idx < len(candidates) and score >= 0.3:
                entry = {k: v for k, v in candidates[idx].items() if not k.startswith("_") or k == "_id"}
                entry["score"] = round(score, 4)
                reranked.append(entry)

        reranked.sort(key=lambda x: x["score"], reverse=True)
        return {"answer": answer, "results": reranked[:n_results]}

    except Exception as e:
        print(f"Warning: Haiku rerank failed ({e}), falling back to BM25", file=sys.stderr)
        return None


def search(query, from_filter=None, agent_filter=None, days=None, n_results=5, rerank=False):
    """Query ChromaDB and return ranked results.

    Auto-detects entity-specific queries (PR/issue numbers) and switches to
    Entity Timeline Mode for full-coverage recall when rerank is enabled.
    """
    import chromadb

    # Auto-detect entity queries for timeline mode (when rerank enabled)
    if rerank:
        timeline_result = search_entity_timeline(query, n_results=n_results)
        if timeline_result:
            return timeline_result

    # Parse natural language time from query if no explicit --days
    time_days, clean_query = parse_time_from_query(query)
    if time_days and not days:
        days = time_days
        query = clean_query

    client = chromadb.PersistentClient(path=DB_PATH)
    col = client.get_collection(COLLECTION)

    # Build where clause (only non-date filters — ChromaDB $gte needs numeric)
    conditions = []
    if from_filter:
        conditions.append({"from": from_filter.lower()})
    if agent_filter:
        conditions.append({"target_agents": {"$contains": agent_filter.lower()}})

    # Wing + room scoping — narrow search to project/topic when context is clear
    wing = _detect_wing(query)
    if wing:
        conditions.append({"wing": wing})
    room = _detect_room(query, wing)
    if room:
        conditions.append({"room": room})

    # Date cutoff for post-query filtering
    date_cutoff = None
    if days:
        date_cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")

    where = None
    if len(conditions) == 1:
        where = conditions[0]
    elif len(conditions) > 1:
        where = {"$and": conditions}

    # Fetch extra results when date-filtering (many may be excluded)
    fetch_n = n_results * 4 if date_cutoff else n_results
    kwargs = {"query_texts": [query], "n_results": fetch_n}
    if where:
        kwargs["where"] = where

    # Fetch extra candidates for reranking
    kwargs["n_results"] = max(fetch_n, 20)
    results = col.query(**kwargs)

    # Keyword fallback: find chunks containing exact terms that embeddings miss
    # (PR numbers, issue IDs, library names, URLs)
    import re as _re_kw
    exact_terms = _re_kw.findall(r'\b(?:\d{4,}|[a-z][\w.-]*\.(?:js|py|ts|go|rs))\b', query.lower())
    if exact_terms:
        try:
            kw_where = {"$or": [{"$contains": term} for term in exact_terms]} if len(exact_terms) > 1 else None
            total = col.count()
            all_chunks = col.get(include=["documents", "metadatas"], limit=total)
            semantic_ids = set(results["ids"][0]) if results["ids"] else set()
            for idx, (cid, doc, meta) in enumerate(zip(all_chunks["ids"], all_chunks["documents"], all_chunks["metadatas"])):
                if cid in semantic_ids:
                    continue
                doc_lower = doc.lower()
                if any(term in doc_lower for term in exact_terms):
                    # Inject into results
                    results["ids"][0].append(cid)
                    results["documents"][0].append(doc)
                    results["metadatas"][0].append(meta)
                    results["distances"][0].append(0.8)  # neutral distance
        except Exception:
            pass

    if not results["documents"] or not results["documents"][0]:
        return {"answer": "", "results": []} if rerank else []

    # Tokenize query for keyword overlap
    import re as _re
    query_tokens = set(_re.split(r'[\s\W]+', query.lower())) - {'', 'the', 'a', 'an', 'is', 'was', 'what', 'who', 'when', 'how', 'did', 'do', 'does'}
    now = datetime.now(timezone.utc)

    candidates = []
    result_ids = results.get("ids", [[]])[0]
    for i, (doc, meta, dist) in enumerate(zip(
        results["documents"][0], results["metadatas"][0], results["distances"][0]
    )):
        # Post-query date filter
        ts = meta.get("timestamp_start", "")
        if date_cutoff and ts < date_cutoff:
            continue

        semantic_score = 1 - dist  # cosine: 1=identical, 0=unrelated

        # BM25-lite: jaccard keyword overlap
        doc_tokens = set(_re.split(r'[\s\W]+', doc.lower())) - {''}
        overlap = len(query_tokens & doc_tokens) / len(query_tokens | doc_tokens) if query_tokens | doc_tokens else 0

        # Temporal boost: recent results score higher
        time_boost = 1.0
        if ts:
            try:
                doc_dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
                days_ago = max(0, (now - doc_dt).days)
                time_boost = 1.0 / (1.0 + days_ago * 0.05)
            except (ValueError, TypeError):
                pass

        final_score = (0.7 * semantic_score + 0.3 * overlap) * time_boost

        candidates.append({
            "text": doc,
            "from": meta.get("from", ""),
            "target_agents": meta.get("target_agents", "").split(",") if meta.get("target_agents") else [],
            "date": (ts or "")[:10],
            "score": round(final_score, 4),
            "msg_count": meta.get("msg_count", 0),
            "_id": result_ids[i] if i < len(result_ids) else "",
            "_meta": meta,
        })

    # Sort by hybrid score
    candidates.sort(key=lambda x: x["score"], reverse=True)

    # Stage 2.5: Graph expansion — find related chunks via entity index
    entity_ids_for_cards = []  # saved for Stage 2.8 entity card context
    try:
        try:
            from .entities import EntityIndex
        except ImportError:
            from entities import EntityIndex
        idx = EntityIndex()

        # Extract entities from top 5 candidates + the query itself
        top_texts = query + " " + " ".join(c["text"] for c in candidates[:5])
        entity_ids = idx.extract_entities_from_text(top_texts)
        entity_ids_for_cards = list(entity_ids) if entity_ids else []

        if entity_ids:
            existing_ids = {c.get("_id", "") for c in candidates}
            related = idx.find_related_chunks(entity_ids, exclude_chunks=existing_ids)

            related_chunk_ids = [r["chunk_id"] for r in related[:10]]
            if related_chunk_ids:
                fetched = col.get(ids=related_chunk_ids, include=["documents", "metadatas"])
                for cid, doc, meta in zip(fetched["ids"], fetched["documents"], fetched["metadatas"]):
                    ts = meta.get("timestamp_start", "")
                    candidates.append({
                        "text": doc,
                        "from": meta.get("from", ""),
                        "target_agents": meta.get("target_agents", "").split(",") if meta.get("target_agents") else [],
                        "date": (ts or "")[:10],
                        "score": 0.5,
                        "msg_count": meta.get("msg_count", 0),
                        "_id": cid,
                        "_meta": meta,
                    })
        idx.close()
    except Exception as e:
        print(f"Warning: graph expansion failed ({e})", file=sys.stderr)

    # Stage 2.7: Summary collection fallback for broad queries
    summary_context = ""
    if _is_broad_query(query):
        try:
            summ_col = client.get_collection(SUMMARY_COLLECTION)
            summ_results = summ_col.query(query_texts=[query], n_results=3)
            if summ_results["documents"] and summ_results["documents"][0]:
                summary_context = "\n\n".join(summ_results["documents"][0][:2])
        except Exception as e:
            print(f"Warning: summary search failed ({e})", file=sys.stderr)

    # Stage 2.8: Entity cards for Haiku context enrichment
    entity_card_context = ""
    if entity_ids_for_cards:
        try:
            try:
                from .entities import EntityIndex as EI2
            except ImportError:
                from entities import EntityIndex as EI2
            eidx = EI2()
            cards = []
            for eid in entity_ids_for_cards[:3]:
                card = eidx.format_entity_card(eid)
                if card:
                    cards.append(card)
            eidx.close()
            if cards:
                entity_card_context = "\n".join(cards)
        except Exception as e:
            print(f"Warning: entity cards failed ({e})", file=sys.stderr)

    # Stage 3: Haiku LLM reranking (optional)
    if rerank:
        top_candidates = candidates[:15]  # Send top 15 to Haiku (enriched by graph)
        haiku_result = rerank_with_haiku(
            query, top_candidates, n_results, collection=col,
            summary_context=summary_context,
            entity_card_context=entity_card_context,
        )
        if haiku_result:
            return haiku_result
        # Fallback: wrap BM25 results in same format
        fallback = [{k: v for k, v in c.items() if not k.startswith("_") or k == "_id"} for c in candidates[:n_results]]
        return {"answer": "", "results": fallback}

    clean = [{k: v for k, v in c.items() if not k.startswith("_") or k == "_id"} for c in candidates[:n_results]]
    return clean


def search_raw_messages(query, from_filter=None, days=None, n_results=10):
    """Search the raw per-message index. Returns individual messages with exact attribution.

    Uses semantic search + exact keyword matching for numbers/terms that
    embeddings miss. Best for: "who said X", "who first mentioned Y"
    """
    import chromadb
    import re as _re

    # Parse time from query
    time_days, clean_query = parse_time_from_query(query)
    if time_days and not days:
        days = time_days
        query = clean_query

    client = chromadb.PersistentClient(path=DB_PATH)
    try:
        col = client.get_collection(MESSAGES_COLLECTION)
    except Exception:
        return []  # Collection doesn't exist yet

    # Build where clause with optional wing/room scoping
    conditions = []
    if from_filter:
        conditions.append({"from": from_filter.lower()})
    wing = _detect_wing(query)
    if wing:
        conditions.append({"wing": wing})
        room = _detect_room(query, wing)
        if room:
            conditions.append({"room": room})

    where = None
    if len(conditions) == 1:
        where = conditions[0]
    elif len(conditions) > 1:
        where = {"$and": conditions}

    date_cutoff = None
    if days:
        date_cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")

    # Semantic search
    kwargs = {"query_texts": [query], "n_results": n_results * 3 if date_cutoff else n_results}
    if where:
        kwargs["where"] = where
    results = col.query(**kwargs)

    # Exact keyword search: find messages containing specific terms
    # (PR numbers, issue IDs, exact phrases that embeddings miss)
    exact_terms = _re.findall(r'\b(\d{4,5})\b', query)
    keyword_hits = {}
    if exact_terms:
        try:
            total = col.count()
            semantic_ids = set(results["ids"][0]) if results["ids"] else set()
            batch_size = 5000
            for offset in range(0, total, batch_size):
                batch = col.get(include=["documents", "metadatas"], limit=batch_size, offset=offset)
                for cid, doc, meta in zip(batch["ids"], batch["documents"], batch["metadatas"]):
                    doc_text = doc.lower()
                    if any(f"#{term}" in doc_text or f"/{term}" in doc_text
                           or f" {term}" in doc_text or f":{term}" in doc_text
                           for term in exact_terms):
                        keyword_hits[cid] = (doc, meta)
                        if cid not in semantic_ids:
                            results["ids"][0].append(cid)
                            results["documents"][0].append(doc)
                            results["metadatas"][0].append(meta)
                            results["distances"][0].append(0.3)  # high relevance
                            semantic_ids.add(cid)
        except Exception as e:
            print(f"Warning: keyword scan failed ({e})", file=sys.stderr)

    if not results["documents"] or not results["documents"][0]:
        return []

    messages = []
    for doc, meta, dist in zip(
        results["documents"][0], results["metadatas"][0], results["distances"][0]
    ):
        ts = meta.get("timestamp", "")
        if date_cutoff and ts < date_cutoff:
            continue

        # Boost score for exact keyword matches
        cid = f"msg_{meta.get('msg_id', '')}"
        score = 1 - dist
        if cid in keyword_hits:
            score = max(score, 0.8)  # exact match gets high score

        messages.append({
            "text": doc,
            "from": meta.get("from", ""),
            "timestamp": ts,
            "score": round(score, 4),
            "msg_id": meta.get("msg_id", ""),
        })

    # Sort by score, then by timestamp for ties
    messages.sort(key=lambda x: (-x["score"], x["timestamp"]))
    return messages[:n_results]


def detect_entity_query(query):
    """Detect if a query targets a specific entity. Returns entity_id or None."""
    import re
    # PR number: "PR 6312", "PR #6312", "#6312", "pull/6312"
    m = re.search(r'(?:PR\s*#?|pr\s*#?|pull/)(\d{4,5})', query)
    if m:
        return f"pr:{m.group(1)}"
    # Bare 4-5 digit number that looks like a PR
    m = re.search(r'#(\d{4,5})(?:\s|$)', query)
    if m:
        return f"pr:{m.group(1)}"
    # Issue: "issue 6382", "issue #6382"
    m = re.search(r'(?:issue\s*#?)(\d{4,5})', query, re.IGNORECASE)
    if m:
        return f"issue:{m.group(1)}"
    # Bare 4-5 digit number (no prefix) — check if it exists as a PR in entity index
    m = re.search(r'\b(\d{4,5})\b', query)
    if m:
        candidate_id = f"pr:{m.group(1)}"
        try:
            try:
                from .entities import EntityIndex as _EI
            except ImportError:
                from entities import EntityIndex as _EI
            _idx = _EI()
            if _idx.get_entity(candidate_id):
                _idx.close()
                return candidate_id
            # Also check as issue
            issue_id = f"issue:{m.group(1)}"
            if _idx.get_entity(issue_id):
                _idx.close()
                return issue_id
            _idx.close()
        except Exception:
            pass
    # Agent: "what did lee do", "lee's work"
    try:
        from .entities import AGENT_NAMES
    except ImportError:
        from entities import AGENT_NAMES
    for agent in AGENT_NAMES:
        if re.search(rf'\b{agent}\b', query.lower()):
            return f"agent:{agent}"
    return None


def search_entity_timeline(query, n_results=5):
    """Entity Timeline Mode: full-coverage recall for entity-specific queries.

    Instead of precision search (find best match), this fetches ALL chunks
    related to the detected entity, condenses them chronologically, and
    sends the full set to Haiku for timeline synthesis.

    Returns dict: {"answer": str, "results": list, "mode": "timeline"}
    Falls back to regular search() if no entity detected or entity not found.
    """
    import chromadb
    import anthropic

    entity_id = detect_entity_query(query)
    if not entity_id:
        return None  # Not an entity query — caller should use regular search()

    # Load entity index
    try:
        from .entities import EntityIndex
    except ImportError:
        from entities import EntityIndex
    idx = EntityIndex()
    entity = idx.get_entity(entity_id)
    if not entity:
        idx.close()
        return None  # Entity not in index

    # Get ChromaDB collection
    client = chromadb.PersistentClient(path=DB_PATH)
    col = client.get_collection(COLLECTION)

    # Build full timeline
    timeline = idx.build_entity_timeline(entity_id, col)
    idx.close()

    if not timeline:
        return None

    # Send to Haiku for synthesis
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        # No API key — return raw timeline as answer
        return {
            "answer": timeline,
            "results": [],
            "mode": "timeline",
            "entity": entity_id,
            "chunks_used": timeline.count("\n["),
        }

    system_prompt = (
        "You are a team memory recall system. Given a query and a full chronological "
        "timeline of all conversations about an entity, synthesize a CONCISE but COMPLETE answer.\n\n"
        "Format: one-line summary, then bullet timeline.\n"
        "Each bullet: date + agent + what happened.\n"
        "CRITICAL: You MUST mention EVERY unique agent who appears in the timeline. "
        "Do not skip any agent's contribution, even if minor.\n"
        "Max 15 bullets. Start with agents list.\n\n"
        "Respond ONLY with valid JSON, no markdown:\n"
        '{"answer": "summary\\n\\nAgents: a, b, c\\n\\n• date - agent: event"}'
    )

    user_msg = f"Query: {query}\n\n{timeline}"

    try:
        client_ai = anthropic.Anthropic(api_key=api_key)
        response = client_ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        data = json.loads(raw)
        return {
            "answer": data.get("answer", ""),
            "results": [],
            "mode": "timeline",
            "entity": entity_id,
            "chunks_used": timeline.count("\n["),
        }

    except Exception as e:
        print(f"Warning: Timeline Haiku call failed ({e})", file=sys.stderr)
        return {
            "answer": timeline,
            "results": [],
            "mode": "timeline",
        }


def search_memory(query_text):
    """Bridge-friendly wrapper. Parses filters from query, auto-detects entity timeline mode.

    Returns {"answer": str, "results": list[dict]}.
    """
    import re

    # Parse --agent, --from, --days flags
    agent_filter = None
    from_filter = None
    days = None
    query = query_text

    m = re.search(r'--agent\s+(\S+)', query)
    if m:
        agent_filter = m.group(1)
        query = query[:m.start()] + query[m.end():]

    m = re.search(r'--from\s+(\S+)', query)
    if m:
        from_filter = m.group(1)
        query = query[:m.start()] + query[m.end():]

    m = re.search(r'--days\s+(\d+)', query)
    if m:
        days = int(m.group(1))
        query = query[:m.start()] + query[m.end():]

    query = re.sub(r'\s+', ' ', query).strip()

    # Try Entity Timeline Mode first (for entity-specific queries)
    timeline_result = search_entity_timeline(query)
    if timeline_result:
        return timeline_result

    # Search raw messages for direct recall ("who said X", "who first mentioned Y")
    raw_msgs = search_raw_messages(query, from_filter=from_filter, days=days, n_results=5)

    # Standard search with rerank
    result = search(
        query,
        from_filter=from_filter,
        agent_filter=agent_filter,
        days=days,
        n_results=5,
        rerank=True,
    )

    # Merge raw message hits into the result for Haiku to consider
    if raw_msgs and isinstance(result, dict):
        top_raw = raw_msgs[:3]
        if top_raw:
            raw_summary = "\n".join(
                f"[{m['timestamp']}] {m['from']}: {m['text'][:200]}"
                for m in top_raw
            )
            existing_answer = result.get("answer", "")
            if existing_answer:
                result["answer"] = existing_answer + "\n\nDirect message matches:\n" + raw_summary
            else:
                result["answer"] = "Direct message matches:\n" + raw_summary
        result["raw_messages"] = raw_msgs

    return result


def pretty_print(results, query, answer=None):
    """Format results for terminal display."""
    if not results:
        print(f"No results for: {query}")
        return

    print(f"\n\033[1mQuery:\033[0m {query}")
    if answer:
        print(f"\033[1;32mAnswer:\033[0m {answer}")
    print(f"\033[90m{len(results)} results\033[0m\n")

    for i, r in enumerate(results):
        score_color = "\033[32m" if r["score"] > 0.5 else "\033[33m" if r["score"] > 0.3 else "\033[31m"
        agents = ", ".join(r["target_agents"]) if r["target_agents"] else "—"
        print(f"\033[1m[{i+1}]\033[0m {score_color}{r['score']:.2f}\033[0m  "
              f"\033[36m{r['from']}\033[0m → \033[35m{agents}\033[0m  "
              f"\033[90m{r['date']}  ({r['msg_count']} msgs)\033[0m")

        # Show text preview (first 3 lines, max 200 chars each)
        lines = r["text"].split("\n")
        for line in lines[:3]:
            text = line[:200] + "..." if len(line) > 200 else line
            print(f"    {text}")
        if len(lines) > 3:
            print(f"    \033[90m...({len(lines)-3} more lines)\033[0m")
        print()


def main():
    parser = argparse.ArgumentParser(description="Search team memory")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--from", dest="from_filter", help="Filter by sender (thinh|beasts)")
    parser.add_argument("--agent", help="Filter by target agent name")
    parser.add_argument("--days", type=int, help="Only last N days")
    parser.add_argument("--n", type=int, default=5, help="Number of results (default: 5)")
    parser.add_argument("--rerank", action="store_true", help="Use Haiku LLM reranking (needs ANTHROPIC_API_KEY)")
    parser.add_argument("--timeline", action="store_true", help="Entity Timeline Mode: full recall for entity queries")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print for terminal")
    parser.add_argument("--json", action="store_true", help="Force JSON output (default when piped)")
    args = parser.parse_args()

    # Try Entity Timeline Mode first if --timeline or auto-detect entity queries
    results = None
    if args.timeline:
        results = search_entity_timeline(args.query, n_results=args.n)

    if results is None:
        results = search(
            args.query,
            from_filter=args.from_filter,
            agent_filter=args.agent,
            days=args.days,
            n_results=args.n,
            rerank=args.rerank,
        )

    # Handle rerank dict output vs plain list
    answer = None
    if isinstance(results, dict):
        answer = results.get("answer", "")
        result_list = results.get("results", [])
    else:
        result_list = results

    # Pretty-print if --pretty or interactive terminal (not piped)
    if args.pretty or (not args.json and sys.stdout.isatty()):
        pretty_print(result_list, args.query, answer=answer)
    else:
        if isinstance(results, dict):
            json.dump(results, sys.stdout, indent=2)
        else:
            json.dump(results, sys.stdout, indent=2)
        print()


if __name__ == "__main__":
    main()
