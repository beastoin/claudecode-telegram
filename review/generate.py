#!/usr/bin/env python3
"""Standalone PR review HTML generator.

Fetches PR data from GitHub via `gh` CLI, caches results in SQLite,
and generates a self-contained HTML review page.

Usage:
    python generate.py <pr_url_or_number> [--fresh] [--no-serve]
    python generate.py https://github.com/owner/repo/pull/123
    python generate.py 123   # defaults to BasedHardware/omi
"""
from __future__ import annotations

import base64
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CACHE_DB = os.environ.get("PR_CACHE_DB", "/tmp/pr-review-cache.db")
USER_PROFILE_TTL = 86400

_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
_REVIEW_CSS = (_TEMPLATE_DIR / "review.css").read_text() if (_TEMPLATE_DIR / "review.css").exists() else ""
_REVIEW_JS = (_TEMPLATE_DIR / "review.js").read_text() if (_TEMPLATE_DIR / "review.js").exists() else ""
_REVIEW_PAGE_TPL = (
    (_TEMPLATE_DIR / "review_page.html").read_text().replace("__CSS__", _REVIEW_CSS).replace("__JS__", _REVIEW_JS)
    if (_TEMPLATE_DIR / "review_page.html").exists()
    else ""
)
_REVIEW_SCHEMA = (
    (_TEMPLATE_DIR / "review_schema.sql").read_text() if (_TEMPLATE_DIR / "review_schema.sql").exists() else ""
)


# ---------------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------------


def parse_patch(patch_text: str | None) -> list[dict[str, object]]:
    """Parse a unified diff patch into a list of hunks."""
    if not patch_text:
        return []
    hunks: list[dict[str, object]] = []
    current_hunk: dict[str, object] | None = None
    for line in patch_text.split("\n"):
        if line.startswith("@@"):
            if current_hunk:
                hunks.append(current_hunk)
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)", line)
            if m:
                current_hunk = {
                    "header": line,
                    "old_start": int(m.group(1)),
                    "old_count": int(m.group(2) or 1),
                    "new_start": int(m.group(3)),
                    "new_count": int(m.group(4) or 1),
                    "context": m.group(5),
                    "lines": [],
                }
            else:
                current_hunk = {
                    "header": line,
                    "old_start": 0,
                    "old_count": 0,
                    "new_start": 0,
                    "new_count": 0,
                    "context": "",
                    "lines": [],
                }
        elif current_hunk is not None:
            lines_list = current_hunk["lines"]
            assert isinstance(lines_list, list)
            if line.startswith("+"):
                lines_list.append(("add", line[1:]))
            elif line.startswith("-"):
                lines_list.append(("del", line[1:]))
            else:
                lines_list.append(("ctx", line[1:] if line.startswith(" ") else line))
    if current_hunk:
        hunks.append(current_hunk)
    return hunks


# ---------------------------------------------------------------------------
# Logging (simple print-based for standalone use)
# ---------------------------------------------------------------------------


