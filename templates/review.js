// ── Client-side PR review renderer ──────────────────────────────────────────
// Data is embedded as JSON in #pr-data; this script renders the full page DOM.

const _data = JSON.parse(document.getElementById('pr-data').textContent);
const prMeta = { owner: _data.owner, repo: _data.repo, pr_num: _data.pr_num,
    head_sha: _data.meta.head_sha || '', base_sha: _data.meta.base_sha || '' };
const fileIndex = _data.files.map(f => ({
    id: f.filename.replace(/\//g,'_').replace(/\./g,'_'),
    filename: f.filename, basename: f.filename.split('/').pop(),
    dir: f.filename.split('/').slice(0,-1).join('/'), status: f.status
}));
const totalFiles = _data.files.length;
let reviewedSet = new Set();
const _fcCache = {};
const _esc = _escHtml;

function _escHtml(s) { const d=document.createElement('div'); d.textContent=String(s); return d.innerHTML; }

function _userBadge(username) {
    const p = (_data.user_profiles || {})[username] || {};
    if (!p.name && !p.avatar_url) return '<strong>' + _esc(username) + '</strong>';
    const name = _esc(p.name || username), joined = (p.created_at||'').slice(0,10);
    const av = p.avatar_url ? '<img src="'+_esc(p.avatar_url)+'" class="disc-av">' : '';
    const bio = p.bio ? '<br><span class="disc-user-bio">'+_esc((p.bio||'').slice(0,80))+'</span>' : '';
    return av + '<span class="disc-user-info"><strong>'+name+'</strong>' +
        ' <span class="disc-user-meta">@'+_esc(username)+' &middot; joined '+joined+
        ' &middot; '+(p.followers||0).toLocaleString()+' followers &middot; '+(p.public_repos||0)+
        ' repos</span>'+bio+'</span>';
}

function _parsePatch(text) {
    if (!text) return [];
    const hunks = []; let cur = null;
    for (const line of text.split('\n')) {
        if (line.startsWith('@@')) {
            if (cur) hunks.push(cur);
            const m = line.match(/^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)/);
            cur = m ? { header:line, old_start:+m[1], old_count:+(m[2]||1), new_start:+m[3], new_count:+(m[4]||1), context:m[5], lines:[] }
                    : { header:line, old_start:0, old_count:0, new_start:0, new_count:0, context:'', lines:[] };
        } else if (cur) {
            if (line.startsWith('+')) cur.lines.push(['add', line.slice(1)]);
            else if (line.startsWith('-')) cur.lines.push(['del', line.slice(1)]);
            else cur.lines.push(['ctx', line.startsWith(' ') ? line.slice(1) : line]);
        }
    }
    if (cur) hunks.push(cur);
    return hunks;
}

function _renderCommitDiff(cf) {
    const cls = {added:'added',modified:'modified',removed:'removed'}[cf.status]||'';
    let lines = '';
    if (cf.patch) {
        for (const line of cf.patch.split('\n')) {
            const rest = _esc(line.length > 0 && '+-  '.includes(line[0]) ? line.slice(1) : line);
            if (line.startsWith('@@')) lines += '<div class="cdiff-hunk">'+_esc(line)+'</div>';
            else if (line.startsWith('+')) lines += '<div class="cdiff-add">+'+rest+'</div>';
            else if (line.startsWith('-')) lines += '<div class="cdiff-del">-'+rest+'</div>';
            else lines += '<div class="cdiff-ctx"> '+rest+'</div>';
        }
    }
    return '<details class="cdiff-file"><summary class="cdiff-file-head">' +
        '<svg class="chev" viewBox="0 0 16 16"><path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>' +
        '<span class="file-path">'+_esc(cf.filename)+'</span>' +
        '<span class="file-badge '+cls+'">'+cf.status+'</span>' +
        '<span class="diff-stat"><span class="plus">+'+cf.additions+'</span> <span class="minus">-'+cf.deletions+'</span></span>' +
        '</summary><div class="cdiff-body">'+lines+'</div></details>\n';
}

