/**
 * Shared Bun server infrastructure for spawn-on-demand viewers.
 * Provides: CLI arg parsing, token auth, idle TTL self-destruct, Bun.serve wrapper.
 */

export interface ServerArgs {
  port: number;
  token: string;
  ttl: number;
  [key: string]: string | number | boolean;
}

export interface RouteHandler {
  (req: Request, url: URL): Response | Promise<Response>;
}

export interface ServerOptions {
  args: ServerArgs;
  routes: (url: URL, req: Request) => Response | Promise<Response> | null;
  onStart?: () => void;
}

/**
 * Parse CLI args: --port 10171 --token abc --ttl 300 --custom-arg value
 * Booleans: --flag (no value) → true
 */
export function parseArgs(argv: string[]): ServerArgs {
  const args: Record<string, string | number | boolean> = {};
  // Skip bun + script path
  const raw = argv.slice(2);
  for (let i = 0; i < raw.length; i++) {
    const a = raw[i];
    if (!a.startsWith("--")) continue;
    const key = a.slice(2);
    const next = raw[i + 1];
    if (next === undefined || next.startsWith("--")) {
      args[key] = true;
    } else {
      // Only parse known numeric keys as numbers
      if (key === "port" || key === "ttl") {
        const num = Number(next);
        args[key] = isNaN(num) ? next : num;
      } else {
        args[key] = next;
      }
      i++;
    }
  }
  // Required fields with defaults
  const port = typeof args.port === "number" ? args.port : 10171;
  const token = typeof args.token === "string" ? args.token : "";
  const ttl = typeof args.ttl === "number" ? args.ttl : 300;
  if (!token) {
    console.error("ERROR: --token is required");
    process.exit(1);
  }
  return { ...args, port, token, ttl } as ServerArgs;
}

/** Validate token from ?token= query param */
function checkToken(url: URL, expected: string): boolean {
  return url.searchParams.get("token") === expected;
}

/** JSON response helper */
export function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

/** HTML response helper */
export function htmlResponse(html: string, status = 200): Response {
  return new Response(html, {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
  });
}

/** Error page HTML */
export function errorPage(title: string, detail = ""): string {
  return `<!DOCTYPE html><html><head><meta charset="utf-8"><title>Error</title>
<style>body{font-family:system-ui;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;background:#0b0d0b;color:#e5e5e0}
.e{text-align:center}.e h1{font-size:1.5rem;margin-bottom:.5rem}.e p{color:#9ca49c;font-size:.9rem}</style></head>
<body><div class="e"><h1>${title}</h1>${detail ? `<p>${detail}</p>` : ""}</div></body></html>`;
}

/**
 * Create and start a Bun server with token auth and idle TTL.
 * The server self-destructs after `ttl` seconds of inactivity.
 */
export function createServer(opts: ServerOptions): void {
  const { args, routes, onStart } = opts;
  let timer: Timer | null = null;

  function resetTimer() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      console.log(`[viewer] TTL expired after ${args.ttl}s of inactivity — shutting down`);
      process.exit(0);
    }, args.ttl * 1000);
  }

  const server = Bun.serve({
    port: args.port,
    hostname: "127.0.0.1",
    fetch(req: Request): Response | Promise<Response> {
      const url = new URL(req.url);

      // Token auth on every request
      if (!checkToken(url, args.token)) {
        return htmlResponse(
          errorPage(
            "Session Expired",
            "This link has expired or is invalid. Send <code>/rewind &lt;name&gt;</code> in Telegram to get a fresh link."
          ),
          403
        );
      }

      // Reset idle timer on each valid request
      resetTimer();

      // Route
      const result = routes(url, req);
      if (result) return result;
      return jsonResponse({ error: "Not found" }, 404);
    },
  });

  // Start idle timer
  resetTimer();

  console.log(`[viewer] Listening on http://localhost:${server.port} (TTL: ${args.ttl}s)`);
  if (onStart) onStart();
}
