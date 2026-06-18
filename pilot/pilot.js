#!/usr/bin/env node

import fs from 'fs';
import http from 'http';
import path from 'path';
import { execFile } from 'child_process';
import { promisify } from 'util';
import { createRequire } from 'module';
import pty from '@lydell/node-pty';
import { WebSocketServer } from 'ws';

const execFileAsync = promisify(execFile);
const require = createRequire(import.meta.url);

const PORT = Number.parseInt(process.env.PORT || '10170', 10);
const PILOT_TOKEN = process.env.PILOT_TOKEN || '';
const ACTIVE_CONNECTIONS = new Map();
const ENABLED_SESSIONS = new Map(); // sessionName -> { host?: string }
const SESSION_TIMERS = new Map(); // sessionName -> timeout handle
const PILOT_TIMEOUT_MS = 5 * 60 * 1000; // 5 minutes
const GRID_SESSIONS = new Map(); // slug -> { sessions: [...], createdAt, expiresAt, timer }

function enableSession(sessionName, host) {
  ENABLED_SESSIONS.set(sessionName, { host: host || null });
  refreshSessionTimer(sessionName);
}

function disableSession(sessionName) {
  ENABLED_SESSIONS.delete(sessionName);
  const timer = SESSION_TIMERS.get(sessionName);
  if (timer) {
    clearTimeout(timer);
    SESSION_TIMERS.delete(sessionName);
  }
}

function refreshSessionTimer(sessionName) {
  const existing = SESSION_TIMERS.get(sessionName);
  if (existing) clearTimeout(existing);
  SESSION_TIMERS.set(sessionName, setTimeout(() => {
    console.log(`[pilot] auto-disabling ${sessionName} (5min idle)`);
    disableSession(sessionName);
  }, PILOT_TIMEOUT_MS));
}

function createGridSession(slug, sessionNames, ttlMs) {
  const existing = GRID_SESSIONS.get(slug);
  if (existing && existing.timer) clearTimeout(existing.timer);
  const now = Date.now();
  const expiresAt = now + ttlMs;
  const timer = setTimeout(() => {
    console.log(`[pilot] grid session '${slug}' expired`);
    GRID_SESSIONS.delete(slug);
  }, ttlMs);
  GRID_SESSIONS.set(slug, { sessions: sessionNames, createdAt: now, expiresAt, timer });
  return { slug, expiresAt };
}

function getGridSession(slug) {
  const gs = GRID_SESSIONS.get(slug);
  if (!gs) return null;
  if (Date.now() >= gs.expiresAt) {
    GRID_SESSIONS.delete(slug);
    return null;
  }
  return gs;
}

const MIME_TYPES = {
  '.css': 'text/css; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.mjs': 'application/javascript; charset=utf-8',
  '.wasm': 'application/wasm',
};

function resolveGhosttyAssets() {
  const resolved = require.resolve('ghostty-web');
  const packageRoot = resolved.replace(/[/\\]dist[/\\].*$/, '');
  const distPath = path.join(packageRoot, 'dist');
  const wasmPath = path.join(packageRoot, 'ghostty-vt.wasm');

  if (!fs.existsSync(path.join(distPath, 'ghostty-web.js')) || !fs.existsSync(wasmPath)) {
    throw new Error('ghostty-web assets were not found in node_modules');
  }

  return { distPath, wasmPath };
}

const { distPath, wasmPath } = resolveGhosttyAssets();

function escapeHtml(value) {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function parseIntOrDefault(value, fallback) {
  const parsed = Number.parseInt(value || '', 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return fallback;
  }
  return Math.min(parsed, 500);
}

function isAllowedSessionName(name) {
  return typeof name === 'string' && /^claude-[A-Za-z0-9._:-]+$/.test(name);
}

function isAuthorized(urlObj) {
  if (!PILOT_TOKEN) {
    return true;
  }
  return urlObj.searchParams.get('token') === PILOT_TOKEN;
}

function getTokenForTemplates(urlObj) {
  if (!PILOT_TOKEN) {
    return '';
  }
  return urlObj.searchParams.get('token') || '';
}

function tokenSearch(token) {
  return token ? `?token=${encodeURIComponent(token)}` : '';
}

async function diagnoseRemote(host, sessionName) {
  try {
    await execFileAsync('ssh', ['-o', 'ConnectTimeout=3', host, 'true'], { timeout: 5000 });
  } catch {
    return `Cannot reach ${host} — SSH connection failed`;
  }
  try {
    await execFileAsync('ssh', ['-o', 'ConnectTimeout=3', host, `tmux has-session -t '${sessionName.replace(/'/g, "'\\''")}'`], { timeout: 5000 });
  } catch {
    return `Session ${sessionName} does not exist on ${host}`;
  }
  try {
    const { stdout } = await execFileAsync('ssh', ['-o', 'ConnectTimeout=3', host,
      'ptys=$(sysctl -n kern.tty.ptmx_max 2>/dev/null || echo 0); used=$(lsof /dev/ptmx 2>/dev/null | tail -n+2 | wc -l | tr -d " "); echo "$used/$ptys"'
    ], { timeout: 5000 });
    const parts = stdout.trim().split('/');
    if (parts.length === 2) {
      const used = parseInt(parts[0], 10);
      const max = parseInt(parts[1], 10);
      if (max > 0 && used >= max) {
        return `PTY exhaustion on ${host}: ${used}/${max} PTYs in use — cannot allocate terminal (try killing Terminal.app or leaked processes)`;
      }
    }
  } catch {
    // PTY check failed, not fatal
  }
  return null;
}

async function execTmux(args, host) {
  if (host) {
    const quotedArgs = args.map((a) => `'${a.replace(/'/g, "'\\''")}'`);
    const remoteCmd = `tmux ${quotedArgs.join(' ')}`;
    return execFileAsync('ssh', ['-o', 'ConnectTimeout=3', host, remoteCmd]);
  }
  return execFileAsync('tmux', args);
}

async function listSessionsFrom(host) {
  let stdout = '';
  try {
    ({ stdout } = await execTmux([
      'list-sessions',
      '-F',
      '#{session_name}:#{session_attached}:#{session_activity}',
    ], host));
  } catch (error) {
    const stderr = `${error.stderr || ''}`.toLowerCase();
    if (stderr.includes('no server running')) {
      return [];
    }
    throw error;
  }

  return stdout
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const [name = '', attachedRaw = '0', activityRaw = '0'] = line.split(':');
      return {
        name,
        attached: attachedRaw === '1',
        activity: Number.parseInt(activityRaw, 10) || 0,
      };
    })
    .filter((session) => isAllowedSessionName(session.name));
}

async function listSessions() {
  const local = await listSessionsFrom(null);

  // Also list sessions from remote hosts of enabled sessions
  const remoteHosts = new Set();
  for (const [, entry] of ENABLED_SESSIONS) {
    if (entry.host) remoteHosts.add(entry.host);
  }

  const remoteResults = await Promise.all(
    [...remoteHosts].map((host) =>
      listSessionsFrom(host).catch((err) => {
        console.log(`[pilot] remote list-sessions from ${host} failed: ${err.message}`);
        return [];
      })
    )
  );

  const localNames = new Set(local.map((s) => s.name));
  for (const remoteSessions of remoteResults) {
    for (const session of remoteSessions) {
      if (!localNames.has(session.name)) {
        local.push(session);
      }
    }
  }

  return local;
}

