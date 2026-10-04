/**
 * Behavior tests for pilot server — the terminal session sharing server.
 *
 * Tests HTTP + WebSocket server that exposes tmux sessions in a browser.
 * Run: bun test pilot/pilot_behavior.test.ts
 *
 * Requires: bun, node (for ws), tmux
 */
import { describe, test, expect, beforeAll, afterAll } from "bun:test";
import { spawn, execSync, type ChildProcess } from "child_process";
import { join } from "path";
import { existsSync } from "fs";
import WebSocket from "ws";

// ── Config ──────────────────────────────────────────────────────────────────

const PILOT_JS = join(import.meta.dir, "dist", "pilot.js");
const TEST_SESSION = "claude-test-pilot-a";
const TEST_SESSION_B = "claude-test-pilot-b";

let serverProc: ChildProcess;
let authServerProc: ChildProcess;
let BASE_URL: string;
let PORT: number;
let AUTH_URL: string;
let AUTH_PORT: number;
const AUTH_TOKEN = "test-secret-token-42";

// ── Helpers ─────────────────────────────────────────────────────────────────

function freePort(): number {
  const server = Bun.serve({ port: 0, fetch: () => new Response() });
  const port = server.port;
  server.stop(true);
  return port;
}

function tmuxRun(args: string[]): string {
  try {
    return execSync(["tmux", ...args].join(" "), { encoding: "utf-8", timeout: 5000 }).trim();
  } catch {
    return "";
  }
}

function createTmuxSession(name: string): void {
  tmuxRun(["kill-session", "-t", name]);
  execSync(`tmux new-session -d -s ${name} "bash -lc 'echo ${name}-ready; exec bash'"`, { timeout: 5000 });
}

function killTmuxSession(name: string): void {
  tmuxRun(["kill-session", "-t", name]);
}

async function waitForServer(url: string, timeout = 8000): Promise<boolean> {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(url);
      if (r.status === 200 || r.status === 401) return true;
    } catch {}
    await Bun.sleep(100);
  }
  return false;
}

async function enableSession(base: string, name: string, token?: string): Promise<Response> {
  const qs = token ? `session=${name}&token=${token}` : `session=${name}`;
  return fetch(`${base}/api/pilot?${qs}`, { method: "POST" });
}

async function disableSession(base: string, name: string): Promise<Response> {
  return fetch(`${base}/api/pilot?session=${name}`, { method: "DELETE" });
}

function startServer(port: number, env: Record<string, string> = {}): ChildProcess {
  return spawn("node", [PILOT_JS], {
    env: { ...process.env, PORT: String(port), ...env },
    stdio: "pipe",
  });
}

function wsConnect(url: string): Promise<{ ws: WebSocket; opened: boolean; closed: boolean; error?: string }> {
  return new Promise((resolve) => {
    let opened = false;
    const ws = new WebSocket(url);
    ws.on("open", () => {
      opened = true;
      resolve({ ws, opened: true, closed: false });
    });
    ws.on("error", (e) => resolve({ ws, opened: false, closed: false, error: (e as Error).message }));
    ws.on("close", () => {
      if (!opened) resolve({ ws, opened: false, closed: true });
    });
    setTimeout(() => resolve({ ws, opened: false, closed: false, error: "TIMEOUT" }), 5000);
  });
}

// ── Setup / Teardown ────────────────────────────────────────────────────────

beforeAll(async () => {
  if (!existsSync(PILOT_JS)) throw new Error(`dist/pilot.js not found at ${PILOT_JS}`);

  createTmuxSession(TEST_SESSION);
  createTmuxSession(TEST_SESSION_B);

  // No-auth server
  PORT = freePort();
  BASE_URL = `http://127.0.0.1:${PORT}`;
  serverProc = startServer(PORT);
  const ready = await waitForServer(BASE_URL);
  if (!ready) throw new Error("Pilot server failed to start");

  // Auth server
  AUTH_PORT = freePort();
  AUTH_URL = `http://127.0.0.1:${AUTH_PORT}`;
  authServerProc = startServer(AUTH_PORT, { PILOT_TOKEN: AUTH_TOKEN });
  const authReady = await waitForServer(`${AUTH_URL}/?token=${AUTH_TOKEN}`);
  if (!authReady) throw new Error("Auth pilot server failed to start");
});

afterAll(() => {
  serverProc?.kill("SIGTERM");
  authServerProc?.kill("SIGTERM");
  killTmuxSession(TEST_SESSION);
  killTmuxSession(TEST_SESSION_B);
});

// ── Tests: API capture ──────────────────────────────────────────────────────

