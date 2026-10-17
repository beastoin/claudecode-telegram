// Client-side transcript rendering: reads JSON from #transcript-data, builds DOM
;(function() {
var _data = JSON.parse(document.getElementById('transcript-data').textContent);
var esc = function(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; };
var ICONS = _data.icons || {};
var CHEV = ICONS.chevron || '';
var CLAUDE_AV = ICONS.claude_avatar || '';
var MANAGER_AV = ICONS.manager_avatar || '';
var TOOL_SVGS = ICONS.tools || {};
var DEFAULT_TOOL = ICONS.default_tool || '';
var NAV_ICONS = ICONS.nav || {};

// marked.js setup
marked.setOptions({
  highlight: function(code, lang) {
    if (lang && hljs.getLanguage(lang)) return hljs.highlight(code, {language: lang}).value;
    return hljs.highlightAuto(code).value;
  },
  breaks: true, gfm: true
});

function formatModelName(m) {
  var s = m.replace('claude-', '');
  s = s.replace(/-\d{8}$/, '');
  s = s.replace(/-(\d+)-(\d+)$/, ' $1.$2');
  s = s.replace(/-(\d+)\.(\d+)$/, ' $1.$2');
  return s.replace(/-/g, ' ').replace(/\b\w/g, function(c) { return c.toUpperCase(); });
}

// Detect message author from content
function detectAuthor(raw) {
  var m = raw.match(/^(\w[\w.-]{0,19}):\s/);
  if (m) return { name: m[1], avatar: '', display: raw.slice(m[0].length) };
  return { name: 'manager', avatar: MANAGER_AV, display: raw };
}

// Build tool_results map: tool_use_id → {content, is_error}
var toolResults = {};
_data.entries.forEach(function(entry) {
  var ct = (entry.type === 'user' && entry.message && entry.message.content);
  if (!Array.isArray(ct)) return;
  ct.forEach(function(item) {
    if (item && item.type === 'tool_result') {
      var rt = item.content;
      if (Array.isArray(rt)) {
        rt = rt.filter(function(r) { return r && r.type === 'text'; }).map(function(r) { return r.text || ''; }).join('\n');
      } else { rt = String(rt || ''); }
      toolResults[item.tool_use_id || ''] = { content: rt, is_error: !!item.is_error };
    }
  });
});

var INPUT_KEYS = { Read: 'file_path', Write: 'file_path', Edit: 'file_path', Glob: 'pattern', Bash: 'command', Grep: 'pattern', Search: 'pattern' };

function renderEntry(entry) {
  var etype = entry.type || '';
  var msg = entry.message || {};
  var role = msg.role || '';
  var content = msg.content;
  var parts = [];
  var tsRaw = entry.timestamp || '';
  var tsHtml = tsRaw ? '<span class="ts" data-ts="' + esc(tsRaw) + '">' + esc(tsRaw.slice(0,16).replace('T',' ')) + '</span>' : '';

  if (etype === 'user' && role === 'user') {
    if (typeof content === 'string' && content.trim()) {
      var raw = content.trim();
      if (raw.indexOf('<task-notification>') === 0 || raw.indexOf('<system-reminder>') === 0) return '';
      var author = detectAuthor(raw);
      var text = esc(author.display);
      if (text.length > 2000) text = text.slice(0, 2000) + '\n\n<em>… truncated</em>';
      var nameHtml = author.name !== 'manager' ? '<span class="u-name">' + esc(author.name) + '</span>' : '';
      var av = author.avatar || MANAGER_AV;
      parts.push('<div class="user-msg">' + av + '<div class="u-body">' + nameHtml + '<div class="u-text">' + text + tsHtml + '</div></div></div>');
    }
  } else if (etype === 'assistant' && role === 'assistant') {
    if (Array.isArray(content)) {
      content.forEach(function(item) {
        var ct = item.type || '';
        if (ct === 'thinking') {
          var tt = item.thinking || '';
          if (tt.trim()) {
            var tesc = esc(tt.slice(0, 3000));
            if (tt.length > 3000) tesc += '\n… truncated';
            parts.push('<details class="think"><summary class="think-h">' + CHEV + ' Thinking</summary><div class="think-t">' + tesc + '</div></details>');
          }
        } else if (ct === 'text') {
          var text = item.text || '';
          if (text && text !== '(no content)') {
            parts.push('<div class="a-text markdown">' + marked.parse(text) + '</div>');
          }
        } else if (ct === 'tool_use') {
          var tn = item.name || '?';
          var ti = item.input || {};
          var toolSvg = TOOL_SVGS[tn] || DEFAULT_TOOL;
          var key = INPUT_KEYS[tn];
          var inp;
          if (key) { inp = ti[key] || ''; }
          else if (tn === 'Agent') { inp = ti.description || String(ti.prompt || '').slice(0, 80); }
          else { inp = JSON.stringify(ti).slice(0, 200); }
          var isFp = (tn === 'Read' || tn === 'Write' || tn === 'Edit') && inp && inp.indexOf('/') >= 0;
          inp = String(inp).slice(0, 300);
          var toolId = item.id || '';
          var tr = toolResults[toolId] || {};
          var trText = tr.content || '';
          var trErr = tr.is_error || false;
          var hasResult = !!(trText.trim() && trText !== 'Bash completed with no output');
          var trEsc = '';
          if (hasResult) { trEsc = esc(trText.slice(0, 5000)); if (trText.length > 5000) trEsc += '\n… truncated'; }
          var errCls = trErr ? ' act-err' : '';

          if (tn === 'Edit' && ti.old_string != null) {
            var fp = esc(ti.file_path || '?');
            var oldLines = (ti.old_string || '').split(/\n/);
            var newLines = (ti.new_string || '').split(/\n/);
            var dl = [];
            oldLines.slice(0, 60).forEach(function(l, i) {
              dl.push('<div class="diff-del"><span class="diff-ln">' + (i+1) + '</span><span class="diff-sign">-</span>' + esc(l) + '</div>');
            });
            newLines.slice(0, 60).forEach(function(l, i) {
              dl.push('<div class="diff-add"><span class="diff-ln">' + (i+1) + '</span><span class="diff-sign">+</span>' + esc(l) + '</div>');
            });
            if (oldLines.length > 60 || newLines.length > 60) {
              dl.push('<div class="diff-ctx"><span class="diff-ln"></span><span class="diff-sign"> </span>… truncated</div>');
            }
            var no = oldLines.length, na = newLines.length, nm = Math.min(no, na);
            var stats = '<span class="diff-stat"><span class="diff-plus">+' + (na-nm) + '</span> <span class="diff-minus">-' + (no-nm) + '</span> <span class="diff-mod">~' + nm + '</span></span>';
            var pp = fp.split('/'); var fpH = pp.length > 1 ? '<span class="fp-dir">' + pp.slice(0,-1).join('/') + '/</span>' + pp[pp.length-1] : fp;
            parts.push('<details class="act diff-act' + errCls + '"><summary class="act-h">' + toolSvg + '<span class="fp">' + fpH + '</span>' + stats + CHEV + '</summary><div class="diff-body">' + dl.join('\n') + '</div></details>');
          } else if (tn === 'Bash' && inp) {
            var bodyH = '<div class="act-cmd">' + esc(inp) + '</div>';
            if (trEsc) bodyH += '<div class="act-out' + (trErr ? ' act-out-err' : '') + '">' + trEsc + '</div>';
            parts.push('<details class="act' + errCls + '"><summary class="act-h">' + toolSvg + '<span class="t-det">' + esc(inp.slice(0,80)) + '</span>' + CHEV + '</summary><div class="act-body">' + bodyH + '</div></details>');
          } else {
            var label;
            if (isFp) {
              var pp2 = inp.split('/'); var dp = pp2.length > 1 ? esc(pp2.slice(0,-1).join('/')) : '';
              var bp = esc(pp2[pp2.length-1]);
              label = '<span class="fp">' + (dp ? '<span class="fp-dir">' + dp + '/</span>' + bp : bp) + '</span>';
            } else { label = '<span class="t-det">' + esc(inp.slice(0,80)) + '</span>'; }
            if (trEsc && hasResult) {
              parts.push('<details class="act' + errCls + '"><summary class="act-h">' + toolSvg + label + CHEV + '</summary><div class="act-body"><pre class="t-out">' + trEsc + '</pre></div></details>');
            } else { parts.push('<div class="chip">' + toolSvg + label + '</div>'); }
          }
        }
      });
    } else if (typeof content === 'string' && content.trim()) {
      parts.push('<div class="a-text markdown">' + marked.parse(content) + '</div>');
    }
  }
  return parts.join('\n');
}

// Build pagination
function buildPagination() {
  var pg = _data.pagination;
  if (pg.total_pages <= 1) return '';
  function pgBtn(label, p, dis, cur) {
    var cls = dis ? ' pg-dis' : (cur ? ' pg-cur' : '');
    return '<a class="pg-btn' + cls + '" href="' + pageUrl(p) + '">' + label + '</a>';
  }
  var startP = Math.max(1, Math.min(pg.page - 3, pg.total_pages - 6));
  var endP = Math.min(pg.total_pages, startP + 6);
  var items = [pgBtn('First', 1, pg.page <= 1), pgBtn('Prev', pg.page - 1, pg.page <= 1)];
  for (var p = startP; p <= endP; p++) items.push(pgBtn(String(p), p, false, p === pg.page));
  items.push(pgBtn('Next', pg.page + 1, pg.page >= pg.total_pages));
  items.push(pgBtn('Last', pg.total_pages, pg.page >= pg.total_pages));
  return '<nav class="pg">' + items.join('') + '<span class="pg-info">Page ' + pg.page + '/' + pg.total_pages + ' (' + pg.total + ' entries)</span></nav>';
}

function buildQs() {
  var parts = [];
  if (_data.token) parts.push('token=' + encodeURIComponent(_data.token));
  if (_data.session_id) parts.push('sid=' + encodeURIComponent(_data.session_id));
  if (_data.pagination.per_page !== 50) parts.push('per_page=' + _data.pagination.per_page);
  if (_data.search_query) parts.push('q=' + encodeURIComponent(_data.search_query));
  if (_data.filter_mode) parts.push('filter=' + encodeURIComponent(_data.filter_mode));
  return parts.join('&');
}
var urlPrefix = (_data.live_base_url || '') + '?';
function pageUrl(p) { var qs = buildQs(); return urlPrefix + 'page=' + p + (qs ? '&' + qs : ''); }

function ctxUrl(entry) {
  if (!_data.search_query) return '';
  var idx = entry._idx; if (idx == null || idx < 0) return '';
  var ctxPage = Math.floor(idx / _data.pagination.per_page) + 1;
  var parts = [];
  if (_data.token) parts.push('token=' + encodeURIComponent(_data.token));
  if (_data.session_id) parts.push('sid=' + encodeURIComponent(_data.session_id));
  if (_data.pagination.per_page !== 50) parts.push('per_page=' + _data.pagination.per_page);
  parts.push('page=' + ctxPage);
  return urlPrefix + parts.join('&') + '#e-' + idx;
}

// Render header
function renderHeader() {
  var st = _data.stats;
  var meta = [];
  if (st.first_ts) meta.push("<span class='mi'>" + (NAV_ICONS.clock||'') + esc(st.first_ts.slice(0,10)) + "</span>");
  if (st.model) meta.push("<span class='mi'>" + (NAV_ICONS.model||'') + esc(st.model) + "</span>");
  if (st.git_branch) meta.push("<span class='mi'>" + (NAV_ICONS.branch||'') + esc(st.git_branch) + "</span>");
  var filtQs = buildQs().replace(/&?filter=[^&]*/g, '');
  var filtUrl = _data.filter_mode !== 'prompts' ? urlPrefix + (filtQs ? filtQs + '&' : '') + 'filter=prompts' : urlPrefix + filtQs;
  meta.push('<a class="mi" href="' + filtUrl + '" style="cursor:pointer" title="Filter to prompts only">' + (NAV_ICONS.chat||'') + st.n_user + ' prompts</a>');
  meta.push('<span class="mi">' + (NAV_ICONS.tool||'') + st.n_tool + ' tool call' + (st.n_tool !== 1 ? 's' : '') + '</span>');
  return '<header><div class="h-row"><h1>' + esc(_data.name) + '</h1><button class="sb-toggle" onclick="document.querySelector(\'.sidebar\').classList.toggle(\'sb-open\')" title="Session info">' + (NAV_ICONS.info||'') + '</button></div><div class="meta">' + meta.join('') + '</div></header>';
}

// Render search bar
function renderSearch() {
  var val = _data.search_query ? esc(_data.search_query) : '';
  var hidden = '';
  [['token', _data.token], ['sid', _data.session_id], ['per_page', _data.pagination.per_page !== 50 ? _data.pagination.per_page : ''], ['filter', _data.filter_mode === 'prompts' ? 'prompts' : '']].forEach(function(kv) {
    if (kv[1]) hidden += "<input type='hidden' name='" + kv[0] + "' value='" + esc(String(kv[1])) + "'>";
  });
  var action = _data.live_base_url ? ' action="' + esc(_data.live_base_url) + '"' : '';
  return '<form class="search-bar" method="get"' + action + '><input type="text" name="q" placeholder="Search transcript…" value="' + val + '"><button type="submit">Search</button>' + hidden + '</form>';
}

function renderSearchInfo() {
  if (!_data.search_query) return '';
  var sortLabel = _data.search_sort === 'time' ? 'by time' : 'by relevance';
  var altSort = _data.search_sort === 'relevance' ? 'time' : 'relevance';
  var qs = buildQs().replace(/&?sort=[^&]*/g, '');
  var sortUrl = urlPrefix + qs + '&sort=' + altSort;
  return '<div class="search-info">Found ' + _data.pagination.total + ' matching entries for "<strong>' + esc(_data.search_query) + '</strong>" (sorted ' + sortLabel + ') &middot; <a href="' + sortUrl + '">sort by ' + altSort + '</a></div>';
}

function renderFilterBanner() {
  if (_data.filter_mode !== 'prompts') return '';
  var qs = buildQs().replace(/&?filter=[^&]*/g, '');
  var clearUrl = urlPrefix + qs;
  return '<div class="search-info">Showing prompts only — <a href="' + clearUrl + '">show all</a></div>';
}

// Render sidebar
function renderSidebar() {
  var st = _data.stats;
  function row(label, val) { return "<div class='sb-row'><span class='sb-label'>" + label + "</span><span class='sb-val'>" + val + "</span></div>"; }
  var d = '<div class="sb-divider"></div>';
  var rows = [row('Session', esc((_data.session_id||'').slice(0,12)))];
  if (st.model) rows.push(row('Model', esc(formatModelName(st.model))));
  if (st.version) rows.push(row('Version', esc(st.version)));
  if (st.git_branch) rows.push(row('Branch', esc(st.git_branch)));
  rows.push(d);
  rows.push(row('Prompts', st.n_user));
  rows.push(row('Tool calls', st.n_tool));
  if (st.n_edit) rows.push(row('Edits', st.n_edit));
  if (st.n_files) rows.push(row('Files touched', st.n_files));
  if (st.lines_add || st.lines_del || st.lines_mod) {
    rows.push(d + '<div class="sb-lines"><span class="plus">+' + st.lines_add + '</span><span class="minus">-' + st.lines_del + '</span><span class="mod">~' + st.lines_mod + '</span></div>');
  }
  if (st.duration || _data.file_size || st.input_tokens) rows.push(d);
  if (st.duration) rows.push(row('Duration', esc(st.duration)));
  if (_data.file_size) rows.push(row('File size', esc(_data.file_size)));
  if (st.input_tokens) rows.push(row('Input tokens', Number(st.input_tokens).toLocaleString()));
  if (st.output_tokens) rows.push(row('Output tokens', Number(st.output_tokens).toLocaleString()));
  rows.push(d); rows.push(row('Total entries', _data.pagination.total)); rows.push(row('Page', _data.pagination.page + '/' + _data.pagination.total_pages));
  return '<aside class="sidebar"><div class="sidebar-inner"><div class="sb-title">Session Info</div>' + rows.join('\n') + '</div></aside>';
}

// Main render
var app = document.getElementById('app');
var navHtml = buildPagination();

// Build entries thread
var blocks = [];
var inAssistantTurn = false;
_data.entries.forEach(function(entry) {
  var etype = entry.type || '';
  var role = (entry.message || {}).role || '';
  // Skip tool_result entries
  var ct = (etype === 'user' && role === 'user') ? (entry.message || {}).content : null;
  if (Array.isArray(ct) && ct.some(function(c) { return c && c.type === 'tool_result'; })) return;

  var html = renderEntry(entry);
  if (!html) return;
  var eidx = entry._idx != null ? entry._idx : -1;
  var anchor = eidx >= 0 ? ' id="e-' + eidx + '"' : '';
  var curl = ctxUrl(entry);
  var isAssistant = etype === 'assistant' && role === 'assistant';

  if (isAssistant) {
    if (curl) {
      if (inAssistantTurn) { blocks.push('</div>'); inAssistantTurn = false; }
      blocks.push('<a class="ctx-wrap" href="' + curl + '"' + anchor + '>' + html + '</a>');
    } else {
      if (!inAssistantTurn) { blocks.push('<div class="turn-body"' + anchor + '>'); inAssistantTurn = true; }
      blocks.push(html);
    }
  } else {
    if (inAssistantTurn) { blocks.push('</div>'); inAssistantTurn = false; }
    if (curl) { blocks.push('<a class="ctx-wrap" href="' + curl + '"' + anchor + '>' + html + '</a>'); }
    else if (anchor) { blocks.push('<div' + anchor + '>' + html + '</div>'); }
    else { blocks.push(html); }
  }
});
if (inAssistantTurn) blocks.push('</div>');

var updUrl = _data.live_base_url ? esc(_data.live_base_url) + '/updates' : '/api/updates';
var pgUrl = _data.live_base_url ? esc(_data.live_base_url) : '/';
var searchAttr = _data.search_query ? ' data-search="' + esc(_data.search_query) + '"' : '';

app.innerHTML = renderHeader()
  + renderSearch() + renderFilterBanner() + renderSearchInfo() + navHtml
  + '<div id="live-banner" class="live-banner" style="display:none"></div>'
  + '<div class="thread" id="thread"' + searchAttr
  + ' data-total="' + _data.pagination.total + '" data-name="' + esc(_data.name) + '"'
  + ' data-token="' + esc(_data.token || '') + '" data-updates-url="' + updUrl + '"'
  + ' data-page-url="' + pgUrl + '" data-per-page="' + _data.pagination.per_page + '"'
  + ' data-page="' + _data.pagination.page + '" data-total-pages="' + _data.pagination.total_pages + '">'
  + blocks.join('') + '</div>' + navHtml;

// Close content div, add sidebar + jump buttons
app.parentElement.parentElement.innerHTML = '<div class="main"><div class="content">' + app.innerHTML + '</div>' + renderSidebar() + '</div>'
  + '<div class="jump"><a href="#" title="Top" onclick="window.scrollTo(0,0);return false">↑</a><a href="#" title="Bottom" onclick="window.scrollTo(0,document.body.scrollHeight);return false">↓</a></div>';

// Post-render: wrap tables, add copy buttons
document.querySelectorAll('.a-text table').forEach(function(table) {
  var wrap = document.createElement('div'); wrap.className = 'table-wrap';
  table.parentNode.insertBefore(wrap, table); wrap.appendChild(table);
});
document.querySelectorAll('.a-text pre').forEach(function(pre) {
  var btn = document.createElement('button'); btn.className = 'copy-btn'; btn.textContent = 'Copy';
  btn.onclick = function() {
    var code = pre.querySelector('code');
    navigator.clipboard.writeText(code ? code.textContent : pre.textContent).then(function() {
      btn.textContent = 'Copied!'; btn.classList.add('copied');
      setTimeout(function() { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 2000);
    });
  };
  pre.appendChild(btn);
});

// Keyboard shortcuts
document.addEventListener('keydown', function(e) {
  if (e.target.tagName === 'INPUT') return;
  if (e.key === '/') { e.preventDefault(); document.querySelector('.search-bar input').focus(); }
  if (e.key === 'Home') window.scrollTo(0, 0);
  if (e.key === 'End') window.scrollTo(0, document.body.scrollHeight);
});

// Search term highlighting
(function() {
  var thread = document.getElementById('thread');
  var q = thread && thread.getAttribute('data-search');
  if (!q) return;
  var terms = q.split(/\s+/).filter(function(t) { return t.length > 0; });
  if (!terms.length) return;
  var pattern = new RegExp('(' + terms.map(function(t) {
    return t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }).join('|') + ')', 'gi');
  function walk(node) {
    if (node.nodeType === 3) {
      var text = node.textContent;
      if (!pattern.test(text)) return;
      pattern.lastIndex = 0;
      var frag = document.createDocumentFragment();
      var last = 0, match;
      while ((match = pattern.exec(text)) !== null) {
        if (match.index > last) frag.appendChild(document.createTextNode(text.slice(last, match.index)));
        var mark = document.createElement('mark'); mark.textContent = match[0]; frag.appendChild(mark);
        last = pattern.lastIndex;
      }
      if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
      node.parentNode.replaceChild(frag, node);
    } else if (node.nodeType === 1 && !/^(script|style|mark|code|pre)$/i.test(node.tagName)) {
      Array.from(node.childNodes).forEach(walk);
    }
  }
  thread.querySelectorAll('.u-text, .a-text').forEach(function(el) { walk(el); });
})();

// Timestamp localization
var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
document.querySelectorAll('.ts[data-ts]').forEach(function(el) {
  try {
    var d = new Date(el.getAttribute('data-ts'));
    if (!isNaN(d)) {
      el.textContent = months[d.getMonth()] + ' ' + d.getDate() + ' ' + String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0');
    }
  } catch(e) {}
});

// Live updates polling
(function() {
  var thread = document.getElementById('thread');
  if (!thread) return;
  var total = parseInt(thread.getAttribute('data-total')) || 0;
  var updatesUrl = thread.getAttribute('data-updates-url');
  var pageUrlAttr = thread.getAttribute('data-page-url');
  var token = thread.getAttribute('data-token');
  var perPage = thread.getAttribute('data-per-page') || '50';
  var curPage = parseInt(thread.getAttribute('data-page')) || 1;
  var totalPages = parseInt(thread.getAttribute('data-total-pages')) || 1;
  if (!updatesUrl || !token || curPage < totalPages) return;
  var banner = document.getElementById('live-banner');
  var polling = true;
  var pollUrl = updatesUrl + '?token=' + encodeURIComponent(token) + '&since=' + total;
  function poll() {
    if (!polling) return;
    fetch(pollUrl).then(function(r) { return r.json(); }).then(function(d) {
      if (d.new > 0) {
        banner.textContent = d.new + ' new entr' + (d.new === 1 ? 'y' : 'ies') + ' — click to load';
        banner.style.display = 'block';
        polling = false;
      } else { setTimeout(poll, 5000); }
    }).catch(function() { setTimeout(poll, 10000); });
  }
  banner.addEventListener('click', function() {
    window.location.href = pageUrlAttr + '?token=' + encodeURIComponent(token) + '&per_page=' + perPage;
  });
  setTimeout(poll, 5000);
})();
})();
