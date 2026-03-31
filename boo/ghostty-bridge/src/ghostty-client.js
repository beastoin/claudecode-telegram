import net from 'node:net';

/**
 * JSON-RPC 2.0 client for Ghostty's Unix socket API.
 */
export class GhosttyClient {
  constructor(socketPath) {
    this.socketPath = socketPath;
    this._nextId = 1;
  }

  /**
   * Send a JSON-RPC request to the Ghostty socket.
   * Uses HTTP/1.1 POST with Connection: close (same as Ghostty's protocol).
   */
  async _call(method, params) {
    const id = this._nextId++;
    const request = { jsonrpc: '2.0', method, id };
    if (params !== undefined) request.params = params;

    const body = JSON.stringify(request);
    const httpReq = [
      'POST / HTTP/1.1',
      'Content-Type: application/json',
      `Content-Length: ${Buffer.byteLength(body)}`,
      'Connection: close',
      '',
      body,
    ].join('\r\n');

    const responseData = await new Promise((resolve, reject) => {
      const socket = net.createConnection(this.socketPath, () => {
        socket.write(httpReq);
      });

      let data = '';
      socket.on('data', chunk => { data += chunk.toString(); });
      socket.on('end', () => resolve(data));
      socket.on('error', reject);
      socket.setTimeout(5000, () => {
        socket.destroy(new Error('Socket timeout'));
      });
    });

    // Parse HTTP response body
    const bodyStart = responseData.indexOf('\r\n\r\n');
    if (bodyStart === -1) throw new Error('Invalid HTTP response');

    const json = JSON.parse(responseData.slice(bodyStart + 4));

    if (json.error) {
      throw new Error(json.error.message || `JSON-RPC error ${json.error.code}`);
    }

    return json.result;
  }

  async listTerminals() {
    const result = await this._call('list_terminals');
    return result.terminals;
  }

  async getTerminalInfo(terminalId) {
    return this._call('get_terminal_info', { terminal_id: terminalId });
  }

  async sendText(terminalId, text) {
    return this._call('send_text', { terminal_id: terminalId, text });
  }

  async sendKey(terminalId, key, modifiers = []) {
    return this._call('send_key', { terminal_id: terminalId, key, modifiers });
  }

  async readScreen(terminalId, start, end) {
    const params = { terminal_id: terminalId };
    if (start !== undefined || end !== undefined) {
      params.rows = {};
      if (start !== undefined) params.rows.start = start;
      if (end !== undefined) params.rows.end = end;
    }
    const result = await this._call('read_screen', params);
    return result.lines;
  }
}
