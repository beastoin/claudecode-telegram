#!/usr/bin/env bun
/**
 * Spawn-on-demand PR review server — Bun + token auth + TTL self-destruct.
 *
 * Serves the generated HTML page and proxies GitHub API calls (comments,
 * merge, file content) via the `gh` CLI.
 *
 * Usage:
 *   bun serve.ts --port 10172 --token <secret> --ttl 300 \
 *     --html-path /tmp/pr-review-123.html \
 *     --owner BasedHardware --repo omi --pr-num 123
 */

import { spawn } from "bun";
import { readFileSync, existsSync } from "fs";

// ---------------------------------------------------------------------------
// CLI arg parsing
// ---------------------------------------------------------------------------

interface Args {
  port: number;
  token: string;
  ttl: number;
  htmlPath: string;
  owner: string;
  repo: string;
  prNum: number;
}

function parseArgs(argv: string[]): Args {
  const args: Record<string, string> = {};
  for (let i = 2; i < argv.length; i++) {
    const key = argv[i];
    if (key.startsWith("--") && i + 1 < argv.length && !argv[i + 1].startsWith("--")) {
      args[key.slice(2)] = argv[++i];
    }
  }
  const port = parseInt(args["port"] || "0", 10);
  const token = args["token"] || "";
  const ttl = parseInt(args["ttl"] || "300", 10);
  const htmlPath = args["html-path"] || "";
  const owner = args["owner"] || "";
  const repo = args["repo"] || "";
  const prNum = parseInt(args["pr-num"] || "0", 10);

  if (!port || !token || !htmlPath || !owner || !repo || !prNum) {
    console.error("Missing required args: --port, --token, --html-path, --owner, --repo, --pr-num");
    process.exit(1);
  }
  return { port, token, ttl, htmlPath, owner, repo, prNum };
}

const CLI = parseArgs(process.argv);

// ---------------------------------------------------------------------------
// Idle TTL timer
// ---------------------------------------------------------------------------

let _idleTimer: ReturnType<typeof setTimeout> | null = null;

function resetIdleTimer(): void {
  if (CLI.ttl <= 0) return;
  if (_idleTimer) clearTimeout(_idleTimer);
  _idleTimer = setTimeout(() => {
    console.log(`[review] TTL expired after ${CLI.ttl}s of inactivity — shutting down`);
    process.exit(0);
  }, CLI.ttl * 1000);
}

// ---------------------------------------------------------------------------
// GitHub API helpers
// ---------------------------------------------------------------------------

async function ghApiCall(
  args: string[],
  opts?: { input?: string; timeout?: number }
): Promise<{ ok: boolean; output: string }> {
  const timeout = opts?.timeout ?? 60000;
  try {
    const proc = spawn(["gh", "api", ...args], {
      stdin: opts?.input ? new Blob([opts.input]) : "ignore",
      stdout: "pipe",
      stderr: "pipe",
    });
    const stdout = await new Response(proc.stdout).text();
    const stderr = await new Response(proc.stderr).text();
    const code = await proc.exited;
    if (code !== 0) {
      return { ok: false, output: stderr.trim() || stdout.trim() };
    }
    return { ok: true, output: stdout };
  } catch (e: any) {
    return { ok: false, output: e?.message || "unknown error" };
  }
}

// ---------------------------------------------------------------------------
// Notification helpers
// ---------------------------------------------------------------------------

function notifyBridge(text: string): void {
  const bridgeUrl = process.env.BRIDGE_URL;
  if (!bridgeUrl) return;
  fetch(`${bridgeUrl}/notifications`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
    signal: AbortSignal.timeout(5000),
  }).catch(() => {});
}

// ---------------------------------------------------------------------------
// Request handlers
// ---------------------------------------------------------------------------

function checkToken(url: URL): boolean {
  return url.searchParams.get("token") === CLI.token;
}

function json(status: number, data: unknown): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function text(status: number, body: string): Response {
  return new Response(body, { status, headers: { "Content-Type": "text/plain" } });
}

async function handleFileContent(url: URL): Promise<Response> {
  const owner = url.searchParams.get("owner") || CLI.owner;
  const repo = url.searchParams.get("repo") || CLI.repo;
  const path = url.searchParams.get("path");
  const ref = url.searchParams.get("ref");
  if (!path || !ref) return text(400, "Missing path or ref");

  const { ok, output } = await ghApiCall([
    `repos/${owner}/${repo}/contents/${path}?ref=${ref}`,
    "--jq", ".content",
  ]);
  if (!ok) return text(output === "timeout" ? 504 : 404, output);

  try {
    const raw = atob(output.trim());
    return json(200, raw.split("\n"));
  } catch {
    return text(500, "Failed to decode file content");
  }
}

