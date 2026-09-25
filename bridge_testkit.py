"""Bridge test harness — ready objects for behavior tests.

Usage:
    from bridge_testkit import BridgeHarness
    import bridge

    def test_something():
        with BridgeHarness() as h:
            w = h.worker("alice", cwd="/home/claude/project-a")
            # ... call bridge functions, assert behavior ...

BridgeHarness patches all bridge globals (SESSIONS_DIR, NODE_DIR, etc.)
and DI seams (_clock, _subprocess_runner, _urlopen) for the duration of
the `with` block.  Cleanup is automatic — globals are restored and the
temp filesystem is removed even if the test raises.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.request import Request

import bridge


# ---------------------------------------------------------------------------
# Fake DI seams
# ---------------------------------------------------------------------------

class FakeClock:
    """Deterministic clock.  Starts at a fixed epoch, advance manually."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeHTTPResponse:
    """Minimal urllib response stand-in."""

    def __init__(self, body: bytes = b'{"ok":true,"result":{}}', status: int = 200):
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


@dataclass
class CapturedRequest:
    url: str
    data: bytes | None
    method: str
    headers: dict[str, str]


class FakeTelegram:
    """Captures urlopen calls instead of hitting the Telegram API."""

    def __init__(self):
        self.requests: list[CapturedRequest] = []
        self.response_body = b'{"ok":true,"result":{}}'
        self.response_status = 200

    def urlopen(self, req: Request | str, **kwargs) -> FakeHTTPResponse:
        if isinstance(req, str):
            self.requests.append(CapturedRequest(url=req, data=None, method="GET", headers={}))
        else:
            self.requests.append(CapturedRequest(
                url=req.full_url,
                data=req.data,
                method=req.get_method(),
                headers=dict(req.headers),
            ))
        return FakeHTTPResponse(self.response_body, self.response_status)

    def sent_messages(self) -> list[dict]:
        """Parse captured sendMessage requests into dicts."""
        msgs = []
        for r in self.requests:
            if "sendMessage" in r.url and r.data:
                try:
                    msgs.append(json.loads(r.data))
                except (json.JSONDecodeError, TypeError):
                    pass
        return msgs

    def clear(self) -> None:
        self.requests.clear()


class FakeSubprocessRunner:
    """Captures subprocess.run / Popen calls.

    By default returns success (rc=0, empty stdout/stderr).
    Register custom responses with `stub(args_prefix, result)`.
    """

    def __init__(self):
        self.calls: list[dict] = []
        self._stubs: list[tuple[list[str], subprocess.CompletedProcess]] = []

    def stub(self, args_prefix: list[str], result: subprocess.CompletedProcess) -> None:
        """Register a canned response for calls whose args start with *args_prefix*."""
        self._stubs.append((args_prefix, result))

    def run(self, args, **kwargs) -> subprocess.CompletedProcess:
        self.calls.append({"args": args, "kwargs": kwargs})
        # Check stubs (most-recently-added wins)
        for prefix, result in reversed(self._stubs):
            if list(args[:len(prefix)]) == prefix:
                return result
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    def popen(self, args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs, "popen": True})

        class FakePopen:
            pid = 99999
            returncode = 0
            stdin = io.BytesIO()
            stdout = io.BytesIO(b"")
            stderr = io.BytesIO(b"")
            def poll(self): return 0
            def wait(self, timeout=None): return 0
            def communicate(self, input=None, timeout=None): return b"", b""
            def kill(self): pass
            def terminate(self): pass

        return FakePopen()


# ---------------------------------------------------------------------------
# Worker fixture
# ---------------------------------------------------------------------------

