"""The narrow re-judgement of ``event_types``, and the boundaries it must keep.

Shape and clause list follow ``tests.test_article_sentiment``; two things are
specific to this module and are the reason it exists at all.

The claims fingerprint (``NarrowWriteTests``) is the invariant the whole approach
rests on: a ``claim_id`` is ``sha256(event_id, ordinal)`` and survives a
re-extraction that replaces the text beneath it, so re-running ``ai_label`` to fix
a prompt would leave 3,922 adjudications describing claims that no longer say what
they said (交付物11 §4.8).  A sidecar is only safe while it provably touches
neither table.

The risk replay (``RiskReplayTests``) is the other one: ``event_types`` is a
``risk_parts`` input, and writing it without replaying the score is exactly what
left 296 events scored on a production mode they no longer carried.
"""

import json
import sqlite3
import unittest

from processor.event_types import (
    LOW_CONFIDENCE, MAX_TYPES, candidate_rows, contract, ensure_label_schema, make_prompt,
    schema, store,
)
from processor.pipeline import EVENT_TYPES, ensure_schema

EVENT_DEFAULTS = {
    "event_types_json": '["CUSTOMER_SUPPLIER_OPERATIONS"]', "issue_codes_json": "[]",
    "production_mode": "ORIGINAL_INVESTIGATION", "event_subjects_json": "[]",
    "affected_objects_json": "[]", "impact_direction": "negative", "impact_magnitude": "major",
    "article_sentiment": "neutral", "relevance_score": 0.9, "eventhood": "EVENT",
    "summary": "摘要", "amounts_json": "[]", "ratios_json": "[]", "persons_json": "[]",
    "organizations_json": "[]", "products_json": "[]", "risk_score": 55.0,
    "risk_level": "medium", "recommended_action": "monitor", "label_source": "ai",
    "extraction_confidence": 0.9, "needs_review": 0, "raw_label_json": '{"event_types": ["X"]}',
    "created_at": "2026-08-01", "updated_at": "2026-08-01", "review_reasons_json": "[]",
}


def companion_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS document_versions(
        version_id TEXT PRIMARY KEY, source_id TEXT, company_id TEXT, title TEXT,
        published_at TEXT, document_type TEXT)""")


def insert_event(db, event_id, version_id, company_id="cxmt", **overrides):
    values = dict(EVENT_DEFAULTS, event_id=event_id, version_id=version_id, run_id="r1",
                  company_id=company_id, **overrides)
    columns = ",".join(values)
    db.execute(f"INSERT INTO event_records({columns}) VALUES({','.join('?' * len(values))})",
               list(values.values()))


def seeded_db(sources=("securities_times", "sse_announcements")):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    ensure_schema(db)
    ensure_label_schema(db)
    companion_tables(db)
    for index, source in enumerate(sources):
        version = f"v{index}"
        db.execute("""INSERT INTO document_versions(version_id,source_id,company_id,title,
            published_at,document_type) VALUES(?,?,?,?,?,?)""",
            (version, source, "cxmt", f"标题{index}", "2026-08-01", "independent_news"))
        db.execute("""INSERT INTO processed_documents(version_id,run_id,content_hash,
            text_chars,extraction_status,inclusion_status,relevance_score,eventhood,
            rejection_reasons_json,processor_version,processed_at,text_path)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (version, "r1", f"h{index}", 500, "ok", "included", 0.9, "EVENT", "[]",
             "v2", "2026-08-01", f"data/text/{version}.txt"))
        insert_event(db, f"e{index}", version)
    db.commit()
    return db


class SchemaTests(unittest.TestCase):
    def test_every_type_is_offered_to_the_model(self):
        self.assertEqual(schema()["properties"]["event_types"]["items"]["enum"], list(EVENT_TYPES))

    def test_the_guide_explains_every_type(self):
        # The original defect in one sentence: twelve values the extraction prompt
        # never named, let alone defined.
        for value in EVENT_TYPES:
            self.assertIn(value, contract().system_prompt)

    def test_an_empty_answer_is_allowed(self):
        # These twelve are drawn up by risk type and the organiser's names by
        # business action; some events fit none of them, and forcing one is worse
        # than saying so.
        self.assertNotIn("minItems", schema()["properties"]["event_types"])
        self.assertIn("返回空列表，不要硬凑", contract().system_prompt)

    def test_repeats_are_refused_by_the_schema(self):
        # 138 stored rows carry a type twice because the extraction schema never
        # declared uniqueItems.
        self.assertTrue(schema()["properties"]["event_types"]["uniqueItems"])
        self.assertEqual(schema()["properties"]["event_types"]["maxItems"], MAX_TYPES)

    def test_the_reason_must_cite_the_document_not_the_criteria(self):
        prompt = contract().system_prompt
        self.assertIn("reason 必须指出原文中支撑该判断的**具体内容**", prompt)
        self.assertIn("不要写「根据关键词/相似度」之类的计算过程", prompt)

    def test_the_speech_act_code_is_declared_not_a_fallback(self):
        self.assertIn("不得把它当兜底", contract().system_prompt)


