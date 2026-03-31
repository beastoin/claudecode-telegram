import { describe, it, expect, beforeEach, vi } from 'vitest';
import { PeerRegistry } from '../src/peer-registry.js';

describe('PeerRegistry', () => {
  let registry;

  beforeEach(() => {
    registry = new PeerRegistry();
  });

  describe('register', () => {
    it('registers a peer and returns peer_id', () => {
      const peer = registry.register({
        name: 'geni',
        role: 'researcher',
        machine: 'mac-mini',
        terminal_id: 'term-1',
      });
      expect(peer.peer_id).toBeDefined();
      expect(peer.name).toBe('geni');
      expect(peer.role).toBe('researcher');
    });

    it('replaces existing peer with same name', () => {
      const p1 = registry.register({ name: 'geni', machine: 'mac-mini', terminal_id: 't1' });
      const p2 = registry.register({ name: 'geni', machine: 'macbook', terminal_id: 't2' });
      expect(p2.peer_id).not.toBe(p1.peer_id);
      expect(registry.listPeers()).toHaveLength(1);
      expect(registry.listPeers()[0].machine).toBe('macbook');
    });
  });

  describe('unregister', () => {
    it('removes a peer by id', () => {
      const peer = registry.register({ name: 'lee', machine: 'vps', terminal_id: 't1' });
      registry.unregister(peer.peer_id);
      expect(registry.listPeers()).toHaveLength(0);
    });

    it('no-ops for unknown id', () => {
      registry.unregister('nonexistent');
      expect(registry.listPeers()).toHaveLength(0);
    });
  });

  describe('listPeers', () => {
    it('returns all registered peers', () => {
      registry.register({ name: 'geni', machine: 'mac-mini', terminal_id: 't1' });
      registry.register({ name: 'lee', machine: 'vps', terminal_id: 't2' });
      const peers = registry.listPeers();
      expect(peers).toHaveLength(2);
      expect(peers.map(p => p.name).sort()).toEqual(['geni', 'lee']);
    });

    it('returns empty array when no peers', () => {
      expect(registry.listPeers()).toEqual([]);
    });
  });

  describe('getPeer', () => {
    it('finds peer by name', () => {
      registry.register({ name: 'geni', machine: 'mac-mini', terminal_id: 't1' });
      const peer = registry.getPeer('geni');
      expect(peer.name).toBe('geni');
    });

    it('finds peer by id', () => {
      const reg = registry.register({ name: 'geni', machine: 'mac-mini', terminal_id: 't1' });
      const peer = registry.getPeer(reg.peer_id);
      expect(peer.name).toBe('geni');
    });

    it('returns null for unknown peer', () => {
      expect(registry.getPeer('nobody')).toBeNull();
    });
  });

  describe('TTL expiry', () => {
    it('expires peers after TTL', () => {
      vi.useFakeTimers();
      registry = new PeerRegistry({ peerTtlMs: 1000 });
      registry.register({ name: 'geni', machine: 'mac-mini', terminal_id: 't1' });
      expect(registry.listPeers()).toHaveLength(1);

      vi.advanceTimersByTime(1001);
      registry.evictExpired();
      expect(registry.listPeers()).toHaveLength(0);
      vi.useRealTimers();
    });

    it('touch refreshes TTL', () => {
      vi.useFakeTimers();
      registry = new PeerRegistry({ peerTtlMs: 1000 });
      const peer = registry.register({ name: 'geni', machine: 'mac-mini', terminal_id: 't1' });

      vi.advanceTimersByTime(800);
      registry.touch(peer.peer_id);

      vi.advanceTimersByTime(800); // 1600ms total, but touched at 800ms
      registry.evictExpired();
      expect(registry.listPeers()).toHaveLength(1);

      vi.advanceTimersByTime(201); // now past the touched TTL
      registry.evictExpired();
      expect(registry.listPeers()).toHaveLength(0);
      vi.useRealTimers();
    });
  });

  describe('message inbox', () => {
    it('enqueues and dequeues messages', () => {
      const peer = registry.register({ name: 'geni', machine: 'mac-mini', terminal_id: 't1' });
      registry.enqueueMessage(peer.peer_id, { from: 'lee', content: 'hello' });
      registry.enqueueMessage(peer.peer_id, { from: 'lee', content: 'world' });

      const messages = registry.dequeueMessages(peer.peer_id);
      expect(messages).toHaveLength(2);
      expect(messages[0].content).toBe('hello');
      expect(messages[1].content).toBe('world');

      // Dequeue again returns empty
      expect(registry.dequeueMessages(peer.peer_id)).toHaveLength(0);
    });

    it('returns empty for unknown peer', () => {
      expect(registry.dequeueMessages('nobody')).toEqual([]);
    });

    it('evicts oldest when over capacity', () => {
      registry = new PeerRegistry({ maxMessagesPerPeer: 3 });
      const peer = registry.register({ name: 'geni', machine: 'mac-mini', terminal_id: 't1' });
      for (let i = 0; i < 5; i++) {
        registry.enqueueMessage(peer.peer_id, { from: 'lee', content: `msg-${i}` });
      }
      const messages = registry.dequeueMessages(peer.peer_id);
      expect(messages).toHaveLength(3);
      expect(messages[0].content).toBe('msg-2'); // oldest 2 evicted
    });

    it('expires old messages', () => {
      vi.useFakeTimers();
      registry = new PeerRegistry({ messageTtlMs: 1000 });
      const peer = registry.register({ name: 'geni', machine: 'mac-mini', terminal_id: 't1' });
      registry.enqueueMessage(peer.peer_id, { from: 'lee', content: 'old' });

      vi.advanceTimersByTime(1001);
      registry.enqueueMessage(peer.peer_id, { from: 'lee', content: 'new' });

      const messages = registry.dequeueMessages(peer.peer_id);
      expect(messages).toHaveLength(1);
      expect(messages[0].content).toBe('new');
      vi.useRealTimers();
    });
  });
});
