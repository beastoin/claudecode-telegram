import json
import threading
import time
import unittest

from gateway import GatewayApp, GatewayConfig, InMemorySessionManager, TokenValidationError, mint_session_token


class MockRelay:
    def __init__(self):
        self.calls = []

    def forward_offer(self, session_id: str, token: str, offer: dict):
        self.calls.append({"session_id": session_id, "token": token, "offer": offer})
        return {"type": "answer", "sdp": "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 102\r\na=rtpmap:102 H264/90000\r\n"}


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_700_000_000
        self.relay = MockRelay()
        self.app = GatewayApp(
            GatewayConfig(
                session_secret="gateway-secret",
                turn_secret="turn-secret",
                turn_url="turn:turn.example.com:3478?transport=udp",
                gateway_api_key="gateway-api-key",
                allowed_origin="https://console.example.com",
                token_ttl_seconds=60,
            ),
            session_manager=InMemorySessionManager(),
            signal_relay=self.relay,
            now_fn=lambda: self.now,
        )

    def _auth_headers(self, origin: str = "https://console.example.com") -> dict[str, str]:
        return {"X-API-Key": "gateway-api-key", "Origin": origin}

    def test_post_session_create_returns_session_id_and_token(self):
        status, body = self.app.handle("POST", "/session/create", {}, headers=self._auth_headers())
        self.assertEqual(status, 200)
        self.assertIn("session_id", body)
        self.assertIn("token", body)
        self.assertTrue(body["session_id"].startswith("sess-"))

    def test_post_session_destroy_kills_session(self):
        _, created = self.app.handle("POST", "/session/create", {}, headers=self._auth_headers())
        session_id = created["session_id"]

        status, body = self.app.handle("POST", "/session/destroy", {"session_id": session_id})
        self.assertEqual(status, 200)
        self.assertEqual(body["ok"], True)

        status, body = self.app.handle("GET", f"/session/status?session_id={session_id}", None)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "dead")

    def test_get_session_status_returns_active_idle_dead(self):
        _, created = self.app.handle("POST", "/session/create", {}, headers=self._auth_headers())
        session_id = created["session_id"]

        status, body = self.app.handle("GET", f"/session/status?session_id={session_id}", None)
        self.assertEqual(status, 200)
        self.assertIn(body["status"], {"active", "idle"})

        self.app.handle("POST", "/session/destroy", {"session_id": session_id})
        status, body = self.app.handle("GET", f"/session/status?session_id={session_id}", None)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "dead")

    def test_signaling_relay_forwards_offer_and_returns_answer(self):
        _, created = self.app.handle("POST", "/session/create", {}, headers=self._auth_headers())
        token = created["token"]
        session_id = created["session_id"]
        offer = {"type": "offer", "sdp": "v=0\r\n"}

        status, body = self.app.handle("POST", "/signal/offer", {"session_id": session_id, "token": token, "offer": offer})
        self.assertEqual(status, 200)
        self.assertIn("answer", body)
        self.assertEqual(self.relay.calls[-1]["offer"], offer)

    def test_token_validation_rejects_expired_tokens(self):
        token = mint_session_token("gateway-secret", "sess-expired", ttl_seconds=-1, now_fn=lambda: self.now)
        status, body = self.app.handle(
            "POST",
            "/signal/offer",
            {"session_id": "sess-expired", "token": token, "offer": {"type": "offer", "sdp": "v=0\r\n"}},
        )
        self.assertEqual(status, 401)
        self.assertIn("expired", body["error"])

    def test_token_validation_rejects_replayed_tokens(self):
        _, created = self.app.handle("POST", "/session/create", {}, headers=self._auth_headers())
        token = created["token"]
        session_id = created["session_id"]
        offer = {"type": "offer", "sdp": "v=0\r\n"}

        status, _ = self.app.handle("POST", "/signal/offer", {"session_id": session_id, "token": token, "offer": offer})
        self.assertEqual(status, 200)

        status, body = self.app.handle("POST", "/signal/offer", {"session_id": session_id, "token": token, "offer": offer})
        self.assertEqual(status, 401)
        self.assertIn("replay", body["error"])

    def test_turn_credential_generation_returns_ephemeral_creds(self):
        _, created = self.app.handle("POST", "/session/create", {}, headers=self._auth_headers())
        session_id = created["session_id"]
        token = created["token"]

        status, body = self.app.handle(
            "POST",
            "/turn/credentials",
            {"session_id": session_id, "token": token, "ttl_seconds": 120},
            headers=self._auth_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["ttl"], 120)
        self.assertEqual(body["urls"], ["turn:turn.example.com:3478?transport=udp"])
        self.assertIn(str(self.now + 120), body["username"])
        self.assertTrue(body["credential"])

    def test_turn_credentials_reject_missing_token(self):
        _, created = self.app.handle("POST", "/session/create", {}, headers=self._auth_headers())
        status, body = self.app.handle(
            "POST",
            "/turn/credentials",
            {"session_id": created["session_id"]},
            headers=self._auth_headers(),
        )
        self.assertEqual(status, 401)
        self.assertIn("token", body["error"].lower())

    def test_turn_credentials_reject_invalid_token(self):
        _, created = self.app.handle("POST", "/session/create", {}, headers=self._auth_headers())
        status, body = self.app.handle(
            "POST",
            "/turn/credentials",
            {"session_id": created["session_id"], "token": "invalid-token"},
            headers=self._auth_headers(),
        )
        self.assertEqual(status, 401)
        self.assertIn("token", body["error"].lower())

    def test_session_create_rejects_missing_api_key(self):
        status, body = self.app.handle(
            "POST",
            "/session/create",
            {},
            headers={"Origin": "https://console.example.com"},
        )
        self.assertEqual(status, 401)
        self.assertIn("api key", body["error"].lower())

    def test_session_create_rejects_wrong_origin(self):
        status, body = self.app.handle(
            "POST",
            "/session/create",
            {},
            headers=self._auth_headers("https://evil.example.com"),
        )
        self.assertEqual(status, 403)
        self.assertIn("origin", body["error"].lower())

    def test_session_create_accepts_api_key_and_allowed_origin(self):
        status, body = self.app.handle(
            "POST",
            "/session/create",
            {},
            headers=self._auth_headers(),
        )
        self.assertEqual(status, 200)
        self.assertIn("session_id", body)

    def test_turn_credentials_rejects_missing_api_key(self):
        _, created = self.app.handle(
            "POST",
            "/session/create",
            {},
            headers=self._auth_headers(),
        )
        status, body = self.app.handle(
            "POST",
            "/turn/credentials",
            {"session_id": created["session_id"]},
            headers={"Origin": "https://console.example.com"},
        )
        self.assertEqual(status, 401)
        self.assertIn("api key", body["error"].lower())

    def test_session_manager_has_thread_lock(self):
        self.assertTrue(hasattr(self.app.session_manager, "_lock"))
        self.assertIsInstance(self.app.session_manager._lock, type(threading.Lock()))

    def test_jti_replay_cache_prunes_after_ttl(self):
        now_ref = {"value": self.now}
        app = GatewayApp(
            GatewayConfig(
                session_secret="gateway-secret",
                turn_secret="turn-secret",
                turn_url="turn:turn.example.com:3478?transport=udp",
                gateway_api_key="gateway-api-key",
                allowed_origin="https://console.example.com",
                token_ttl_seconds=1,
            ),
            session_manager=InMemorySessionManager(now_fn=lambda: now_ref["value"]),
            signal_relay=self.relay,
            now_fn=lambda: now_ref["value"],
        )
        _, created = app.handle("POST", "/session/create", {}, headers=self._auth_headers())
        session_id = created["session_id"]
        offer = {"type": "offer", "sdp": "v=0\r\n"}

        token_one = mint_session_token(
            "gateway-secret",
            session_id,
            ttl_seconds=60,
            now_fn=lambda: now_ref["value"],
            jti="stable-jti",
        )
        status, _ = app.handle(
            "POST",
            "/signal/offer",
            {"session_id": session_id, "token": token_one, "offer": offer},
        )
        self.assertEqual(status, 200)

        now_ref["value"] += 5
        token_two = mint_session_token(
            "gateway-secret",
            session_id,
            ttl_seconds=60,
            now_fn=lambda: now_ref["value"],
            jti="stable-jti",
        )
        status, body = app.handle(
            "POST",
            "/signal/offer",
            {"session_id": session_id, "token": token_two, "offer": offer},
        )
        self.assertEqual(status, 200, body)

    def test_concurrent_session_create_is_consistent(self):
        created_ids = []
        failures = []
        collect_lock = threading.Lock()

        def worker() -> None:
            try:
                status, body = self.app.handle("POST", "/session/create", {}, headers=self._auth_headers())
                if status != 200:
                    raise AssertionError(f"unexpected status {status}: {body}")
                with collect_lock:
                    created_ids.append(body["session_id"])
            except Exception as exc:  # pragma: no cover - assertion path
                with collect_lock:
                    failures.append(str(exc))

        threads = [threading.Thread(target=worker) for _ in range(40)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(failures, [])
        self.assertEqual(len(created_ids), 40)
        self.assertEqual(len(set(created_ids)), 40)


if __name__ == "__main__":
    unittest.main()
