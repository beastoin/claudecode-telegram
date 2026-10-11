#!/usr/bin/env python3
from __future__ import annotations
import base64
import json
import html
import sys
import os
import subprocess
import re
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable
from _review_render import generate_html, parse_patch
_TEMPLATE_DIR = Path(__file__).parent / "templates"
_REVIEW_SCHEMA = (_TEMPLATE_DIR / "review_schema.sql").read_text() if (_TEMPLATE_DIR / "review_schema.sql").exists() else ""
CACHE_DB = os.environ.get("PR_CACHE_DB", "/tmp/pr-review-cache.db")
USER_PROFILE_TTL = 86400
class PRCache:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or CACHE_DB
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_REVIEW_SCHEMA)
    def clear_pr(self, pr_key: str) -> None:
        for table in ("pr_meta", "pr_files", "pr_commits", "pr_comments", "pr_reviews"): self.conn.execute(f"DELETE FROM {table} WHERE pr_key=?", (pr_key,))
        self.conn.commit()
    def get_meta(self, pr_key: str) -> dict[str, object] | None:
        row = self.conn.execute(
            "SELECT data, head_sha, comment_count, review_count, commit_count "
            "FROM pr_meta WHERE pr_key=?", (pr_key,)).fetchone()
        if not row: return None
        return {
            "data": json.loads(row[0]),
            "head_sha": row[1],
            "comment_count": row[2],
            "review_count": row[3],
            "commit_count": row[4],
        }
    def set_meta(self, pr_key: str, meta: dict[str, object], comment_count: int, review_count: int, commit_count: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO pr_meta VALUES (?,?,?,?,?,?,?)",
            (pr_key, json.dumps(meta), meta.get("head_sha", ""),
             comment_count, review_count, commit_count, time.time()))
        self.conn.commit()
    def get_files(self, pr_key: str, head_sha: str) -> list[dict[str, object]] | None:
        row = self.conn.execute(
            "SELECT data FROM pr_files WHERE pr_key=? AND head_sha=?",
            (pr_key, head_sha)).fetchone()
        return json.loads(row[0]) if row else None
    def set_files(self, pr_key: str, head_sha: str, files: list[dict[str, object]]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO pr_files VALUES (?,?,?)",
            (pr_key, head_sha, json.dumps(files)))
        self.conn.commit()
    def get_commits(self, pr_key: str) -> dict[str, dict[str, object]]:
        rows = self.conn.execute(
            "SELECT sha, data FROM pr_commits WHERE pr_key=?", (pr_key,)).fetchall()
        return {r[0]: json.loads(r[1]) for r in rows}
    def set_commit(self, pr_key: str, sha: str, commit_data: dict[str, object]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO pr_commits VALUES (?,?,?)",
            (pr_key, sha, json.dumps(commit_data)))
        self.conn.commit()
    def _get_id_list(self, table: str, pr_key: str) -> list[dict[str, object]]:
        rows = self.conn.execute(f"SELECT data FROM {table} WHERE pr_key=? ORDER BY id", (pr_key,)).fetchall()
        return [json.loads(r[0]) for r in rows]
    def _set_id_list(self, table: str, pr_key: str, items: list[dict[str, object]]) -> None:
        self.conn.execute(f"DELETE FROM {table} WHERE pr_key=?", (pr_key,))
        for item in items: self.conn.execute(f"INSERT OR REPLACE INTO {table} VALUES (?,?,?)", (pr_key, item.get("id", 0), json.dumps(item)))
        self.conn.commit()
    def get_comments(self, pr_key: str) -> list[dict[str, object]]: return self._get_id_list("pr_comments", pr_key)
    def set_comments(self, pr_key: str, comments: list[dict[str, object]]) -> None: self._set_id_list("pr_comments", pr_key, comments)
    def get_reviews(self, pr_key: str) -> list[dict[str, object]]: return self._get_id_list("pr_reviews", pr_key)
    def set_reviews(self, pr_key: str, reviews: list[dict[str, object]]) -> None: self._set_id_list("pr_reviews", pr_key, reviews)
    def get_user_profile(self, username: str) -> dict[str, object] | None:
        row = self.conn.execute(
            "SELECT data, fetched_at FROM user_profiles WHERE username=?",
            (username,)).fetchone()
        if not row: return None
        if time.time() - row[1] > USER_PROFILE_TTL: return None  # expired
        return json.loads(row[0])
    def set_user_profile(self, username: str, profile: dict[str, object]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO user_profiles VALUES (?,?,?)",
            (username, json.dumps(profile), time.time()))
        self.conn.commit()
    def close(self) -> None:
        self.conn.close()
