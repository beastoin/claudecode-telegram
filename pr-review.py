#!/usr/bin/env python3
"""Generate a self-contained HTML PR review page from GitHub API data."""

import json
import html
import sys
import os
import subprocess
import re
from pathlib import Path


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


def fetch_pr_data(owner, repo, pr_num):
    """Fetch PR metadata and file patches via gh CLI."""
    # PR metadata
    meta_raw = subprocess.run(
        ["gh", "api", f"repos/{owner}/{repo}/pulls/{pr_num}",
         "--jq", '{title: .title, body: .body, user: .user.login, '
                  'changed_files: .changed_files, additions: .additions, '
                  'deletions: .deletions, commits: .commits, '
                  'base: .base.ref, head: .head.ref, html_url: .html_url}'],
        capture_output=True, text=True, timeout=30)
    meta = json.loads(meta_raw.stdout)

    # File patches (API truncates large patches)
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

    # For files missing patches, fetch full PR diff and fill them in
    missing = [f for f in files if not f.get('patch') and f.get('additions', 0) > 0]
    if missing:
        print(f"Fetching full diff for {len(missing)} large files...")
        full_diff = _fetch_full_diff(owner, repo, pr_num)
        if full_diff:
            diff_patches = _parse_full_diff(full_diff)
            for f in files:
                if not f.get('patch') and f['filename'] in diff_patches:
                    f['patch'] = diff_patches[f['filename']]

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
                    'new_start': int(m.group(3)),
                    'context': m.group(5),
                    'lines': []
                }
            else:
                current_hunk = {'header': line, 'old_start': 0, 'new_start': 0, 'context': '', 'lines': []}
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
                   user_profiles=None, highlight_comment_id=None,
                   graph_html=None, graph_summary=None):
    """Generate self-contained HTML review page."""
    comments = comments or []
    reviews = reviews or []
    user_profiles = user_profiles or {}

    def _user_badge(username):
        """Generate a user badge with profile info tooltip."""
        p = user_profiles.get(username, {})
        if not p:
            return html.escape(username)
        name = html.escape(p.get('name', '') or username)
        joined = (p.get('created_at', '') or '')[:10]
        followers = p.get('followers', 0)
        repos = p.get('public_repos', 0)
        bio = html.escape((p.get('bio', '') or '')[:80])
        avatar = html.escape(p.get('avatar_url', '') or '')
        avatar_img = f'<img src="{avatar}" class="user-avatar">' if avatar else ''
        return f'''{avatar_img}<span class="user-info"><strong>{name}</strong> <span class="user-meta">@{html.escape(username)} &middot; joined {joined} &middot; {followers:,} followers &middot; {repos} repos</span>{f'<br><span class="user-bio">{bio}</span>' if bio else ''}</span>'''

    # Build discussion sections (PR body + reviews + comments as file-like cards)
    discussion_sections = ''
    discussion_sidebar = ''

    # 1. PR Description as a section
    pr_author = meta.get('user', '')
    pr_body_escaped = html.escape(meta.get('body', '') or '(no description)', quote=True)
    discussion_sections += f'''<div class="file-section discussion-section" id="section-pr-desc" data-filename="PR Description">
        <div class="file-header open" onclick="toggleSection(this)">
            <svg class="chev" viewBox="0 0 16 16"><path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
            <span class="file-path">PR Description</span>
            <span class="file-badge modified">by {html.escape(pr_author)}</span>
        </div>
        <div class="file-body"><div class="discussion-card">
            <div class="disc-author">{_user_badge(pr_author)}</div>
            <div class="disc-body" data-body="{pr_body_escaped}"></div>
        </div></div>
    </div>\n'''
    discussion_sidebar += f'''<div class="file-item" data-target="pr-desc" onclick="jumpToFile('pr-desc')">
        <span class="status-badge modified">D</span>
        <span class="filename">PR Description</span>
    </div>\n'''

    # 2. Reviews (approve/changes/comment)
    for rv in reviews:
        if not rv.get('body') and rv.get('state') == 'COMMENTED':
            continue  # Skip empty review comments
        rid = rv.get('id', 0)
        user = rv.get('user', '')
        state = rv.get('state', '')
        state_icon = {'APPROVED': '+', 'CHANGES_REQUESTED': '-', 'COMMENTED': '~'}.get(state, '?')
        state_class = {'APPROVED': 'added', 'CHANGES_REQUESTED': 'removed', 'COMMENTED': 'modified'}.get(state, '')
        state_label = state.lower().replace('_', ' ')
        date = (rv.get('submitted_at', '') or '')[:10]
        body_escaped = html.escape(rv.get('body', '') or '(no comment)', quote=True)

        discussion_sections += f'''<div class="file-section discussion-section" id="section-review-{rid}" data-filename="Review: {html.escape(user)}">
            <div class="file-header" onclick="toggleSection(this)">
                <svg class="chev" viewBox="0 0 16 16"><path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
                <span class="file-path">Review: {html.escape(user)}</span>
                <span class="file-badge {state_class}">{state_label}</span>
                <span class="diff-stat">{date}</span>
            </div>
            <div class="file-body"><div class="discussion-card">
                <div class="disc-author">{_user_badge(user)}</div>
                <div class="disc-body" data-body="{body_escaped}"></div>
            </div></div>
        </div>\n'''
        discussion_sidebar += f'''<div class="file-item {state_class}" data-target="review-{rid}" onclick="jumpToFile('review-{rid}')">
            <span class="status-badge {state_class}">{state_icon}</span>
            <span class="filename">Review: {html.escape(user)}</span>
        </div>\n'''

    # 3. Comments
    for c in comments:
        cid = c.get('id', 0)
        is_highlighted = str(cid) == str(highlight_comment_id)
        hl_class = ' comment-highlight' if is_highlighted else ''
        user = c.get('user', '')
        date = (c.get('created_at', '') or '')[:10]
        body_escaped = html.escape(c.get('body', '') or '', quote=True)
        ctype = c.get('type', 'issue')

        file_context = ''
        if ctype == 'review' and c.get('path'):
            line_info = f":{c['line']}" if c.get('line') else ''
            file_context = f'<div class="comment-file"><span class="comment-file-path">{html.escape(c["path"])}{line_info}</span></div>'

        discussion_sections += f'''<div class="file-section discussion-section{hl_class}" id="section-comment-{cid}" data-filename="Comment: {html.escape(user)}">
            <div class="file-header" onclick="toggleSection(this)">
                <svg class="chev" viewBox="0 0 16 16"><path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
                <span class="file-path">Comment: {html.escape(user)}</span>
                <span class="file-badge modified">{ctype}</span>
                <span class="diff-stat">{date}</span>
            </div>
            <div class="file-body"><div class="discussion-card">
                <div class="disc-author">{_user_badge(user)}</div>
                {file_context}
                <div class="disc-body" data-body="{body_escaped}"></div>
            </div></div>
        </div>\n'''
        discussion_sidebar += f'''<div class="file-item" data-target="comment-{cid}" onclick="jumpToFile('comment-{cid}')">
            <span class="status-badge modified">C</span>
            <span class="filename">{html.escape(user)}: {date}</span>
        </div>\n'''

    # 4. Graph section (blast radius visualization)
    graph_section = ''
    graph_sidebar = ''
    graph_scripts = ''
    if graph_html and graph_summary:
        gs = graph_summary
        badges = []
        if gs.get('god_nodes_hit'):
            badges.append(f'<span class="file-badge removed">{len(gs["god_nodes_hit"])} god nodes</span>')
        if gs.get('coupling_score', 0) >= 0.3:
            badges.append(f'<span class="file-badge modified">coupling {gs["coupling_score"]:.1f}</span>')
        if gs.get('communities_touched', 0) >= 5:
            badges.append(f'<span class="file-badge added">{gs["communities_touched"]} communities</span>')
        badges_html = ' '.join(badges)

        # Split graph HTML: extract <script> tags to defer them after our JS
        import re as _re
        graph_body = _re.sub(r'<script[\s\S]*?</script>', '', graph_html)
        graph_script_tags = _re.findall(r'<script[\s\S]*?</script>', graph_html)
        # Wrap graph scripts in try/catch so they can't break page JS
        for tag in graph_script_tags:
            if 'src=' in tag:
                graph_scripts += tag + '\n'
            else:
                # Wrap inline scripts in try/catch
                inner = _re.sub(r'^<script>', '', tag)
                inner = _re.sub(r'</script>$', '', inner)
                graph_scripts += f'<script>try{{{inner}}}catch(e){{console.warn("graph error:",e)}}</script>\n'

        graph_section = f'''<div class="file-section discussion-section" id="section-graph" data-filename="Blast Radius Graph">
            <div class="file-header open" onclick="toggleSection(this)">
                <svg class="chev" viewBox="0 0 16 16"><path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
                <span class="file-path">Blast Radius Graph</span>
                {badges_html}
                <span class="diff-stat">{gs.get("files_in_blast_radius", 0)} files affected</span>
            </div>
            <div class="file-body" style="padding:0">{graph_body}</div>
        </div>\n'''
        graph_sidebar = f'''<div class="file-item" data-target="graph" onclick="jumpToFile('graph')">
            <span class="status-badge modified">G</span>
            <span class="filename">Blast Radius Graph</span>
        </div>\n'''

    # Build file list HTML
    file_list_html = ''
    for f in files:
        status_badge = {'added': '+', 'modified': '~', 'removed': '-', 'renamed': 'R'}.get(f['status'], '?')
        status_class = {'added': 'added', 'modified': 'modified', 'removed': 'removed'}.get(f['status'], '')
        file_id = f['filename'].replace('/', '_').replace('.', '_')
        stats = f'+{f["additions"]}' if f["additions"] else ''
        if f["deletions"]:
            stats += f' -{f["deletions"]}'
        file_list_html += f'''<div class="file-item {status_class}" data-filename="{html.escape(f['filename'])}" data-dir="{html.escape('/'.join(f['filename'].split('/')[:-1]))}" data-target="{file_id}" onclick="jumpToFile('{file_id}')">
            <span class="status-badge {status_class}">{status_badge}</span>
            <span class="filename" title="{html.escape(f['filename'])}">{html.escape(f['filename'].split('/')[-1])}</span>
            <span class="file-stats">{stats}</span>
            <button class="review-btn" onclick="event.stopPropagation();toggleReviewed(this,'{file_id}')" title="Mark reviewed">&#10003;</button>
        </div>\n'''

    # Build diff HTML for each file
    diff_sections = ''
    for f in files:
        file_id = f['filename'].replace('/', '_').replace('.', '_')
        status_class = {'added': 'added', 'modified': 'modified', 'removed': 'removed'}.get(f['status'], '')

        if f.get('patch'):
            hunks = parse_patch(f['patch'])
            total_lines = sum(len(h['lines']) + 1 for h in hunks)  # +1 for hunk header
            is_large = total_lines > 300
            hunks_html = ''
            line_count = 0

            for hunk in hunks:
                old_num = hunk['old_start']
                new_num = hunk['new_start']
                lines_html = f'<tr class="hunk-header"><td colspan="4">{html.escape(hunk["header"])}</td></tr>\n'
                line_count += 1
                for ltype, text in hunk['lines']:
                    escaped = html.escape(text)
                    # Insert truncation point for large files
                    if is_large and line_count == 300:
                        lines_html += f'</tbody></table><div class="show-more-wrap" id="showmore-{file_id}"><button class="show-more-btn" onclick="showAllLines(this, \'{file_id}\')">Show all {total_lines:,} lines ({f["additions"]:,}+ / {f["deletions"]:,}-)</button></div><table class="diff-table diff-hidden" id="rest-{file_id}"><tbody>'
                    if ltype == 'add':
                        lines_html += f'<tr class="line-add"><td class="ln"></td><td class="ln">{new_num}</td><td class="sign">+</td><td class="code">{escaped}</td></tr>\n'
                        new_num += 1
                    elif ltype == 'del':
                        lines_html += f'<tr class="line-del"><td class="ln">{old_num}</td><td class="ln"></td><td class="sign">-</td><td class="code">{escaped}</td></tr>\n'
                        old_num += 1
                    else:
                        lines_html += f'<tr class="line-ctx"><td class="ln">{old_num}</td><td class="ln">{new_num}</td><td class="sign"> </td><td class="code">{escaped}</td></tr>\n'
                        old_num += 1
                        new_num += 1
                    line_count += 1
                hunks_html += lines_html

            diff_content = f'<table class="diff-table"><tbody>{hunks_html}</tbody></table>'
        else:
            diff_content = f'<div class="large-file-notice">No diff available &mdash; binary or empty file</div>'

        dir_part = '/'.join(f['filename'].split('/')[:-1])
        base_part = f['filename'].split('/')[-1]
        dir_prefix = html.escape(dir_part + '/') if dir_part else ''

        diff_sections += f'''<div class="file-section" id="file-{file_id}" data-filename="{html.escape(f['filename'])}">
            <div class="file-header {status_class}" onclick="toggleSection(this)">
                <svg class="chev" viewBox="0 0 16 16"><path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
                <span class="file-path"><span class="fp-dir">{dir_prefix}</span>{html.escape(base_part)}</span>
                <span class="file-badge {status_class}">{f['status']}</span>
                <span class="diff-stat"><span class="plus">+{f['additions']}</span> <span class="minus">-{f['deletions']}</span></span>
            </div>
            <div class="file-body">{diff_content}</div>
        </div>\n'''

    # Build JSON file index for unified search
    file_index_json = json.dumps([{
        'id': f['filename'].replace('/', '_').replace('.', '_'),
        'filename': f['filename'],
        'basename': f['filename'].split('/')[-1],
        'dir': '/'.join(f['filename'].split('/')[:-1]),
        'status': f['status'],
    } for f in files])

    page_html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PR #{pr_num}: {html.escape(meta.get("title", ""))}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
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
  --sidebar-w:320px;
}}
@media(prefers-color-scheme:light){{
  :root{{--bg:#fafaf8;--fg:#1a1a1a;--muted:#595959;--border:rgba(135,139,134,.2);
    --card:rgba(246,255,245,.03);--user-bg:rgba(0,0,0,.03);
    --code-bg:#f4f4f0;--green:#16a34a;--red:#d44444;--link:#0969da;
    --diff-add-bg:rgba(34,197,94,.1);--diff-add-fg:#16a34a;
    --diff-del-bg:rgba(239,68,68,.1);--diff-del-fg:#dc2626;}}
}}
*{{ margin:0; padding:0; box-sizing:border-box; }}
html{{ height:100%; }}
body{{ font-family:var(--sans); background:var(--bg); color:var(--fg); display:flex; height:100vh; -webkit-font-smoothing:antialiased; -moz-osx-font-smoothing:grayscale; }}

/* ---- Sidebar ---- */
.sidebar{{ width:var(--sidebar-w); min-width:var(--sidebar-w); background:var(--bg); border-right:1px solid var(--border); display:flex; flex-direction:column; overflow:hidden; z-index:50; }}
.sidebar-inner{{ border:1px solid var(--border); border-radius:10px; padding:16px; background:var(--card); margin:12px; flex:1; display:flex; flex-direction:column; overflow:hidden; }}
.sidebar-header{{ padding-bottom:12px; border-bottom:1px solid var(--border); margin-bottom:12px; }}
.sidebar-header h2{{ font-size:.875rem; font-weight:600; color:var(--link); margin-bottom:6px; }}
.sidebar-header .pr-meta{{ font-size:.75rem; color:var(--muted); line-height:1.5; }}

/* Search & filter */
.search-box{{ padding-bottom:10px; border-bottom:1px solid var(--border); margin-bottom:8px; }}
.search-box input{{
  width:100%; padding:8px 12px; border:1px solid var(--border); border-radius:8px;
  background:var(--card); color:var(--fg); font-size:.875rem; font-family:var(--sans);
  transition:border-color .15s ease;
}}
.search-box input::placeholder{{ color:var(--muted); }}
.search-box input:focus{{ border-color:var(--link); outline:none; }}

/* Bulk actions */
.bulk-actions{{ display:flex; gap:6px; flex-wrap:wrap; padding-bottom:10px; border-bottom:1px solid var(--border); margin-bottom:8px; }}
.bulk-actions button{{
  padding:4px 10px; background:var(--user-bg); border:1px solid var(--border); border-radius:var(--radius);
  color:var(--fg); cursor:pointer; font-size:.75rem; font-family:var(--sans); transition:background .15s ease, border-color .15s ease;
}}
.bulk-actions button:hover{{ background:var(--border); border-color:var(--muted); }}

/* File list */
.file-list{{ overflow-y:auto; flex:1; margin:0 -4px; }}
.file-item{{ display:flex; align-items:center; padding:5px 8px; cursor:pointer; font-size:.8125rem; gap:6px; border-radius:var(--radius); transition:background .1s ease; }}
.file-item:hover{{ background:var(--user-bg); }}
.file-item.hidden{{ display:none; }}
.file-item input{{ flex-shrink:0; accent-color:var(--green); }}
.status-badge{{ width:16px; text-align:center; font-weight:600; font-size:.6875rem; flex-shrink:0; font-family:var(--mono); }}
.status-badge.added{{ color:var(--green); }}
.status-badge.modified{{ color:var(--link); }}
.status-badge.removed{{ color:var(--red); }}
.filename{{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; flex:1; }}
.file-stats{{ color:var(--muted); font-size:.6875rem; flex-shrink:0; font-family:var(--mono); }}
.file-item.active{{ background:rgba(117,219,240,.08); border-left:2px solid var(--link); }}
.file-item.reviewed{{ opacity:.45; }}
.review-btn{{ background:none; border:1px solid var(--border); border-radius:50%; width:18px; height:18px;
  color:var(--muted); cursor:pointer; font-size:.6rem; display:flex; align-items:center; justify-content:center;
  flex-shrink:0; transition:all .15s; padding:0; line-height:1; }}
.review-btn:hover{{ border-color:var(--green); color:var(--green); }}
.review-btn.checked{{ background:var(--green); border-color:var(--green); color:var(--bg); }}
/* Progress bar */
.progress-bar{{ padding:8px 0; font-size:.7rem; color:var(--muted); }}
.progress-track{{ height:3px; background:var(--border); border-radius:2px; margin-top:4px; overflow:hidden; }}
.progress-fill{{ height:100%; background:var(--green); border-radius:2px; transition:width .3s ease; }}

/* ---- Main content ---- */
.main{{ flex:1; overflow-y:auto; padding:0; }}

/* PR summary */
.pr-summary{{ padding:24px 32px; border-bottom:1px solid var(--border); }}
.pr-summary h1{{ font-size:1.25rem; font-weight:600; margin-bottom:8px; }}
.pr-summary h1 a{{ color:var(--link); text-decoration:none; }}
.pr-summary h1 a:hover{{ text-decoration:underline; }}
.pr-summary .stats{{ display:flex; gap:12px; margin-top:10px; font-size:.8125rem; font-family:var(--mono); }}
.pr-summary .stats span{{ padding:3px 10px; border-radius:999px; }}
.stat-add{{ background:var(--diff-add-bg); color:var(--diff-add-fg); }}
.stat-del{{ background:var(--diff-del-bg); color:var(--diff-del-fg); }}
.stat-files{{ background:var(--user-bg); color:var(--muted); }}
.pr-summary .pr-desc{{
  font-size:.875rem; color:var(--muted); max-height:300px; overflow-y:auto;
  margin-top:14px; padding:14px; background:var(--code-bg); border:1px solid var(--border);
  border-radius:var(--radius); line-height:1.6; word-break:break-word;
}}
.pr-desc p{{ margin:.4em 0; }}
.pr-desc h1,.pr-desc h2,.pr-desc h3{{ color:var(--fg); margin:.6em 0 .3em; font-weight:600; }}
.pr-desc h1{{ font-size:1.1em; }} .pr-desc h2{{ font-size:1em; }} .pr-desc h3{{ font-size:.95em; }}
.pr-desc ul,.pr-desc ol{{ padding-left:1.5rem; margin:.4em 0; }}
.pr-desc li{{ margin:.2em 0; }}
.pr-desc code{{ background:var(--user-bg); padding:1px 5px; border-radius:3px; font-family:var(--mono); font-size:.85em; }}
.pr-desc pre{{ background:var(--bg); padding:10px; border-radius:var(--radius); overflow-x:auto; margin:.5em 0; }}
.pr-desc pre code{{ background:none; padding:0; }}
.pr-desc a{{ color:var(--link); }}
.pr-desc blockquote{{ border-left:3px solid var(--border); padding-left:10px; color:var(--muted); margin:.4em 0; }}
.pr-desc img{{ max-width:100%; border-radius:var(--radius); }}
.pr-desc table{{ border-collapse:collapse; margin:.5em 0; font-size:.85em; }}
.pr-desc th,.pr-desc td{{ border:1px solid var(--border); padding:4px 8px; }}
.pr-desc th{{ background:var(--user-bg); font-weight:600; }}

/* Search results */
.search-results{{ padding:14px 32px; background:var(--code-bg); border-bottom:1px solid var(--border); display:none; }}
.search-results.active{{ display:block; }}
.search-results h3{{ font-size:.8125rem; color:var(--muted); margin-bottom:8px; font-weight:500; }}
.search-hit{{
  padding:7px 12px; margin:4px 0; background:var(--user-bg); border:1px solid var(--border);
  border-radius:var(--radius); cursor:pointer; font-size:.8125rem; transition:background .1s ease;
}}
.search-hit:hover{{ background:var(--border); }}
.search-hit .hit-file{{ color:var(--link); font-weight:500; }}
.search-hit .hit-line{{ color:var(--muted); }}
.search-hit mark{{ background:rgba(117,219,240,.15); color:var(--link); padding:0 2px; border-radius:2px; }}

/* ---- Discussion cards (PR body, reviews, comments) ---- */
.discussion-section .file-body{{ padding:0; }}
.discussion-card{{ padding:14px 24px; }}
.disc-author{{ display:flex; align-items:center; gap:8px; margin-bottom:10px; padding-bottom:8px; border-bottom:1px solid var(--border); }}
.user-avatar{{ width:24px; height:24px; border-radius:50%; flex-shrink:0; }}
.user-info{{ font-size:.8rem; line-height:1.4; }}
.user-info strong{{ color:var(--fg); }}
.user-meta{{ color:var(--muted); font-size:.7rem; }}
.user-bio{{ color:var(--muted); font-size:.7rem; font-style:italic; }}
.disc-body{{ font-size:.85rem; line-height:1.6; color:var(--fg); }}
.disc-body p{{ margin:.4em 0; }}
.disc-body code{{ background:var(--code-bg); padding:1px 5px; border-radius:3px; font-family:var(--mono); font-size:.85em; }}
.disc-body pre{{ background:var(--code-bg); padding:10px; border-radius:var(--radius); overflow-x:auto; margin:.5em 0; }}
.disc-body pre code{{ background:none; padding:0; }}
.disc-body a{{ color:var(--link); }}
.disc-body blockquote{{ border-left:3px solid var(--border); padding-left:10px; color:var(--muted); margin:.4em 0; }}
.disc-body img{{ max-width:100%; border-radius:var(--radius); }}
.disc-body ul,.disc-body ol{{ padding-left:1.5rem; margin:.4em 0; }}
.disc-body li{{ margin:.2em 0; }}
.disc-body table{{ border-collapse:collapse; margin:.5em 0; font-size:.85em; }}
.disc-body th,.disc-body td{{ border:1px solid var(--border); padding:4px 8px; }}
.disc-body th{{ background:var(--user-bg); font-weight:600; }}
.disc-body h1,.disc-body h2,.disc-body h3{{ color:var(--fg); margin:.5em 0 .3em; font-weight:600; }}
.comment-highlight{{ border-left:3px solid var(--link); }}
.comment-highlight > .file-header{{ background:rgba(117,219,240,.04); }}
/* Sidebar section divider */
.sidebar-divider{{ font-size:.65rem; color:var(--muted); padding:8px 8px 4px; text-transform:uppercase; letter-spacing:.05em; font-weight:600; }}

/* ---- Comments (legacy, unused) ---- */
.comments-section{{ border-bottom:1px solid var(--border); }}
.comments-header{{ padding:10px 24px; cursor:pointer; font-size:.85rem; font-weight:500; color:var(--muted);
  display:flex; align-items:center; gap:6px; transition:color .15s; }}
.comments-header:hover{{ color:var(--fg); }}
.comments-toggle{{ font-size:.65rem; transition:transform .2s; display:inline-block; }}
.comments-section.collapsed .comments-toggle{{ transform:rotate(-90deg); }}
.comments-section.collapsed .comments-list{{ display:none; }}
.comments-list{{ padding:0 24px 16px; display:flex; flex-direction:column; gap:10px; }}
.comment{{ border:1px solid var(--border); border-radius:var(--radius); padding:10px 14px; background:var(--card); }}
.comment-highlight{{ border-color:var(--link); background:rgba(117,219,240,.06); }}
.comment-header{{ display:flex; align-items:center; gap:8px; margin-bottom:6px; font-size:.75rem; }}
.comment-user{{ font-weight:600; color:var(--fg); }}
.comment-date{{ color:var(--muted); }}
.comment-type{{ font-size:.65rem; padding:1px 6px; border-radius:999px; background:var(--user-bg); color:var(--muted); }}
.comment-file{{ margin-bottom:6px; }}
.comment-file-path{{ font-family:var(--mono); font-size:.75rem; color:var(--link); background:var(--code-bg); padding:2px 6px; border-radius:3px; }}
.comment-body{{ font-size:.85rem; line-height:1.5; color:var(--fg); }}
.comment-body p{{ margin:.3em 0; }}
.comment-body code{{ background:var(--code-bg); padding:1px 4px; border-radius:3px; font-family:var(--mono); font-size:.8em; }}
.comment-body pre{{ background:var(--code-bg); padding:8px; border-radius:var(--radius); overflow-x:auto; margin:.4em 0; }}
.comment-body pre code{{ background:none; padding:0; }}
.comment-body a{{ color:var(--link); }}
.comment-body blockquote{{ border-left:3px solid var(--border); padding-left:10px; color:var(--muted); margin:.3em 0; }}

/* ---- File sections ---- */
.file-section{{ border-bottom:1px solid var(--border); }}
.file-section.hidden{{ display:none; }}
.file-header{{
  padding:10px 32px; background:var(--code-bg); cursor:pointer; display:flex; align-items:center;
  gap:8px; position:sticky; top:0; z-index:10; border-bottom:1px solid var(--border);
  transition:background .1s ease;
}}
.file-header:hover{{ background:var(--user-bg); }}
.chev{{ width:14px; height:14px; transition:transform .15s ease; flex-shrink:0; color:var(--muted); }}
.file-header.open .chev{{ transform:rotate(90deg); }}
.file-path{{ font-family:var(--mono); font-size:.8125rem; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.fp-dir{{ color:var(--muted); opacity:.6; }}
.file-badge{{ font-size:.6875rem; padding:2px 8px; border-radius:999px; font-weight:500; }}
.file-badge.added{{ background:var(--diff-add-bg); color:var(--diff-add-fg); }}
.file-badge.modified{{ background:rgba(117,219,240,.1); color:var(--link); }}
.file-badge.removed{{ background:var(--diff-del-bg); color:var(--diff-del-fg); }}
.diff-stat{{ font-family:var(--mono); font-size:.7rem; flex-shrink:0; display:inline-flex; gap:4px; }}
.diff-stat .plus{{ color:var(--green); }} .diff-stat .minus{{ color:var(--red); }}
.file-body{{ overflow-x:auto; }}
.file-header:not(.open) + .file-body{{ display:none; }}
.large-file-notice{{ padding:24px 32px; color:var(--muted); font-size:.875rem; }}
.large-file-notice a{{ color:var(--link); text-decoration:none; }}
.large-file-notice a:hover{{ text-decoration:underline; }}
/* Lazy loading for large files */
.diff-hidden{{ display:none; }}
.show-more-wrap{{ padding:8px 24px; text-align:center; border-top:1px dashed var(--border); }}
.show-more-btn{{ padding:6px 16px; background:var(--card); border:1px solid var(--border);
  border-radius:var(--radius); color:var(--link); cursor:pointer; font-family:var(--sans);
  font-size:.8rem; transition:all .15s; }}
.show-more-btn:hover{{ background:var(--user-bg); border-color:var(--link); }}
/* content-visibility for smooth scrolling */
.file-section{{ content-visibility:auto; contain-intrinsic-size:auto 500px; }}

/* ---- Blast Radius Graph overrides (use theme vars instead of inline colors) ---- */
#pr-graph-container{{ background:var(--card) !important; border:1px solid var(--border) !important; border-radius:var(--radius) !important; font-family:var(--sans) !important; color:var(--fg) !important; }}
#pr-graph-container > div:first-child{{ border-bottom-color:var(--border) !important; }}
#pr-graph-container > div:first-child span[style*="font-weight:600"]{{ color:var(--fg); }}
#pr-graph-container > div:first-child span[style*="color:#888"]{{ color:var(--muted) !important; }}
#pr-graph-container > div:first-child span[style*="background:#7b1fa2"]{{ background:rgba(117,219,240,.1) !important; color:var(--link) !important; border-radius:999px !important; font-size:.6875rem !important; font-weight:500 !important; }}
#pr-graph-container > div:last-of-type > div:last-child{{ border-left-color:var(--border) !important; }}
#pr-graph-container div[style*="color:#888"]{{ color:var(--muted) !important; }}
#pr-graph-container div[style*="color:#81c784"]{{ color:var(--green) !important; }}
#pr-graph-container div[style*="color:#e53935"]{{ color:var(--red) !important; }}
#pr-graph-container div[style*="border:2px solid #fff"] span{{ border-color:var(--fg) !important; }}

/* ---- Diff table ---- */
.diff-table{{ width:100%; border-collapse:collapse; font-family:var(--mono); font-size:.75rem; line-height:1.65; }}
.diff-table td{{ padding:0 10px; white-space:pre-wrap; word-break:break-all; }}
.diff-table .ln{{ width:48px; min-width:48px; text-align:right; color:var(--muted); opacity:.5; user-select:none; vertical-align:top; }}
.diff-table .sign{{ width:16px; min-width:16px; text-align:center; user-select:none; }}
.diff-table .code{{ width:100%; }}
.line-add{{ background:var(--diff-add-bg); }}
.line-add .sign{{ color:var(--diff-add-fg); }}
.line-add .code{{ color:var(--diff-add-fg); }}
.line-del{{ background:var(--diff-del-bg); }}
.line-del .sign{{ color:var(--diff-del-fg); }}
.line-del .code{{ color:var(--diff-del-fg); }}
.line-ctx{{ background:transparent; }}
.hunk-header{{ background:var(--code-bg); }}
.hunk-header td{{ color:var(--link); font-size:.75rem; padding:6px 10px; opacity:.7; }}
tr.highlight{{ outline:2px solid var(--link); }}
tr.search-match td.code mark{{ background:rgba(117,219,240,.15); color:var(--link); }}

/* ---- Mobile toggle ---- */
.mobile-toggle{{ display:none; }}

/* ---- Responsive (900px breakpoint) ---- */
@media(max-width:900px){{
  .sidebar{{
    width:85vw; min-width:280px; max-width:360px; position:fixed; top:0; left:0; bottom:0;
    z-index:200; transform:translateX(-100%); transition:transform .3s cubic-bezier(.4,0,.2,1);
    box-shadow:none; background:var(--bg);
  }}
  .sidebar.open{{ transform:translateX(0); box-shadow:4px 0 24px rgba(0,0,0,.4); }}
  .sidebar-overlay{{ display:none; position:fixed; inset:0; z-index:199; background:rgba(0,0,0,.5); }}
  .sidebar.open ~ .sidebar-overlay{{ display:block; }}
  .main{{ width:100%; }}
  .mobile-toggle{{
    display:block !important; position:fixed; top:12px; left:12px; z-index:201;
    padding:8px 14px; background:var(--code-bg); border:1px solid var(--border);
    border-radius:8px; color:var(--fg); cursor:pointer; font-family:var(--sans);
    font-size:.8125rem; font-weight:500; backdrop-filter:blur(8px); -webkit-backdrop-filter:blur(8px);
  }}
  .mobile-toggle:hover{{ background:var(--user-bg); }}
  .file-header{{ padding:10px 16px; }}
  .pr-summary{{ padding:60px 16px 16px; }}
  .search-results{{ padding:12px 16px; }}
  .large-file-notice{{ padding:20px 16px; }}
  .diff-table td{{ padding:0 4px; }}
  .diff-table .ln{{ width:28px; min-width:28px; font-size:.65rem; }}
  .diff-table{{ font-size:.65rem; }}
}}
/* Jump buttons */
.jump{{ position:fixed; bottom:16px; right:16px; display:flex; flex-direction:column; gap:6px; z-index:50; }}
.jump a{{ width:36px; height:36px; border-radius:50%; border:1px solid var(--border);
  background:var(--bg); display:flex; align-items:center; justify-content:center;
  color:var(--muted); text-decoration:none; font-size:1rem; transition:all .15s;
  box-shadow:0 2px 8px rgba(0,0,0,.15); }}
.jump a:hover{{ border-color:var(--link); color:var(--link); text-decoration:none; }}
</style>
</head>
<body>
<button class="mobile-toggle" onclick="toggleSidebar()">Files</button>
<div class="sidebar" id="sidebar">
  <div class="sidebar-inner">
    <div class="sidebar-header">
        <h2>PR #{pr_num}</h2>
        <div class="pr-meta">{html.escape(meta.get('head',''))} &rarr; {html.escape(meta.get('base',''))}</div>
        <div class="pr-meta">{len(files)} files, +{meta.get('additions',0):,} -{meta.get('deletions',0):,}</div>
    </div>
    <div class="search-box">
        <input type="text" id="searchInput" placeholder="Search code, files, folders..." oninput="debounceSearch()">
    </div>
    <div class="bulk-actions">
        <button onclick="toggleAll(true)">All</button>
        <button onclick="toggleAll(false)">None</button>
        <button onclick="toggleModifiedOnly()">Modified</button>
        <button onclick="expandAll()">Expand</button>
        <button onclick="collapseAll()">Collapse</button>
    </div>
    <div class="progress-bar">
        <span id="progressText">0 / {len(files)} reviewed</span>
        <div class="progress-track"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>
    </div>
    <div class="file-list">
        {f'<div class="sidebar-divider">Graph</div>{graph_sidebar}' if graph_sidebar else ''}
        <div class="sidebar-divider">Discussion ({len(reviews) + len(comments) + 1})</div>
        {discussion_sidebar}
        <div class="sidebar-divider">Files ({len(files)})</div>
        {file_list_html}
    </div>
  </div>
</div>
<div class="sidebar-overlay" onclick="toggleSidebar()"></div>
<div class="main" id="mainContent">
    <div class="pr-summary">
        <h1><a href="{meta.get('html_url','')}" target="_blank">{html.escape(meta.get('title',''))}</a></h1>
        <div class="stats">
            <span class="stat-add">+{meta.get('additions',0):,}</span>
            <span class="stat-del">-{meta.get('deletions',0):,}</span>
            <span class="stat-files">{meta.get('changed_files',0)} files</span>
            <span class="stat-files">{meta.get('commits',0)} commits</span>
            <span class="stat-files">{len(comments) + len(reviews)} comments</span>
        </div>
    </div>
    <div class="search-results" id="searchResults">
        <h3 id="searchResultsTitle"></h3>
        <div id="searchResultsList"></div>
    </div>
    {graph_section}
    {discussion_sections}
    {diff_sections}
</div>
<div class="jump">
    <a href="#" onclick="event.preventDefault();document.getElementById('mainContent').scrollTo({{top:0,behavior:'smooth'}})" title="Top">&uarr;</a>
    <a href="#" onclick="event.preventDefault();jumpNext()" title="Next file">&darr;</a>
</div>
<script>
const totalFiles = {len(files)};
let reviewedSet = new Set();
const fileIndex = {file_index_json};

// Sidebar toggle (mobile)
function toggleSidebar() {{
    document.getElementById('sidebar').classList.toggle('open');
}}

// File click → jump to section + expand
function jumpToFile(id) {{
    const section = document.getElementById('file-' + id);
    if (!section) return;
    const header = section.querySelector('.file-header');
    if (header && !header.classList.contains('open')) header.classList.add('open');
    section.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
    document.querySelectorAll('.file-item').forEach(f => f.classList.remove('active'));
    const item = document.querySelector('.file-item[data-target="'+id+'"]');
    if (item) item.classList.add('active');
    document.getElementById('sidebar').classList.remove('open');
}}

// Collapse/expand
function toggleSection(header) {{ header.classList.toggle('open'); }}
function expandAll() {{ document.querySelectorAll('.file-header').forEach(h => h.classList.add('open')); }}
function collapseAll() {{ document.querySelectorAll('.file-header').forEach(h => h.classList.remove('open')); }}
function toggleAll(show) {{ document.querySelectorAll('.file-section').forEach(s => s.classList.toggle('hidden', !show)); resetFileListFilter(); }}
function toggleModifiedOnly() {{
    document.querySelectorAll('.file-section').forEach(s => {{
        const fn = s.dataset.filename;
        const item = document.querySelector('.file-item[data-filename="'+fn+'"]');
        const isMod = item && item.classList.contains('modified');
        s.classList.toggle('hidden', !isMod);
    }});
}}
function resetFileListFilter() {{
    document.querySelectorAll('.file-item').forEach(item => item.classList.remove('hidden'));
}}

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
    const main = document.getElementById('mainContent');
    const sections = [...document.querySelectorAll('.file-section:not(.hidden)')];
    const scrollTop = main.scrollTop;
    for (const s of sections) {{
        if (s.offsetTop - main.offsetTop > scrollTop + 60) {{
            main.scrollTo({{ top: s.offsetTop - main.offsetTop, behavior: 'smooth' }});
            return;
        }}
    }}
}}

// ---- Unified Search (code + files + folders) ----
let searchTimer;
function debounceSearch() {{ clearTimeout(searchTimer); searchTimer = setTimeout(doSearch, 300); }}

function doSearch() {{
    const query = document.getElementById('searchInput').value.trim().toLowerCase();
    const resultsDiv = document.getElementById('searchResults');
    const listDiv = document.getElementById('searchResultsList');
    const titleDiv = document.getElementById('searchResultsTitle');

    // Clear previous
    document.querySelectorAll('tr.search-match').forEach(tr => tr.classList.remove('search-match'));
    document.querySelectorAll('td.code mark').forEach(m => m.replaceWith(m.textContent));
    resetFileListFilter();
    document.querySelectorAll('.file-section.hidden').forEach(s => s.classList.remove('hidden'));

    if (!query) {{ resultsDiv.classList.remove('active'); return; }}

    const terms = query.split(/\\s+/).filter(t => t.length > 0);
    const allResults = [];

    // 1) File/folder matches — match against full path, basename, directory at all levels
    const fileMatches = [];
    fileIndex.forEach(f => {{
        const lname = f.filename.toLowerCase();
        const lbase = f.basename.toLowerCase();
        const ldir = f.dir.toLowerCase();
        const dirParts = ldir.split('/');
        let score = 0, matched = false;
        terms.forEach(term => {{
            // Exact basename match (highest)
            if (lbase === term) {{ score += 10; matched = true; }}
            // Basename contains
            else if (lbase.indexOf(term) >= 0) {{ score += 5; matched = true; }}
            // Dir segment exact match (any level)
            else if (dirParts.some(p => p === term)) {{ score += 4; matched = true; }}
            // Dir contains
            else if (ldir.indexOf(term) >= 0) {{ score += 2; matched = true; }}
            // Full path contains
            else if (lname.indexOf(term) >= 0) {{ score += 1; matched = true; }}
        }});
        if (matched) fileMatches.push({{ ...f, score, type: 'file' }});
    }});
    fileMatches.sort((a,b) => b.score - a.score);

    // If search looks like a path/folder filter (contains / or matches dirs well), filter sidebar + sections
    const hasPathLike = query.indexOf('/') >= 0 || fileMatches.length > 0;
    if (fileMatches.length > 0 && fileMatches.length < fileIndex.length) {{
        const matchedIds = new Set(fileMatches.map(f => f.id));
        // Filter sidebar
        document.querySelectorAll('.file-item').forEach(item => {{
            const id = item.dataset.target;
            item.classList.toggle('hidden', !matchedIds.has(id));
        }});
        // Filter main sections
        document.querySelectorAll('.file-section').forEach(s => {{
            const fn = s.dataset.filename;
            const id = fn.replace(/\\//g, '_').replace(/\\./g, '_');
            s.classList.toggle('hidden', !matchedIds.has(id));
        }});
    }}

    // Add file match results
    fileMatches.slice(0, 15).forEach(f => {{
        allResults.push({{ type: 'file', filename: f.filename, id: f.id, score: f.score + 100 }});
    }});

    // 2) Code content matches (BM25)
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

    // Sort: file matches first (score+100), then code by BM25
    allResults.sort((a, b) => b.score - a.score);
    const top = allResults.slice(0, 60);

    const fileCount = top.filter(r => r.type === 'file').length;
    const codeCount = top.filter(r => r.type === 'code').length;
    let summary = '';
    if (fileCount > 0) summary += fileCount + ' files';
    if (fileCount > 0 && codeCount > 0) summary += ', ';
    if (codeCount > 0) summary += codeCount + ' code matches';
    const totalCount = allResults.length;
    if (totalCount > 60) summary += ' (showing top 60 of ' + totalCount + ')';
    titleDiv.textContent = summary;

    listDiv.innerHTML = '';
    top.forEach(r => {{
        const div = document.createElement('div');
        div.className = 'search-hit';
        if (r.type === 'file') {{
            // File/folder match
            let hl = r.filename;
            terms.forEach(term => {{
                const re = new RegExp('(' + term.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&') + ')', 'gi');
                hl = hl.replace(re, '<mark>$1</mark>');
            }});
            div.innerHTML = '<span class="hit-file" style="font-family:var(--mono);font-size:.75rem">' + hl + '</span> <span class="hit-line">' + r.id.split('_').pop() + '</span>';
            div.onclick = () => jumpToFile(r.id);
        }} else {{
            // Code match
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

// Toggle comments section
function toggleComments() {{
    document.getElementById('commentsSection')?.classList.toggle('collapsed');
}}

// Render all discussion bodies as markdown
(function() {{
    if (typeof marked === 'undefined') return;
    document.querySelectorAll('.disc-body[data-body], .comment-body[data-body]').forEach(el => {{
        try {{ el.innerHTML = marked.parse(el.dataset.body); }}
        catch(e) {{ el.textContent = el.dataset.body; }}
    }});
}})();

// Auto-expand first 5 files on load
(function() {{
    const headers = document.querySelectorAll('.file-header');
    headers.forEach((h, i) => {{ if (i < 5) h.classList.add('open'); }});
}})();

// Keyboard shortcuts
document.addEventListener('keydown', e => {{
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    if (e.key === '/' || e.key === 's') {{ e.preventDefault(); document.getElementById('searchInput').focus(); }}
    if (e.key === 'j') jumpNext();
    if (e.key === 'Escape') {{
        document.getElementById('searchInput').value = '';
        doSearch();
        document.getElementById('searchInput').blur();
    }}
}});

// Auto-scroll to highlighted comment if present
{f'setTimeout(function(){{var el=document.getElementById("section-comment-{highlight_comment_id}");if(el){{var h=el.querySelector(".file-header");if(h)h.classList.add("open");el.scrollIntoView({{behavior:"smooth",block:"center"}})}}}},600);' if highlight_comment_id else ''}
</script>
{graph_scripts}
</body>
</html>'''
    return page_html


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

    print(f"Fetching PR #{pr_num} from {owner}/{repo}...")
    meta, files = fetch_pr_data(owner, repo, pr_num)
    print(f"Got {len(files)} files, fetching comments & reviews...")
    comments = fetch_pr_comments(owner, repo, pr_num)
    reviews = fetch_pr_reviews(owner, repo, pr_num)
    print(f"Got {len(comments)} comments, {len(reviews)} reviews, fetching user profiles...")

    # Collect unique usernames for profile fetch
    usernames = {meta.get('user', '')}
    for c in comments:
        usernames.add(c.get('user', ''))
    for r in reviews:
        usernames.add(r.get('user', ''))
    usernames.discard('')
    user_profiles = fetch_user_info(usernames)
    print(f"Got {len(user_profiles)} user profiles", end='')

    # Run pr-graph.py for blast radius visualization (optional, best-effort)
    graph_html = None
    graph_summary = None
    if '--no-graph' not in sys.argv:
        pr_graph_script = Path(__file__).parent.parent / 'beast' / 'tools' / 'pr-graph.py'
        if not pr_graph_script.exists():
            pr_graph_script = Path.home() / 'beast' / 'tools' / 'pr-graph.py'
        repo_root = Path.home() / repo  # e.g. ~/omi
        if pr_graph_script.exists() and repo_root.exists():
            graph_out = f'/tmp/pr-graph-{pr_num}.html'
            summary_out = f'/tmp/pr-graph-{pr_num}.json'
            pr_files = [f['filename'] for f in files]
            try:
                print(', generating graph...', end='')
                cmd = ['python3', str(pr_graph_script),
                       '--pr-files'] + pr_files + [
                       '--repo-root', str(repo_root),
                       '--output', graph_out,
                       '--summary', summary_out]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if r.returncode == 0 and os.path.exists(graph_out):
                    with open(graph_out) as gf:
                        graph_html = gf.read()
                    with open(summary_out) as sf:
                        graph_summary = json.loads(sf.read())
                    print(f' {graph_summary.get("files_in_blast_radius", 0)} files in blast radius', end='')
            except Exception as e:
                print(f' (graph failed: {e})', end='')

    print(', generating HTML...')

    html_content = generate_html(meta, files, pr_num, owner, repo,
                                 comments=comments, reviews=reviews,
                                 user_profiles=user_profiles,
                                 highlight_comment_id=highlight_comment_id,
                                 graph_html=graph_html,
                                 graph_summary=graph_summary)

    out_path = f'/tmp/pr-review-{pr_num}.html'
    with open(out_path, 'w') as f:
        f.write(html_content)
    print(f"Written to {out_path}")

    # Serve
    if '--no-serve' not in sys.argv:
        import http.server
        import socketserver

        os.chdir('/tmp')
        handler = http.server.SimpleHTTPRequestHandler

        class QuietHandler(handler):
            def log_message(self, format, *args):
                pass

        with socketserver.TCPServer(("0.0.0.0", port), QuietHandler) as httpd:
            url = f"http://100.125.36.102:{port}/pr-review-{pr_num}.html"
            print(f"\nServing at {url}")
            print("Press Ctrl+C to stop")
            httpd.serve_forever()


if __name__ == '__main__':
    main()
