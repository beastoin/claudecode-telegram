"""Behavior tests for indexer.py — covers gaps not in test_transcript.py.

Focuses on edge cases: FTS5 special-char handling, compound queries,
incremental reindex triggers, unicode search, and duration formatting.
"""
import json
import os
import sqlite3
import subprocess
import sys

import pytest


def _run_indexer(tmp_path, jsonl_name, entries, query, **extra_args):
    """Helper: write entries to JSONL, run indexer, return parsed JSON."""
    tmp = tmp_path / jsonl_name
    db = tmp_path / f"{jsonl_name}.db"
    with open(tmp, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    cmd = [
        sys.executable, "indexer.py", "transcript",
        "--jsonl", str(tmp), "--db", str(db), "--query", query,
    ]
    for k, v in extra_args.items():
        cmd.extend([f"--{k.replace('_', '-')}", str(v)])

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=os.getcwd())
    assert result.returncode == 0, f"indexer crashed: {result.stderr}"
    return json.loads(result.stdout), tmp, db


def _make_entry(etype, content, ts="2026-04-05T10:00:00Z", **extras):
    """Build a minimal transcript entry."""
    entry = {"type": etype, "timestamp": ts}
    if etype == "user":
        entry["message"] = {"role": "user", "content": content}
    elif etype == "assistant":
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        entry["message"] = {"role": "assistant", "content": content, "model": "claude-opus-4-6"}
    entry.update(extras)
    return entry


# ── FTS5 query sanitization ──────────────────────────────────────────────────

def test_fts5_special_chars_search(tmp_path):
    """Dots, colons, hyphens in search terms don't crash FTS5."""
    entries = [
        _make_entry("user", "Upgraded to v0.45.2 today"),
        _make_entry("assistant", "The file bridge.py was modified"),
        _make_entry("user", "Check http://localhost:8271/health", ts="2026-04-05T10:00:02Z"),
    ]
    d, _, _ = _run_indexer(tmp_path, "fts5-special.jsonl", entries, "search",
                           search="v0.45.2")
    assert d["total_results"] >= 1, f"Should find v0.45.2, got {d['total_results']} results"

    d2, _, _ = _run_indexer(tmp_path, "fts5-special2.jsonl", entries, "search",
                            search="bridge.py")
    assert d2["total_results"] >= 1, "Should find bridge.py"

    d3, _, _ = _run_indexer(tmp_path, "fts5-special3.jsonl", entries, "search",
                            search="http://localhost:8271")
    assert d3["total_results"] >= 1, "Should find URL with colons"


def test_fts5_passthrough_boolean_query(tmp_path):
    """AND/OR/NOT pass through as FTS5 boolean operators."""
    entries = [
        _make_entry("user", "There was an error with the timeout"),
        _make_entry("user", "The timeout was resolved", ts="2026-04-05T10:00:01Z"),
        _make_entry("user", "An error in the config file", ts="2026-04-05T10:00:02Z"),
    ]
    d, _, _ = _run_indexer(tmp_path, "fts5-bool.jsonl", entries, "search",
                           search="error AND timeout")
    # Only the first entry has both "error" AND "timeout"
    assert d["total_results"] >= 1, "Boolean AND should find matching entry"
    texts = [json.loads(e["raw_json"])["message"]["content"] for e in d["entries"]]
    assert any("error" in t and "timeout" in t for t in texts), \
        "AND query should return entries containing both terms"


def test_fts5_fallback_on_bad_syntax(tmp_path):
    """Broken FTS5 syntax (unclosed quote) doesn't crash — falls back to quoting."""
    entries = [
        _make_entry("user", 'The "broken query test'),
        _make_entry("assistant", "Response about broken things"),
    ]
    d, _, _ = _run_indexer(tmp_path, "fts5-bad.jsonl", entries, "search",
                           search='"broken')
    # Should not crash — returncode=0 is already asserted by _run_indexer
    assert isinstance(d["total_results"], int), "Should return valid result structure"


# ── Compound queries ─────────────────────────────────────────────────────────

