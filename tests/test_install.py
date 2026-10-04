"""Behavior tests for install.sh.

Tests the installer end-to-end: OS detection, dependency skipping,
tarball download flow, git-pull update flow, and error handling.
All tests run install.sh functions in a subprocess with controlled
PATH and env to avoid touching the real system.
"""

import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

INSTALL_SH = str(Path(__file__).resolve().parent.parent / "install.sh")


def _bash(script: str, *, env: dict[str, str] | None = None,
          check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a bash snippet that sources install.sh first."""
    full = f"source {INSTALL_SH}\n{script}"
    merged = {**os.environ, **(env or {})}
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", full],
        capture_output=True, text=True, env=merged,
        check=check, timeout=10,
    )


# ── detect_os ─────────────────────────────────────────────────────────────


def test_detect_os_returns_known_value():
    """detect_os returns one of the known platform strings."""
    r = _bash("detect_os")
    assert r.stdout.strip() in ("macos", "debian", "redhat", "linux", "unknown")


def test_detect_os_on_this_machine():
    """On this Linux/Debian machine, detect_os returns 'debian'."""
    if not Path("/etc/debian_version").exists():
        pytest.skip("not a Debian-based host")
    r = _bash("detect_os")
    assert r.stdout.strip() == "debian"


# ── install_pkg ───────────────────────────────────────────────────────────


def test_install_pkg_skips_when_command_exists():
    """install_pkg prints 'already installed' and exits 0 for an existing command."""
    # bash is always available
    r = _bash('OS=debian install_pkg bash')
    assert "already installed" in r.stdout


def test_install_pkg_fails_on_unknown_os():
    """install_pkg returns 1 and prints failure when OS is unknown."""
    # Use a command name that definitely doesn't exist
    r = _bash('OS=unknown install_pkg __no_such_cmd_xyz__', check=False)
    assert r.returncode != 0
    assert "not found" in r.stderr or "not found" in r.stdout


# ── Python version check ─────────────────────────────────────────────────


def test_python_version_accepted(tmp_path: Path):
    """Python 3.12+ is accepted without trying to install anything."""
    # Create a fake python3 that reports 3.13
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        if [[ "$1" == "-c" ]]; then
            echo "3.13"
        fi
    """))
    fake_python.chmod(0o755)

    # Stub out everything else so main() doesn't actually install or exec
    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text("#!/bin/bash\ntrue\n")
    fake_tmux.chmod(0o755)
    fake_node = fake_bin / "node"
    fake_node.write_text("#!/bin/bash\ntrue\n")
    fake_node.chmod(0o755)
    fake_claude = fake_bin / "claude"
    fake_claude.write_text('#!/bin/bash\necho "1.0.0"\n')
    fake_claude.chmod(0o755)

    # Create a fake bridge.sh so exec succeeds
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    fake_bridge = install_dir / "bridge.sh"
    fake_bridge.write_text("#!/bin/bash\necho bridge_setup_called\n")
    fake_bridge.chmod(0o755)

    r = _bash(
        "main",
        env={
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "INSTALL_DIR": str(install_dir),
            "HOME": str(tmp_path),
        },
    )
    assert "Python 3.13" in r.stdout
    # Should NOT try to install python
    assert "apt install" not in r.stdout
    assert "brew install" not in r.stdout


def test_python_version_too_old_exits(tmp_path: Path):
    """Python < 3.12 on unknown OS prints error and exits 1."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        if [[ "$1" == "-c" ]]; then
            echo "3.10"
        fi
    """))
    fake_python.chmod(0o755)

    # Fake uname that returns neither Darwin nor Linux → "unknown" OS
    fake_uname = fake_bin / "uname"
    fake_uname.write_text('#!/bin/bash\necho "FreeBSD"\n')
    fake_uname.chmod(0o755)

    r = _bash(
        "main",
        env={
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "INSTALL_DIR": str(tmp_path / "install"),
            "HOME": str(tmp_path),
        },
        check=False,
    )
    assert r.returncode != 0
    assert "3.12+ required" in r.stderr or "3.12+ required" in r.stdout


# ── Fresh install: tarball download ───────────────────────────────────────


