"""Regression tests for portable submission report references (no business data)."""
import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
if importlib.util.find_spec("docx") is None:
    raise unittest.SkipTest("Document-tool tests need requirements-materials.txt in a separate environment")
spec = importlib.util.spec_from_file_location("submission_reports", ROOT / "scripts/submission_reports.py")
reports = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reports)


class PortableReportTests(unittest.TestCase):
    def test_relative_file_reference(self):
        source = ROOT / "docs/submission/02-技术报告.md"
        url = reports.reference_url("../../README.md", source, "v1.0")
        self.assertIn("/blob/v1.0/README.md", url)

    def test_relative_directory_reference(self):
        source = ROOT / "docs/submission/02-技术报告.md"
        self.assertIn("/tree/main/docs/submission",
                      reports.reference_url("./", source, "main"))

    def test_fragment_and_unicode(self):
        source = ROOT / "docs/submission/02-技术报告.md"
        url = reports.reference_url("03-数据说明.md#字段", source, "main")
        self.assertIn("%E6%95%B0", url)
        self.assertTrue(url.endswith("#%E5%AD%97%E6%AE%B5"))

    def test_https_preserved(self):
        source = ROOT / "docs/submission/02-技术报告.md"
        self.assertEqual("https://example.org/a?q=1",
                         reports.reference_url("https://example.org/a?q=1", source, "main"))

    def test_disallowed_scheme(self):
        with self.assertRaises(ValueError):
            reports.reference_url("file:///opt/private", ROOT / "README.md", "main")

    def test_reference_cannot_escape_repository(self):
        with self.assertRaises(ValueError):
            reports.reference_url("../../../../etc/passwd", ROOT / "README.md", "main")

    def test_paragraph_and_table_hyperlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "sample.md"
            source.write_text("# 标题\n\n[段落](https://example.org/a)\n\n"
                              "| 名称 | 链接 |\n|---|---|\n| 行 | [表格](https://example.org/b) |\n"
                              "\n```text\n[代码](https://example.org/not-a-link)\n```\n")
            dest = reports.render(source)
            with zipfile.ZipFile(dest) as z:
                relations = ET.fromstring(z.read("word/_rels/document.xml.rels"))
                targets = {r.attrib["Target"] for r in relations if r.attrib["Type"].endswith("/hyperlink")}
                self.assertEqual({"https://example.org/a", "https://example.org/b"}, targets)
                body = z.read("word/document.xml").decode()
                self.assertIn("标题", body)
                self.assertIn("表格", body)
                self.assertIn("https://example.org/not-a-link", body)


if __name__ == "__main__":
    unittest.main()
