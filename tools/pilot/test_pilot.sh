#!/usr/bin/env bash
set -euo pipefail

TEST_PORT="${TEST_PORT:-10179}"
BASE_URL="http://127.0.0.1:${TEST_PORT}"
TEST_SESSION="claude-test-pilot"
NONCLAUDE_SESSION="pilot-test-nonclaude"
AUTH_TOKEN="pilot-secret-token"
TEST_LOG="/tmp/pilot-test.log"
SERVER_PID=""

cleanup() {
  if [[ -n "${SERVER_PID}" ]] && kill -0 "${SERVER_PID}" 2>/dev/null; then
    kill "${SERVER_PID}" 2>/dev/null || true
    wait "${SERVER_PID}" 2>/dev/null || true
  fi
  tmux kill-session -t "${TEST_SESSION}" 2>/dev/null || true
  tmux kill-session -t "${NONCLAUDE_SESSION}" 2>/dev/null || true
}
trap cleanup EXIT

wait_for_server() {
  local token="${1:-}"
  local url="${BASE_URL}/"
  if [[ -n "${token}" ]]; then
    url="${url}?token=${token}"
  fi

  for _ in $(seq 1 80); do
    local code
    code="$(curl -s -o /dev/null -w '%{http_code}' "${url}" || true)"
    if [[ "${code}" != "000" ]]; then
      return 0
    fi
    sleep 0.1
  done

  echo "Server failed to start. Log output:"
  cat "${TEST_LOG}" || true
  return 1
}

start_server() {
  local token="${1:-}"
  if [[ -n "${SERVER_PID}" ]] && kill -0 "${SERVER_PID}" 2>/dev/null; then
    kill "${SERVER_PID}" 2>/dev/null || true
    wait "${SERVER_PID}" 2>/dev/null || true
    SERVER_PID=""
  fi

  : >"${TEST_LOG}"
  if [[ -n "${token}" ]]; then
    PORT="${TEST_PORT}" PILOT_TOKEN="${token}" node pilot.js >"${TEST_LOG}" 2>&1 &
  else
    PORT="${TEST_PORT}" node pilot.js >"${TEST_LOG}" 2>&1 &
  fi
  SERVER_PID="$!"
  wait_for_server "${token}"
}

create_tmux_sessions() {
  tmux kill-session -t "${TEST_SESSION}" 2>/dev/null || true
  tmux kill-session -t "${NONCLAUDE_SESSION}" 2>/dev/null || true
  tmux new-session -d -s "${TEST_SESSION}" "bash -lc 'echo pilot-ready; exec bash'"
  tmux new-session -d -s "${NONCLAUDE_SESSION}" "bash -lc 'echo nonclaude-ready; exec bash'"
}

enable_session() {
  curl -s -X POST "http://localhost:${TEST_PORT}/api/pilot?session=$1"
}

disable_session() {
  curl -s -X DELETE "http://localhost:${TEST_PORT}/api/pilot?session=$1"
}

assert_http_code() {
  local expected="$1"
  local url="$2"
  local got
  got="$(curl -s -o /dev/null -w '%{http_code}' "${url}" || true)"
  if [[ "${got}" != "${expected}" ]]; then
    echo "Expected HTTP ${expected}, got ${got} for ${url}"
    return 1
  fi
}

ws_expect_open() {
  local url="$1"
  node --input-type=module -e "
    import WebSocket from 'ws';
    const ws = new WebSocket(process.argv[1]);
    ws.on('open', () => { console.log('CONNECTED'); ws.close(); process.exit(0); });
    ws.on('error', (e) => { console.error('ERROR: ' + e.message); process.exit(1); });
    setTimeout(() => { console.error('TIMEOUT'); process.exit(1); }, 5000);
  " "${url}"
}

ws_expect_rejected() {
  local url="$1"
  node --input-type=module -e "
    import WebSocket from 'ws';
    let opened = false;
    const ws = new WebSocket(process.argv[1]);
    ws.on('open', () => {
      opened = true;
      console.error('UNEXPECTED_OPEN');
      process.exit(1);
    });
    ws.on('error', () => process.exit(0));
    ws.on('close', () => {
      if (!opened) process.exit(0);
    });
    setTimeout(() => { console.error('TIMEOUT'); process.exit(1); }, 5000);
  " "${url}"
}

run_test() {
  local name="$1"
  echo "==> ${name}"
  "${name}"
  echo "PASS: ${name}"
}

test_server_starts() {
  assert_http_code "200" "${BASE_URL}/"
}

test_api_sessions_returns_json() {
  local payload
  payload="$(curl -s "${BASE_URL}/api/sessions")"
  node --input-type=module -e "
    const value = JSON.parse(process.argv[1]);
    if (!Array.isArray(value)) process.exit(1);
  " "${payload}"
}

test_api_sessions_lists_tmux() {
  enable_session "${TEST_SESSION}" >/dev/null
  local payload
  payload="$(curl -s "${BASE_URL}/api/sessions")"
  if ! grep -q "\"name\":\"${TEST_SESSION}\"" <<<"${payload}"; then
    echo "Expected ${TEST_SESSION} in /api/sessions"
    return 1
  fi
}

