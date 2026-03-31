import { randomUUID } from 'node:crypto';

const DEFAULT_PEER_TTL_MS = 10 * 60 * 1000;  // 10 minutes
const DEFAULT_MESSAGE_TTL_MS = 5 * 60 * 1000; // 5 minutes
const DEFAULT_MAX_MESSAGES = 100;

export class PeerRegistry {
  constructor(opts = {}) {
    this.peerTtlMs = opts.peerTtlMs ?? DEFAULT_PEER_TTL_MS;
    this.messageTtlMs = opts.messageTtlMs ?? DEFAULT_MESSAGE_TTL_MS;
    this.maxMessagesPerPeer = opts.maxMessagesPerPeer ?? DEFAULT_MAX_MESSAGES;
    this._peers = new Map();    // peer_id -> peer object
    this._byName = new Map();   // name -> peer_id
    this._inboxes = new Map();  // peer_id -> [{from, content, timestamp}]
  }

  register({ name, role, machine, terminal_id }) {
    // Remove existing peer with same name
    const existingId = this._byName.get(name);
    if (existingId) {
      this._peers.delete(existingId);
      this._inboxes.delete(existingId);
    }

    const peer_id = randomUUID();
    const peer = {
      peer_id,
      name,
      role: role || null,
      machine,
      terminal_id,
      status: 'active',
      last_seen: Date.now(),
    };

    this._peers.set(peer_id, peer);
    this._byName.set(name, peer_id);
    this._inboxes.set(peer_id, []);
    return { ...peer };
  }

  unregister(peerId) {
    const peer = this._peers.get(peerId);
    if (!peer) return;
    this._byName.delete(peer.name);
    this._peers.delete(peerId);
    this._inboxes.delete(peerId);
  }

  listPeers() {
    return [...this._peers.values()].map(p => ({
      ...p,
      last_seen: new Date(p.last_seen).toISOString(),
    }));
  }

  getPeer(nameOrId) {
    // Try by ID first
    if (this._peers.has(nameOrId)) {
      return { ...this._peers.get(nameOrId) };
    }
    // Try by name
    const peerId = this._byName.get(nameOrId);
    if (peerId && this._peers.has(peerId)) {
      return { ...this._peers.get(peerId) };
    }
    return null;
  }

  touch(peerId) {
    const peer = this._peers.get(peerId);
    if (peer) peer.last_seen = Date.now();
  }

  evictExpired() {
    const now = Date.now();
    for (const [id, peer] of this._peers) {
      if (now - peer.last_seen > this.peerTtlMs) {
        this._byName.delete(peer.name);
        this._peers.delete(id);
        this._inboxes.delete(id);
      }
    }
  }

  enqueueMessage(peerId, message) {
    const inbox = this._inboxes.get(peerId);
    if (!inbox) return;
    inbox.push({ ...message, timestamp: Date.now() });
    // Evict oldest if over capacity
    while (inbox.length > this.maxMessagesPerPeer) {
      inbox.shift();
    }
  }

  dequeueMessages(peerId) {
    const inbox = this._inboxes.get(peerId);
    if (!inbox) return [];

    // Filter expired messages
    const now = Date.now();
    const valid = inbox.filter(m => now - m.timestamp <= this.messageTtlMs);

    // Clear inbox
    this._inboxes.set(peerId, []);
    return valid;
  }
}
