/**
 * Rewind viewer HTTP routes.
 * Serves transcript HTML page and API endpoints (entries, search, stats, updates).
 */
import { Database } from "bun:sqlite";
import { readFileSync, existsSync, statSync } from "fs";
import { resolve, join, dirname } from "path";
import { jsonResponse, htmlResponse, errorPage } from "./server";
import {
  setupDb,
  createTables,
  indexJsonl,
  queryEntries,
  querySearch,
  queryStats,
  type EntriesResult,
  type SearchResult,
  type TranscriptStats,
} from "./indexer";

interface RewindConfig {
  jsonlPath: string;
  name: string;
  sessionId: string;
  token: string;
}

/** Load and cache static assets at startup */
function loadAssets(): {
  css: string;
  js: string;
  pageTpl: string;
  icons: Record<string, unknown>;
} {
  const assetsDir = join(dirname(dirname(resolve(import.meta.path))), "assets");
  return {
    css: readFileSync(join(assetsDir, "transcript.css"), "utf-8"),
    js: readFileSync(join(assetsDir, "transcript.js"), "utf-8"),
    pageTpl: readFileSync(join(assetsDir, "transcript_page.html"), "utf-8"),
    icons: JSON.parse(readFileSync(join(assetsDir, "transcript_icons.json"), "utf-8")),
  };
}