def fetch_pr_cached(
    owner: str, repo: str, pr_num: int,
    cache: PRCache | None = None, fresh: bool = False,
) -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], dict[str, dict[str, object]]]:
    pr_key = f"{owner}/{repo}/{pr_num}"
    if not cache: cache = PRCache()
    if fresh:
        print(f"  --fresh: clearing cache for {pr_key}")
        cache.clear_pr(pr_key)
    cached = cache.get_meta(pr_key)
    print("  Fetching metadata...")
    meta = fetch_pr_meta(owner, repo, pr_num)
    new_head_sha = meta.get("head_sha", "")
    head_changed = not cached or cached["head_sha"] != new_head_sha
    need_files = head_changed or cache.get_files(pr_key, new_head_sha) is None
    results = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        if need_files:
            print("  Fetching file patches (head SHA changed)..." if cached and head_changed else "  Fetching file patches...")
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
    cached_comments = cache.get_comments(pr_key) if cached else []
    cached_reviews = cache.get_reviews(pr_key) if cached else []
    comments = results["comments"].result()
    reviews = results["reviews"].result()
    if len(comments) != len(cached_comments) or len(reviews) != len(cached_reviews):
        if cached:
            print(f"  Comments/reviews updated ({len(cached_comments)}->{len(comments)}, "
                  f"{len(cached_reviews)}->{len(reviews)})")
        else: print(f"  Fetched {len(comments)} comments, {len(reviews)} reviews")
        cache.set_comments(pr_key, comments)
        cache.set_reviews(pr_key, reviews)
    else:
        print(f"  Comments: cached ({len(comments)}), Reviews: cached ({len(reviews)})")
        comments = cached_comments
        reviews = cached_reviews
    commit_list = results["commit_list"].result()
    cached_commits = cache.get_commits(pr_key)
    to_fetch_commits = []
    commit_map = {}
    for c in commit_list:
        sha = c["sha"]
        if sha in cached_commits and not head_changed: commit_map[sha] = cached_commits[sha]
        else: to_fetch_commits.append(c)
    if to_fetch_commits:
        with ThreadPoolExecutor(max_workers=6) as pool: pool.map(lambda c: _fetch_commit_files_single(owner, repo, c), to_fetch_commits)
        for c in to_fetch_commits:
            cache.set_commit(pr_key, c["sha"], c)
            commit_map[c["sha"]] = c
    commits = [commit_map.get(c["sha"], c) for c in commit_list]
    new_count = len(to_fetch_commits)
    if new_count: print(f"  Fetched {new_count} new commit(s), {len(commits) - new_count} cached")
    else: print(f"  Commits: all {len(commits)} cached")
    usernames = {meta.get('user', '')}
    for c in comments: usernames.add(c.get('user', ''))
    for r in reviews: usernames.add(r.get('user', ''))
    for cm in commits:
        if cm.get('author_login'): usernames.add(cm['author_login'])
    usernames.discard('')
    user_profiles = {}
    to_fetch = []
    for u in usernames:
        cached_profile = cache.get_user_profile(u)
        if cached_profile: user_profiles[u] = cached_profile
        else: to_fetch.append(u)
    if to_fetch:
        print(f"  Fetching {len(to_fetch)} user profile(s)...")
        fresh_profiles = fetch_user_info(to_fetch)
        for u, p in fresh_profiles.items():
            user_profiles[u] = p
            cache.set_user_profile(u, p)
    else: print(f"  User profiles: all {len(user_profiles)} cached")
    cache.set_meta(pr_key, meta, len(comments), len(reviews), len(commits))
    return meta, files, comments, reviews, commits, user_profiles
