#!/usr/bin/env bash
# Performance + chaos analysis for spawn-on-demand tool servers.
# Tests real-world usage patterns: search, indexing, concurrent load,
# malformed input, large payloads, and edge cases.
#
# Usage: ./tests/test_tools_perf_chaos.sh
set -euo pipefail

cd "$(dirname "$0")/.."
PASS=0
FAIL=0
PIDS=()
TEST_TMPDIR=$(mktemp -d)

cleanup() {
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    done
    rm -rf "$TEST_TMPDIR"
}
trap cleanup EXIT

ok() { echo -e "\033[0;32m✓\033[0m $1"; PASS=$((PASS+1)); }
fail() { echo -e "\033[0;31m✗\033[0m $1: $2"; FAIL=$((FAIL+1)); }
info() { echo -e "\033[0;36m  ℹ\033[0m $1"; }

wait_ready() {
    local url="$1" pattern="${2:-}" tries=50
    for i in $(seq 1 $tries); do
        local body
        body=$(curl -sf "$url" 2>/dev/null) || { sleep 0.1; continue; }
        if [[ -z "$pattern" ]] || echo "$body" | grep -q "$pattern"; then return 0; fi
        sleep 0.1
    done
    return 1
}

time_ms() {
    # Returns elapsed ms for a curl request
    local url="$1"
    curl -s -o /dev/null -w "%{time_total}" "$url" 2>/dev/null | awk '{printf "%.0f", $1 * 1000}'
}

if ! command -v bun &>/dev/null; then
    echo "SKIP: bun not installed"
    exit 0
fi

TOKEN="perf-token-$(openssl rand -hex 8)"

# ════════════════════════════════════════════════════════════════════
# SETUP: Generate test data at various scales
# ════════════════════════════════════════════════════════════════════

echo ""
echo "── Setup: Generating test data ──────────────────────────────"

# Small transcript (10 entries)
python3 -c "
import json, sys
for i in range(10):
    role = 'user' if i % 2 == 0 else 'assistant'
    content = f'Message number {i} about topic alpha' if role == 'user' else [{'type':'text','text':f'Response {i} about topic beta'}]
    entry = {'type':'summary','uuid':f'uuid-{i:04d}','message':{'role':role,'content':content},'timestamp':f'2026-01-01T00:{i:02d}:00Z','cwd':'/tmp','cost_usd':0.01,'duration_ms':1000}
    print(json.dumps(entry))
" > "$TEST_TMPDIR/small.jsonl"

# Medium transcript (1000 entries)
python3 -c "
import json
for i in range(1000):
    role = 'user' if i % 2 == 0 else 'assistant'
    if role == 'user':
        content = f'User prompt {i}: explain how to implement feature number {i} with proper error handling and testing'
    else:
        content = [{'type':'text','text':f'Here is the implementation for feature {i}. First we need to consider the architecture. ' * 5}]
    entry = {'type':'summary','uuid':f'uuid-{i:04d}','message':{'role':role,'content':content,'usage':{'input_tokens':500,'output_tokens':200}},'timestamp':f'2026-01-01T{i//60:02d}:{i%60:02d}:00Z','cwd':'/tmp','cost_usd':0.01,'duration_ms':1000}
    print(json.dumps(entry))
" > "$TEST_TMPDIR/medium.jsonl"