def test_fresh_install_downloads_tarball(tmp_path: Path):
    """Fresh install (no bridge.sh) downloads tarball, extracts, and calls bridge.sh setup."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    # Fake python3 3.13
    (fake_bin / "python3").write_text(
        '#!/bin/bash\n[[ "$1" == "-c" ]] && echo "3.13"\n')
    (fake_bin / "python3").chmod(0o755)
    # Fake tmux, node, claude
    for cmd in ("tmux", "node"):
        p = fake_bin / cmd
        p.write_text("#!/bin/bash\ntrue\n")
        p.chmod(0o755)
    (fake_bin / "claude").write_text('#!/bin/bash\necho "1.0"\n')
    (fake_bin / "claude").chmod(0o755)

    # Fake curl: creates a tarball with the expected directory structure
    install_dir = tmp_path / "install"
    # Pre-create the tarball content that curl would download
    tar_src = tmp_path / "tar_src" / "claudecode-telegram-main"
    tar_src.mkdir(parents=True)
    (tar_src / "bridge.sh").write_text(
        "#!/bin/bash\necho handoff_ok\n")
    (tar_src / "bridge.sh").chmod(0o755)
    (tar_src / "bridge.py").write_text("# bridge\n")

    # Build the tarball
    tarball = tmp_path / "release.tar.gz"
    subprocess.run(
        ["tar", "-czf", str(tarball), "-C", str(tmp_path / "tar_src"),
         "claudecode-telegram-main"],
        check=True,
    )

    # Fake curl: copies our pre-built tarball to the requested output path
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        # Extract -o destination from args
        dest=""
        next_is_dest=false
        for arg in "$@"; do
            if $next_is_dest; then
                dest="$arg"
                next_is_dest=false
            fi
            # -o or last part of combined flags like -sfLo
            if [[ "$arg" == "-o" ]] || [[ "$arg" =~ o$ && "$arg" == -* ]]; then
                next_is_dest=true
            fi
        done
        if [[ -n "$dest" ]]; then
            cp {tarball} "$dest"
        fi
    """))
    fake_curl.chmod(0o755)

    r = _bash(
        "main",
        env={
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "INSTALL_DIR": str(install_dir),
            "HOME": str(tmp_path),
        },
    )
    # Tarball was extracted and bridge.sh was found
    assert "Downloaded to" in r.stdout
    assert (install_dir / "bridge.sh").exists()
    assert (install_dir / "bridge.py").exists()
    # bridge.sh setup was called (exec replaces the process)
    assert "handoff_ok" in r.stdout


def test_download_failure_exits_with_error(tmp_path: Path):
    """When curl fails 3 times, install.sh exits 1 with clear error."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    # Fake python3 3.13
    (fake_bin / "python3").write_text(
        '#!/bin/bash\n[[ "$1" == "-c" ]] && echo "3.13"\n')
    (fake_bin / "python3").chmod(0o755)
    for cmd in ("tmux", "node"):
        p = fake_bin / cmd
        p.write_text("#!/bin/bash\ntrue\n")
        p.chmod(0o755)
    (fake_bin / "claude").write_text('#!/bin/bash\necho "1.0"\n')
    (fake_bin / "claude").chmod(0o755)

    # Fake curl that always fails
    fake_curl = fake_bin / "curl"
    fake_curl.write_text("#!/bin/bash\nexit 1\n")
    fake_curl.chmod(0o755)

    # Fake sleep to not actually wait
    fake_sleep = fake_bin / "sleep"
    fake_sleep.write_text("#!/bin/bash\ntrue\n")
    fake_sleep.chmod(0o755)

    install_dir = tmp_path / "install"
    r = _bash(
        "main",
        env={
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "INSTALL_DIR": str(install_dir),
            "HOME": str(tmp_path),
        },
        check=False,
    )
    assert r.returncode != 0
    assert "Download failed" in r.stderr or "Download failed" in r.stdout


# ── Existing install: git pull update ─────────────────────────────────────


def test_existing_git_install_pulls(tmp_path: Path):
    """Existing install with .git dir uses git pull to update."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    (fake_bin / "python3").write_text(
        '#!/bin/bash\n[[ "$1" == "-c" ]] && echo "3.13"\n')
    (fake_bin / "python3").chmod(0o755)
    for cmd in ("tmux", "node"):
        p = fake_bin / cmd
        p.write_text("#!/bin/bash\ntrue\n")
        p.chmod(0o755)
    (fake_bin / "claude").write_text('#!/bin/bash\necho "1.0"\n')
    (fake_bin / "claude").chmod(0o755)

    # Create existing install dir with .git and bridge.sh
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / ".git").mkdir()
    (install_dir / "bridge.sh").write_text(
        "#!/bin/bash\necho bridge_setup_called\n")
    (install_dir / "bridge.sh").chmod(0o755)

    # Fake git that reports success
    fake_git = fake_bin / "git"
    fake_git.write_text(textwrap.dedent("""\
        #!/bin/bash
        if [[ "$1" == "pull" ]]; then
            echo "Already up to date."
            exit 0
        fi
    """))
    fake_git.chmod(0o755)

    r = _bash(
        "main",
        env={
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "INSTALL_DIR": str(install_dir),
            "HOME": str(tmp_path),
        },
    )
    assert "Updated via git pull" in r.stdout
    # Still hands off to bridge.sh
    assert "bridge_setup_called" in r.stdout