def test_entries_plus_stats_compound_query(tmp_path):
    """`entries+stats` returns entries AND stats in one response."""
    entries = [
        _make_entry("user", "Hello world", ts="2026-04-05T10:00:00Z",
                     sessionId="s1", version="2.1.85"),
        _make_entry("assistant", "Hi there", ts="2026-04-05T10:05:00Z"),
        _make_entry("user", "Thanks", ts="2026-04-05T10:10:00Z"),
    ]
    d, _, _ = _run_indexer(tmp_path, "compound-es.jsonl", entries, "entries+stats")
    assert "entries" in d, "Should have entries key"
    assert "stats" in d, "Should have stats key"
    assert len(d["entries"]) == 3
    assert d["stats"]["version"] == "2.1.85"
    assert d["stats"]["model"] == "claude-opus-4-6"


def test_search_plus_stats_compound_query(tmp_path):
    """`search+stats` returns search results AND stats in one response."""
    entries = [
        _make_entry("user", "Fix the login bug", ts="2026-04-05T10:00:00Z",
                     sessionId="s1", version="2.1.85"),
        _make_entry("assistant", "I fixed the login bug in auth.py", ts="2026-04-05T10:05:00Z"),
        _make_entry("user", "Deploy it", ts="2026-04-05T10:10:00Z"),
    ]
    d, _, _ = _run_indexer(tmp_path, "compound-ss.jsonl", entries, "search+stats",
                           search="login")
    assert "entries" in d, "Should have entries key from search"
    assert "stats" in d, "Should have stats key"
    assert d["total_results"] >= 1, "Should find 'login'"
    assert d["stats"]["duration"] == "10m"


# ── Search sort order ────────────────────────────────────────────────────────

def test_search_sort_by_time(tmp_path):
    """--sort time returns results in chronological order, not BM25 rank."""
    entries = [
        _make_entry("user", "deploy the fix now", ts="2026-04-05T10:00:00Z"),
        _make_entry("user", "another task unrelated", ts="2026-04-05T10:01:00Z"),
        _make_entry("user", "deploy deploy deploy triple mention", ts="2026-04-05T10:02:00Z"),
        _make_entry("user", "final deploy step", ts="2026-04-05T10:03:00Z"),
    ]
    # By relevance, the triple-mention entry would rank first
    d_rel, _, _ = _run_indexer(tmp_path, "sort-rel.jsonl", entries, "search",
                               search="deploy", sort="relevance")
    # By time, entries should be in chronological order
    d_time, _, _ = _run_indexer(tmp_path, "sort-time.jsonl", entries, "search",
                                search="deploy", sort="time")

    assert d_time["total_results"] >= 3
    time_idxs = [e["idx"] for e in d_time["entries"]]
    assert time_idxs == sorted(time_idxs), \
        f"Time sort should be chronological: {time_idxs}"


# ── Incremental reindex triggers ─────────────────────────────────────────────

def test_incremental_reindex_on_file_shrink(tmp_path):
    """When JSONL file shrinks (truncation), full reindex triggers."""
    entries_full = [
        _make_entry("user", f"Message {i}", ts=f"2026-04-05T10:{i:02d}:00Z")
        for i in range(5)
    ]
    tmp = tmp_path / "shrink.jsonl"
    db = tmp_path / "shrink.db"
    with open(tmp, "w") as f:
        for e in entries_full:
            f.write(json.dumps(e) + "\n")

    # First index: 5 entries
    cmd = [sys.executable, "indexer.py", "transcript",
           "--jsonl", str(tmp), "--db", str(db), "--query", "entries"]
    r1 = subprocess.run(cmd, capture_output=True, text=True, cwd=os.getcwd())
    d1 = json.loads(r1.stdout)
    assert d1["total"] == 5

    # Truncate to 2 entries
    with open(tmp, "w") as f:
        for e in entries_full[:2]:
            f.write(json.dumps(e) + "\n")

    r2 = subprocess.run(cmd, capture_output=True, text=True, cwd=os.getcwd())
    d2 = json.loads(r2.stdout)
    assert d2["total"] == 2, f"After truncation should have 2 entries, got {d2['total']}"