@dataclass
class WorkerFixture:
    """A test worker with its filesystem and state pre-wired."""
    name: str
    tmux_name: str
    dir: Path
    cwd: str
    session_id: str = ""

    def set_session_id(self, sid: str) -> None:
        """Cache a session_id bound to this worker's current CWD."""
        bridge._cache_session_id(self.name, sid)
        self.session_id = sid

    def get_session_id(self) -> str:
        return bridge.get_claude_session_id(self.name)

    def get_cwd(self) -> str:
        return bridge.get_claude_session_cwd(self.name) or ""

    def change_cwd(self, new_cwd: str) -> None:
        """Simulate a CWD change (what checkin does)."""
        bridge.save_claude_session_cwd(self.name, new_cwd)
        self.cwd = new_cwd


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class BridgeHarness:
    """Context manager that isolates bridge.py globals for testing.

    Patches SESSIONS_DIR, NODE_DIR, TMUX_PREFIX, SANDBOX_ENABLED,
    _CLAUDE_JSON_PATH, _clock, _subprocess_runner, _urlopen.
    All are restored on exit.  A temp filesystem is created and cleaned up.

        with BridgeHarness() as h:
            w = h.worker("alice", cwd="/home/claude/project-a")
            assert bridge.get_claude_session_cwd("alice") == "/home/claude/project-a"
    """

    def __init__(self, *, tmux_prefix: str = "claude-test-"):
        self.root = Path(tempfile.mkdtemp(prefix="bridge-test-"))
        self.sessions_dir = self.root / "sessions"
        self.node_dir = self.root / "node"
        self.claude_json = self.root / "claude.json"
        self.tmux_prefix = tmux_prefix
        self.clock = FakeClock()
        self.telegram = FakeTelegram()
        self.subprocess = FakeSubprocessRunner()
        self._saved: dict[str, Any] = {}

    def __enter__(self) -> BridgeHarness:
        self.sessions_dir.mkdir()
        self.node_dir.mkdir()
        self.claude_json.write_text(json.dumps({"projects": {}}))

        self._patch("SESSIONS_DIR", self.sessions_dir)
        self._patch("NODE_DIR", self.node_dir)
        self._patch("TMUX_PREFIX", self.tmux_prefix)
        self._patch("SANDBOX_ENABLED", False)
        self._patch("_CLAUDE_JSON_PATH", self.claude_json)
        self._patch("_clock", self.clock)
        self._patch("_urlopen", self.telegram.urlopen)
        self._patch("_subprocess_runner", self.subprocess)
        return self

    def __exit__(self, exc_type, exc, tb):
        for attr, value in reversed(list(self._saved.items())):
            setattr(bridge, attr, value)
        shutil.rmtree(self.root, ignore_errors=True)

    def _patch(self, attr: str, value: Any) -> None:
        self._saved[attr] = getattr(bridge, attr)
        setattr(bridge, attr, value)

    # -- Ready objects -------------------------------------------------------

    def worker(self, name: str, *, cwd: str = "/home/claude/default") -> WorkerFixture:
        """Create a worker with session dir, CWD file, and optional session_id."""
        worker_dir = self.sessions_dir / name
        worker_dir.mkdir(parents=True, exist_ok=True)
        bridge.save_claude_session_cwd(name, cwd)
        tmux_name = f"{self.tmux_prefix}{name}"
        return WorkerFixture(
            name=name,
            tmux_name=tmux_name,
            dir=worker_dir,
            cwd=cwd,
        )

    def trusted_dirs(self) -> dict:
        """Read the projects from the fake claude.json."""
        data = json.loads(self.claude_json.read_text())
        return data.get("projects", {})


# ---------------------------------------------------------------------------
# Simple test runner (used by test.sh)
# ---------------------------------------------------------------------------

def run_tests(test_module: dict, *, filter_pattern: str = "") -> tuple[int, int]:
    """Run all functions named test_* in *test_module*.

    Returns (passed, failed).  Prints results and tracebacks.
    """
    import traceback
    tests = sorted(k for k in test_module if k.startswith("test_"))
    if filter_pattern:
        tests = [t for t in tests if filter_pattern in t]

    passed = failed = 0
    for name in tests:
        try:
            test_module[name]()
            passed += 1
            print(f"  \033[32m✓\033[0m {name}")
        except Exception:
            failed += 1
            print(f"  \033[31m✗\033[0m {name}")
            traceback.print_exc()
    return passed, failed