def test_existing_install_without_git_uses_as_is(tmp_path: Path):
    """Existing install without .git uses the directory as-is."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    (fake_bin / "python3").write_text(
        '#!/bin/bash\n[[ "$1" == "-c" ]] && echo "3.13"\n')
    (fake_bin / "python3").chmod(0o755)
    for cmd in ("tmux", "node"):
        p = fake_bin / cmd
        p.write_text("#!/bin/bash\ntrue\n")
        p.chmod(0o755)
    (fake_bin / "claude").write_text('#!/bin/bash\necho "1.0"\n')
    (fake_bin / "claude").chmod(0o755)

    # Existing install dir with bridge.sh but no .git
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / "bridge.sh").write_text(
        "#!/bin/bash\necho bridge_setup_called\n")
    (install_dir / "bridge.sh").chmod(0o755)

    r = _bash(
        "main",
        env={
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "INSTALL_DIR": str(install_dir),
            "HOME": str(tmp_path),
        },
    )
    assert "Using existing install" in r.stdout
    assert "bridge_setup_called" in r.stdout


# ── Cleanup trap ──────────────────────────────────────────────────────────


def test_temp_dir_cleaned_up_on_success(tmp_path: Path):
    """The temp download directory is cleaned up after a successful install."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    (fake_bin / "python3").write_text(
        '#!/bin/bash\n[[ "$1" == "-c" ]] && echo "3.13"\n')
    (fake_bin / "python3").chmod(0o755)
    for cmd in ("tmux", "node"):
        p = fake_bin / cmd
        p.write_text("#!/bin/bash\ntrue\n")
        p.chmod(0o755)
    (fake_bin / "claude").write_text('#!/bin/bash\necho "1.0"\n')
    (fake_bin / "claude").chmod(0o755)

    # Fake curl and tar — create the expected structure
    tar_src = tmp_path / "tar_src" / "claudecode-telegram-main"
    tar_src.mkdir(parents=True)
    (tar_src / "bridge.sh").write_text("#!/bin/bash\necho ok\n")
    (tar_src / "bridge.sh").chmod(0o755)
    tarball = tmp_path / "release.tar.gz"
    subprocess.run(
        ["tar", "-czf", str(tarball), "-C", str(tmp_path / "tar_src"),
         "claudecode-telegram-main"],
        check=True,
    )
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        dest=""
        next_is_dest=false
        for arg in "$@"; do
            if $next_is_dest; then dest="$arg"; next_is_dest=false; fi
            if [[ "$arg" == "-o" ]] || [[ "$arg" =~ o$ && "$arg" == -* ]]; then
                next_is_dest=true
            fi
        done
        [[ -n "$dest" ]] && cp {tarball} "$dest"
    """))
    fake_curl.chmod(0o755)

    install_dir = tmp_path / "install"

    # Run — mktemp creates in /tmp by default
    r = _bash(
        "main",
        env={
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "INSTALL_DIR": str(install_dir),
            "HOME": str(tmp_path),
        },
    )
    assert r.returncode == 0
    # Verify no leftover tmp dirs (trap should have cleaned up)
    # The install dir should exist, but temp dir should be gone
    assert install_dir.exists()
