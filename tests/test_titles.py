"""I-05: sources a reviewer could not identify — "China" and 「H股公告」."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api.routes.risks import source_basis
from collector.collect import meta_content
from processor import titles


class MetaContentTests(unittest.TestCase):
    def test_an_apostrophe_inside_double_quotes_is_kept(self):
        page = '<meta property="og:title" content="China\'s banned memory-maker CXMT">'
        self.assertEqual(meta_content(page, "og:title"), "China's banned memory-maker CXMT")

    def test_content_before_property_is_read_too(self):
        page = '<meta content="Chinese CXMT DRAM doesn\'t look like a savior" property="og:title">'
        self.assertEqual(meta_content(page, "og:title"), "Chinese CXMT DRAM doesn't look like a savior")

    def test_entities_are_unescaped(self):
        self.assertEqual(meta_content('<meta name="og:title" content="A &amp; B">', "og:title"), "A & B")


class TitleFromTextTests(unittest.TestCase):
    def test_an_hkex_return_is_named_by_its_form(self):
        text = "[PAGE 1]\nFF304\nv\n第 1 頁 共 4 頁\n1.2.5\n翌日披露報表\n(股份發行人 ── 已發行股本變動)\n"
        self.assertEqual(titles.title_from_text(text), "翌日披露報表")

    def test_a_long_form_name_keeps_its_last_clause(self):
        text = "FF301\nv\n股份發行人及根據《上市規則》第十九B章上市的香港預託證券發行人的證券變動月報表\n"
        self.assertEqual(titles.title_from_text(text), "證券變動月報表")

    def test_an_announcement_heading_sits_under_the_stock_code(self):
        text = ("香港交易及結算所有限公司對本公告的內容概不負責\n無錫藥明康德新藥開發股份有限公司\n"
                "（於中華人民共和國註冊成立的股份有限公司）\n（股份代號：2359）\n董事會會議召開日期\n"
                "無錫藥明康德新藥開發股份有限公司（「本公司」）董事會謹此宣佈……\n")
        self.assertEqual(titles.title_from_text(text), "董事會會議召開日期")

    def test_a_numbered_heading_is_joined(self):
        text = ("（股份代號：2359）\n(1)根據2022年H股獎勵信託計劃進一步授予獎勵；及\n"
                "(2)因本公司服務供應商辦事處遷址而變更香港主要營業地點地址\n茲提述……\n")
        self.assertEqual(titles.title_from_text(text),
                         "(1)根據2022年H股獎勵信託計劃進一步授予獎勵；及(2)因本公司服務供應商辦事處遷址而變更香港主要營業地點地址")

    def test_the_bank_roster_under_a_placing_is_not_the_title(self):
        text = "（股份代號：2359）\n根據一般授權配售新H股\n獨家整體協調人、獨家全球協調人\n茲提述……\n"
        self.assertEqual(titles.title_from_text(text), "根據一般授權配售新H股")

    def test_a_prospectus_cover_reads_as_its_title(self):
        text = ("[PAGE 1]\n长鑫科技集团股份有限公司\nCXMT Corporation\n（安徽省合肥市经济技术开发区空港工业园兴业大道388 号）\n"
                "首次公开发行股票并在科创板上市\n招股说明书\n（申报稿）\n保荐机构（主承销商）\n")
        self.assertEqual(titles.title_from_text(text), "长鑫科技集团股份有限公司首次公开发行股票并在科创板上市招股说明书（申报稿）")

    def test_an_english_web_page_banner_is_not_a_title(self):
        self.assertIsNone(titles.title_from_text("Here's how you know\nA lock () or https:// means\n"))


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.db = sqlite3.connect(":memory:")
        self.db.execute("""CREATE TABLE document_versions(version_id TEXT PRIMARY KEY, title TEXT,
            raw_path TEXT, content_hash TEXT)""")

    def tearDown(self):
        self.directory.cleanup()

    def _row(self, version_id, title, raw=None, text=None):
        raw_path = None
        if raw is not None:
            raw_path = self.root / f"{version_id}.html"
            raw_path.write_text(raw, encoding="utf-8")
        content_hash = None
        if text is not None:
            content_hash = f"{version_id}hash"
            path = self.root / "text" / content_hash[:2] / f"{content_hash}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.db.execute("INSERT INTO document_versions VALUES(?,?,?,?)",
                        (version_id, title, str(raw_path) if raw_path else None, content_hash))

    def _repair(self, **kwargs):
        with patch("processor.titles.text_file_path",
                   lambda h: self.root / "text" / h[:2] / f"{h}.txt"):
            return titles.repair(self.db, **kwargs)

    def _title(self, version_id):
        return self.db.execute("SELECT title, source_title, title_basis FROM document_versions WHERE version_id=?",
                               (version_id,)).fetchone()

    def test_a_truncated_title_is_reread_from_the_page(self):
        self._row("t", "China", raw='<meta property="og:title" content="China\'s CXMT targets 30% share">')
        self._repair()
        self.assertEqual(self._title("t"), ("China's CXMT targets 30% share", "China", "og_title_reparsed"))

    def test_a_title_that_merely_starts_the_same_is_left_alone(self):
        self._row("t", "China", raw='<meta property="og:title" content="China Daily roundup">')
        self._repair()
        self.assertEqual(self._title("t")[0], "China")

    def test_a_listing_label_gains_the_heading_beside_it(self):
        self._row("h", "H股公告", text="FF304\nv\n翌日披露報表\n")
        self._repair()
        self.assertEqual(self._title("h"), ("H股公告｜翌日披露報表", "H股公告", "read_from_document"))

    def test_dry_run_writes_nothing_not_even_columns(self):
        self._row("h", "H股公告", text="FF304\nv\n翌日披露報表\n")
        self.assertEqual(self._repair(dry_run=True)["changed"], 1)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(document_versions)")}
        self.assertNotIn("source_title", columns)

    def test_a_second_run_changes_nothing(self):
        self._row("h", "H股公告", text="FF304\nv\n翌日披露報表\n")
        self._row("t", "China", raw='<meta property="og:title" content="China\'s CXMT">')
        self.assertEqual(self._repair()["changed"], 2)
        self.assertEqual(self._repair()["changed"], 0)
        self.assertEqual(self._title("h")[1], "H股公告")


class SourceBasisTests(unittest.TestCase):
    MEDIA = {"group": "independent"}
    FILING = {"group": "authoritative"}
    CHECKED = {"verdict": "SUPPORTED", "evidence_page": 43, "evidence_title": "招股说明书"}
    UNCHECKED = {"verdict": "UNVERIFIABLE", "evidence_page": None, "evidence_title": None}

    def test_an_authoritative_source_is_said_so(self):
        self.assertEqual(source_basis([self.FILING, self.MEDIA], [])["kind"], "authoritative")

    def test_media_only_but_checked_against_a_filing(self):
        basis = source_basis([self.MEDIA], [self.CHECKED, self.UNCHECKED])
        self.assertEqual((basis["kind"], basis["checked_claims"], basis["filings_cited"]),
                         ("secondhand_checked", 1, ["招股说明书"]))

    def test_media_only_and_nothing_checked_is_secondhand(self):
        self.assertEqual(source_basis([self.MEDIA], [self.UNCHECKED])["kind"], "secondhand_only")


if __name__ == "__main__":
    unittest.main()
