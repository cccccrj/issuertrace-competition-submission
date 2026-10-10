import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from processor import benchmark
from processor.benchmark import (
    connect, duplicate_ids, ensure_schema, entity_of, gold_version_id,
    import_split, import_taxonomy, integrity, summarise,
)
from tests.test_xlsx_reader import build_workbook

COMPANY_HEADER = ["article_file_id", "article_title", "article_publish_time", "article_source",
                  "content", "trading_code", "secu_abbr", "industry_code", "industry_name",
                  "event_name", "event_emotion", "event_impact_analysis", "duplication_id"]
DUPLICATE_HEADER = ["article_file_id", "article_title", "article_source", "content",
                    "article_publish_time", "duplication_id"]
COLUMNS = "ABCDEFGHIJKLM"


def spec(strings, header_names, records):
    """Build a sheet spec plus the shared-string table it references."""
    def index(value):
        if value not in strings:
            strings.append(value)
        return strings.index(value)

    body = [{COLUMNS[i]: ("s", index(name)) for i, name in enumerate(header_names)}]
    for record in records:
        row = {}
        for i, name in enumerate(header_names):
            value = record.get(name)
            if value is None:
                continue  # an absent cell, exactly as Excel writes it
            if name == "article_publish_time":
                row[COLUMNS[i]] = ("n", value)
            else:
                row[COLUMNS[i]] = ("s", index(value))
        body.append(row)
    return body