test_session_page_returns_html() {
  enable_session "${TEST_SESSION}" >/dev/null
  local html
  html="$(curl -s "${BASE_URL}/session/${TEST_SESSION}")"
  grep -q "ghostty-web" <<<"${html}"
}

test_ws_connects_to_session() {
  enable_session "${TEST_SESSION}" >/dev/null
  ws_expect_open "ws://127.0.0.1:${TEST_PORT}/ws?session=${TEST_SESSION}&cols=80&rows=24"
}

test_ws_invalid_session_rejected() {
  ws_expect_rejected "ws://127.0.0.1:${TEST_PORT}/ws?session=claude-does-not-exist&cols=80&rows=24"
}

test_session_prefix_guard() {
  local payload
  payload="$(curl -s "${BASE_URL}/api/sessions")"
  if grep -q "\"name\":\"${NONCLAUDE_SESSION}\"" <<<"${payload}"; then
    echo "Non-claude session should not be listed in /api/sessions"
    return 1
  fi

  ws_expect_rejected "ws://127.0.0.1:${TEST_PORT}/ws?session=${NONCLAUDE_SESSION}&cols=80&rows=24"
}

test_enable_session() {
  disable_session "${TEST_SESSION}" >/dev/null

  local response
  response="$(enable_session "${TEST_SESSION}")"
  if ! grep -q '"enabled":true' <<<"${response}"; then
    echo "Expected {\"enabled\":true} from POST /api/pilot"
    return 1
  fi

  local sessions_payload
  sessions_payload="$(curl -s "${BASE_URL}/api/sessions")"
  if ! grep -q "\"name\":\"${TEST_SESSION}\"" <<<"${sessions_payload}"; then
    echo "Expected ${TEST_SESSION} in /api/sessions after enable"
    return 1
  fi

  local enabled_payload
  enabled_payload="$(curl -s "${BASE_URL}/api/enabled")"
  if ! grep -q "\"${TEST_SESSION}\"" <<<"${enabled_payload}"; then
    echo "Expected ${TEST_SESSION} in /api/enabled after enable"
    return 1
  fi
}

test_disable_session() {
  enable_session "${TEST_SESSION}" >/dev/null

  local response
  response="$(disable_session "${TEST_SESSION}")"
  if ! grep -q '"enabled":false' <<<"${response}"; then
    echo "Expected {\"enabled\":false} from DELETE /api/pilot"
    return 1
  fi

  local sessions_payload
  sessions_payload="$(curl -s "${BASE_URL}/api/sessions")"
  if grep -q "\"name\":\"${TEST_SESSION}\"" <<<"${sessions_payload}"; then
    echo "Did not expect ${TEST_SESSION} in /api/sessions after disable"
    return 1
  fi

  local enabled_payload
  enabled_payload="$(curl -s "${BASE_URL}/api/enabled")"
  if grep -q "\"${TEST_SESSION}\"" <<<"${enabled_payload}"; then
    echo "Did not expect ${TEST_SESSION} in /api/enabled after disable"
    return 1
  fi
}

test_gated_session_rejected() {
  disable_session "${TEST_SESSION}" >/dev/null
  assert_http_code "404" "${BASE_URL}/session/${TEST_SESSION}"
}

test_gated_ws_rejected() {
  disable_session "${TEST_SESSION}" >/dev/null
  ws_expect_rejected "ws://127.0.0.1:${TEST_PORT}/ws?session=${TEST_SESSION}&cols=80&rows=24"
}

test_select_overlay_exists() {
  enable_session "${TEST_SESSION}" >/dev/null
  local html
  html="$(curl -s "${BASE_URL}/session/${TEST_SESSION}")"
  grep -q 'id="select-overlay"' <<<"${html}"
  grep -q 'id="select-btn"' <<<"${html}"
}

test_auth_token_required() {
  start_server "${AUTH_TOKEN}"
  assert_http_code "401" "${BASE_URL}/"
}

test_auth_token_accepted() {
  assert_http_code "200" "${BASE_URL}/?token=${AUTH_TOKEN}"
}

test_index_page_lists_sessions() {
  enable_session "${TEST_SESSION}" >/dev/null
  local html
  html="$(curl -s "${BASE_URL}/")"
  grep -q "${TEST_SESSION}" <<<"${html}"
}

main() {
  create_tmux_sessions
  start_server
  enable_session "${TEST_SESSION}" >/dev/null

  run_test test_server_starts
  run_test test_api_sessions_returns_json
  run_test test_enable_session
  run_test test_gated_session_rejected
  run_test test_gated_ws_rejected
  run_test test_api_sessions_lists_tmux
  run_test test_session_page_returns_html
  run_test test_select_overlay_exists
  run_test test_ws_connects_to_session
  run_test test_ws_invalid_session_rejected
  run_test test_session_prefix_guard
  run_test test_disable_session
  run_test test_enable_session
  run_test test_index_page_lists_sessions
  run_test test_auth_token_required
  run_test test_auth_token_accepted

  echo "All pilot tests passed."
}

main "$@"
