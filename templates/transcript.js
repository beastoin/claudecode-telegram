// Render markdown blocks with marked.js + highlight.js
marked.setOptions({
  highlight: function(code, lang) {
    if (lang && hljs.getLanguage(lang)) {
      return hljs.highlight(code, {language: lang}).value;
    }
    return hljs.highlightAuto(code).value;
  },
  breaks: true,
  gfm: true
});
function decodeB64Utf8(b64) {
  var bin = atob(b64);
  var bytes = new Uint8Array(bin.length);
  for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new TextDecoder('utf-8').decode(bytes);
}
document.querySelectorAll('.markdown[data-md]').forEach(function(el) {
  try {
    var md = decodeB64Utf8(el.getAttribute('data-md'));
    el.innerHTML = marked.parse(md);
  } catch(e) {
    el.textContent = 'Error rendering markdown: ' + e.message;
  }
});
// Wrap tables in scroll containers
document.querySelectorAll('.a-text table').forEach(function(table) {
  var wrap = document.createElement('div');
  wrap.className = 'table-wrap';
  table.parentNode.insertBefore(wrap, table);
  wrap.appendChild(table);
});
// Add copy buttons to code blocks
document.querySelectorAll('.a-text pre').forEach(function(pre) {
  var btn = document.createElement('button');
  btn.className = 'copy-btn';
  btn.textContent = 'Copy';
  btn.onclick = function() {
    var code = pre.querySelector('code');
    navigator.clipboard.writeText(code ? code.textContent : pre.textContent).then(function() {
      btn.textContent = 'Copied!';
      btn.classList.add('copied');
      setTimeout(function() { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 2000);
    });
  };
  pre.appendChild(btn);
});
// Keyboard shortcuts
document.addEventListener('keydown', function(e) {
  if (e.target.tagName === 'INPUT') return;
  if (e.key === '/') { e.preventDefault(); document.querySelector('.search-bar input').focus(); }
  if (e.key === 'Home') { window.scrollTo(0,0); }
  if (e.key === 'End') { window.scrollTo(0,document.body.scrollHeight); }
});
// Highlight search terms in thread content
(function() {
  var thread = document.getElementById('thread');
  var q = thread && thread.getAttribute('data-search');
  if (!q) return;
  var terms = q.split(/\\s+/).filter(function(t) { return t.length > 0; });
  if (!terms.length) return;
  var pattern = new RegExp('(' + terms.map(function(t) {
    return t.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
  }).join('|') + ')', 'gi');
  function walk(node) {
    if (node.nodeType === 3) {
      var text = node.textContent;
      if (!pattern.test(text)) return;
      pattern.lastIndex = 0;
      var frag = document.createDocumentFragment();
      var last = 0;
      var match;
      while ((match = pattern.exec(text)) !== null) {
        if (match.index > last) frag.appendChild(document.createTextNode(text.slice(last, match.index)));
        var mark = document.createElement('mark');
        mark.textContent = match[0];
        frag.appendChild(mark);
        last = pattern.lastIndex;
      }
      if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
      node.parentNode.replaceChild(frag, node);
    } else if (node.nodeType === 1 && !/^(script|style|mark|code|pre)$/i.test(node.tagName)) {
      var children = Array.from(node.childNodes);
      for (var i = 0; i < children.length; i++) walk(children[i]);
    }
  }
  // Highlight in user messages and assistant text
  thread.querySelectorAll('.u-text, .a-text').forEach(function(el) { walk(el); });
})();
// Render timestamps in browser timezone
var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
document.querySelectorAll('.ts[data-ts]').forEach(function(el) {
  try {
    var d = new Date(el.getAttribute('data-ts'));
    if (!isNaN(d)) {
      var mon = months[d.getMonth()];
      var day = d.getDate();
      var h = String(d.getHours()).padStart(2,'0');
      var m = String(d.getMinutes()).padStart(2,'0');
      el.textContent = mon + ' ' + day + ' ' + h + ':' + m;
    }
  } catch(e) {}
});
// Live updates: poll for new entries, show banner when available
(function() {
  var thread = document.getElementById('thread');
  if (!thread) return;
  var total = parseInt(thread.getAttribute('data-total')) || 0;
  var updatesUrl = thread.getAttribute('data-updates-url');
  var pageUrl = thread.getAttribute('data-page-url');
  var token = thread.getAttribute('data-token');
  var perPage = thread.getAttribute('data-per-page') || '50';
  var curPage = parseInt(thread.getAttribute('data-page')) || 1;
  var totalPages = parseInt(thread.getAttribute('data-total-pages')) || 1;
  if (!updatesUrl || !token) return;
  // Only poll when viewing the last page (most recent entries)
  if (curPage < totalPages) return;
  var banner = document.getElementById('live-banner');
  var polling = true;
  var pollUrl = updatesUrl + '?token=' + encodeURIComponent(token) + '&since=' + total;
  function poll() {
    if (!polling) return;
    fetch(pollUrl).then(function(r) { return r.json(); }).then(function(d) {
      if (d.new > 0) {
        banner.textContent = d.new + ' new entr' + (d.new === 1 ? 'y' : 'ies') + ' — click to load';
        banner.style.display = 'block';
        polling = false;  // Stop polling once banner is shown
      } else {
        setTimeout(poll, 5000);
      }
    }).catch(function() {
      setTimeout(poll, 10000);  // Retry slower on error
    });
  }
  banner.addEventListener('click', function() {
    // Navigate to last page of transcript (fresh render with new entries)
    var url = pageUrl + '?token=' + encodeURIComponent(token) + '&per_page=' + perPage;
    window.location.href = url;
  });
  setTimeout(poll, 5000);  // Start polling after 5s
})();
