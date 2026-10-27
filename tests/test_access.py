"""Key, IP, revocation and concurrency checks without loading the speech model."""

import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from api.access import AccessDenied, AccessStore, RequestLimiter
from api.admin_auth import AdminAuth, create_credentials


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AccessStore(str(Path(self.temp.name) / "access.sqlite3"), "a" * 48)

    def tearDown(self):
        self.temp.cleanup()

    def test_keys_are_unique_hashed_ip_limited_and_revocable(self):
        first = self.store.create_key("Customer A", ["203.0.113.10"], 30, 2, 60)
        second = self.store.create_key("Customer B", [], 30, 2, 60)
        self.assertNotEqual(first["key"], second["key"])
        with sqlite3.connect(self.store.db_path) as db:
            contents = " ".join(str(row) for row in db.execute("SELECT * FROM client_keys"))
        self.assertNotIn(first["key"], contents)
        self.assertEqual(self.store.authenticate(first["key"], "203.0.113.10").label, "Customer A")
        with self.assertRaises(AccessDenied):
            self.store.authenticate(first["key"], "203.0.113.11")
        self.store.set_allowed_ips(["203.0.113.0/24"])
        with self.assertRaises(AccessDenied):
            self.store.authenticate(second["key"], "198.51.100.2")
        self.assertTrue(self.store.revoke_key(first["id"]))
        with self.assertRaises(AccessDenied):
            self.store.authenticate(first["key"], "203.0.113.10")

    def test_report_has_metadata_only(self):
        issued = self.store.create_key("Test", [], 1, 1, 10)
        key = self.store.authenticate(issued["key"], "127.0.0.1")
        self.store.log_request(key=key, address="127.0.0.1", transport="websocket",
                               route="/v1/audio/speech", voice="hi_female", status="ok",
                               duration_ms=1200, first_audio_ms=400, audio_bytes=48000)
        report = self.store.report()
        self.assertEqual(report["speech_requests"], 1)
        self.assertEqual(report["first_audio_p95_ms"], 400)
        self.assertNotIn(issued["key"], str(report))

    def test_report_filters_match_counts_and_rows(self):
        issued = self.store.create_key("Test", [], 1, 1, 10)
        key = self.store.authenticate(issued["key"], "127.0.0.1")
        self.store.log_request(key=key, address="127.0.0.1", transport="http",
                               route="/v1/audio/speech", voice="hi_female", status="ok", duration_ms=10)
        self.store.log_request(key=key, address="127.0.0.1", transport="websocket",
                               route="/v1/audio/speech", voice="hi_male", status="error", duration_ms=20)
        report = self.store.report(status="ok", transport="http", voice="hi_female", ip="127.0.0.1")
        self.assertEqual(report["total_requests"], 1)
        self.assertEqual(len(report["recent"]), 1)
        self.assertEqual(self.store.report(status="denied")["total_requests"], 0)

    def test_usage_minutes_and_revoked_key_deletion_preserves_history(self):
        issued = self.store.create_key("Metered", [], 30, 1, 10)
        client = self.store.authenticate(issued["key"], "203.0.113.5")
        self.store.log_request(key=client, address="203.0.113.5", transport="http",
                               route="/v1/audio/speech", voice="test", status="ok",
                               duration_ms=100, audio_seconds=150)
        self.assertFalse(self.store.delete_revoked_key(issued["id"]))
        self.assertTrue(self.store.revoke_key(issued["id"]))
        self.assertTrue(self.store.delete_revoked_key(issued["id"]))
        self.assertFalse(self.store.list_keys())
        report = self.store.report()
        self.assertEqual(report["audio_minutes"], 2.5)
        self.assertEqual(report["metered_requests"], 1)
        self.assertEqual(report["usage_breakdown"][0]["client"], "Metered")

    def test_custom_dates_filter_every_report_section(self):
        issued = self.store.create_key("Dates", [], 30, 1, 10)
        client = self.store.authenticate(issued["key"], "203.0.113.5")
        for seconds in (60, 180):
            self.store.log_request(key=client, address="203.0.113.5", transport="http",
                                   route="/v1/audio/speech", voice="test", status="ok",
                                   duration_ms=100, audio_seconds=seconds)
        with self.store._connect() as db:
            db.execute("UPDATE request_logs SET created_at=? WHERE id=(SELECT MIN(id) FROM request_logs)",
                       ("2026-10-05T10:00:00+00:00",))
            db.execute("UPDATE request_logs SET created_at=? WHERE id=(SELECT MAX(id) FROM request_logs)",
                       ("2026-10-06T10:00:00+00:00",))
        report = self.store.report(start_at=datetime(2026, 10, 5, tzinfo=timezone.utc),
                                   end_at=datetime(2026, 10, 6, tzinfo=timezone.utc))
        self.assertEqual(report["total_requests"], 1)
        self.assertEqual(report["audio_minutes"], 1)
        self.assertEqual(len(report["recent"]), 1)
        self.assertEqual(report["usage_breakdown"][0]["audio_minutes"], 1)
        self.assertEqual(report["usage_trend"][0]["audio_minutes"], 1)
        self.assertEqual(report["first_audio_p95_ms"], None)
        with self.assertRaises(ValueError):
            self.store.report(start_at=datetime(2026, 10, 6, tzinfo=timezone.utc),
                              end_at=datetime(2026, 10, 5, tzinfo=timezone.utc))

    def test_admin_password_sessions_and_logout(self):
        credentials = Path(self.temp.name) / "admin-login.json"
        create_credentials(credentials, "example_admin", "strong-example-password")
        self.assertNotIn("strong-example-password", credentials.read_text())
        auth = AdminAuth(str(self.store.db_path), str(credentials))
        with self.assertRaises(AccessDenied):
            auth.login("example_admin", "wrong-password", "127.0.0.1")
        token, csrf = auth.login("example_admin", "strong-example-password", "127.0.0.1")
        self.assertEqual(auth.session(token, "127.0.0.1"), csrf)
        with self.assertRaises(AccessDenied):
            auth.session(token, "127.0.0.2")
        auth.logout(token)
        with self.assertRaises(AccessDenied):
            auth.session(token, "127.0.0.1")

    def test_admin_can_login_from_public_ip_with_password(self):
        credentials = Path(self.temp.name) / "admin-login.json"
        create_credentials(credentials, "example_admin", "strong-example-password")
        auth = AdminAuth(str(self.store.db_path), str(credentials))
        with self.assertRaises(AccessDenied) as wrong:
            auth.login("example_admin", "wrong-password", "198.51.100.77")
        self.assertEqual(wrong.exception.status_code, 401)
        token, csrf = auth.login("example_admin", "strong-example-password", "198.51.100.77")
        self.assertEqual(auth.session(token, "198.51.100.77"), csrf)

    def test_concurrency_limit_releases(self):
        issued = self.store.create_key("Test", [], 1, 1, 10)
        key = self.store.authenticate(issued["key"], "127.0.0.1")
        limiter = RequestLimiter(global_concurrent=1, max_queue=0)

        async def check():
            await limiter.acquire(key)
            with self.assertRaises(AccessDenied):
                await limiter.acquire(key)
            await limiter.release(key)
            await limiter.acquire(key)
            await limiter.release(key)

        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