def test_incremental_reindex_on_path_change(tmp_path):
    """Indexing a different JSONL path with same DB triggers full reindex."""
    entries_a = [
        _make_entry("user", "From file A"),
        _make_entry("user", "Also file A", ts="2026-04-05T10:01:00Z"),
    ]
    entries_b = [
        _make_entry("user", "From file B"),
    ]
    db = tmp_path / "shared.db"

    # Index file A
    path_a = tmp_path / "file_a.jsonl"
    with open(path_a, "w") as f:
        for e in entries_a:
            f.write(json.dumps(e) + "\n")
    cmd_a = [sys.executable, "indexer.py", "transcript",
             "--jsonl", str(path_a), "--db", str(db), "--query", "entries"]
    r1 = subprocess.run(cmd_a, capture_output=True, text=True, cwd=os.getcwd())
    d1 = json.loads(r1.stdout)
    assert d1["total"] == 2

    # Index file B with same DB
    path_b = tmp_path / "file_b.jsonl"
    with open(path_b, "w") as f:
        for e in entries_b:
            f.write(json.dumps(e) + "\n")
    cmd_b = [sys.executable, "indexer.py", "transcript",
             "--jsonl", str(path_b), "--db", str(db), "--query", "entries"]
    r2 = subprocess.run(cmd_b, capture_output=True, text=True, cwd=os.getcwd())
    d2 = json.loads(r2.stdout)
    assert d2["total"] == 1, f"Path change should reindex — expected 1, got {d2['total']}"


# ── Content extraction ───────────────────────────────────────────────────────

def test_thinking_block_extraction(tmp_path):
    """Thinking blocks in assistant content are extracted and searchable."""
    entries = [
        _make_entry("assistant", [
            {"type": "thinking", "thinking": "The user wants zephyr configuration details"},
            {"type": "text", "text": "Here are the settings."},
        ]),
    ]
    d, _, _ = _run_indexer(tmp_path, "thinking.jsonl", entries, "search",
                           search="zephyr")
    assert d["total_results"] >= 1, "Thinking block text should be searchable"


def test_unicode_search(tmp_path):
    """Unicode text near emoji is indexed and searchable; search doesn't crash."""
    entries = [
        _make_entry("user", "Deploy the 🚀 rocket feature", ts="2026-04-05T10:00:00Z"),
        _make_entry("assistant", "Deploying résumé handler für München", ts="2026-04-05T10:00:01Z"),
        _make_entry("user", "Check naïve implementation", ts="2026-04-05T10:00:02Z"),
    ]
    d, _, _ = _run_indexer(tmp_path, "unicode.jsonl", entries, "search",
                           search="rocket")
    assert d["total_results"] >= 1, "Should find text near emoji"

    d2, _, _ = _run_indexer(tmp_path, "unicode2.jsonl", entries, "search",
                            search="München")
    assert d2["total_results"] >= 1, "Should find accented text"

    # Searching for emoji itself doesn't crash (even if FTS5 can't match it)
    d3, _, _ = _run_indexer(tmp_path, "unicode3.jsonl", entries, "search",
                            search="🚀")
    assert isinstance(d3["total_results"], int), "Emoji search should not crash"


# ── Duration formatting ──────────────────────────────────────────────────────

def test_duration_days_calculation(tmp_path):
    """Stats spanning multiple days show days in duration (e.g. '1d 2h')."""
    entries = [
        _make_entry("user", "Start of session", ts="2026-04-05T08:00:00Z",
                     sessionId="s1", version="2.1.85"),
        _make_entry("assistant", "Working on it", ts="2026-04-06T10:30:00Z"),
    ]
    d, _, _ = _run_indexer(tmp_path, "duration-days.jsonl", entries, "stats")
    # 26h30m = 1d 2h (days format truncates minutes)
    assert d["duration"].startswith("1d"), \
        f"Should show days for 26h30m span, got '{d['duration']}'"
    assert "h" in d["duration"], "Should include hours component"


