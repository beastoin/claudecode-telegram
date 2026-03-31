import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { SshTunnel } from '../src/ssh-tunnel.js';

describe('SshTunnel', () => {
  let tunnel;

  afterEach(async () => {
    if (tunnel) await tunnel.stop();
  });

  it('builds correct ssh command', () => {
    tunnel = new SshTunnel({
      host: 'mac-mini',
      localSocket: '/tmp/ghostty-mm.sock',
      remoteSocket: '/tmp/ghostty.sock',
    });
    const cmd = tunnel._buildCommand();
    expect(cmd).toContain('ssh');
    expect(cmd).toContain('-L');
    expect(cmd).toContain('/tmp/ghostty-mm.sock:/tmp/ghostty.sock');
    expect(cmd).toContain('mac-mini');
    expect(cmd).toContain('-N'); // no remote command
    expect(cmd).toContain('-o');
    expect(cmd).toContain('ExitOnForwardFailure=yes');
  });

  it('starts in stopped state', () => {
    tunnel = new SshTunnel({
      host: 'mac-mini',
      localSocket: '/tmp/ghostty-mm.sock',
      remoteSocket: '/tmp/ghostty.sock',
    });
    expect(tunnel.isRunning()).toBe(false);
  });

  it('reports not running after stop', async () => {
    tunnel = new SshTunnel({
      host: 'mac-mini',
      localSocket: `/tmp/test-tunnel-${process.pid}.sock`,
      remoteSocket: '/tmp/ghostty.sock',
    });
    // Don't actually start (would need real SSH), just verify stop is safe
    await tunnel.stop();
    expect(tunnel.isRunning()).toBe(false);
  });

  it('cleans up local socket on stop', async () => {
    const fs = await import('node:fs');
    const sockPath = `/tmp/test-tunnel-cleanup-${process.pid}.sock`;
    // Create a fake socket file
    fs.writeFileSync(sockPath, '');

    tunnel = new SshTunnel({
      host: 'mac-mini',
      localSocket: sockPath,
      remoteSocket: '/tmp/ghostty.sock',
    });
    await tunnel.stop();
    expect(fs.existsSync(sockPath)).toBe(false);
  });

  it('tracks reconnect count', () => {
    tunnel = new SshTunnel({
      host: 'mac-mini',
      localSocket: '/tmp/ghostty-mm.sock',
      remoteSocket: '/tmp/ghostty.sock',
    });
    expect(tunnel.reconnectCount).toBe(0);
  });
});
