#!/usr/bin/env python3
"""E2E tests for the PR review Bun server (src/serve.ts).

Tests token auth, HTML serving, TTL self-destruct, and API proxy
auth enforcement.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Generator
from pathlib import Path

import pytest

SERVE_SCRIPT = os.path.join(os.path.dirname(__file__), "src", "serve.ts")
TOKEN = "test-token-abc123"
HTML_CONTENT = "<!DOCTYPE html><html><body>PR #42 review</body></html>"


@pytest.fixture
def html_file(tmp_path: Path) -> str:
    p = tmp_path / "pr-review-42.html"
    p.write_text(HTML_CONTENT)
    return str(p)


@pytest.fixture
def server(html_file: str) -> Generator[tuple[int, str], None, None]:
    """Spawn the Bun review server and yield (port, token). Kill on teardown."""
    port = 10178
    proc = subprocess.Popen(
        [
            "bun",
            SERVE_SCRIPT,
            "--port",
            str(port),
            "--token",
            TOKEN,
            "--ttl",
            "30",
            "--html-path",
            html_file,
            "--owner",
            "TestOwner",
            "--repo",
            "test-repo",
            "--pr-num",
            "42",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Wait for server to start
    for _ in range(20):
        time.sleep(0.15)
        try:
            req = urllib.request.Request(f"http://localhost:{port}/health")
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    break
        except (urllib.error.URLError, OSError):
            continue
    else:
        proc.kill()
        pytest.fail("Server did not start in time")

    yield port, TOKEN

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


class TestTokenAuth:
    def test_no_token_returns_403(self, server: tuple[int, str]) -> None:
        port, _ = server
        req = urllib.request.Request(f"http://localhost:{port}/")
        try:
            urllib.request.urlopen(req, timeout=5)
            pytest.fail("Expected 403")
        except urllib.error.HTTPError as e:
            assert e.code == 403

    def test_wrong_token_returns_403(self, server: tuple[int, str]) -> None:
        port, _ = server
        req = urllib.request.Request(f"http://localhost:{port}/?token=wrong")
        try:
            urllib.request.urlopen(req, timeout=5)
            pytest.fail("Expected 403")
        except urllib.error.HTTPError as e:
            assert e.code == 403

    def test_valid_token_returns_html(self, server: tuple[int, str]) -> None:
        port, token = server
        req = urllib.request.Request(f"http://localhost:{port}/?token={token}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            body = resp.read().decode()
            assert "PR #42 review" in body

    def test_health_endpoint_no_token(self, server: tuple[int, str]) -> None:
        """Health check doesn't require auth."""
        port, _ = server
        req = urllib.request.Request(f"http://localhost:{port}/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            data = json.loads(resp.read())
            assert data["ok"] is True


class TestHtmlServing:
    def test_root_serves_html(self, server: tuple[int, str]) -> None:
        port, token = server
        req = urllib.request.Request(f"http://localhost:{port}/?token={token}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert "text/html" in resp.headers.get("Content-Type", "")
            body = resp.read().decode()
            assert HTML_CONTENT in body

    def test_review_path_serves_html(self, server: tuple[int, str]) -> None:
        """The /tools/review/<num> path also serves the HTML."""
        port, token = server
        req = urllib.request.Request(f"http://localhost:{port}/tools/review/42?token={token}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            body = resp.read().decode()
            assert "PR #42 review" in body


class TestPostEndpoints:
    def test_comment_without_token_returns_403(self, server: tuple[int, str]) -> None:
        port, _ = server
        data = json.dumps({"body": "test comment"}).encode()
        req = urllib.request.Request(
            f"http://localhost:{port}/tools/review/comments",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            pytest.fail("Expected 403")
        except urllib.error.HTTPError as e:
            assert e.code == 403

    def test_merge_without_token_returns_403(self, server: tuple[int, str]) -> None:
        port, _ = server
        data = json.dumps({"pr_num": 42}).encode()
        req = urllib.request.Request(
            f"http://localhost:{port}/tools/review/merges",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            pytest.fail("Expected 403")
        except urllib.error.HTTPError as e:
            assert e.code == 403


class TestTTL:
    def test_server_self_destructs_after_ttl(self, tmp_path: Path) -> None:
        """Server with 2s TTL exits after inactivity."""
        html_file = tmp_path / "ttl-test.html"
        html_file.write_text("<html>test</html>")
        proc = subprocess.Popen(
            [
                "bun",
                SERVE_SCRIPT,
                "--port",
                "10179",
                "--token",
                "ttl-test",
                "--ttl",
                "2",
                "--html-path",
                str(html_file),
                "--owner",
                "O",
                "--repo",
                "R",
                "--pr-num",
                "1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            # Wait for startup
            time.sleep(1)
            assert proc.poll() is None, "Server should be running"
            # Wait for TTL expiry
            time.sleep(3)
            # Server should have exited
            assert proc.poll() is not None, "Server should have self-destructed after TTL"
        finally:
            if proc.poll() is None:
                proc.kill()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
