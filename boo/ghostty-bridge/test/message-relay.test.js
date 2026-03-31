import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { MockGhosttySocket } from './fixtures/mock-ghostty-socket.js';
import { MachineManager } from '../src/machine-manager.js';
import { PeerRegistry } from '../src/peer-registry.js';
import { MessageRelay } from '../src/message-relay.js';

const SOCK_A = `/tmp/test-relay-a-${process.pid}.sock`;
const SOCK_B = `/tmp/test-relay-b-${process.pid}.sock`;

describe('MessageRelay', () => {
  let mockA, mockB;
  let machines, peers, relay;
  let termA, termB;

  beforeEach(async () => {
    mockA = new MockGhosttySocket(SOCK_A);
    mockB = new MockGhosttySocket(SOCK_B);
    termA = mockA.addTerminal({ title: 'claude — ~/rnd' });
    termB = mockB.addTerminal({ title: 'codex — ~/omi' });
    await mockA.start();
    await mockB.start();

    machines = new MachineManager();
    machines.addMachine({ name: 'macbook', socket: SOCK_A, local: true });
    machines.addMachine({ name: 'mac-mini', socket: SOCK_B, local: true });
    await machines.pollTerminals();

    peers = new PeerRegistry();
    relay = new MessageRelay(machines, peers);
  });

  afterEach(async () => {
    await mockA.stop();
    await mockB.stop();
  });

  it('sends message to correct terminal via send_text', async () => {
    const peerA = peers.register({ name: 'lee', machine: 'macbook', terminal_id: termA.id });
    const peerB = peers.register({ name: 'geni', machine: 'mac-mini', terminal_id: termB.id });

    await relay.sendMessage('lee', 'geni', 'hello from lee');

    expect(mockB.sendTextCalls).toHaveLength(1);
    expect(mockB.sendTextCalls[0].terminal_id).toBe(termB.id);
    expect(mockB.sendTextCalls[0].text).toContain('hello from lee');
    expect(mockB.sendTextCalls[0].text).toContain('lee'); // from field in wrapper
  });

  it('wraps message in boo-message tags', async () => {
    peers.register({ name: 'lee', machine: 'macbook', terminal_id: termA.id });
    peers.register({ name: 'geni', machine: 'mac-mini', terminal_id: termB.id });

    await relay.sendMessage('lee', 'geni', 'test content');

    const sent = mockB.sendTextCalls[0].text;
    expect(sent).toMatch(/<boo-message from="lee" machine="macbook">/);
    expect(sent).toContain('test content');
    expect(sent).toMatch(/<\/boo-message>/);
  });

  it('broadcasts to all peers except sender', async () => {
    const termA2 = mockA.addTerminal({ title: 'bash' });
    await machines.pollTerminals();

    peers.register({ name: 'lee', machine: 'macbook', terminal_id: termA.id });
    peers.register({ name: 'geni', machine: 'mac-mini', terminal_id: termB.id });
    peers.register({ name: 'hiro', machine: 'macbook', terminal_id: termA2.id });

    await relay.broadcast('lee', 'announcement');

    // geni gets it on mac-mini, hiro gets it on macbook
    expect(mockB.sendTextCalls).toHaveLength(1);
    expect(mockA.sendTextCalls).toHaveLength(1);
    expect(mockA.sendTextCalls[0].terminal_id).toBe(termA2.id); // hiro, not lee
  });

  it('throws for unknown recipient', async () => {
    peers.register({ name: 'lee', machine: 'macbook', terminal_id: termA.id });
    await expect(relay.sendMessage('lee', 'nobody', 'hi'))
      .rejects.toThrow('Unknown peer: nobody');
  });

  it('throws for disconnected machine', async () => {
    await mockB.stop();
    peers.register({ name: 'lee', machine: 'macbook', terminal_id: termA.id });
    peers.register({ name: 'geni', machine: 'mac-mini', terminal_id: termB.id });

    await expect(relay.sendMessage('lee', 'geni', 'hi'))
      .rejects.toThrow();
  });

  it('enforces message size limit', async () => {
    peers.register({ name: 'lee', machine: 'macbook', terminal_id: termA.id });
    peers.register({ name: 'geni', machine: 'mac-mini', terminal_id: termB.id });

    const bigContent = 'x'.repeat(65 * 1024); // 65KB > 64KB limit
    await expect(relay.sendMessage('lee', 'geni', bigContent))
      .rejects.toThrow(/size limit/i);
  });

  it('also enqueues message in inbox for receive_messages', async () => {
    peers.register({ name: 'lee', machine: 'macbook', terminal_id: termA.id });
    const peerB = peers.register({ name: 'geni', machine: 'mac-mini', terminal_id: termB.id });

    await relay.sendMessage('lee', 'geni', 'queued msg');

    const messages = peers.dequeueMessages(peerB.peer_id);
    expect(messages).toHaveLength(1);
    expect(messages[0].content).toBe('queued msg');
    expect(messages[0].from).toBe('lee');
  });
});