def fetch_user_info(usernames: Iterable[str]) -> dict[str, dict[str, object]]:
    profiles = {}
    for username in set(usernames):
        if not username: continue
        try:
            r = subprocess.run(
                ["gh", "api", f"users/{username}",
                 "--jq", '{login: .login, name: .name, followers: .followers, '
                          'public_repos: .public_repos, created_at: .created_at, '
                          'avatar_url: .avatar_url, bio: .bio}'],
                capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip(): profiles[username] = json.loads(r.stdout)
        except Exception: pass
    return profiles
def fetch_pr_reviews(owner: str, repo: str, pr_num: int) -> list[dict[str, object]]:
    reviews = []
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/pulls/{pr_num}/reviews",
             "--paginate", "--jq",
             '.[] | {id: .id, body: .body, user: .user.login, '
             'state: .state, submitted_at: .submitted_at, '
             'html_url: .html_url}'],
            capture_output=True, text=True, timeout=30)
        for line in r.stdout.strip().split('\n'):
            if line.strip(): reviews.append(json.loads(line))
    except Exception as e: print(f"Warning: could not fetch reviews: {e}")
    return reviews
def fetch_pr_comments(owner: str, repo: str, pr_num: int) -> list[dict[str, object]]:
    comments = []
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/issues/{pr_num}/comments",
             "--paginate", "--jq",
             '.[] | {id: .id, body: .body, user: .user.login, '
             'created_at: .created_at, updated_at: .updated_at, '
             'html_url: .html_url, type: "issue"}'],
            capture_output=True, text=True, timeout=30)
        for line in r.stdout.strip().split('\n'):
            if line.strip(): comments.append(json.loads(line))
    except Exception as e: print(f"Warning: could not fetch issue comments: {e}")
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/pulls/{pr_num}/comments",
             "--paginate", "--jq",
             '.[] | {id: .id, body: .body, user: .user.login, '
             'created_at: .created_at, path: .path, line: .line, '
             'original_line: .original_line, side: .side, '
             'diff_hunk: .diff_hunk, html_url: .html_url, type: "review"}'],
            capture_output=True, text=True, timeout=30)
        for line in r.stdout.strip().split('\n'):
            if line.strip(): comments.append(json.loads(line))
    except Exception as e: print(f"Warning: could not fetch review comments: {e}")
    comments.sort(key=lambda c: c.get('created_at', ''))
    return comments
def _fetch_commit_list(owner: str, repo: str, pr_num: int) -> list[dict[str, object]]:
    commits = []
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/pulls/{pr_num}/commits",
             "--paginate", "--jq",
             '.[] | {sha: .sha, message: .commit.message, '
             'author: .commit.author.name, author_login: .author.login, '
             'date: .commit.author.date}'],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            print(f"Warning: commit list API returned {r.returncode}: {r.stderr.strip()[:200]}")
            return commits
        for line in r.stdout.strip().split('\n'):
            if line.strip():
                obj = json.loads(line)
                if "sha" in obj: commits.append(obj)
    except Exception as e: print(f"Warning: could not fetch commits: {e}")
    return commits
def _fetch_commit_files_single(owner: str, repo: str, c: dict[str, object]) -> None:
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/commits/{c['sha']}",
             "--jq", '{files: [.files[] | {filename: .filename, status: .status, '
                      'additions: .additions, deletions: .deletions, patch: .patch}]}'],
            capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout)
            c['files'] = data.get('files', [])
    except Exception: c['files'] = []
