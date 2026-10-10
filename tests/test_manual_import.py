import tempfile
import unittest
from pathlib import Path

from collector.collect import RuntimeSource
from collector.manual_import import absolute_http_url, content_type_for, validate_request


class ManualImportTests(unittest.TestCase):
    def setUp(self):
        self.source = RuntimeSource({
            "source_id": "exchange", "base_url": "https://example.com/disclosure/",
            "allowed_hosts": ["example.com", "static.example.com"],
            "path_allow_prefixes": ["/disclosure/", "/files/"],
            "company_scope": ["cxmt", "wuxi_apptec"],
            "document_types": ["announcements", "annual_reports"],
        }, "MANUAL", {})

    def test_request_requires_explicit_company_and_configured_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notice.pdf"
            path.write_bytes(b"%PDF-1.4\n")
            with self.assertRaisesRegex(ValueError, "company-id"):
                validate_request(self.source, path, "https://static.example.com/files/a.pdf",
                                 None, "announcements", "2026-08-24", 1000)
            normalized, company_id, kind = validate_request(
                self.source, path, "https://static.example.com/files/a.pdf#page=1",
                "cxmt", "announcements", "2026-08-24", 1000)
            self.assertEqual(normalized, "https://static.example.com/files/a.pdf")
            self.assertEqual((company_id, kind), ("cxmt", "announcements"))
            self.assertEqual(content_type_for(path, path.read_bytes()), "application/pdf")

    def test_request_rejects_out_of_scope_url(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notice.pdf"
            path.write_bytes(b"%PDF-1.4\n")
            with self.assertRaisesRegex(ValueError, "outside"):
                validate_request(self.source, path, "https://other.example/a.pdf",
                                 "cxmt", "announcements", None, 1000)

    def test_acquisition_url_must_be_absolute_http(self):
        self.assertEqual(absolute_http_url("HTTPS://Example.com/a#x", "--acquisition-url"),
                         "https://example.com/a")
        with self.assertRaisesRegex(ValueError, "absolute HTTP"):
            absolute_http_url("file:///tmp/a.pdf", "--acquisition-url")


if __name__ == "__main__":
    unittest.main()
