"""Keep competition docs limited to formal deliverables and evidence."""
from pathlib import Path
import re
import unittest
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


class SubmissionScopeTests(unittest.TestCase):
    def test_documentation_directories(self):
        self.assertEqual({"submission", "figures"}, {p.name for p in (ROOT / "docs").iterdir()})

    def test_nine_formal_reports_have_all_formats(self):
        folder = ROOT / "docs/submission"
        sources = sorted(folder.glob("[0-9][0-9]-*.md"))
        self.assertEqual(9, len(sources))
        for source in sources:
            for suffix in (".docx", ".pdf"):
                self.assertTrue(source.with_suffix(suffix).is_file(), source.name)
        self.assertEqual({p.name for p in sources} | {"处理后数据交付范围.md", "演示材料.md"},
                         {p.name for p in folder.glob("*.md")})
        self.assertEqual({"media-manifest.json"}, {p.name for p in folder.glob("*.json")})

    def test_formal_local_references_resolve(self):
        sources = [ROOT / "README.md", *(ROOT / "docs/submission").glob("*.md")]
        for source in sources:
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", source.read_text()):
                parsed = urlsplit(target.strip("<>"))
                if parsed.scheme or not parsed.path:
                    continue
                resolved = (source.parent / unquote(parsed.path)).resolve()
                self.assertTrue(resolved.is_relative_to(ROOT), (source.name, target))
                self.assertTrue(resolved.exists(), (source.name, target))


if __name__ == "__main__":
    unittest.main()
