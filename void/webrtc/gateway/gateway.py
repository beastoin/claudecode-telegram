from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


class TokenValidationError(Exception):
    pass


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * ((4 - (len(value) % 4)) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def mint_session_token(
    secret: str,
    session_id: str,
    ttl_seconds: int = 60,
    now_fn: Callable[[], float] = time.time,
    jti: str | None = None,
) -> str:
    now = int(now_fn())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": session_id,
        "jti": jti or secrets.token_hex(8),
        "iat": now,
        "exp": now + ttl_seconds,
    }
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64url_encode(sig)}"


def validate_session_token(
    token: str,
    secret: str,
    used_jti: set[str],
    now_fn: Callable[[], float] = time.time,
) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenValidationError("invalid token format")

    header_b64, payload_b64, sig_b64 = parts
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected_sig = _b64url_encode(hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest())
    if not hmac.compare_digest(expected_sig, sig_b64):
        raise TokenValidationError("invalid token signature")

    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise TokenValidationError("invalid token payload") from exc

    exp = int(payload.get("exp", 0))
    now = int(now_fn())
    if exp <= now:
        raise TokenValidationError("token expired")

    jti = str(payload.get("jti", "")).strip()
    if not jti:
        raise TokenValidationError("token missing jti")
    if jti in used_jti:
        raise TokenValidationError("token replay detected")

    used_jti.add(jti)
    return payload


@dataclass
class GatewayConfig:
    session_secret: str
    turn_secret: str
    turn_url: str
    gateway_api_key: str
    allowed_origin: str = ""
    token_ttl_seconds: int = 60
    idle_threshold_seconds: int = 30