function _renderDiscussion() {
    const m = _data.meta, comments = _data.comments||[], reviews = _data.reviews||[], commits = _data.commits||[];
    let h = '';
    // PR description
    h += '<div class="disc-card" id="disc-pr-desc"><div class="disc-head">' + _userBadge(m.user||'') +
        '<span class="disc-label">author</span></div>' +
        '<div class="disc-body" data-body="'+_esc(m.body||'(no description)')+'"></div></div>\n';
    // Commits
    for (const c of commits) {
        const sha = (c.sha||'').slice(0,8), msg = c.message||'', first = msg.split('\n')[0]||'(no message)';
        const rest = msg.includes('\n') ? msg.split('\n').slice(1).join('\n').trim() : '';
        const cfiles = c.files||[], ta = cfiles.reduce((s,f)=>s+(f.additions||0),0), td = cfiles.reduce((s,f)=>s+(f.deletions||0),0);
        const badge = c.author_login ? _userBadge(c.author_login) : '<strong>'+_esc(c.author||'')+'</strong>';
        const body = rest ? '<div class="disc-body" data-body="'+_esc(rest)+'"></div>' : '';
        h += '<div class="disc-card disc-commit" id="disc-commit-'+sha+'"><div class="disc-head">' +
            badge + '<span class="disc-label">commit</span><span class="disc-date">'+(c.date||'').slice(0,10)+'</span></div>' +
            '<div class="commit-title"><code class="commit-sha">'+sha+'</code> '+_esc(first) +
            '<span class="diff-stat" style="margin-left:8px"><span class="plus">+'+ta+'</span> <span class="minus">-'+td+'</span> ' +
            '<span style="color:var(--muted)">'+cfiles.length+' files</span></span></div>'+body +
            '<div class="commit-files">'+cfiles.map(_renderCommitDiff).join('')+'</div></div>\n';
    }
    // Reviews
    for (const rv of reviews) {
        if (!rv.body && rv.state==='COMMENTED') continue;
        const sc = {APPROVED:'approved',CHANGES_REQUESTED:'changes',COMMENTED:'commented'}[rv.state]||'';
        h += '<div class="disc-card" id="disc-review-'+rv.id+'"><div class="disc-head">' +
            _userBadge(rv.user||'') + '<span class="disc-label '+sc+'">'+(rv.state||'').toLowerCase().replace(/_/g,' ')+'</span>' +
            '<span class="disc-date">'+(rv.submitted_at||'').slice(0,10)+'</span></div>' +
            '<div class="disc-body" data-body="'+_esc(rv.body||'(no comment)')+'"></div></div>\n';
    }
    // Comments
    for (const c of comments) {
        const hl = String(c.id)===String(_data.highlight_comment_id) ? ' disc-highlight' : '';
        let fc = '';
        if (c.type==='review' && c.path) {
            fc = '<div class="disc-file"><code>'+_esc(c.path)+(c.line?':'+c.line:'')+'</code></div>';
        }
        h += '<div class="disc-card'+hl+'" id="disc-comment-'+c.id+'"><div class="disc-head">' +
            _userBadge(c.user||'') + '<span class="disc-label">'+(c.type||'issue')+'</span>' +
            '<span class="disc-date">'+(c.created_at||'').slice(0,10)+'</span></div>' +
            fc + '<div class="disc-body" data-body="'+_esc(c.body||'')+'"></div></div>\n';
    }
    return { html: h, count: 1 + commits.length + reviews.filter(r=>r.body||r.state!=='COMMENTED').length + comments.length };
}

