import { spawn } from 'node:child_process';
import fs from 'node:fs';

/**
 * Manages an SSH tunnel that forwards a remote Unix socket to a local path.
 * ssh -L localSocket:remoteSocket host -N
 */
export class SshTunnel {
  constructor({ host, localSocket, remoteSocket, reconnectDelayMs = 3000 }) {
    this.host = host;
    this.localSocket = localSocket;
    this.remoteSocket = remoteSocket;
    this.reconnectDelayMs = reconnectDelayMs;
    this.reconnectCount = 0;
    this._process = null;
    this._stopping = false;
    this._reconnectTimer = null;
  }

  _buildCommand() {
    return [
      'ssh',
      '-N',                              // no remote command
      '-o', 'ExitOnForwardFailure=yes',  // fail fast if forward fails
      '-o', 'ServerAliveInterval=10',    // detect dead connections
      '-o', 'ServerAliveCountMax=3',
      '-L', `${this.localSocket}:${this.remoteSocket}`,
      this.host,
    ];
  }

  isRunning() {
    return this._process !== null && !this._process.killed;
  }

  async start() {
    this._stopping = false;
    this._cleanupSocket();

    const args = this._buildCommand();
    const cmd = args.shift();

    this._process = spawn(cmd, args, {
      stdio: 'ignore',
      detached: false,
    });

    this._process.on('exit', (code) => {
      this._process = null;
      if (!this._stopping) {
        this.reconnectCount++;
        this._reconnectTimer = setTimeout(() => this.start(), this.reconnectDelayMs);
      }
    });

    this._process.on('error', () => {
      this._process = null;
    });
  }

  async stop() {
    this._stopping = true;
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    if (this._process) {
      this._process.kill('SIGTERM');
      this._process = null;
    }
    this._cleanupSocket();
  }

  _cleanupSocket() {
    try { fs.unlinkSync(this.localSocket); } catch {}
  }
}
