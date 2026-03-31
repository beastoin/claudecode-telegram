import { MachineManager } from './machine-manager.js';
import { PeerRegistry } from './peer-registry.js';
import { MessageRelay } from './message-relay.js';
import { loadConfig } from './config.js';

const TOOL_SCHEMAS = [
  {
    name: 'register_peer',
    description: 'Register this agent session as a peer for cross-agent communication',
    inputSchema: {
      type: 'object',
      properties: {
        name: { type: 'string', description: 'Agent name (e.g. "geni", "lee")' },
        role: { type: 'string', description: 'Agent role (e.g. "researcher", "engineer")' },
        machine: { type: 'string', description: 'Machine name to register on (optional — auto-discovers if omitted)' },
        terminal_id: { type: 'string', description: 'Terminal ID (optional — auto-discovers first terminal on machine)' },
      },
      required: ['name'],
    },
  },
  {
    name: 'list_peers',
    description: 'List all registered agent peers across all connected machines',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'send_message',
    description: 'Send a message to another agent peer',
    inputSchema: {
      type: 'object',
      properties: {
        to: { type: 'string', description: 'Target peer name or ID' },
        content: { type: 'string', description: 'Message content (max 64KB)' },
      },
      required: ['to', 'content'],
    },
  },
  {
    name: 'broadcast',
    description: 'Broadcast a message to all registered peers',
    inputSchema: {
      type: 'object',
      properties: {
        from: { type: 'string', description: 'Sender name' },
        content: { type: 'string', description: 'Message content' },
      },
      required: ['content'],
    },
  },
  {
    name: 'receive_messages',
    description: 'Check for new messages from other peers',
    inputSchema: {
      type: 'object',
      properties: {
        peer_id: { type: 'string', description: 'Your peer ID' },
      },
      required: ['peer_id'],
    },
  },
  {
    name: 'get_peer_status',
    description: 'Check the status of a specific peer',
    inputSchema: {
      type: 'object',
      properties: {
        peer_id: { type: 'string', description: 'Peer ID to check' },
      },
      required: ['peer_id'],
    },
  },
];

/**
 * Create a Bridge instance for programmatic use (testing) or MCP stdio.
 */
export function createBridge(config) {
  const machines = new MachineManager();
  const peers = new PeerRegistry();
  const relay = new MessageRelay(machines, peers);

  // Track which peer name is the "self" for this bridge instance
  let selfName = null;

  for (const m of config.machines) {
    machines.addMachine(m);
  }

  return {
    async init() {
      await machines.pollTerminals();
    },

    listTools() {
      return TOOL_SCHEMAS;
    },

    async callTool(name, args) {
      switch (name) {
        case 'register_peer': {
          let { name: peerName, role, machine, terminal_id } = args;

          // Auto-discover machine/terminal if not provided
          if (!terminal_id && machine) {
            const client = machines.getClient(machine);
            if (client) {
              const terminals = await client.listTerminals();
              if (terminals.length > 0) terminal_id = terminals[0].id;
            }
          }
          if (!machine && !terminal_id) {
            // Pick first machine with terminals
            const allTerminals = await machines.pollTerminals();
            if (allTerminals.length > 0) {
              machine = allTerminals[0].machine;
              terminal_id = allTerminals[0].id;
            }
          }

          selfName = peerName;
          return peers.register({
            name: peerName,
            role,
            machine: machine || 'unknown',
            terminal_id: terminal_id || 'unknown',
          });
        }

        case 'list_peers':
          return peers.listPeers();

        case 'send_message': {
          const fromName = selfName || 'unknown';
          await relay.sendMessage(fromName, args.to, args.content);
          return { ok: true };
        }

        case 'broadcast': {
          const fromName = args.from || selfName || 'unknown';
          return relay.broadcast(fromName, args.content);
        }

        case 'receive_messages':
          return peers.dequeueMessages(args.peer_id);

        case 'get_peer_status': {
          const peer = peers.getPeer(args.peer_id);
          return peer || null;
        }

        default:
          throw new Error(`Unknown tool: ${name}`);
      }
    },

    // Expose internals for advanced testing
    machines,
    peers,
    relay,
  };
}

/**
 * MCP stdio server entry point.
 * Spawned by Claude Code / Codex as an MCP server.
 */
async function main() {
  const configPath = process.env.BOO_CONFIG;
  if (!configPath) {
    console.error('BOO_CONFIG environment variable not set');
    process.exit(1);
  }

  const { McpServer } = await import('@modelcontextprotocol/sdk/server/mcp.js');
  const { StdioServerTransport } = await import('@modelcontextprotocol/sdk/server/stdio.js');

  const config = loadConfig(configPath);
  const bridge = createBridge(config);
  await bridge.init();

  const server = new McpServer({ name: 'boo', version: '0.1.0' });

  for (const schema of TOOL_SCHEMAS) {
    server.tool(schema.name, schema.description, schema.inputSchema.properties, async (args) => {
      try {
        const result = await bridge.callTool(schema.name, args);
        return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
      } catch (err) {
        return { content: [{ type: 'text', text: `Error: ${err.message}` }], isError: true };
      }
    });
  }

  const transport = new StdioServerTransport();
  await server.connect(transport);
}

// Run if executed directly (not imported)
if (process.argv[1] && import.meta.url.endsWith(process.argv[1].replace(/^\//, ''))) {
  main().catch(err => {
    console.error(err);
    process.exit(1);
  });
}