describe("API capture", () => {
  test("returns actual tmux pane content", async () => {
    await enableSession(BASE_URL, TEST_SESSION);
    const marker = "PILOT_CAPTURE_TEST_12345";
    execSync(`tmux send-keys -t ${TEST_SESSION} "echo ${marker}" Enter`, { timeout: 3000 });
    await Bun.sleep(300);

    const r = await fetch(`${BASE_URL}/api/capture?session=${TEST_SESSION}`);
    expect(r.status).toBe(200);
    const data = (await r.json()) as { text: string };
    expect(data.text).toContain(marker);
  });

  test("disabled session returns 404", async () => {
    await disableSession(BASE_URL, TEST_SESSION);
    const r = await fetch(`${BASE_URL}/api/capture?session=${TEST_SESSION}`);
    expect(r.status).toBe(404);
  });

  test("nonexistent session returns 404", async () => {
    const r = await fetch(`${BASE_URL}/api/capture?session=claude-does-not-exist-xyz`);
    expect(r.status).toBe(404);
  });
});

// ── Tests: Session page modes ───────────────────────────────────────────────

describe("session page modes", () => {
  test("readonly=1 includes readonly indicator", async () => {
    await enableSession(BASE_URL, TEST_SESSION);
    const r = await fetch(`${BASE_URL}/session/${TEST_SESSION}?readonly=1`);
    expect(r.status).toBe(200);
    const html = await r.text();
    expect(html.toLowerCase()).toContain("readonly");
  });

  test("embed=1&hideheader=1 hides header", async () => {
    await enableSession(BASE_URL, TEST_SESSION);
    const r = await fetch(`${BASE_URL}/session/${TEST_SESSION}?embed=1&hideheader=1`);
    expect(r.status).toBe(200);
    const html = await r.text();
    expect(html).toContain("display:none !important");
  });

  test("normal page shows session name", async () => {
    await enableSession(BASE_URL, TEST_SESSION);
    const r = await fetch(`${BASE_URL}/session/${TEST_SESSION}`);
    expect(r.status).toBe(200);
    const html = await r.text();
    expect(html).toContain(TEST_SESSION);
  });
});

// ── Tests: Grid view ────────────────────────────────────────────────────────

