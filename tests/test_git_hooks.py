"""Behavior tests for tools/git-hooks/pre-commit secret scanner."""

import os
import stat
import subprocess
import sys

import pytest

HOOK_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools", "git-hooks", "pre-commit",
)


def _init_repo(path):
    """Create a minimal git repo with an initial commit so staging works."""
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(path), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path), capture_output=True, check=True,
    )
    # Need an initial commit so that git diff --cached works correctly
    readme = path / "README.md"
    readme.write_text("init\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init", "--no-verify"],
        cwd=str(path), capture_output=True, check=True,
    )


def _run_hook(repo_path, env_override=None):
    """Run the pre-commit hook in the given repo and return the CompletedProcess."""
    env = os.environ.copy()
    # Prevent the real user's git hooks path from interfering
    env.pop("GIT_HOOKS_PATH", None)
    if env_override:
        env.update(env_override)
    return subprocess.run(
        ["bash", HOOK_PATH],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        env=env,
    )


def _make_mock_trufflehog(path, exit_code=0, stdout=""):
    """Create a mock trufflehog script that exits with the given code and prints stdout."""
    script = path / "mock-trufflehog"
    # Write stdout to a temp file and cat it to avoid quoting issues in bash
    stdout_file = path / "mock-trufflehog-stdout.txt"
    stdout_file.write_text(stdout)
    script.write_text(
        f'#!/usr/bin/env bash\n'
        f'cat "{stdout_file}"\n'
        f'exit {exit_code}\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _make_logging_mock_trufflehog(path, exit_code=0, stdout=""):
    """Create a mock trufflehog that logs all args to a file, then exits."""
    log_file = path / "trufflehog-args.log"
    script = path / "mock-trufflehog"
    # Write all arguments to the log file, one per line
    script.write_text(
        '#!/usr/bin/env bash\n'
        f'printf "%s\\n" "$@" > "{log_file}"\n'
        f'echo -n "{stdout}"\n'
        f'exit {exit_code}\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script), str(log_file)


# ── Tests ────────────────────────────────────────────────────────────────────


def test_hook_skips_when_trufflehog_missing(tmp_path):
    """When trufflehog binary doesn't exist, hook warns and allows commit."""
    _init_repo(tmp_path)
    # Stage a file
    f = tmp_path / "secret.txt"
    f.write_text("AKIA1234567890ABCDEF\n")
    subprocess.run(["git", "add", "secret.txt"], cwd=str(tmp_path), capture_output=True)

    result = _run_hook(tmp_path, env_override={
        "TRUFFLEHOG": "/nonexistent/path/trufflehog-does-not-exist",
    })
    assert result.returncode == 0, f"Hook should exit 0 when trufflehog missing: {result.stderr}"
    assert "skipping secret scan" in result.stdout


def test_hook_passes_with_no_staged_files(tmp_path):
    """When nothing is staged, hook exits 0 immediately."""
    _init_repo(tmp_path)
    mock = _make_mock_trufflehog(tmp_path, exit_code=183, stdout="should not run")
    result = _run_hook(tmp_path, env_override={"TRUFFLEHOG": mock})
    assert result.returncode == 0, f"Hook should exit 0 with no staged files: {result.stderr}"
    # trufflehog should not even have been called
    assert "SECRET DETECTED" not in result.stdout


def test_hook_passes_clean_file(tmp_path):
    """When trufflehog finds no secrets (exit 0), commit is allowed."""
    _init_repo(tmp_path)
    f = tmp_path / "clean.py"
    f.write_text("print('hello world')\n")
    subprocess.run(["git", "add", "clean.py"], cwd=str(tmp_path), capture_output=True)

    mock = _make_mock_trufflehog(tmp_path, exit_code=0)
    result = _run_hook(tmp_path, env_override={"TRUFFLEHOG": mock})
    assert result.returncode == 0, f"Hook should pass on clean file: {result.stderr}"
    assert "SECRET DETECTED" not in result.stdout


def test_hook_blocks_on_secret_detected(tmp_path):
    """When trufflehog exits 183, commit is blocked with clear error output."""
    _init_repo(tmp_path)
    f = tmp_path / "config.py"
    f.write_text('AWS_KEY = "AKIAIOSFODNN7EXAMPLE"\n')
    subprocess.run(["git", "add", "config.py"], cwd=str(tmp_path), capture_output=True)

    # Mock trufflehog that returns exit 183 with JSON output matching real format
    json_output = '{"DetectorName":"AWS","file":"config.py"}'
    mock = _make_mock_trufflehog(tmp_path, exit_code=183, stdout=json_output)
    result = _run_hook(tmp_path, env_override={"TRUFFLEHOG": mock})

    assert result.returncode == 1, f"Hook should block commit on secret: {result.stderr}"
    assert "SECRET DETECTED" in result.stdout
    assert "commit blocked" in result.stdout
    assert "AWS" in result.stdout, "Should show detector name"
    assert "config.py" in result.stdout, "Should show file path"
    assert "git commit --no-verify" in result.stdout, "Should mention bypass option"


def test_hook_respects_trufflehogignore(tmp_path):
    """When .trufflehogignore exists, --exclude-paths flag is passed to trufflehog."""
    _init_repo(tmp_path)
    f = tmp_path / "app.py"
    f.write_text("x = 1\n")
    subprocess.run(["git", "add", "app.py"], cwd=str(tmp_path), capture_output=True)

    # Create .trufflehogignore
    ignore = tmp_path / ".trufflehogignore"
    ignore.write_text("vendor/\n*.test.js\n")

    mock, log_file = _make_logging_mock_trufflehog(tmp_path, exit_code=0)
    result = _run_hook(tmp_path, env_override={"TRUFFLEHOG": mock})
    assert result.returncode == 0, f"Hook should pass: {result.stderr}"

    logged_args = open(log_file).read()
    assert f"--exclude-paths={ignore}" in logged_args, (
        f"Should pass --exclude-paths to trufflehog, got args:\n{logged_args}"
    )


def test_hook_no_trufflehogignore_no_exclude_flag(tmp_path):
    """When .trufflehogignore does NOT exist, --exclude-paths is NOT passed."""
    _init_repo(tmp_path)
    f = tmp_path / "app.py"
    f.write_text("x = 1\n")
    subprocess.run(["git", "add", "app.py"], cwd=str(tmp_path), capture_output=True)

    # Ensure no .trufflehogignore
    ignore = tmp_path / ".trufflehogignore"
    if ignore.exists():
        ignore.unlink()

    mock, log_file = _make_logging_mock_trufflehog(tmp_path, exit_code=0)
    result = _run_hook(tmp_path, env_override={"TRUFFLEHOG": mock})
    assert result.returncode == 0

    logged_args = open(log_file).read()
    assert "--exclude-paths" not in logged_args, (
        f"Should NOT pass --exclude-paths when no ignore file, got:\n{logged_args}"
    )


def test_hook_copies_staged_content_not_working_tree(tmp_path):
    """Hook scans the staged version of a file, not the working tree version.

    If the working tree has a secret but the staged version doesn't, hook should pass.
    If the staged version has a secret but working tree was cleaned, hook should still catch it.
    This test verifies the hook uses `git show :file` to get staged content.
    """
    _init_repo(tmp_path)

    # Stage a clean file
    f = tmp_path / "config.env"
    f.write_text("DB_HOST=localhost\n")
    subprocess.run(["git", "add", "config.env"], cwd=str(tmp_path), capture_output=True)

    # Now modify working tree (but don't stage the modification)
    f.write_text("DB_HOST=localhost\nAWS_SECRET=supersecret\n")

    # Mock trufflehog that inspects the content it receives and fails only if it
    # sees "supersecret" in any file under the tmpdir
    script = tmp_path / "mock-trufflehog"
    script.write_text(
        '#!/usr/bin/env bash\n'
        'dir="$2"\n'  # filesystem <dir> is argv[2] (after "filesystem")
        'if grep -rq "supersecret" "$dir" 2>/dev/null; then\n'
        '  echo \'{"DetectorName":"Generic","file":"config.env"}\'\n'
        '  exit 183\n'
        'fi\n'
        'exit 0\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    result = _run_hook(tmp_path, env_override={"TRUFFLEHOG": str(script)})
    # The staged version is clean, so hook should pass even though working tree is dirty
    assert result.returncode == 0, (
        f"Hook should scan staged content, not working tree. stderr: {result.stderr}"
    )


def test_hook_handles_nested_directory_files(tmp_path):
    """Hook correctly stages files in subdirectories (mkdir -p for nested paths)."""
    _init_repo(tmp_path)

    nested_dir = tmp_path / "src" / "config"
    nested_dir.mkdir(parents=True)
    f = nested_dir / "settings.py"
    f.write_text("DEBUG = True\n")
    subprocess.run(
        ["git", "add", "src/config/settings.py"],
        cwd=str(tmp_path), capture_output=True,
    )

    # Mock trufflehog that verifies the file exists in the tmpdir it receives
    script = tmp_path / "mock-trufflehog"
    script.write_text(
        '#!/usr/bin/env bash\n'
        'dir="$2"\n'
        'if [ -f "$dir/src/config/settings.py" ]; then\n'
        '  exit 0\n'
        'fi\n'
        '# File not found — indicates hook failed to copy nested file\n'
        'exit 183\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    result = _run_hook(tmp_path, env_override={"TRUFFLEHOG": str(script)})
    assert result.returncode == 0, (
        f"Hook should correctly handle nested dirs. stderr: {result.stderr}"
    )
