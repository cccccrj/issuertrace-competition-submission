import sqlite3
import unittest

from processor.pipeline import PRODUCTION_MODES, ensure_schema
from processor.production_mode import (candidate_rows, classify_safely, contract, derived_reposts,
                                       ensure_label_schema, make_prompt, schema, store)


EVENT_DEFAULTS = {
    "event_types_json": "[]", "issue_codes_json": "[]", "production_mode": "BRIEF",
    "event_subjects_json": "[]", "affected_objects_json": "[]", "impact_direction": "neutral",
    "impact_magnitude": "low", "article_sentiment": "neutral", "relevance_score": 0.9,
    "eventhood": "EVENT", "summary": "摘要", "amounts_json": "[]", "ratios_json": "[]",
    "persons_json": "[]", "organizations_json": "[]", "products_json": "[]", "risk_score": 50.0,
    "risk_level": "medium", "recommended_action": "monitor", "label_source": "ai",
    "extraction_confidence": 0.9, "needs_review": 0, "raw_label_json": "{}",
    "created_at": "2026-08-01", "updated_at": "2026-08-01", "review_reasons_json": "[]",
}


def companion_tables(db):
    """The two tables this module reads that live in other schemas."""
    db.execute("""CREATE TABLE IF NOT EXISTS document_versions(
        version_id TEXT PRIMARY KEY, source_id TEXT, company_id TEXT, title TEXT,
        published_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS propagation_edges(
        edge_id TEXT PRIMARY KEY, company_id TEXT, edge_kind TEXT, from_kind TEXT,
        from_version_id TEXT, to_version_id TEXT)""")


def insert_event(db, event_id, version_id, company_id="cxmt", mode="BRIEF"):
    """An event row with every NOT NULL column filled, so a test can vary one thing."""
    values = dict(EVENT_DEFAULTS, event_id=event_id, version_id=version_id, run_id="r1",
                  company_id=company_id, production_mode=mode)
    columns = ",".join(values)
    db.execute(f"INSERT INTO event_records({columns}) VALUES({','.join('?' * len(values))})",
               list(values.values()))


class SchemaTests(unittest.TestCase):
    def test_every_production_mode_is_offered_to_the_model(self):
        # The first pass collapsed onto BRIEF partly because the prompt named
        # values it never explained; the enum and the guide must stay in step.
        self.assertEqual(schema()["properties"]["production_mode"]["enum"],
                         list(PRODUCTION_MODES))

    def test_the_guide_explains_every_value(self):
        for mode in PRODUCTION_MODES:
            self.assertIn(mode, contract().system_prompt)

    def test_brief_is_documented_as_the_last_resort(self):
        self.assertIn("才使用 BRIEF", contract().system_prompt)


class PromptTests(unittest.TestCase):
    def test_the_article_is_fenced_off_from_the_instructions(self):
        prompt = make_prompt("标题", "2026-08-01", "securities_times", "正文")
        self.assertIn("〔外部内容〕", prompt)
        self.assertIn("〔外部内容结束〕", prompt)

    def test_a_long_article_is_truncated(self):
        prompt = make_prompt("标题", "2026-08-01", "jiemian", "字" * 5000)
        self.assertLess(prompt.count("字"), 5000)


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        ensure_schema(self.db)
        ensure_label_schema(self.db)
        companion_tables(self.db)
        for index, source in enumerate(("securities_times", "sse_announcements")):
            version = f"v{index}"
            self.db.execute("""INSERT INTO document_versions(version_id,source_id,company_id,title,
                published_at) VALUES(?,?,?,?,?)""",
                (version, source, "cxmt", f"标题{index}", "2026-08-01"))
            self.db.execute("""INSERT INTO processed_documents(version_id,run_id,content_hash,
                text_chars,extraction_status,inclusion_status,relevance_score,eventhood,
                rejection_reasons_json,processor_version,processed_at,text_path)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (version, "r1", f"h{index}", 500, "ok", "included", 0.9, "EVENT", "[]",
                 "v2", "2026-08-01", f"data/text/{version}.txt"))
            insert_event(self.db, f"e{index}", version)
        self.db.commit()

    def test_sources_scope_the_run(self):
        rows = candidate_rows(self.db, None, {"securities_times"}, False)
        self.assertEqual([row["event_id"] for row in rows], ["e0"])

    def test_an_event_already_classified_is_skipped_until_forced(self):
        store(self.db, "e0", "cxmt", "AI_GENERATED", "模板化", 0.98, "model", "qwen-plus")
        self.assertEqual([row["event_id"] for row in candidate_rows(self.db, None, None, False)],
                         ["e1"])
        self.assertEqual(len(candidate_rows(self.db, None, None, True)), 2)

    def test_a_document_without_text_is_not_a_candidate(self):
        self.db.execute("UPDATE processed_documents SET text_path=NULL WHERE version_id='v0'")
        self.assertEqual([row["event_id"] for row in candidate_rows(self.db, None, None, False)],
                         ["e1"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        ensure_schema(self.db)
        ensure_label_schema(self.db)
        companion_tables(self.db)
        insert_event(self.db, "e0", "v0")

    def test_the_event_and_the_reason_are_both_written(self):
        # The event table is what the risk model reads; the label table is what
        # lets a reviewer disagree with one specific judgement.
        store(self.db, "e0", "cxmt", "SPONSORED", "通篇正面、无信源", 0.9, "model", "qwen-plus")
        self.assertEqual(self.db.execute(
            "SELECT production_mode FROM event_records WHERE event_id='e0'").fetchone()[0],
            "SPONSORED")
        row = self.db.execute("SELECT * FROM production_mode_labels WHERE event_id='e0'").fetchone()
        self.assertEqual((row["mode"], row["basis"], row["reason"]),
                         ("SPONSORED", "model", "通篇正面、无信源"))

    def test_reclassifying_replaces_rather_than_duplicates(self):
        store(self.db, "e0", "cxmt", "BRIEF", "一句话", 0.5, "model", "qwen-plus")
        store(self.db, "e0", "cxmt", "AI_GENERATED", "模板化", 0.98, "model", "qwen-plus")
        rows = self.db.execute("SELECT mode FROM production_mode_labels WHERE event_id='e0'").fetchall()
        self.assertEqual([row["mode"] for row in rows], ["AI_GENERATED"])


class DerivedRepostTests(unittest.TestCase):
    def test_a_carried_article_is_read_from_the_propagation_graph(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        companion_tables(db)
        db.execute("""INSERT INTO propagation_edges(edge_id,company_id,edge_kind,from_kind,
            from_version_id,to_version_id) VALUES('x','cxmt','REPOST','media','v1','v2')""")
        db.execute("""INSERT INTO propagation_edges(edge_id,company_id,edge_kind,from_kind,
            from_version_id,to_version_id) VALUES('y','cxmt','RECURRING','media','v3','v4')""")
        self.assertEqual(derived_reposts(db), {"v2"})


class FailureIsolationTests(unittest.TestCase):
    def test_one_document_failing_does_not_end_the_run(self):
        def explode(row):
            raise ValueError("gateway said no")
        row, payload, basis = classify_safely(explode)("row")
        self.assertIsNone(payload)
        self.assertEqual(basis, "failed")


if __name__ == "__main__":
    unittest.main()
