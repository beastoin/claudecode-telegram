import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import net from 'node:net';
import { MockGhosttySocket } from './fixtures/mock-ghostty-socket.js';

const SOCK = `/tmp/test-mock-ghostty-${process.pid}.sock`;

function sendRequest(socketPath, request) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify(request);
    const httpReq = [
      'POST / HTTP/1.1',
      'Content-Type: application/json',
      `Content-Length: ${Buffer.byteLength(body)}`,
      'Connection: close',
      '',
      body,
    ].join('\r\n');

    const socket = net.createConnection(socketPath, () => {
      socket.write(httpReq);
    });

    let data = '';
    socket.on('data', chunk => { data += chunk.toString(); });
    socket.on('end', () => {
      const bodyStart = data.indexOf('\r\n\r\n');
      if (bodyStart === -1) return reject(new Error('No HTTP body'));
      resolve(JSON.parse(data.slice(bodyStart + 4)));
    });
    socket.on('error', reject);
  });
}

describe('MockGhosttySocket', () => {
  let mock;

  beforeEach(async () => {
    mock = new MockGhosttySocket(SOCK);
  });

  afterEach(async () => {
    await mock.stop();
  });

  it('starts and stops cleanly', async () => {
    await mock.start();
    await mock.stop();
  });

  it('list_terminals returns empty list', async () => {
    await mock.start();
    const resp = await sendRequest(SOCK, {
      jsonrpc: '2.0', method: 'list_terminals', id: 1,
    });
    expect(resp.result.terminals).toEqual([]);
  });

  it('list_terminals returns added terminals', async () => {
    const t = mock.addTerminal({ title: 'claude — ~/project', pid: 1234 });
    await mock.start();
    const resp = await sendRequest(SOCK, {
      jsonrpc: '2.0', method: 'list_terminals', id: 1,
    });
    expect(resp.result.terminals).toHaveLength(1);
    expect(resp.result.terminals[0].title).toBe('claude — ~/project');
    expect(resp.result.terminals[0].id).toBe(t.id);
  });

  it('send_text records calls', async () => {
    const t = mock.addTerminal();
    await mock.start();
    const resp = await sendRequest(SOCK, {
      jsonrpc: '2.0', method: 'send_text',
      params: { terminal_id: t.id, text: 'hello\n' }, id: 2,
    });
    expect(resp.result.ok).toBe(true);
    expect(mock.sendTextCalls).toHaveLength(1);
    expect(mock.sendTextCalls[0].text).toBe('hello\n');
  });

  it('send_text returns error for unknown terminal', async () => {
    await mock.start();
    const resp = await sendRequest(SOCK, {
      jsonrpc: '2.0', method: 'send_text',
      params: { terminal_id: 'nonexistent', text: 'hi' }, id: 3,
    });
    expect(resp.error.code).toBe(-32001);
  });

  it('get_terminal_info returns foreground_process', async () => {
    const t = mock.addTerminal({ foreground_process: '/usr/bin/claude' });
    await mock.start();
    const resp = await sendRequest(SOCK, {
      jsonrpc: '2.0', method: 'get_terminal_info',
      params: { terminal_id: t.id }, id: 4,
    });
    expect(resp.result.foreground_process).toBe('/usr/bin/claude');
  });

  it('read_screen returns configured lines', async () => {
    const t = mock.addTerminal({
      screen_lines: ['$ claude', 'Claude Code v1.2.3', '> Ready'],
    });
    await mock.start();
    const resp = await sendRequest(SOCK, {
      jsonrpc: '2.0', method: 'read_screen',
      params: { terminal_id: t.id, rows: { start: 0, end: 2 } }, id: 5,
    });
    expect(resp.result.lines).toEqual(['$ claude', 'Claude Code v1.2.3']);
  });

  it('send_key records calls with modifiers', async () => {
    const t = mock.addTerminal();
    await mock.start();
    const resp = await sendRequest(SOCK, {
      jsonrpc: '2.0', method: 'send_key',
      params: { terminal_id: t.id, key: 'c', modifiers: ['ctrl'] }, id: 6,
    });
    expect(resp.result.ok).toBe(true);
    expect(mock.sendKeyCalls[0].key).toBe('c');
    expect(mock.sendKeyCalls[0].modifiers).toEqual(['ctrl']);
  });

  it('unknown method returns -32601', async () => {
    await mock.start();
    const resp = await sendRequest(SOCK, {
      jsonrpc: '2.0', method: 'bogus_method', id: 7,
    });
    expect(resp.error.code).toBe(-32601);
  });
});
