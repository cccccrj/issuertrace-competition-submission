import sqlite3
import unittest

from processor.article_sentiment import (SENTIMENTS, candidate_rows, classify_safely, contract,
                                         ensure_label_schema, make_prompt, schema, store)
from processor.pipeline import ensure_schema


EVENT_DEFAULTS = {
    "event_types_json": "[]", "issue_codes_json": "[]", "production_mode": "BRIEF",
    "event_subjects_json": "[]", "affected_objects_json": "[]", "impact_direction": "negative",
    "impact_magnitude": "major", "article_sentiment": "negative", "relevance_score": 0.9,
    "eventhood": "EVENT", "summary": "摘要", "amounts_json": "[]", "ratios_json": "[]",
    "persons_json": "[]", "organizations_json": "[]", "products_json": "[]", "risk_score": 50.0,
    "risk_level": "medium", "recommended_action": "monitor", "label_source": "ai",
    "extraction_confidence": 0.9, "needs_review": 0, "raw_label_json": "{}",
    "created_at": "2026-08-01", "updated_at": "2026-08-01", "review_reasons_json": "[]",
}


def companion_tables(db):
    """The one table this module reads that lives in another schema."""
    db.execute("""CREATE TABLE IF NOT EXISTS document_versions(
        version_id TEXT PRIMARY KEY, source_id TEXT, company_id TEXT, title TEXT,
        published_at TEXT)""")


def insert_event(db, event_id, version_id, company_id="cxmt",
                 sentiment="negative", impact="negative"):
    """An event row with every NOT NULL column filled, so a test can vary one thing."""
    values = dict(EVENT_DEFAULTS, event_id=event_id, version_id=version_id, run_id="r1",
                  company_id=company_id, article_sentiment=sentiment, impact_direction=impact)
    columns = ",".join(values)
    db.execute(f"INSERT INTO event_records({columns}) VALUES({','.join('?' * len(values))})",
               list(values.values()))


class SchemaTests(unittest.TestCase):
    def test_every_value_is_offered_to_the_model(self):
        self.assertEqual(schema()["properties"]["article_sentiment"]["enum"], list(SENTIMENTS))

    def test_the_guide_explains_every_value(self):
        # The original defect was an enum the prompt never defined; the values
        # and their guide must stay in step.
        for value in SENTIMENTS:
            self.assertIn(value, contract().system_prompt)

    def test_the_prompt_separates_tone_from_impact(self):
        # The whole point: tone is not the event's consequence.  If this sentence
        # ever leaves, the field collapses back onto impact_direction.
        prompt = contract().system_prompt
        self.assertIn("语气看措辞，不看事件后果", prompt)


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

    def test_a_filing_is_a_candidate_too(self):
        # Unlike production mode, tone applies to filings; both rows are in scope.
        rows = candidate_rows(self.db, None, None, False)
        self.assertEqual(sorted(row["event_id"] for row in rows), ["e0", "e1"])

    def test_sources_scope_the_run(self):
        rows = candidate_rows(self.db, None, {"sse_announcements"}, False)
        self.assertEqual([row["event_id"] for row in rows], ["e1"])

    def test_impact_direction_is_carried_for_the_divergence_report(self):
        rows = candidate_rows(self.db, None, {"securities_times"}, False)
        self.assertEqual(rows[0]["impact_direction"], "negative")

    def test_an_event_already_classified_is_skipped_until_forced(self):
        store(self.db, "e0", "cxmt", "positive", "褒扬", 0.98, "model", "qwen-plus")
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
        # A dry filing: negative impact, and the fix is to let its tone read neutral.
        insert_event(self.db, "e0", "v0", sentiment="negative", impact="negative")

    def test_the_event_and_the_reason_are_both_written(self):
        store(self.db, "e0", "cxmt", "neutral", "通篇陈述事实与法条", 0.9, "model", "qwen-plus")
        self.assertEqual(self.db.execute(
            "SELECT article_sentiment FROM event_records WHERE event_id='e0'").fetchone()[0],
            "neutral")
        row = self.db.execute(
            "SELECT * FROM article_sentiment_labels WHERE event_id='e0'").fetchone()
        self.assertEqual((row["sentiment"], row["basis"], row["reason"]),
                         ("neutral", "model", "通篇陈述事实与法条"))

    def test_tone_can_diverge_from_impact_after_the_fix(self):
        # Before: article_sentiment == impact_direction == negative. After: the
        # tone is neutral while impact stays negative — the split the field is for.
        store(self.db, "e0", "cxmt", "neutral", "措辞平实", 0.9, "model", "qwen-plus")
        row = self.db.execute(
            "SELECT article_sentiment, impact_direction FROM event_records WHERE event_id='e0'"
        ).fetchone()
        self.assertNotEqual(row["article_sentiment"], row["impact_direction"])

    def test_reclassifying_replaces_rather_than_duplicates(self):
        store(self.db, "e0", "cxmt", "neutral", "一版", 0.5, "model", "qwen-plus")
        store(self.db, "e0", "cxmt", "positive", "二版", 0.98, "model", "qwen-plus")
        rows = self.db.execute(
            "SELECT sentiment FROM article_sentiment_labels WHERE event_id='e0'").fetchall()
        self.assertEqual([row["sentiment"] for row in rows], ["positive"])


class FailureIsolationTests(unittest.TestCase):
    def test_one_document_failing_does_not_end_the_run(self):
        def explode(row):
            raise ValueError("gateway said no")
        row, payload, basis = classify_safely(explode)("row")
        self.assertIsNone(payload)
        self.assertEqual(basis, "failed")


if __name__ == "__main__":
    unittest.main()