def fetch_pr_meta(owner: str, repo: str, pr_num: int) -> dict[str, object]:
    meta_raw = subprocess.run(
        ["gh", "api", f"repos/{owner}/{repo}/pulls/{pr_num}",
         "--jq", '{title: .title, body: .body, user: .user.login, '
                  'changed_files: .changed_files, additions: .additions, '
                  'deletions: .deletions, commits: .commits, '
                  'base: .base.ref, head: .head.ref, html_url: .html_url, '
                  'head_sha: .head.sha, base_sha: .base.sha, '
                  'mergeable: .mergeable, mergeable_state: .mergeable_state, '
                  'merged: .merged, state: .state, draft: .draft}'],
        capture_output=True, text=True, timeout=30)
    if meta_raw.returncode != 0: raise RuntimeError(f"PR #{pr_num} not found or API error: {meta_raw.stderr.strip()[:200]}")
    meta = json.loads(meta_raw.stdout)
    head_sha = meta.get("head_sha", "")
    if head_sha:
        try:
            checks_raw = subprocess.run(
                ["gh", "api", f"repos/{owner}/{repo}/commits/{head_sha}/check-runs",
                 "--jq", '[.check_runs[] | {name: .name, status: .status, conclusion: .conclusion}]'],
                capture_output=True, text=True, timeout=15)
            if checks_raw.returncode == 0 and checks_raw.stdout.strip(): meta["checks"] = json.loads(checks_raw.stdout)
            else: meta["checks"] = []
        except Exception: meta["checks"] = []
    return meta
def fetch_pr_files(owner: str, repo: str, pr_num: int) -> list[dict[str, object]]:
    files_raw = subprocess.run(
        ["gh", "api", f"repos/{owner}/{repo}/pulls/{pr_num}/files",
         "--paginate", "--jq",
         '.[] | {filename: .filename, status: .status, '
         'additions: .additions, deletions: .deletions, patch: .patch}'],
        capture_output=True, text=True, timeout=60)
    files = []
    for line in files_raw.stdout.strip().split('\n'):
        if line.strip(): files.append(json.loads(line))
    missing = [f for f in files if not f.get('patch') and f.get('additions', 0) > 0]
    if missing:
        print(f"    Fetching full diff for {len(missing)} large files...")
        full_diff = _fetch_full_diff(owner, repo, pr_num)
        if full_diff:
            diff_patches = _parse_full_diff(full_diff)
            for f in files:
                if not f.get('patch') and f['filename'] in diff_patches: f['patch'] = diff_patches[f['filename']]
    return files
def _fetch_full_diff(owner: str, repo: str, pr_num: int) -> str | None:
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/pulls/{pr_num}",
             "-H", "Accept: application/vnd.github.v3.diff"],
            capture_output=True, text=True, timeout=120)
        if r.returncode == 0: return r.stdout
    except subprocess.TimeoutExpired: print("Full diff fetch timed out")
    return None
def _parse_full_diff(diff_text: str) -> dict[str, str]:
    patches = {}
    current_file = None
    current_lines = []
    for line in diff_text.split('\n'):
        if line.startswith('diff --git'):
            if current_file and current_lines: patches[current_file] = '\n'.join(current_lines)
            current_lines = []
            parts = line.split(' b/', 1)
            current_file = parts[1] if len(parts) > 1 else None
        elif line.startswith('@@') or (current_lines and current_file):
            if line.startswith('---') or line.startswith('+++'): continue
            if line.startswith('@@') or current_lines: current_lines.append(line)
    if current_file and current_lines: patches[current_file] = '\n'.join(current_lines)
    return patches
def _fetch_file_context(owner: str, repo: str, path: str, line: int, ref: str, context: int = 3) -> tuple[str | None, str]:
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/contents/{path}?ref={ref}",
             "--jq", ".content"],
            capture_output=True, text=True, timeout=15)
        if r.returncode != 0: return None, ref[:12]
        content = base64.b64decode(r.stdout.strip()).decode("utf-8", errors="replace")
        lines = content.split('\n')
        start = max(0, line - context - 1)
        end = min(len(lines), line + context)
        snippet = []
        for i in range(start, end):
            ln = i + 1
            marker = " \u2190" if ln == line else ""
            snippet.append(f"\u2502 {ln:>4}: {lines[i]}{marker}")
        return '\n'.join(snippet), ref[:12]
    except Exception as e:
        print(f"[pr-comment] Failed to fetch file context: {e}")
        return None, ref[:12]
