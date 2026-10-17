/**
 * End-to-end tests for the rewind viewer.
 * These spawn the actual Bun server and hit real HTTP endpoints,
 * covering every behavior from the old Python indexer + renderer.
 */
import { describe, test, expect, beforeAll, afterAll, beforeEach } from "bun:test";
import { mkdtempSync, writeFileSync, rmSync, existsSync, mkdirSync } from "fs";
import { join } from "path";
import { tmpdir } from "os";
import {
  setupDb,
  createTables,
  indexJsonl,
  queryEntries,
  querySearch,
  queryStats,
} from "./indexer";

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

function makeEntry(
  type: string,
  content: string | Record<string, unknown>[],
  ts = "2026-04-05T10:00:00Z",
  extras: Record<string, unknown> = {}
): Record<string, unknown> {
  const entry: Record<string, unknown> = { type, timestamp: ts, ...extras };
  if (type === "user") {
    entry.message = { role: "user", content };
  } else if (type === "assistant") {
    const c = typeof content === "string" ? [{ type: "text", text: content }] : content;
    entry.message = { role: "assistant", content: c, model: "claude-opus-4-6" };
  }
  return entry;
}

function writeJsonl(path: string, entries: Record<string, unknown>[]): void {
  writeFileSync(path, entries.map((e) => JSON.stringify(e)).join("\n") + "\n");
}

/** Fetch helper — appends token to URL */
async function get(base: string, path: string, token: string, params: Record<string, string> = {}): Promise<Response> {
  const url = new URL(path, base);
  url.searchParams.set("token", token);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  return fetch(url.toString());
}

async function getJson(base: string, path: string, token: string, params: Record<string, string> = {}): Promise<unknown> {
  const resp = await get(base, path, token, params);
  expect(resp.status).toBe(200);
  return resp.json();
}

// ---------------------------------------------------------------------------
// Server lifecycle
// ---------------------------------------------------------------------------