class PromptTests(unittest.TestCase):
    def test_the_document_is_fenced_off_from_the_instructions(self):
        prompt = make_prompt("标题", "2026-08-01", "securities_times", "正文", "announcements")
        self.assertIn("〔外部内容〕", prompt)
        self.assertIn("〔外部内容结束〕", prompt)
        self.assertIn("announcements", prompt)

    def test_a_long_document_is_truncated(self):
        self.assertLess(make_prompt("标题", "2026-08-01", "jiemian", "字" * 5000).count("字"), 5000)


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.db = seeded_db()

    def test_a_filing_is_a_candidate_too(self):
        # Unlike production mode, this is not scoped to media: the announcement
        # half of the corpus is where EQUITY_CONTROL and FINANCIAL_AUDIT sit.
        rows = candidate_rows(self.db, None, False)
        self.assertEqual(sorted(row["event_id"] for row in rows), ["e0", "e1"])

    def test_an_event_already_judged_is_skipped_until_forced(self):
        store(self.db, "e0", "cxmt", ["IPO_REFINANCING"], "招股书受理", 0.9, "model", "qwen-plus")
        self.assertEqual([row["event_id"] for row in candidate_rows(self.db, None, False)], ["e1"])
        self.assertEqual(len(candidate_rows(self.db, None, True)), 2)

    def test_a_company_filter_scopes_the_run(self):
        self.db.execute("""INSERT INTO document_versions(version_id,source_id,company_id,title,
            published_at,document_type) VALUES('v9','yicai','wuxi_apptec','标题9','2026-08-01',
            'independent_news')""")
        self.db.execute("""INSERT INTO processed_documents(version_id,run_id,content_hash,
            text_chars,extraction_status,inclusion_status,relevance_score,eventhood,
            rejection_reasons_json,processor_version,processed_at,text_path)
            VALUES('v9','r1','h9',500,'ok','included',0.9,'EVENT','[]','v2','2026-08-01',
                   'data/text/v9.txt')""")
        insert_event(self.db, "e9", "v9", company_id="wuxi_apptec")
        self.assertNotIn("e9", [row["event_id"] for row in candidate_rows(self.db, {"cxmt"}, False)])

    def test_a_document_without_text_is_not_a_candidate(self):
        self.db.execute("UPDATE processed_documents SET text_path=NULL WHERE version_id='v0'")
        self.assertEqual([row["event_id"] for row in candidate_rows(self.db, None, False)], ["e1"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.db = seeded_db()

    def test_the_event_and_the_reason_are_both_written(self):
        store(self.db, "e0", "cxmt", ["IPO_REFINANCING", "RESPONSE_CLARIFICATION"],
              "公告称上市委会议通过", 0.88, "model", "qwen-plus")
        self.assertEqual(json.loads(self.db.execute(
            "SELECT event_types_json FROM event_records WHERE event_id='e0'").fetchone()[0]),
            ["IPO_REFINANCING", "RESPONSE_CLARIFICATION"])
        row = self.db.execute("SELECT * FROM event_type_labels WHERE event_id='e0'").fetchone()
        self.assertEqual((row["basis"], row["reason"], row["confidence"]),
                         ("model", "公告称上市委会议通过", 0.88))
        self.assertEqual(row["prompt_version"], "event-types-v2")

    def test_an_empty_answer_is_stored_as_an_empty_answer(self):
        store(self.db, "e0", "cxmt", [], "这是一则指数播报，与本主体无关", 0.7, "model", "qwen-plus")
        self.assertEqual(self.db.execute(
            "SELECT event_types_json FROM event_records WHERE event_id='e0'").fetchone()[0], "[]")

    def test_a_repeat_does_not_survive_the_write(self):
        store(self.db, "e0", "cxmt", ["FINANCIAL_AUDIT", "FINANCIAL_AUDIT"], "理由", 0.9,
              "model", "qwen-plus")
        self.assertEqual(json.loads(self.db.execute(
            "SELECT event_types_json FROM event_records WHERE event_id='e0'").fetchone()[0]),
            ["FINANCIAL_AUDIT"])

    def test_a_low_confidence_judgement_goes_to_the_review_queue(self):
        store(self.db, "e0", "cxmt", ["ESG_REPUTATION"], "理由", LOW_CONFIDENCE - 0.1,
              "model", "qwen-plus")
        row = self.db.execute(
            "SELECT needs_review, review_reasons_json FROM event_records WHERE event_id='e0'").fetchone()
        self.assertEqual(row["needs_review"], 1)
        self.assertIn("event_type_low_confidence", json.loads(row["review_reasons_json"]))

    def test_a_confident_judgement_clears_that_reason_again(self):
        store(self.db, "e0", "cxmt", ["ESG_REPUTATION"], "理由", 0.2, "model", "qwen-plus")
        store(self.db, "e0", "cxmt", ["ESG_REPUTATION"], "理由", 0.95, "model", "qwen-plus")
        row = self.db.execute(
            "SELECT needs_review, review_reasons_json FROM event_records WHERE event_id='e0'").fetchone()
        self.assertEqual(json.loads(row["review_reasons_json"]), [])
        self.assertEqual(row["needs_review"], 0)


class RiskReplayTests(unittest.TestCase):
    """event_types is a risk input; writing it without replaying the score is the
    defect that left 296 events scored on a value they no longer carried."""

    def setUp(self):
        self.db = seeded_db()

    def test_gaining_a_sensitive_type_moves_the_score_and_the_breakdown(self):
        before = self.db.execute("SELECT risk_score FROM event_records WHERE event_id='e0'").fetchone()[0]
        store(self.db, "e0", "cxmt", ["REGULATORY_COMPLIANCE"], "监管出具警示函", 0.9,
              "model", "qwen-plus")
        row = self.db.execute("""SELECT risk_score, risk_level, risk_breakdown_json
                                 FROM event_records WHERE event_id='e0'""").fetchone()
        self.assertGreater(row["risk_score"], before)
        parts = json.loads(row["risk_breakdown_json"])
        self.assertIn("sensitive_type", {part["key"] for part in parts})
        self.assertEqual(round(sum(part["points"] for part in parts), 1), row["risk_score"])

    def test_losing_the_sensitive_type_takes_the_points_back(self):
        store(self.db, "e0", "cxmt", ["REGULATORY_COMPLIANCE"], "理由", 0.9, "model", "qwen-plus")
        high = self.db.execute("SELECT risk_score FROM event_records WHERE event_id='e0'").fetchone()[0]
        store(self.db, "e0", "cxmt", ["CUSTOMER_SUPPLIER_OPERATIONS"], "理由", 0.9, "model", "qwen-plus")
        self.assertLess(self.db.execute(
            "SELECT risk_score FROM event_records WHERE event_id='e0'").fetchone()[0], high)


class NarrowWriteTests(unittest.TestCase):
    """The reason this is a sidecar and not a re-extraction.

    A claim_id is sha256(event_id, ordinal): it survives a re-extraction that
    replaces the text beneath it, so 3,922 adjudications keyed on those ids would
    silently come to describe claims that no longer say what they said.
    """

    def setUp(self):
        self.db = seeded_db()
        self.db.execute("""INSERT INTO event_claims(claim_id,event_id,ordinal,text,issue_code,
            claim_type,source_start,source_end,cited_source,confidence)
            VALUES('c1','e0',1,'公司募集资金总额为 295 亿元','FUND_RAISE','fact',10,28,'第七节',0.9)""")
        self.db.commit()

    def fingerprint(self):
        return self.db.execute("""SELECT claim_id, text, source_start, source_end, issue_code
                                  FROM event_claims ORDER BY claim_id""").fetchall()

    def test_claims_are_untouched_by_a_re_judgement(self):
        before = [tuple(row) for row in self.fingerprint()]
        store(self.db, "e0", "cxmt", ["EQUITY_CONTROL"], "实控人变更", 0.9, "model", "qwen-plus")
        self.assertEqual([tuple(row) for row in self.fingerprint()], before)

    def test_the_extraction_provenance_is_left_alone(self):
        # prompt_version records which prompt produced the claims, and
        # raw_label_json is that prompt's own answer.  Neither is this module's
        # to rewrite -- the 1,907 stored events were extracted by event-label-v2
        # and must keep saying so.
        before = self.db.execute("""SELECT prompt_version, raw_label_json
                                    FROM event_records WHERE event_id='e0'""").fetchone()
        store(self.db, "e0", "cxmt", ["EQUITY_CONTROL"], "实控人变更", 0.9, "model", "qwen-plus")
        after = self.db.execute("""SELECT prompt_version, raw_label_json
                                   FROM event_records WHERE event_id='e0'""").fetchone()
        self.assertEqual(tuple(before), tuple(after))


if __name__ == "__main__":
    unittest.main()
