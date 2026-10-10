"""What the platform answers to someone who has not signed in (I-06 platform-wide review).

Before this, ``GET /%2e%2e/%2e%2e/.env`` returned the model gateway key and the
session-signing secret, ``/docs`` listed every endpoint, and ``/api/health``
named the model provider and printed its raw exception text.
"""
import unittest
import unittest.mock
import sqlite3
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from api.main import CLIENT_DIST, app

VENDORS = ("qwen", "deepseek", "zhipu", "openai", "claude", "gpt-", "zhizengzeng")


class TraversalTests(unittest.TestCase):
    def setUp(self):
        if not CLIENT_DIST.is_dir():
            self.skipTest("web/dist is not built")
        self.client = TestClient(app)

    def _shell(self, response):
        # Anything outside web/dist falls back to the app shell, never to the file.
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertNotIn("AUTH_SECRET", response.text)
        self.assertNotIn("LLM_API_KEY", response.text)

    def test_encoded_dot_segments_cannot_leave_the_client_directory(self):
        for path in ("/%2e%2e/%2e%2e/.env", "/..%2F..%2F.env", "/%2e%2e/%2e%2e/README.md",
                     "/%2e%2e/%2e%2e/data/processed/exports/events.jsonl",
                     "/%2e%2e/%2e%2e/config/accounts.json"):
            with self.subTest(path=path):
                self._shell(self.client.get(path))

    def test_an_absolute_path_is_not_served(self):
        self._shell(self.client.get("//etc/passwd"))

    def test_the_built_client_is_still_served(self):
        self.assertIn("<div id=\"app\">", self.client.get("/").text)
        self.assertEqual(self.client.get("/login").status_code, 200)


class AnonymousSurfaceTests(unittest.TestCase):
    def setUp(self):
        from tests.test_api import build_fixture
        from api.routes import events
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path=Path(directory.name)/'anonymous.sqlite3'
        build_fixture(path).close()
        def connect():
            db=sqlite3.connect(path,check_same_thread=False)
            db.row_factory=sqlite3.Row
            return db
        patch=unittest.mock.patch.object(events,'connect',connect)
        patch.start();self.addCleanup(patch.stop)
        self.client = TestClient(app)

    def test_api_documentation_is_not_published(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertNotIn("openapi", response.text.lower()[:2000])
                self.assertNotIn("swagger", response.text.lower())

    def test_health_says_only_that_the_platform_is_up(self):
        body = self.client.get("/api/health").json()
        self.assertEqual(set(body), {"status", "events", "companies"})
        text = str(body).lower()
        for vendor in VENDORS:
            self.assertNotIn(vendor, text)

    def test_system_status_needs_a_session(self):
        self.assertEqual(self.client.get("/api/system/status").status_code, 401)

    def test_every_response_carries_a_request_id(self):
        response = self.client.get("/api/events")
        self.assertEqual(response.status_code, 401)
        self.assertRegex(response.headers.get("X-Request-ID", ""), r"^[0-9a-f]{12}$")


class ErrorShapeTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_an_unknown_api_route_answers_in_chinese(self):
        from api import auth as auth_module
        with unittest.mock.patch.object(auth_module, "live_session", return_value={"username": "x"}):
            body = self.client.get("/api/no-such-thing").json()
        self.assertNotIn("unknown endpoint", str(body))
        self.assertIn("request_id", body)

    def test_an_unexpected_failure_hides_the_exception(self):
        from api.routes import meta
        from api import auth as auth_module
        with unittest.mock.patch.object(auth_module, "live_session", return_value={"username": "x"}), \
                unittest.mock.patch.object(meta, "schema_gaps", side_effect=RuntimeError("secret table xyz")):
            response = self.client.get("/api/system/status")
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("secret table", response.text)
        self.assertIn("服务暂时不可用", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