def _format_comment_with_context(comment_body: str, pr_num: int, path: str, line: int, context_snippet: str | None, short_sha: str) -> str:
    parts = [f"PR #{pr_num} comment on {path}:{line} (ref {short_sha})"]
    if context_snippet: parts.append(context_snippet)
    parts.append("")
    parts.append(comment_body)
    return '\n'.join(parts)
def _notify_telegram(comment_body: str, pr_num: int, path: str, line: int, context_snippet: str | None = None, short_sha: str = "") -> None:
    bridge_url = os.environ.get("BRIDGE_URL", "http://localhost:8271")
    text = f"\U0001f4ac " + _format_comment_with_context(
        comment_body, pr_num, path, line, context_snippet, short_sha)
    try:
        import urllib.request
        req = urllib.request.Request(
            f"{bridge_url}/notifications",
            data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception as e: print(f"[pr-comment] Telegram notify failed: {e}")
def _route_mentions_to_workers(comment_body: str, pr_num: int, path: str, line: int, context_snippet: str | None = None, short_sha: str = "") -> None:
    tmux_prefix = os.environ.get("TMUX_PREFIX", "claude-prod-")
    mentions = re.findall(r'@(\w+)', comment_body)
    if not mentions: return
    try:
        r = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=5)
        active_sessions = set(r.stdout.strip().split('\n')) if r.returncode == 0 else set()
    except Exception: active_sessions = set()
    msg = "manager: " + _format_comment_with_context(
        comment_body, pr_num, path, line, context_snippet, short_sha)
    for mention in mentions:
        target_session = f"{tmux_prefix}{mention}"
        if target_session not in active_sessions: continue
        try:
            import tempfile as _tf, uuid as _uuid
            buf_name = f"pr-{_uuid.uuid4().hex[:8]}"
            fd, tmpfile = _tf.mkstemp(suffix=".msg", prefix="pr-send-")
            try:
                os.write(fd, msg.encode())
                os.close(fd)
                subprocess.run(
                    ["tmux", "load-buffer", "-b", buf_name, tmpfile],
                    capture_output=True, timeout=5)
                subprocess.run(
                    ["tmux", "paste-buffer", "-b", buf_name, "-t", target_session],
                    capture_output=True, timeout=5)
                subprocess.run(
                    ["tmux", "send-keys", "-t", target_session, "Enter"],
                    capture_output=True, timeout=5)
                subprocess.run(
                    ["tmux", "delete-buffer", "-b", buf_name],
                    capture_output=True, timeout=5)
            finally:
                try: os.unlink(tmpfile)
                except OSError: pass
            print(f"[pr-comment] Routed to @{mention} via tmux")
        except Exception as e: print(f"[pr-comment] Failed to route to @{mention}: {e}")
