/**
 * JSONL transcript indexer using bun:sqlite with FTS5.
 * Port of bridge.py's Python indexer (~370 lines → ~250 lines).
 */
import { Database } from "bun:sqlite";
import { readFileSync, statSync, existsSync, mkdirSync } from "fs";
import { dirname, resolve } from "path";

// Types skipped from JSONL indexing
const SKIP_TYPES = new Set([
  "progress",
  "queue-operation",
  "file-history-snapshot",
  "system",
]);

export interface EntriesResult {
  entries: Record<string, unknown>[];
  total: number;
  total_pages: number;
  page: number;
  per_page: number;
}

export interface SearchResult extends EntriesResult {
  total_results: number;
  query: string;
}

export interface TranscriptStats {
  input_tokens: number;
  output_tokens: number;
  first_ts: string;
  last_ts: string;
  model: string;
  version: string;
  git_branch: string;
  n_user: number;
  n_tool: number;
  n_edit: number;
  n_files: number;
  lines_add: number;
  lines_del: number;
  lines_mod: number;
  duration: string;
}

function emptyStats(): TranscriptStats {
  return {
    input_tokens: 0,
    output_tokens: 0,
    first_ts: "",
    last_ts: "",
    model: "",
    version: "",
    git_branch: "",
    n_user: 0,
    n_tool: 0,
    n_edit: 0,
    n_files: 0,
    lines_add: 0,
    lines_del: 0,
    lines_mod: 0,
    duration: "",
  };
}

function emptyEntries(perPage = 50): EntriesResult {
  return { entries: [], total: 0, total_pages: 0, page: 0, per_page: perPage };
}

function emptySearch(query = "", perPage = 50): SearchResult {
  return { ...emptyEntries(perPage), total_results: 0, query };
}

/** Setup SQLite DB with WAL mode */
export function setupDb(dbPath: string): Database {
  const dir = dirname(dbPath);
  if (dir && !existsSync(dir)) mkdirSync(dir, { recursive: true });
  const db = new Database(dbPath);
  db.exec("PRAGMA journal_mode=WAL");
  db.exec("PRAGMA busy_timeout=5000");
  return db;
}

/** Create tables + FTS5 virtual table */
export function createTables(db: Database): void {
  db.exec(`CREATE TABLE IF NOT EXISTS entries (
    idx INTEGER PRIMARY KEY,
    byte_offset INTEGER,
    type TEXT,
    role TEXT,
    timestamp TEXT,
    model TEXT,
    version TEXT,
    git_branch TEXT,
    raw_json TEXT,
    plain_text TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0
  )`);
  const hasFts = db
    .query("SELECT name FROM sqlite_master WHERE type='table' AND name='entries_fts'")
    .get();
  if (!hasFts) {
    db.exec(`CREATE VIRTUAL TABLE entries_fts USING fts5(
      plain_text, content=entries, content_rowid=idx,
      tokenize='unicode61'
    )`);
  }
  db.exec("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)");
}

/** Check if incremental indexing is needed. Returns [oldSize, needsWork]. */
function checkIncremental(
  db: Database,
  jsonlPath: string
): [number, boolean] {
  const fileSize = statSync(jsonlPath).size;
  if (fileSize === 0) return [0, false];

  const absPath = resolve(jsonlPath);
  const sizeRow = db.query("SELECT value FROM meta WHERE key='file_size'").get() as
    | { value: string }
    | null;
  const oldSize = sizeRow ? parseInt(sizeRow.value) : 0;

  const pathRow = db.query("SELECT value FROM meta WHERE key='jsonl_path'").get() as
    | { value: string }
    | null;
  const oldPath = pathRow ? pathRow.value : "";

  // Path changed — full reindex
  if (oldPath && oldPath !== absPath) {
    db.exec("DELETE FROM entries");
    db.exec("DELETE FROM entries_fts");
    db.exec("DELETE FROM meta");
    return [0, true];
  }
  // No changes
  if (oldSize === fileSize) return [oldSize, false];
  // File shrank — full reindex
  if (oldSize > fileSize) {
    db.exec("DELETE FROM entries");
    db.exec("DELETE FROM entries_fts");
    db.exec("DELETE FROM meta");
    return [0, true];
  }
  return [oldSize, true];
}

/** Update meta after indexing */
function updateMeta(db: Database, jsonlPath: string, nextIdx: number): void {
  const fileSize = statSync(jsonlPath).size;
  const absPath = resolve(jsonlPath);
  const upsert = db.prepare("INSERT OR REPLACE INTO meta VALUES (?, ?)");
  upsert.run("file_size", String(fileSize));
  upsert.run("entry_count", String(nextIdx));
  upsert.run("jsonl_path", absPath);
}