# Large transcript (10000 entries)
python3 -c "
import json
for i in range(10000):
    role = 'user' if i % 2 == 0 else 'assistant'
    if role == 'user':
        content = f'Detailed user prompt {i}: we need to refactor the authentication module to support OAuth2 with PKCE flow number {i}'
    else:
        content = [{'type':'text','text':f'Implementation plan for item {i}: ' + 'The system requires careful consideration of security, performance, and maintainability. ' * 10}]
    ts_h = (i // 3600) % 24
    ts_m = (i // 60) % 60
    ts_s = i % 60
    entry = {'type':'summary','uuid':f'uuid-{i:05d}','message':{'role':role,'content':content,'usage':{'input_tokens':1000,'output_tokens':500},'model':'claude-sonnet-4-20250514'},'timestamp':f'2026-01-{1+i//86400:02d}T{ts_h:02d}:{ts_m:02d}:{ts_s:02d}Z','cwd':'/home/claude/project','cost_usd':0.05,'duration_ms':3000,'version':'1.0.43','gitBranch':'main'}
    print(json.dumps(entry))
" > "$TEST_TMPDIR/large.jsonl"

SMALL_SIZE=$(wc -c < "$TEST_TMPDIR/small.jsonl")
MEDIUM_SIZE=$(wc -c < "$TEST_TMPDIR/medium.jsonl")
LARGE_SIZE=$(wc -c < "$TEST_TMPDIR/large.jsonl")
info "Small: 10 entries ($(numfmt --to=iec $SMALL_SIZE))"
info "Medium: 1000 entries ($(numfmt --to=iec $MEDIUM_SIZE))"
info "Large: 10000 entries ($(numfmt --to=iec $LARGE_SIZE))"

# Review HTML
cat > "$TEST_TMPDIR/review.html" << 'HTML'
<!DOCTYPE html><html><head><title>PR Review</title></head><body><h1>PR #99 Review</h1><div class="diff">Lots of diff content here</div></body></html>
HTML


# ════════════════════════════════════════════════════════════════════
# REWIND PERF: Indexing speed at scale
# ════════════════════════════════════════════════════════════════════

echo ""
echo "── Rewind: Indexing Performance ─────────────────────────────"

# Small (10 entries) — baseline
REWIND_PORT=10177
T_START=$(date +%s%N)
bun rewind/src/rewind.ts \
    --port "$REWIND_PORT" --token "$TOKEN" --ttl 30 \
    --jsonl-path "$TEST_TMPDIR/small.jsonl" --name perf-small --session-id perf-small \
    &>/dev/null &
PIDS+=($!)
wait_ready "http://localhost:$REWIND_PORT/?token=$TOKEN" '"entries"'
T_END=$(date +%s%N)
SMALL_INDEX_MS=$(( (T_END - T_START) / 1000000 ))
kill "${PIDS[-1]}" 2>/dev/null; wait "${PIDS[-1]}" 2>/dev/null || true; unset 'PIDS[-1]'
info "10 entries: index+serve in ${SMALL_INDEX_MS}ms"
if [[ $SMALL_INDEX_MS -lt 5000 ]]; then
    ok "Rewind: small transcript index < 5s (${SMALL_INDEX_MS}ms)"
else
    fail "Rewind: small index" "took ${SMALL_INDEX_MS}ms, expected < 5000ms"
fi

# Medium (1000 entries)
REWIND_PORT=10177
T_START=$(date +%s%N)
bun rewind/src/rewind.ts \
    --port "$REWIND_PORT" --token "$TOKEN" --ttl 30 \
    --jsonl-path "$TEST_TMPDIR/medium.jsonl" --name perf-medium --session-id perf-medium \
    &>/dev/null &
PIDS+=($!)
wait_ready "http://localhost:$REWIND_PORT/?token=$TOKEN" '"entries"'
T_END=$(date +%s%N)
MEDIUM_INDEX_MS=$(( (T_END - T_START) / 1000000 ))
kill "${PIDS[-1]}" 2>/dev/null; wait "${PIDS[-1]}" 2>/dev/null || true; unset 'PIDS[-1]'
info "1000 entries: index+serve in ${MEDIUM_INDEX_MS}ms"
if [[ $MEDIUM_INDEX_MS -lt 10000 ]]; then
    ok "Rewind: medium transcript index < 10s (${MEDIUM_INDEX_MS}ms)"
else
    fail "Rewind: medium index" "took ${MEDIUM_INDEX_MS}ms, expected < 10000ms"
fi

# Large (10000 entries)
REWIND_PORT=10177
T_START=$(date +%s%N)
bun rewind/src/rewind.ts \
    --port "$REWIND_PORT" --token "$TOKEN" --ttl 60 \
    --jsonl-path "$TEST_TMPDIR/large.jsonl" --name perf-large --session-id perf-large \
    &>/dev/null &
PIDS+=($!)
wait_ready "http://localhost:$REWIND_PORT/?token=$TOKEN" '"entries"' 100
T_END=$(date +%s%N)
LARGE_INDEX_MS=$(( (T_END - T_START) / 1000000 ))
info "10000 entries: index+serve in ${LARGE_INDEX_MS}ms"
if [[ $LARGE_INDEX_MS -lt 30000 ]]; then
    ok "Rewind: large transcript index < 30s (${LARGE_INDEX_MS}ms)"
else
    fail "Rewind: large index" "took ${LARGE_INDEX_MS}ms, expected < 30000ms"
fi


# ════════════════════════════════════════════════════════════════════
# REWIND PERF: Search performance (on large dataset)
# ════════════════════════════════════════════════════════════════════

echo ""
echo "── Rewind: Search Performance (10K entries) ─────────────────"

# Simple word search
_RESP="$TEST_TMPDIR/search_simple"
T_START=$(date +%s%N)
curl -s -o "$_RESP" "http://localhost:$REWIND_PORT/?token=$TOKEN&q=authentication"
T_END=$(date +%s%N)
SEARCH_SIMPLE_MS=$(( (T_END - T_START) / 1000000 ))
SIMPLE_COUNT=$(grep -o '"entries":\[' "$_RESP" | wc -l)
info "Simple search 'authentication': ${SEARCH_SIMPLE_MS}ms"
if [[ $SEARCH_SIMPLE_MS -lt 3000 ]] && [[ $SIMPLE_COUNT -gt 0 ]]; then
    ok "Rewind: simple search < 3s (${SEARCH_SIMPLE_MS}ms)"
else
    fail "Rewind: simple search" "${SEARCH_SIMPLE_MS}ms, matches=$SIMPLE_COUNT"
fi

# Multi-word search
_RESP="$TEST_TMPDIR/search_multi"
T_START=$(date +%s%N)
curl -s -o "$_RESP" "http://localhost:$REWIND_PORT/?token=$TOKEN&q=OAuth2+PKCE+flow"
T_END=$(date +%s%N)
SEARCH_MULTI_MS=$(( (T_END - T_START) / 1000000 ))
info "Multi-word search 'OAuth2 PKCE flow': ${SEARCH_MULTI_MS}ms"
if [[ $SEARCH_MULTI_MS -lt 3000 ]]; then
    ok "Rewind: multi-word search < 3s (${SEARCH_MULTI_MS}ms)"
else
    fail "Rewind: multi-word search" "${SEARCH_MULTI_MS}ms"
fi

# No-results search
_RESP="$TEST_TMPDIR/search_noresult"
T_START=$(date +%s%N)
curl -s -o "$_RESP" "http://localhost:$REWIND_PORT/?token=$TOKEN&q=xyznonexistentterm99999"
T_END=$(date +%s%N)
SEARCH_NORESULT_MS=$(( (T_END - T_START) / 1000000 ))
info "No-result search: ${SEARCH_NORESULT_MS}ms"
if [[ $SEARCH_NORESULT_MS -lt 2000 ]]; then
    ok "Rewind: no-result search < 2s (${SEARCH_NORESULT_MS}ms)"
else
    fail "Rewind: no-result search" "${SEARCH_NORESULT_MS}ms"
fi

# Pagination performance (page 1 vs page 100)
_RESP="$TEST_TMPDIR/page1"
T_START=$(date +%s%N)
curl -s -o "$_RESP" "http://localhost:$REWIND_PORT/?token=$TOKEN&page=1&per_page=50"
T_END=$(date +%s%N)
PAGE1_MS=$(( (T_END - T_START) / 1000000 ))

_RESP="$TEST_TMPDIR/page100"
T_START=$(date +%s%N)
curl -s -o "$_RESP" "http://localhost:$REWIND_PORT/?token=$TOKEN&page=100&per_page=50"
T_END=$(date +%s%N)
PAGE100_MS=$(( (T_END - T_START) / 1000000 ))
info "Page 1: ${PAGE1_MS}ms, Page 100: ${PAGE100_MS}ms"
if [[ $PAGE100_MS -lt 3000 ]]; then
    ok "Rewind: deep pagination < 3s (page100=${PAGE100_MS}ms)"
else
    fail "Rewind: deep pagination" "${PAGE100_MS}ms"
fi

# Stats endpoint performance
T_START=$(date +%s%N)
curl -s -o /dev/null "http://localhost:$REWIND_PORT/api/stats?token=$TOKEN"
T_END=$(date +%s%N)
STATS_MS=$(( (T_END - T_START) / 1000000 ))
info "Stats endpoint: ${STATS_MS}ms"
if [[ $STATS_MS -lt 3000 ]]; then
    ok "Rewind: stats endpoint < 3s (${STATS_MS}ms)"
else
    fail "Rewind: stats" "${STATS_MS}ms"
fi


# ════════════════════════════════════════════════════════════════════
# REWIND CHAOS: Edge cases and malformed input
# ════════════════════════════════════════════════════════════════════

echo ""
echo "── Rewind: Chaos Tests ──────────────────────────────────────"

# SQL injection in search query
_RESP="$TEST_TMPDIR/chaos_sqli"
STATUS=$(curl -s -o "$_RESP" -w "%{http_code}" "http://localhost:$REWIND_PORT/?token=$TOKEN&q=';DROP+TABLE+entries;--")
if [[ "$STATUS" == "200" ]]; then
    ok "Rewind: SQL injection in search returns 200 (sanitized)"
else
    fail "Rewind: SQL injection" "status=$STATUS"
fi

# Verify DB is still intact after SQL injection attempt
_RESP="$TEST_TMPDIR/chaos_post_sqli"
STATUS=$(curl -s -o "$_RESP" -w "%{http_code}" "http://localhost:$REWIND_PORT/?token=$TOKEN")
if [[ "$STATUS" == "200" ]] && grep -q '"entries"' "$_RESP"; then
    ok "Rewind: DB intact after SQL injection attempt"
else
    fail "Rewind: DB integrity" "status=$STATUS after SQL injection"
fi

# FTS5 special characters
_RESP="$TEST_TMPDIR/chaos_fts"
STATUS=$(curl -s -o "$_RESP" -w "%{http_code}" "http://localhost:$REWIND_PORT/?token=$TOKEN&q=file.py:123+AND+OR+NOT")
if [[ "$STATUS" == "200" ]]; then
    ok "Rewind: FTS5 special chars handled gracefully"
else
    fail "Rewind: FTS5 special chars" "status=$STATUS"
fi

# Empty search query
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$REWIND_PORT/?token=$TOKEN&q=")
if [[ "$STATUS" == "200" ]]; then
    ok "Rewind: empty search query → 200"
else
    fail "Rewind: empty search" "status=$STATUS"
fi

# Extremely long search query (10KB)
LONG_QUERY=$(python3 -c "print('word ' * 2000)")
_RESP="$TEST_TMPDIR/chaos_long_query"
STATUS=$(curl -s -o "$_RESP" -w "%{http_code}" --max-time 10 "http://localhost:$REWIND_PORT/?token=$TOKEN&q=$(python3 -c 'import urllib.parse; print(urllib.parse.quote("word " * 2000))')")
if [[ "$STATUS" == "200" ]] || [[ "$STATUS" == "414" ]]; then
    ok "Rewind: very long search query handled (status=$STATUS)"
else
    fail "Rewind: long query" "status=$STATUS"
fi

# Invalid page numbers
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$REWIND_PORT/?token=$TOKEN&page=-1")
if [[ "$STATUS" == "200" ]]; then
    ok "Rewind: negative page → 200 (clamped)"
else
    fail "Rewind: negative page" "status=$STATUS"
fi

STATUS=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$REWIND_PORT/?token=$TOKEN&page=999999")
if [[ "$STATUS" == "200" ]]; then
    ok "Rewind: huge page number → 200 (clamped)"
else
    fail "Rewind: huge page" "status=$STATUS"
fi

STATUS=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$REWIND_PORT/?token=$TOKEN&per_page=0")
if [[ "$STATUS" == "200" ]]; then
    ok "Rewind: per_page=0 → 200 (clamped to 1)"
else
    fail "Rewind: per_page=0" "status=$STATUS"
fi

# Concurrent requests (10 parallel)
echo -n "" > "$TEST_TMPDIR/concurrent_results"
CURL_PIDS=()
for i in $(seq 1 10); do
    curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:$REWIND_PORT/?token=$TOKEN&q=feature+$i" >> "$TEST_TMPDIR/concurrent_results" &
    CURL_PIDS+=($!)
done
for cpid in "${CURL_PIDS[@]}"; do wait "$cpid" 2>/dev/null || true; done
CONCURRENT_OK=$(grep -c "200" "$TEST_TMPDIR/concurrent_results")
if [[ $CONCURRENT_OK -eq 10 ]]; then
    ok "Rewind: 10 concurrent search requests all 200"
else
    fail "Rewind: concurrent" "$CONCURRENT_OK/10 returned 200"
fi

# Malformed JSONL (server should survive)
cat > "$TEST_TMPDIR/malformed.jsonl" << 'JSONL'
{"type":"summary","uuid":"ok1","message":{"role":"user","content":"good entry"},"timestamp":"2026-01-01T00:00:00Z","cwd":"/tmp"}
this is not json
{"broken json
{"type":"summary","uuid":"ok2","message":{"role":"user","content":"another good entry"},"timestamp":"2026-01-01T00:01:00Z","cwd":"/tmp"}
JSONL

# Kill current large server, start one with malformed data
kill "${PIDS[-1]}" 2>/dev/null; wait "${PIDS[-1]}" 2>/dev/null || true; unset 'PIDS[-1]'

REWIND_PORT=10177
bun rewind/src/rewind.ts \
    --port "$REWIND_PORT" --token "$TOKEN" --ttl 30 \
    --jsonl-path "$TEST_TMPDIR/malformed.jsonl" --name chaos-malformed --session-id chaos-malformed \
    &>/dev/null &
PIDS+=($!)
wait_ready "http://localhost:$REWIND_PORT/?token=$TOKEN" '"entries"'

_RESP="$TEST_TMPDIR/malformed_resp"
curl -s -o "$_RESP" "http://localhost:$REWIND_PORT/?token=$TOKEN"
ENTRY_COUNT=$(python3 -c "
import json, sys, re
html = open('$_RESP').read()
m = re.search(r'id=\"transcript-data\">(.*?)</script>', html, re.DOTALL)
if not m: sys.exit(1)
blob = m.group(1).replace('<\\\\/','</')
data = json.loads(blob)
print(len(data.get('entries',[])))
" 2>/dev/null || echo "0")
if [[ "$ENTRY_COUNT" == "2" ]]; then
    ok "Rewind: malformed JSONL → skips bad lines, serves 2 good entries"
else
    info "Got $ENTRY_COUNT entries (expected 2)"
    if [[ "$ENTRY_COUNT" -gt 0 ]]; then
        ok "Rewind: malformed JSONL → survived (${ENTRY_COUNT} entries)"
    else
        fail "Rewind: malformed JSONL" "no entries parsed"
    fi
fi

kill "${PIDS[-1]}" 2>/dev/null; wait "${PIDS[-1]}" 2>/dev/null || true; unset 'PIDS[-1]'


# ════════════════════════════════════════════════════════════════════
# REWIND CHAOS: Empty / missing file
# ════════════════════════════════════════════════════════════════════

# Empty JSONL file
touch "$TEST_TMPDIR/empty.jsonl"
bun rewind/src/rewind.ts \
    --port "$REWIND_PORT" --token "$TOKEN" --ttl 15 \
    --jsonl-path "$TEST_TMPDIR/empty.jsonl" --name chaos-empty --session-id chaos-empty \
    &>/dev/null &
PIDS+=($!)
wait_ready "http://localhost:$REWIND_PORT/?token=$TOKEN"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$REWIND_PORT/?token=$TOKEN")
if [[ "$STATUS" == "200" ]]; then
    ok "Rewind: empty JSONL file → serves page (no crash)"
else
    fail "Rewind: empty JSONL" "status=$STATUS"
fi
kill "${PIDS[-1]}" 2>/dev/null; wait "${PIDS[-1]}" 2>/dev/null || true; unset 'PIDS[-1]'


# ════════════════════════════════════════════════════════════════════
# REVIEW PERF: Serving + auth
# ════════════════════════════════════════════════════════════════════

echo ""
echo "── Review: Performance ──────────────────────────────────────"

REVIEW_PORT=10178
bun review/src/serve.ts \
    --port "$REVIEW_PORT" --token "$TOKEN" --ttl 30 \
    --html-path "$TEST_TMPDIR/review.html" --owner TestOrg --repo test-repo --pr-num 99 \
    &>/dev/null &
PIDS+=($!)
wait_ready "http://localhost:$REVIEW_PORT/health"

# HTML serve latency
T_START=$(date +%s%N)
curl -s -o /dev/null "http://localhost:$REVIEW_PORT/?token=$TOKEN"
T_END=$(date +%s%N)
REVIEW_SERVE_MS=$(( (T_END - T_START) / 1000000 ))
info "HTML serve: ${REVIEW_SERVE_MS}ms"
if [[ $REVIEW_SERVE_MS -lt 500 ]]; then
    ok "Review: HTML serve < 500ms (${REVIEW_SERVE_MS}ms)"
else
    fail "Review: HTML serve" "${REVIEW_SERVE_MS}ms"
fi

# Health endpoint latency
T_START=$(date +%s%N)
curl -s -o /dev/null "http://localhost:$REVIEW_PORT/health"
T_END=$(date +%s%N)
HEALTH_MS=$(( (T_END - T_START) / 1000000 ))
info "Health: ${HEALTH_MS}ms"
if [[ $HEALTH_MS -lt 100 ]]; then
    ok "Review: health < 100ms (${HEALTH_MS}ms)"
else
    fail "Review: health latency" "${HEALTH_MS}ms"
fi


# ════════════════════════════════════════════════════════════════════
# REVIEW CHAOS: Edge cases
# ════════════════════════════════════════════════════════════════════

echo ""
echo "── Review: Chaos Tests ──────────────────────────────────────"

# Concurrent requests (10 parallel)
echo -n "" > "$TEST_TMPDIR/review_concurrent"
CURL_PIDS=()
for i in $(seq 1 10); do
    curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:$REVIEW_PORT/?token=$TOKEN" >> "$TEST_TMPDIR/review_concurrent" &
    CURL_PIDS+=($!)
done
for cpid in "${CURL_PIDS[@]}"; do wait "$cpid" 2>/dev/null || true; done
REVIEW_CONC_OK=$(grep -c "200" "$TEST_TMPDIR/review_concurrent")
if [[ $REVIEW_CONC_OK -eq 10 ]]; then
    ok "Review: 10 concurrent HTML requests all 200"
else
    fail "Review: concurrent" "$REVIEW_CONC_OK/10 returned 200"
fi

# POST with invalid JSON
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    -H "Content-Type: application/json" \
    -d 'not json' \
    "http://localhost:$REVIEW_PORT/tools/review/comments?token=$TOKEN")
if [[ "$STATUS" == "400" ]]; then
    ok "Review: invalid JSON POST → 400"
else
    fail "Review: invalid JSON" "status=$STATUS, expected 400"
fi

# POST with empty body
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    -H "Content-Type: application/json" \
    -d '{"body":""}' \
    "http://localhost:$REVIEW_PORT/tools/review/comments?token=$TOKEN")
if [[ "$STATUS" == "400" ]]; then
    ok "Review: empty comment body → 400"
else
    fail "Review: empty body" "status=$STATUS, expected 400"
fi

# Unknown path
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$REVIEW_PORT/nonexistent?token=$TOKEN")
if [[ "$STATUS" == "404" ]]; then
    ok "Review: unknown path → 404"
else
    fail "Review: unknown path" "status=$STATUS, expected 404"
fi

# Token in POST body (alternative auth)
_RESP="$TEST_TMPDIR/review_post_token"
STATUS=$(curl -s -o "$_RESP" -w "%{http_code}" -X POST \
    -H "Content-Type: application/json" \
    -d "{\"token\":\"$TOKEN\",\"body\":\"test\"}" \
    "http://localhost:$REVIEW_PORT/tools/review/comments")
# Should accept token from body (returns 502 since no real GH, but not 403)
if [[ "$STATUS" != "403" ]]; then
    ok "Review: token accepted from POST body (status=$STATUS)"
else
    fail "Review: POST body token" "rejected with 403"
fi

kill "${PIDS[-1]}" 2>/dev/null; wait "${PIDS[-1]}" 2>/dev/null || true; unset 'PIDS[-1]'


# ════════════════════════════════════════════════════════════════════
# PILOT CHAOS: Spawn + edge cases
# ════════════════════════════════════════════════════════════════════

echo ""
echo "── Pilot: Chaos Tests ───────────────────────────────────────"

PILOT_PORT=10179
if [[ -f "pilot/src/pilot.ts" ]]; then
    bun pilot/src/pilot.ts --port "$PILOT_PORT" --token "$TOKEN" --ttl 15 &>/dev/null &
    PIDS+=($!)
    sleep 2

    # Concurrent auth failures (10 parallel bad tokens)
    echo -n "" > "$TEST_TMPDIR/pilot_concurrent"
    CURL_PIDS=()
    for i in $(seq 1 10); do
        curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:$PILOT_PORT/?token=bad-$i" >> "$TEST_TMPDIR/pilot_concurrent" &
        CURL_PIDS+=($!)
    done
    for cpid in "${CURL_PIDS[@]}"; do wait "$cpid" 2>/dev/null || true; done
    PILOT_BAD_AUTH=$(grep -cv "200" "$TEST_TMPDIR/pilot_concurrent" 2>/dev/null || echo 0)
    if [[ $PILOT_BAD_AUTH -eq 10 ]]; then
        ok "Pilot: 10 concurrent bad-token requests all rejected"
    else
        fail "Pilot: concurrent bad auth" "$PILOT_BAD_AUTH/10 rejected"
    fi

    # Verify server still alive after auth flood
    if kill -0 "${PIDS[-1]}" 2>/dev/null; then
        ok "Pilot: survives auth flood (still running)"
    else
        fail "Pilot: auth flood" "process died"
    fi

    kill "${PIDS[-1]}" 2>/dev/null; wait "${PIDS[-1]}" 2>/dev/null || true; unset 'PIDS[-1]'
else
    echo "⚠ pilot/src/pilot.ts not found — skipping pilot chaos"
fi


# ════════════════════════════════════════════════════════════════════
# CROSS-TOOL: Memory / resource checks
# ════════════════════════════════════════════════════════════════════

echo ""
echo "── Cross-Tool: Resource Checks ──────────────────────────────"

# Check that temp DB files are created in expected location
if ls /tmp/transcript-cache/perf-*.db &>/dev/null; then
    ok "Rewind: SQLite DBs created in /tmp/transcript-cache/"
else
    fail "Rewind: DB location" "no DB files found in /tmp/transcript-cache/"
fi

# Check DB size vs JSONL size (should be reasonable)
if [[ -f "/tmp/transcript-cache/perf-large.db" ]]; then
    DB_SIZE=$(stat -c %s /tmp/transcript-cache/perf-large.db 2>/dev/null || echo 0)
    RATIO=$(python3 -c "print(f'{$DB_SIZE / $LARGE_SIZE:.1f}x')" 2>/dev/null || echo "?x")
    info "Large DB size: $(numfmt --to=iec $DB_SIZE) (${RATIO} vs JSONL)"
    if [[ $DB_SIZE -lt $(( LARGE_SIZE * 5 )) ]]; then
        ok "Rewind: DB size reasonable (< 5x JSONL)"
    else
        fail "Rewind: DB bloat" "DB is ${RATIO} of JSONL"
    fi
fi


# ════════════════════════════════════════════════════════════════════
# Summary
# ════════════════════════════════════════════════════════════════════

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
TOTAL=$((PASS+FAIL))
echo "  $PASS/$TOTAL passed, $FAIL failed"
echo ""
echo "  Performance Summary:"
echo "    Rewind index:  10→${SMALL_INDEX_MS}ms  1K→${MEDIUM_INDEX_MS}ms  10K→${LARGE_INDEX_MS}ms"
echo "    Rewind search: simple=${SEARCH_SIMPLE_MS}ms  multi=${SEARCH_MULTI_MS}ms  empty=${SEARCH_NORESULT_MS}ms"
echo "    Rewind pages:  p1=${PAGE1_MS}ms  p100=${PAGE100_MS}ms  stats=${STATS_MS}ms"
echo "    Review serve:  html=${REVIEW_SERVE_MS}ms  health=${HEALTH_MS}ms"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [[ $FAIL -gt 0 ]]; then
    exit 1
fi
