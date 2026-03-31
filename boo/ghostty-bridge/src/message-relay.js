const MAX_MESSAGE_SIZE = 64 * 1024; // 64KB

export class MessageRelay {
  constructor(machineManager, peerRegistry) {
    this.machines = machineManager;
    this.peers = peerRegistry;
  }

  _wrapMessage(fromName, fromMachine, content) {
    return `<boo-message from="${fromName}" machine="${fromMachine}">\n${content}\n</boo-message>\n`;
  }

  async sendMessage(fromName, toName, content) {
    if (Buffer.byteLength(content) > MAX_MESSAGE_SIZE) {
      throw new Error(`Message exceeds size limit (${MAX_MESSAGE_SIZE} bytes)`);
    }

    const sender = this.peers.getPeer(fromName);
    const recipient = this.peers.getPeer(toName);
    if (!recipient) throw new Error(`Unknown peer: ${toName}`);

    const senderMachine = sender?.machine || 'unknown';
    const client = this.machines.getClient(recipient.machine);
    if (!client) throw new Error(`Machine not connected: ${recipient.machine}`);

    const wrapped = this._wrapMessage(fromName, senderMachine, content);
    await client.sendText(recipient.terminal_id, wrapped);

    // Also enqueue in inbox for polling via receive_messages
    this.peers.enqueueMessage(recipient.peer_id, {
      from: fromName,
      content,
    });
  }

  async broadcast(fromName, content) {
    if (Buffer.byteLength(content) > MAX_MESSAGE_SIZE) {
      throw new Error(`Message exceeds size limit (${MAX_MESSAGE_SIZE} bytes)`);
    }

    const allPeers = this.peers.listPeers();
    const errors = [];

    for (const peer of allPeers) {
      if (peer.name === fromName) continue;

      try {
        await this.sendMessage(fromName, peer.name, content);
      } catch (err) {
        errors.push({ peer: peer.name, error: err.message });
      }
    }

    return { sent: allPeers.length - 1 - errors.length, errors };
  }
}