function isEnabledSessionName(sessionName) {
  return ENABLED_SESSIONS.has(sessionName);
}

function getSessionHost(sessionName) {
  const entry = ENABLED_SESSIONS.get(sessionName);
  return entry ? entry.host : null;
}

function isRemoteSession(sessionName) {
  return !!getSessionHost(sessionName);
}

function filterEnabledSessions(sessions) {
  return sessions.filter((session) => isEnabledSessionName(session.name));
}

async function sessionExists(sessionName) {
  try {
    const host = getSessionHost(sessionName);
    await execTmux(['has-session', '-t', sessionName], host);
    return true;
  } catch {
    return false;
  }
}

function sendJson(res, statusCode, data) {
  res.writeHead(statusCode, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
  });
  res.end(JSON.stringify(data));
}

function sendText(res, statusCode, body) {
  res.writeHead(statusCode, { 'Content-Type': 'text/plain; charset=utf-8' });
  res.end(body);
}

function sendHtml(res, statusCode, html) {
  res.writeHead(statusCode, { 'Content-Type': 'text/html; charset=utf-8' });
  res.end(html);
}

async function serveFile(filePath, res) {
  const ext = path.extname(filePath);
  const contentType = MIME_TYPES[ext] || 'application/octet-stream';
  try {
    const data = await fs.promises.readFile(filePath);
    res.writeHead(200, { 'Content-Type': contentType });
    res.end(data);
  } catch {
    sendText(res, 404, 'Not Found');
  }
}

function renderIndexPage({ sessions, token, enabledCount }) {
  const tokenQuery = tokenSearch(token);
  const hasEnabledSessions = enabledCount > 0;
  const emptyMessage = hasEnabledSessions
    ? 'No enabled sessions are currently running.'
    : 'No sessions enabled.';
  const hintMessage = hasEnabledSessions
    ? 'Open a session to attach. Use readonly mode to observe without sending input.'
    : 'Use /pilot &lt;name&gt; in Telegram to enable a session.';
  const listHtml = sessions.length
    ? sessions
        .map((session) => {
          const safeName = escapeHtml(session.name);
          const sessionHref = `/session/${encodeURIComponent(session.name)}${tokenQuery}`;
          const readonlyHref = `${sessionHref}${tokenQuery ? '&' : '?'}readonly=1`;
          return `<li class="session-item">
            <a class="session-link" href="${sessionHref}">
              <span class="name">${safeName}</span>
              <span class="meta">${session.attached ? 'attached' : 'detached'}</span>
            </a>
            <a class="readonly-link" href="${readonlyHref}">readonly</a>
          </li>`;
        })
        .join('\n')
    : `<li class="empty">${emptyMessage}</li>`;

  return `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Pilot Session Picker</title>
    <style>
      :root {
        --bg: #0b1020;
        --card: #101a35;
        --line: #263456;
        --text: #eaf1ff;
        --muted: #94a3c6;
        --accent: #5fd0ff;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        min-height: 100vh;
        font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: radial-gradient(100% 120% at 80% 0%, #14326d 0%, var(--bg) 60%);
        color: var(--text);
        padding: 24px;
      }
      .card {
        max-width: 860px;
        margin: 0 auto;
        background: color-mix(in oklab, var(--card) 85%, #000 15%);
        border: 1px solid var(--line);
        border-radius: 16px;
        overflow: hidden;
      }
      .header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding: 16px 20px;
        border-bottom: 1px solid var(--line);
      }
      h1 {
        margin: 0;
        font-size: 20px;
      }
      .btn {
        border: 1px solid var(--line);
        border-radius: 10px;
        background: transparent;
        color: var(--text);
        padding: 8px 12px;
        cursor: pointer;
      }
      ul {
        margin: 0;
        padding: 0;
        list-style: none;
      }
      .session-item {
        padding: 14px 20px;
        border-bottom: 1px solid var(--line);
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 10px;
      }
      .session-link {
        color: inherit;
        text-decoration: none;
        display: inline-flex;
        flex-direction: column;
        gap: 4px;
      }
      .name {
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      }
      .meta {
        color: var(--muted);
        font-size: 12px;
      }
      .readonly-link {
        color: var(--accent);
        text-decoration: none;
        font-size: 13px;
      }
      .empty {
        padding: 20px;
        color: var(--muted);
      }
      .hint {
        padding: 12px 20px 18px;
        color: var(--muted);
        font-size: 13px;
      }
      @media (max-width: 700px) {
        body { padding: 12px; }
        .session-item { padding: 12px; }
      }
    </style>
  </head>
  <body>
    <main class="card">
      <header class="header">
        <h1>Pilot Sessions</h1>
        <button class="btn" id="refresh">Refresh</button>
      </header>
      <ul id="session-list">
        ${listHtml}
      </ul>
      <div class="hint" id="hint">${hintMessage}</div>
    </main>
    <script type="module">
      const token = ${JSON.stringify(token)};
      let enabledCount = ${Number.isFinite(enabledCount) ? enabledCount : 0};
      const list = document.getElementById('session-list');
      const hint = document.getElementById('hint');
      const refresh = document.getElementById('refresh');
      const query = token ? ('?' + new URLSearchParams({ token }).toString()) : '';

      function emptyMessage() {
        return enabledCount > 0
          ? 'No enabled sessions are currently running.'
          : 'No sessions enabled.';
      }

      function hintMessage() {
        return enabledCount > 0
          ? 'Open a session to attach. Use readonly mode to observe without sending input.'
          : 'Use /pilot &lt;name&gt; in Telegram to enable a session.';
      }

      function render(items) {
        if (!Array.isArray(items) || items.length === 0) {
          list.innerHTML = '<li class="empty">' + emptyMessage() + '</li>';
          hint.textContent = hintMessage();
          return;
        }
        list.innerHTML = items.map((session) => {
          const safeName = String(session.name || '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
          const sessionHref = '/session/' + encodeURIComponent(session.name) + query;
          const readonlyHref = sessionHref + (query ? '&' : '?') + 'readonly=1';
          return '<li class="session-item">' +
            '<a class="session-link" href="' + sessionHref + '">' +
            '<span class="name">' + safeName + '</span>' +
            '<span class="meta">' + (session.attached ? 'attached' : 'detached') + '</span>' +
            '</a>' +
            '<a class="readonly-link" href="' + readonlyHref + '">readonly</a>' +
            '</li>';
        }).join('');
        hint.textContent = hintMessage();
      }

      async function loadSessions() {
        const [sessionsResponse, enabledResponse] = await Promise.all([
          fetch('/api/sessions' + query),
          fetch('/api/enabled' + query),
        ]);
        if (!sessionsResponse.ok || !enabledResponse.ok) {
          return;
        }
        const enabled = await enabledResponse.json();
        enabledCount = Array.isArray(enabled) ? enabled.length : 0;
        render(await sessionsResponse.json());
      }

      refresh.addEventListener('click', () => {
        loadSessions().catch(() => {});
      });
      loadSessions().catch(() => {});
    </script>
  </body>
</html>`;
}

