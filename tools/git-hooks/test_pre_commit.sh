#!/usr/bin/env bash
# Behavior tests for tools/git-hooks/pre-commit secret scanner.
# Run: bash tools/git-hooks/test_pre_commit.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="$SCRIPT_DIR/pre-commit"
PASS=0
FAIL=0

# ── Helpers ──────────────────────────────────────────────────────────────────

init_repo() {
  local dir="$1"
  git init "$dir" --quiet
  git -C "$dir" config user.email "test@test.com"
  git -C "$dir" config user.name "Test"
  echo "init" > "$dir/README.md"
  git -C "$dir" add README.md
  git -C "$dir" commit -m "init" --no-verify --quiet
}

make_mock_trufflehog() {
  local dir="$1"
  local exit_code="${2:-0}"
  local stdout="${3:-}"
  local script="$dir/mock-trufflehog"
  cat > "$script" <<SCRIPT
#!/usr/bin/env bash
echo -n '$stdout'
exit $exit_code
SCRIPT
  chmod +x "$script"
  echo "$script"
}

make_logging_mock() {
  local dir="$1"
  local exit_code="${2:-0}"
  local script="$dir/mock-trufflehog"
  local logfile="$dir/trufflehog-args.log"
  cat > "$script" <<SCRIPT
#!/usr/bin/env bash
printf "%s\n" "\$@" > "$logfile"
exit $exit_code
SCRIPT
  chmod +x "$script"
  echo "$script"
}

make_content_checking_mock() {
  # Mock that detects "supersecret" in scanned files, exits 183 if found
  local dir="$1"
  local script="$dir/mock-trufflehog"
  cat > "$script" <<'SCRIPT'
#!/usr/bin/env bash
scandir="$2"
if grep -rq "supersecret" "$scandir" 2>/dev/null; then
  echo '{"DetectorName":"Generic","file":"config.env"}'
  exit 183
fi
exit 0
SCRIPT
  chmod +x "$script"
  echo "$script"
}

make_nested_file_mock() {
  # Mock that checks a nested file exists in the scanned dir
  local dir="$1"
  local expected_path="$2"
  local script="$dir/mock-trufflehog"
  cat > "$script" <<SCRIPT
#!/usr/bin/env bash
scandir="\$2"
if [ -f "\$scandir/$expected_path" ]; then
  exit 0
fi
exit 183
SCRIPT
  chmod +x "$script"
  echo "$script"
}