def _log(level: str, tag: str, msg: str) -> None:
    print(f"[{level}] [{tag}] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------


def _gh_api(endpoint: str, jq: str, *, paginate: bool = True, timeout: int = 30) -> list[dict[str, object]]:
    """Run gh api and parse JSON line output. Returns [] on error."""
    cmd = ["gh", "api", endpoint]
    if paginate:
        cmd.append("--paginate")
    cmd.extend(["--jq", jq])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            _log("WARN", "pr-review", f"gh api {endpoint} returned {r.returncode}: {r.stderr.strip()[:200]}")
            return []
        return [json.loads(line) for line in r.stdout.strip().split("\n") if line.strip()]
    except Exception as e:
        _log("WARN", "pr-review", f"gh api {endpoint} failed: {e}")
        return []


def _gh_api_call(args: list[str], *, input_data: str | None = None, timeout: int = 60) -> tuple[bool, str]:
    """Run gh api with arbitrary args. Returns (success, output)."""
    try:
        kw: dict[str, object] = {"capture_output": True, "text": True, "timeout": timeout}
        if input_data is not None:
            kw["input"] = input_data
        r = subprocess.run(["gh", "api", *args], **kw)
        if r.returncode != 0:
            return False, r.stderr.strip() or r.stdout.strip()
        return True, r.stdout
    except subprocess.TimeoutExpired:
        return False, "timeout"


# ---------------------------------------------------------------------------
# PR Cache (SQLite)
# ---------------------------------------------------------------------------


class PRCache:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or CACHE_DB
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_REVIEW_SCHEMA)

    def clear_pr(self, pr_key: str) -> None:
        for table in ("pr_meta", "pr_files", "pr_commits", "pr_comments", "pr_reviews"):
            self.conn.execute(f"DELETE FROM {table} WHERE pr_key=?", (pr_key,))
        self.conn.commit()

    def get_meta(self, pr_key: str) -> dict[str, object] | None:
        row = self.conn.execute(
            "SELECT data, head_sha, comment_count, review_count, commit_count FROM pr_meta WHERE pr_key=?", (pr_key,)
        ).fetchone()
        if not row:
            return None
        return {
            "data": json.loads(row[0]),
            "head_sha": row[1],
            "comment_count": row[2],
            "review_count": row[3],
            "commit_count": row[4],
        }

    def set_meta(
        self, pr_key: str, meta: dict[str, object], comment_count: int, review_count: int, commit_count: int
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO pr_meta VALUES (?,?,?,?,?,?,?)",
            (
                pr_key,
                json.dumps(meta),
                meta.get("head_sha", ""),
                comment_count,
                review_count,
                commit_count,
                time.time(),
            ),
        )
        self.conn.commit()

    def get_files(self, pr_key: str, head_sha: str) -> list[dict[str, object]] | None:
        row = self.conn.execute(
            "SELECT data FROM pr_files WHERE pr_key=? AND head_sha=?", (pr_key, head_sha)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def set_files(self, pr_key: str, head_sha: str, files: list[dict[str, object]]) -> None:
        self.conn.execute("INSERT OR REPLACE INTO pr_files VALUES (?,?,?)", (pr_key, head_sha, json.dumps(files)))
        self.conn.commit()

    def get_commits(self, pr_key: str) -> dict[str, dict[str, object]]:
        rows = self.conn.execute("SELECT sha, data FROM pr_commits WHERE pr_key=?", (pr_key,)).fetchall()
        return {r[0]: json.loads(r[1]) for r in rows}

    def set_commit(self, pr_key: str, sha: str, commit_data: dict[str, object]) -> None:
        self.conn.execute("INSERT OR REPLACE INTO pr_commits VALUES (?,?,?)", (pr_key, sha, json.dumps(commit_data)))
        self.conn.commit()

    def _get_id_list(self, table: str, pr_key: str) -> list[dict[str, object]]:
        rows = self.conn.execute(f"SELECT data FROM {table} WHERE pr_key=? ORDER BY id", (pr_key,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def _set_id_list(self, table: str, pr_key: str, items: list[dict[str, object]]) -> None:
        self.conn.execute(f"DELETE FROM {table} WHERE pr_key=?", (pr_key,))
        for item in items:
            self.conn.execute(
                f"INSERT OR REPLACE INTO {table} VALUES (?,?,?)", (pr_key, item.get("id", 0), json.dumps(item))
            )
        self.conn.commit()

    def get_user_profile(self, username: str) -> dict[str, object] | None:
        row = self.conn.execute("SELECT data, fetched_at FROM user_profiles WHERE username=?", (username,)).fetchone()
        if not row:
            return None
        if time.time() - row[1] > USER_PROFILE_TTL:
            return None
        return json.loads(row[0])

    def set_user_profile(self, username: str, profile: dict[str, object]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO user_profiles VALUES (?,?,?)", (username, json.dumps(profile), time.time())
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------------------
# PR Data Fetching
# ---------------------------------------------------------------------------


def fetch_user_info(usernames: Iterable[str]) -> dict[str, dict[str, object]]:
    profiles: dict[str, dict[str, object]] = {}
    for username in set(usernames):
        if not username:
            continue
        items = _gh_api(
            f"users/{username}",
            "{login: .login, name: .name, followers: .followers, "
            "public_repos: .public_repos, created_at: .created_at, "
            "avatar_url: .avatar_url, bio: .bio}",
            paginate=False,
            timeout=10,
        )
        if items:
            profiles[username] = items[0]
    return profiles


def fetch_pr_reviews(owner: str, repo: str, pr_num: int) -> list[dict[str, object]]:
    return _gh_api(
        f"repos/{owner}/{repo}/pulls/{pr_num}/reviews",
        ".[] | {id: .id, body: .body, user: .user.login, "
        "state: .state, submitted_at: .submitted_at, html_url: .html_url}",
    )


def fetch_pr_comments(owner: str, repo: str, pr_num: int) -> list[dict[str, object]]:
    issue = _gh_api(
        f"repos/{owner}/{repo}/issues/{pr_num}/comments",
        ".[] | {id: .id, body: .body, user: .user.login, "
        "created_at: .created_at, updated_at: .updated_at, "
        'html_url: .html_url, type: "issue"}',
    )
    review = _gh_api(
        f"repos/{owner}/{repo}/pulls/{pr_num}/comments",
        ".[] | {id: .id, body: .body, user: .user.login, "
        "created_at: .created_at, path: .path, line: .line, "
        "original_line: .original_line, side: .side, "
        'diff_hunk: .diff_hunk, html_url: .html_url, type: "review"}',
    )
    return sorted(issue + review, key=lambda c: c.get("created_at", ""))


def _fetch_commit_list(owner: str, repo: str, pr_num: int) -> list[dict[str, object]]:
    return [
        c
        for c in _gh_api(
            f"repos/{owner}/{repo}/pulls/{pr_num}/commits",
            ".[] | {sha: .sha, message: .commit.message, "
            "author: .commit.author.name, author_login: .author.login, "
            "date: .commit.author.date}",
        )
        if "sha" in c
    ]


def _fetch_commit_files_single(owner: str, repo: str, c: dict[str, object]) -> None:
    items = _gh_api(
        f"repos/{owner}/{repo}/commits/{c['sha']}",
        "{files: [.files[] | {filename: .filename, status: .status, "
        "additions: .additions, deletions: .deletions, patch: .patch}]}",
        paginate=False,
    )
    c["files"] = items[0].get("files", []) if items else []


def fetch_pr_meta(owner: str, repo: str, pr_num: int) -> dict[str, object]:
    items = _gh_api(
        f"repos/{owner}/{repo}/pulls/{pr_num}",
        "{title: .title, body: .body, user: .user.login, "
        "changed_files: .changed_files, additions: .additions, "
        "deletions: .deletions, commits: .commits, "
        "base: .base.ref, head: .head.ref, html_url: .html_url, "
        "head_sha: .head.sha, base_sha: .base.sha, "
        "mergeable: .mergeable, mergeable_state: .mergeable_state, "
        "merged: .merged, state: .state, draft: .draft}",
        paginate=False,
    )
    if not items:
        raise RuntimeError(f"PR #{pr_num} not found or API error")
    meta = items[0]
    head_sha = meta.get("head_sha", "")
    if head_sha:
        checks = _gh_api(
            f"repos/{owner}/{repo}/commits/{head_sha}/check-runs",
            "[.check_runs[] | {name: .name, status: .status, conclusion: .conclusion}]",
            paginate=False,
            timeout=15,
        )
        meta["checks"] = checks[0] if checks and isinstance(checks[0], list) else checks or []
    return meta


def fetch_pr_files(owner: str, repo: str, pr_num: int) -> list[dict[str, object]]:
    files = _gh_api(
        f"repos/{owner}/{repo}/pulls/{pr_num}/files",
        ".[] | {filename: .filename, status: .status, additions: .additions, deletions: .deletions, patch: .patch}",
        timeout=60,
    )
    missing = [f for f in files if not f.get("patch") and f.get("additions", 0) > 0]
    if missing:
        print(f"    Fetching full diff for {len(missing)} large files...")
        full_diff = _fetch_full_diff(owner, repo, pr_num)
        if full_diff:
            diff_patches = _parse_full_diff(full_diff)
            for f in files:
                if not f.get("patch") and f["filename"] in diff_patches:
                    f["patch"] = diff_patches[f["filename"]]
    return files


def _fetch_full_diff(owner: str, repo: str, pr_num: int) -> str | None:
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/pulls/{pr_num}", "-H", "Accept: application/vnd.github.v3.diff"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return r.stdout if r.returncode == 0 else None
    except subprocess.TimeoutExpired:
        _log("WARN", "pr-review", "Full diff fetch timed out")
        return None


def _parse_full_diff(diff_text: str) -> dict[str, str]:
    patches: dict[str, str] = {}
    current_file: str | None = None
    current_lines: list[str] = []
    for line in diff_text.split("\n"):
        if line.startswith("diff --git"):
            if current_file and current_lines:
                patches[current_file] = "\n".join(current_lines)
            current_lines = []
            parts = line.split(" b/", 1)
            current_file = parts[1] if len(parts) > 1 else None
        elif line.startswith("@@") or (current_lines and current_file):
            if line.startswith(("---", "+++")):
                continue
            if line.startswith("@@") or current_lines:
                current_lines.append(line)
    if current_file and current_lines:
        patches[current_file] = "\n".join(current_lines)
    return patches


def fetch_pr_cached(
    owner: str,
    repo: str,
    pr_num: int,
    cache: PRCache | None = None,
    fresh: bool = False,
) -> tuple[
    dict[str, object],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    dict[str, dict[str, object]],
]:
    pr_key = f"{owner}/{repo}/{pr_num}"
    if not cache:
        cache = PRCache()
    if fresh:
        print(f"  --fresh: clearing cache for {pr_key}")
        cache.clear_pr(pr_key)
    cached = cache.get_meta(pr_key)
    print("  Fetching metadata...")
    meta = fetch_pr_meta(owner, repo, pr_num)
    new_head_sha = meta.get("head_sha", "")
    head_changed = not cached or cached["head_sha"] != new_head_sha
    need_files = head_changed or cache.get_files(pr_key, new_head_sha) is None
    results: dict[str, object] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        if need_files:
            print(
                "  Fetching file patches (head SHA changed)..."
                if cached and head_changed
                else "  Fetching file patches..."
            )
            results["files"] = pool.submit(fetch_pr_files, owner, repo, pr_num)
        results["comments"] = pool.submit(fetch_pr_comments, owner, repo, pr_num)
        results["reviews"] = pool.submit(fetch_pr_reviews, owner, repo, pr_num)
        results["commit_list"] = pool.submit(_fetch_commit_list, owner, repo, pr_num)
    if need_files:
        files = results["files"].result()
        cache.set_files(pr_key, new_head_sha, files)
    else:
        files = cache.get_files(pr_key, new_head_sha)
        print(f"  Files: cached ({len(files)} files)")
    cached_comments = cache._get_id_list("pr_comments", pr_key) if cached else []
    cached_reviews = cache._get_id_list("pr_reviews", pr_key) if cached else []
    comments = results["comments"].result()
    reviews = results["reviews"].result()
    if len(comments) != len(cached_comments) or len(reviews) != len(cached_reviews):
        if cached:
            print(
                f"  Comments/reviews updated ({len(cached_comments)}->{len(comments)}, "
                f"{len(cached_reviews)}->{len(reviews)})"
            )
        else:
            print(f"  Fetched {len(comments)} comments, {len(reviews)} reviews")
        cache._set_id_list("pr_comments", pr_key, comments)
        cache._set_id_list("pr_reviews", pr_key, reviews)
    else:
        print(f"  Comments: cached ({len(comments)}), Reviews: cached ({len(reviews)})")
        comments = cached_comments
        reviews = cached_reviews
    commit_list = results["commit_list"].result()
    cached_commits = cache.get_commits(pr_key)
    to_fetch_commits: list[dict[str, object]] = []
    commit_map: dict[str, dict[str, object]] = {}
    for c in commit_list:
        sha = c["sha"]
        if sha in cached_commits and not head_changed:
            commit_map[sha] = cached_commits[sha]
        else:
            to_fetch_commits.append(c)
    if to_fetch_commits:
        with ThreadPoolExecutor(max_workers=6) as pool:
            pool.map(lambda c: _fetch_commit_files_single(owner, repo, c), to_fetch_commits)
        for c in to_fetch_commits:
            cache.set_commit(pr_key, c["sha"], c)
            commit_map[c["sha"]] = c
    commits = [commit_map.get(c["sha"], c) for c in commit_list]
    new_count = len(to_fetch_commits)
    if new_count:
        print(f"  Fetched {new_count} new commit(s), {len(commits) - new_count} cached")
    else:
        print(f"  Commits: all {len(commits)} cached")
    usernames: set[str] = {meta.get("user", "")}
    for c in comments:
        usernames.add(c.get("user", ""))
    for r in reviews:
        usernames.add(r.get("user", ""))
    for cm in commits:
        if cm.get("author_login"):
            usernames.add(cm["author_login"])
    usernames.discard("")
    user_profiles: dict[str, dict[str, object]] = {}
    to_fetch: list[str] = []
    for u in usernames:
        cached_profile = cache.get_user_profile(u)
        if cached_profile:
            user_profiles[u] = cached_profile
        else:
            to_fetch.append(u)
    if to_fetch:
        print(f"  Fetching {len(to_fetch)} user profile(s)...")
        fresh_profiles = fetch_user_info(to_fetch)
        for u, p in fresh_profiles.items():
            user_profiles[u] = p
            cache.set_user_profile(u, p)
    else:
        print(f"  User profiles: all {len(user_profiles)} cached")
    cache.set_meta(pr_key, meta, len(comments), len(reviews), len(commits))
    return meta, files, comments, reviews, commits, user_profiles


# ---------------------------------------------------------------------------
# HTML Generation
# ---------------------------------------------------------------------------


def generate_html(
    meta: dict[str, object],
    files: list[dict[str, object]],
    pr_num: int,
    owner: str,
    repo: str,
    comments: list[dict[str, object]] | None = None,
    reviews: list[dict[str, object]] | None = None,
    user_profiles: dict[str, dict[str, object]] | None = None,
    highlight_comment_id: str | int | None = None,
    commits: list[dict[str, object]] | None = None,
) -> str:
    """Render PR review page. All DOM rendering is done client-side by review.js."""
    data = json.dumps(
        {
            "meta": meta,
            "files": files,
            "pr_num": pr_num,
            "owner": owner,
            "repo": repo,
            "comments": comments or [],
            "reviews": reviews or [],
            "user_profiles": user_profiles or {},
            "highlight_comment_id": highlight_comment_id,
            "commits": commits or [],
        },
        default=str,
    ).replace("</", "<\\/")
    title = f"PR #{pr_num}: {html.escape(str(meta.get('title', '')))}"
    return _REVIEW_PAGE_TPL.replace("__TITLE__", title).replace("__DATA__", data)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <pr_url_or_number> [--fresh] [--no-serve]", file=sys.stderr)
        sys.exit(1)
    pr_input = sys.argv[1]
    highlight_comment_id = None
    fragment_match = re.search(r"#issuecomment-(\d+)", pr_input)
    if fragment_match:
        highlight_comment_id = fragment_match.group(1)
    clean_url = pr_input.split("#")[0]
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", clean_url)
    if m:
        owner, repo, pr_num = m.group(1), m.group(2), int(m.group(3))
    else:
        owner, repo, pr_num = "BasedHardware", "omi", int(clean_url)
    fresh = "--fresh" in sys.argv
    cache = PRCache()
    print(f"Fetching PR #{pr_num} from {owner}/{repo}..." + (" (fresh)" if fresh else ""))
    meta, files, comments, reviews, commits, user_profiles = fetch_pr_cached(
        owner, repo, pr_num, cache=cache, fresh=fresh
    )
    cache.close()
    print(
        f"Ready: {len(files)} files, {len(comments)} comments, {len(reviews)} reviews, "
        f"{len(commits)} commits, {len(user_profiles)} profiles"
    )
    html_content = generate_html(
        meta,
        files,
        pr_num,
        owner,
        repo,
        comments=comments,
        reviews=reviews,
        user_profiles=user_profiles,
        highlight_comment_id=highlight_comment_id,
        commits=commits,
    )
    out_path = f"/tmp/pr-review-{pr_num}.html"
    with open(out_path, "w") as f:
        f.write(html_content)
    print(f"Written to {out_path}")


if __name__ == "__main__":
    main()