function renderSessionPage({ sessionName, token, readonly, embed }) {
  const hideChrome = embed ? 'display:none !important;' : '';
  return `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
    <title>Pilot: ${escapeHtml(sessionName)}</title>
    <style>
      :root {
        --bg: #04070f;
        --panel: #0f1528;
        --line: #293554;
        --text: #edf2ff;
        --muted: #90a0c2;
        --good: #4ade80;
        --warn: #f59e0b;
        --bad: #ef4444;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        min-height: 100vh;
        background: linear-gradient(180deg, #071026 0%, var(--bg) 100%);
        color: var(--text);
        font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }
      .window {
        min-height: 100vh;
        display: flex;
        flex-direction: column;
      }
      .bar {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 10px 12px;
        background: var(--panel);
        border-bottom: 1px solid var(--line);
        ${hideChrome}
      }
      .back {
        color: var(--text);
        text-decoration: none;
        font-size: 13px;
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 5px 9px;
      }
      .session {
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 13px;
      }
      .status {
        margin-left: auto;
        display: inline-flex;
        align-items: center;
        gap: 8px;
        color: var(--muted);
        font-size: 12px;
      }
      .dot {
        width: 10px;
        height: 10px;
        border-radius: 999px;
        background: var(--warn);
      }
      .dot.connected { background: var(--good); }
      .dot.disconnected { background: var(--bad); }
      .badge {
        margin-left: 8px;
        border: 1px solid var(--line);
        border-radius: 999px;
        padding: 2px 8px;
        font-size: 11px;
        color: var(--muted);
      }
      #terminal {
        flex: 1;
        min-height: 0;
        padding: ${embed ? '0' : '10px'};
        position: relative;
        ${embed ? 'overflow: hidden;' : ''}
      }
      ${embed ? `
      #terminal *, #terminal ::-webkit-scrollbar { scrollbar-width: none; }
      #terminal *::-webkit-scrollbar { display: none; }
      html, body { overflow: hidden; }
      ` : ''}
      #mobile-input {
        position: absolute;
        top: 0; left: 0;
        width: 100%; height: 100%;
        opacity: 0;
        z-index: 10;
        font-size: 16px;
        resize: none;
        caret-color: transparent;
        -webkit-user-select: none;
        user-select: none;
      }
      .toolbar {
        display: none;
        gap: 6px;
        padding: 8px 10px;
        background: var(--panel);
        border-top: 1px solid var(--line);
        flex-wrap: wrap;
        position: fixed;
        bottom: 0;
        left: 0;
        right: 0;
        z-index: 20;
        ${hideChrome}
      }
      .toolbar button {
        background: #1a2744;
        color: var(--text);
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 10px 14px;
        font-size: 14px;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        cursor: pointer;
        -webkit-tap-highlight-color: transparent;
      }
      .toolbar button:active {
        background: #2a3f66;
      }
      .toolbar .key-enter { background: #1a3a2a; border-color: #2a5a3a; }
      #ctrl-btn.armed { background: #5f3a1a; border-color: #8a5a2a; }
      #select-overlay {
        position: fixed;
        inset: 0;
        display: none;
        background: rgba(3, 6, 13, 0.85);
        z-index: 30;
        padding: 14px;
        ${hideChrome}
      }
      .select-panel {
        width: 100%;
        height: 100%;
        background: #0a1326;
        border: 1px solid var(--line);
        border-radius: 12px;
        overflow: hidden;
        display: flex;
        flex-direction: column;
      }
      .select-header {
        display: flex;
        align-items: center;
        justify-content: flex-end;
        gap: 8px;
        padding: 10px;
        border-bottom: 1px solid var(--line);
        background: var(--panel);
      }
      .select-header button {
        background: #1a2744;
        color: var(--text);
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 8px 12px;
        font-size: 13px;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        cursor: pointer;
      }
      #select-text {
        margin: 0;
        padding: 14px;
        overflow: auto;
        flex: 1;
        white-space: pre-wrap;
        word-break: break-word;
        color: var(--text);
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 13px;
        line-height: 1.35;
        -webkit-user-select: text;
        user-select: text;
      }
      @media (pointer: coarse) {
        .toolbar { display: flex; }
        #mobile-input { pointer-events: none; }
        #terminal { padding-bottom: 56px; }
      }
      @media (pointer: fine) {
        #mobile-input { display: none; }
      }
    </style>
  </head>
  <body>
    <main class="window">
      <header class="bar">
        <a class="back" href="/${tokenSearch(token)}">sessions</a>
        <span class="session">${escapeHtml(sessionName)}</span>
        ${readonly ? '<span class="badge">readonly</span>' : ''}
        <span class="status"><span class="dot" id="status-dot"></span><span id="status-text">connecting</span></span>
      </header>
      <section id="terminal">
        <textarea id="mobile-input" autocapitalize="off" autocomplete="off" autocorrect="off" spellcheck="false" enterkeyhint="send"></textarea>
      </section>
      <nav class="toolbar" id="toolbar">
        <button class="key-enter" data-seq="cr">Enter</button>
        <button id="ctrl-btn">Ctrl</button>
        <button data-seq="esc">Esc</button>
        <button data-seq="up">▲</button>
        <button data-seq="down">▼</button>
        <button data-seq="pageup">PgUp</button>
        <button data-seq="pagedn">PgDn</button>
        <button data-seq="first">First</button>
        <button data-seq="last">Last</button>
        <button id="select-btn">Select</button>
      </nav>
    </main>
    <div id="select-overlay" style="display:none">
      <div class="select-panel">
        <div class="select-header">
          <button id="select-close">Close</button>
          <button id="select-copy-all">Copy All</button>
        </div>
        <pre id="select-text"></pre>
      </div>
    </div>

    <script type="module">
      try {
      const sessionName = ${JSON.stringify(sessionName)};
      const token = ${JSON.stringify(token)};
      const readonly = ${readonly ? 'true' : 'false'};
      const embed = ${embed ? 'true' : 'false'};
      const statusDot = document.getElementById('status-dot');
      const statusText = document.getElementById('status-text');

      function setStatus(type, text) {
        statusDot.className = 'dot ' + type;
        statusText.textContent = text;
      }

      const originalFetch = window.fetch.bind(window);
      window.fetch = (input, init) => {
        if (!token) {
          return originalFetch(input, init);
        }

        const url = new URL(typeof input === 'string' ? input : input.url, window.location.origin);
        if (url.origin === window.location.origin && !url.searchParams.has('token')) {
          url.searchParams.set('token', token);
        }
        if (typeof input === 'string') {
          return originalFetch(url.toString(), init);
        }
        return originalFetch(new Request(url.toString(), input), init);
      };

      let terminal, fitAddon;
      try {
        setStatus('', 'loading terminal...');
        const moduleUrl = '/dist/ghostty-web.js' + (token ? ('?' + new URLSearchParams({ token }).toString()) : '');
        const { init, Terminal, FitAddon } = await import(moduleUrl);
        await init();

        terminal = new Terminal({
          cols: 80,
          rows: 24,
          fontFamily: 'JetBrains Mono, Menlo, Monaco, monospace',
          fontSize: embed ? 10 : 14,
        });
        fitAddon = new FitAddon();
        terminal.loadAddon(fitAddon);

        await terminal.open(document.getElementById('terminal'));
        fitAddon.fit();
        fitAddon.observeResize();
      } catch (err) {
        setStatus('disconnected', 'terminal load failed');
        document.getElementById('terminal').innerHTML =
          '<pre style="color:#ef4444;padding:20px;font-size:14px;">Failed to load ghostty-web:\\n' +
          err.message + '\\n\\nThis may be a browser compatibility issue.\\nTry Chrome/Safari desktop.</pre>';
        throw err;
      }

      let ws;
      let reconnectTimer = null;
      let reconnectAttempts = 0;
      let reconnectStopped = false;

      function websocketUrl() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = new URL(protocol + '//' + window.location.host + '/ws');
        url.searchParams.set('session', sessionName);
        url.searchParams.set('cols', String(terminal.cols));
        url.searchParams.set('rows', String(terminal.rows));
        if (readonly) {
          url.searchParams.set('readonly', '1');
        }
        if (token) {
          url.searchParams.set('token', token);
        }
        return url.toString();
      }

      function connect() {
        if (reconnectStopped) return;
        setStatus('connecting', 'connecting');
        ws = new WebSocket(websocketUrl());

        ws.onopen = () => { setStatus('connected', 'connected'); reconnectAttempts = 0; };
        ws.onmessage = (event) => terminal.write(event.data);
        ws.onerror = () => setStatus('disconnected', 'error');
        ws.onclose = (event) => {
          setStatus('disconnected', 'disconnected');
          // code 4000 = diagnostic failure (PTY exhaustion, host unreachable, etc.)
          if (event.code === 4000) {
            reconnectStopped = true;
            terminal.write('\\r\\n\\x1b[33mReconnect stopped — fix the issue above, then refresh the page.\\x1b[0m\\r\\n');
            return;
          }
          reconnectAttempts++;
          const delay = Math.min(2000 * Math.pow(2, reconnectAttempts - 1), 30000);
          reconnectTimer = setTimeout(connect, delay);
        };
      }

      connect();

      terminal.onData((data) => {
        if (readonly) {
          return;
        }
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(data);
        }
      });

      terminal.onResize(({ cols, rows }) => {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'resize', cols, rows }));
        }
      });

      window.addEventListener('resize', () => {
        fitAddon.fit();
      });

      // Toolbar buttons (touch devices)
      const SEQ = { cr: '\\r', esc: '\\x1b', up: '\\x1b[A', down: '\\x1b[B', pageup: '\\x02\\x1b[5~', pagedn: '\\x1b[6~', last: 'q' };
      // First needs split sends (Ctrl+B [ then M-<) with delay so terminal parses correctly
      const MULTI = { first: ['\\x02[', '\\x1b<'] };
      document.querySelectorAll('#toolbar button[data-seq]').forEach((btn) => {
        btn.addEventListener('click', (e) => {
          e.preventDefault();
          if (readonly) return;
          if (!ws || ws.readyState !== WebSocket.OPEN) return;
          const key = btn.dataset.seq;
          if (MULTI[key]) {
            ws.send(MULTI[key][0]);
            setTimeout(() => ws.send(MULTI[key][1]), 150);
          } else if (SEQ[key]) {
            ws.send(SEQ[key]);
          }
        });
      });

      // Select mode overlay — fetches pane content for native text selection
      const selectOverlay = document.getElementById('select-overlay');
      const selectText = document.getElementById('select-text');
      const selectButton = document.getElementById('select-btn');
      const selectClose = document.getElementById('select-close');
      const selectCopyAll = document.getElementById('select-copy-all');
      let selectedPaneText = '';

      function closeSelectOverlay() {
        if (!selectOverlay) return;
        selectOverlay.style.display = 'none';
        document.body.style.overflow = '';
      }

      selectButton?.addEventListener('click', async (e) => {
        e.preventDefault();
        const btn = e.currentTarget;
        try {
          const r = await fetch('/api/capture?session=' + encodeURIComponent(sessionName));
          if (!r.ok) {
            throw new Error('capture failed');
          }
          const payload = await r.json();
          selectedPaneText = typeof payload.text === 'string' ? payload.text : '';
          if (selectText) {
            selectText.textContent = selectedPaneText;
          }
          if (selectOverlay) {
            selectOverlay.style.display = 'block';
            document.body.style.overflow = 'hidden';
          }
        } catch {
          btn.textContent = 'Failed';
          setTimeout(() => { btn.textContent = 'Select'; }, 1500);
        }
      });

      selectClose?.addEventListener('click', (e) => {
        e.preventDefault();
        closeSelectOverlay();
      });

      selectOverlay?.addEventListener('click', (e) => {
        if (e.target === selectOverlay) {
          closeSelectOverlay();
        }
      });

      selectCopyAll?.addEventListener('click', async (e) => {
        e.preventDefault();
        const btn = e.currentTarget;
        try {
          await navigator.clipboard.writeText(selectedPaneText);
          btn.textContent = 'Copied!';
        } catch {
          btn.textContent = 'Failed';
        }
        setTimeout(() => { btn.textContent = 'Copy All'; }, 1500);
      });

      // Tap terminal to bring up keyboard (touch devices)
      const mobileInput = document.getElementById('mobile-input');
      if (mobileInput) {
        document.getElementById('terminal')?.addEventListener('click', () => {
          mobileInput.style.pointerEvents = 'auto';
          mobileInput.focus();
          setTimeout(() => { mobileInput.style.pointerEvents = 'none'; }, 500);
        });
        mobileInput.addEventListener('input', (e) => {
          if (readonly) return;
          const text = e.target.value;
          if (text && ws && ws.readyState === WebSocket.OPEN) {
            if (ctrlArmed) {
              // Send Ctrl+<char> for each character
              for (const ch of text) {
                const code = ch.toUpperCase().charCodeAt(0);
                if (code >= 64 && code <= 95) {
                  ws.send(String.fromCharCode(code - 64));
                }
              }
              disarmCtrl();
            } else {
              ws.send(text);
            }
          }
          e.target.value = '';
        });
        mobileInput.addEventListener('keydown', (e) => {
          if (readonly) return;
          if (e.key === 'Enter') {
            e.preventDefault();
            if (ws && ws.readyState === WebSocket.OPEN) {
              ws.send('\\r');
            }
            if (ctrlArmed) disarmCtrl();
          }
        });
      }

      // Ctrl modifier button
      const ctrlBtn = document.getElementById('ctrl-btn');
      let ctrlArmed = false;
      function disarmCtrl() {
        ctrlArmed = false;
        if (ctrlBtn) ctrlBtn.classList.remove('armed');
      }
      ctrlBtn?.addEventListener('click', (e) => {
        e.preventDefault();
        ctrlArmed = !ctrlArmed;
        ctrlBtn.classList.toggle('armed', ctrlArmed);
        // Focus input so next keypress goes through
        if (ctrlArmed && mobileInput) {
          mobileInput.style.pointerEvents = 'auto';
          mobileInput.focus();
          setTimeout(() => { mobileInput.style.pointerEvents = 'none'; }, 500);
        }
      });

      // Handle iPad keyboard resize
      if (window.visualViewport) {
        const termEl = document.getElementById('terminal');
        const barEl = document.querySelector('.bar');
        const toolbarEl = document.getElementById('toolbar');
        window.visualViewport.addEventListener('resize', () => {
          const vh = window.visualViewport.height;
          const barH = barEl ? barEl.offsetHeight : 0;
          const tbH = toolbarEl ? toolbarEl.offsetHeight : 0;
          termEl.style.height = (vh - barH - tbH) + 'px';
          if (fitAddon) fitAddon.fit();
        });
      }

      window.addEventListener('beforeunload', () => {
        if (reconnectTimer) {
          clearTimeout(reconnectTimer);
        }
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.close();
        }
      });

      } catch (e) {
        const dot = document.getElementById('status-dot');
        const txt = document.getElementById('status-text');
        if (dot) dot.className = 'dot disconnected';
        if (txt) txt.textContent = 'error';
        const el = document.getElementById('terminal');
        if (el) el.innerHTML = '<pre style="color:#ef4444;padding:20px;font-size:14px;white-space:pre-wrap;">JS Error:\\n' + e.message + '\\n\\n' + (e.stack || '') + '</pre>';
      }
    </script>
  </body>
</html>`;
}

