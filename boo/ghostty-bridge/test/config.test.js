import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { loadConfig, validateConfig } from '../src/config.js';

function tmpConfig(data) {
  const p = path.join(os.tmpdir(), `boo-config-test-${process.pid}-${Date.now()}.json`);
  fs.writeFileSync(p, JSON.stringify(data));
  return p;
}

describe('config', () => {
  describe('validateConfig', () => {
    it('accepts valid config with local machine', () => {
      const config = {
        machines: [{ name: 'macbook', socket: '/tmp/ghostty.sock', local: true }],
      };
      expect(() => validateConfig(config)).not.toThrow();
    });

    it('accepts valid config with remote machine', () => {
      const config = {
        machines: [{
          name: 'mac-mini',
          socket: '/tmp/ghostty-mm.sock',
          local: false,
          ssh_tunnel: { host: 'mac-mini', remote_socket: '/tmp/ghostty.sock' },
        }],
      };
      expect(() => validateConfig(config)).not.toThrow();
    });

    it('rejects missing machines array', () => {
      expect(() => validateConfig({})).toThrow(/machines/);
    });

    it('rejects empty machines array', () => {
      expect(() => validateConfig({ machines: [] })).toThrow(/at least one/i);
    });

    it('rejects machine without name', () => {
      expect(() => validateConfig({
        machines: [{ socket: '/tmp/x.sock', local: true }],
      })).toThrow(/name/);
    });

    it('rejects machine without socket', () => {
      expect(() => validateConfig({
        machines: [{ name: 'x', local: true }],
      })).toThrow(/socket/);
    });

    it('rejects remote machine without ssh_tunnel', () => {
      expect(() => validateConfig({
        machines: [{ name: 'x', socket: '/tmp/x.sock', local: false }],
      })).toThrow(/ssh_tunnel/);
    });

    it('rejects duplicate machine names', () => {
      expect(() => validateConfig({
        machines: [
          { name: 'x', socket: '/tmp/a.sock', local: true },
          { name: 'x', socket: '/tmp/b.sock', local: true },
        ],
      })).toThrow(/duplicate/i);
    });
  });

  describe('loadConfig', () => {
    it('loads valid config from file', () => {
      const p = tmpConfig({
        machines: [{ name: 'macbook', socket: '/tmp/ghostty.sock', local: true }],
      });
      const config = loadConfig(p);
      expect(config.machines).toHaveLength(1);
      fs.unlinkSync(p);
    });

    it('throws on missing file', () => {
      expect(() => loadConfig('/tmp/nonexistent-boo-config.json')).toThrow();
    });

    it('throws on invalid JSON', () => {
      const p = path.join(os.tmpdir(), `boo-bad-${process.pid}.json`);
      fs.writeFileSync(p, 'not json');
      expect(() => loadConfig(p)).toThrow();
      fs.unlinkSync(p);
    });
  });
});
