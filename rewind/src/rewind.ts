/**
 * Rewind transcript tool — spawn-on-demand Bun server.
 * Usage: bun rewind/src/rewind.ts --port 10171 --token abc --ttl 300 --jsonl-path /path/to.jsonl --name lee --session-id abc123
 */
import { parseArgs, createServer, type ServerArgs } from "./server";
import { createRewindRoutes } from "./routes";

const args = parseArgs(process.argv);
const jsonlPath = String(args["jsonl-path"] || "");
const name = String(args.name || "");
const sessionId = String(args["session-id"] || "");

if (!jsonlPath) {
  console.error("ERROR: --jsonl-path is required");
  process.exit(1);
}
if (!name) {
  console.error("ERROR: --name is required");
  process.exit(1);
}
if (!sessionId) {
  console.error("ERROR: --session-id is required");
  process.exit(1);
}

const route = createRewindRoutes({
  jsonlPath,
  name,
  sessionId,
  token: args.token,
});

createServer({
  args,
  routes: route,
  onStart: () => {
    console.log(`[rewind] Serving transcript for "${name}" (session: ${sessionId.slice(0, 12)}…)`);
    console.log(`[rewind] JSONL: ${jsonlPath}`);
  },
});