function renderGridPage({ token, slug, gridSession }) {
  const expiresAt = gridSession ? gridSession.expiresAt : 0;
  const filterSessions = gridSession ? JSON.stringify(gridSession.sessions) : 'null';
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Pilot${slug ? ' — ' + escapeHtml(slug) : ''}</title>
  <style>
    :root {
      --bg: #0a0a08;
      --surface: #1a1814;
      --border: #3d362e;
      --text: #40d870;
      --text-muted: #2a7a40;
      --green: #40d870;
      --accent: #60ff90;
      --hover: #252018;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, monospace;
      overflow-x: hidden;
    }

    .topbar {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 10px 14px;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      position: sticky;
      top: 0;
      z-index: 10;
    }
    .topbar h1 {
      font-size: 14px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 2px;
      text-shadow: 0 0 8px rgba(64, 216, 112, 0.5);
    }
    .worker-count { color: var(--text-muted); font-size: 12px; }
    .countdown {
      font-size: 11px;
      color: var(--text-muted);
      font-variant-numeric: tabular-nums;
    }
    .countdown.urgent { color: #f59e0b; text-shadow: 0 0 4px rgba(245, 158, 11, 0.4); }
    .countdown.expired { color: #ef4444; }
    .topbar-actions { margin-left: auto; }

    .btn {
      background: transparent;
      border: 1px solid var(--border);
      color: var(--text);
      border-radius: 4px;
      padding: 4px 10px;
      cursor: pointer;
      font-size: 11px;
      font-family: inherit;
      text-transform: uppercase;
      letter-spacing: 1px;
    }
    .btn:hover { background: var(--hover); text-shadow: 0 0 6px rgba(64, 216, 112, 0.4); }

    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(min(480px, 100%), 1fr));
      gap: 10px;
      padding: 10px;
    }

    .panel {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 6px;
      overflow: hidden;
      cursor: pointer;
      transition: border-color 0.15s;
      display: flex;
      flex-direction: column;
    }
    .panel:hover { border-color: var(--accent); }

    .panel-header {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 6px 10px;
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
    }
    .status-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--green);
      flex-shrink: 0;
      box-shadow: 0 0 4px var(--green);
      animation: pulse 2s ease-in-out infinite;
    }
    @keyframes pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.6; }
    }
    .worker-name {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 13px;
      font-weight: 600;
      color: var(--green);
      text-shadow: 0 0 6px rgba(74, 222, 128, 0.3);
    }

    .panel-terminal {
      height: 260px;
      position: relative;
      background: #001200;
      border-radius: 12px;
      overflow: hidden;
      margin: 6px;
    }
    .panel-terminal iframe {
      width: 100%;
      height: 100%;
      border: none;
      pointer-events: none;
      filter: saturate(0.5) brightness(1.1);
    }
    /* CRT scanlines */
    .panel-terminal::after {
      content: '';
      position: absolute;
      inset: 0;
      background: repeating-linear-gradient(
        0deg,
        rgba(0,0,0,0.3) 0px,
        rgba(0,0,0,0.3) 1px,
        transparent 1px,
        transparent 2px
      );
      pointer-events: none;
      z-index: 2;
      border-radius: 12px;
    }
    /* CRT screen glow + heavy vignette */
    .panel-terminal::before {
      content: '';
      position: absolute;
      inset: 0;
      background: radial-gradient(
        ellipse 80% 80% at center,
        rgba(50, 200, 80, 0.06) 0%,
        rgba(30, 140, 50, 0.03) 40%,
        rgba(0, 0, 0, 0.7) 100%
      );
      pointer-events: none;
      z-index: 3;
      border-radius: 12px;
    }
    .panel {
      background: #001200;
      border: 1px solid #1a3a1a;
      border-radius: 8px;
      box-shadow: 0 0 12px rgba(50, 200, 80, 0.08);
    }
    .panel:hover {
      border-color: var(--green);
      box-shadow: 0 0 20px rgba(50, 200, 80, 0.15);
    }
    .panel-header {
      background: rgba(0, 18, 0, 0.8);
      border-bottom: 1px solid #1a3a1a;
    }
    /* Flicker animation */
    @keyframes flicker {
      0% { opacity: 1; }
      3% { opacity: 0.97; }
      6% { opacity: 1; }
      92% { opacity: 1; }
      94% { opacity: 0.96; }
      96% { opacity: 1; }
    }
    .panel-terminal iframe {
      animation: flicker 4s linear infinite;
    }

    .focus-overlay {
      position: fixed;
      inset: 0;
      background: var(--bg);
      z-index: 50;
      display: none;
      flex-direction: column;
    }
    .focus-overlay.active { display: flex; }

    .focus-bar {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 8px 14px;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
    }
    .focus-worker-name {
      font-size: 13px;
      color: var(--green);
      text-shadow: 0 0 6px rgba(64, 216, 112, 0.4);
    }
    .focus-overlay iframe {
      flex: 1;
      border: none;
      width: 100%;
      background: #001200;
    }

    /* Solo mode — single worker, full screen */
    .solo {
      position: fixed;
      top: 38px;
      left: 0;
      right: 0;
      bottom: 0;
    }
    .solo iframe {
      width: 100%;
      height: 100%;
      border: none;
      background: #001200;
    }

    .empty-state {
      text-align: center;
      color: var(--text-muted);
      padding: 80px 20px;
      font-size: 14px;
    }

    @media (max-width: 500px) {
      .grid { grid-template-columns: 1fr; gap: 4px; padding: 4px; }
      .panel-terminal { height: 200px; }
    }
  </style>