/** Extract plain text from a JSONL entry for FTS indexing */
function extractPlainText(entry: Record<string, unknown>): string {
  const msg = (entry.message as Record<string, unknown>) || {};
  const content = msg.content;
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";

  const parts: string[] = [];
  for (const c of content) {
    if (!c || typeof c !== "object") continue;
    const item = c as Record<string, unknown>;
    if (item.text) parts.push(String(item.text));
    if (item.thinking) parts.push(String(item.thinking));
    if (typeof item.content === "string") parts.push(item.content);
    if (item.type === "tool_use") {
      parts.push(JSON.stringify(item.input || {}));
    }
  }
  return parts.join(" ");
}

/** Index JSONL file incrementally. Returns count of new entries. */
export function indexJsonl(db: Database, jsonlPath: string): number {
  const [oldSize, needsWork] = checkIncremental(db, jsonlPath);
  if (!needsWork && oldSize === statSync(jsonlPath).size) return 0;

  const maxRow = db.query("SELECT MAX(idx) as m FROM entries").get() as { m: number | null };
  let nextIdx = maxRow?.m != null ? maxRow.m + 1 : 0;
  let newCount = 0;

  // Read the file from oldSize offset
  const buf = readFileSync(jsonlPath);
  const text = buf.slice(oldSize).toString("utf-8");

  const insertEntry = db.prepare(
    "INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
  );
  const insertFts = db.prepare(
    "INSERT INTO entries_fts(rowid, plain_text) VALUES (?, ?)"
  );

  const transaction = db.transaction(() => {
    for (const line of text.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      let entry: Record<string, unknown>;
      try {
        entry = JSON.parse(trimmed);
      } catch {
        continue;
      }

      const etype = String(entry.type || "");
      if (SKIP_TYPES.has(etype)) continue;

      const msg = (entry.message as Record<string, unknown>) || {};
      const plainText = extractPlainText(entry);
      const usage = (msg.usage as Record<string, number>) || {};
      const inputTokens =
        (usage.input_tokens || 0) +
        (usage.cache_read_input_tokens || 0) +
        (usage.cache_creation_input_tokens || 0);
      const outputTokens = usage.output_tokens || 0;

      insertEntry.run(
        nextIdx,
        oldSize,
        etype,
        String(msg.role || ""),
        String(entry.timestamp || ""),
        String(msg.model || ""),
        String(entry.version || ""),
        String(entry.gitBranch || ""),
        JSON.stringify(entry),
        plainText,
        inputTokens,
        outputTokens
      );
      insertFts.run(nextIdx, plainText);
      nextIdx++;
      newCount++;
    }
    updateMeta(db, jsonlPath, nextIdx);
  });

  transaction();
  return newCount;
}

/** Paginate helper */
function paginate(
  total: number,
  page: number | null,
  perPage: number
): [number, number, number] {
  const totalPages = total > 0 ? Math.max(1, Math.ceil(total / perPage)) : 0;
  let p = page ?? totalPages; // Default to last page
  p = Math.max(1, Math.min(p, totalPages));
  const offset = (p - 1) * perPage;
  return [p, totalPages, offset];
}

/** Query paginated entries */
export function queryEntries(
  db: Database,
  page: number | null = null,
  perPage = 50,
  filterMode = ""
): EntriesResult {
  let where = "";
  if (filterMode === "prompts") {
    where =
      "WHERE type='user' AND role='user' AND plain_text != '' AND plain_text NOT LIKE '<%' AND json_type(raw_json, '$.message.content') = 'text'";
  }
  const totalRow = db.query(`SELECT COUNT(*) as c FROM entries ${where}`).get() as { c: number };
  const total = totalRow.c;
  if (total === 0) return emptyEntries(perPage);

  const [pageNum, totalPages, offset] = paginate(total, page, perPage);
  const rows = db
    .query(
      `SELECT idx, type, role, timestamp, raw_json FROM entries ${where} ORDER BY idx LIMIT ? OFFSET ?`
    )
    .all(perPage, offset) as { idx: number; type: string; role: string; timestamp: string; raw_json: string }[];

  return {
    entries: rows.map((r) => ({
      idx: r.idx,
      type: r.type,
      role: r.role,
      timestamp: r.timestamp,
      raw_json: r.raw_json,
    })),
    total,
    total_pages: totalPages,
    page: pageNum,
    per_page: perPage,
  };
}