def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <pr_url_or_number> [--port PORT]", file=sys.stderr)
        sys.exit(1)
    pr_input = sys.argv[1]
    port = 10171
    for i, arg in enumerate(sys.argv):
        if arg == '--port' and i + 1 < len(sys.argv): port = int(sys.argv[i + 1])
    highlight_comment_id = None
    fragment_match = re.search(r'#issuecomment-(\d+)', pr_input)
    if fragment_match: highlight_comment_id = fragment_match.group(1)
    clean_url = pr_input.split('#')[0]
    m = re.match(r'https://github\.com/([^/]+)/([^/]+)/pull/(\d+)', clean_url)
    if m: owner, repo, pr_num = m.group(1), m.group(2), int(m.group(3))
    else: owner, repo, pr_num = 'BasedHardware', 'omi', int(clean_url)
    fresh = '--fresh' in sys.argv
    cache = PRCache()
    print(f"Fetching PR #{pr_num} from {owner}/{repo}..." + (" (fresh)" if fresh else ""))
    meta, files, comments, reviews, commits, user_profiles = fetch_pr_cached(
        owner, repo, pr_num, cache=cache, fresh=fresh)
    cache.close()
    print(f"Ready: {len(files)} files, {len(comments)} comments, {len(reviews)} reviews, "
          f"{len(commits)} commits, {len(user_profiles)} profiles")
    html_content = generate_html(meta, files, pr_num, owner, repo,
                                 comments=comments, reviews=reviews,
                                 user_profiles=user_profiles,
                                 highlight_comment_id=highlight_comment_id,
                                 commits=commits)
    out_path = f'/tmp/pr-review-{pr_num}.html'
    with open(out_path, 'w') as f: f.write(html_content)
    print(f"Written to {out_path}")
    if '--no-serve' not in sys.argv:
        import http.server
        import socketserver
        from urllib.parse import urlparse, parse_qs
        os.chdir('/tmp')
        base_handler = http.server.SimpleHTTPRequestHandler
        class PRHandler(base_handler):
            def log_message(self, fmt: str, *args: object) -> None:
                pass
            def do_POST(self) -> None:
                if self.path == '/tools/review/comments':
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length)
                    try: data = json.loads(body)
                    except (json.JSONDecodeError, ValueError):
                        self.send_response(400)
                        self.end_headers()
                        self.wfile.write(b"Invalid JSON")
                        return
                    o = data.get("owner", "")
                    r = data.get("repo", "")
                    pn = data.get("pr_num", 0)
                    path = data.get("path", "")
                    line = data.get("line", 0)
                    side = data.get("side", "RIGHT")
                    comment_body = data.get("body", "").strip()
                    head_sha = data.get("head_sha", "")
                    if not all([o, r, pn, path, line, comment_body, head_sha]):
                        self.send_response(400)
                        self.end_headers()
                        self.wfile.write(b"Missing required fields")
                        return
                    gh_payload = json.dumps({
                        "body": comment_body, "commit_id": head_sha,
                        "path": path, "line": line, "side": side,
                    })
                    try:
                        result = subprocess.run(
                            ["gh", "api", f"repos/{o}/{r}/pulls/{pn}/comments",
                             "--method", "POST", "--input", "-"],
                            input=gh_payload, capture_output=True, text=True, timeout=15)
                        if result.returncode != 0:
                            err = result.stderr.strip() or result.stdout.strip()
                            self.send_response(502)
                            self.end_headers()
                            self.wfile.write(f"GitHub API error: {err}".encode())
                            return
                    except subprocess.TimeoutExpired:
                        self.send_response(504)
                        self.end_headers()
                        self.wfile.write(b"GitHub API timeout")
                        return
                    context_snippet, short_sha = _fetch_file_context(
                        o, r, path, line, head_sha)
                    _notify_telegram(comment_body, pn, path, line,
                                     context_snippet, short_sha)
                    _route_mentions_to_workers(
                        comment_body, pn, path, line,
                        context_snippet, short_sha)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"ok": True}).encode())
                else:
                    self.send_response(404)
                    self.end_headers()
            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == '/tools/review/files':
                    params = parse_qs(parsed.query)
                    o = params.get('owner', [''])[0]
                    r = params.get('repo', [''])[0]
                    fp = params.get('path', [''])[0]
                    ref = params.get('ref', [''])[0]
                    if not all([o, r, fp, ref]):
                        self.send_response(400)
                        self.end_headers()
                        return
                    try:
                        result = subprocess.run(
                            ["gh", "api", f"repos/{o}/{r}/contents/{fp}?ref={ref}",
                             "--jq", ".content"],
                            capture_output=True, text=True, timeout=15)
                        if result.returncode != 0:
                            self.send_response(404)
                            self.end_headers()
                            return
                        import base64 as b64
                        content = b64.b64decode(result.stdout.strip()).decode("utf-8", errors="replace")
                        lines = content.split('\n')
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps(lines).encode())
                    except Exception:
                        self.send_response(500)
                        self.end_headers()
                    return
                super().do_GET()
        with socketserver.TCPServer(("0.0.0.0", port), PRHandler) as httpd:
            url = f"http://100.125.36.102:{port}/pr-review-{pr_num}.html"
            print(f"\nServing at {url}")
            print("Press Ctrl+C to stop")
            httpd.serve_forever()
if __name__ == '__main__': main()