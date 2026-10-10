"""No endpoint hands a signed-in reader the platform's internals (I-06).

Every GET route is called as an administrator against the API fixture — whose
rows carry ``qwen-plus``, prompt versions and raw method codes on purpose — and
every response is searched for what must stay on the server: the model vendor
and its identifiers, prompt and processor versions, raw method codes, exception
text, file paths and configuration variable names.  A new endpoint is covered
the day it is added; there is no list of routes to keep up to date.
"""
import json
import re
import unittest

from api.main import (alerts, audit, auth, benchmark, claims, companies, diffs, duplicates, elements,
                      events, ingestion, issues, meta, notifications, notify, profiling, propagation,
                      reports, review, risks, search, timeline, users)
from tests.test_api import ApiTestCase

MODULES = (auth, meta, events, claims, diffs, duplicates, elements, ingestion, profiling, benchmark,
           issues, timeline, propagation, alerts, risks, companies, review, notifications, notify,
           audit, reports, search, users)
SAMPLES = {"event_id": "e1", "alert_id": "a1", "issue_id": "i1", "claim_id": "c1", "company_id": "cxmt",
           "version_id": "f1", "page": "164"}
FORBIDDEN = {
    "vendor": re.compile(r"qwen|deepseek|zhipu|openai|claude|gpt-|zhizengzeng", re.I),
    "provenance key": re.compile(r'"(model_name|prompt_version|processor_version|code_rev|provider|options_json|'
                                 r'error_message|run_id|round_id|first_seen_round|secret_ref|digest)"\s*:'),
    "prompt version": re.compile(r"\b(claim-verdict|event-label|event-types|event-impact|issue-codes|"
                                 r"production-mode|article-sentiment|causal-link|pre-annotate)-v\d"),
    "raw method": re.compile(r'"(llm_adjudicated|rule_numeric|model_unquoted)"|"label_source":\s*"ai"'),
    "exception": re.compile(r"Traceback|structured-output|maxItems|HTTPSConnectionPool"),
    "path": re.compile(r"data/(state|raw|processed|logs)/|\.sqlite3|config/[a-z_]+\.ya?ml|processor\.[a-z_]+"),
    "env var": re.compile(r"LLM_API_KEY|AUTH_SECRET|NOTIFY_WEBHOOK_\w+|_API_KEY"),
}


def get_routes():
    for module in MODULES:
        for route in module.router.routes:
            if "GET" in getattr(route, "methods", ()) and route.path.startswith("/api"):
                yield route.path


class NoInternalLeakTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.become("ops", "admin")
        # Exercise a populated subscription list: an empty list used to hide
        # the intentional admin-only operator configuration exception.
        created = self.client.post('/api/notify/subscriptions', json={
            'name':'fixture signing', 'channel':'inapp', 'secret_ref':'FIXTURE_SIGNING_KEY'})
        self.assertEqual(created.status_code, 200)

    def test_no_get_endpoint_returns_internals(self):
        checked = 0
        for template in get_routes():
            if template.endswith(".docx"):
                continue
            path = re.sub(r"{(\w+)(?::\w+)?}", lambda match: SAMPLES.get(match.group(1), "x"), template)
            response = self.client.get(path)
            text = response.text
            if template == '/api/notify/subscriptions' and response.status_code == 200:
                body = response.json()
                for subscription in body['items']:
                    # Admin can see the variable name, never its resolved value;
                    # this is covered separately for lower-privilege readers.
                    subscription.pop('secret_ref', None)
                text = json.dumps(body, ensure_ascii=False)
            checked += 1
            with self.subTest(route=template, status=response.status_code):
                for name, pattern in FORBIDDEN.items():
                    found = pattern.search(text)
                    self.assertIsNone(found, f"{template} returns {name}: {found.group(0) if found else ''}")
        self.assertGreater(checked, 50)

    def test_operator_secret_names_are_hidden_from_non_admin_readers(self):
        self.become('reader', 'viewer')
        response = self.client.get('/api/notify/subscriptions')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('FIXTURE_SIGNING_KEY', response.text)
        self.assertNotIn('"secret_ref"', response.text)

    def test_a_delivery_record_carries_no_remote_response(self):
        # Elsewhere response_excerpt is the company's inquiry reply; here it was the
        # receiving server's raw body.
        text = self.client.get("/api/notify/outbox").text
        self.assertNotIn('"response_excerpt"', text)

    def test_every_source_code_travels_with_its_name(self):
        for template in get_routes():
            if template.endswith(".docx"):
                continue
            path = re.sub(r"{(\w+)(?::\w+)?}", lambda match: SAMPLES.get(match.group(1), "x"), template)
            response = self.client.get(path)
            if not response.headers.get("content-type", "").startswith("application/json"):
                continue
            stack = [response.json()]
            while stack:
                value = stack.pop()
                if isinstance(value, dict):
                    if isinstance(value.get("source_id"), str) and value["source_id"]:
                        with self.subTest(route=template):
                            self.assertTrue(value.get("source_name"), f"{template}: source_id without a name")
                    stack.extend(value.values())
                elif isinstance(value, list):
                    stack.extend(value)

    def test_a_word_report_carries_no_internal_ids_or_codes(self):
        import io
        import zipfile
        for path in ("/api/reports/events/e1.docx", "/api/reports/issues/i1.docx", "/api/reports/risks/a1.docx"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            text = zipfile.ZipFile(io.BytesIO(response.content)).read("word/document.xml").decode()
            with self.subTest(path=path):
                self.assertNotIn("事件号", text)
                self.assertNotIn("事项号", text)
                self.assertNotRegex(text, r"\b(FUND_RAISE|FINANCIAL|SUPPORTED|cnstock|sse)\b")
                self.assertNotIn("filename=event-e1", response.headers["content-disposition"])


if __name__ == "__main__":
    unittest.main()
