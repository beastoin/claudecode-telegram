"""Behavior tests for tools/review.py — PR review page generator.

Tests the cache layer (PRCache), diff parser (parse_patch), and HTML
generation (generate_html) without any GitHub API calls.
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tools'))
from review import PRCache, parse_patch, generate_html


# ── PRCache tests ────────────────────────────────────────────────────────────


def test_cache_create_tables(tmp_path):
    """Fresh PRCache creates all 6 required tables."""
    db = tmp_path / "cache.db"
    cache = PRCache(str(db))
    tables = {r[0] for r in cache.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    cache.close()

    expected = {"pr_meta", "pr_files", "pr_commits", "pr_comments", "pr_reviews", "user_profiles"}
    assert expected.issubset(tables), f"Missing tables: {expected - tables}"


def test_cache_meta_round_trip(tmp_path):
    """set_meta then get_meta returns identical data and correct counts."""
    cache = PRCache(str(tmp_path / "cache.db"))
    meta = {"title": "Fix bug", "head_sha": "abc123", "user": "alice"}
    cache.set_meta("owner/repo/42", meta, comment_count=5, review_count=2, commit_count=3)

    result = cache.get_meta("owner/repo/42")
    cache.close()

    assert result is not None
    assert result["data"] == meta
    assert result["head_sha"] == "abc123"
    assert result["comment_count"] == 5
    assert result["review_count"] == 2
    assert result["commit_count"] == 3


def test_cache_meta_missing_returns_none(tmp_path):
    """get_meta for a nonexistent PR key returns None."""
    cache = PRCache(str(tmp_path / "cache.db"))
    assert cache.get_meta("nonexistent/repo/999") is None
    cache.close()


def test_cache_files_round_trip(tmp_path):
    """set_files / get_files returns data for matching head_sha, None for different sha."""
    cache = PRCache(str(tmp_path / "cache.db"))
    files = [{"filename": "main.py", "patch": "@@ -1 +1 @@\n-old\n+new"}]
    cache.set_files("owner/repo/42", "sha_v1", files)

    assert cache.get_files("owner/repo/42", "sha_v1") == files
    assert cache.get_files("owner/repo/42", "sha_v2") is None
    cache.close()


def test_cache_commits_round_trip(tmp_path):
    """set_commit for multiple shas, get_commits returns all as {sha: data}."""
    cache = PRCache(str(tmp_path / "cache.db"))
    c1 = {"message": "first commit", "sha": "aaa"}
    c2 = {"message": "second commit", "sha": "bbb"}
    cache.set_commit("owner/repo/42", "aaa", c1)
    cache.set_commit("owner/repo/42", "bbb", c2)

    commits = cache.get_commits("owner/repo/42")
    cache.close()

    assert len(commits) == 2
    assert commits["aaa"] == c1
    assert commits["bbb"] == c2


def test_cache_comments_round_trip(tmp_path):
    """set_comments stores a list, get_comments returns in order by id."""
    cache = PRCache(str(tmp_path / "cache.db"))
    comments = [
        {"id": 30, "body": "third"},
        {"id": 10, "body": "first"},
        {"id": 20, "body": "second"},
    ]
    cache.set_comments("owner/repo/42", comments)
    result = cache.get_comments("owner/repo/42")
    cache.close()

    # Should be ordered by id (10, 20, 30), not insertion order
    assert [c["id"] for c in result] == [10, 20, 30]
    assert result[0]["body"] == "first"


def test_cache_reviews_round_trip(tmp_path):
    """set_reviews / get_reviews stores and retrieves review data."""
    cache = PRCache(str(tmp_path / "cache.db"))
    reviews = [
        {"id": 1, "state": "APPROVED", "body": "LGTM"},
        {"id": 2, "state": "CHANGES_REQUESTED", "body": "Needs fix"},
    ]
    cache.set_reviews("owner/repo/42", reviews)
    result = cache.get_reviews("owner/repo/42")
    cache.close()

    assert len(result) == 2
    assert result[0]["state"] == "APPROVED"
    assert result[1]["body"] == "Needs fix"


def test_cache_user_profile_ttl(tmp_path):
    """Profile is returned when fresh, returns None after TTL expires."""
    cache = PRCache(str(tmp_path / "cache.db"))
    profile = {"name": "Alice", "followers": 100, "public_repos": 5}
    cache.set_user_profile("alice", profile)

    # Immediately retrievable
    assert cache.get_user_profile("alice") == profile

    # Expire by backdating fetched_at past the 24h TTL
    cache.conn.execute(
        "UPDATE user_profiles SET fetched_at = ? WHERE username = ?",
        (time.time() - 86401, "alice"))
    cache.conn.commit()

    assert cache.get_user_profile("alice") is None
    cache.close()


def test_cache_clear_pr(tmp_path):
    """clear_pr removes all data for that PR but preserves other PRs."""
    cache = PRCache(str(tmp_path / "cache.db"))

    # Store data for two PRs
    cache.set_meta("owner/repo/1", {"title": "PR1", "head_sha": "a"}, 1, 1, 1)
    cache.set_meta("owner/repo/2", {"title": "PR2", "head_sha": "b"}, 2, 2, 2)
    cache.set_files("owner/repo/1", "a", [{"filename": "f1.py"}])
    cache.set_files("owner/repo/2", "b", [{"filename": "f2.py"}])
    cache.set_commit("owner/repo/1", "c1", {"msg": "commit1"})
    cache.set_comments("owner/repo/1", [{"id": 1, "body": "hi"}])
    cache.set_reviews("owner/repo/1", [{"id": 1, "state": "APPROVED"}])

    # Clear PR 1
    cache.clear_pr("owner/repo/1")

    # PR 1 is gone
    assert cache.get_meta("owner/repo/1") is None
    assert cache.get_files("owner/repo/1", "a") is None
    assert cache.get_commits("owner/repo/1") == {}
    assert cache.get_comments("owner/repo/1") == []
    assert cache.get_reviews("owner/repo/1") == []

    # PR 2 survives
    assert cache.get_meta("owner/repo/2") is not None
    assert cache.get_files("owner/repo/2", "b") is not None
    cache.close()


def test_cache_meta_update_replaces(tmp_path):
    """set_meta on the same PR key replaces old data."""
    cache = PRCache(str(tmp_path / "cache.db"))
    cache.set_meta("owner/repo/1", {"title": "old", "head_sha": "old_sha"}, 1, 0, 0)
    cache.set_meta("owner/repo/1", {"title": "new", "head_sha": "new_sha"}, 5, 3, 2)

    result = cache.get_meta("owner/repo/1")
    cache.close()

    assert result["data"]["title"] == "new"
    assert result["head_sha"] == "new_sha"
    assert result["comment_count"] == 5


# ── parse_patch tests ────────────────────────────────────────────────────────


def test_parse_patch_empty():
    """parse_patch(None) and parse_patch('') both return empty list."""
    assert parse_patch(None) == []
    assert parse_patch("") == []


def test_parse_patch_single_hunk():
    """Standard unified diff with adds, dels, and context lines."""
    patch = (
        "@@ -10,4 +10,5 @@\n"
        " context line\n"
        "-removed line\n"
        "+added line\n"
        "+another add\n"
        " more context"
    )
    hunks = parse_patch(patch)

    assert len(hunks) == 1
    h = hunks[0]
    assert h['old_start'] == 10
    assert h['old_count'] == 4
    assert h['new_start'] == 10
    assert h['new_count'] == 5
    assert len(h['lines']) == 5
    assert h['lines'][0] == ('ctx', 'context line')
    assert h['lines'][1] == ('del', 'removed line')
    assert h['lines'][2] == ('add', 'added line')
    assert h['lines'][3] == ('add', 'another add')
    assert h['lines'][4] == ('ctx', 'more context')


def test_parse_patch_multi_hunk():
    """Patch with two @@ sections produces two hunks."""
    patch = (
        "@@ -1,3 +1,3 @@\n"
        " a\n"
        "-b\n"
        "+c\n"
        "@@ -20,2 +20,3 @@\n"
        " x\n"
        "+y\n"
        "+z"
    )
    hunks = parse_patch(patch)

    assert len(hunks) == 2
    assert hunks[0]['old_start'] == 1
    assert hunks[0]['new_start'] == 1
    assert len(hunks[0]['lines']) == 3

    assert hunks[1]['old_start'] == 20
    assert hunks[1]['new_start'] == 20
    assert len(hunks[1]['lines']) == 3


def test_parse_patch_no_count():
    """@@ -1 +1 @@ (no comma count) defaults count to 1."""
    patch = "@@ -1 +1 @@\n-old\n+new"
    hunks = parse_patch(patch)

    assert len(hunks) == 1
    assert hunks[0]['old_count'] == 1
    assert hunks[0]['new_count'] == 1
    assert hunks[0]['lines'] == [('del', 'old'), ('add', 'new')]


def test_parse_patch_context_after_header():
    """Function name after @@ is captured in the context field."""
    patch = "@@ -10,5 +10,7 @@ def my_function():\n context"
    hunks = parse_patch(patch)

    assert len(hunks) == 1
    assert hunks[0]['context'] == "def my_function():"


def test_parse_patch_only_adds():
    """Patch with only additions (new file) parses correctly."""
    patch = "@@ -0,0 +1,3 @@\n+line1\n+line2\n+line3"
    hunks = parse_patch(patch)

    assert len(hunks) == 1
    assert hunks[0]['old_start'] == 0
    assert hunks[0]['old_count'] == 0
    assert all(t == 'add' for t, _ in hunks[0]['lines'])


def test_parse_patch_only_deletions():
    """Patch with only deletions (deleted file) parses correctly."""
    patch = "@@ -1,2 +0,0 @@\n-line1\n-line2"
    hunks = parse_patch(patch)

    assert len(hunks) == 1
    assert hunks[0]['new_count'] == 0
    assert all(t == 'del' for t, _ in hunks[0]['lines'])


# ── generate_html tests ─────────────────────────────────────────────────────


# Minimal PR metadata for generate_html tests
_MINI_META = {
    "title": "Fix critical alignment bug",
    "body": "This PR fixes the alignment in the header component.",
    "user": "alice",
    "head_sha": "abc123",
    "state": "open",
    "merged": False,
    "base_ref": "main",
    "head_ref": "fix-alignment",
    "additions": 10,
    "deletions": 3,
    "mergeable_state": "clean",
    "labels": [],
    "checks": [],
}

# Minimal file list
_MINI_FILES = [
    {
        "filename": "src/components/Header.tsx",
        "status": "modified",
        "additions": 7,
        "deletions": 2,
        "patch": "@@ -15,4 +15,9 @@ export function Header() {\n"
                 " return (\n"
                 "-    <div className=\"header\">\n"
                 "+    <div className=\"header aligned\">\n"
                 "+      <Logo />\n"
                 " );"
    },
    {
        "filename": "src/styles/main.css",
        "status": "modified",
        "additions": 3,
        "deletions": 1,
        "patch": "@@ -1,2 +1,4 @@\n .header { display: flex; }\n+.header.aligned { align-items: center; }\n+.logo { margin-right: 8px; }"
    },
]


def test_generate_html_contains_pr_title():
    """HTML output includes the PR title and number in the <title> tag."""
    output = generate_html(_MINI_META, _MINI_FILES, 42, "owner", "repo")

    assert "PR #42" in output
    assert "Fix critical alignment bug" in output
    assert "<title>PR #42: Fix critical alignment bug</title>" in output


def test_generate_html_file_sidebar():
    """HTML output contains filenames from the files list."""
    output = generate_html(_MINI_META, _MINI_FILES, 42, "owner", "repo")

    assert "Header.tsx" in output
    assert "main.css" in output
    # Full paths should also appear (in data attributes)
    assert "src/components/Header.tsx" in output
    assert "src/styles/main.css" in output


def test_generate_html_diff_lines():
    """HTML output contains actual diff content (added and deleted lines)."""
    output = generate_html(_MINI_META, _MINI_FILES, 42, "owner", "repo")

    # Diff lines appear inside <td class="code"> tags with HTML-escaped content
    # The patch has: -    <div className="header"> and +    <div className="header aligned">
    assert 'line-del' in output, 'Should contain deletion line markers'
    assert 'line-add' in output, 'Should contain addition line markers'
    # Content from the actual patch (Logo is in an added line)
    assert 'Logo' in output


def test_generate_html_highlight_comment():
    """When highlight_comment_id is set, HTML includes scroll-to-comment JS."""
    # In real data, fetch_pr_comments extracts user.login via jq, so 'user' is a string
    comments = [
        {"id": 7777, "user": "bob", "body": "Looks good!",
         "created_at": "2026-01-01T00:00:00Z", "html_url": ""},
    ]

    output = generate_html(
        _MINI_META, _MINI_FILES, 42, "owner", "repo",
        comments=comments, highlight_comment_id="7777")

    assert "disc-comment-7777" in output
    assert "disc-highlight" in output
    # Scroll-to JS
    assert "switchTab" in output
    assert "scrollIntoView" in output


def test_generate_html_no_files():
    """generate_html works with empty file list (no crash)."""
    output = generate_html(_MINI_META, [], 1, "owner", "repo")
    assert "PR #1" in output
    assert "<title>" in output


def test_generate_html_pr_description_in_body():
    """PR body text appears in the discussion section."""
    output = generate_html(_MINI_META, _MINI_FILES, 42, "owner", "repo")
    assert "alignment in the header component" in output


def test_generate_html_review_state():
    """Reviews with APPROVED/CHANGES_REQUESTED state appear in HTML."""
    reviews = [
        {"id": 1, "user": "reviewer", "body": "Ship it!",
         "state": "APPROVED", "submitted_at": "2026-01-02T00:00:00Z",
         "html_url": ""},
    ]
    output = generate_html(_MINI_META, _MINI_FILES, 42, "owner", "repo",
                           reviews=reviews)
    # State is lowercased in output (state_label = state.lower())
    assert "approved" in output
    assert "Ship it!" in output


def test_generate_html_commit_diffs():
    """When commits have files with patches, commit diffs appear in HTML."""
    commits = [
        {
            "sha": "abc123full",
            "message": "Initial fix\n\nDetailed description here.",
            "author": "Alice",
            "author_login": "alice",
            "date": "2026-01-01T00:00:00Z",
            "files": [
                {
                    "filename": "fix.py",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 1,
                    "patch": "@@ -1,1 +1,1 @@\n-old_code()\n+new_code()"
                }
            ]
        }
    ]
    output = generate_html(_MINI_META, _MINI_FILES, 42, "owner", "repo",
                           commits=commits)
    assert "abc123" in output  # Short SHA
    assert "Initial fix" in output
    assert "fix.py" in output
