"""Delivery, checked for the things that would make it dangerous to have built.

An outward channel is the part of a notification feature that can hurt someone:
a webhook target pointed inward is server-side request forgery, a signing key in
a response body is a leaked credential, and a send that happens without anyone
asking is the worst of both. Each of those has an assertion here.

The end-to-end case runs against `scripts/webhook_receiver.py` on an ephemeral
loopback port, so "a delivery really left and really arrived, signed" is proved
rather than asserted — and proved without a network.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

from api import notify_delivery
from api.notify_delivery import Refused, check_target, deliver, resolve_secret, sign

import importlib.util

SPEC = importlib.util.spec_from_file_location(
    "webhook_receiver", Path(__file__).resolve().parents[1] / "scripts" / "webhook_receiver.py")
receiver_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(receiver_module)

PAYLOAD = {"alert_id": "a1", "summary": "募资规模传闻", "company_id": "cxmt",
           "issue_code": "FUND_RAISE", "risk_level": "high", "credibility_label": "high",
           "risk_score": 82.5, "window": {"start": "2026-09-01", "end": "2026-09-03"},
           "reasons": ["unverified_major_rumour"],
           "evidence": [{"claim": "募资 295 亿元", "verdict": "PARTIAL",
                         "document": "招股说明书", "page": 164}]}


def subscription(**overrides):
    base = {"subscription_id": "s1", "channel": "webhook", "dry_run": 1,
            "target": "http://127.0.0.1:8787/hook", "secret_ref": None}
    return {**base, **overrides}


class TargetTests(unittest.TestCase):
    def test_loopback_is_allowed_by_default(self):
        self.assertTrue(check_target("http://127.0.0.1:9000/hook"))

    def test_anything_else_is_refused_by_default(self):
        # The safe default for a feature that makes outbound requests is one
        # that cannot reach anything interesting.
        with self.assertRaises(Refused):
            check_target("http://169.254.169.254/latest/meta-data/")
        with self.assertRaises(Refused):
            check_target("https://example.test/hook")

    def test_the_refusal_names_the_rule_that_refused(self):
        with self.assertRaises(Refused) as caught:
            check_target("https://example.test/hook")
        # The operator learns the rule; a page shown the refusal does not (I-06).
        self.assertIn("NOTIFY_WEBHOOK_ALLOWLIST", caught.exception.for_operator())
        self.assertNotIn("NOTIFY_WEBHOOK_ALLOWLIST", str(caught.exception))

    def test_an_operator_can_widen_it_deliberately(self):
        with mock.patch.dict(os.environ, {"NOTIFY_WEBHOOK_ALLOWLIST": "https://hooks.internal/*"}):
            self.assertTrue(check_target("https://hooks.internal/team"))
            with self.assertRaises(Refused):
                check_target("http://127.0.0.1:9000/hook")

    def test_an_empty_target_is_refused_rather_than_treated_as_blank(self):
        with self.assertRaises(Refused):
            check_target(None)


class SecretTests(unittest.TestCase):
    def test_the_name_resolves_to_the_value_from_the_environment(self):
        with mock.patch.dict(os.environ, {"DEMO_KEY": "s3cret"}):
            self.assertEqual(resolve_secret("DEMO_KEY"), "s3cret")

    def test_an_unset_variable_refuses_rather_than_signing_with_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(Refused):
                resolve_secret("MISSING_KEY")

    def test_the_signature_is_hmac_sha256_over_the_exact_body(self):
        headers = sign(b"payload", "s3cret")
        expected = hmac.new(b"s3cret", b"payload", hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-Signature"], f"sha256={expected}")

    def test_no_secret_means_no_header_rather_than_an_empty_one(self):
        self.assertEqual(sign(b"payload", None), {})

    def test_a_delivery_result_never_carries_the_key(self):
        with mock.patch.dict(os.environ, {"DEMO_KEY": "s3cret"}):
            result = deliver(subscription(secret_ref="DEMO_KEY"), PAYLOAD)
        self.assertNotIn("s3cret", json.dumps(result, ensure_ascii=False))


class DryRunTests(unittest.TestCase):
    def test_dry_run_opens_no_socket_at_all(self):
        # Not "sends to a sink" — does not call the transport. Anything that
        # touches urlopen here fails the test loudly.
        def explode(*args, **kwargs):
            raise AssertionError("dry-run 不得发起任何网络请求")

        with mock.patch.object(notify_delivery.urllib.request, "urlopen", explode):
            result = deliver(subscription(dry_run=1), PAYLOAD)
        self.assertEqual(result["status"], "dry_run")

    def test_dry_run_still_reports_what_would_have_gone_out(self):
        with mock.patch.dict(os.environ, {"DEMO_KEY": "s3cret"}):
            result = deliver(subscription(dry_run=1, secret_ref="DEMO_KEY"), PAYLOAD)
        self.assertIn("字节", result["response"])
        self.assertIn("sha256=", result["response"])

    def test_dry_run_email_writes_a_real_message_not_a_lookalike(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(notify_delivery, "OUTBOX_DIR", Path(directory)):
                result = deliver(
                    subscription(channel="email", target="desk@example.test", dry_run=1), PAYLOAD)
            written = list(Path(directory).glob("*.eml"))
            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(len(written), 1)
            raw = written[0].read_text(encoding="utf-8")
        # A rendered message, not a string that looks like one: the headers are
        # what prove it would survive an actual mail transport.
        self.assertIn("Subject:", raw)
        self.assertIn("To: desk@example.test", raw)
        self.assertIn("募资规模传闻", raw)

    def test_a_refused_target_is_one_failed_delivery_not_an_exception(self):
        # One bad endpoint must not stop the rest of the queue.
        result = deliver(subscription(dry_run=0, target="https://example.test/hook"), PAYLOAD)
        self.assertEqual(result["status"], "failed")
        self.assertIn("NOTIFY_WEBHOOK_ALLOWLIST", result["error"])

    def test_real_email_is_refused_because_smtp_is_not_configured(self):
        result = deliver(subscription(channel="email", target="x@example.test", dry_run=0), PAYLOAD)
        self.assertEqual(result["status"], "failed")
        self.assertIn("dry-run", result["error"])


class EndToEndTests(unittest.TestCase):
    """A signed delivery, over a real socket, to the receiver stub — offline."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.receipts = Path(self.directory.name) / "receipts.jsonl"
        receiver_module.Handler.secret = "demo-secret"
        receiver_module.Handler.receipts = self.receipts
        self.server = HTTPServer(("127.0.0.1", 0), receiver_module.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_a_signed_delivery_arrives_and_is_recorded(self):
        with mock.patch.dict(os.environ, {"DEMO_KEY": "demo-secret"}):
            result = deliver(subscription(
                dry_run=0, target=f"http://127.0.0.1:{self.port}/hook", secret_ref="DEMO_KEY"), PAYLOAD)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["http_status"], 200)
        received = [json.loads(line) for line in self.receipts.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(received[0]["payload"]["alert_id"], "a1")
        self.assertTrue(received[0]["signed"])

    def test_a_wrong_signature_is_rejected_so_a_green_delivery_means_something(self):
        with mock.patch.dict(os.environ, {"DEMO_KEY": "not-the-secret"}):
            result = deliver(subscription(
                dry_run=0, target=f"http://127.0.0.1:{self.port}/hook", secret_ref="DEMO_KEY"), PAYLOAD)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["http_status"], 401)
        self.assertFalse(self.receipts.exists())


if __name__ == "__main__":
    unittest.main()
