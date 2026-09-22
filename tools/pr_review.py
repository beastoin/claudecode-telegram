#!/usr/bin/env python3
"""Generate a self-contained HTML PR review page from GitHub API data."""

import base64
import json
import html
import sys
import os
import subprocess
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

CACHE_DB = os.environ.get("PR_CACHE_DB", "/tmp/pr-review-cache.db")
USER_PROFILE_TTL = 86400  # 24 hours


class PRCache:
    """SQLite cache for PR review data. One DB for all PRs."""

    def __init__(self, db_path=None):
        self.db_path = db_path or CACHE_DB
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS pr_meta (
                pr_key TEXT PRIMARY KEY,
                data TEXT,
                head_sha TEXT,
                comment_count INTEGER DEFAULT 0,
                review_count INTEGER DEFAULT 0,
                commit_count INTEGER DEFAULT 0,
                fetched_at REAL
            );
            CREATE TABLE IF NOT EXISTS pr_files (
                pr_key TEXT,
                head_sha TEXT,
                data TEXT,
                PRIMARY KEY (pr_key, head_sha)
            );
            CREATE TABLE IF NOT EXISTS pr_commits (
                pr_key TEXT,
                sha TEXT,
                data TEXT,
                PRIMARY KEY (pr_key, sha)
            );
            CREATE TABLE IF NOT EXISTS pr_comments (
                pr_key TEXT,
                id INTEGER,
                data TEXT,
                PRIMARY KEY (pr_key, id)
            );
            CREATE TABLE IF NOT EXISTS pr_reviews (
                pr_key TEXT,
                id INTEGER,
                data TEXT,
                PRIMARY KEY (pr_key, id)
            );
            CREATE TABLE IF NOT EXISTS user_profiles (
                username TEXT PRIMARY KEY,
                data TEXT,
                fetched_at REAL
            );
        """)

    def clear_pr(self, pr_key):
        """Clear all cached data for a specific PR."""
        for table in ("pr_meta", "pr_files", "pr_commits", "pr_comments", "pr_reviews"):
            self.conn.execute(f"DELETE FROM {table} WHERE pr_key=?", (pr_key,))
        self.conn.commit()

    def get_meta(self, pr_key):
        row = self.conn.execute(
            "SELECT data, head_sha, comment_count, review_count, commit_count "
            "FROM pr_meta WHERE pr_key=?", (pr_key,)).fetchone()
        if not row:
            return None
        return {
            "data": json.loads(row[0]),
            "head_sha": row[1],
            "comment_count": row[2],
            "review_count": row[3],
            "commit_count": row[4],
        }

    def set_meta(self, pr_key, meta, comment_count, review_count, commit_count):
        self.conn.execute(
            "INSERT OR REPLACE INTO pr_meta VALUES (?,?,?,?,?,?,?)",
            (pr_key, json.dumps(meta), meta.get("head_sha", ""),
             comment_count, review_count, commit_count, time.time()))
        self.conn.commit()

    def get_files(self, pr_key, head_sha):
        row = self.conn.execute(
            "SELECT data FROM pr_files WHERE pr_key=? AND head_sha=?",
            (pr_key, head_sha)).fetchone()
        return json.loads(row[0]) if row else None

    def set_files(self, pr_key, head_sha, files):
        self.conn.execute(
            "INSERT OR REPLACE INTO pr_files VALUES (?,?,?)",
            (pr_key, head_sha, json.dumps(files)))
        self.conn.commit()

    def get_commits(self, pr_key):
        rows = self.conn.execute(
            "SELECT sha, data FROM pr_commits WHERE pr_key=?", (pr_key,)).fetchall()
        return {r[0]: json.loads(r[1]) for r in rows}

    def set_commit(self, pr_key, sha, commit_data):
        self.conn.execute(
            "INSERT OR REPLACE INTO pr_commits VALUES (?,?,?)",
            (pr_key, sha, json.dumps(commit_data)))
        self.conn.commit()

    def get_comments(self, pr_key):
        rows = self.conn.execute(
            "SELECT data FROM pr_comments WHERE pr_key=? ORDER BY id", (pr_key,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def set_comments(self, pr_key, comments):
        self.conn.execute("DELETE FROM pr_comments WHERE pr_key=?", (pr_key,))
        for c in comments:
            self.conn.execute(
                "INSERT OR REPLACE INTO pr_comments VALUES (?,?,?)",
                (pr_key, c.get("id", 0), json.dumps(c)))
        self.conn.commit()

    def get_reviews(self, pr_key):
        rows = self.conn.execute(
            "SELECT data FROM pr_reviews WHERE pr_key=? ORDER BY id", (pr_key,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def set_reviews(self, pr_key, reviews):
        self.conn.execute("DELETE FROM pr_reviews WHERE pr_key=?", (pr_key,))
        for r in reviews:
            self.conn.execute(
                "INSERT OR REPLACE INTO pr_reviews VALUES (?,?,?)",
                (pr_key, r.get("id", 0), json.dumps(r)))
        self.conn.commit()

    def get_user_profile(self, username):
        row = self.conn.execute(
            "SELECT data, fetched_at FROM user_profiles WHERE username=?",
            (username,)).fetchone()
        if not row:
            return None
        if time.time() - row[1] > USER_PROFILE_TTL:
            return None  # expired
        return json.loads(row[0])

    def set_user_profile(self, username, profile):
        self.conn.execute(
            "INSERT OR REPLACE INTO user_profiles VALUES (?,?,?)",
            (username, json.dumps(profile), time.time()))
        self.conn.commit()

    def close(self):
        self.conn.close()


def fetch_pr_cached(owner, repo, pr_num, cache=None, fresh=False):
    """Fetch PR data with SQLite caching. Returns (meta, files, comments, reviews, commits, user_profiles).

    Strategy:
    - Always re-fetch meta (1 API call, tiny) to detect changes
    - head_sha changed → re-fetch files + all commits
    - head_sha same → use cached files, only fetch new commits
    - comment/review count increased → re-fetch all comments/reviews
    - User profiles → cached for 24h
    """
    pr_key = f"{owner}/{repo}/{pr_num}"

    if not cache:
        cache = PRCache()

    if fresh:
        print(f"  --fresh: clearing cache for {pr_key}")
        cache.clear_pr(pr_key)

    cached = cache.get_meta(pr_key)

    # 1. Always fetch fresh metadata (1 API call)
    print("  Fetching metadata...")
    meta = fetch_pr_meta(owner, repo, pr_num)
    new_head_sha = meta.get("head_sha", "")
    head_changed = not cached or cached["head_sha"] != new_head_sha

    # 2. Parallel fetch: files + comments + reviews + commit list
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
        else:
            print(f"  Fetched {len(comments)} comments, {len(reviews)} reviews")
        cache.set_comments(pr_key, comments)
        cache.set_reviews(pr_key, reviews)
    else:
        print(f"  Comments: cached ({len(comments)}), Reviews: cached ({len(reviews)})")
        comments = cached_comments
        reviews = cached_reviews

    # 3. Commits: use parallel-fetched list, fetch file patches for new ones
    commit_list = results["commit_list"].result()
    cached_commits = cache.get_commits(pr_key)
    to_fetch_commits = []
    commit_map = {}
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

    # 4. User profiles: cached with 24h TTL
    usernames = {meta.get('user', '')}
    for c in comments:
        usernames.add(c.get('user', ''))
    for r in reviews:
        usernames.add(r.get('user', ''))
    for cm in commits:
        if cm.get('author_login'):
            usernames.add(cm['author_login'])
    usernames.discard('')

    user_profiles = {}
    to_fetch = []
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

    # Update meta cache with current counts
    cache.set_meta(pr_key, meta, len(comments), len(reviews), len(commits))

    return meta, files, comments, reviews, commits, user_profiles


def fetch_user_info(usernames):
    """Fetch GitHub user profiles for a list of usernames. Returns dict of username -> info."""
    profiles = {}
    for username in set(usernames):
        if not username:
            continue
        try:
            r = subprocess.run(
                ["gh", "api", f"users/{username}",
                 "--jq", '{login: .login, name: .name, followers: .followers, '
                          'public_repos: .public_repos, created_at: .created_at, '
                          'avatar_url: .avatar_url, bio: .bio}'],
                capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                profiles[username] = json.loads(r.stdout)
        except Exception:
            pass
    return profiles


def fetch_pr_reviews(owner, repo, pr_num):
    """Fetch PR reviews (approve/request changes/comment)."""
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
            if line.strip():
                reviews.append(json.loads(line))
    except Exception as e:
        print(f"Warning: could not fetch reviews: {e}")
    return reviews


def fetch_pr_comments(owner, repo, pr_num):
    """Fetch all PR comments (issue comments + review comments)."""
    comments = []

    # Issue comments (general discussion)
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/issues/{pr_num}/comments",
             "--paginate", "--jq",
             '.[] | {id: .id, body: .body, user: .user.login, '
             'created_at: .created_at, updated_at: .updated_at, '
             'html_url: .html_url, type: "issue"}'],
            capture_output=True, text=True, timeout=30)
        for line in r.stdout.strip().split('\n'):
            if line.strip():
                comments.append(json.loads(line))
    except Exception as e:
        print(f"Warning: could not fetch issue comments: {e}")

    # Review comments (inline on code)
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
            if line.strip():
                comments.append(json.loads(line))
    except Exception as e:
        print(f"Warning: could not fetch review comments: {e}")

    # Sort all by created_at
    comments.sort(key=lambda c: c.get('created_at', ''))
    return comments


def _fetch_commit_list(owner, repo, pr_num):
    """Fetch PR commit list (metadata only, no file patches)."""
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
                if "sha" in obj:
                    commits.append(obj)
    except Exception as e:
        print(f"Warning: could not fetch commits: {e}")
    return commits


def _fetch_commit_files_single(owner, repo, c):
    """Fetch file patches for a single commit."""
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/commits/{c['sha']}",
             "--jq", '{files: [.files[] | {filename: .filename, status: .status, '
                      'additions: .additions, deletions: .deletions, patch: .patch}]}'],
            capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout)
            c['files'] = data.get('files', [])
    except Exception:
        c['files'] = []


def fetch_pr_commits(owner, repo, pr_num):
    """Fetch PR commits with their file patches (non-cached path)."""
    commits = _fetch_commit_list(owner, repo, pr_num)
    with ThreadPoolExecutor(max_workers=4) as pool:
        pool.map(lambda c: _fetch_commit_files_single(owner, repo, c), commits)
    return commits


def fetch_pr_meta(owner, repo, pr_num):
    """Fetch only PR metadata (1 API call)."""
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
    if meta_raw.returncode != 0:
        raise RuntimeError(f"PR #{pr_num} not found or API error: {meta_raw.stderr.strip()[:200]}")
    meta = json.loads(meta_raw.stdout)
    # Fetch CI check runs for merge readiness
    head_sha = meta.get("head_sha", "")
    if head_sha:
        try:
            checks_raw = subprocess.run(
                ["gh", "api", f"repos/{owner}/{repo}/commits/{head_sha}/check-runs",
                 "--jq", '[.check_runs[] | {name: .name, status: .status, conclusion: .conclusion}]'],
                capture_output=True, text=True, timeout=15)
            if checks_raw.returncode == 0 and checks_raw.stdout.strip():
                meta["checks"] = json.loads(checks_raw.stdout)
            else:
                meta["checks"] = []
        except Exception:
            meta["checks"] = []
    return meta


def fetch_pr_files(owner, repo, pr_num):
    """Fetch PR file patches."""
    files_raw = subprocess.run(
        ["gh", "api", f"repos/{owner}/{repo}/pulls/{pr_num}/files",
         "--paginate", "--jq",
         '.[] | {filename: .filename, status: .status, '
         'additions: .additions, deletions: .deletions, patch: .patch}'],
        capture_output=True, text=True, timeout=60)
    files = []
    for line in files_raw.stdout.strip().split('\n'):
        if line.strip():
            files.append(json.loads(line))

    missing = [f for f in files if not f.get('patch') and f.get('additions', 0) > 0]
    if missing:
        print(f"    Fetching full diff for {len(missing)} large files...")
        full_diff = _fetch_full_diff(owner, repo, pr_num)
        if full_diff:
            diff_patches = _parse_full_diff(full_diff)
            for f in files:
                if not f.get('patch') and f['filename'] in diff_patches:
                    f['patch'] = diff_patches[f['filename']]
    return files


def fetch_pr_data(owner, repo, pr_num):
    """Fetch PR metadata and file patches via gh CLI (non-cached path)."""
    meta = fetch_pr_meta(owner, repo, pr_num)
    files = fetch_pr_files(owner, repo, pr_num)
    return meta, files



def _fetch_full_diff(owner, repo, pr_num):
    """Fetch the full unified diff for a PR."""
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/pulls/{pr_num}",
             "-H", "Accept: application/vnd.github.v3.diff"],
            capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            return r.stdout
    except subprocess.TimeoutExpired:
        print("Full diff fetch timed out")
    return None


def _parse_full_diff(diff_text):
    """Parse a full unified diff into per-file patches."""
    patches = {}
    current_file = None
    current_lines = []

    for line in diff_text.split('\n'):
        if line.startswith('diff --git'):
            # Save previous file
            if current_file and current_lines:
                patches[current_file] = '\n'.join(current_lines)
            current_lines = []
            # Extract filename from "diff --git a/path b/path"
            parts = line.split(' b/', 1)
            current_file = parts[1] if len(parts) > 1 else None
        elif line.startswith('@@') or (current_lines and current_file):
            # Skip --- and +++ headers, keep @@ and content lines
            if line.startswith('---') or line.startswith('+++'):
                continue
            if line.startswith('@@') or current_lines:
                current_lines.append(line)

    if current_file and current_lines:
        patches[current_file] = '\n'.join(current_lines)

    return patches


def parse_patch(patch_text):
    """Parse unified diff patch into structured hunks."""
    if not patch_text:
        return []
    hunks = []
    current_hunk = None
    for line in patch_text.split('\n'):
        if line.startswith('@@'):
            if current_hunk:
                hunks.append(current_hunk)
            # Parse @@ -old_start,old_count +new_start,new_count @@
            m = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)', line)
            if m:
                current_hunk = {
                    'header': line,
                    'old_start': int(m.group(1)),
                    'old_count': int(m.group(2) or 1),
                    'new_start': int(m.group(3)),
                    'new_count': int(m.group(4) or 1),
                    'context': m.group(5),
                    'lines': []
                }
            else:
                current_hunk = {'header': line, 'old_start': 0, 'old_count': 0, 'new_start': 0, 'new_count': 0, 'context': '', 'lines': []}
        elif current_hunk is not None:
            if line.startswith('+'):
                current_hunk['lines'].append(('add', line[1:]))
            elif line.startswith('-'):
                current_hunk['lines'].append(('del', line[1:]))
            else:
                current_hunk['lines'].append(('ctx', line[1:] if line.startswith(' ') else line))
    if current_hunk:
        hunks.append(current_hunk)
    return hunks



def generate_html(meta, files, pr_num, owner, repo, comments=None, reviews=None,
                   user_profiles=None, highlight_comment_id=None, commits=None):
    """Generate self-contained HTML review page with transcript-style layout."""
    comments = comments or []
    reviews = reviews or []
    commits = commits or []
    user_profiles = user_profiles or {}

    def _user_badge(username):
        """Generate a user badge with avatar and profile info."""
        p = user_profiles.get(username, {})
        if not p:
            return f'<strong>{html.escape(username)}</strong>'
        name = html.escape(p.get('name', '') or username)
        joined = (p.get('created_at', '') or '')[:10]
        followers = p.get('followers', 0)
        repos = p.get('public_repos', 0)
        bio = html.escape((p.get('bio', '') or '')[:80])
        avatar = html.escape(p.get('avatar_url', '') or '')
        avatar_img = f'<img src="{avatar}" class="disc-av">' if avatar else ''
        return (f'{avatar_img}<span class="disc-user-info"><strong>{name}</strong>'
                f' <span class="disc-user-meta">@{html.escape(username)} &middot; '
                f'joined {joined} &middot; {followers:,} followers &middot; '
                f'{repos} repos</span>'
                f'{"<br><span class=disc-user-bio>" + bio + "</span>" if bio else ""}'
                f'</span>')

    # ---- Discussion HTML (for Discussion tab) ----
    discussion_html = ''
    active_reviews = [r for r in reviews if r.get('body') or r.get('state') != 'COMMENTED']
    disc_count = 1 + len(active_reviews) + len(comments)

    # PR Description
    pr_author = meta.get('user', '')
    pr_body_escaped = html.escape(meta.get('body', '') or '(no description)', quote=True)
    discussion_html += f'''<div class="disc-card" id="disc-pr-desc">
        <div class="disc-head">
            {_user_badge(pr_author)}
            <span class="disc-label">author</span>
        </div>
        <div class="disc-body" data-body="{pr_body_escaped}"></div>
    </div>\n'''

    # Commits (with inline diffs)
    for ci, commit in enumerate(commits):
        sha = commit.get('sha', '')[:8]
        sha_full = commit.get('sha', '')
        msg = commit.get('message', '')
        msg_first = msg.split('\n')[0] if msg else '(no message)'
        msg_rest = '\n'.join(msg.split('\n')[1:]).strip() if '\n' in msg else ''
        author = commit.get('author', '')
        author_login = commit.get('author_login', '')
        date = (commit.get('date', '') or '')[:10]
        commit_files = commit.get('files', [])
        total_add = sum(cf.get('additions', 0) for cf in commit_files)
        total_del = sum(cf.get('deletions', 0) for cf in commit_files)

        # Build compact diff for each file in this commit
        commit_diff_html = ''
        for cf in commit_files:
            cf_status = cf.get('status', 'modified')
            cf_class = {'added': 'added', 'modified': 'modified',
                        'removed': 'removed'}.get(cf_status, '')
            cf_patch = cf.get('patch', '')
            cf_lines = ''
            if cf_patch:
                for line in cf_patch.split('\n'):
                    escaped = html.escape(line[1:] if len(line) > 0 and line[0] in ('+', '-', ' ') else line)
                    if line.startswith('@@'):
                        cf_lines += (f'<div class="cdiff-hunk">'
                                     f'{html.escape(line)}</div>')
                    elif line.startswith('+'):
                        cf_lines += f'<div class="cdiff-add">+{escaped}</div>'
                    elif line.startswith('-'):
                        cf_lines += f'<div class="cdiff-del">-{escaped}</div>'
                    else:
                        cf_lines += f'<div class="cdiff-ctx"> {escaped}</div>'

            commit_diff_html += (
                f'<details class="cdiff-file">'
                f'<summary class="cdiff-file-head">'
                f'<svg class="chev" viewBox="0 0 16 16"><path d="M6 4l4 4-4 4" '
                f'fill="none" stroke="currentColor" stroke-width="1.5" '
                f'stroke-linecap="round"/></svg>'
                f'<span class="file-path">{html.escape(cf["filename"])}</span>'
                f'<span class="file-badge {cf_class}">{cf_status}</span>'
                f'<span class="diff-stat">'
                f'<span class="plus">+{cf.get("additions", 0)}</span> '
                f'<span class="minus">-{cf.get("deletions", 0)}</span></span>'
                f'</summary>'
                f'<div class="cdiff-body">{cf_lines}</div>'
                f'</details>\n')

        msg_body = ''
        if msg_rest:
            msg_escaped = html.escape(msg_rest, quote=True)
            msg_body = f'<div class="disc-body" data-body="{msg_escaped}"></div>'

        badge_user = _user_badge(author_login) if author_login else f'<strong>{html.escape(author)}</strong>'
        discussion_html += (
            f'<div class="disc-card disc-commit" id="disc-commit-{sha}">'
            f'<div class="disc-head">'
            f'{badge_user}'
            f'<span class="disc-label">commit</span>'
            f'<span class="disc-date">{date}</span>'
            f'</div>'
            f'<div class="commit-title">'
            f'<code class="commit-sha">{sha}</code> {html.escape(msg_first)}'
            f'<span class="diff-stat" style="margin-left:8px">'
            f'<span class="plus">+{total_add}</span> '
            f'<span class="minus">-{total_del}</span> '
            f'<span style="color:var(--muted)">{len(commit_files)} files</span>'
            f'</span></div>'
            f'{msg_body}'
            f'<div class="commit-files">{commit_diff_html}</div>'
            f'</div>\n')

    disc_count += len(commits)

    # Reviews
    for rv in reviews:
        if not rv.get('body') and rv.get('state') == 'COMMENTED':
            continue
        rid = rv.get('id', 0)
        user = rv.get('user', '')
        state = rv.get('state', '')
        state_class = {'APPROVED': 'approved', 'CHANGES_REQUESTED': 'changes',
                       'COMMENTED': 'commented'}.get(state, '')
        state_label = state.lower().replace('_', ' ')
        date = (rv.get('submitted_at', '') or '')[:10]
        body_escaped = html.escape(rv.get('body', '') or '(no comment)', quote=True)
        discussion_html += f'''<div class="disc-card" id="disc-review-{rid}">
            <div class="disc-head">
                {_user_badge(user)}
                <span class="disc-label {state_class}">{state_label}</span>
                <span class="disc-date">{date}</span>
            </div>
            <div class="disc-body" data-body="{body_escaped}"></div>
        </div>\n'''

    # Comments
    for c in comments:
        cid = c.get('id', 0)
        is_highlighted = str(cid) == str(highlight_comment_id)
        hl_class = ' disc-highlight' if is_highlighted else ''
        user = c.get('user', '')
        date = (c.get('created_at', '') or '')[:10]
        body_escaped = html.escape(c.get('body', '') or '', quote=True)
        ctype = c.get('type', 'issue')
        file_context = ''
        if ctype == 'review' and c.get('path'):
            line_info = f":{c['line']}" if c.get('line') else ''
            file_context = (f'<div class="disc-file"><code>'
                            f'{html.escape(c["path"])}{line_info}</code></div>')
        discussion_html += f'''<div class="disc-card{hl_class}" id="disc-comment-{cid}">
            <div class="disc-head">
                {_user_badge(user)}
                <span class="disc-label">{ctype}</span>
                <span class="disc-date">{date}</span>
            </div>
            {file_context}
            <div class="disc-body" data-body="{body_escaped}"></div>
        </div>\n'''

    # ---- File list HTML (for sidebar) ----
    file_list_html = ''
    for f in files:
        status_badge = {'added': '+', 'modified': '~', 'removed': '-',
                        'renamed': 'R'}.get(f['status'], '?')
        status_class = {'added': 'added', 'modified': 'modified',
                        'removed': 'removed'}.get(f['status'], '')
        file_id = f['filename'].replace('/', '_').replace('.', '_')
        stats = f'+{f["additions"]}' if f["additions"] else ''
        if f["deletions"]:
            stats += f' -{f["deletions"]}'
        file_list_html += (
            f'<div class="file-item {status_class}" '
            f'data-filename="{html.escape(f["filename"])}" '
            f'data-dir="{html.escape("/".join(f["filename"].split("/")[:-1]))}" '
            f'data-target="{file_id}" onclick="jumpToFile(\'{file_id}\')">'
            f'<span class="status-badge {status_class}">{status_badge}</span>'
            f'<span class="filename" title="{html.escape(f["filename"])}">'
            f'{html.escape(f["filename"].split("/")[-1])}</span>'
            f'<span class="file-stats">{stats}</span>'
            f'<button class="review-btn" '
            f'onclick="event.stopPropagation();toggleReviewed(this,\'{file_id}\')" '
            f'title="Mark reviewed">&#10003;</button>'
            f'</div>\n')

    # ---- Diff sections HTML ----
    diff_sections = ''
    for f in files:
        file_id = f['filename'].replace('/', '_').replace('.', '_')
        status_class = {'added': 'added', 'modified': 'modified',
                        'removed': 'removed'}.get(f['status'], '')

        if f.get('patch'):
            hunks = parse_patch(f['patch'])
            total_lines = sum(len(h['lines']) + 1 for h in hunks)
            is_large = total_lines > 300
            hunks_html = ''
            line_count = 0
            fn_js = html.escape(f['filename'].replace("'", "\\'"), quote=True)
            fn_attr = html.escape(f['filename'], quote=True)
            f_status = f.get('status', 'modified')

            for hi, hunk in enumerate(hunks):
                if hi == 0 and hunk['new_start'] > 1:
                    hidden = hunk['new_start'] - 1
                    hunks_html += (
                        f'<tr class="expand-row"><td colspan="4">'
                        f'<button class="expand-btn" onclick="expandCtx(\'{fn_js}\','
                        f'1,{hunk["new_start"]},1,\'{f_status}\',this)">'
                        f'\u2195 {hidden} hidden lines (top)</button></td></tr>\n')
                elif hi > 0:
                    prev = hunks[hi - 1]
                    gap_new_s = prev['new_start'] + prev['new_count']
                    gap_old_s = prev['old_start'] + prev['old_count']
                    gap_new_e = hunk['new_start']
                    hidden = gap_new_e - gap_new_s
                    if hidden > 0:
                        hunks_html += (
                            f'<tr class="expand-row"><td colspan="4">'
                            f'<button class="expand-btn" onclick="expandCtx(\'{fn_js}\','
                            f'{gap_new_s},{gap_new_e},{gap_old_s},\'{f_status}\',this)">'
                            f'\u2195 {hidden} hidden lines</button></td></tr>\n')

                old_num = hunk['old_start']
                new_num = hunk['new_start']
                lines_html = (f'<tr class="hunk-header"><td colspan="4">'
                              f'{html.escape(hunk["header"])}</td></tr>\n')
                line_count += 1
                for ltype, text in hunk['lines']:
                    escaped = html.escape(text)
                    if is_large and line_count == 300:
                        lines_html += (
                            f'</tbody></table><div class="show-more-wrap" '
                            f'id="showmore-{file_id}"><button class="show-more-btn" '
                            f'onclick="showAllLines(this, \'{file_id}\')">'
                            f'Show all {total_lines:,} lines '
                            f'({f["additions"]:,}+ / {f["deletions"]:,}-)</button></div>'
                            f'<table class="diff-table diff-hidden" id="rest-{file_id}">'
                            f'<tbody>')
                    if ltype == 'add':
                        lines_html += (
                            f'<tr class="line-add" data-path="{fn_attr}" '
                            f'data-line="{new_num}" data-side="RIGHT">'
                            f'<td class="ln"></td><td class="ln">{new_num}</td>'
                            f'<td class="sign">+</td><td class="code">{escaped}</td></tr>\n')
                        new_num += 1
                    elif ltype == 'del':
                        lines_html += (
                            f'<tr class="line-del" data-path="{fn_attr}" '
                            f'data-line="{old_num}" data-side="LEFT">'
                            f'<td class="ln">{old_num}</td><td class="ln"></td>'
                            f'<td class="sign">-</td><td class="code">{escaped}</td></tr>\n')
                        old_num += 1
                    else:
                        lines_html += (
                            f'<tr class="line-ctx" data-path="{fn_attr}" '
                            f'data-line="{new_num}" data-side="RIGHT">'
                            f'<td class="ln">{old_num}</td><td class="ln">{new_num}</td>'
                            f'<td class="sign"> </td><td class="code">{escaped}</td></tr>\n')
                        old_num += 1
                        new_num += 1
                    line_count += 1
                hunks_html += lines_html

            diff_content = f'<table class="diff-table"><tbody>{hunks_html}</tbody></table>'
        else:
            diff_content = ('<div class="large-file-notice">'
                            'No diff available &mdash; binary or empty file</div>')

        dir_part = '/'.join(f['filename'].split('/')[:-1])
        base_part = f['filename'].split('/')[-1]
        dir_prefix = html.escape(dir_part + '/') if dir_part else ''

        diff_sections += (
            f'<div class="file-section" id="file-{file_id}" '
            f'data-filename="{html.escape(f["filename"])}">'
            f'<div class="file-header {status_class}" onclick="toggleSection(this)">'
            f'<svg class="chev" viewBox="0 0 16 16"><path d="M6 4l4 4-4 4" '
            f'fill="none" stroke="currentColor" stroke-width="1.5" '
            f'stroke-linecap="round"/></svg>'
            f'<span class="file-path"><span class="fp-dir">{dir_prefix}</span>'
            f'{html.escape(base_part)}</span>'
            f'<span class="file-badge {status_class}">{f["status"]}</span>'
            f'<span class="diff-stat"><span class="plus">+{f["additions"]}</span> '
            f'<span class="minus">-{f["deletions"]}</span></span></div>'
            f'<div class="file-body">{diff_content}</div></div>\n')

    # ---- JSON data for client-side features ----
    pr_meta_json = json.dumps({
        "owner": owner, "repo": repo, "pr_num": pr_num,
        "head_sha": meta.get("head_sha", ""),
        "base_sha": meta.get("base_sha", "")})
    file_index_json = json.dumps([{
        'id': f['filename'].replace('/', '_').replace('.', '_'),
        'filename': f['filename'],
        'basename': f['filename'].split('/')[-1],
        'dir': '/'.join(f['filename'].split('/')[:-1]),
        'status': f['status'],
    } for f in files])

    # ---- Merge readiness bar ----
    merge_bar_html = ''
    pr_state = meta.get('state', 'open')
    pr_merged = meta.get('merged', False)
    pr_draft = meta.get('draft', False)
    pr_mergeable = meta.get('mergeable')
    pr_mergeable_state = meta.get('mergeable_state', '')
    checks = meta.get('checks', [])

    if pr_merged:
        badge_class, badge_text = 'merged', 'Merged'
    elif pr_state == 'closed':
        badge_class, badge_text = 'closed', 'Closed'
    elif pr_draft:
        badge_class, badge_text = 'draft', 'Draft'
    elif pr_mergeable is False:
        badge_class, badge_text = 'conflict', 'Has conflicts'
    elif pr_mergeable_state == 'blocked':
        badge_class, badge_text = 'blocked', 'Blocked'
    elif pr_mergeable_state == 'clean' or (pr_mergeable and pr_mergeable_state != 'unstable'):
        badge_class, badge_text = 'ready', 'Ready to merge'
    else:
        badge_class, badge_text = 'blocked', pr_mergeable_state.replace('_', ' ').title() or 'Checking...'

    checks_html = ''
    for ck in checks:
        cstatus = ck.get('conclusion') or ck.get('status', 'pending')
        cclass = {'success': 'success', 'failure': 'failure', 'action_required': 'failure',
                  'cancelled': 'failure', 'timed_out': 'failure'}.get(cstatus, 'pending')
        csym = {'success': '\u2713', 'failure': '\u2717'}.get(cclass, '\u25cb')
        checks_html += f'<span class="check-item {cclass}">{csym} {html.escape(ck.get("name", ""))}</span>'

    can_merge = (pr_state == 'open' and not pr_merged and not pr_draft
                 and pr_mergeable is not False)

    # Sidebar merge section
    sidebar_merge_html = (
        f'<div class="sb-row"><span class="sb-label">Status</span>'
        f'<span class="merge-badge {badge_class}">{badge_text}</span></div>')
    if checks_html:
        sidebar_merge_html += f'<div class="sb-checks">{checks_html}</div>'
    if can_merge:
        sidebar_merge_html += (
            '<div class="sb-merge" id="sbMerge">'
            '<select class="merge-select" id="mergeMethod">'
            '<option value="squash">Squash</option>'
            '<option value="merge">Merge</option>'
            '<option value="rebase">Rebase</option></select>'
            '<button class="merge-btn merge" id="mergeBtn" onclick="doMerge()">Merge</button>'
            '</div>')

    # ---- HTML page ----
    page_html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PR #{pr_num}: {html.escape(meta.get("title", ""))}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
:root {{
  --bg:#0b0d0b; --fg:#e5e5e0; --border:rgba(135,139,134,.12); --muted:#9ca49c;
  --card:rgba(11,13,11,.02); --user-bg:rgba(255,255,255,.04);
  --code-bg:#1a1c1a;
  --green:#22c55e; --red:#bd2b2b; --link:#75dbf0; --radius:6px;
  --mono:"JetBrains Mono","Berkeley Mono","Fira Code","SF Mono",monospace;
  --sans:"Inter",ui-sans-serif,system-ui,-apple-system,sans-serif;
  --diff-add-bg:rgba(34,197,94,.1); --diff-add-fg:#22c55e;
  --diff-del-bg:rgba(239,68,68,.1); --diff-del-fg:#ef4444;
}}
@media(prefers-color-scheme:light){{
  :root{{--bg:#fafaf8;--fg:#1a1a1a;--muted:#595959;--border:rgba(135,139,134,.2);
    --card:rgba(246,255,245,.03);--user-bg:rgba(0,0,0,.03);
    --code-bg:#f4f4f0;--green:#16a34a;--red:#d44444;--link:#0969da;
    --diff-add-bg:rgba(34,197,94,.1);--diff-add-fg:#16a34a;
    --diff-del-bg:rgba(239,68,68,.1);--diff-del-fg:#dc2626;}}
}}
*{{margin:0;padding:0;box-sizing:border-box}}
html{{font-size:14px}}
body{{font-family:var(--sans);background:var(--bg);color:var(--fg);line-height:1.6;
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}}

/* Layout (transcript style) */
.wrap{{display:flex;flex-direction:column;min-height:100vh}}
.main{{flex:1;display:flex;justify-content:center;padding:24px 16px 80px;gap:24px}}
.content{{flex:1;min-width:0}}

/* Header (transcript style) */
header{{border-bottom:1px solid var(--border);padding-bottom:20px;margin-bottom:0}}
.h-row{{display:flex;align-items:center;gap:8px}}
h1{{font-size:1.5rem;font-weight:600;letter-spacing:-.02em}}
h1 a{{color:var(--link);text-decoration:none}}
h1 a:hover{{text-decoration:underline}}
.meta{{display:flex;flex-wrap:wrap;gap:12px;margin-top:10px;color:var(--muted);font-size:.8rem}}
.mi{{display:inline-flex;align-items:center;gap:5px}}
.mi svg{{width:14px;height:14px;opacity:.7;flex-shrink:0}}
.stat-add{{color:var(--green);font-family:var(--mono);font-weight:600}}
.stat-del{{color:var(--red);font-family:var(--mono);font-weight:600}}
.stat-neutral{{color:var(--muted);font-family:var(--mono)}}
/* Sidebar toggle (mobile) */
.sb-toggle{{display:none;background:none;border:none;color:var(--muted);cursor:pointer;
  padding:2px;line-height:0;transition:color .15s}}
.sb-toggle:hover{{color:var(--fg)}}

/* Tabs */
.tabs{{display:flex;gap:0;margin-top:20px;border-bottom:2px solid var(--border)}}
.tab{{padding:10px 24px;background:none;border:none;border-bottom:2px solid transparent;
  margin-bottom:-2px;color:var(--muted);cursor:pointer;font-family:var(--sans);font-size:.875rem;
  font-weight:500;transition:all .15s}}
.tab:hover{{color:var(--fg)}}
.tab.active{{color:var(--link);border-bottom-color:var(--link)}}
.tab-content{{display:none}}
.tab-content.active{{display:block}}

/* Sidebar (right, transcript style) */
.sidebar{{width:280px;flex-shrink:0;position:sticky;top:24px;align-self:flex-start;
  max-height:calc(100vh - 48px);font-size:.8rem;color:var(--muted)}}
.sidebar-inner{{border:1px solid var(--border);border-radius:10px;padding:16px;
  background:var(--card);display:flex;flex-direction:column;gap:8px;
  max-height:calc(100vh - 64px);overflow:hidden}}
.sb-title{{font-weight:600;color:var(--fg);font-size:.85rem;margin-bottom:4px}}
.sb-row{{display:flex;justify-content:space-between;align-items:center;font-size:.8rem}}
.sb-label{{color:var(--muted)}}
.sb-val{{color:var(--fg);font-weight:500;font-family:var(--mono);font-size:.75rem}}
.sb-divider{{border-top:1px solid var(--border);margin:4px 0}}

/* Search box in sidebar */
.search-box input{{
  width:100%;padding:7px 10px;border:1px solid var(--border);border-radius:6px;
  background:var(--card);color:var(--fg);font-size:.8rem;font-family:var(--sans);
  outline:none;transition:border-color .15s}}
.search-box input::placeholder{{color:var(--muted)}}
.search-box input:focus{{border-color:var(--link)}}

/* File list in sidebar */
.file-list{{overflow-y:auto;flex:1;margin:0 -4px}}
.file-item{{display:flex;align-items:center;padding:5px 8px;cursor:pointer;
  font-size:.8rem;gap:6px;border-radius:var(--radius);transition:background .1s}}
.file-item:hover{{background:var(--user-bg)}}
.file-item.hidden{{display:none}}
.status-badge{{width:16px;text-align:center;font-weight:600;font-size:.7rem;
  flex-shrink:0;font-family:var(--mono)}}
.status-badge.added{{color:var(--green)}}
.status-badge.modified{{color:var(--link)}}
.status-badge.removed{{color:var(--red)}}
.filename{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}}
.file-stats{{color:var(--muted);font-size:.65rem;flex-shrink:0;font-family:var(--mono)}}
.file-item.active{{background:rgba(117,219,240,.08);border-left:2px solid var(--link)}}
.file-item.reviewed{{opacity:.45}}
.review-btn{{background:none;border:1px solid var(--border);border-radius:50%;
  width:18px;height:18px;color:var(--muted);cursor:pointer;font-size:.6rem;
  display:flex;align-items:center;justify-content:center;flex-shrink:0;
  transition:all .15s;padding:0;line-height:1}}
.review-btn:hover{{border-color:var(--green);color:var(--green)}}
.review-btn.checked{{background:var(--green);border-color:var(--green);color:var(--bg)}}

/* Progress bar */
.progress-bar{{font-size:.7rem;color:var(--muted)}}
.progress-track{{height:3px;background:var(--border);border-radius:2px;margin-top:4px;overflow:hidden}}
.progress-fill{{height:100%;background:var(--green);border-radius:2px;transition:width .3s ease}}

/* Bulk actions */
.bulk-actions{{display:flex;gap:4px;flex-wrap:wrap}}
.bulk-actions button{{
  padding:3px 8px;background:var(--user-bg);border:1px solid var(--border);
  border-radius:var(--radius);color:var(--fg);cursor:pointer;font-size:.7rem;
  font-family:var(--sans);transition:background .15s,border-color .15s}}
.bulk-actions button:hover{{background:var(--border);border-color:var(--muted)}}

/* Search results (main content) */
.search-results{{padding:14px 0;display:none}}
.search-results.active{{display:block}}
.search-results h3{{font-size:.8rem;color:var(--muted);margin-bottom:8px;font-weight:500}}
.search-hit{{
  padding:7px 12px;margin:4px 0;background:var(--user-bg);border:1px solid var(--border);
  border-radius:var(--radius);cursor:pointer;font-size:.8rem;transition:background .1s}}
.search-hit:hover{{background:var(--border)}}
.search-hit .hit-file{{color:var(--link);font-weight:500}}
.search-hit .hit-line{{color:var(--muted)}}
.search-hit mark{{background:rgba(117,219,240,.15);color:var(--link);padding:0 2px;border-radius:2px}}

/* File sections */
.file-section{{border:1px solid var(--border);border-radius:var(--radius);margin-top:12px;
  overflow:hidden;content-visibility:auto;contain-intrinsic-size:auto 500px}}
.file-header{{
  padding:10px 16px;background:var(--code-bg);cursor:pointer;display:flex;
  align-items:center;gap:8px;border-bottom:1px solid var(--border);transition:background .1s}}
.file-header:hover{{background:var(--user-bg)}}
.chev{{width:14px;height:14px;transition:transform .15s ease;flex-shrink:0;color:var(--muted)}}
.file-header.open .chev{{transform:rotate(90deg)}}
.file-path{{font-family:var(--mono);font-size:.8rem;flex:1;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}}
.fp-dir{{color:var(--muted);opacity:.6}}
.file-badge{{font-size:.65rem;padding:2px 8px;border-radius:999px;font-weight:500}}
.file-badge.added{{background:var(--diff-add-bg);color:var(--diff-add-fg)}}
.file-badge.modified{{background:rgba(117,219,240,.1);color:var(--link)}}
.file-badge.removed{{background:var(--diff-del-bg);color:var(--diff-del-fg)}}
.diff-stat{{font-family:var(--mono);font-size:.7rem;flex-shrink:0;display:inline-flex;gap:4px}}
.diff-stat .plus{{color:var(--green)}}.diff-stat .minus{{color:var(--red)}}
.file-body{{overflow-x:auto}}
.file-header:not(.open) + .file-body{{display:none}}
.large-file-notice{{padding:24px 16px;color:var(--muted);font-size:.85rem}}
.diff-hidden{{display:none}}
.show-more-wrap{{padding:8px 16px;text-align:center;border-top:1px dashed var(--border)}}
.show-more-btn{{padding:6px 16px;background:var(--card);border:1px solid var(--border);
  border-radius:var(--radius);color:var(--link);cursor:pointer;font-family:var(--sans);
  font-size:.8rem;transition:all .15s}}
.show-more-btn:hover{{background:var(--user-bg);border-color:var(--link)}}

/* Expand context rows */
.expand-row td{{padding:4px 10px;text-align:center;background:var(--code-bg);
  border-top:1px dashed var(--border);border-bottom:1px dashed var(--border)}}
.expand-btn{{padding:3px 14px;background:transparent;border:1px solid var(--border);
  border-radius:var(--radius);color:var(--muted);cursor:pointer;font-family:var(--mono);
  font-size:.7rem;transition:all .15s}}
.expand-btn:hover{{background:var(--user-bg);border-color:var(--link);color:var(--link)}}

/* Inline comment form */
tr[data-path] td.ln{{cursor:pointer;position:relative}}
tr[data-path] td.ln:hover{{color:var(--link);opacity:1 !important}}
tr[data-path] td.ln:hover::after{{content:'+';position:absolute;left:2px;top:50%;
  transform:translateY(-50%);font-size:.6rem;font-weight:700;color:var(--link)}}
.comment-form-row td{{padding:0 !important}}
.inline-comment-form{{padding:10px 16px;background:var(--code-bg);
  border-top:2px solid var(--link);border-bottom:2px solid var(--link)}}
.inline-comment-form textarea{{width:100%;min-height:60px;padding:8px 10px;
  border:1px solid var(--border);border-radius:var(--radius);background:var(--bg);
  color:var(--fg);font-family:var(--sans);font-size:.8rem;resize:vertical}}
.inline-comment-form textarea:focus{{border-color:var(--link);outline:none}}
.comment-actions{{display:flex;gap:6px;margin-top:6px;justify-content:flex-end}}
.comment-submit{{padding:5px 14px;background:var(--green);border:none;
  border-radius:var(--radius);color:#fff;cursor:pointer;font-family:var(--sans);
  font-size:.75rem;font-weight:500}}
.comment-submit:hover{{opacity:.9}}
.comment-submit:disabled{{opacity:.5;cursor:not-allowed}}
.comment-cancel{{padding:5px 14px;background:transparent;border:1px solid var(--border);
  border-radius:var(--radius);color:var(--muted);cursor:pointer;font-family:var(--sans);
  font-size:.75rem}}
.comment-cancel:hover{{border-color:var(--muted)}}
.inline-comment-posted{{padding:8px 16px;font-size:.8rem;color:var(--green);
  background:var(--diff-add-bg)}}
.merge-badge{{display:inline-flex;align-items:center;padding:4px 8px;
  border-radius:6px;font-weight:400;font-size:.72rem}}
.merge-badge.ready{{background:rgba(34,197,94,.15);color:var(--green)}}
.merge-badge.blocked{{background:rgba(245,158,11,.12);color:#f59e0b}}
.merge-badge.conflict{{background:rgba(189,43,43,.15);color:var(--red)}}
.merge-badge.merged{{background:rgba(130,80,223,.15);color:#a78bfa}}
.merge-badge.draft{{background:rgba(156,164,156,.12);color:var(--muted)}}
.merge-badge.closed{{background:rgba(189,43,43,.15);color:var(--red)}}
.sb-checks{{display:flex;gap:5px;flex-wrap:wrap;margin:6px 0}}
.check-item{{font-size:.65rem;padding:1px 6px;border-radius:6px;
  border:1px solid var(--border);font-family:var(--sans)}}
.check-item.success{{color:var(--green);border-color:rgba(34,197,94,.3);background:rgba(34,197,94,.08)}}
.check-item.failure{{color:var(--red);border-color:rgba(189,43,43,.3);background:rgba(189,43,43,.08)}}
.check-item.pending{{color:var(--muted)}}
.sb-merge{{display:flex;gap:6px;margin:6px 0;align-items:center}}
.merge-select{{flex:1;padding:4px 6px;border:1px solid var(--border);border-radius:6px;
  background:var(--bg);color:var(--fg);font-size:.72rem;font-family:var(--sans)}}
.merge-btn{{padding:4px 12px;border:none;border-radius:6px;color:#fff;
  cursor:pointer;font-family:var(--sans);font-size:.72rem;font-weight:500;transition:all .15s}}
.merge-btn.merge{{background:var(--green)}}
.merge-btn.merge:hover{{opacity:.85}}
.merge-btn:disabled{{opacity:.4;cursor:not-allowed}}
.chat-input{{position:sticky;bottom:0;z-index:40;
  background:var(--bg);box-shadow:0 -2px 8px rgba(0,0,0,.1);
  padding:12px 16px;display:flex;gap:8px;align-items:flex-end}}
.chat-input textarea{{flex:1;min-height:72px;max-height:200px;padding:8px 12px;
  border:1px solid var(--border);border-radius:8px;background:var(--card);
  color:var(--fg);font-family:var(--sans);font-size:.85rem;resize:none;
  line-height:1.4;outline:none;transition:border-color .15s}}
.chat-input textarea:focus{{border-color:var(--link)}}
.chat-input textarea::placeholder{{color:var(--muted)}}
.chat-send{{width:32px;height:32px;border-radius:8px;border:1px solid var(--border);
  background:var(--card);color:var(--muted);cursor:pointer;display:flex;
  align-items:center;justify-content:center;flex-shrink:0;transition:all .15s}}
.chat-send:hover{{border-color:var(--link);color:var(--link)}}
.chat-send:disabled{{opacity:.3;cursor:not-allowed}}
.chat-send svg{{width:14px;height:14px}}
.chat-status{{position:absolute;top:-24px;left:12px;font-size:.72rem;
  color:var(--green);opacity:0;transition:opacity .3s}}
.chat-status.show{{opacity:1}}

/* Diff table */
.diff-table{{width:100%;border-collapse:collapse;font-family:var(--mono);
  font-size:.75rem;line-height:1.65}}
.diff-table td{{padding:0 10px;white-space:pre-wrap;word-break:break-all}}
.diff-table .ln{{width:48px;min-width:48px;text-align:right;color:var(--muted);
  opacity:.5;user-select:none;vertical-align:top}}
.diff-table .sign{{width:16px;min-width:16px;text-align:center;user-select:none}}
.diff-table .code{{width:100%}}
.line-add{{background:var(--diff-add-bg)}}
.line-add .sign{{color:var(--diff-add-fg)}}
.line-add .code{{color:var(--diff-add-fg)}}
.line-del{{background:var(--diff-del-bg)}}
.line-del .sign{{color:var(--diff-del-fg)}}
.line-del .code{{color:var(--diff-del-fg)}}
.line-ctx{{background:transparent}}
.hunk-header{{background:var(--code-bg)}}
.hunk-header td{{color:var(--link);font-size:.75rem;padding:6px 10px;opacity:.7}}
tr.highlight{{outline:2px solid var(--link)}}
tr.search-match td.code mark{{background:rgba(117,219,240,.15);color:var(--link)}}

/* Discussion cards */
.disc-thread{{display:flex;flex-direction:column;gap:12px;padding-top:16px}}
.disc-card{{border:1px solid var(--border);border-radius:10px;padding:16px 20px;
  background:var(--card)}}
.disc-highlight{{border-color:var(--link);background:rgba(117,219,240,.04)}}
.disc-head{{display:flex;align-items:center;gap:8px;margin-bottom:12px;
  padding-bottom:10px;border-bottom:1px solid var(--border);flex-wrap:wrap}}
.disc-av{{width:28px;height:28px;border-radius:50%;flex-shrink:0;
  border:1px solid var(--border)}}
.disc-user-info{{font-size:.8rem;line-height:1.4;flex:1;min-width:0}}
.disc-user-info strong{{color:var(--fg)}}
.disc-user-meta{{color:var(--muted);font-size:.7rem}}
.disc-user-bio{{color:var(--muted);font-size:.7rem;font-style:italic}}
.disc-label{{font-size:.65rem;padding:2px 8px;border-radius:999px;
  background:var(--user-bg);color:var(--muted);font-weight:500;flex-shrink:0}}
.disc-label.approved{{background:var(--diff-add-bg);color:var(--diff-add-fg)}}
.disc-label.changes{{background:var(--diff-del-bg);color:var(--diff-del-fg)}}
.disc-date{{font-size:.75rem;color:var(--muted);margin-left:auto;flex-shrink:0}}
.disc-file{{margin-bottom:8px}}
.disc-file code{{font-family:var(--mono);font-size:.8rem;color:var(--link);
  background:var(--code-bg);padding:2px 6px;border-radius:3px}}
.disc-body{{font-size:.875rem;line-height:1.6;color:var(--fg);word-break:break-word}}
.disc-body p{{margin:.4em 0}}
.disc-body h1,.disc-body h2,.disc-body h3{{color:var(--fg);margin:.5em 0 .3em;font-weight:600}}
.disc-body h1{{font-size:1.1em}}.disc-body h2{{font-size:1em}}.disc-body h3{{font-size:.95em}}
.disc-body ul,.disc-body ol{{padding-left:1.5rem;margin:.4em 0}}
.disc-body li{{margin:.2em 0}}
.disc-body code{{background:var(--code-bg);padding:1px 5px;border-radius:3px;
  font-family:var(--mono);font-size:.85em}}
.disc-body pre{{background:var(--code-bg);padding:10px;border-radius:var(--radius);
  overflow-x:auto;margin:.5em 0}}
.disc-body pre code{{background:none;padding:0}}
.disc-body a{{color:var(--link)}}
.disc-body blockquote{{border-left:3px solid var(--border);padding-left:10px;
  color:var(--muted);margin:.4em 0}}
.disc-body img{{max-width:100%;border-radius:var(--radius)}}
.disc-body table{{border-collapse:collapse;margin:.5em 0;font-size:.85em;
  display:block;overflow-x:auto;-webkit-overflow-scrolling:touch;max-width:100%}}
.disc-body th,.disc-body td{{border:1px solid var(--border);padding:6px 10px;white-space:nowrap}}
.disc-body td{{white-space:normal;min-width:80px}}
.disc-body th{{background:var(--user-bg);font-weight:600;white-space:nowrap}}

/* Commit diffs */
.disc-commit .commit-title{{font-size:.875rem;font-weight:500;margin-bottom:8px;line-height:1.5}}
.commit-sha{{font-family:var(--mono);font-size:.75rem;color:var(--link);background:var(--code-bg);
  padding:2px 6px;border-radius:3px;font-weight:400}}
.commit-files{{margin-top:10px;display:flex;flex-direction:column;gap:4px}}
.cdiff-file{{border:1px solid var(--border);border-radius:var(--radius);overflow:hidden}}
.cdiff-file-head{{display:flex;align-items:center;gap:6px;padding:6px 10px;
  background:var(--code-bg);font-size:.8rem;cursor:pointer;user-select:none;
  list-style:none;transition:background .1s}}
.cdiff-file-head::-webkit-details-marker{{display:none}}
.cdiff-file-head:hover{{background:var(--user-bg)}}
.cdiff-file-head .chev{{width:12px;height:12px}}
.cdiff-file-head .file-path{{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.cdiff-file-head .file-badge{{font-size:.6rem;padding:1px 6px}}
.cdiff-file-head .diff-stat{{font-size:.65rem}}
.cdiff-file[open] .chev{{transform:rotate(90deg)}}
.cdiff-body{{border-top:1px solid var(--border);font-family:var(--mono);font-size:.7rem;
  line-height:1.65;overflow-x:auto;background:var(--code-bg)}}
.cdiff-hunk{{color:var(--link);opacity:.7;padding:0 10px}}
.cdiff-add{{background:var(--diff-add-bg);color:var(--diff-add-fg);padding:0 10px;white-space:pre}}
.cdiff-del{{background:var(--diff-del-bg);color:var(--diff-del-fg);padding:0 10px;white-space:pre}}
.cdiff-ctx{{color:var(--muted);padding:0 10px;white-space:pre}}

/* Jump buttons (transcript style) */
.jump{{position:fixed;bottom:68px;right:20px;display:flex;flex-direction:column;gap:6px;z-index:50}}
.jump a{{width:36px;height:36px;border-radius:50%;border:1px solid var(--border);
  background:var(--bg);display:flex;align-items:center;justify-content:center;
  color:var(--muted);text-decoration:none;font-size:1.1rem;transition:all .15s;
  box-shadow:0 2px 8px rgba(0,0,0,.15)}}
.jump a:hover{{border-color:var(--link);color:var(--link);text-decoration:none}}

/* Links */
a{{color:var(--link);text-decoration:none}}
a:hover{{text-decoration:underline}}

/* Mobile */
@media(max-width:900px){{
  .sidebar{{display:none;position:fixed;top:0;right:0;bottom:0;width:280px;z-index:100;
    padding:16px;background:var(--bg);border-left:1px solid var(--border);
    overflow-y:auto;box-shadow:-4px 0 20px rgba(0,0,0,.3)}}
  .sidebar.sb-open{{display:block}}
  .sb-toggle{{display:inline-flex}}
  .file-header{{padding:10px 12px}}
  .diff-table td{{padding:0 4px}}
  .diff-table .ln{{width:28px;min-width:28px;font-size:.65rem}}
  .diff-table{{font-size:.65rem}}
  .disc-card{{padding:12px 14px}}
}}
</style>
</head>
<body>
<div class="wrap">
<div class="main">
<div class="content">
<header>
<div class="h-row">
    <h1><a href="{meta.get('html_url','')}" target="_blank">{html.escape(meta.get('title',''))}</a></h1>
    <button class="sb-toggle" onclick="document.querySelector('.sidebar').classList.toggle('sb-open')" title="Files">
        <svg viewBox="0 0 16 16" fill="currentColor" width="16" height="16"><path d="M1.5 3.25a.75.75 0 000 1.5h13a.75.75 0 000-1.5h-13zm0 4a.75.75 0 000 1.5h13a.75.75 0 000-1.5h-13zm0 4a.75.75 0 000 1.5h13a.75.75 0 000-1.5h-13z"/></svg>
    </button>
</div>
<div class="meta">
    <span class="mi"><svg viewBox="0 0 16 16" fill="currentColor"><path d="M11.93 8.5a4.002 4.002 0 01-7.86 0H.75a.75.75 0 010-1.5h3.32a4.002 4.002 0 017.86 0h3.32a.75.75 0 010 1.5h-3.32zm-1.43-.75a2.5 2.5 0 10-5 0 2.5 2.5 0 005 0z"/></svg>{html.escape(meta.get('head',''))} &rarr; {html.escape(meta.get('base',''))}</span>
    <span class="mi"><span class="stat-add">+{meta.get('additions',0):,}</span></span>
    <span class="mi"><span class="stat-del">-{meta.get('deletions',0):,}</span></span>
    <span class="mi"><span class="stat-neutral">{meta.get('changed_files',0)} files</span></span>
    <span class="mi"><span class="stat-neutral">{meta.get('commits',0)} commits</span></span>
    <span class="mi"><svg viewBox="0 0 16 16" fill="currentColor"><path d="M1.75 1h8.5c.966 0 1.75.784 1.75 1.75v5.5A1.75 1.75 0 0110.25 10H7.061l-2.574 2.573A1.458 1.458 0 012 11.543V10h-.25A1.75 1.75 0 010 8.25v-5.5C0 1.784.784 1 1.75 1z"/></svg>{len(comments) + len(reviews)} comments</span>
</div>
</header>
<div class="tabs">
    <button class="tab active" onclick="switchTab('diff')">Diff ({len(files)})</button>
    <button class="tab" onclick="switchTab('discussion')">Discussion ({disc_count})</button>
</div>
<div class="tab-content active" id="tab-diff">
    <div class="search-results" id="searchResults">
        <h3 id="searchResultsTitle"></h3>
        <div id="searchResultsList"></div>
    </div>
    {diff_sections}
</div>
<div class="tab-content" id="tab-discussion">
    <div class="disc-thread">
        {discussion_html}
    </div>
</div>
</div>
<aside class="sidebar" id="sidebar">
<div class="sidebar-inner">
    <div class="sb-title">PR #{pr_num}</div>
    <div class="sb-row"><span class="sb-label">{html.escape(meta.get('head',''))}</span><span class="sb-val">&rarr; {html.escape(meta.get('base',''))}</span></div>
    <div class="sb-row"><span class="sb-label">Files</span><span class="sb-val">{len(files)}</span></div>
    <div class="sb-row"><span class="sb-label">Changes</span><span class="sb-val" style="color:var(--green)">+{meta.get('additions',0):,}</span></div>
    <div class="sb-divider"></div>
    {sidebar_merge_html}
    <div class="sb-divider"></div>
    <div class="progress-bar">
        <span id="progressText">0 / {len(files)} reviewed</span>
        <div class="progress-track"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>
    </div>
    <div class="sb-divider"></div>
    <div class="search-box">
        <input type="text" id="searchInput" placeholder="Search code, files..." oninput="debounceSearch()">
    </div>
    <div class="bulk-actions">
        <button onclick="expandAll()">Expand</button>
        <button onclick="collapseAll()">Collapse</button>
    </div>
    <div class="sb-divider"></div>
    <div class="file-list">
        {file_list_html}
    </div>
</div>
</aside>
</div>
</div>
<div class="chat-input" id="chatInput">
    <div class="chat-status" id="chatStatus">&check; Comment posted</div>
    <textarea id="generalCommentText" placeholder="Comment on this PR... (Shift+Enter for new line)" rows="3"
      onkeydown="if(event.key==='Enter'&&!event.shiftKey){{event.preventDefault();submitGeneralComment();}}"
      oninput="this.style.height='auto';this.style.height=Math.min(this.scrollHeight,200)+'px'"></textarea>
    <button class="chat-send" onclick="submitGeneralComment()" id="chatSendBtn" title="Send comment">
        <svg viewBox="0 0 16 16" fill="currentColor"><path d="M.989 2.524a.75.75 0 01.87-.206l13 6a.75.75 0 010 1.364l-13 6A.75.75 0 01.855 14.3l1.573-5.26a.25.25 0 01.2-.18L7.3 8.11a.25.25 0 000-.486L2.628 6.88a.25.25 0 01-.2-.18L.855 1.443a.75.75 0 01.134-.92z"/></svg>
    </button>
</div>
<div class="jump">
<a href="#" title="Top" onclick="window.scrollTo(0,0);return false">&uarr;</a>
<a href="#" title="Next file" onclick="jumpNext();return false">&darr;</a>
</div>
<script type="application/json" id="pr-meta">{pr_meta_json}</script>
<script>
const totalFiles = {len(files)};
let reviewedSet = new Set();
const fileIndex = {file_index_json};
const _fcCache = {{}};

function _escHtml(s) {{ const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }}

// Tab switching
function switchTab(name) {{
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.querySelector('.tab[onclick*="' + name + '"]').classList.add('active');
    document.getElementById('tab-' + name).classList.add('active');
}}

// Expand context
async function expandCtx(filename, newStart, newEnd, oldStart, status, btn) {{
    btn.disabled = true;
    btn.textContent = 'Loading...';
    const tr = btn.closest('tr');
    try {{
        let lines = _fcCache[filename];
        if (!lines) {{
            const token = new URLSearchParams(window.location.search).get('token');
            const ref = status === 'removed' ? prMeta.base_sha : prMeta.head_sha;
            const resp = await fetch('/pr-file-content?token=' + encodeURIComponent(token) +
                '&owner=' + encodeURIComponent(prMeta.owner) +
                '&repo=' + encodeURIComponent(prMeta.repo) +
                '&path=' + encodeURIComponent(filename) +
                '&ref=' + encodeURIComponent(ref));
            if (!resp.ok) {{ btn.textContent = 'Failed'; return; }}
            lines = await resp.json();
            _fcCache[filename] = lines;
        }}
        let h = '';
        for (let i = newStart; i < newEnd; i++) {{
            const oldNum = oldStart + (i - newStart);
            const text = _escHtml(lines[i - 1] || '');
            h += '<tr class="line-ctx"><td class="ln">' + oldNum + '</td><td class="ln">' + i + '</td><td class="sign"> </td><td class="code">' + text + '</td></tr>\\n';
        }}
        tr.insertAdjacentHTML('beforebegin', h);
        tr.remove();
    }} catch(e) {{
        btn.textContent = 'Error';
        console.error('expandCtx:', e);
    }}
}}

// Inline commenting
const prMeta = JSON.parse(document.getElementById('pr-meta')?.textContent || '{{}}');

document.addEventListener('click', function(e) {{
    const ln = e.target.closest('td.ln');
    if (!ln) return;
    const tr = ln.closest('tr[data-path]');
    if (!tr) return;
    e.preventDefault();
    openCommentForm(tr);
}});

function openCommentForm(tr) {{
    document.querySelectorAll('.comment-form-row').forEach(r => r.remove());
    const path = tr.dataset.path;
    const line = tr.dataset.line;
    const side = tr.dataset.side;
    if (!path || !line) return;
    const formRow = document.createElement('tr');
    formRow.className = 'comment-form-row';
    formRow.innerHTML = '<td colspan="4"><div class="inline-comment-form">' +
        '<textarea placeholder="Write a comment on ' + _escHtml(path) + ':' + line + '... (use @name to route to worker)" data-path="' + _escHtml(path) + '" data-line="' + line + '" data-side="' + side + '"></textarea>' +
        '<div class="comment-actions"><button class="comment-cancel" onclick="cancelComment(this)">Cancel</button>' +
        '<button class="comment-submit" onclick="submitComment(this)">Comment</button></div></div></td>';
    tr.insertAdjacentElement('afterend', formRow);
    formRow.querySelector('textarea').focus();
}}

function cancelComment(btn) {{ btn.closest('.comment-form-row').remove(); }}

async function submitComment(btn) {{
    const form = btn.closest('.inline-comment-form');
    const ta = form.querySelector('textarea');
    const body = ta.value.trim();
    if (!body) return;
    btn.disabled = true;
    btn.textContent = 'Posting...';
    try {{
        const token = new URLSearchParams(window.location.search).get('token');
        const resp = await fetch('/pr-comment', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{
                owner: prMeta.owner, repo: prMeta.repo,
                pr_num: prMeta.pr_num, head_sha: prMeta.head_sha,
                path: ta.dataset.path, line: parseInt(ta.dataset.line),
                side: ta.dataset.side, body: body, token: token
            }})
        }});
        if (resp.ok) {{
            const formRow = btn.closest('.comment-form-row');
            formRow.innerHTML = '<td colspan="4"><div class="inline-comment-posted">\u2713 Comment posted on ' + _escHtml(ta.dataset.path) + ':' + ta.dataset.line + '</div></td>';
        }} else {{
            const err = await resp.text();
            btn.textContent = 'Comment';
            btn.disabled = false;
            alert('Failed: ' + err);
        }}
    }} catch(e) {{
        btn.textContent = 'Comment';
        btn.disabled = false;
        alert('Network error: ' + e.message);
    }}
}}

// Jump to file (auto-switches to Diff tab)
function jumpToFile(id) {{
    switchTab('diff');
    const section = document.getElementById('file-' + id);
    if (!section) return;
    const header = section.querySelector('.file-header');
    if (header && !header.classList.contains('open')) header.classList.add('open');
    setTimeout(function() {{
        section.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
    }}, 50);
    document.querySelectorAll('.file-item').forEach(f => f.classList.remove('active'));
    const item = document.querySelector('.file-item[data-target="'+id+'"]');
    if (item) item.classList.add('active');
    document.querySelector('.sidebar').classList.remove('sb-open');
}}

// Collapse/expand
function toggleSection(header) {{ header.classList.toggle('open'); }}
function expandAll() {{ document.querySelectorAll('.file-header').forEach(h => h.classList.add('open')); }}
function collapseAll() {{ document.querySelectorAll('.file-header').forEach(h => h.classList.remove('open')); }}

// Review tracking
function toggleReviewed(btn, id) {{
    btn.classList.toggle('checked');
    const item = btn.closest('.file-item');
    if (btn.classList.contains('checked')) {{
        reviewedSet.add(id); if (item) item.classList.add('reviewed');
    }} else {{
        reviewedSet.delete(id); if (item) item.classList.remove('reviewed');
    }}
    updateProgress();
}}
function updateProgress() {{
    const n = reviewedSet.size;
    document.getElementById('progressText').textContent = n + ' / ' + totalFiles + ' reviewed';
    document.getElementById('progressFill').style.width = (n / totalFiles * 100) + '%';
}}

// Show all lines for truncated large files
function showAllLines(btn, fileId) {{
    const rest = document.getElementById('rest-' + fileId);
    const wrap = document.getElementById('showmore-' + fileId);
    if (rest) rest.classList.remove('diff-hidden');
    if (wrap) wrap.remove();
}}

// Jump to next file section
function jumpNext() {{
    const sections = [...document.querySelectorAll('#tab-diff .file-section')];
    const scrollTop = window.scrollY || document.documentElement.scrollTop;
    for (const s of sections) {{
        if (s.getBoundingClientRect().top > 60) {{
            s.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
            return;
        }}
    }}
}}

// ---- Search (code + files) ----
let searchTimer;
function debounceSearch() {{ clearTimeout(searchTimer); searchTimer = setTimeout(doSearch, 300); }}

function doSearch() {{
    const query = document.getElementById('searchInput').value.trim().toLowerCase();
    const resultsDiv = document.getElementById('searchResults');
    const listDiv = document.getElementById('searchResultsList');
    const titleDiv = document.getElementById('searchResultsTitle');

    document.querySelectorAll('tr.search-match').forEach(tr => tr.classList.remove('search-match'));
    document.querySelectorAll('td.code mark').forEach(m => m.replaceWith(m.textContent));
    document.querySelectorAll('.file-item.hidden').forEach(i => i.classList.remove('hidden'));
    document.querySelectorAll('.file-section.hidden').forEach(s => s.classList.remove('hidden'));

    if (!query) {{ resultsDiv.classList.remove('active'); return; }}

    // Auto-switch to Diff tab for search
    switchTab('diff');

    const terms = query.split(/\\s+/).filter(t => t.length > 0);
    const allResults = [];

    // File/folder matches
    const fileMatches = [];
    fileIndex.forEach(f => {{
        const lname = f.filename.toLowerCase();
        const lbase = f.basename.toLowerCase();
        const ldir = f.dir.toLowerCase();
        const dirParts = ldir.split('/');
        let score = 0, matched = false;
        terms.forEach(term => {{
            if (lbase === term) {{ score += 10; matched = true; }}
            else if (lbase.indexOf(term) >= 0) {{ score += 5; matched = true; }}
            else if (dirParts.some(p => p === term)) {{ score += 4; matched = true; }}
            else if (ldir.indexOf(term) >= 0) {{ score += 2; matched = true; }}
            else if (lname.indexOf(term) >= 0) {{ score += 1; matched = true; }}
        }});
        if (matched) fileMatches.push({{ ...f, score, type: 'file' }});
    }});
    fileMatches.sort((a,b) => b.score - a.score);

    if (fileMatches.length > 0 && fileMatches.length < fileIndex.length) {{
        const matchedIds = new Set(fileMatches.map(f => f.id));
        document.querySelectorAll('.file-item').forEach(item => {{
            const id = item.dataset.target;
            item.classList.toggle('hidden', !matchedIds.has(id));
        }});
        document.querySelectorAll('.file-section').forEach(s => {{
            const fn = s.dataset.filename;
            const id = fn ? fn.replace(/\\//g, '_').replace(/\\./g, '_') : '';
            s.classList.toggle('hidden', id && !matchedIds.has(id));
        }});
    }}

    fileMatches.slice(0, 15).forEach(f => {{
        allResults.push({{ type: 'file', filename: f.filename, id: f.id, score: f.score + 100 }});
    }});

    // Code content matches
    document.querySelectorAll('.file-section').forEach(section => {{
        const filename = section.dataset.filename;
        section.querySelectorAll('tr').forEach(tr => {{
            const codeTd = tr.querySelector('td.code');
            if (!codeTd) return;
            const text = codeTd.textContent.toLowerCase();
            let score = 0, matched = false;
            terms.forEach(term => {{
                if (text.indexOf(term) >= 0) {{
                    matched = true;
                    const tf = (text.split(term).length - 1);
                    const dl = text.length, k1 = 1.2, b = 0.75, avgdl = 60;
                    score += (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avgdl));
                }}
            }});
            if (tr.classList.contains('line-add') || tr.classList.contains('line-del')) score *= 1.5;
            if (matched) allResults.push({{ type: 'code', tr, filename, text: codeTd.textContent, score }});
        }});
    }});

    allResults.sort((a, b) => b.score - a.score);
    const top = allResults.slice(0, 60);

    const fileCount = top.filter(r => r.type === 'file').length;
    const codeCount = top.filter(r => r.type === 'code').length;
    let summary = '';
    if (fileCount > 0) summary += fileCount + ' files';
    if (fileCount > 0 && codeCount > 0) summary += ', ';
    if (codeCount > 0) summary += codeCount + ' code matches';
    if (allResults.length > 60) summary += ' (showing top 60 of ' + allResults.length + ')';
    titleDiv.textContent = summary;

    listDiv.innerHTML = '';
    top.forEach(r => {{
        const div = document.createElement('div');
        div.className = 'search-hit';
        if (r.type === 'file') {{
            let hl = r.filename;
            terms.forEach(term => {{
                const re = new RegExp('(' + term.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&') + ')', 'gi');
                hl = hl.replace(re, '<mark>$1</mark>');
            }});
            div.innerHTML = '<span class="hit-file" style="font-family:var(--mono);font-size:.75rem">' + hl + '</span>';
            div.onclick = () => jumpToFile(r.id);
        }} else {{
            r.tr.classList.add('search-match');
            const codeTd = r.tr.querySelector('td.code');
            if (codeTd) {{
                let h = codeTd.innerHTML;
                terms.forEach(term => {{
                    const re = new RegExp('(' + term.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&') + ')', 'gi');
                    h = h.replace(re, '<mark>$1</mark>');
                }});
                codeTd.innerHTML = h;
            }}
            let preview = r.text.trim().substring(0, 120);
            terms.forEach(term => {{
                const re = new RegExp('(' + term.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&') + ')', 'gi');
                preview = preview.replace(re, '<mark>$1</mark>');
            }});
            div.innerHTML = '<span class="hit-file">' + r.filename.split('/').pop() + '</span> <span class="hit-line">' + preview + '</span>';
            div.onclick = () => {{
                const header = r.tr.closest('.file-section')?.querySelector('.file-header');
                if (header && !header.classList.contains('open')) header.classList.add('open');
                setTimeout(() => r.tr.scrollIntoView({{ behavior:'smooth', block:'center' }}), 100);
                r.tr.style.outline = '2px solid var(--link)';
                setTimeout(() => r.tr.style.outline = '', 2000);
            }};
        }}
        listDiv.appendChild(div);
    }});
    resultsDiv.classList.add('active');
}}

// Render all discussion bodies as markdown
(function() {{
    if (typeof marked === 'undefined') return;
    document.querySelectorAll('.disc-body[data-body]').forEach(el => {{
        try {{ el.innerHTML = marked.parse(el.dataset.body); }}
        catch(e) {{ el.textContent = el.dataset.body; }}
    }});
}})();

// Keyboard shortcuts
document.addEventListener('keydown', e => {{
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    if (e.key === '/' || e.key === 's') {{ e.preventDefault(); document.getElementById('searchInput').focus(); }}
    if (e.key === 'j') jumpNext();
    if (e.key === '1') switchTab('diff');
    if (e.key === '2') switchTab('discussion');
    if (e.key === 'Escape') {{
        document.getElementById('searchInput').value = '';
        doSearch();
        document.getElementById('searchInput').blur();
    }}
}});

// General comment
async function submitGeneralComment() {{
    const ta = document.getElementById('generalCommentText');
    const body = ta.value.trim();
    if (!body) return;
    const btn = document.getElementById('chatSendBtn');
    const status = document.getElementById('chatStatus');
    btn.disabled = true;
    try {{
        const token = new URLSearchParams(window.location.search).get('token');
        const resp = await fetch('/pr-general-comment', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{
                owner: prMeta.owner, repo: prMeta.repo,
                pr_num: prMeta.pr_num, body: body, token: token
            }})
        }});
        if (resp.ok) {{
            ta.value = '';
            ta.style.height = '36px';
            status.classList.add('show');
            setTimeout(function(){{ status.classList.remove('show'); }}, 2000);
        }} else {{
            const err = await resp.text();
            status.textContent = 'Failed: ' + err;
            status.style.color = 'var(--red)';
            status.classList.add('show');
            setTimeout(function(){{ status.classList.remove('show'); status.textContent='\u2713 Comment posted'; status.style.color=''; }}, 3000);
        }}
    }} catch(e) {{
        status.textContent = 'Network error';
        status.style.color = 'var(--red)';
        status.classList.add('show');
        setTimeout(function(){{ status.classList.remove('show'); status.textContent='\u2713 Comment posted'; status.style.color=''; }}, 3000);
    }}
    btn.disabled = false;
}}

// Merge PR
async function doMerge() {{
    const btn = document.getElementById('mergeBtn');
    if (!confirm('Merge this PR?')) return;
    const method = document.getElementById('mergeMethod').value;
    btn.disabled = true;
    btn.textContent = '...';
    try {{
        const token = new URLSearchParams(window.location.search).get('token');
        const resp = await fetch('/pr-merge', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{
                owner: prMeta.owner, repo: prMeta.repo,
                pr_num: prMeta.pr_num, merge_method: method, token: token
            }})
        }});
        if (resp.ok) {{
            const m = document.getElementById('sbMerge');
            if (m) m.innerHTML = '<span class="merge-badge merged">Merged</span>';
        }} else {{
            const err = await resp.text();
            alert('Merge failed: ' + err);
            btn.disabled = false;
            btn.textContent = 'Merge';
        }}
    }} catch(e) {{
        alert('Network error: ' + e.message);
        btn.disabled = false;
        btn.textContent = 'Merge';
    }}
}}

// Token keepalive — extend on user interaction
(function() {{
    const token = new URLSearchParams(window.location.search).get('token');
    if (!token) return;
    let lastPing = Date.now();
    let dirty = false;
    function ping() {{
        if (!dirty) return;
        dirty = false;
        lastPing = Date.now();
        fetch('/pr-keepalive?token=' + encodeURIComponent(token)).catch(function(){{}});
    }}
    ['mousemove','keydown','scroll','click','touchstart','focus'].forEach(function(ev) {{
        document.addEventListener(ev, function() {{ dirty = true; }}, {{passive:true,capture:true}});
    }});
    setInterval(ping, 180000);
}})();

// Auto-scroll to highlighted comment if present
{f'setTimeout(function(){{switchTab("discussion");var el=document.getElementById("disc-comment-{highlight_comment_id}");if(el)el.scrollIntoView({{behavior:"smooth",block:"center"}})}},600);' if highlight_comment_id else ''}
</script>
</body>
</html>'''
    return page_html


def _fetch_file_context(owner, repo, path, line, ref, context=3):
    """Fetch file from GitHub and return ±context lines around target line."""
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/contents/{path}?ref={ref}",
             "--jq", ".content"],
            capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return None, ref[:12]
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


def _format_comment_with_context(comment_body, pr_num, path, line, context_snippet, short_sha):
    """Build a rich comment message with file context."""
    parts = [f"PR #{pr_num} comment on {path}:{line} (ref {short_sha})"]
    if context_snippet:
        parts.append(context_snippet)
    parts.append("")
    parts.append(comment_body)
    return '\n'.join(parts)


def _notify_telegram(comment_body, pr_num, path, line, context_snippet=None, short_sha=""):
    """Send PR comment notification to Telegram via bridge's /notify endpoint."""
    bridge_url = os.environ.get("BRIDGE_URL", "http://localhost:8271")
    text = f"\U0001f4ac " + _format_comment_with_context(
        comment_body, pr_num, path, line, context_snippet, short_sha)
    try:
        import urllib.request
        req = urllib.request.Request(
            f"{bridge_url}/notify",
            data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[pr-comment] Telegram notify failed: {e}")


def _route_mentions_to_workers(comment_body, pr_num, path, line, context_snippet=None, short_sha=""):
    """Parse @mentions and send the comment to targeted workers via tmux."""
    tmux_prefix = os.environ.get("TMUX_PREFIX", "claude-prod-")
    mentions = re.findall(r'@(\w+)', comment_body)
    if not mentions:
        return
    try:
        r = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=5)
        active_sessions = set(r.stdout.strip().split('\n')) if r.returncode == 0 else set()
    except Exception:
        active_sessions = set()

    msg = "manager: " + _format_comment_with_context(
        comment_body, pr_num, path, line, context_snippet, short_sha)

    for mention in mentions:
        target_session = f"{tmux_prefix}{mention}"
        if target_session not in active_sessions:
            continue
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
                try:
                    os.unlink(tmpfile)
                except OSError:
                    pass
            print(f"[pr-comment] Routed to @{mention} via tmux")
        except Exception as e:
            print(f"[pr-comment] Failed to route to @{mention}: {e}")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <pr_url_or_number> [--port PORT]", file=sys.stderr)
        sys.exit(1)

    pr_input = sys.argv[1]
    port = 10171

    for i, arg in enumerate(sys.argv):
        if arg == '--port' and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])

    # Parse PR URL or number (supports #issuecomment-XXXXX fragments)
    highlight_comment_id = None
    fragment_match = re.search(r'#issuecomment-(\d+)', pr_input)
    if fragment_match:
        highlight_comment_id = fragment_match.group(1)
    # Strip fragment for URL parsing
    clean_url = pr_input.split('#')[0]

    m = re.match(r'https://github\.com/([^/]+)/([^/]+)/pull/(\d+)', clean_url)
    if m:
        owner, repo, pr_num = m.group(1), m.group(2), int(m.group(3))
    else:
        # Assume BasedHardware/omi
        owner, repo, pr_num = 'BasedHardware', 'omi', int(clean_url)

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
    with open(out_path, 'w') as f:
        f.write(html_content)
    print(f"Written to {out_path}")

    # Serve
    if '--no-serve' not in sys.argv:
        import http.server
        import socketserver
        from urllib.parse import urlparse, parse_qs

        os.chdir('/tmp')
        base_handler = http.server.SimpleHTTPRequestHandler

        class PRHandler(base_handler):
            def log_message(self, fmt, *args):
                pass

            def do_POST(self):
                if self.path == '/pr-comment':
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length)
                    try:
                        data = json.loads(body)
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

                    # Fetch surrounding lines for context
                    context_snippet, short_sha = _fetch_file_context(
                        o, r, path, line, head_sha)

                    # Notify Telegram via bridge
                    _notify_telegram(comment_body, pn, path, line,
                                     context_snippet, short_sha)

                    # Route @mentions to workers via tmux
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

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == '/pr-file-content':
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


if __name__ == '__main__':
    main()