async function handleComment(data: any): Promise<Response> {
  const owner = data.owner || CLI.owner;
  const repo = data.repo || CLI.repo;
  const prNum = data.pr_num || CLI.prNum;
  const body = (data.body || "").trim();
  const path = data.path || "";
  const line = data.line || 0;

  if (path && line) {
    const side = data.side || "RIGHT";
    const headSha = data.head_sha || "";
    if (!body || !headSha) return text(400, "Missing required fields");

    const ghPayload = JSON.stringify({
      body, commit_id: headSha, path, line, side,
    });
    const { ok, output } = await ghApiCall(
      [`repos/${owner}/${repo}/pulls/${prNum}/comments`, "--method", "POST", "--input", "-"],
      { input: ghPayload }
    );
    if (!ok) return text(output === "timeout" ? 504 : 502, `GitHub API error: ${output}`);
    notifyBridge(`💬 PR #${prNum} comment\n${path}:${line}\n\n${body}`);
  } else {
    if (!body) return text(400, "Missing required fields");
    const { ok, output } = await ghApiCall([
      `repos/${owner}/${repo}/issues/${prNum}/comments`,
      "--method", "POST", "-f", `body=${body}`,
    ]);
    if (!ok) return text(output === "timeout" ? 504 : 502, `GitHub API error: ${output.slice(0, 200)}`);
    notifyBridge(`💬 PR #${prNum} comment:\n${body.slice(0, 500)}`);
  }
  return json(200, { ok: true });
}

async function handleMerge(data: any): Promise<Response> {
  const owner = data.owner || CLI.owner;
  const repo = data.repo || CLI.repo;
  const prNum = data.pr_num || CLI.prNum;
  let method = data.merge_method || "merge";
  if (!["merge", "squash", "rebase"].includes(method)) method = "merge";

  const { ok, output } = await ghApiCall(
    [`repos/${owner}/${repo}/pulls/${prNum}/merge`, "--method", "PUT", "-f", `merge_method=${method}`],
    { timeout: 120000 }
  );
  if (!ok) return text(output === "timeout" ? 504 : 502, `Merge failed: ${output.slice(0, 300)}`);
  notifyBridge(`✅ PR #${prNum} merged (${method}) via review page`);
  return json(200, { ok: true });
}

// ---------------------------------------------------------------------------
// HTML cache
// ---------------------------------------------------------------------------

let _htmlCache: string | null = null;

function getHtml(): string | null {
  if (_htmlCache) return _htmlCache;
  if (!existsSync(CLI.htmlPath)) return null;
  _htmlCache = readFileSync(CLI.htmlPath, "utf-8");
  return _htmlCache;
}

// ---------------------------------------------------------------------------
// Server
// ---------------------------------------------------------------------------

const server = Bun.serve({
  port: CLI.port,
  async fetch(req) {
    const url = new URL(req.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    // Health check — no auth
    if (path === "/health") {
      return json(200, { ok: true });
    }

    // POST endpoints
    if (req.method === "POST") {
      let data: any;
      try {
        data = await req.json();
      } catch {
        return text(400, "Invalid JSON");
      }
      const token = data.token || url.searchParams.get("token");
      if (token !== CLI.token) return text(403, "Token expired");
      resetIdleTimer();

      if (path === "/tools/review/comments") return handleComment(data);
      if (path === "/tools/review/merges") return handleMerge(data);
      return text(404, "Not found");
    }

    // GET endpoints — all require token
    if (!checkToken(url)) return text(403, "Invalid or expired token");
    resetIdleTimer();

    if (path === "/tools/review/files") return handleFileContent(url);

    // Serve HTML for root or /tools/review/*
    if (path === "/" || path.startsWith("/tools/review")) {
      const html = getHtml();
      if (!html) return text(404, "Review HTML not found");
      return new Response(html, {
        headers: { "Content-Type": "text/html; charset=utf-8" },
      });
    }

    return text(404, "Not found");
  },
});

resetIdleTimer();
console.log(
  `[review] listening on http://localhost:${server.port} ` +
  `(TTL: ${CLI.ttl}s, PR: ${CLI.owner}/${CLI.repo}#${CLI.prNum})`
);
