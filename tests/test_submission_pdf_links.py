"""PDF reference regression tests, using generated fixtures only."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if importlib.util.find_spec("docx") is None or importlib.util.find_spec("fitz") is None:
    raise unittest.SkipTest("PDF-tool tests need requirements-materials.txt in a separate environment")
import fitz

def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

renderer = load("submission_reports")
links = load("submission_pdf_links")


class PdfReferenceTests(unittest.TestCase):
    def fixture(self, directory, markdown, texts):
        source = Path(directory) / "fixture.md"
        source.write_text(markdown)
        renderer.render(source)
        pdf = fitz.open()
        page = pdf.new_page()
        for y, text, blue in texts:
            page.insert_text((72, y), text, color=(5/255, 99/255, 193/255) if blue else (0, 0, 0))
        path = source.with_suffix(".pdf")
        pdf.save(path)
        pdf.close()
        return path

    def test_links_and_layout_preserved_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(directory, "# Fixture\n[Guide](https://example.org/a)\n[Guide](https://example.org/b)\n",
                                [(80, "Guide", True), (100, "Guide", True)])
            with fitz.open(path) as pdf:
                original = pdf[0].get_text()
            self.assertEqual(2, links.restore(path))
            with fitz.open(path) as pdf:
                self.assertEqual(original, pdf[0].get_text())
                self.assertEqual(["https://example.org/a", "https://example.org/b"],
                                 [r["uri"] for r in pdf[0].get_links()])
            self.assertEqual(0, links.restore(path))

    def test_wrapped_label_across_black_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(directory, "# Fixture\n[LongLink](https://example.org/a)\n",
                                [(80, "Long", True), (100, "Other table cell", False), (120, "Link", True)])
            self.assertEqual(2, links.restore(path))

    def test_missing_label_fails_without_replacing_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(directory, "# Fixture\n[Guide](https://example.org/a)\n",
                                [(80, "Different", True)])
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                links.restore(path)
            self.assertEqual(original, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
