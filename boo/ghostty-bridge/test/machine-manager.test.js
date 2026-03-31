import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { MockGhosttySocket } from './fixtures/mock-ghostty-socket.js';
import { MachineManager } from '../src/machine-manager.js';

const SOCK_A = `/tmp/test-mm-a-${process.pid}.sock`;
const SOCK_B = `/tmp/test-mm-b-${process.pid}.sock`;

describe('MachineManager', () => {
  let mockA, mockB;
  let manager;

  beforeEach(async () => {
    mockA = new MockGhosttySocket(SOCK_A);
    mockB = new MockGhosttySocket(SOCK_B);
    await mockA.start();
    await mockB.start();
    manager = new MachineManager();
  });

  afterEach(async () => {
    await mockA.stop();
    await mockB.stop();
  });

  it('adds a local machine', () => {
    manager.addMachine({ name: 'macbook', socket: SOCK_A, local: true });
    expect(manager.getMachines()).toHaveLength(1);
    expect(manager.getMachines()[0].name).toBe('macbook');
  });

  it('removes a machine', () => {
    manager.addMachine({ name: 'macbook', socket: SOCK_A, local: true });
    manager.removeMachine('macbook');
    expect(manager.getMachines()).toHaveLength(0);
  });

  it('polls terminals from one machine', async () => {
    mockA.addTerminal({ title: 'claude — ~/rnd' });
    manager.addMachine({ name: 'macbook', socket: SOCK_A, local: true });

    const terminals = await manager.pollTerminals();
    expect(terminals).toHaveLength(1);
    expect(terminals[0].machine).toBe('macbook');
    expect(terminals[0].title).toBe('claude — ~/rnd');
  });

  it('polls terminals from multiple machines', async () => {
    mockA.addTerminal({ title: 'pane-A1' });
    mockA.addTerminal({ title: 'pane-A2' });
    mockB.addTerminal({ title: 'pane-B1' });
    manager.addMachine({ name: 'macbook', socket: SOCK_A, local: true });
    manager.addMachine({ name: 'mac-mini', socket: SOCK_B, local: true });

    const terminals = await manager.pollTerminals();
    expect(terminals).toHaveLength(3);
    const machines = terminals.map(t => t.machine);
    expect(machines).toContain('macbook');
    expect(machines).toContain('mac-mini');
  });

  it('handles disconnected machine gracefully', async () => {
    mockA.addTerminal({ title: 'alive' });
    await mockB.stop(); // kill machine B

    manager.addMachine({ name: 'macbook', socket: SOCK_A, local: true });
    manager.addMachine({ name: 'mac-mini', socket: SOCK_B, local: true });

    const terminals = await manager.pollTerminals();
    // Should still return terminals from machine A
    expect(terminals).toHaveLength(1);
    expect(terminals[0].machine).toBe('macbook');
  });

  it('getClient returns client for a machine', () => {
    manager.addMachine({ name: 'macbook', socket: SOCK_A, local: true });
    const client = manager.getClient('macbook');
    expect(client).toBeDefined();
  });

  it('getClient returns null for unknown machine', () => {
    expect(manager.getClient('unknown')).toBeNull();
  });

  it('maps terminal IDs to machine names', async () => {
    const tA = mockA.addTerminal({ title: 'pane-A' });
    const tB = mockB.addTerminal({ title: 'pane-B' });
    manager.addMachine({ name: 'macbook', socket: SOCK_A, local: true });
    manager.addMachine({ name: 'mac-mini', socket: SOCK_B, local: true });

    await manager.pollTerminals();
    expect(manager.getMachineForTerminal(tA.id)).toBe('macbook');
    expect(manager.getMachineForTerminal(tB.id)).toBe('mac-mini');
    expect(manager.getMachineForTerminal('unknown')).toBeNull();
  });
});
