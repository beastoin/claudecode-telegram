const totalFiles = __TOTAL_FILES__;
let reviewedSet = new Set();
const fileIndex = __FILE_INDEX_JSON__;
const _fcCache = {};

function _escHtml(s) { const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }

// Tab switching
function switchTab(name) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.querySelector('.tab[onclick*="' + name + '"]').classList.add('active');
    document.getElementById('tab-' + name).classList.add('active');
}

// Expand context
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
            h += '<tr class="line-ctx"><td class="ln">' + oldNum + '</td><td class="ln">' + i + '</td><td class="sign"> </td><td class="code">' + text + '</td></tr>\\n';
        }
        tr.insertAdjacentHTML('beforebegin', h);
        tr.remove();
    } catch(e) {
        btn.textContent = 'Error';
        console.error('expandCtx:', e);
    }
}

// Inline commenting
const prMeta = JSON.parse(document.getElementById('pr-meta')?.textContent || '{}');

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
            formRow.innerHTML = '<td colspan="4"><div class="inline-comment-posted">\u2713 Comment posted on ' + _escHtml(ta.dataset.path) + ':' + ta.dataset.line + '</div></td>';
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

// Jump to file (auto-switches to Diff tab)
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

// Collapse/expand
function toggleSection(header) { header.classList.toggle('open'); }
function expandAll() { document.querySelectorAll('.file-header').forEach(h => h.classList.add('open')); }
function collapseAll() { document.querySelectorAll('.file-header').forEach(h => h.classList.remove('open')); }

// Review tracking
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

// Show all lines for truncated large files
function showAllLines(btn, fileId) {
    const rest = document.getElementById('rest-' + fileId);
    const wrap = document.getElementById('showmore-' + fileId);
    if (rest) rest.classList.remove('diff-hidden');
    if (wrap) wrap.remove();
}

// Jump to next file section
function jumpNext() {
    const sections = [...document.querySelectorAll('#tab-diff .file-section')];
    const scrollTop = window.scrollY || document.documentElement.scrollTop;
    for (const s of sections) {
        if (s.getBoundingClientRect().top > 60) {
            s.scrollIntoView({ behavior: 'smooth', block: 'start' });
            return;
        }
    }
}

// ---- Search (code + files) ----
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

    // Auto-switch to Diff tab for search
    switchTab('diff');

    const terms = query.split(/\\s+/).filter(t => t.length > 0);
    const allResults = [];

    // File/folder matches
    const fileMatches = [];
    fileIndex.forEach(f => {
        const lname = f.filename.toLowerCase();
        const lbase = f.basename.toLowerCase();
        const ldir = f.dir.toLowerCase();
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
        document.querySelectorAll('.file-item').forEach(item => {
            const id = item.dataset.target;
            item.classList.toggle('hidden', !matchedIds.has(id));
        });
        document.querySelectorAll('.file-section').forEach(s => {
            const fn = s.dataset.filename;
            const id = fn ? fn.replace(/\\//g, '_').replace(/\\./g, '_') : '';
            s.classList.toggle('hidden', id && !matchedIds.has(id));
        });
    }

    fileMatches.slice(0, 15).forEach(f => {
        allResults.push({ type: 'file', filename: f.filename, id: f.id, score: f.score + 100 });
    });

    // Code content matches
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
                const re = new RegExp('(' + term.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&') + ')', 'gi');
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
                    const re = new RegExp('(' + term.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&') + ')', 'gi');
                    h = h.replace(re, '<mark>$1</mark>');
                });
                codeTd.innerHTML = h;
            }
            let preview = r.text.trim().substring(0, 120);
            terms.forEach(term => {
                const re = new RegExp('(' + term.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&') + ')', 'gi');
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

// Render all discussion bodies as markdown
(function() {
    if (typeof marked === 'undefined') return;
    document.querySelectorAll('.disc-body[data-body]').forEach(el => {
        try { el.innerHTML = marked.parse(el.dataset.body); }
        catch(e) { el.textContent = el.dataset.body; }
    });
})();

// Keyboard shortcuts
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

// General comment
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
            body: JSON.stringify({
                owner: prMeta.owner, repo: prMeta.repo,
                pr_num: prMeta.pr_num, body: body, token: token
            })
        });
        if (resp.ok) {
            ta.value = '';
            ta.style.height = '36px';
            status.classList.add('show');
            setTimeout(function(){ status.classList.remove('show'); }, 2000);
        } else {
            const err = await resp.text();
            status.textContent = 'Failed: ' + err;
            status.style.color = 'var(--red)';
            status.classList.add('show');
            setTimeout(function(){ status.classList.remove('show'); status.textContent='\u2713 Comment posted'; status.style.color=''; }, 3000);
        }
    } catch(e) {
        status.textContent = 'Network error';
        status.style.color = 'var(--red)';
        status.classList.add('show');
        setTimeout(function(){ status.classList.remove('show'); status.textContent='\u2713 Comment posted'; status.style.color=''; }, 3000);
    }
    btn.disabled = false;
}

// Merge PR
async function doMerge() {
    const btn = document.getElementById('mergeBtn');
    if (!confirm('Merge this PR?')) return;
    const method = document.getElementById('mergeMethod').value;
    btn.disabled = true;
    btn.textContent = '...';
    try {
        const token = new URLSearchParams(window.location.search).get('token');
        const resp = await fetch('/tools/review/merges', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                owner: prMeta.owner, repo: prMeta.repo,
                pr_num: prMeta.pr_num, merge_method: method, token: token
            })
        });
        if (resp.ok) {
            const m = document.getElementById('sbMerge');
            if (m) m.innerHTML = '<span class="merge-badge merged">Merged</span>';
        } else {
            const err = await resp.text();
            alert('Merge failed: ' + err);
            btn.disabled = false;
            btn.textContent = 'Merge';
        }
    } catch(e) {
        alert('Network error: ' + e.message);
        btn.disabled = false;
        btn.textContent = 'Merge';
    }
}

// Token keepalive — extend on user interaction
(function() {
    const token = new URLSearchParams(window.location.search).get('token');
    if (!token) return;
    let lastPing = Date.now();
    let dirty = false;
    function ping() {
        if (!dirty) return;
        dirty = false;
        lastPing = Date.now();
        fetch('/pr-keepalive?token=' + encodeURIComponent(token)).catch(function(){});
    }
    ['mousemove','keydown','scroll','click','touchstart','focus'].forEach(function(ev) {
        document.addEventListener(ev, function() { dirty = true; }, {passive:true,capture:true});
    });
    setInterval(ping, 180000);
})();

// Auto-scroll to highlighted comment if present
__HIGHLIGHT_SCROLL__