</head>
<body>
  <header class="topbar">
    <h1>Pilot</h1>
    <span class="worker-count" id="worker-count"></span>
    <span class="countdown" id="countdown"></span>
    <div class="topbar-actions">
      <button class="btn" id="refresh-button">Refresh</button>
    </div>
  </header>

  <div class="grid" id="grid-container"><div class="empty-state">Loading...</div></div>

  <div class="focus-overlay" id="focus-overlay">
    <div class="focus-bar">
      <button class="btn" id="back-to-grid">&larr; Grid</button>
      <span class="focus-worker-name" id="focus-worker-name"></span>
    </div>
    <iframe id="focus-iframe" src="about:blank"></iframe>
  </div>

  <script>
    window.onerror = function(msg, src, line) {
      document.getElementById('grid-container').innerHTML =
        '<div class="empty-state">JS Error: ' + msg + ' (line ' + line + ')</div>';
    };
    var authToken = ${JSON.stringify(token)};
    var tokenQuery = authToken ? '?token=' + encodeURIComponent(authToken) : '';
    var expiresAt = ${expiresAt};
    var filterSessions = ${filterSessions};

    var gridContainer = document.getElementById('grid-container');
    var workerCountEl = document.getElementById('worker-count');
    var countdownEl = document.getElementById('countdown');
    var focusOverlay = document.getElementById('focus-overlay');
    var focusIframe = document.getElementById('focus-iframe');
    var focusWorkerName = document.getElementById('focus-worker-name');

    var sessions = [];

    function updateCountdown() {
      if (!expiresAt) return;
      var remaining = Math.max(0, Math.floor((expiresAt - Date.now()) / 1000));
      var min = Math.floor(remaining / 60);
      var sec = remaining % 60;
      countdownEl.textContent = min + ':' + (sec < 10 ? '0' : '') + sec;
      if (remaining <= 0) {
        countdownEl.textContent = 'expired';
        countdownEl.className = 'countdown expired';
      } else if (remaining <= 60) {
        countdownEl.className = 'countdown urgent';
      }
    }
    if (expiresAt) {
      updateCountdown();
      setInterval(updateCountdown, 1000);
    }

    function extractWorkerName(sessionName) {
      return sessionName.replace(/^claude-[^-]+-/, '');
    }

    function escapeHtml(str) {
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }

    function sessionUrl(sessionName, opts) {
      var url = '/session/' + encodeURIComponent(sessionName) + tokenQuery;
      var sep = tokenQuery ? '&' : '?';
      if (opts && opts.readonly) { url += sep + 'readonly=1'; sep = '&'; }
      if (opts && opts.embed) { url += sep + 'embed=1'; sep = '&'; }
      return url;
    }

    function buildGrid() {
      if (!sessions.length) {
        gridContainer.innerHTML = '<div class="empty-state">' +
          'No sessions enabled.<br>Use /pilot name1 name2 in Telegram.</div>';
        workerCountEl.textContent = '';
        return;
      }

      if (sessions.length === 1) {
        workerCountEl.textContent = extractWorkerName(sessions[0].name);
        gridContainer.className = 'solo';
        gridContainer.innerHTML =
          '<iframe src="' + escapeHtml(sessionUrl(sessions[0].name, { embed: true })) + '"></iframe>';
        return;
      }

      gridContainer.className = 'grid';
      workerCountEl.textContent = sessions.length + ' workers';

      gridContainer.innerHTML = sessions.map(function(session) {
        var name = extractWorkerName(session.name);
        var src = sessionUrl(session.name, { readonly: true, embed: true });
        return '<div class="panel" data-session="' + escapeHtml(session.name) + '">' +
          '<div class="panel-header">' +
          '<span class="status-dot"></span>' +
          '<span class="worker-name">' + escapeHtml(name) + '</span>' +
          '</div>' +
          '<div class="panel-terminal">' +
          '<iframe src="' + escapeHtml(src) + '" loading="lazy"></iframe>' +
          '</div>' +
          '</div>';
      }).join('');
    }

    function openFocusView(sessionName) {
      focusWorkerName.textContent = extractWorkerName(sessionName);
      focusIframe.src = sessionUrl(sessionName, {});
      focusOverlay.classList.add('active');
    }

    function closeFocusView() {
      focusOverlay.classList.remove('active');
      focusIframe.src = 'about:blank';
    }

    gridContainer.addEventListener('click', function(event) {
      if (sessions.length <= 1) return;
      var panel = event.target.closest('.panel');
      if (panel) openFocusView(panel.getAttribute('data-session'));
    });

    document.getElementById('back-to-grid').addEventListener('click', closeFocusView);

    document.addEventListener('keydown', function(event) {
      if (event.key === 'Escape' && focusOverlay.classList.contains('active')) {
        closeFocusView();
      }
    });

    document.getElementById('refresh-button').addEventListener('click', function() {
      loadEnabledSessions();
    });

    function cacheBust(url) {
      return url + (url.indexOf('?') === -1 ? '?' : '&') + '_t=' + Date.now();
    }

    async function loadEnabledSessions() {
      try {
        var response = await fetch(cacheBust('/api/enabled' + tokenQuery));
        if (!response.ok) return;
        var newSessions = await response.json();
        if (filterSessions) {
          newSessions = newSessions.filter(function(s) {
            return filterSessions.indexOf(s.name) !== -1;
          });
        }
        var oldKeys = sessions.map(function(s) { return s.name; }).sort().join(',');
        var newKeys = newSessions.map(function(s) { return s.name; }).sort().join(',');
        sessions = newSessions;
        if (oldKeys !== newKeys) buildGrid();
      } catch (err) { /* retry next interval */ }
    }

    (async function init() {
      try {
        await loadEnabledSessions();
        buildGrid();
        setInterval(loadEnabledSessions, 15000);
      } catch (err) {
        gridContainer.innerHTML = '<div class="empty-state">Error: ' + err.message + '</div>';
      }
    })();
  </script>