run_hook() {
  local repo="$1"
  shift
  local env_args=()
  while [[ $# -gt 0 ]]; do
    env_args+=("$1")
    shift
  done
  env "${env_args[@]}" bash "$HOOK" 2>&1
}

run_test() {
  local name="$1"
  echo -n "  $name ... "
  if "$name"; then
    echo "PASS"
    ((PASS++))
  else
    echo "FAIL"
    ((FAIL++))
  fi
}

# ── Tests ────────────────────────────────────────────────────────────────────

test_hook_skips_when_trufflehog_missing() {
  # When trufflehog binary doesn't exist, hook warns and allows commit
  local tmp
  tmp=$(mktemp -d)
  trap "rm -rf '$tmp'" RETURN

  init_repo "$tmp"
  echo "AKIA1234567890ABCDEF" > "$tmp/secret.txt"
  git -C "$tmp" add secret.txt

  local output
  local rc=0
  output=$(cd "$tmp" && TRUFFLEHOG="/nonexistent/trufflehog-999" bash "$HOOK" 2>&1) || rc=$?

  [[ $rc -eq 0 ]] || { echo "Expected exit 0, got $rc"; return 1; }
  echo "$output" | grep -q "skipping secret scan" || { echo "Expected 'skipping secret scan'"; return 1; }
}

test_hook_passes_with_no_staged_files() {
  # When nothing is staged, hook exits 0 immediately
  local tmp
  tmp=$(mktemp -d)
  trap "rm -rf '$tmp'" RETURN

  init_repo "$tmp"
  local mock
  mock=$(make_mock_trufflehog "$tmp" 183 "should not run")

  local output rc=0
  output=$(cd "$tmp" && TRUFFLEHOG="$mock" bash "$HOOK" 2>&1) || rc=$?

  [[ $rc -eq 0 ]] || { echo "Expected exit 0, got $rc"; return 1; }
  if echo "$output" | grep -q "SECRET DETECTED"; then
    echo "Trufflehog should not have been called"
    return 1
  fi
}

test_hook_passes_clean_file() {
  # When trufflehog finds no secrets (exit 0), commit is allowed
  local tmp
  tmp=$(mktemp -d)
  trap "rm -rf '$tmp'" RETURN

  init_repo "$tmp"
  echo "print('hello world')" > "$tmp/clean.py"
  git -C "$tmp" add clean.py

  local mock
  mock=$(make_mock_trufflehog "$tmp" 0)

  local output
  output=$(cd "$tmp" && TRUFFLEHOG="$mock" bash "$HOOK" 2>&1)
  local rc=$?

  [[ $rc -eq 0 ]] || { echo "Expected exit 0, got $rc"; return 1; }
}

test_hook_blocks_on_secret_detected() {
  # When trufflehog exits 183, commit is blocked with clear error
  local tmp
  tmp=$(mktemp -d)
  trap "rm -rf '$tmp'" RETURN

  init_repo "$tmp"
  echo 'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"' > "$tmp/config.py"
  git -C "$tmp" add config.py

  local json_output='{"DetectorName":"AWS","file":"config.py"}'
  local mock
  mock=$(make_mock_trufflehog "$tmp" 183 "$json_output")

  local output rc=0
  output=$(cd "$tmp" && TRUFFLEHOG="$mock" bash "$HOOK" 2>&1) || rc=$?

  [[ $rc -eq 1 ]] || { echo "Expected exit 1, got $rc"; return 1; }
  echo "$output" | grep -q "SECRET DETECTED" || { echo "Expected 'SECRET DETECTED'"; return 1; }
  echo "$output" | grep -q "commit blocked" || { echo "Expected 'commit blocked'"; return 1; }
  echo "$output" | grep -q "AWS" || { echo "Expected detector name 'AWS'"; return 1; }
  echo "$output" | grep -q "config.py" || { echo "Expected file name 'config.py'"; return 1; }
  echo "$output" | grep -q "git commit --no-verify" || { echo "Expected bypass hint"; return 1; }
}

test_hook_respects_trufflehogignore() {
  # When .trufflehogignore exists, --exclude-paths flag is passed
  local tmp
  tmp=$(mktemp -d)
  trap "rm -rf '$tmp'" RETURN

  init_repo "$tmp"
  echo "x = 1" > "$tmp/app.py"
  git -C "$tmp" add app.py

  # Create .trufflehogignore
  echo -e "vendor/\n*.test.js" > "$tmp/.trufflehogignore"

  local mock
  mock=$(make_logging_mock "$tmp" 0)
  local logfile="$tmp/trufflehog-args.log"

  cd "$tmp" && TRUFFLEHOG="$mock" bash "$HOOK" >/dev/null 2>&1 || true

  grep -q "\-\-exclude-paths=" "$logfile" || { echo "Expected --exclude-paths in args"; return 1; }
}

test_hook_no_trufflehogignore_no_exclude_flag() {
  # When .trufflehogignore does NOT exist, --exclude-paths is NOT passed
  local tmp
  tmp=$(mktemp -d)
  trap "rm -rf '$tmp'" RETURN

  init_repo "$tmp"
  echo "x = 1" > "$tmp/app.py"
  git -C "$tmp" add app.py

  rm -f "$tmp/.trufflehogignore"

  local mock
  mock=$(make_logging_mock "$tmp" 0)
  local logfile="$tmp/trufflehog-args.log"

  cd "$tmp" && TRUFFLEHOG="$mock" bash "$HOOK" >/dev/null 2>&1 || true

  if grep -q "exclude-paths" "$logfile" 2>/dev/null; then
    echo "Should NOT pass --exclude-paths when no ignore file"
    return 1
  fi
}

test_hook_scans_staged_content_not_working_tree() {
  # Hook scans git-staged version, not the dirty working tree
  local tmp
  tmp=$(mktemp -d)
  trap "rm -rf '$tmp'" RETURN

  init_repo "$tmp"

  # Stage clean content
  echo "DB_HOST=localhost" > "$tmp/config.env"
  git -C "$tmp" add config.env

  # Dirty the working tree (but don't stage)
  echo -e "DB_HOST=localhost\nAWS_SECRET=supersecret" > "$tmp/config.env"

  local mock
  mock=$(make_content_checking_mock "$tmp")

  local output
  output=$(cd "$tmp" && TRUFFLEHOG="$mock" bash "$HOOK" 2>&1)
  local rc=$?

  # Staged version is clean → hook should pass
  [[ $rc -eq 0 ]] || { echo "Hook should scan staged content, not working tree (exit $rc)"; return 1; }
}

test_hook_handles_nested_directory_files() {
  # Hook correctly copies files in subdirectories via mkdir -p
  local tmp
  tmp=$(mktemp -d)
  trap "rm -rf '$tmp'" RETURN

  init_repo "$tmp"

  mkdir -p "$tmp/src/config"
  echo "DEBUG = True" > "$tmp/src/config/settings.py"
  git -C "$tmp" add src/config/settings.py

  local mock
  mock=$(make_nested_file_mock "$tmp" "src/config/settings.py")

  local output
  output=$(cd "$tmp" && TRUFFLEHOG="$mock" bash "$HOOK" 2>&1)
  local rc=$?

  [[ $rc -eq 0 ]] || { echo "Hook should handle nested dirs (exit $rc)"; return 1; }
}

test_hook_passes_on_trufflehog_crash() {
  # BUG DOCUMENTATION: When trufflehog exits non-zero but NOT 183 (e.g. crash,
  # segfault, bad args), the hook silently passes (fail-open). The hook only
  # checks exit_code == 183, so any other failure falls through to exit 0.
  # This test documents the current (buggy) fail-open behavior.
  local tmp
  tmp=$(mktemp -d)
  trap "rm -rf '$tmp'" RETURN

  init_repo "$tmp"
  echo "AKIA1234567890ABCDEF" > "$tmp/secret.txt"
  git -C "$tmp" add secret.txt

  # Mock trufflehog that crashes with exit code 1 (not 183)
  local mock
  mock=$(make_mock_trufflehog "$tmp" 1 "trufflehog: segfault")

  local output rc=0
  output=$(cd "$tmp" && TRUFFLEHOG="$mock" bash "$HOOK" 2>&1) || rc=$?

  # Current behavior: hook passes (exit 0) on non-183 errors — fail-open bug
  [[ $rc -eq 0 ]] || { echo "Expected exit 0 (fail-open bug), got $rc"; return 1; }
  # Hook should NOT show SECRET DETECTED since exit code wasn't 183
  if echo "$output" | grep -q "SECRET DETECTED"; then
    echo "Should not show SECRET DETECTED for non-183 exit"
    return 1
  fi
}

test_hook_handles_filenames_with_spaces() {
  # Files with spaces in their names are staged and scanned correctly
  local tmp
  tmp=$(mktemp -d)
  trap "rm -rf '$tmp'" RETURN

  init_repo "$tmp"
  echo "DB_PASSWORD=hunter2" > "$tmp/my config.env"
  git -C "$tmp" add "my config.env"

  # Mock that verifies the spaced filename exists in the scan directory
  local script="$tmp/mock-trufflehog"
  cat > "$script" <<'SCRIPT'
#!/usr/bin/env bash
scandir="$2"
if [ -f "$scandir/my config.env" ]; then
  exit 0
fi
# File not found — spaces broke the copy
exit 183
SCRIPT
  chmod +x "$script"

  local output rc=0
  output=$(cd "$tmp" && TRUFFLEHOG="$script" bash "$HOOK" 2>&1) || rc=$?

  [[ $rc -eq 0 ]] || { echo "Hook should handle filenames with spaces (exit $rc)"; return 1; }
}

test_hook_cleans_up_tmpdir() {
  # After hook runs, the temporary staged-copy directory is removed
  local tmp
  tmp=$(mktemp -d)
  trap "rm -rf '$tmp'" RETURN

  init_repo "$tmp"
  echo "clean file" > "$tmp/app.py"
  git -C "$tmp" add app.py

  # Mock that records the tmpdir path it receives
  local script="$tmp/mock-trufflehog"
  local scandir_log="$tmp/scandir.log"
  cat > "$script" <<SCRIPT
#!/usr/bin/env bash
echo "\$2" > "$scandir_log"
exit 0
SCRIPT
  chmod +x "$script"

  cd "$tmp" && TRUFFLEHOG="$script" bash "$HOOK" >/dev/null 2>&1 || true

  [[ -f "$scandir_log" ]] || { echo "Mock didn't record scandir"; return 1; }
  local scandir
  scandir=$(cat "$scandir_log")
  if [ -d "$scandir" ]; then
    echo "Tmpdir $scandir still exists — hook failed to clean up"
    return 1
  fi
}

# ── Main ─────────────────────────────────────────────────────────────────────

echo "=== Pre-commit hook behavior tests ==="
echo ""

run_test test_hook_skips_when_trufflehog_missing
run_test test_hook_passes_with_no_staged_files
run_test test_hook_passes_clean_file
run_test test_hook_blocks_on_secret_detected
run_test test_hook_respects_trufflehogignore
run_test test_hook_no_trufflehogignore_no_exclude_flag
run_test test_hook_scans_staged_content_not_working_tree
run_test test_hook_handles_nested_directory_files
run_test test_hook_passes_on_trufflehog_crash
run_test test_hook_handles_filenames_with_spaces
run_test test_hook_cleans_up_tmpdir

echo ""
echo "Results: $PASS passed, $FAIL failed"

if [[ $FAIL -gt 0 ]]; then
  exit 1
fi
echo "All pre-commit hook tests passed."