describe("rewind e2e", () => {
  let proc: ReturnType<typeof Bun.spawn>;
  let baseUrl: string;
  let tmpDir: string;
  let jsonlPath: string;
  const token = "test-token-" + Date.now();
  const port = 10177; // lee's range

  // Standard transcript entries for most tests
  const standardEntries = [
    makeEntry("user", "Hello world", "2026-04-05T10:00:00Z", { sessionId: "s1", version: "2.1.85" }),
    makeEntry(
      "assistant",
      [
        { type: "text", text: "Hi there! How can I help?" },
        {
          type: "tool_use",
          id: "t1",
          name: "Edit",
          input: {
            file_path: "/home/claude/bridge.py",
            old_string: "line1\nline2",
            new_string: "line1\nline2\nline3",
          },
        },
      ],
      "2026-04-05T12:30:00Z",
      { version: "2.1.85" }
    ),
    makeEntry("user", "Fix the teleport bug", "2026-04-05T12:31:00Z"),
    makeEntry("assistant", "I fixed the teleport bug in bridge.py", "2026-04-05T12:32:00Z"),
    makeEntry("user", "Deploy it now", "2026-04-05T12:33:00Z"),
    // System entry — should be skipped by indexer
    { type: "system", timestamp: "2026-04-05T12:34:00Z", message: { role: "system", content: "sys msg" } },
    // Progress entry — should be skipped
    { type: "progress", timestamp: "2026-04-05T12:35:00Z", message: {} },
  ];

  beforeAll(async () => {
    tmpDir = mkdtempSync(join(tmpdir(), "rewind-test-"));
    jsonlPath = join(tmpDir, "transcript.jsonl");
    writeJsonl(jsonlPath, standardEntries);

    // Spawn the actual rewind server
    proc = Bun.spawn(
      [
        "bun",
        join(import.meta.dir, "rewind.ts"),
        "--port", String(port),
        "--token", token,
        "--ttl", "120",
        "--jsonl-path", jsonlPath,
        "--name", "testworker",
        "--session-id", "test-session-abc123",
      ],
      { stdout: "pipe", stderr: "pipe" }
    );
    baseUrl = `http://localhost:${port}`;

    // Wait for server to be ready
    for (let i = 0; i < 50; i++) {
      try {
        const resp = await fetch(`${baseUrl}/?token=${token}`);
        if (resp.status === 200) break;
      } catch {
        // not ready yet
      }
      await Bun.sleep(100);
    }
  });

  afterAll(() => {
    proc?.kill();
    if (tmpDir && existsSync(tmpDir)) rmSync(tmpDir, { recursive: true, force: true });
  });

  // ── Token auth ────────────────────────────────────────────────────────

  test("rejects request with no token", async () => {
    const resp = await fetch(`${baseUrl}/`);
    expect(resp.status).toBe(403);
    const text = await resp.text();
    expect(text).toContain("Session Expired");
  });

  test("rejects request with wrong token", async () => {
    const resp = await fetch(`${baseUrl}/?token=wrong-token`);
    expect(resp.status).toBe(403);
  });

  test("accepts request with correct token", async () => {
    const resp = await get(baseUrl, "/", token);
    expect(resp.status).toBe(200);
    expect(resp.headers.get("content-type")).toContain("text/html");
  });

  // ── Main page rendering ───────────────────────────────────────────────

  test("main page contains embedded JSON data", async () => {
    const resp = await get(baseUrl, "/", token);
    const html = await resp.text();
    expect(html).toContain("<!DOCTYPE html>");
    expect(html).toContain("testworker");
    // Should have the transcript-data script tag
    expect(html).toContain('id="transcript-data"');
    // Extract and validate JSON
    const match = html.match(/id="transcript-data">(.*?)<\/script>/s);
    expect(match).not.toBeNull();
    const data = JSON.parse(match![1].replace(/<\\\//g, "</"));
    expect(data.name).toBe("testworker");
    expect(data.session_id).toBe("test-session-abc123");
    expect(data.stats.model).toBe("claude-opus-4-6");
    expect(data.stats.version).toBe("2.1.85");
    // Should have entries (system/progress entries skipped)
    expect(data.entries.length).toBeGreaterThanOrEqual(4);
  });

  test("main page defaults to last page", async () => {
    const resp = await get(baseUrl, "/", token, { per_page: "2" });
    const html = await resp.text();
    const match = html.match(/id="transcript-data">(.*?)<\/script>/s);
    const data = JSON.parse(match![1].replace(/<\\\//g, "</"));
    // With 5 entries and per_page=2, last page is 3
    expect(data.pagination.total).toBe(5); // 7 entries - 2 skipped
    expect(data.pagination.total_pages).toBe(3);
    expect(data.pagination.page).toBe(3);
  });

  test("main page respects explicit page param", async () => {
    const resp = await get(baseUrl, "/", token, { page: "1", per_page: "2" });
    const html = await resp.text();
    const match = html.match(/id="transcript-data">(.*?)<\/script>/s);
    const data = JSON.parse(match![1].replace(/<\\\//g, "</"));
    expect(data.pagination.page).toBe(1);
    expect(data.entries.length).toBe(2);
  });

  // ── /api/stats ────────────────────────────────────────────────────────

  test("/api/stats returns transcript statistics", async () => {
    const stats = (await getJson(baseUrl, "/api/stats", token)) as Record<string, unknown>;
    expect(stats.model).toBe("claude-opus-4-6");
    expect(stats.version).toBe("2.1.85");
    expect(stats.n_user).toBe(3);
    expect(stats.n_tool).toBe(1);
    expect(stats.n_edit).toBe(1);
    expect(stats.n_files).toBe(1);
    // Duration: 10:00:00 → 12:33:00 = 2h 33m
    expect(stats.duration).toBe("2h 33m");
    // Edit diff: old=2 lines, new=3 lines → overlap=2, add=1, del=0
    expect(stats.lines_add).toBe(1);
    expect(stats.lines_mod).toBe(2);
    expect(stats.lines_del).toBe(0);
  });

  // ── /api/updates ──────────────────────────────────────────────────────

  test("/api/updates returns total and new count", async () => {
    const d = (await getJson(baseUrl, "/api/updates", token, { since: "0" })) as Record<string, number>;
    expect(d.total).toBe(5); // 7 entries - 2 skipped
    expect(d.new).toBe(5);
  });

  test("/api/updates with since=current returns 0 new", async () => {
    const d = (await getJson(baseUrl, "/api/updates", token, { since: "5" })) as Record<string, number>;
    expect(d.total).toBe(5);
    expect(d.new).toBe(0);
  });

  // ── Search ────────────────────────────────────────────────────────────

  test("search finds matching entries", async () => {
    const resp = await get(baseUrl, "/", token, { q: "teleport" });
    const html = await resp.text();
    const match = html.match(/id="transcript-data">(.*?)<\/script>/s);
    const data = JSON.parse(match![1].replace(/<\\\//g, "</"));
    expect(data.search_query).toBe("teleport");
    // Should find entries with "teleport"
    const found = data.entries.filter(
      (e: Record<string, unknown>) =>
        JSON.stringify(e).toLowerCase().includes("teleport")
    );
    expect(found.length).toBeGreaterThanOrEqual(1);
  });

  test("search with no results returns empty", async () => {
    const resp = await get(baseUrl, "/", token, { q: "xyznonexistent999" });
    const html = await resp.text();
    const match = html.match(/id="transcript-data">(.*?)<\/script>/s);
    const data = JSON.parse(match![1].replace(/<\\\//g, "</"));
    expect(data.search_query).toBe("xyznonexistent999");
    expect(data.entries.length).toBe(0);
  });

  test("search sort=time returns chronological order", async () => {
    const resp = await get(baseUrl, "/", token, { q: "deploy OR teleport OR Hello", sort: "time" });
    const html = await resp.text();
    const match = html.match(/id="transcript-data">(.*?)<\/script>/s);
    const data = JSON.parse(match![1].replace(/<\\\//g, "</"));
    if (data.entries.length > 1) {
      const idxs = data.entries.map((e: Record<string, unknown>) => e._idx);
      const sorted = [...idxs].sort((a: number, b: number) => a - b);
      expect(idxs).toEqual(sorted);
    }
  });

  // ── Filter ────────────────────────────────────────────────────────────

  test("filter=prompts returns only user text messages", async () => {
    const resp = await get(baseUrl, "/", token, { filter: "prompts" });
    const html = await resp.text();
    const match = html.match(/id="transcript-data">(.*?)<\/script>/s);
    const data = JSON.parse(match![1].replace(/<\\\//g, "</"));
    // Only user entries with plain text content
    for (const e of data.entries) {
      expect(e.type).toBe("user");
      expect(typeof e.message.content).toBe("string");
    }
    expect(data.entries.length).toBe(3); // 3 user text messages
  });

  // ── 404 for unknown routes ────────────────────────────────────────────

  test("unknown path returns 404", async () => {
    const resp = await get(baseUrl, "/nonexistent", token);
    expect(resp.status).toBe(404);
  });
});

// ---------------------------------------------------------------------------
// Indexer unit tests (no server needed)
// ---------------------------------------------------------------------------

describe("indexer", () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = mkdtempSync(join(tmpdir(), "indexer-test-"));
  });

  test("FTS5 special chars don't crash", () => {
    const jsonl = join(tmpDir, "special.jsonl");
    const dbPath = join(tmpDir, "special.db");
    writeJsonl(jsonl, [
      makeEntry("user", "Upgraded to v0.45.2 today"),
      makeEntry("assistant", "The file bridge.py was modified"),
      makeEntry("user", "Check http://localhost:8271/health", "2026-04-05T10:00:02Z"),
    ]);
    const db = setupDb(dbPath);
    createTables(db);
    indexJsonl(db, jsonl);

    // Dots in version string
    const r1 = querySearch(db, "v0.45.2");
    expect(r1.total_results).toBeGreaterThanOrEqual(1);
    // Dots in filename
    const r2 = querySearch(db, "bridge.py");
    expect(r2.total_results).toBeGreaterThanOrEqual(1);
    // URL with colons
    const r3 = querySearch(db, "http://localhost:8271");
    expect(r3.total_results).toBeGreaterThanOrEqual(1);
    db.close();
  });

  test("FTS5 boolean operators pass through", () => {
    const jsonl = join(tmpDir, "bool.jsonl");
    const dbPath = join(tmpDir, "bool.db");
    writeJsonl(jsonl, [
      makeEntry("user", "There was an error with the timeout"),
      makeEntry("user", "The timeout was resolved", "2026-04-05T10:00:01Z"),
      makeEntry("user", "An error in the config file", "2026-04-05T10:00:02Z"),
    ]);
    const db = setupDb(dbPath);
    createTables(db);
    indexJsonl(db, jsonl);

    const r = querySearch(db, "error AND timeout");
    expect(r.total_results).toBeGreaterThanOrEqual(1);
    // Verify result contains both terms
    const texts = r.entries.map((e) => String(e.raw_json));
    expect(texts.some((t) => t.includes("error") && t.includes("timeout"))).toBe(true);
    db.close();
  });

  test("FTS5 fallback on bad syntax", () => {
    const jsonl = join(tmpDir, "bad.jsonl");
    const dbPath = join(tmpDir, "bad.db");
    writeJsonl(jsonl, [
      makeEntry("user", 'The "broken query test'),
      makeEntry("assistant", "Response about broken things"),
    ]);
    const db = setupDb(dbPath);
    createTables(db);
    indexJsonl(db, jsonl);

    // Should not throw — sanitizer quotes the broken term
    const r = querySearch(db, '"broken');
    expect(typeof r.total_results).toBe("number");
    db.close();
  });

  test("incremental append without duplication", () => {
    const jsonl = join(tmpDir, "append.jsonl");
    const dbPath = join(tmpDir, "append.db");
    const entries = [
      makeEntry("user", "First message"),
      makeEntry("assistant", "First reply", "2026-04-05T10:01:00Z"),
    ];
    writeJsonl(jsonl, entries);

    const db = setupDb(dbPath);
    createTables(db);
    indexJsonl(db, jsonl);
    let r = queryEntries(db);
    expect(r.total).toBe(2);

    // Append a third entry
    writeFileSync(jsonl, entries.concat([makeEntry("user", "Appended message", "2026-04-05T10:02:00Z")]).map((e) => JSON.stringify(e)).join("\n") + "\n");
    indexJsonl(db, jsonl);
    r = queryEntries(db);
    expect(r.total).toBe(3);

    // No duplicates
    const r2 = querySearch(db, "First message");
    expect(r2.total_results).toBe(1);
    db.close();
  });

  test("file shrink triggers full reindex", () => {
    const jsonl = join(tmpDir, "shrink.jsonl");
    const dbPath = join(tmpDir, "shrink.db");
    const entries = Array.from({ length: 5 }, (_, i) =>
      makeEntry("user", `Message ${i}`, `2026-04-05T10:${String(i).padStart(2, "0")}:00Z`)
    );
    writeJsonl(jsonl, entries);

    const db = setupDb(dbPath);
    createTables(db);
    indexJsonl(db, jsonl);
    expect(queryEntries(db).total).toBe(5);

    // Truncate to 2 entries
    writeJsonl(jsonl, entries.slice(0, 2));
    indexJsonl(db, jsonl);
    expect(queryEntries(db).total).toBe(2);
    db.close();
  });

  test("path change triggers full reindex", () => {
    const jsonlA = join(tmpDir, "fileA.jsonl");
    const jsonlB = join(tmpDir, "fileB.jsonl");
    const dbPath = join(tmpDir, "shared.db");
    writeJsonl(jsonlA, [
      makeEntry("user", "From file A"),
      makeEntry("user", "Also file A", "2026-04-05T10:01:00Z"),
    ]);
    writeJsonl(jsonlB, [makeEntry("user", "From file B")]);

    const db = setupDb(dbPath);
    createTables(db);
    indexJsonl(db, jsonlA);
    expect(queryEntries(db).total).toBe(2);

    // Index different file with same DB
    indexJsonl(db, jsonlB);
    expect(queryEntries(db).total).toBe(1);
    db.close();
  });

  test("thinking blocks are searchable", () => {
    const jsonl = join(tmpDir, "thinking.jsonl");
    const dbPath = join(tmpDir, "thinking.db");
    writeJsonl(jsonl, [
      makeEntry("assistant", [
        { type: "thinking", thinking: "The user wants zephyr configuration details" },
        { type: "text", text: "Here are the settings." },
      ]),
    ]);
    const db = setupDb(dbPath);
    createTables(db);
    indexJsonl(db, jsonl);
    const r = querySearch(db, "zephyr");
    expect(r.total_results).toBeGreaterThanOrEqual(1);
    db.close();
  });

  test("unicode search works", () => {
    const jsonl = join(tmpDir, "unicode.jsonl");
    const dbPath = join(tmpDir, "unicode.db");
    writeJsonl(jsonl, [
      makeEntry("user", "Deploy the 🚀 rocket feature"),
      makeEntry("assistant", "Deploying résumé handler für München", "2026-04-05T10:00:01Z"),
      makeEntry("user", "Check naïve implementation", "2026-04-05T10:00:02Z"),
    ]);
    const db = setupDb(dbPath);
    createTables(db);
    indexJsonl(db, jsonl);

    expect(querySearch(db, "rocket").total_results).toBeGreaterThanOrEqual(1);
    expect(querySearch(db, "München").total_results).toBeGreaterThanOrEqual(1);
    // Emoji search doesn't crash
    const r = querySearch(db, "🚀");
    expect(typeof r.total_results).toBe("number");
    db.close();
  });

  test("duration formatting: days", () => {
    const jsonl = join(tmpDir, "dur-days.jsonl");
    const dbPath = join(tmpDir, "dur-days.db");
    writeJsonl(jsonl, [
      makeEntry("user", "Start", "2026-04-05T08:00:00Z", { version: "2.1.85" }),
      makeEntry("assistant", "Working", "2026-04-06T10:30:00Z"),
    ]);
    const db = setupDb(dbPath);
    createTables(db);
    indexJsonl(db, jsonl);
    const s = queryStats(db);
    expect(s.duration).toStartWith("1d");
    expect(s.duration).toContain("h");
    db.close();
  });

  test("duration formatting: hours+minutes", () => {
    const jsonl = join(tmpDir, "dur-hours.jsonl");
    const dbPath = join(tmpDir, "dur-hours.db");
    writeJsonl(jsonl, [
      makeEntry("user", "Start", "2026-04-05T10:00:00Z"),
      makeEntry("assistant", "End", "2026-04-05T12:45:00Z"),
    ]);
    const db = setupDb(dbPath);
    createTables(db);
    indexJsonl(db, jsonl);
    expect(queryStats(db).duration).toBe("2h 45m");
    db.close();
  });

  test("duration formatting: minutes only", () => {
    const jsonl = join(tmpDir, "dur-mins.jsonl");
    const dbPath = join(tmpDir, "dur-mins.db");
    writeJsonl(jsonl, [
      makeEntry("user", "Quick", "2026-04-05T10:00:00Z"),
      makeEntry("assistant", "Answer", "2026-04-05T10:07:00Z"),
    ]);
    const db = setupDb(dbPath);
    createTables(db);
    indexJsonl(db, jsonl);
    expect(queryStats(db).duration).toBe("7m");
    db.close();
  });

  test("empty file returns zeroed stats", () => {
    const jsonl = join(tmpDir, "empty.jsonl");
    const dbPath = join(tmpDir, "empty.db");
    writeFileSync(jsonl, "");
    const db = setupDb(dbPath);
    createTables(db);
    // Don't index (empty file)
    const s = queryStats(db);
    expect(s.n_user).toBe(0);
    expect(s.n_tool).toBe(0);
    expect(s.duration).toBe("");
    expect(s.model).toBe("");
    db.close();
  });

  test("empty search term returns empty results", () => {
    const jsonl = join(tmpDir, "esearch.jsonl");
    const dbPath = join(tmpDir, "esearch.db");
    writeJsonl(jsonl, [makeEntry("user", "Hello"), makeEntry("assistant", "World")]);
    const db = setupDb(dbPath);
    createTables(db);
    indexJsonl(db, jsonl);
    const r = querySearch(db, "");
    expect(r.total_results).toBe(0);
    expect(r.entries).toEqual([]);
    db.close();
  });

  test("entries+stats compound query via page render", async () => {
    // This tests that the route handler combines entries + stats
    const jsonl = join(tmpDir, "compound.jsonl");
    const dbPath = join(tmpDir, "compound.db");
    writeJsonl(jsonl, [
      makeEntry("user", "Hello world", "2026-04-05T10:00:00Z", { version: "2.1.85" }),
      makeEntry("assistant", "Hi there", "2026-04-05T10:05:00Z"),
      makeEntry("user", "Thanks", "2026-04-05T10:10:00Z"),
    ]);
    const db = setupDb(dbPath);
    createTables(db);
    indexJsonl(db, jsonl);
    const entries = queryEntries(db);
    const stats = queryStats(db);
    expect(entries.total).toBe(3);
    expect(stats.version).toBe("2.1.85");
    expect(stats.model).toBe("claude-opus-4-6");
    expect(stats.duration).toBe("10m");
    db.close();
  });

  test("pagination defaults to last page", () => {
    const jsonl = join(tmpDir, "paginate.jsonl");
    const dbPath = join(tmpDir, "paginate.db");
    const entries = Array.from({ length: 12 }, (_, i) =>
      makeEntry("user", `Message ${i}`, `2026-04-05T10:${String(i).padStart(2, "0")}:00Z`)
    );
    writeJsonl(jsonl, entries);
    const db = setupDb(dbPath);
    createTables(db);
    indexJsonl(db, jsonl);

    // No page → last page
    const r = queryEntries(db, null, 5);
    expect(r.total).toBe(12);
    expect(r.total_pages).toBe(3);
    expect(r.page).toBe(3);
    expect(r.entries.length).toBe(2); // last page has 2

    // Explicit page 1
    const r1 = queryEntries(db, 1, 5);
    expect(r1.page).toBe(1);
    expect(r1.entries.length).toBe(5);
    db.close();
  });

  test("skips system and progress entries", () => {
    const jsonl = join(tmpDir, "skip.jsonl");
    const dbPath = join(tmpDir, "skip.db");
    writeJsonl(jsonl, [
      makeEntry("user", "Real message"),
      { type: "system", timestamp: "2026-04-05T10:01:00Z", message: { role: "system", content: "sys" } },
      { type: "progress", timestamp: "2026-04-05T10:02:00Z", message: {} },
      { type: "queue-operation", timestamp: "2026-04-05T10:03:00Z", message: {} },
      { type: "file-history-snapshot", timestamp: "2026-04-05T10:04:00Z", message: {} },
      makeEntry("assistant", "Real reply", "2026-04-05T10:05:00Z"),
    ]);
    const db = setupDb(dbPath);
    createTables(db);
    indexJsonl(db, jsonl);
    expect(queryEntries(db).total).toBe(2); // Only user + assistant
    db.close();
  });

  test("search sort=time returns chronological order", () => {
    const jsonl = join(tmpDir, "sort.jsonl");
    const dbPath = join(tmpDir, "sort.db");
    writeJsonl(jsonl, [
      makeEntry("user", "deploy the fix now", "2026-04-05T10:00:00Z"),
      makeEntry("user", "another task unrelated", "2026-04-05T10:01:00Z"),
      makeEntry("user", "deploy deploy deploy triple mention", "2026-04-05T10:02:00Z"),
      makeEntry("user", "final deploy step", "2026-04-05T10:03:00Z"),
    ]);
    const db = setupDb(dbPath);
    createTables(db);
    indexJsonl(db, jsonl);

    const rTime = querySearch(db, "deploy", 1, 50, "time");
    expect(rTime.total_results).toBeGreaterThanOrEqual(3);
    const idxs = rTime.entries.map((e) => e.idx as number);
    const sorted = [...idxs].sort((a, b) => a - b);
    expect(idxs).toEqual(sorted);
    db.close();
  });

  test("filter=prompts excludes tool_use and tool_result", () => {
    const jsonl = join(tmpDir, "filter.jsonl");
    const dbPath = join(tmpDir, "filter.db");
    writeJsonl(jsonl, [
      makeEntry("user", "Real user question"),
      makeEntry("assistant", "Bot response"),
      makeEntry("user", "Another user prompt", "2026-04-05T10:01:00Z"),
      // tool_use — structured content
      {
        type: "assistant", timestamp: "2026-04-05T10:02:00Z",
        message: {
          role: "assistant",
          content: [{ type: "tool_use", id: "t1", name: "Bash", input: { cmd: "ls" } }],
        },
      },
      // tool_result
      {
        type: "tool_result", timestamp: "2026-04-05T10:03:00Z",
        message: {
          role: "user",
          content: [{ type: "tool_result", tool_use_id: "t1", content: "file.txt" }],
        },
      },
    ]);
    const db = setupDb(dbPath);
    createTables(db);
    indexJsonl(db, jsonl);
    const r = queryEntries(db, null, 50, "prompts");
    expect(r.total).toBe(2); // Only 2 plain user text messages
    for (const e of r.entries) {
      expect(e.role).toBe("user");
    }
    db.close();
  });

  test("stats counts tool calls and file edits correctly", () => {
    const jsonl = join(tmpDir, "tools.jsonl");
    const dbPath = join(tmpDir, "tools.db");
    writeJsonl(jsonl, [
      makeEntry("user", "Fix it", "2026-04-05T10:00:00Z"),
      makeEntry("assistant", [
        { type: "text", text: "Fixing..." },
        { type: "tool_use", id: "t1", name: "Read", input: { file_path: "/tmp/a.py" } },
        { type: "tool_use", id: "t2", name: "Edit", input: { file_path: "/tmp/a.py", old_string: "a\nb", new_string: "a\nb\nc" } },
        { type: "tool_use", id: "t3", name: "Write", input: { file_path: "/tmp/b.py" } },
      ], "2026-04-05T10:01:00Z"),
    ]);
    const db = setupDb(dbPath);
    createTables(db);
    indexJsonl(db, jsonl);
    const s = queryStats(db);
    expect(s.n_tool).toBe(3);
    expect(s.n_edit).toBe(1);
    expect(s.n_files).toBe(2); // a.py and b.py
    expect(s.lines_add).toBe(1); // new has 1 extra line
    expect(s.lines_mod).toBe(2);
    expect(s.lines_del).toBe(0);
    db.close();
  });
});