describe("grid view", () => {
  test("GET /grid returns HTML", async () => {
    const r = await fetch(`${BASE_URL}/grid`);
    expect(r.status).toBe(200);
    expect(r.headers.get("Content-Type")).toContain("text/html");
  });

  test("grid session CRUD", async () => {
    await enableSession(BASE_URL, TEST_SESSION);
    const create = await fetch(`${BASE_URL}/api/grid-session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug: "test-grid", sessions: [TEST_SESSION], ttl: 60 }),
    });
    expect(create.status).toBe(200);
    const data = (await create.json()) as { ok: boolean; slug: string };
    expect(data.ok).toBe(true);
    expect(data.slug).toBe("test-grid");

    const get = await fetch(`${BASE_URL}/grid/test-grid`);
    expect(get.status).toBe(200);
  });

  test("missing slug or sessions returns 400", async () => {
    const noSlug = await fetch(`${BASE_URL}/api/grid-session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessions: ["x"] }),
    });
    expect(noSlug.status).toBe(400);

    const noSessions = await fetch(`${BASE_URL}/api/grid-session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug: "bad" }),
    });
    expect(noSessions.status).toBe(400);
  });

  test("nonexistent grid slug returns 404", async () => {
    const r = await fetch(`${BASE_URL}/grid/no-such-grid-session`);
    expect(r.status).toBe(404);
  });
});

// ── Tests: Debug page ───────────────────────────────────────────────────────

describe("debug page", () => {
  test("GET /debug returns HTML", async () => {
    const r = await fetch(`${BASE_URL}/debug`);
    expect(r.status).toBe(200);
    expect(r.headers.get("Content-Type")).toContain("text/html");
  });
});

// ── Tests: Session enable/disable edge cases ────────────────────────────────

describe("session management", () => {
  test("enable nonexistent session returns 404", async () => {
    const r = await fetch(`${BASE_URL}/api/pilot?session=claude-nonexistent-xyz-999`, { method: "POST" });
    expect(r.status).toBe(404);
  });

  test("non-claude prefix rejected with 400", async () => {
    const r = await fetch(`${BASE_URL}/api/pilot?session=not-claude-prefix`, { method: "POST" });
    expect(r.status).toBe(400);
  });

  test("claude-prefixed session accepted", async () => {
    const r = await enableSession(BASE_URL, TEST_SESSION);
    expect(r.status).toBe(200);
    const data = (await r.json()) as { enabled: boolean };
    expect(data.enabled).toBe(true);
  });

  test("bad remote host handled gracefully", async () => {
    const r = await fetch(`${BASE_URL}/api/pilot?session=${TEST_SESSION}&host=nonexistent-host-xyz`, {
      method: "POST",
    });
    expect([404, 500]).toContain(r.status);
    // Server still alive
    const alive = await fetch(`${BASE_URL}/api/sessions`);
    expect(alive.status).toBe(200);
  });

  test("disable already-disabled session is idempotent", async () => {
    await disableSession(BASE_URL, TEST_SESSION);
    const r = await disableSession(BASE_URL, TEST_SESSION);
    expect([200, 404]).toContain(r.status);
  });
});

// ── Tests: Auth token ───────────────────────────────────────────────────────

describe("auth token", () => {
  test("blocks unauthenticated requests with 401", async () => {
    const r = await fetch(`${AUTH_URL}/`);
    expect(r.status).toBe(401);

    const r2 = await fetch(`${AUTH_URL}/api/sessions`);
    expect(r2.status).toBe(401);
  });

  test("allows authenticated requests", async () => {
    const r = await fetch(`${AUTH_URL}/?token=${AUTH_TOKEN}`);
    expect(r.status).toBe(200);

    const r2 = await fetch(`${AUTH_URL}/api/sessions?token=${AUTH_TOKEN}`);
    expect(r2.status).toBe(200);
  });

  test("wrong token returns 401", async () => {
    const r = await fetch(`${AUTH_URL}/?token=wrong-value`);
    expect(r.status).toBe(401);
  });

  test("protects POST /api/pilot", async () => {
    const r = await fetch(`${AUTH_URL}/api/pilot?session=${TEST_SESSION}`, { method: "POST" });
    expect(r.status).toBe(401);
  });

  test("protects session page", async () => {
    await enableSession(AUTH_URL, TEST_SESSION, AUTH_TOKEN);
    const r = await fetch(`${AUTH_URL}/session/${TEST_SESSION}`);
    expect(r.status).toBe(401);
  });
});

// ── Tests: WebSocket readonly ───────────────────────────────────────────────

describe("WebSocket readonly", () => {
  test("readonly mode drops input", async () => {
    await enableSession(BASE_URL, TEST_SESSION);
    execSync(`tmux send-keys -t ${TEST_SESSION} "clear" Enter`, { timeout: 3000 });
    await Bun.sleep(300);

    const marker = "READONLY_TEST_SHOULD_NOT_APPEAR";
    const { ws, opened } = await wsConnect(
      `ws://127.0.0.1:${PORT}/ws?session=${TEST_SESSION}&cols=80&rows=24&readonly=1`
    );
    expect(opened).toBe(true);

    ws.send(`${marker}\n`);
    await Bun.sleep(500);
    ws.close();

    const after = execSync(`tmux capture-pane -t ${TEST_SESSION} -p`, { encoding: "utf-8" });
    expect(after).not.toContain(marker);
  });
});

// ── Tests: Multiple sessions ────────────────────────────────────────────────

describe("multiple sessions", () => {
  test("enabling two sessions lists both", async () => {
    await enableSession(BASE_URL, TEST_SESSION);
    await enableSession(BASE_URL, TEST_SESSION_B);

    const r = await fetch(`${BASE_URL}/api/sessions`);
    const sessions = (await r.json()) as Array<{ name: string }>;
    const names = sessions.map((s) => s.name);
    expect(names).toContain(TEST_SESSION);
    expect(names).toContain(TEST_SESSION_B);
  });

  test("disabling one keeps the other", async () => {
    await enableSession(BASE_URL, TEST_SESSION);
    await enableSession(BASE_URL, TEST_SESSION_B);
    await disableSession(BASE_URL, TEST_SESSION);

    const r = await fetch(`${BASE_URL}/api/sessions`);
    const names = ((await r.json()) as Array<{ name: string }>).map((s) => s.name);
    expect(names).not.toContain(TEST_SESSION);
    expect(names).toContain(TEST_SESSION_B);
  });
});

// ── Tests: Index page ───────────────────────────────────────────────────────