class InMemorySessionManager:
    def __init__(self, now_fn: Callable[[], float] = time.time):
        self._now_fn = now_fn
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._used_jti: set[str] = set()
        self._used_jti_seen_at: dict[str, int] = {}

    def _prune_used_jti_locked(self, token_ttl_seconds: int, now_ts: int) -> None:
        ttl = max(1, int(token_ttl_seconds))
        cutoff = now_ts - ttl
        stale = [jti for jti, seen_at in self._used_jti_seen_at.items() if seen_at <= cutoff]
        for jti in stale:
            self._used_jti_seen_at.pop(jti, None)
            self._used_jti.discard(jti)

    def validate_and_consume_token(
        self,
        token: str,
        secret: str,
        token_ttl_seconds: int,
        now_fn: Callable[[], float] = time.time,
    ) -> dict[str, Any]:
        now_ts = int(now_fn())
        with self._lock:
            self._prune_used_jti_locked(token_ttl_seconds, now_ts)
            claims = validate_session_token(token, secret, self._used_jti, now_fn=now_fn)
            jti = str(claims.get("jti", "")).strip()
            if jti:
                self._used_jti_seen_at[jti] = now_ts
            return claims

    def create(self) -> str:
        with self._lock:
            session_id = f"sess-{secrets.token_hex(4)}"
            now = int(self._now_fn())
            self._sessions[session_id] = {"status": "active", "created_at": now, "last_seen": now}
            return session_id

    def destroy(self, session_id: str) -> bool:
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id]["status"] = "dead"
                return True
            return False

    def status(self, session_id: str, idle_threshold_seconds: int) -> str:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return "dead"
            if session["status"] == "dead":
                return "dead"
            now = int(self._now_fn())
            if now - int(session["last_seen"]) >= idle_threshold_seconds:
                return "idle"
            return "active"

    def touch(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session and session["status"] != "dead":
                session["last_seen"] = int(self._now_fn())


class HTTPStreamdRelay:
    def __init__(self, streamd_base_url: str, timeout_seconds: float = 5.0):
        self._base_url = streamd_base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def forward_offer(self, session_id: str, token: str, offer: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps({"token": token, "offer": offer}).encode("utf-8")
        req = Request(
            f"{self._base_url}/offer",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=self._timeout_seconds) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        answer = body.get("answer")
        if not isinstance(answer, dict):
            raise RuntimeError("streamd response missing answer")
        return answer


class GatewayApp:
    def __init__(
        self,
        config: GatewayConfig,
        session_manager: InMemorySessionManager | None = None,
        signal_relay: Any | None = None,
        now_fn: Callable[[], float] = time.time,
    ):
        if not config.session_secret:
            raise ValueError("session_secret is required")
        if not config.turn_secret:
            raise ValueError("turn_secret is required")
        if not config.turn_url:
            raise ValueError("turn_url is required")
        if not config.gateway_api_key:
            raise ValueError("gateway_api_key is required")

        self.config = config
        self.now_fn = now_fn
        self.session_manager = session_manager or InMemorySessionManager(now_fn=now_fn)
        self.signal_relay = signal_relay or HTTPStreamdRelay(os.environ.get("STREAMD_URL", "http://127.0.0.1:8097"))

    @staticmethod
    def _normalize_headers(headers: dict[str, Any] | None) -> dict[str, str]:
        if not headers:
            return {}
        out: dict[str, str] = {}
        for key, value in headers.items():
            out[str(key).lower()] = str(value)
        return out

    def _is_api_key_valid(self, headers: dict[str, str]) -> bool:
        provided = headers.get("x-api-key", "")
        return hmac.compare_digest(provided, self.config.gateway_api_key)

    def _origin_allowed(self, headers: dict[str, str]) -> bool:
        origin = headers.get("origin", "").strip()
        if not origin:
            return True
        allowed_origin = self.config.allowed_origin.strip()
        if not allowed_origin:
            return False
        return hmac.compare_digest(origin, allowed_origin)

    def handle(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        headers: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        norm_headers = self._normalize_headers(headers)
        if not self._origin_allowed(norm_headers):
            return 403, {"error": "origin not allowed"}

        parsed = urlparse(path)

        if method == "POST" and parsed.path == "/session/create":
            if not self._is_api_key_valid(norm_headers):
                return 401, {"error": "invalid API key"}
            session_id = self.session_manager.create()
            token = mint_session_token(
                self.config.session_secret,
                session_id,
                ttl_seconds=self.config.token_ttl_seconds,
                now_fn=self.now_fn,
            )
            return 200, {"session_id": session_id, "token": token}

        if method == "POST" and parsed.path == "/session/destroy":
            if not body or not body.get("session_id"):
                return 400, {"error": "session_id is required"}
            ok = self.session_manager.destroy(str(body["session_id"]))
            return 200, {"ok": bool(ok)}

        if method == "GET" and parsed.path == "/session/status":
            query = parse_qs(parsed.query)
            session_id = (query.get("session_id") or [""])[0]
            if not session_id:
                return 400, {"error": "session_id is required"}
            status = self.session_manager.status(session_id, self.config.idle_threshold_seconds)
            return 200, {"session_id": session_id, "status": status}

        if method == "POST" and parsed.path == "/signal/offer":
            if not body:
                return 400, {"error": "missing request body"}
            session_id = str(body.get("session_id", "")).strip()
            token = str(body.get("token", "")).strip()
            offer = body.get("offer")
            if not session_id or not token or not isinstance(offer, dict):
                return 400, {"error": "session_id, token, offer are required"}
            try:
                claims = self.session_manager.validate_and_consume_token(
                    token,
                    self.config.session_secret,
                    self.config.token_ttl_seconds,
                    now_fn=self.now_fn,
                )
            except TokenValidationError as exc:
                return 401, {"error": str(exc)}
            if claims.get("sub") != session_id:
                return 401, {"error": "token subject mismatch"}
            try:
                answer = self.signal_relay.forward_offer(session_id, token, offer)
            except Exception as exc:  # fail closed on relay errors
                return 502, {"error": f"signaling relay failed: {exc}"}
            self.session_manager.touch(session_id)
            return 200, {"answer": answer}

        if method == "POST" and parsed.path == "/turn/credentials":
            if not self._is_api_key_valid(norm_headers):
                return 401, {"error": "invalid API key"}
            if not body:
                return 400, {"error": "missing request body"}
            session_id = str(body.get("session_id", "")).strip()
            token = str(body.get("token", "")).strip()
            if not session_id or not token:
                return 401, {"error": "session_id and token are required"}
            try:
                claims = self.session_manager.validate_and_consume_token(
                    token,
                    self.config.session_secret,
                    self.config.token_ttl_seconds,
                    now_fn=self.now_fn,
                )
            except TokenValidationError as exc:
                return 401, {"error": str(exc)}
            if claims.get("sub") != session_id:
                return 401, {"error": "token subject mismatch"}
            ttl = int(body.get("ttl_seconds", 600))
            expiry = int(self.now_fn()) + ttl
            username = f"{expiry}:{session_id}"
            digest = hmac.new(self.config.turn_secret.encode("utf-8"), username.encode("utf-8"), hashlib.sha1).digest()
            credential = base64.b64encode(digest).decode("ascii")
            return 200, {"username": username, "credential": credential, "ttl": ttl, "urls": [self.config.turn_url]}

        return 404, {"error": "not found"}


def load_config_from_env() -> GatewayConfig:
    return GatewayConfig(
        session_secret=os.environ.get("SESSION_SECRET", ""),
        turn_secret=os.environ.get("TURN_SECRET", ""),
        turn_url=os.environ.get("TURN_URL", ""),
        gateway_api_key=os.environ.get("GATEWAY_API_KEY", ""),
        allowed_origin=os.environ.get("ALLOWED_ORIGIN", ""),
        token_ttl_seconds=int(os.environ.get("TOKEN_TTL_SECONDS", "60")),
        idle_threshold_seconds=int(os.environ.get("SESSION_IDLE_THRESHOLD_SECONDS", "30")),
    )


def make_http_handler(app: GatewayApp, static_dir: str | None = None):
    class GatewayHandler(BaseHTTPRequestHandler):
        def _cors_origin(self) -> str:
            origin = self.headers.get("Origin", "").strip()
            allowed_origin = app.config.allowed_origin.strip()
            if not origin or not allowed_origin:
                return ""
            if hmac.compare_digest(origin, allowed_origin):
                return allowed_origin
            return ""

        def _set_cors_headers(self) -> None:
            cors_origin = self._cors_origin()
            if cors_origin:
                self.send_header("Access-Control-Allow-Origin", cors_origin)
                self.send_header("Vary", "Origin")

        def _serve(self) -> None:
            parsed = urlparse(self.path)

            # Serve static files (web client)
            if self.command == "GET" and static_dir and parsed.path in ("/", "/index.html"):
                self._serve_file(os.path.join(static_dir, "index.html"), "text/html")
                return

            body = None
            length = int(self.headers.get("Content-Length", "0"))
            if length > 0:
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except json.JSONDecodeError:
                    self._write_json(400, {"error": "invalid JSON"})
                    return
            status, payload = app.handle(self.command, self.path, body, headers=dict(self.headers.items()))
            self._write_json(status, payload)

        def _write_json(self, status: int, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self._set_cors_headers()
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _serve_file(self, path: str, content_type: str) -> None:
            try:
                with open(path, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(data)
            except FileNotFoundError:
                self._write_json(404, {"error": "not found"})

        def do_GET(self) -> None:  # noqa: N802
            self._serve()

        def do_POST(self) -> None:  # noqa: N802
            self._serve()

        def do_OPTIONS(self) -> None:  # noqa: N802
            if not self._cors_origin():
                self._write_json(403, {"error": "origin not allowed"})
                return
            self.send_response(204)
            self._set_cors_headers()
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key")
            self.end_headers()

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    return GatewayHandler


def serve_http(app: GatewayApp, host: str, port: int, static_dir: str | None = None) -> None:
    server = ThreadingHTTPServer((host, port), make_http_handler(app, static_dir))
    print(f"Gateway listening on {host}:{port}", flush=True)
    if static_dir:
        print(f"Serving web client from {static_dir}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    cfg = load_config_from_env()
    gateway = GatewayApp(cfg)
    addr = os.environ.get("GATEWAY_ADDR", "127.0.0.1:8096")
    host, port_s = addr.rsplit(":", 1)
    static = os.environ.get("CLIENT_DIR")
    serve_http(gateway, host, int(port_s), static_dir=static)