/** Sanitize FTS5 query — quote special chars, preserve boolean operators */
function sanitizeFts5Query(term: string): string {
  if (/\b(AND|OR|NOT|NEAR)\b/.test(term)) return term;
  if (/[.*:^(){}"\-]/.test(term)) {
    return term
      .split(/\s+/)
      .filter(Boolean)
      .map((t) => `"${t.replace(/"/g, "")}"`)
      .join(" ");
  }
  return term;
}

/** Search entries via FTS5 */
export function querySearch(
  db: Database,
  searchTerm: string,
  page = 1,
  perPage = 50,
  sort = "relevance"
): SearchResult {
  if (!searchTerm) return emptySearch("", perPage);

  let safeTerm = sanitizeFts5Query(searchTerm);
  let totalResults: number;
  try {
    const row = db
      .query("SELECT COUNT(*) as c FROM entries_fts WHERE entries_fts MATCH ?")
      .get(safeTerm) as { c: number };
    totalResults = row.c;
  } catch {
    safeTerm = `"${searchTerm.replace(/"/g, '""')}"`;
    try {
      const row = db
        .query("SELECT COUNT(*) as c FROM entries_fts WHERE entries_fts MATCH ?")
        .get(safeTerm) as { c: number };
      totalResults = row.c;
    } catch {
      return emptySearch(searchTerm, perPage);
    }
  }

  if (totalResults === 0) return emptySearch(searchTerm, perPage);

  const [pageNum, totalPages, offset] = paginate(totalResults, page, perPage);
  const order = sort === "time" ? "ORDER BY e.idx" : "ORDER BY f.rank";
  const rows = db
    .query(
      `SELECT e.idx, e.type, e.role, e.timestamp, e.raw_json, f.rank
       FROM entries_fts f JOIN entries e ON f.rowid = e.idx
       WHERE entries_fts MATCH ? ${order} LIMIT ? OFFSET ?`
    )
    .all(safeTerm, perPage, offset) as {
    idx: number;
    type: string;
    role: string;
    timestamp: string;
    raw_json: string;
    rank: number;
  }[];

  return {
    entries: rows.map((r) => ({
      idx: r.idx,
      type: r.type,
      role: r.role,
      timestamp: r.timestamp,
      raw_json: r.raw_json,
      rank: r.rank,
    })),
    total: totalResults,
    total_results: totalResults,
    total_pages: totalPages,
    page: pageNum,
    per_page: perPage,
    query: searchTerm,
  };
}

/** Compute transcript statistics */
export function queryStats(db: Database): TranscriptStats {
  const agg = db
    .query(
      `SELECT
        SUM(input_tokens) as it, SUM(output_tokens) as ot,
        MIN(timestamp) as first_ts, MAX(timestamp) as last_ts,
        (SELECT model FROM entries WHERE type='assistant' AND model != '' LIMIT 1) as model,
        (SELECT version FROM entries WHERE version != '' LIMIT 1) as version,
        (SELECT git_branch FROM entries WHERE git_branch != '' LIMIT 1) as branch
      FROM entries`
    )
    .get() as Record<string, unknown> | null;

  if (!agg || agg.it == null) return emptyStats();

  const stats = emptyStats();
  stats.input_tokens = Number(agg.it) || 0;
  stats.output_tokens = Number(agg.ot) || 0;
  stats.first_ts = String(agg.first_ts || "");
  stats.last_ts = String(agg.last_ts || "");
  stats.model = String(agg.model || "");
  stats.version = String(agg.version || "");
  stats.git_branch = String(agg.branch || "");

  // User prompt count
  const nUser = db
    .query(
      "SELECT COUNT(*) as c FROM entries WHERE type='user' AND role='user' AND plain_text != '' AND json_type(raw_json, '$.message.content') = 'text'"
    )
    .get() as { c: number };
  stats.n_user = nUser.c;

  // Tool stats from assistant entries
  const filesModified = new Set<string>();
  const assistantRows = db
    .query("SELECT raw_json FROM entries WHERE type='assistant'")
    .all() as { raw_json: string }[];

  for (const row of assistantRows) {
    let entry: Record<string, unknown>;
    try {
      entry = JSON.parse(row.raw_json);
    } catch {
      continue;
    }
    const content = ((entry.message as Record<string, unknown>) || {}).content;
    if (!Array.isArray(content)) continue;

    for (const item of content) {
      if (!item || typeof item !== "object" || (item as Record<string, unknown>).type !== "tool_use")
        continue;
      const ti = item as Record<string, unknown>;
      stats.n_tool++;
      const input = (ti.input as Record<string, unknown>) || {};
      const fp = String(input.file_path || input.path || "");
      if (fp) filesModified.add(fp);
      if (ti.name === "Edit") {
        stats.n_edit++;
        const nOld = String(input.old_string || "").split("\n").length;
        const nNew = String(input.new_string || "").split("\n").length;
        const overlap = Math.min(nOld, nNew);
        stats.lines_mod += overlap;
        stats.lines_add += nNew - overlap;
        stats.lines_del += nOld - overlap;
      }
    }
  }
  stats.n_files = filesModified.size;

  // Duration
  if (stats.first_ts && stats.last_ts) {
    try {
      const t0 = new Date(stats.first_ts).getTime();
      const t1 = new Date(stats.last_ts).getTime();
      const delta = Math.floor((t1 - t0) / 1000);
      if (delta > 0) {
        const days = Math.floor(delta / 86400);
        const hours = Math.floor((delta % 86400) / 3600);
        const mins = Math.floor((delta % 3600) / 60);
        if (days > 0) stats.duration = `${days}d ${hours}h`;
        else if (hours > 0) stats.duration = `${hours}h ${mins}m`;
        else stats.duration = `${mins}m`;
      }
    } catch {
      // ignore
    }
  }

  return stats;
}
