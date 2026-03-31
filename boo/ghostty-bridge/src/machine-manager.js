import { GhosttyClient } from './ghostty-client.js';

export class MachineManager {
  constructor() {
    this._machines = new Map();  // name -> { config, client }
    this._terminalMap = new Map(); // terminal_id -> machine_name
  }

  addMachine(config) {
    const client = new GhosttyClient(config.socket);
    this._machines.set(config.name, { config, client });
  }

  removeMachine(name) {
    this._machines.delete(name);
    // Clean up terminal map entries for this machine
    for (const [termId, machineName] of this._terminalMap) {
      if (machineName === name) this._terminalMap.delete(termId);
    }
  }

  getMachines() {
    return [...this._machines.values()].map(m => m.config);
  }

  getClient(name) {
    const entry = this._machines.get(name);
    return entry ? entry.client : null;
  }

  getMachineForTerminal(terminalId) {
    return this._terminalMap.get(terminalId) || null;
  }

  async pollTerminals() {
    const results = [];

    const polls = [...this._machines.entries()].map(async ([name, { client }]) => {
      try {
        const terminals = await client.listTerminals();
        for (const t of terminals) {
          this._terminalMap.set(t.id, name);
          results.push({ ...t, machine: name });
        }
      } catch {
        // Machine unreachable — skip it
      }
    });

    await Promise.all(polls);
    return results;
  }
}