function _renderFileList() {
    let h = '';
    for (const f of _data.files) {
        const sb = {added:'+',modified:'~',removed:'-',renamed:'R'}[f.status]||'?';
        const sc = {added:'added',modified:'modified',removed:'removed'}[f.status]||'';
        const fid = f.filename.replace(/\//g,'_').replace(/\./g,'_');
        let stats = f.additions ? '+'+f.additions : '';
        if (f.deletions) stats += ' -'+f.deletions;
        h += '<div class="file-item '+sc+'" data-filename="'+_esc(f.filename)+'" ' +
            'data-dir="'+_esc(f.filename.split('/').slice(0,-1).join('/'))+'" ' +
            'data-target="'+fid+'" onclick="jumpToFile(\''+fid+'\')">' +
            '<span class="status-badge '+sc+'">'+sb+'</span>' +
            '<span class="filename" title="'+_esc(f.filename)+'">'+_esc(f.filename.split('/').pop())+'</span>' +
            '<span class="file-stats">'+stats+'</span>' +
            '<button class="review-btn" onclick="event.stopPropagation();toggleReviewed(this,\''+fid+'\')" title="Mark reviewed">&#10003;</button></div>\n';
    }
    return h;
}

function _renderDiffSections() {
    let h = '';
    for (const f of _data.files) {
        const fid = f.filename.replace(/\//g,'_').replace(/\./g,'_');
        const sc = {added:'added',modified:'modified',removed:'removed'}[f.status]||'';
        let content;
        if (f.patch) {
            const hunks = _parsePatch(f.patch);
            const totalLines = hunks.reduce((s,hk) => s+hk.lines.length+1, 0);
            const isLarge = totalLines > 300;
            const fnJs = f.filename.replace(/'/g, "\\'");
            const fnAttr = _esc(f.filename);
            let rows = '', lineCount = 0;
            for (let hi=0; hi<hunks.length; hi++) {
                const hk = hunks[hi];
                if (hi===0 && hk.new_start>1) {
                    rows += '<tr class="expand-row"><td colspan="4"><button class="expand-btn" onclick="expandCtx(\''+fnJs+'\',1,'+hk.new_start+',1,\''+f.status+'\',this)">↕ '+(hk.new_start-1)+' hidden lines (top)</button></td></tr>\n';
                } else if (hi>0) {
                    const prev = hunks[hi-1], gns = prev.new_start+prev.new_count, gos = prev.old_start+prev.old_count, gne = hk.new_start;
                    if (gne-gns>0) rows += '<tr class="expand-row"><td colspan="4"><button class="expand-btn" onclick="expandCtx(\''+fnJs+'\','+gns+','+gne+','+gos+',\''+f.status+'\',this)">↕ '+(gne-gns)+' hidden lines</button></td></tr>\n';
                }
                let on = hk.old_start, nn = hk.new_start;
                rows += '<tr class="hunk-header"><td colspan="4">'+_esc(hk.header)+'</td></tr>\n';
                lineCount++;
                for (const [lt, text] of hk.lines) {
                    const e = _esc(text);
                    if (isLarge && lineCount===300) {
                        rows += '</tbody></table><div class="show-more-wrap" id="showmore-'+fid+'"><button class="show-more-btn" onclick="showAllLines(this,\''+fid+'\')">Show all '+totalLines.toLocaleString()+' lines ('+f.additions.toLocaleString()+'+ / '+f.deletions.toLocaleString()+'-)</button></div><table class="diff-table diff-hidden" id="rest-'+fid+'"><tbody>';
                    }
                    if (lt==='add') { rows += '<tr class="line-add" data-path="'+fnAttr+'" data-line="'+nn+'" data-side="RIGHT"><td class="ln"></td><td class="ln">'+nn+'</td><td class="sign">+</td><td class="code">'+e+'</td></tr>\n'; nn++; }
                    else if (lt==='del') { rows += '<tr class="line-del" data-path="'+fnAttr+'" data-line="'+on+'" data-side="LEFT"><td class="ln">'+on+'</td><td class="ln"></td><td class="sign">-</td><td class="code">'+e+'</td></tr>\n'; on++; }
                    else { rows += '<tr class="line-ctx" data-path="'+fnAttr+'" data-line="'+nn+'" data-side="RIGHT"><td class="ln">'+on+'</td><td class="ln">'+nn+'</td><td class="sign"> </td><td class="code">'+e+'</td></tr>\n'; on++; nn++; }
                    lineCount++;
                }
            }
            content = '<table class="diff-table"><tbody>'+rows+'</tbody></table>';
        } else {
            content = '<div class="large-file-notice">No diff available &mdash; binary or empty file</div>';
        }
        const dirPart = f.filename.split('/').slice(0,-1).join('/');
        const dirPfx = dirPart ? _esc(dirPart+'/') : '';
        h += '<div class="file-section" id="file-'+fid+'" data-filename="'+_esc(f.filename)+'">' +
            '<div class="file-header '+sc+'" onclick="toggleSection(this)">' +
            '<svg class="chev" viewBox="0 0 16 16"><path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>' +
            '<span class="file-path"><span class="fp-dir">'+dirPfx+'</span>'+_esc(f.filename.split('/').pop())+'</span>' +
            '<span class="file-badge '+sc+'">'+f.status+'</span>' +
            '<span class="diff-stat"><span class="plus">+'+f.additions+'</span> <span class="minus">-'+f.deletions+'</span></span></div>' +
            '<div class="file-body">'+content+'</div></div>\n';
    }
    return h;
}

function _renderMergeBar() {
    const m = _data.meta, checks = m.checks||[];
    let bc, bt;
    if (m.merged) { bc='merged'; bt='Merged'; }
    else if (m.state==='closed') { bc='closed'; bt='Closed'; }
    else if (m.draft) { bc='draft'; bt='Draft'; }
    else if (m.mergeable===false) { bc='conflict'; bt='Has conflicts'; }
    else if (m.mergeable_state==='blocked') { bc='blocked'; bt='Blocked'; }
    else if (m.mergeable_state==='clean'||(m.mergeable&&m.mergeable_state!=='unstable')) { bc='ready'; bt='Ready to merge'; }
    else { bc='blocked'; bt=(m.mergeable_state||'').replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase())||'Checking...'; }
    let ch = '';
    for (const ck of checks) {
        const cs = ck.conclusion||ck.status||'pending';
        const cc = {success:'success',failure:'failure',action_required:'failure',cancelled:'failure',timed_out:'failure'}[cs]||'pending';
        ch += '<span class="check-item '+cc+'">'+(cc==='success'?'✓':cc==='failure'?'✗':'○')+' '+_esc(ck.name||'')+'</span>';
    }
    const canMerge = m.state==='open' && !m.merged && !m.draft && m.mergeable!==false;
    let h = '<div class="sb-row"><span class="sb-label">Status</span><span class="merge-badge '+bc+'">'+bt+'</span></div>';
    if (ch) h += '<div class="sb-checks">'+ch+'</div>';
    if (canMerge) h += '<div class="sb-merge" id="sbMerge"><select class="merge-select" id="mergeMethod"><option value="squash">Squash</option><option value="merge">Merge</option><option value="rebase">Rebase</option></select><button class="merge-btn merge" id="mergeBtn" onclick="doMerge()">Merge</button></div>';
    return h;
}

// ── Build the page ──────────────────────────────────────────────────────────
(function renderPage() {
    const m = _data.meta, files = _data.files, comments = _data.comments||[], reviews = _data.reviews||[];
    const disc = _renderDiscussion();
    const chevSvg = '<svg viewBox="0 0 16 16" fill="currentColor" width="16" height="16"><path d="M1.5 3.25a.75.75 0 000 1.5h13a.75.75 0 000-1.5h-13zm0 4a.75.75 0 000 1.5h13a.75.75 0 000-1.5h-13zm0 4a.75.75 0 000 1.5h13a.75.75 0 000-1.5h-13z"/></svg>';
    const branchSvg = '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M11.93 8.5a4.002 4.002 0 01-7.86 0H.75a.75.75 0 010-1.5h3.32a4.002 4.002 0 017.86 0h3.32a.75.75 0 010 1.5h-3.32zm-1.43-.75a2.5 2.5 0 10-5 0 2.5 2.5 0 005 0z"/></svg>';
    const commentSvg = '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M1.75 1h8.5c.966 0 1.75.784 1.75 1.75v5.5A1.75 1.75 0 0110.25 10H7.061l-2.574 2.573A1.458 1.458 0 012 11.543V10h-.25A1.75 1.75 0 010 8.25v-5.5C0 1.784.784 1 1.75 1z"/></svg>';
    const sendSvg = '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M.989 2.524a.75.75 0 01.87-.206l13 6a.75.75 0 010 1.364l-13 6A.75.75 0 01.855 14.3l1.573-5.26a.25.25 0 01.2-.18L7.3 8.11a.25.25 0 000-.486L2.628 6.88a.25.25 0 01-.2-.18L.855 1.443a.75.75 0 01.134-.92z"/></svg>';

    document.getElementById('app').innerHTML =
    '<div class="wrap"><div class="main"><div class="content">' +
    '<header><div class="h-row"><h1><a href="'+(m.html_url||'')+'" target="_blank">'+_esc(m.title||'')+'</a></h1>' +
    '<button class="sb-toggle" onclick="document.querySelector(\'.sidebar\').classList.toggle(\'sb-open\')" title="Files">'+chevSvg+'</button></div>' +
    '<div class="meta">' +
    '<span class="mi">'+branchSvg+_esc(m.head||'')+' &rarr; '+_esc(m.base||'')+'</span>' +
    '<span class="mi"><span class="stat-add">+'+(m.additions||0).toLocaleString()+'</span></span>' +
    '<span class="mi"><span class="stat-del">-'+(m.deletions||0).toLocaleString()+'</span></span>' +
    '<span class="mi"><span class="stat-neutral">'+(m.changed_files||0)+' files</span></span>' +
    '<span class="mi"><span class="stat-neutral">'+(m.commits||0)+' commits</span></span>' +
    '<span class="mi">'+commentSvg+(comments.length+reviews.length)+' comments</span></div></header>' +
    '<div class="tabs"><button class="tab active" onclick="switchTab(\'diff\')">Diff ('+files.length+')</button>' +
    '<button class="tab" onclick="switchTab(\'discussion\')">Discussion ('+disc.count+')</button></div>' +
    '<div class="tab-content active" id="tab-diff"><div class="search-results" id="searchResults"><h3 id="searchResultsTitle"></h3><div id="searchResultsList"></div></div>'+_renderDiffSections()+'</div>' +
    '<div class="tab-content" id="tab-discussion"><div class="disc-thread">'+disc.html+'</div></div>' +
    '</div>' +
    '<aside class="sidebar" id="sidebar"><div class="sidebar-inner">' +
    '<div class="sb-title">PR #'+_data.pr_num+'</div>' +
    '<div class="sb-row"><span class="sb-label">'+_esc(m.head||'')+'</span><span class="sb-val">&rarr; '+_esc(m.base||'')+'</span></div>' +
    '<div class="sb-row"><span class="sb-label">Files</span><span class="sb-val">'+files.length+'</span></div>' +
    '<div class="sb-row"><span class="sb-label">Changes</span><span class="sb-val" style="color:var(--green)">+'+(m.additions||0).toLocaleString()+'</span></div>' +
    '<div class="sb-divider"></div>'+_renderMergeBar()+
    '<div class="sb-divider"></div>' +
    '<div class="progress-bar"><span id="progressText">0 / '+files.length+' reviewed</span><div class="progress-track"><div class="progress-fill" id="progressFill" style="width:0%"></div></div></div>' +
    '<div class="sb-divider"></div>' +
    '<div class="search-box"><input type="text" id="searchInput" placeholder="Search code, files..." oninput="debounceSearch()"></div>' +
    '<div class="bulk-actions"><button onclick="expandAll()">Expand</button><button onclick="collapseAll()">Collapse</button></div>' +
    '<div class="sb-divider"></div><div class="file-list">'+_renderFileList()+'</div></div></aside></div></div>' +
    '<div class="chat-input" id="chatInput"><div class="chat-status" id="chatStatus">&check; Comment posted</div>' +
    '<textarea id="generalCommentText" placeholder="Comment on this PR... (Shift+Enter for new line)" rows="3" ' +
    'onkeydown="if(event.key===\'Enter\'&&!event.shiftKey){event.preventDefault();submitGeneralComment();}" ' +
    'oninput="this.style.height=\'auto\';this.style.height=Math.min(this.scrollHeight,200)+\'px\'"></textarea>' +
    '<button class="chat-send" onclick="submitGeneralComment()" id="chatSendBtn" title="Send comment">'+sendSvg+'</button></div>' +
    '<div class="jump"><a href="#" title="Top" onclick="window.scrollTo(0,0);return false">&uarr;</a>' +
    '<a href="#" title="Next file" onclick="jumpNext();return false">&darr;</a></div>';

    // Render markdown in discussion bodies
    if (typeof marked !== 'undefined') {
        document.querySelectorAll('.disc-body[data-body]').forEach(el => {
            try { el.innerHTML = marked.parse(el.dataset.body); }
            catch(e) { el.textContent = el.dataset.body; }
        });
    }

    // Auto-scroll to highlighted comment
    if (_data.highlight_comment_id) {
        setTimeout(function(){
            switchTab('discussion');
            var el = document.getElementById('disc-comment-'+_data.highlight_comment_id);
            if (el) el.scrollIntoView({behavior:'smooth',block:'center'});
        }, 600);
    }
})();

// ── Interactivity (unchanged) ───────────────────────────────────────────────

function switchTab(name) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.querySelector('.tab[onclick*="' + name + '"]').classList.add('active');
    document.getElementById('tab-' + name).classList.add('active');
}

async function expandCtx(filename, newStart, newEnd, oldStart, status, btn) {
    btn.disabled = true;
    btn.textContent = 'Loading...';
    const tr = btn.closest('tr');
    try {
        let lines = _fcCache[filename];
        if (!lines) {
            const token = new URLSearchParams(window.location.search).get('token');
            const ref = status === 'removed' ? prMeta.base_sha : prMeta.head_sha;
            const resp = await fetch('/tools/review/files?token=' + encodeURIComponent(token) +
                '&owner=' + encodeURIComponent(prMeta.owner) +
                '&repo=' + encodeURIComponent(prMeta.repo) +
                '&path=' + encodeURIComponent(filename) +
                '&ref=' + encodeURIComponent(ref));
            if (!resp.ok) { btn.textContent = 'Failed'; return; }
            lines = await resp.json();
            _fcCache[filename] = lines;
        }
        let h = '';
        for (let i = newStart; i < newEnd; i++) {
            const oldNum = oldStart + (i - newStart);
            const text = _escHtml(lines[i - 1] || '');
            h += '<tr class="line-ctx"><td class="ln">' + oldNum + '</td><td class="ln">' + i + '</td><td class="sign"> </td><td class="code">' + text + '</td></tr>\n';
        }
        tr.insertAdjacentHTML('beforebegin', h);
        tr.remove();
    } catch(e) {
        btn.textContent = 'Error';
        console.error('expandCtx:', e);
    }
}

document.addEventListener('click', function(e) {
    const ln = e.target.closest('td.ln');
    if (!ln) return;
    const tr = ln.closest('tr[data-path]');
    if (!tr) return;
    e.preventDefault();
    openCommentForm(tr);
});

function openCommentForm(tr) {
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
}

function cancelComment(btn) { btn.closest('.comment-form-row').remove(); }

async function submitComment(btn) {
    const form = btn.closest('.inline-comment-form');
    const ta = form.querySelector('textarea');
    const body = ta.value.trim();
    if (!body) return;
    btn.disabled = true;
    btn.textContent = 'Posting...';
    try {
        const token = new URLSearchParams(window.location.search).get('token');
        const resp = await fetch('/tools/review/comments', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                owner: prMeta.owner, repo: prMeta.repo,
                pr_num: prMeta.pr_num, head_sha: prMeta.head_sha,
                path: ta.dataset.path, line: parseInt(ta.dataset.line),
                side: ta.dataset.side, body: body, token: token
            })
        });
        if (resp.ok) {
            const formRow = btn.closest('.comment-form-row');
            formRow.innerHTML = '<td colspan="4"><div class="inline-comment-posted">✓ Comment posted on ' + _escHtml(ta.dataset.path) + ':' + ta.dataset.line + '</div></td>';
        } else {
            const err = await resp.text();
            btn.textContent = 'Comment';
            btn.disabled = false;
            alert('Failed: ' + err);
        }
    } catch(e) {
        btn.textContent = 'Comment';
        btn.disabled = false;
        alert('Network error: ' + e.message);
    }
}