/** Format file size in human-readable form */
function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Build full transcript HTML page with embedded JSON data */
function renderPage(
  config: RewindConfig,
  assets: ReturnType<typeof loadAssets>,
  db: Database,
  params: {
    page: number | null;
    perPage: number;
    searchQuery: string;
    searchSort: string;
    filterMode: string;
  }
): string {
  // Index if needed
  if (existsSync(config.jsonlPath) && statSync(config.jsonlPath).size > 0) {
    indexJsonl(db, config.jsonlPath);
  }

  let queryResult: EntriesResult | SearchResult;
  let statsResult: TranscriptStats;

  if (params.searchQuery) {
    queryResult = querySearch(
      db,
      params.searchQuery,
      params.page || 1,
      params.perPage,
      params.searchSort
    );
    statsResult = queryStats(db);
  } else {
    queryResult = queryEntries(db, params.page, params.perPage, params.filterMode);
    statsResult = queryStats(db);
  }

  // Parse raw_json for each entry
  const pageEntries: Record<string, unknown>[] = [];
  for (const e of queryResult.entries) {
    try {
      const parsed = JSON.parse(String(e.raw_json));
      parsed._idx = e.idx;
      pageEntries.push(parsed);
    } catch {
      continue;
    }
  }

  const total =
    "total_results" in queryResult && params.searchQuery
      ? (queryResult as SearchResult).total_results
      : queryResult.total;

  let fileSizeStr = "";
  try {
    if (existsSync(config.jsonlPath)) {
      fileSizeStr = formatFileSize(statSync(config.jsonlPath).size);
    }
  } catch {
    // ignore
  }

  const data = JSON.stringify(
    {
      name: config.name,
      session_id: config.sessionId,
      entries: pageEntries,
      stats: statsResult,
      pagination: {
        page: queryResult.page,
        total_pages: queryResult.total_pages,
        total,
        per_page: queryResult.per_page,
      },
      search_query: params.searchQuery,
      search_sort: params.searchSort,
      filter_mode: params.filterMode,
      token: config.token,
      live_base_url: "",
      file_size: fileSizeStr,
      icons: assets.icons,
    },
  ).replace(/<\//g, "<\\/");

  const title = `${config.name} — Transcript`;
  return assets.pageTpl
    .replace("__TITLE__", () => title)
    .replace("__CSS__", () => assets.css)
    .replace("__JS__", () => assets.js)
    .replace("__DATA__", () => data);
}

/** Create router for rewind viewer */
export function createRewindRoutes(config: RewindConfig) {
  const assets = loadAssets();

  // Setup DB
  const dbPath = `/tmp/transcript-cache/${config.sessionId}.db`;
  const db = setupDb(dbPath);
  createTables(db);

  return function route(url: URL, _req: Request): Response | null {
    const path = url.pathname;

    // GET /api/updates?since=N — live update polling
    if (path === "/api/updates") {
      const since = parseInt(url.searchParams.get("since") || "0") || 0;

      // Re-index to get fresh data
      if (existsSync(config.jsonlPath) && statSync(config.jsonlPath).size > 0) {
        indexJsonl(db, config.jsonlPath);
      }

      const totalRow = db
        .query("SELECT COUNT(*) as c FROM entries")
        .get() as { c: number };
      const total = totalRow.c;
      const newCount = Math.max(0, total - since);
      return jsonResponse({ total, new: newCount });
    }

    // GET /api/entries?page=1&per_page=50&filter=user — paginated transcript entries
    if (path === "/api/entries") {
      if (existsSync(config.jsonlPath) && statSync(config.jsonlPath).size > 0) {
        indexJsonl(db, config.jsonlPath);
      }
      const page = parseInt(url.searchParams.get("page") || "1", 10);
      const perPage = Math.min(500, Math.max(1, parseInt(url.searchParams.get("per_page") || "50", 10) || 50));
      const filterMode = (url.searchParams.get("filter") || "").trim();
      const result = queryEntries(db, Number.isFinite(page) ? Math.max(1, page) : 1, perPage, filterMode);
      const entries = result.entries.map((e) => {
        try { const p = JSON.parse(String(e.raw_json)); p._idx = e.idx; return p; } catch { return null; }
      }).filter(Boolean);
      return jsonResponse({
        entries,
        page: result.page,
        total_pages: result.total_pages,
        total: result.total,
        per_page: result.per_page,
      });
    }

    // GET /api/search?q=term&sort=relevance&page=1&per_page=50 — FTS5 search
    if (path === "/api/search") {
      if (existsSync(config.jsonlPath) && statSync(config.jsonlPath).size > 0) {
        indexJsonl(db, config.jsonlPath);
      }
      const q = (url.searchParams.get("q") || "").trim();
      if (!q) return jsonResponse({ error: "Missing ?q= parameter" }, 400);
      const page = parseInt(url.searchParams.get("page") || "1", 10);
      const perPage = Math.min(500, Math.max(1, parseInt(url.searchParams.get("per_page") || "50", 10) || 50));
      let sort = (url.searchParams.get("sort") || "relevance").trim();
      if (sort !== "relevance" && sort !== "time") sort = "relevance";
      const result = querySearch(db, q, Number.isFinite(page) ? Math.max(1, page) : 1, perPage, sort);
      const entries = result.entries.map((e) => {
        try { const p = JSON.parse(String(e.raw_json)); p._idx = e.idx; return p; } catch { return null; }
      }).filter(Boolean);
      return jsonResponse({
        entries,
        page: result.page,
        total_pages: result.total_pages,
        total_results: result.total_results,
        per_page: result.per_page,
        query: q,
        sort,
      });
    }

    // GET /api/stats — transcript stats
    if (path === "/api/stats") {
      if (existsSync(config.jsonlPath) && statSync(config.jsonlPath).size > 0) {
        indexJsonl(db, config.jsonlPath);
      }
      return jsonResponse(queryStats(db));
    }

    // GET / — main page
    if (path === "/" || path === "") {
      const page = url.searchParams.get("page");
      const perPage = Math.min(
        500,
        Math.max(1, parseInt(url.searchParams.get("per_page") || "50") || 50)
      );
      const searchQuery = (url.searchParams.get("q") || "").trim();
      let searchSort = (url.searchParams.get("sort") || "relevance").trim();
      if (searchSort !== "relevance" && searchSort !== "time") searchSort = "relevance";
      const filterMode = (url.searchParams.get("filter") || "").trim();

      try {
        const parsedPage = page ? parseInt(page, 10) : NaN;
        const html = renderPage(config, assets, db, {
          page: Number.isFinite(parsedPage) ? Math.max(1, parsedPage) : null,
          perPage,
          searchQuery,
          searchSort,
          filterMode,
        });
        return htmlResponse(html);
      } catch (err) {
        console.error("[rewind] Render error:", err);
        return htmlResponse(errorPage("Render Error", String(err)), 500);
      }
    }

    return null;
  };
}
