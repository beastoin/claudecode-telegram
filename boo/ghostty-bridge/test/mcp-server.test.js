import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { MockGhosttySocket } from './fixtures/mock-ghostty-socket.js';
import { createBridge } from '../src/index.js';

const SOCK_A = `/tmp/test-mcp-a-${process.pid}.sock`;
const SOCK_B = `/tmp/test-mcp-b-${process.pid}.sock`;

describe('MCP Server (integration)', () => {
  let mockA, mockB;
  let bridge;
  let termA, termB;

  beforeEach(async () => {
    mockA = new MockGhosttySocket(SOCK_A);
    mockB = new MockGhosttySocket(SOCK_B);
    termA = mockA.addTerminal({ title: 'claude — ~/rnd', working_directory: '/home/user/rnd' });
    termB = mockB.addTerminal({ title: 'codex — ~/omi', working_directory: '/Users/user/omi' });
    await mockA.start();
    await mockB.start();

    bridge = createBridge({
      machines: [
        { name: 'macbook', socket: SOCK_A, local: true },
        { name: 'mac-mini', socket: SOCK_B, local: true },
      ],
    });
    await bridge.init();
  });

  afterEach(async () => {
    await mockA.stop();
    await mockB.stop();
  });

  it('lists available tools', () => {
    const tools = bridge.listTools();
    const names = tools.map(t => t.name);
    expect(names).toContain('register_peer');
    expect(names).toContain('list_peers');
    expect(names).toContain('send_message');
    expect(names).toContain('broadcast');
    expect(names).toContain('receive_messages');
    expect(names).toContain('get_peer_status');
    expect(tools).toHaveLength(6);
  });

  it('register_peer + list_peers round trip', async () => {
    const regResult = await bridge.callTool('register_peer', {
      name: 'lee', role: 'engineer',
    });
    expect(regResult.peer_id).toBeDefined();

    const listResult = await bridge.callTool('list_peers', {});
    expect(listResult).toHaveLength(1);
    expect(listResult[0].name).toBe('lee');
    expect(listResult[0].role).toBe('engineer');
  });

  it('send_message delivers to correct terminal', async () => {
    await bridge.callTool('register_peer', {
      name: 'lee', role: 'engineer', machine: 'macbook', terminal_id: termA.id,
    });
    await bridge.callTool('register_peer', {
      name: 'geni', role: 'researcher', machine: 'mac-mini', terminal_id: termB.id,
    });

    const result = await bridge.callTool('send_message', {
      to: 'geni', content: 'hello from lee',
    });
    expect(result.ok).toBe(true);

    expect(mockB.sendTextCalls).toHaveLength(1);
    expect(mockB.sendTextCalls[0].text).toContain('hello from lee');
    expect(mockB.sendTextCalls[0].text).toContain('lee');
  });

  it('broadcast sends to all except self', async () => {
    await bridge.callTool('register_peer', {
      name: 'lee', machine: 'macbook', terminal_id: termA.id,
    });
    await bridge.callTool('register_peer', {
      name: 'geni', machine: 'mac-mini', terminal_id: termB.id,
    });

    const result = await bridge.callTool('broadcast', {
      from: 'lee', content: 'announcement',
    });
    expect(result.sent).toBe(1);

    expect(mockB.sendTextCalls).toHaveLength(1);
    expect(mockA.sendTextCalls).toHaveLength(0); // lee doesn't get own broadcast
  });

  it('receive_messages returns queued messages', async () => {
    const reg = await bridge.callTool('register_peer', {
      name: 'geni', machine: 'mac-mini', terminal_id: termB.id,
    });
    await bridge.callTool('register_peer', {
      name: 'lee', machine: 'macbook', terminal_id: termA.id,
    });

    await bridge.callTool('send_message', {
      to: 'geni', content: 'check this',
    });

    const messages = await bridge.callTool('receive_messages', {
      peer_id: reg.peer_id,
    });
    expect(messages).toHaveLength(1);
    expect(messages[0].content).toBe('check this');

    // Second call returns empty
    const empty = await bridge.callTool('receive_messages', {
      peer_id: reg.peer_id,
    });
    expect(empty).toHaveLength(0);
  });

  it('get_peer_status returns active peer info', async () => {
    const reg = await bridge.callTool('register_peer', {
      name: 'geni', role: 'researcher', machine: 'mac-mini', terminal_id: termB.id,
    });

    const status = await bridge.callTool('get_peer_status', {
      peer_id: reg.peer_id,
    });
    expect(status.name).toBe('geni');
    expect(status.status).toBe('active');
    expect(status.machine).toBe('mac-mini');
  });

  it('get_peer_status returns null for unknown peer', async () => {
    const status = await bridge.callTool('get_peer_status', {
      peer_id: 'nonexistent',
    });
    expect(status).toBeNull();
  });

  it('send_message to unknown peer returns error', async () => {
    await bridge.callTool('register_peer', {
      name: 'lee', machine: 'macbook', terminal_id: termA.id,
    });

    await expect(bridge.callTool('send_message', {
      to: 'nobody', content: 'hi',
    })).rejects.toThrow(/unknown peer/i);
  });

  it('register_peer auto-discovers terminal from machine', async () => {
    // Register without explicit terminal_id — bridge should find it
    const reg = await bridge.callTool('register_peer', {
      name: 'lee', machine: 'macbook',
    });
    expect(reg.peer_id).toBeDefined();
    expect(reg.terminal_id).toBe(termA.id);
  });
});