</body>
</html>`;
}

async function handleHttp(req, res) {
  const urlObj = new URL(req.url || '/', `http://${req.headers.host || 'localhost'}`);
  const pathname = urlObj.pathname;

  if (!isAuthorized(urlObj)) {
    sendText(res, 401, 'Unauthorized');
    return;
  }

  if (pathname === '/debug') {
    sendHtml(res, 200, `<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pilot Debug</title>
<style>body{background:#111;color:#eee;font:14px monospace;padding:20px}#log{white-space:pre-wrap}.ok{color:#4ade80}.err{color:#ef4444}.warn{color:#f59e0b}</style>
</head><body><h2>Pilot Debug</h2><div id="log"></div>
<script type="module">
const log = document.getElementById('log');
function L(cls, msg) { log.innerHTML += '<div class="'+cls+'">[' + new Date().toISOString().substr(11,8) + '] ' + msg + '</div>'; }

L('ok', '1. Page loaded');
L('ok', '2. UserAgent: ' + navigator.userAgent);

// Test fetch
try {
  const r = await fetch('/api/sessions');
  const d = await r.json();
  L('ok', '3. /api/sessions: ' + d.length + ' sessions');
} catch(e) { L('err', '3. /api/sessions FAILED: ' + e.message); }

// Test WASM fetch
try {
  const r = await fetch('/ghostty-vt.wasm');
  L(r.ok ? 'ok' : 'err', '4. WASM fetch: HTTP ' + r.status + ' (' + r.headers.get('content-type') + ', ' + r.headers.get('content-length') + ' bytes)');
} catch(e) { L('err', '4. WASM fetch FAILED: ' + e.message); }

// Test ghostty-web JS module load
try {
  L('warn', '5. Loading ghostty-web module...');
  const mod = await import('/dist/ghostty-web.js');
  L('ok', '5. Module loaded, exports: ' + Object.keys(mod).join(', '));
  L('warn', '6. Calling init()...');
  await mod.init();
  L('ok', '6. init() OK');
} catch(e) { L('err', '5/6. ghostty-web FAILED: ' + e.message + '\\n' + e.stack); }

// Test WebSocket
try {
  L('warn', '7. Connecting WebSocket...');
  const ws = new WebSocket('ws://' + location.host + '/ws?session=claude-prod-lee&cols=80&rows=24');
  ws.onopen = () => { L('ok', '7. WebSocket OPEN'); ws.close(); };
  ws.onerror = (e) => L('err', '7. WebSocket ERROR');
  ws.onclose = (e) => L(e.code === 1000 ? 'ok' : 'warn', '7. WebSocket CLOSE code=' + e.code + ' reason=' + e.reason);
  setTimeout(() => { if (ws.readyState < 2) { L('err', '7. WebSocket TIMEOUT (5s)'); ws.close(); } }, 5000);
} catch(e) { L('err', '7. WebSocket FAILED: ' + e.message); }
</script></body></html>`);
    return;
  }

  if (pathname === '/' || pathname === '/index.html') {
    const sessions = filterEnabledSessions(await listSessions());
    sendHtml(
      res,
      200,
      renderIndexPage({
        sessions,
        token: getTokenForTemplates(urlObj),
        enabledCount: ENABLED_SESSIONS.size,
      })
    );
    return;
  }

  if (pathname === '/grid' || pathname.startsWith('/grid/')) {
    const slug = pathname === '/grid' ? null : decodeURIComponent(pathname.slice('/grid/'.length));
    const gs = slug ? getGridSession(slug) : null;
    if (slug && !gs) {
      sendHtml(res, 404, `<!doctype html><html><body style="background:#0a0a08;color:#40d870;font-family:monospace;padding:40px;text-align:center"><h2>Session expired</h2><p>This pilot session has ended. Run /pilot again from Telegram.</p></body></html>`);
      return;
    }
    sendHtml(
      res,
      200,
      renderGridPage({
        token: getTokenForTemplates(urlObj),
        slug: slug || null,
        gridSession: gs,
      })
    );
    return;
  }

  if (req.method === 'POST' && pathname === '/api/grid-session') {
    try {
      const body = await new Promise((resolve, reject) => {
        let data = '';
        req.on('data', (c) => { data += c; if (data.length > 1e5) reject(new Error('too large')); });
        req.on('end', () => resolve(data));
        req.on('error', reject);
      });
      const { slug, sessions: sessionNames, ttl } = JSON.parse(body);
      if (!slug || !Array.isArray(sessionNames) || !sessionNames.length) {
        sendJson(res, 400, { error: 'slug and sessions[] required' });
        return;
      }
      const ttlMs = Math.min((ttl || 300) * 1000, 30 * 60 * 1000);
      const result = createGridSession(slug, sessionNames, ttlMs);
      sendJson(res, 200, { ok: true, slug: result.slug, expiresAt: result.expiresAt });
    } catch (e) {
      sendJson(res, 400, { error: e.message });
    }
    return;
  }

  if (pathname === '/api/sessions') {
    const sessions = filterEnabledSessions(await listSessions());
    sendJson(res, 200, sessions);
    return;
  }

  if (pathname === '/api/enabled') {
    const entries = [...ENABLED_SESSIONS.keys()].sort().map((name) => {
      const entry = ENABLED_SESSIONS.get(name);
      return { name, host: entry ? entry.host : null };
    });
    sendJson(res, 200, entries);
    return;
  }

  if (pathname === '/api/pilot') {
    if (req.method !== 'POST' && req.method !== 'DELETE') {
      res.writeHead(405, { Allow: 'POST, DELETE' });
      res.end('Method Not Allowed');
      return;
    }

    const sessionName = urlObj.searchParams.get('session') || '';
    const host = urlObj.searchParams.get('host') || null;
    if (!isAllowedSessionName(sessionName)) {
      sendText(res, 400, 'Invalid session name');
      return;
    }

    // For remote sessions, check via SSH before enabling
    if (host) {
      try {
        await execTmux(['has-session', '-t', sessionName], host);
      } catch {
        sendText(res, 404, 'Session not found');
        return;
      }
    } else if (!(await sessionExists(sessionName))) {
      sendText(res, 404, 'Session not found');
      return;
    }

    if (req.method === 'DELETE') {
      disableSession(sessionName);
      sendJson(res, 200, { ok: true, enabled: false });
    } else {
      enableSession(sessionName, host);
      sendJson(res, 200, { ok: true, enabled: true });
    }
    return;
  }

  if (pathname === '/api/capture') {
    const sessionName = urlObj.searchParams.get('session') || '';
    if (
      !isAllowedSessionName(sessionName) ||
      !isEnabledSessionName(sessionName) ||
      !(await sessionExists(sessionName))
    ) {
      sendText(res, 404, 'Session not found');
      return;
    }
    try {
      refreshSessionTimer(sessionName);
      const host = getSessionHost(sessionName);
      const { stdout } = await execTmux(['capture-pane', '-t', sessionName, '-p'], host);
      sendJson(res, 200, { text: stdout });
    } catch {
      sendText(res, 500, 'Capture failed');
    }
    return;
  }

  if (pathname.startsWith('/session/')) {
    let sessionName = '';
    try {
      sessionName = decodeURIComponent(pathname.slice('/session/'.length));
    } catch {
      sendText(res, 404, 'Session not found');
      return;
    }
    if (!isAllowedSessionName(sessionName)) {
      sendText(res, 404, 'Session not found');
      return;
    }
    if (!isEnabledSessionName(sessionName)) {
      sendText(res, 404, 'Session not found');
      return;
    }
    if (!(await sessionExists(sessionName))) {
      sendText(res, 404, 'Session not found');
      return;
    }
    refreshSessionTimer(sessionName);
    sendHtml(
      res,
      200,
      renderSessionPage({
        sessionName,
        token: getTokenForTemplates(urlObj),
        readonly: urlObj.searchParams.get('readonly') === '1',
        embed: urlObj.searchParams.get('embed') === '1',
      })
    );
    return;
  }

  if (pathname.startsWith('/dist/')) {
    const relativePath = pathname.slice('/dist/'.length);
    const requestedPath = path.resolve(distPath, relativePath);
    if (requestedPath !== distPath && !requestedPath.startsWith(`${distPath}${path.sep}`)) {
      sendText(res, 404, 'Not Found');
      return;
    }
    await serveFile(requestedPath, res);
    return;
  }

  if (pathname === '/ghostty-vt.wasm') {
    await serveFile(wasmPath, res);
    return;
  }

  sendText(res, 404, 'Not Found');
}

