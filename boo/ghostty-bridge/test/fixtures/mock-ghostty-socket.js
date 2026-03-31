import net from 'node:net';
import fs from 'node:fs';
import { randomUUID } from 'node:crypto';

/**
 * Mock Ghostty Unix socket server.
 * Simulates the JSON-RPC 2.0 API that ghostty-socket exposes.
 *
 * Usage:
 *   const mock = new MockGhosttySocket('/tmp/test-ghostty.sock');
 *   mock.addTerminal({ title: 'claude — ~/project', pid: 1234 });
 *   await mock.start();
 *   // ... run tests ...
 *   await mock.stop();
 */
export class MockGhosttySocket {
  constructor(socketPath) {
    this.socketPath = socketPath;
    this.terminals = [];
    this.sendTextCalls = [];
    this.sendKeyCalls = [];
    this.server = null;
  }

  addTerminal(opts = {}) {
    const terminal = {
      id: opts.id || randomUUID(),
      title: opts.title || 'bash',
      working_directory: opts.working_directory || '/tmp',
      focused: opts.focused ?? false,
      pid: opts.pid || Math.floor(Math.random() * 90000) + 10000,
      columns: opts.columns || 120,
      rows: opts.rows || 40,
      foreground_process: opts.foreground_process || '/bin/bash',
      screen_lines: opts.screen_lines || [],
    };
    this.terminals.push(terminal);
    return terminal;
  }

  removeTerminal(id) {
    this.terminals = this.terminals.filter(t => t.id !== id);
  }

  _handleRequest(method, params) {
    switch (method) {
      case 'list_terminals':
        return {
          terminals: this.terminals.map(t => ({
            id: t.id,
            title: t.title,
            working_directory: t.working_directory,
            focused: t.focused,
            pid: t.pid,
            columns: t.columns,
            rows: t.rows,
          })),
        };

      case 'get_terminal_info': {
        const terminal = this.terminals.find(t => t.id === params?.terminal_id);
        if (!terminal) {
          return { __error: { code: -32001, message: 'Terminal not found' } };
        }
        return {
          id: terminal.id,
          title: terminal.title,
          working_directory: terminal.working_directory,
          focused: terminal.focused,
          pid: terminal.pid,
          columns: terminal.columns,
          rows: terminal.rows,
          foreground_process: terminal.foreground_process,
        };
      }

      case 'send_text': {
        const terminal = this.terminals.find(t => t.id === params?.terminal_id);
        if (!terminal) {
          return { __error: { code: -32001, message: 'Terminal not found' } };
        }
        this.sendTextCalls.push({
          terminal_id: params.terminal_id,
          text: params.text,
          timestamp: Date.now(),
        });
        return { ok: true };
      }

      case 'send_key': {
        const terminal = this.terminals.find(t => t.id === params?.terminal_id);
        if (!terminal) {
          return { __error: { code: -32001, message: 'Terminal not found' } };
        }
        this.sendKeyCalls.push({
          terminal_id: params.terminal_id,
          key: params.key,
          modifiers: params.modifiers || [],
          timestamp: Date.now(),
        });
        return { ok: true };
      }

      case 'read_screen': {
        const terminal = this.terminals.find(t => t.id === params?.terminal_id);
        if (!terminal) {
          return { __error: { code: -32001, message: 'Terminal not found' } };
        }
        const lines = terminal.screen_lines || [];
        const start = params?.rows?.start ?? 0;
        const end = params?.rows?.end ?? lines.length;
        return { lines: lines.slice(start, end) };
      }

      default:
        return { __error: { code: -32601, message: 'Method not found' } };
    }
  }

  _handleConnection(socket) {
    let data = '';
    socket.on('data', chunk => {
      data += chunk.toString();

      // Simple HTTP/1.1 parsing: look for double CRLF then read body
      const headerEnd = data.indexOf('\r\n\r\n');
      if (headerEnd === -1) return;

      const headers = data.slice(0, headerEnd);
      const contentLengthMatch = headers.match(/content-length:\s*(\d+)/i);
      const contentLength = contentLengthMatch ? parseInt(contentLengthMatch[1], 10) : 0;
      const body = data.slice(headerEnd + 4);

      if (body.length < contentLength) return; // wait for more data

      const jsonBody = body.slice(0, contentLength);

      let request;
      try {
        request = JSON.parse(jsonBody);
      } catch {
        const errResp = JSON.stringify({
          jsonrpc: '2.0',
          id: null,
          error: { code: -32700, message: 'Parse error' },
        });
        socket.end(`HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ${Buffer.byteLength(errResp)}\r\nConnection: close\r\n\r\n${errResp}`);
        return;
      }

      const result = this._handleRequest(request.method, request.params);

      let response;
      if (result?.__error) {
        response = {
          jsonrpc: '2.0',
          id: request.id ?? null,
          error: result.__error,
        };
      } else {
        response = {
          jsonrpc: '2.0',
          id: request.id ?? null,
          result,
        };
      }

      const respBody = JSON.stringify(response);
      socket.end(`HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ${Buffer.byteLength(respBody)}\r\nConnection: close\r\n\r\n${respBody}`);
    });
  }

  async start() {
    // Clean up stale socket file
    try { fs.unlinkSync(this.socketPath); } catch {}

    return new Promise((resolve, reject) => {
      this.server = net.createServer(socket => this._handleConnection(socket));
      this.server.on('error', reject);
      this.server.listen(this.socketPath, () => resolve());
    });
  }

  async stop() {
    if (!this.server) return;
    return new Promise(resolve => {
      this.server.close(() => {
        try { fs.unlinkSync(this.socketPath); } catch {}
        this.server = null;
        resolve();
      });
    });
  }
}
