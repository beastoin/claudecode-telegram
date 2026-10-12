"""PR review HTML rendering — extracted from review.py for separation of concerns."""

from __future__ import annotations
import html
import json
import re
from pathlib import Path

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_REVIEW_CSS = (_TEMPLATE_DIR / "review.css").read_text() if (_TEMPLATE_DIR / "review.css").exists() else ""
_REVIEW_JS = (_TEMPLATE_DIR / "review.js").read_text() if (_TEMPLATE_DIR / "review.js").exists() else ""


def parse_patch(patch_text: str | None) -> list[dict[str, object]]:
    if not patch_text:
        return []
    hunks = []
    current_hunk = None
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
            if line.startswith("+"):
                current_hunk["lines"].append(("add", line[1:]))
            elif line.startswith("-"):
                current_hunk["lines"].append(("del", line[1:]))
            else:
                current_hunk["lines"].append(("ctx", line[1:] if line.startswith(" ") else line))
    if current_hunk:
        hunks.append(current_hunk)
    return hunks


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
    comments = comments or []
    reviews = reviews or []
    commits = commits or []
    user_profiles = user_profiles or {}

    def _user_badge(username: str) -> str:
        p = user_profiles.get(username, {})
        if not p:
            return f"<strong>{html.escape(username)}</strong>"
        name = html.escape(p.get("name", "") or username)
        joined = (p.get("created_at", "") or "")[:10]
        followers = p.get("followers", 0)
        repos = p.get("public_repos", 0)
        bio = html.escape((p.get("bio", "") or "")[:80])
        avatar = html.escape(p.get("avatar_url", "") or "")
        avatar_img = f'<img src="{avatar}" class="disc-av">' if avatar else ""
        return (
            f'{avatar_img}<span class="disc-user-info"><strong>{name}</strong>'
            f' <span class="disc-user-meta">@{html.escape(username)} &middot; '
            f"joined {joined} &middot; {followers:,} followers &middot; "
            f"{repos} repos</span>"
            f'{"<br><span class=disc-user-bio>" + bio + "</span>" if bio else ""}'
            f"</span>"
        )

    discussion_html = ""
    active_reviews = [r for r in reviews if r.get("body") or r.get("state") != "COMMENTED"]
    disc_count = 1 + len(active_reviews) + len(comments)
    pr_author = meta.get("user", "")
    pr_body_escaped = html.escape(meta.get("body", "") or "(no description)", quote=True)
    discussion_html += f"""<div class="disc-card" id="disc-pr-desc">
        <div class="disc-head">
            {_user_badge(pr_author)}
            <span class="disc-label">author</span>
        </div>
        <div class="disc-body" data-body="{pr_body_escaped}"></div>
    </div>\n"""
    for ci, commit in enumerate(commits):
        sha = commit.get("sha", "")[:8]
        sha_full = commit.get("sha", "")
        msg = commit.get("message", "")
        msg_first = msg.split("\n")[0] if msg else "(no message)"
        msg_rest = "\n".join(msg.split("\n")[1:]).strip() if "\n" in msg else ""
        author = commit.get("author", "")
        author_login = commit.get("author_login", "")
        date = (commit.get("date", "") or "")[:10]
        commit_files = commit.get("files", [])
        total_add = sum(cf.get("additions", 0) for cf in commit_files)
        total_del = sum(cf.get("deletions", 0) for cf in commit_files)
        commit_diff_html = ""
        for cf in commit_files:
            cf_status = cf.get("status", "modified")
            cf_class = {"added": "added", "modified": "modified", "removed": "removed"}.get(cf_status, "")
            cf_patch = cf.get("patch", "")
            cf_lines = ""
            if cf_patch:
                for line in cf_patch.split("\n"):
                    escaped = html.escape(line[1:] if len(line) > 0 and line[0] in ("+", "-", " ") else line)
                    if line.startswith("@@"):
                        cf_lines += f'<div class="cdiff-hunk">' f"{html.escape(line)}</div>"
                    elif line.startswith("+"):
                        cf_lines += f'<div class="cdiff-add">+{escaped}</div>'
                    elif line.startswith("-"):
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
                f"</summary>"
                f'<div class="cdiff-body">{cf_lines}</div>'
                f"</details>\n"
            )
        msg_body = ""
        if msg_rest:
            msg_escaped = html.escape(msg_rest, quote=True)
            msg_body = f'<div class="disc-body" data-body="{msg_escaped}"></div>'
        badge_user = _user_badge(author_login) if author_login else f"<strong>{html.escape(author)}</strong>"
        discussion_html += (
            f'<div class="disc-card disc-commit" id="disc-commit-{sha}">'
            f'<div class="disc-head">'
            f"{badge_user}"
            f'<span class="disc-label">commit</span>'
            f'<span class="disc-date">{date}</span>'
            f"</div>"
            f'<div class="commit-title">'
            f'<code class="commit-sha">{sha}</code> {html.escape(msg_first)}'
            f'<span class="diff-stat" style="margin-left:8px">'
            f'<span class="plus">+{total_add}</span> '
            f'<span class="minus">-{total_del}</span> '
            f'<span style="color:var(--muted)">{len(commit_files)} files</span>'
            f"</span></div>"
            f"{msg_body}"
            f'<div class="commit-files">{commit_diff_html}</div>'
            f"</div>\n"
        )
    disc_count += len(commits)
    for rv in reviews:
        if not rv.get("body") and rv.get("state") == "COMMENTED":
            continue
        rid = rv.get("id", 0)
        user = rv.get("user", "")
        state = rv.get("state", "")
        state_class = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes", "COMMENTED": "commented"}.get(state, "")
        state_label = state.lower().replace("_", " ")
        date = (rv.get("submitted_at", "") or "")[:10]
        body_escaped = html.escape(rv.get("body", "") or "(no comment)", quote=True)
        discussion_html += f"""<div class="disc-card" id="disc-review-{rid}">
            <div class="disc-head">
                {_user_badge(user)}
                <span class="disc-label {state_class}">{state_label}</span>
                <span class="disc-date">{date}</span>
            </div>
            <div class="disc-body" data-body="{body_escaped}"></div>
        </div>\n"""
    for c in comments:
        cid = c.get("id", 0)
        is_highlighted = str(cid) == str(highlight_comment_id)
        hl_class = " disc-highlight" if is_highlighted else ""
        user = c.get("user", "")
        date = (c.get("created_at", "") or "")[:10]
        body_escaped = html.escape(c.get("body", "") or "", quote=True)
        ctype = c.get("type", "issue")
        file_context = ""
        if ctype == "review" and c.get("path"):
            line_info = f":{c['line']}" if c.get("line") else ""
            file_context = f'<div class="disc-file"><code>' f'{html.escape(c["path"])}{line_info}</code></div>'
        discussion_html += f"""<div class="disc-card{hl_class}" id="disc-comment-{cid}">
            <div class="disc-head">
                {_user_badge(user)}
                <span class="disc-label">{ctype}</span>
                <span class="disc-date">{date}</span>
            </div>
            {file_context}
            <div class="disc-body" data-body="{body_escaped}"></div>
        </div>\n"""
    file_list_html = ""
    for f in files:
        status_badge = {"added": "+", "modified": "~", "removed": "-", "renamed": "R"}.get(f["status"], "?")
        status_class = {"added": "added", "modified": "modified", "removed": "removed"}.get(f["status"], "")
        file_id = f["filename"].replace("/", "_").replace(".", "_")
        stats = f'+{f["additions"]}' if f["additions"] else ""
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
            f"onclick=\"event.stopPropagation();toggleReviewed(this,'{file_id}')\" "
            f'title="Mark reviewed">&#10003;</button>'
            f"</div>\n"
        )
    diff_sections = ""
    for f in files:
        file_id = f["filename"].replace("/", "_").replace(".", "_")
        status_class = {"added": "added", "modified": "modified", "removed": "removed"}.get(f["status"], "")
        if f.get("patch"):
            hunks = parse_patch(f["patch"])
            total_lines = sum(len(h["lines"]) + 1 for h in hunks)
            is_large = total_lines > 300
            hunks_html = ""
            line_count = 0
            fn_js = html.escape(f["filename"].replace("'", "\\'"), quote=True)
            fn_attr = html.escape(f["filename"], quote=True)
            f_status = f.get("status", "modified")
            for hi, hunk in enumerate(hunks):
                if hi == 0 and hunk["new_start"] > 1:
                    hidden = hunk["new_start"] - 1
                    hunks_html += (
                        f'<tr class="expand-row"><td colspan="4">'
                        f'<button class="expand-btn" onclick="expandCtx(\'{fn_js}\','
                        f'1,{hunk["new_start"]},1,\'{f_status}\',this)">'
                        f"↕ {hidden} hidden lines (top)</button></td></tr>\n"
                    )
                elif hi > 0:
                    prev = hunks[hi - 1]
                    gap_new_s = prev["new_start"] + prev["new_count"]
                    gap_old_s = prev["old_start"] + prev["old_count"]
                    gap_new_e = hunk["new_start"]
                    hidden = gap_new_e - gap_new_s
                    if hidden > 0:
                        hunks_html += (
                            f'<tr class="expand-row"><td colspan="4">'
                            f'<button class="expand-btn" onclick="expandCtx(\'{fn_js}\','
                            f"{gap_new_s},{gap_new_e},{gap_old_s},'{f_status}',this)\">"
                            f"↕ {hidden} hidden lines</button></td></tr>\n"
                        )
                old_num = hunk["old_start"]
                new_num = hunk["new_start"]
                lines_html = f'<tr class="hunk-header"><td colspan="4">' f'{html.escape(hunk["header"])}</td></tr>\n'
                line_count += 1
                for ltype, text in hunk["lines"]:
                    escaped = html.escape(text)
                    if is_large and line_count == 300:
                        lines_html += (
                            f'</tbody></table><div class="show-more-wrap" '
                            f'id="showmore-{file_id}"><button class="show-more-btn" '
                            f"onclick=\"showAllLines(this, '{file_id}')\">"
                            f"Show all {total_lines:,} lines "
                            f'({f["additions"]:,}+ / {f["deletions"]:,}-)</button></div>'
                            f'<table class="diff-table diff-hidden" id="rest-{file_id}">'
                            f"<tbody>"
                        )
                    if ltype == "add":
                        lines_html += (
                            f'<tr class="line-add" data-path="{fn_attr}" '
                            f'data-line="{new_num}" data-side="RIGHT">'
                            f'<td class="ln"></td><td class="ln">{new_num}</td>'
                            f'<td class="sign">+</td><td class="code">{escaped}</td></tr>\n'
                        )
                        new_num += 1
                    elif ltype == "del":
                        lines_html += (
                            f'<tr class="line-del" data-path="{fn_attr}" '
                            f'data-line="{old_num}" data-side="LEFT">'
                            f'<td class="ln">{old_num}</td><td class="ln"></td>'
                            f'<td class="sign">-</td><td class="code">{escaped}</td></tr>\n'
                        )
                        old_num += 1
                    else:
                        lines_html += (
                            f'<tr class="line-ctx" data-path="{fn_attr}" '
                            f'data-line="{new_num}" data-side="RIGHT">'
                            f'<td class="ln">{old_num}</td><td class="ln">{new_num}</td>'
                            f'<td class="sign"> </td><td class="code">{escaped}</td></tr>\n'
                        )
                        old_num += 1
                        new_num += 1
                    line_count += 1
                hunks_html += lines_html
            diff_content = f'<table class="diff-table"><tbody>{hunks_html}</tbody></table>'
        else:
            diff_content = '<div class="large-file-notice">' "No diff available &mdash; binary or empty file</div>"
        dir_part = "/".join(f["filename"].split("/")[:-1])
        base_part = f["filename"].split("/")[-1]
        dir_prefix = html.escape(dir_part + "/") if dir_part else ""
        diff_sections += (
            f'<div class="file-section" id="file-{file_id}" '
            f'data-filename="{html.escape(f["filename"])}">'
            f'<div class="file-header {status_class}" onclick="toggleSection(this)">'
            f'<svg class="chev" viewBox="0 0 16 16"><path d="M6 4l4 4-4 4" '
            f'fill="none" stroke="currentColor" stroke-width="1.5" '
            f'stroke-linecap="round"/></svg>'
            f'<span class="file-path"><span class="fp-dir">{dir_prefix}</span>'
            f"{html.escape(base_part)}</span>"
            f'<span class="file-badge {status_class}">{f["status"]}</span>'
            f'<span class="diff-stat"><span class="plus">+{f["additions"]}</span> '
            f'<span class="minus">-{f["deletions"]}</span></span></div>'
            f'<div class="file-body">{diff_content}</div></div>\n'
        )
    pr_meta_json = json.dumps(
        {
            "owner": owner,
            "repo": repo,
            "pr_num": pr_num,
            "head_sha": meta.get("head_sha", ""),
            "base_sha": meta.get("base_sha", ""),
        }
    )
    file_index_json = json.dumps(
        [
            {
                "id": f["filename"].replace("/", "_").replace(".", "_"),
                "filename": f["filename"],
                "basename": f["filename"].split("/")[-1],
                "dir": "/".join(f["filename"].split("/")[:-1]),
                "status": f["status"],
            }
            for f in files
        ]
    )
    merge_bar_html = ""
    pr_state = meta.get("state", "open")
    pr_merged = meta.get("merged", False)
    pr_draft = meta.get("draft", False)
    pr_mergeable = meta.get("mergeable")
    pr_mergeable_state = meta.get("mergeable_state", "")
    checks = meta.get("checks", [])
    if pr_merged:
        badge_class, badge_text = "merged", "Merged"
    elif pr_state == "closed":
        badge_class, badge_text = "closed", "Closed"
    elif pr_draft:
        badge_class, badge_text = "draft", "Draft"
    elif pr_mergeable is False:
        badge_class, badge_text = "conflict", "Has conflicts"
    elif pr_mergeable_state == "blocked":
        badge_class, badge_text = "blocked", "Blocked"
    elif pr_mergeable_state == "clean" or (pr_mergeable and pr_mergeable_state != "unstable"):
        badge_class, badge_text = "ready", "Ready to merge"
    else:
        badge_class, badge_text = "blocked", pr_mergeable_state.replace("_", " ").title() or "Checking..."
    checks_html = ""
    for ck in checks:
        cstatus = ck.get("conclusion") or ck.get("status", "pending")
        cclass = {
            "success": "success",
            "failure": "failure",
            "action_required": "failure",
            "cancelled": "failure",
            "timed_out": "failure",
        }.get(cstatus, "pending")
        csym = {"success": "✓", "failure": "✗"}.get(cclass, "○")
        checks_html += f'<span class="check-item {cclass}">{csym} {html.escape(ck.get("name", ""))}</span>'
    can_merge = pr_state == "open" and not pr_merged and not pr_draft and pr_mergeable is not False
    sidebar_merge_html = (
        f'<div class="sb-row"><span class="sb-label">Status</span>'
        f'<span class="merge-badge {badge_class}">{badge_text}</span></div>'
    )
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
            "</div>"
        )
    _hl_js = (
        f'setTimeout(function(){{switchTab("discussion");var el=document.getElementById("disc-comment-{highlight_comment_id}");if(el)el.scrollIntoView({{behavior:"smooth",block:"center"}})}},600);'
        if highlight_comment_id
        else ""
    )
    _rjs = (
        _REVIEW_JS.replace("__TOTAL_FILES__", str(len(files)))
        .replace("__FILE_INDEX_JSON__", file_index_json)
        .replace("__HIGHLIGHT_SCROLL__", _hl_js)
    )
    page_html = (
        f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PR #{pr_num}: {html.escape(meta.get("title", ""))}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>"""
        + _REVIEW_CSS
        + f"""</style>
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
<script>"""
        + _rjs
        + """</script>
</body>
</html>"""
    )
    return page_html