const httpServer = http.createServer((req, res) => {
  handleHttp(req, res).catch((error) => {
    console.error('HTTP error:', error);
    sendText(res, 500, 'Internal Server Error');
  });
});

function sendUpgradeError(socket, statusCode, reason) {
  if (socket.destroyed) {
    return;
  }
  socket.write(
    `HTTP/1.1 ${statusCode} ${reason}\r\n` +
      'Connection: close\r\n' +
      'Content-Type: text/plain; charset=utf-8\r\n' +
      '\r\n' +
      `${reason}`
  );
  socket.destroy();
}

const wss = new WebSocketServer({ noServer: true });

async function handleUpgrade(req, socket, head) {
  const urlObj = new URL(req.url || '/', `http://${req.headers.host || 'localhost'}`);
  if (urlObj.pathname !== '/ws') {
    sendUpgradeError(socket, 404, 'Not Found');
    return;
  }

  if (!isAuthorized(urlObj)) {
    sendUpgradeError(socket, 401, 'Unauthorized');
    return;
  }

  const sessionName = urlObj.searchParams.get('session') || '';
  if (!isAllowedSessionName(sessionName)) {
    sendUpgradeError(socket, 404, 'Session not found');
    return;
  }
  if (!isEnabledSessionName(sessionName)) {
    sendUpgradeError(socket, 404, 'Session not found');
    return;
  }

  if (!(await sessionExists(sessionName))) {
    sendUpgradeError(socket, 404, 'Session not found');
    return;
  }

  wss.handleUpgrade(req, socket, head, (ws) => {
    wss.emit('connection', ws, req);
  });
}

