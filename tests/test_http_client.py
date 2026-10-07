"""Tests for core.HttpClient — connection pooling, rate limiting, exponential backoff.

Behavior tests: each test verifies something the bridge depends on.
All tests use injected clock/urlopen — no real network calls.
"""
import urllib.error
from unittest.mock import MagicMock

from core import (
    HttpClient, RetryConfig, RateLimitConfig, _TokenBucket,
    _RealClock,
)


# ── Test doubles ──────────────────────────────────────────────────────


class FakeClock:
    """Deterministic clock for testing."""

    def __init__(self) -> None:
        self._time = 1000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self._time

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._time += seconds


class FakeResponse:
    """Minimal HTTP response."""

    def __init__(self, data: bytes = b'{"ok":true}', code: int = 200) -> None:
        self._data = data
        self.status = code

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *a: object) -> None:
        pass


# ── Token bucket ──────────────────────────────────────────────────────


def test_token_bucket_allows_burst() -> None:
    """Token bucket allows `burst` requests immediately."""
    clock = FakeClock()
    bucket = _TokenBucket(rate=1.0, burst=3, clock=clock)

    # Should allow 3 immediately
    assert bucket.acquire(timeout=0.01) is True
    assert bucket.acquire(timeout=0.01) is True
    assert bucket.acquire(timeout=0.01) is True


def test_token_bucket_refills_over_time() -> None:
    """Token bucket refills at the configured rate."""
    clock = FakeClock()
    bucket = _TokenBucket(rate=1.0, burst=1, clock=clock)

    assert bucket.acquire(timeout=0) is True
    # Bucket empty — advance time by 1s to refill 1 token
    clock._time += 1.0
    assert bucket.acquire(timeout=0) is True


def test_token_bucket_blocks_when_empty() -> None:
    """Token bucket blocks and sleeps when no tokens available."""
    clock = FakeClock()
    bucket = _TokenBucket(rate=10.0, burst=1, clock=clock)

    assert bucket.acquire(timeout=1.0) is True
    # Next acquire should block and sleep
    assert bucket.acquire(timeout=1.0) is True
    assert len(clock.sleeps) > 0


# ── HttpClient retry ─────────────────────────────────────────────────


def test_retry_on_429() -> None:
    """Client retries on 429 with exponential backoff."""
    clock = FakeClock()
    call_count = 0

    def fake_urlopen(req, timeout=30):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests", {}, None)
        return FakeResponse()

    client = HttpClient(
        retry=RetryConfig(max_retries=3, initial_delay=1.0, backoff_factor=2.0),
        rate_limit=RateLimitConfig(requests_per_second=100, burst=100),
        clock=clock,
        urlopen=fake_urlopen,
    )

    resp = client.get("https://example.com/test")
    assert resp.status == 200
    assert call_count == 3  # 2 failures + 1 success
    # Backoff: 1.0s then 2.0s
    assert len(clock.sleeps) >= 2
    assert clock.sleeps[0] == 1.0
    assert clock.sleeps[1] == 2.0


def test_no_retry_on_400() -> None:
    """Client does NOT retry on 400 (client error, not retryable)."""
    def fake_urlopen(req, timeout=30):
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", {}, None)

    client = HttpClient(
        retry=RetryConfig(max_retries=3),
        rate_limit=RateLimitConfig(requests_per_second=100, burst=100),
        urlopen=fake_urlopen,
    )

    try:
        client.get("https://example.com/test")
        assert False, "Should have raised"
    except urllib.error.HTTPError as e:
        assert e.code == 400  # Raised immediately, no retry


def test_retry_exhausted_raises() -> None:
    """Client raises last exception when all retries fail."""
    clock = FakeClock()

    def fake_urlopen(req, timeout=30):
        raise urllib.error.HTTPError(
            req.full_url, 503, "Service Unavailable", {}, None)

    client = HttpClient(
        retry=RetryConfig(max_retries=2, initial_delay=0.5),
        rate_limit=RateLimitConfig(requests_per_second=100, burst=100),
        clock=clock,
        urlopen=fake_urlopen,
    )

    try:
        client.get("https://example.com/test")
        assert False, "Should have raised"
    except urllib.error.HTTPError as e:
        assert e.code == 503
    # 2 retries = 2 sleeps
    assert len(clock.sleeps) == 2


def test_retry_on_connection_error() -> None:
    """Client retries on network errors (URLError, OSError)."""
    clock = FakeClock()
    call_count = 0

    def fake_urlopen(req, timeout=30):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionError("Connection refused")
        return FakeResponse()

    client = HttpClient(
        retry=RetryConfig(max_retries=2, initial_delay=1.0),
        rate_limit=RateLimitConfig(requests_per_second=100, burst=100),
        clock=clock,
        urlopen=fake_urlopen,
    )

    resp = client.get("https://example.com/test")
    assert resp.status == 200
    assert call_count == 2


def test_backoff_caps_at_max_delay() -> None:
    """Exponential backoff is capped at max_delay."""
    clock = FakeClock()

    def fake_urlopen(req, timeout=30):
        raise urllib.error.HTTPError(
            req.full_url, 429, "Too Many", {}, None)

    client = HttpClient(
        retry=RetryConfig(max_retries=4, initial_delay=10.0,
                          max_delay=30.0, backoff_factor=3.0),
        rate_limit=RateLimitConfig(requests_per_second=100, burst=100),
        clock=clock,
        urlopen=fake_urlopen,
    )

    try:
        client.get("https://example.com/test")
    except urllib.error.HTTPError:
        pass

    # Delays: 10, 30 (capped from 30), 30 (capped from 90), 30 (capped from 270)
    assert all(d <= 30.0 for d in clock.sleeps)


def test_per_host_rate_limiting() -> None:
    """Different hosts get independent rate limiters."""
    client = HttpClient(
        rate_limit=RateLimitConfig(requests_per_second=100, burst=100),
        urlopen=lambda req, timeout=30: FakeResponse(),
    )

    # Should succeed — each host has its own bucket
    client.get("https://host-a.com/test")
    client.get("https://host-b.com/test")
    # Both used, both have separate limiters
    assert "host-a.com" in client._limiters
    assert "host-b.com" in client._limiters