function jumpToFile(id) {
    switchTab('diff');
    const section = document.getElementById('file-' + id);
    if (!section) return;
    const header = section.querySelector('.file-header');
    if (header && !header.classList.contains('open')) header.classList.add('open');
    setTimeout(function() {
        section.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, 50);
    document.querySelectorAll('.file-item').forEach(f => f.classList.remove('active'));
    const item = document.querySelector('.file-item[data-target="'+id+'"]');
    if (item) item.classList.add('active');
    document.querySelector('.sidebar').classList.remove('sb-open');
}

function toggleSection(header) { header.classList.toggle('open'); }
function expandAll() { document.querySelectorAll('.file-header').forEach(h => h.classList.add('open')); }
function collapseAll() { document.querySelectorAll('.file-header').forEach(h => h.classList.remove('open')); }

function toggleReviewed(btn, id) {
    btn.classList.toggle('checked');
    const item = btn.closest('.file-item');
    if (btn.classList.contains('checked')) {
        reviewedSet.add(id); if (item) item.classList.add('reviewed');
    } else {
        reviewedSet.delete(id); if (item) item.classList.remove('reviewed');
    }
    updateProgress();
}
function updateProgress() {
    const n = reviewedSet.size;
    document.getElementById('progressText').textContent = n + ' / ' + totalFiles + ' reviewed';
    document.getElementById('progressFill').style.width = (n / totalFiles * 100) + '%';
}

function showAllLines(btn, fileId) {
    const rest = document.getElementById('rest-' + fileId);
    const wrap = document.getElementById('showmore-' + fileId);
    if (rest) rest.classList.remove('diff-hidden');
    if (wrap) wrap.remove();
}

function jumpNext() {
    const sections = [...document.querySelectorAll('#tab-diff .file-section')];
    for (const s of sections) {
        if (s.getBoundingClientRect().top > 60) {
            s.scrollIntoView({ behavior: 'smooth', block: 'start' });
            return;
        }
    }
}

let searchTimer;
function debounceSearch() { clearTimeout(searchTimer); searchTimer = setTimeout(doSearch, 300); }

function doSearch() {
    const query = document.getElementById('searchInput').value.trim().toLowerCase();
    const resultsDiv = document.getElementById('searchResults');
    const listDiv = document.getElementById('searchResultsList');
    const titleDiv = document.getElementById('searchResultsTitle');

    document.querySelectorAll('tr.search-match').forEach(tr => tr.classList.remove('search-match'));
    document.querySelectorAll('td.code mark').forEach(m => m.replaceWith(m.textContent));
    document.querySelectorAll('.file-item.hidden').forEach(i => i.classList.remove('hidden'));
    document.querySelectorAll('.file-section.hidden').forEach(s => s.classList.remove('hidden'));

    if (!query) { resultsDiv.classList.remove('active'); return; }
    switchTab('diff');
    const terms = query.split(/\s+/).filter(t => t.length > 0);
    const allResults = [];

    const fileMatches = [];
    fileIndex.forEach(f => {
        const lname = f.filename.toLowerCase(), lbase = f.basename.toLowerCase(), ldir = f.dir.toLowerCase();
        const dirParts = ldir.split('/');
        let score = 0, matched = false;
        terms.forEach(term => {
            if (lbase === term) { score += 10; matched = true; }
            else if (lbase.indexOf(term) >= 0) { score += 5; matched = true; }
            else if (dirParts.some(p => p === term)) { score += 4; matched = true; }
            else if (ldir.indexOf(term) >= 0) { score += 2; matched = true; }
            else if (lname.indexOf(term) >= 0) { score += 1; matched = true; }
        });
        if (matched) fileMatches.push({ ...f, score, type: 'file' });
    });
    fileMatches.sort((a,b) => b.score - a.score);

    if (fileMatches.length > 0 && fileMatches.length < fileIndex.length) {
        const matchedIds = new Set(fileMatches.map(f => f.id));
        document.querySelectorAll('.file-item').forEach(item => { item.classList.toggle('hidden', !matchedIds.has(item.dataset.target)); });
        document.querySelectorAll('.file-section').forEach(s => {
            const fn = s.dataset.filename;
            const id = fn ? fn.replace(/\//g, '_').replace(/\./g, '_') : '';
            s.classList.toggle('hidden', id && !matchedIds.has(id));
        });
    }

    fileMatches.slice(0, 15).forEach(f => { allResults.push({ type: 'file', filename: f.filename, id: f.id, score: f.score + 100 }); });

    document.querySelectorAll('.file-section').forEach(section => {
        const filename = section.dataset.filename;
        section.querySelectorAll('tr').forEach(tr => {
            const codeTd = tr.querySelector('td.code');
            if (!codeTd) return;
            const text = codeTd.textContent.toLowerCase();
            let score = 0, matched = false;
            terms.forEach(term => {
                if (text.indexOf(term) >= 0) {
                    matched = true;
                    const tf = (text.split(term).length - 1);
                    const dl = text.length, k1 = 1.2, b = 0.75, avgdl = 60;
                    score += (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avgdl));
                }
            });
            if (tr.classList.contains('line-add') || tr.classList.contains('line-del')) score *= 1.5;
            if (matched) allResults.push({ type: 'code', tr, filename, text: codeTd.textContent, score });
        });
    });

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
    top.forEach(r => {
        const div = document.createElement('div');
        div.className = 'search-hit';
        if (r.type === 'file') {
            let hl = r.filename;
            terms.forEach(term => {
                const re = new RegExp('(' + term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
                hl = hl.replace(re, '<mark>$1</mark>');
            });
            div.innerHTML = '<span class="hit-file" style="font-family:var(--mono);font-size:.75rem">' + hl + '</span>';
            div.onclick = () => jumpToFile(r.id);
        } else {
            r.tr.classList.add('search-match');
            const codeTd = r.tr.querySelector('td.code');
            if (codeTd) {
                let h = codeTd.innerHTML;
                terms.forEach(term => {
                    const re = new RegExp('(' + term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
                    h = h.replace(re, '<mark>$1</mark>');
                });
                codeTd.innerHTML = h;
            }
            let preview = r.text.trim().substring(0, 120);
            terms.forEach(term => {
                const re = new RegExp('(' + term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
                preview = preview.replace(re, '<mark>$1</mark>');
            });
            div.innerHTML = '<span class="hit-file">' + r.filename.split('/').pop() + '</span> <span class="hit-line">' + preview + '</span>';
            div.onclick = () => {
                const header = r.tr.closest('.file-section')?.querySelector('.file-header');
                if (header && !header.classList.contains('open')) header.classList.add('open');
                setTimeout(() => r.tr.scrollIntoView({ behavior:'smooth', block:'center' }), 100);
                r.tr.style.outline = '2px solid var(--link)';
                setTimeout(() => r.tr.style.outline = '', 2000);
            };
        }
        listDiv.appendChild(div);
    });
    resultsDiv.classList.add('active');
}

document.addEventListener('keydown', e => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    if (e.key === '/' || e.key === 's') { e.preventDefault(); document.getElementById('searchInput').focus(); }
    if (e.key === 'j') jumpNext();
    if (e.key === '1') switchTab('diff');
    if (e.key === '2') switchTab('discussion');
    if (e.key === 'Escape') {
        document.getElementById('searchInput').value = '';
        doSearch();
        document.getElementById('searchInput').blur();
    }
});

async function submitGeneralComment() {
    const ta = document.getElementById('generalCommentText');
    const body = ta.value.trim();
    if (!body) return;
    const btn = document.getElementById('chatSendBtn');
    const status = document.getElementById('chatStatus');
    btn.disabled = true;
    try {
        const token = new URLSearchParams(window.location.search).get('token');
        const resp = await fetch('/tools/review/comments', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ owner: prMeta.owner, repo: prMeta.repo, pr_num: prMeta.pr_num, body: body, token: token })
        });
        if (resp.ok) {
            ta.value = ''; ta.style.height = '36px';
            status.classList.add('show');
            setTimeout(function(){ status.classList.remove('show'); }, 2000);
        } else {
            const err = await resp.text();
            status.textContent = 'Failed: ' + err; status.style.color = 'var(--red)'; status.classList.add('show');
            setTimeout(function(){ status.classList.remove('show'); status.textContent='✓ Comment posted'; status.style.color=''; }, 3000);
        }
    } catch(e) {
        status.textContent = 'Network error'; status.style.color = 'var(--red)'; status.classList.add('show');
        setTimeout(function(){ status.classList.remove('show'); status.textContent='✓ Comment posted'; status.style.color=''; }, 3000);
    }
    btn.disabled = false;
}

