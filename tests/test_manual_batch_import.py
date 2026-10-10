import hashlib
import tempfile
import unittest
from pathlib import Path

from collector.manual_batch_import import CORE_STEMS, classify, selected_artifacts, validate_artifact


class ManualBatchImportTests(unittest.TestCase):
    def test_core_scope_requires_one_pdf_per_declared_document(self):
        artifacts = [{"filename": stem + ".pdf"} for stem in CORE_STEMS]
        artifacts.append({"filename": "2025-12-30_申报稿_招股说明书.html"})
        selected = selected_artifacts({"artifacts": artifacts}, "core")
        self.assertEqual(len(selected), 12)
        self.assertTrue(all(item["filename"].endswith(".pdf") for item in selected))

    def test_source_and_document_type_follow_official_path(self):
        self.assertEqual(classify(
            "2026-05-20_上会稿_招股说明书.pdf",
            "https://static.sse.com.cn/stock/disclosure/a.pdf"),
            ("sse_ipo_review", "prospectus"))
        self.assertEqual(classify(
            "2026-07-15_发行公告.pdf",
            "https://static.sse.com.cn/disclosure/listedinfo/announcement/c/new/a.pdf"),
            ("sse_announcements", "announcements"))

    def test_artifact_validation_checks_declared_bytes_hash_and_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            body = b"%PDF-1.4\nvalidated"
            (base / "notice.pdf").write_bytes(body)
            item = {
                "filename": "notice.pdf", "mirror_url": "https://mirror.example/a.pdf",
                "official_sse_url": "https://static.sse.com.cn/a.pdf",
                "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                "validation": {"title_match": True},
            }
            path, digest = validate_artifact(base, item)
            self.assertEqual(path.name, "notice.pdf")
            self.assertEqual(digest, item["sha256"])
            item["sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                validate_artifact(base, item)


if __name__ == "__main__":
    unittest.main()