httpServer.on('upgrade', (req, socket, head) => {
  handleUpgrade(req, socket, head).catch((error) => {
    console.error('Upgrade error:', error);
    sendUpgradeError(socket, 500, 'Internal Server Error');
  });
});

function closeConnection(ws) {
  const ptyProcess = ACTIVE_CONNECTIONS.get(ws);
  if (!ptyProcess) {
    return;
  }
  ACTIVE_CONNECTIONS.delete(ws);
  try {
    ptyProcess.kill();
  } catch {
    // ignore
  }
}

wss.on('connection', async (ws, req) => {
  const urlObj = new URL(req.url || '/', `http://${req.headers.host || 'localhost'}`);
  const sessionName = urlObj.searchParams.get('session') || '';
  const readonly = urlObj.searchParams.get('readonly') === '1';
  const cols = parseIntOrDefault(urlObj.searchParams.get('cols'), 80);
  const rows = parseIntOrDefault(urlObj.searchParams.get('rows'), 24);

  console.log(`[ws] connect session=${sessionName} readonly=${readonly} cols=${cols} rows=${rows}`);

  if (!isEnabledSessionName(sessionName)) {
    console.log(`[ws] session not enabled: ${sessionName}`);
    ws.send(`\r\n\x1b[31mSession expired — run /pilot ${sessionName.replace(/^claude-prod-/, '')} again from Telegram\x1b[0m\r\n`);
    ws.close(4000, 'session not enabled');
    return;
  }

  refreshSessionTimer(sessionName);

  const host = getSessionHost(sessionName);

  // Pre-flight: diagnose common failures before spawning PTY
  if (host) {
    try {
      const diag = await diagnoseRemote(host, sessionName);
      if (diag) {
        console.log(`[ws] pre-flight failed session=${sessionName}: ${diag}`);
        ws.send(`\r\n\x1b[31m${diag}\x1b[0m\r\n`);
        ws.close(4000, diag);
        return;
      }
    } catch (err) {
      console.log(`[ws] pre-flight error session=${sessionName}: ${err.message}`);
    }
  }

  const ptyCmd = host
    ? { file: 'ssh', args: ['-t', '-o', 'ConnectTimeout=3', host, 'tmux', 'attach-session', '-t', sessionName] }
    : { file: 'tmux', args: ['attach-session', '-t', sessionName] };
  const ptyProcess = pty.spawn(ptyCmd.file, ptyCmd.args, {
    name: 'xterm-256color',
    cols,
    rows,
    env: {
      ...process.env,
      TERM: 'xterm-256color',
      COLORTERM: 'truecolor',
    },
  });

  // Force tmux to resize the window to match this client (avoids dot-fill)
  setTimeout(() => {
    execTmux(['resize-window', '-t', sessionName, '-x', String(cols), '-y', String(rows)], host)
      .catch(() => {});
  }, 300);

  ACTIVE_CONNECTIONS.set(ws, ptyProcess);

  ptyProcess.onData((data) => {
    if (ws.readyState === ws.OPEN) {
      ws.send(data);
    }
  });

  ptyProcess.onExit(({ exitCode }) => {
    console.log(`[ws] pty exit session=${sessionName} code=${exitCode}`);
    ACTIVE_CONNECTIONS.delete(ws);
    if (ws.readyState === ws.OPEN) {
      if (exitCode !== 0 && host) {
        diagnoseRemote(host, sessionName).then((reason) => {
          const msg = reason || `Session exited (code ${exitCode})`;
          ws.send(`\r\n\x1b[31m${msg}\x1b[0m\r\n`);
          ws.close(4000, msg);
        }).catch(() => {
          ws.send(`\r\n\x1b[33mSession exited (code ${exitCode})\x1b[0m\r\n`);
          ws.close();
        });
      } else {
        ws.send(`\r\n\x1b[33mSession exited (code ${exitCode})\x1b[0m\r\n`);
        ws.close();
      }
    }
  });

  ws.on('message', (raw) => {
    refreshSessionTimer(sessionName);
    const message = raw.toString('utf8');
    if (message.startsWith('{')) {
      try {
        const payload = JSON.parse(message);
        if (payload.type === 'resize') {
          const nextCols = parseIntOrDefault(String(payload.cols || ''), cols);
          const nextRows = parseIntOrDefault(String(payload.rows || ''), rows);
          ptyProcess.resize(nextCols, nextRows);
          execTmux(['resize-window', '-t', sessionName, '-x', String(nextCols), '-y', String(nextRows)], host)
            .catch(() => {});
          return;
        }
      } catch {
        // treat as tty input
      }
    }

    if (!readonly) {
      ptyProcess.write(message);
    }
  });

  ws.on('close', (code) => { console.log(`[ws] close session=${sessionName} code=${code}`); closeConnection(ws); });
  ws.on('error', (err) => { console.log(`[ws] error session=${sessionName}: ${err.message}`); closeConnection(ws); });
});

let shuttingDown = false;

function shutdown() {
  if (shuttingDown) {
    return;
  }
  shuttingDown = true;

  for (const [ws, ptyProcess] of ACTIVE_CONNECTIONS.entries()) {
    ACTIVE_CONNECTIONS.delete(ws);
    try {
      ptyProcess.kill();
    } catch {
      // ignore
    }
    try {
      ws.close();
    } catch {
      // ignore
    }
  }

  wss.close(() => {
    httpServer.close(() => {
      process.exit(0);
    });
  });

  setTimeout(() => process.exit(0), 1000).unref();
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);

httpServer.listen(PORT, () => {
  console.log(`pilot listening on http://localhost:${PORT}`);
});