class BenchmarkFixture(unittest.TestCase):
    """A miniature dataset: two companies, one industry, one duplicate star."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name)
        strings = []
        company = spec(strings, COMPANY_HEADER, [
            {"article_file_id": "A1", "article_title": "甲公司发布新品",
             "article_publish_time": "46024", "article_source": "新浪财经",
             "content": "甲公司今日发布新一代产品。" * 12, "trading_code": "000001",
             "secu_abbr": "甲公司", "industry_code": "280500", "industry_name": "乘用车",
             "event_name": "产品技术创新", "event_emotion": "正面",
             "event_impact_analysis": "影响分析甲", "duplication_id": "D1,D2"},
            {"article_file_id": "A2", "article_title": "乙公司战略调整",
             "article_publish_time": "46025", "article_source": "证券时报",
             "content": "乙公司宣布调整业务战略。" * 12, "trading_code": "000002",
             "secu_abbr": "乙公司", "industry_code": "280500", "industry_name": "乘用车",
             "event_name": "公司战略调整", "event_emotion": "中性",
             "event_impact_analysis": "影响分析乙"},  # duplication_id absent
        ])
        industry = spec(strings, COMPANY_HEADER[:5] + COMPANY_HEADER[7:], [
            {"article_file_id": "B1", "article_title": "行业政策出台",
             "article_publish_time": "46026", "article_source": "中国证券报",
             "content": "监管部门发布行业新政。" * 12, "industry_code": "110100",
             "industry_name": "种植业", "event_name": "农业政策变动", "event_emotion": "正面",
             "event_impact_analysis": "影响分析丙"},
        ])
        duplicates = spec(strings, DUPLICATE_HEADER, [
            {"article_file_id": "D1", "article_title": "甲公司发布新品",
             "article_source": "腾讯网", "content": "甲公司今日发布新一代产品。" * 12,
             "article_publish_time": "46024", "duplication_id": "A1"},
            {"article_file_id": "D2", "article_title": "甲公司发布新品",
             "article_source": "东方财富网", "content": "甲公司今日发布新一代产品。" * 12,
             "article_publish_time": "46024", "duplication_id": "A1"},
        ])
        industry_duplicates = spec(strings, DUPLICATE_HEADER, [
            {"article_file_id": "E1", "article_title": "行业政策出台",
             "article_source": "金融界", "content": "监管部门发布行业新政。" * 12,
             "article_publish_time": "46026", "duplication_id": "B1"},
        ])
        build_workbook(self.source / "news_with_tags_train.xlsx", [
            ("个股新闻", company), ("行业新闻", industry),
            ("个股重复新闻", duplicates), ("行业重复新闻", industry_duplicates),
        ], strings)
        (self.source / "事件类型_标的.json").write_text(json.dumps([
            {"company": {"trading_code": "000001", "secu_abbr": "甲公司"},
             "event_schema": [{"event_name": "产品技术创新", "description": "定义甲"}]},
            {"company": {"trading_code": "000002", "secu_abbr": "乙公司"},
             "event_schema": [{"event_name": "公司战略调整", "description": "定义乙"},
                              {"event_name": "资本市场运作", "description": "定义乙2"}]},
        ], ensure_ascii=False), encoding="utf-8")
        (self.source / "事件类型_行业.json").write_text(json.dumps([
            {"industry": {"inducode": "110100", "induname": "种植业"},
             "event_schema": [{"event_name": "农业政策变动", "description": "定义丙"}]},
        ], ensure_ascii=False), encoding="utf-8")
        self.db = connect(Path(self.tmp.name) / "benchmark.sqlite3")
        self.addCleanup(self.db.close)
        ensure_schema(self.db)

    def load(self):
        import_taxonomy(self.db, self.source)
        return import_split(self.db, "train", self.source)


class SchemaTests(BenchmarkFixture):
    def test_ensure_schema_is_idempotent(self):
        ensure_schema(self.db)
        ensure_schema(self.db)
        tables = {row[0] for row in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertLessEqual({"gold_articles", "gold_article_text", "gold_event_labels",
                              "gold_duplicate_links", "gold_event_schema", "benchmark_runs",
                              "benchmark_predictions", "benchmark_metrics",
                              "benchmark_exclusions"}, tables)

    def test_gold_version_ids_are_prefixed_and_stable(self):
        first = gold_version_id("A1")
        self.assertEqual(first, gold_version_id("A1"))
        self.assertNotEqual(first, gold_version_id("A2"))

    def test_the_harness_never_creates_corpus_tables(self):
        # The claim_id hazard is avoided by construction, not by discipline.
        self.load()
        tables = {row[0] for row in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("event_records", tables)
        self.assertNotIn("event_claims", tables)
        self.assertNotIn("claim_verdicts", tables)


class ImportTests(BenchmarkFixture):
    def test_every_sheet_is_counted(self):
        counts = self.load()
        self.assertEqual(counts, {"个股新闻": 2, "行业新闻": 1,
                                  "个股重复新闻": 2, "行业重复新闻": 1})

    def test_rows_land_with_entity_keys_and_iso_dates(self):
        self.load()
        row = self.db.execute(
            "SELECT * FROM gold_articles WHERE article_file_id='A1'").fetchone()
        self.assertEqual(row["entity_kind"], "company")
        self.assertEqual(row["entity_id"], "co:000001")
        self.assertEqual(row["published_at"], "2026-01-02")
        self.assertEqual(row["split"], "train")

    def test_company_and_industry_keys_cannot_collide(self):
        self.load()
        kinds = {row["entity_id"] for row in self.db.execute(
            "SELECT DISTINCT entity_id FROM gold_articles WHERE entity_id IS NOT NULL")}
        self.assertEqual(kinds, {"co:000001", "co:000002", "ind:110100"})

    def test_duplicate_rows_carry_no_entity_or_label(self):
        self.load()
        row = self.db.execute(
            "SELECT * FROM gold_articles WHERE article_file_id='D1'").fetchone()
        self.assertIsNone(row["entity_id"])
        self.assertIsNone(self.db.execute(
            "SELECT event_name FROM gold_event_labels WHERE article_file_id='D1'").fetchone())

    def test_both_link_directions_are_preserved(self):
        self.load()
        links = {(row["child_article_id"], row["parent_article_id"], row["link_direction"])
                 for row in self.db.execute("SELECT * FROM gold_duplicate_links")}
        self.assertIn(("D1", "A1", "parent_lists_child"), links)
        self.assertIn(("D1", "A1", "child_names_parent"), links)
        self.assertIn(("E1", "B1", "child_names_parent"), links)

    def test_a_missing_duplication_id_creates_no_link(self):
        self.load()
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM gold_duplicate_links WHERE parent_article_id='A2'"
        ).fetchone()[0], 0)

    def test_reimport_does_not_duplicate_rows(self):
        self.load()
        before = self.db.execute("SELECT COUNT(*) FROM gold_articles").fetchone()[0]
        links_before = self.db.execute("SELECT COUNT(*) FROM gold_duplicate_links").fetchone()[0]
        self.load()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM gold_articles").fetchone()[0], before)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM gold_duplicate_links").fetchone()[0], links_before)

    def test_article_text_is_stored_apart_from_metadata(self):
        self.load()
        text = self.db.execute(
            "SELECT text FROM gold_article_text WHERE article_file_id='A1'").fetchone()["text"]
        self.assertIn("甲公司今日发布新一代产品", text)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(gold_articles)")}
        self.assertNotIn("text", columns)


class TaxonomyTests(BenchmarkFixture):
    def test_definitions_load_for_both_entity_kinds(self):
        counts = import_taxonomy(self.db, self.source)
        self.assertEqual(counts, {"company": 3, "industry": 1})

    def test_descriptions_are_kept_for_the_prompt(self):
        import_taxonomy(self.db, self.source)
        row = self.db.execute(
            "SELECT description, entity_name FROM gold_event_schema WHERE entity_id='co:000001'"
        ).fetchone()
        self.assertEqual(row["description"], "定义甲")
        self.assertEqual(row["entity_name"], "甲公司")


class IntegrityTests(BenchmarkFixture):
    def test_a_clean_import_reports_no_anomalies(self):
        self.load()
        facts = integrity(self.db)
        self.assertEqual(facts["labels_outside_entity_schema"], 0)
        self.assertEqual(facts["articles_in_both_splits"], 0)
        self.assertEqual(facts["links_with_unknown_parent"], 0)
        self.assertEqual(facts["labelled_rows"], 3)

    def test_a_label_outside_its_entity_schema_is_counted(self):
        self.load()
        self.db.execute(
            "UPDATE gold_event_labels SET event_name='不存在的事件' WHERE article_file_id='A1'")
        self.assertEqual(integrity(self.db)["labels_outside_entity_schema"], 1)

    def test_a_link_to_an_unknown_parent_is_counted(self):
        self.load()
        self.db.execute("""INSERT INTO gold_duplicate_links
            (child_article_id,parent_article_id,split,link_direction)
            VALUES('D1','MISSING','train','child_names_parent')""")
        self.assertEqual(integrity(self.db)["links_with_unknown_parent"], 1)
        self.assertEqual(integrity(self.db)["children_naming_more_than_one_parent"], 1)

    def test_summary_reports_the_emotion_distribution(self):
        self.load()
        emotions = summarise(self.db)["splits"]["train"]["event_emotion"]
        self.assertEqual(emotions, {"正面": 2, "中性": 1})


class HelperTests(unittest.TestCase):
    def test_duplication_ids_split_on_commas_and_drop_blanks(self):
        self.assertEqual(duplicate_ids("A, B ,,C"), ["A", "B", "C"])
        self.assertEqual(duplicate_ids(None), [])
        self.assertEqual(duplicate_ids(""), [])

    def test_entity_of_uses_the_right_code_per_sheet(self):
        self.assertEqual(entity_of("个股新闻", {"trading_code": "600000"}), ("company", "co:600000"))
        self.assertEqual(entity_of("行业新闻", {"industry_code": "110100"}),
                         ("industry", "ind:110100"))
        self.assertEqual(entity_of("个股重复新闻", {"trading_code": "600000"}), (None, None))
        self.assertEqual(entity_of("个股新闻", {"trading_code": ""}), (None, None))


class ExtractTests(unittest.TestCase):
    def test_only_this_track_is_unpacked(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        archive_path = root / "dataset.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            for member in benchmark.DATASET_MEMBERS:
                archive.writestr(f"{benchmark.DATASET_NAME}/{member}", b"x")
            archive.writestr("另一个赛题的资料/巨大的表.csv", b"y" * 100)
        target = root / "gold"
        written = benchmark.extract(archive_path, target)
        self.assertEqual({path.name for path in written}, set(benchmark.DATASET_MEMBERS))
        self.assertEqual({path.name for path in target.iterdir()}, set(benchmark.DATASET_MEMBERS))

    def test_a_missing_member_is_reported_rather_than_half_extracted(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        archive_path = root / "dataset.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(f"{benchmark.DATASET_NAME}/news_with_tags_train.xlsx", b"x")
        with self.assertRaises(FileNotFoundError):
            benchmark.extract(archive_path, root / "gold")


if __name__ == "__main__":
    unittest.main()
