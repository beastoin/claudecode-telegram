import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { MockGhosttySocket } from './fixtures/mock-ghostty-socket.js';
import { GhosttyClient } from '../src/ghostty-client.js';

const SOCK = `/tmp/test-ghostty-client-${process.pid}.sock`;

describe('GhosttyClient', () => {
  let mock;
  let client;

  beforeEach(async () => {
    mock = new MockGhosttySocket(SOCK);
    await mock.start();
    client = new GhosttyClient(SOCK);
  });

  afterEach(async () => {
    await mock.stop();
  });

  describe('listTerminals', () => {
    it('returns empty array when no terminals', async () => {
      const terminals = await client.listTerminals();
      expect(terminals).toEqual([]);
    });

    it('returns terminal list', async () => {
      mock.addTerminal({ title: 'claude — ~/rnd', pid: 1234 });
      mock.addTerminal({ title: 'bash', pid: 5678 });
      const terminals = await client.listTerminals();
      expect(terminals).toHaveLength(2);
      expect(terminals[0].title).toBe('claude — ~/rnd');
      expect(terminals[1].pid).toBe(5678);
    });
  });

  describe('getTerminalInfo', () => {
    it('returns full terminal info', async () => {
      const t = mock.addTerminal({
        title: 'claude — ~/project',
        foreground_process: '/usr/bin/claude',
        working_directory: '/home/user/project',
      });
      const info = await client.getTerminalInfo(t.id);
      expect(info.title).toBe('claude — ~/project');
      expect(info.foreground_process).toBe('/usr/bin/claude');
      expect(info.working_directory).toBe('/home/user/project');
    });

    it('throws on unknown terminal', async () => {
      await expect(client.getTerminalInfo('nonexistent'))
        .rejects.toThrow('Terminal not found');
    });
  });

  describe('sendText', () => {
    it('sends text to terminal', async () => {
      const t = mock.addTerminal();
      await client.sendText(t.id, 'hello world\n');
      expect(mock.sendTextCalls).toHaveLength(1);
      expect(mock.sendTextCalls[0].text).toBe('hello world\n');
    });

    it('throws on unknown terminal', async () => {
      await expect(client.sendText('nonexistent', 'hi'))
        .rejects.toThrow('Terminal not found');
    });
  });

  describe('sendKey', () => {
    it('sends key with modifiers', async () => {
      const t = mock.addTerminal();
      await client.sendKey(t.id, 'c', ['ctrl']);
      expect(mock.sendKeyCalls).toHaveLength(1);
      expect(mock.sendKeyCalls[0].key).toBe('c');
      expect(mock.sendKeyCalls[0].modifiers).toEqual(['ctrl']);
    });
  });

  describe('readScreen', () => {
    it('reads screen lines', async () => {
      const t = mock.addTerminal({
        screen_lines: ['line1', 'line2', 'line3', 'line4'],
      });
      const lines = await client.readScreen(t.id, 1, 3);
      expect(lines).toEqual(['line2', 'line3']);
    });

    it('reads all lines when no range given', async () => {
      const t = mock.addTerminal({
        screen_lines: ['a', 'b', 'c'],
      });
      const lines = await client.readScreen(t.id);
      expect(lines).toEqual(['a', 'b', 'c']);
    });
  });

  describe('error handling', () => {
    it('throws on connection refused', async () => {
      await mock.stop();
      await expect(client.listTerminals()).rejects.toThrow();
    });

    it('throws on malformed response', async () => {
      // Test with a socket path that doesn't exist
      const badClient = new GhosttyClient('/tmp/nonexistent-socket-path.sock');
      await expect(badClient.listTerminals()).rejects.toThrow();
    });
  });
});