def test_duration_hours_calculation(tmp_path):
    """Stats spanning hours but not days show hours+minutes."""
    entries = [
        _make_entry("user", "Start", ts="2026-04-05T10:00:00Z"),
        _make_entry("assistant", "End", ts="2026-04-05T12:45:00Z"),
    ]
    d, _, _ = _run_indexer(tmp_path, "duration-hours.jsonl", entries, "stats")
    assert d["duration"] == "2h 45m", f"Expected '2h 45m', got '{d['duration']}'"


def test_duration_minutes_only(tmp_path):
    """Short sessions show minutes only."""
    entries = [
        _make_entry("user", "Quick question", ts="2026-04-05T10:00:00Z"),
        _make_entry("assistant", "Quick answer", ts="2026-04-05T10:07:00Z"),
    ]
    d, _, _ = _run_indexer(tmp_path, "duration-mins.jsonl", entries, "stats")
    assert d["duration"] == "7m", f"Expected '7m', got '{d['duration']}'"


# ── Empty / edge cases ───────────────────────────────────────────────────────

def test_empty_search_term_returns_empty(tmp_path):
    """Empty search string returns 0 results without crashing."""
    entries = [
        _make_entry("user", "Hello"),
        _make_entry("assistant", "World"),
    ]
    d, _, _ = _run_indexer(tmp_path, "empty-search.jsonl", entries, "search",
                           search="")
    assert d["total_results"] == 0, f"Empty search should return 0 results, got {d['total_results']}"
    assert d["entries"] == []


def test_stats_on_empty_db(tmp_path):
    """Stats on an empty file returns zeroed-out stats, not crash."""
    d, _, _ = _run_indexer(tmp_path, "empty-stats.jsonl", [], "stats")
    assert d["n_user"] == 0
    assert d["n_tool"] == 0
    assert d["duration"] == ""
    assert d["model"] == ""


# ── Audit-driven additions ──────────────────────────────────────────────────


def test_incremental_append(tmp_path):
    """Appending lines to JSONL and re-indexing adds new entries without duplication."""
    entries_initial = [
        _make_entry("user", "First message", ts="2026-04-05T10:00:00Z"),
        _make_entry("assistant", "First reply", ts="2026-04-05T10:01:00Z"),
    ]
    jsonl = tmp_path / "append.jsonl"
    db = tmp_path / "append.db"
    with open(jsonl, "w") as f:
        for e in entries_initial:
            f.write(json.dumps(e) + "\n")

    cmd = [sys.executable, "indexer.py", "transcript",
           "--jsonl", str(jsonl), "--db", str(db), "--query", "entries"]
    r1 = subprocess.run(cmd, capture_output=True, text=True, cwd=os.getcwd())
    d1 = json.loads(r1.stdout)
    assert d1["total"] == 2, f"Initial index: expected 2, got {d1['total']}"

    # Append a third entry
    with open(jsonl, "a") as f:
        f.write(json.dumps(_make_entry("user", "Appended message", ts="2026-04-05T10:02:00Z")) + "\n")

    r2 = subprocess.run(cmd, capture_output=True, text=True, cwd=os.getcwd())
    d2 = json.loads(r2.stdout)
    assert d2["total"] == 3, f"After append: expected 3, got {d2['total']}"

    # Verify no duplicates: search for original content returns exactly 1
    r3 = subprocess.run([
        sys.executable, "indexer.py", "transcript",
        "--jsonl", str(jsonl), "--db", str(db),
        "--query", "search", "--search", "First message",
    ], capture_output=True, text=True, cwd=os.getcwd())
    d3 = json.loads(r3.stdout)
    assert d3["total_results"] == 1, f"Original entry duplicated: got {d3['total_results']}"