describe("index page", () => {
  test("lists enabled sessions", async () => {
    await enableSession(BASE_URL, TEST_SESSION);
    const r = await fetch(`${BASE_URL}/`);
    expect(r.status).toBe(200);
    expect(await r.text()).toContain(TEST_SESSION);
  });

  test("excludes disabled sessions", async () => {
    await disableSession(BASE_URL, TEST_SESSION);
    await disableSession(BASE_URL, TEST_SESSION_B);
    const r = await fetch(`${BASE_URL}/`);
    const html = await r.text();
    expect(html).not.toContain(TEST_SESSION);
    expect(html).not.toContain(TEST_SESSION_B);
  });
});

// ── Tests: WebSocket interactive input ──────────────────────────────────────

describe("WebSocket interactive input", () => {
  test("non-readonly mode delivers input to tmux", async () => {
    await enableSession(BASE_URL, TEST_SESSION);
    // Clear the pane first
    execSync(`tmux send-keys -t ${TEST_SESSION} "clear" Enter`, { timeout: 3000 });
    await Bun.sleep(300);

    const marker = `PILOT_WS_INPUT_${Date.now()}`;
    const { ws, opened } = await wsConnect(
      `ws://127.0.0.1:${PORT}/ws?session=${TEST_SESSION}&cols=80&rows=24`
    );
    expect(opened).toBe(true);

    // Send an echo command through the WS (non-readonly), then Enter
    ws.send(`echo ${marker}\n`);
    await Bun.sleep(1000);
    ws.close();

    // Verify the marker made it into tmux output
    const pane = execSync(`tmux capture-pane -t ${TEST_SESSION} -p`, { encoding: "utf-8" });
    expect(pane).toContain(marker);
  });
});

// ── Tests: Auth enforcement on protected endpoints ─────────────────────────

describe("auth enforcement on protected endpoints", () => {
  test("/api/capture blocked without token", async () => {
    await enableSession(AUTH_URL, TEST_SESSION, AUTH_TOKEN);
    const r = await fetch(`${AUTH_URL}/api/capture?session=${TEST_SESSION}`);
    expect(r.status).toBe(401);
  });

  test("/api/enabled blocked without token", async () => {
    const r = await fetch(`${AUTH_URL}/api/enabled`);
    expect(r.status).toBe(401);
  });

  test("/grid blocked without token", async () => {
    const r = await fetch(`${AUTH_URL}/grid`);
    expect(r.status).toBe(401);
  });

  test("/debug blocked without token", async () => {
    const r = await fetch(`${AUTH_URL}/debug`);
    expect(r.status).toBe(401);
  });

  test("WS upgrade blocked without token", async () => {
    await enableSession(AUTH_URL, TEST_SESSION, AUTH_TOKEN);
    // Attempt WS without token — should fail to connect
    const { ws, opened, error, closed } = await wsConnect(
      `ws://127.0.0.1:${AUTH_PORT}/ws?session=${TEST_SESSION}&cols=80&rows=24`
    );
    expect(opened).toBe(false);
    // Connection should have been rejected (closed or errored)
    if (ws.readyState === ws.OPEN) ws.close();
  });

  test("WS upgrade succeeds with valid token", async () => {
    await enableSession(AUTH_URL, TEST_SESSION, AUTH_TOKEN);
    const { ws, opened } = await wsConnect(
      `ws://127.0.0.1:${AUTH_PORT}/ws?session=${TEST_SESSION}&cols=80&rows=24&token=${AUTH_TOKEN}`
    );
    expect(opened).toBe(true);
    ws.close();
  });
});

// ── Tests: Grid TTL expiry ─────────────────────────────────────────────────

describe("grid TTL expiry", () => {
  test("grid session disappears after TTL", async () => {
    await enableSession(BASE_URL, TEST_SESSION);

    // Create grid with 1-second TTL
    const create = await fetch(`${BASE_URL}/api/grid-session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug: "ttl-test-grid", sessions: [TEST_SESSION], ttl: 1 }),
    });
    expect(create.status).toBe(200);

    // Immediately accessible
    const before = await fetch(`${BASE_URL}/grid/ttl-test-grid`);
    expect(before.status).toBe(200);

    // Wait for expiry (1s TTL + margin)
    await Bun.sleep(1500);

    // Should be gone
    const after = await fetch(`${BASE_URL}/grid/ttl-test-grid`);
    expect(after.status).toBe(404);
  });
});

// ── Tests: Method validation ────────────────────────────────────────────────

describe("method validation", () => {
  test("GET /api/pilot returns 405", async () => {
    const r = await fetch(`${BASE_URL}/api/pilot?session=${TEST_SESSION}`);
    expect(r.status).toBe(405);
  });

  test("unknown path returns 404", async () => {
    const r = await fetch(`${BASE_URL}/nonexistent/path`);
    expect(r.status).toBe(404);
  });
});