async function doMerge() {
    const btn = document.getElementById('mergeBtn');
    if (!confirm('Merge this PR?')) return;
    const method = document.getElementById('mergeMethod').value;
    btn.disabled = true; btn.textContent = '...';
    try {
        const token = new URLSearchParams(window.location.search).get('token');
        const resp = await fetch('/tools/review/merges', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ owner: prMeta.owner, repo: prMeta.repo, pr_num: prMeta.pr_num, merge_method: method, token: token })
        });
        if (resp.ok) {
            const m = document.getElementById('sbMerge');
            if (m) m.innerHTML = '<span class="merge-badge merged">Merged</span>';
        } else { const err = await resp.text(); alert('Merge failed: ' + err); btn.disabled = false; btn.textContent = 'Merge'; }
    } catch(e) { alert('Network error: ' + e.message); btn.disabled = false; btn.textContent = 'Merge'; }
}

(function() {
    const token = new URLSearchParams(window.location.search).get('token');
    if (!token) return;
    let dirty = false;
    function ping() { if (!dirty) return; dirty = false; fetch('/pr-keepalive?token=' + encodeURIComponent(token)).catch(function(){}); }
    ['mousemove','keydown','scroll','click','touchstart','focus'].forEach(function(ev) {
        document.addEventListener(ev, function() { dirty = true; }, {passive:true,capture:true});
    });
    setInterval(ping, 180000);
})();