def test_pagination_default_last_page(tmp_path):
    """Without --page, indexer returns the last page by default."""
    # Create 12 entries; with per_page=5 that's 3 pages (5, 5, 2)
    entries = [
        _make_entry("user", f"Message number {i}", ts=f"2026-04-05T10:{i:02d}:00Z")
        for i in range(12)
    ]
    jsonl = tmp_path / "pagination.jsonl"
    db = tmp_path / "pagination.db"
    with open(jsonl, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    # No --page → should get page 3 (last page) with 2 entries
    cmd = [sys.executable, "indexer.py", "transcript",
           "--jsonl", str(jsonl), "--db", str(db),
           "--query", "entries", "--per-page", "5"]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=os.getcwd())
    d = json.loads(r.stdout)
    assert d["total"] == 12
    assert d["total_pages"] == 3
    assert d["page"] == 3, f"Default should be last page, got page {d['page']}"
    assert len(d["entries"]) == 2, f"Last page should have 2 entries, got {len(d['entries'])}"

    # Explicit --page 1 → should get first 5 entries
    cmd_p1 = cmd + ["--page", "1"]
    r_p1 = subprocess.run(cmd_p1, capture_output=True, text=True, cwd=os.getcwd())
    d_p1 = json.loads(r_p1.stdout)
    assert d_p1["page"] == 1
    assert len(d_p1["entries"]) == 5


def test_filter_prompts(tmp_path):
    """--filter prompts returns only user text messages, not tool_use or system."""
    entries = [
        _make_entry("user", "Real user question"),
        _make_entry("assistant", "Bot response"),
        _make_entry("user", "Another user prompt", ts="2026-04-05T10:01:00Z"),
        # tool_use content — structured, not plain text
        {"type": "assistant", "timestamp": "2026-04-05T10:02:00Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"cmd": "ls"}},
         ]}},
        # tool_result — from system
        {"type": "tool_result", "timestamp": "2026-04-05T10:03:00Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1", "content": "file.txt"},
         ]}},
    ]
    jsonl = tmp_path / "prompts.jsonl"
    db = tmp_path / "prompts.db"
    with open(jsonl, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    cmd = [sys.executable, "indexer.py", "transcript",
           "--jsonl", str(jsonl), "--db", str(db),
           "--query", "entries", "--filter", "prompts"]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=os.getcwd())
    d = json.loads(r.stdout)

    # Should only get the 2 plain user prompts
    assert d["total"] == 2, f"Filter prompts: expected 2, got {d['total']}"
    for e in d["entries"]:
        raw = json.loads(e["raw_json"])
        assert raw["type"] == "user", f"Non-user entry leaked through filter: {raw['type']}"
        assert isinstance(raw["message"]["content"], str), \
            f"Structured content leaked through filter: {raw['message']['content']}"


def test_missing_file_graceful(tmp_path):
    """Running indexer on a nonexistent JSONL returns empty result, no crash."""
    db = tmp_path / "missing.db"
    nonexistent = tmp_path / "does_not_exist.jsonl"

    # entries query on missing file
    cmd = [sys.executable, "indexer.py", "transcript",
           "--jsonl", str(nonexistent), "--db", str(db), "--query", "entries"]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=os.getcwd())
    assert r.returncode == 0, f"Should not crash on missing file: {r.stderr}"
    d = json.loads(r.stdout)
    assert d["total"] == 0
    assert d["entries"] == []

    # stats query on missing file
    cmd_stats = [sys.executable, "indexer.py", "transcript",
                 "--jsonl", str(nonexistent), "--db", str(db), "--query", "stats"]
    r_stats = subprocess.run(cmd_stats, capture_output=True, text=True, cwd=os.getcwd())
    assert r_stats.returncode == 0, f"Stats should not crash on missing file: {r_stats.stderr}"
    d_stats = json.loads(r_stats.stdout)
    assert d_stats["n_user"] == 0

    # search query on missing file
    cmd_search = [sys.executable, "indexer.py", "transcript",
                  "--jsonl", str(nonexistent), "--db", str(db),
                  "--query", "search", "--search", "anything"]
    r_search = subprocess.run(cmd_search, capture_output=True, text=True, cwd=os.getcwd())
    assert r_search.returncode == 0, f"Search should not crash on missing file: {r_search.stderr}"
    d_search = json.loads(r_search.stdout)
    assert d_search["total_results"] == 0
