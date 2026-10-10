"""The narrow re-judgement of the impact triple, and the two axes it must not merge.

Beyond the clause list shared with ``tests.test_event_types``, three things here
are specific and each records a decision:

* ``AxisSeparationTests`` — this module never writes ``article_sentiment``.  Tone
  and impact are separate outputs that are *allowed and expected* to disagree
  (方案 §8.4), and the corpus's 512 disagreeing rows are the repair that
  established the split.  A sidecar that judged both would quietly undo it.
* ``MagnitudeBasisTests`` — a magnitude whose stated basis cannot support it is
  stored as given and sent to review.  Correcting either half silently would
  destroy the only signal that the judgement was unsound.
* ``NarrowWriteTests`` — the claims fingerprint, for the same reason as in
  ``event_types``: a ``claim_id`` survives a re-extraction that replaces the text
  beneath it (交付物11 §4.8).
"""

import json
import sqlite3
import unittest

from processor.event_impact import (
    LOW_CONFIDENCE, MAX_OBJECTS, basis_conflicts, candidate_rows, contract, ensure_label_schema,
    entity_name, make_prompt, schema, store,
)
from processor.pipeline import (
    IMPACT_DIRECTIONS, IMPACT_MAGNITUDES, IMPACT_OBJECTS, MAGNITUDE_BASES, ensure_schema,
)
from tests.test_event_types import companion_tables, insert_event


def seeded_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    ensure_schema(db)
    ensure_label_schema(db)
    companion_tables(db)
    db.execute("""INSERT INTO document_versions(version_id,source_id,company_id,title,
        published_at,document_type) VALUES('v0','sse_announcements','cxmt','某处罚决定书',
        '2026-08-01','announcements')""")
    db.execute("""INSERT INTO processed_documents(version_id,run_id,content_hash,text_chars,
        extraction_status,inclusion_status,relevance_score,eventhood,rejection_reasons_json,
        processor_version,processed_at,text_path)
        VALUES('v0','r1','h0',500,'ok','included',0.9,'EVENT','[]','v2','2026-08-01',
               'data/text/v0.txt')""")
    # A dry filing: neutral in tone, negative in impact — the canonical split.
    insert_event(db, "e0", "v0", article_sentiment="neutral", impact_direction="negative",
                 impact_magnitude="major")
    db.commit()
    return db


PAYLOAD = {"affected_objects": ["regulatory_compliance", "reputation_brand"],
           "impact_direction": "negative", "impact_magnitude": "moderate",
           "magnitude_basis": "single_scope", "reason": "决定书载明罚款 200 万元，限于单个子公司",
           "confidence": 0.9}


class SchemaTests(unittest.TestCase):
    def test_all_three_vocabularies_are_offered_in_one_call(self):
        # Target, sign and length of one vector: event_emotion established that
        # polarity without a named subject has no meaning, so they are not split.
        properties = schema()["properties"]
        self.assertEqual(properties["affected_objects"]["items"]["enum"], list(IMPACT_OBJECTS))
        self.assertEqual(properties["impact_direction"]["enum"], list(IMPACT_DIRECTIONS))
        self.assertEqual(properties["impact_magnitude"]["enum"], list(IMPACT_MAGNITUDES))
        self.assertEqual(properties["magnitude_basis"]["enum"], list(MAGNITUDE_BASES))

    def test_the_guide_explains_every_value_of_every_one(self):
        prompt = contract().system_prompt
        for value in list(IMPACT_OBJECTS) + list(IMPACT_DIRECTIONS) + list(IMPACT_MAGNITUDES):
            self.assertIn(value, prompt)

    def test_the_fourth_direction_is_reachable_here(self):
        # `uncertain` cannot be produced by the rule path at all, which is why
        # only 18 events in the corpus carry it.
        self.assertIn("uncertain", schema()["properties"]["impact_direction"]["enum"])
        self.assertIn("uncertain 待定", contract().system_prompt)

    def test_objects_are_capped_and_deduplicated(self):
        self.assertEqual(schema()["properties"]["affected_objects"]["maxItems"], MAX_OBJECTS)
        self.assertTrue(schema()["properties"]["affected_objects"]["uniqueItems"])

    def test_the_prompt_keeps_tone_on_the_other_axis(self):
        self.assertIn("行文语气由 article_sentiment 另行承担", contract().system_prompt)

    def test_the_prompt_names_the_two_over_used_objects(self):
        prompt = contract().system_prompt
        self.assertIn("`shareholders_control` 是最容易被滥用的取值", prompt)
        self.assertIn("任何披露都可能影响股价", prompt)

    def test_major_must_name_its_threshold(self):
        self.assertIn("major 是需要举证的判断", contract().system_prompt)


class PromptTests(unittest.TestCase):
    def test_the_subject_is_named_because_polarity_is_relative_to_it(self):
        prompt = make_prompt("某公司", "标题", "2026-08-01", "cninfo", "正文")
        self.assertIn("指定主体：某公司", prompt)
        self.assertIn("〔外部内容〕", prompt)

    def test_the_subject_name_comes_from_configuration(self):
        # Not from a dict in the module: a company identifier in the processing
        # layer is what E7's zero-code-change claim has to be false about, and
        # tests.test_migration fails on any processor file that names one.
        from processor.pipeline import company_display_name, configured_companies
        for company in configured_companies():
            with self.subTest(company=company):
                self.assertEqual(entity_name({"company_id": company}),
                                 company_display_name(company))
                self.assertNotEqual(entity_name({"company_id": company}), company)
        self.assertEqual(entity_name({"company_id": None}), "未指明")


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.db = seeded_db()

    def test_tone_is_carried_so_the_run_can_report_the_divergence(self):
        rows = candidate_rows(self.db, None, False)
        self.assertEqual(rows[0]["article_sentiment"], "neutral")

    def test_an_event_already_judged_is_skipped_until_forced(self):
        store(self.db, "e0", "cxmt", PAYLOAD, "model", "qwen-plus")
        self.assertEqual(candidate_rows(self.db, None, False), [])
        self.assertEqual(len(candidate_rows(self.db, None, True)), 1)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.db = seeded_db()

    def test_all_three_columns_and_the_reason_are_written(self):
        store(self.db, "e0", "cxmt", PAYLOAD, "model", "qwen-plus")
        row = self.db.execute("""SELECT affected_objects_json, impact_direction, impact_magnitude
                                 FROM event_records WHERE event_id='e0'""").fetchone()
        self.assertEqual(json.loads(row["affected_objects_json"]),
                         ["regulatory_compliance", "reputation_brand"])
        self.assertEqual(row["impact_magnitude"], "moderate")
        label = self.db.execute("SELECT * FROM event_impact_labels WHERE event_id='e0'").fetchone()
        self.assertEqual(label["magnitude_basis"], "single_scope")
        self.assertIn("罚款 200 万元", label["reason"])
        self.assertEqual(label["prompt_version"], "event-impact-v3")

    def test_a_repeated_object_does_not_survive_the_write(self):
        payload = dict(PAYLOAD, affected_objects=["customers", "customers"])
        store(self.db, "e0", "cxmt", payload, "model", "qwen-plus")
        self.assertEqual(json.loads(self.db.execute(
            "SELECT affected_objects_json FROM event_records WHERE event_id='e0'").fetchone()[0]),
            ["customers"])

    def test_an_empty_object_list_is_stored_as_such(self):
        # 「一个对象也判断不出来时给空列表，不要拿经营与业务顶替」.
        store(self.db, "e0", "cxmt", dict(PAYLOAD, affected_objects=[]), "model", "qwen-plus")
        self.assertEqual(self.db.execute(
            "SELECT affected_objects_json FROM event_records WHERE event_id='e0'").fetchone()[0], "[]")

    def test_a_low_confidence_judgement_goes_to_review(self):
        store(self.db, "e0", "cxmt", dict(PAYLOAD, confidence=LOW_CONFIDENCE - 0.2),
              "model", "qwen-plus")
        reasons = json.loads(self.db.execute(
            "SELECT review_reasons_json FROM event_records WHERE event_id='e0'").fetchone()[0])
        self.assertIn("impact_low_confidence", reasons)

    def test_the_risk_score_is_replayed_with_the_new_direction(self):
        # negative is +20 and major another +20: 40 of the score's points are
        # exactly the two fields this module rewrites.
        before = self.db.execute("SELECT risk_score FROM event_records WHERE event_id='e0'").fetchone()[0]
        store(self.db, "e0", "cxmt", dict(PAYLOAD, impact_direction="neutral",
                                          impact_magnitude="minor",
                                          magnitude_basis="routine_procedural"),
              "model", "qwen-plus")
        row = self.db.execute("""SELECT risk_score, risk_breakdown_json
                                 FROM event_records WHERE event_id='e0'""").fetchone()
        self.assertLess(row["risk_score"], before)
        parts = json.loads(row["risk_breakdown_json"])
        self.assertNotIn("direction", {part["key"] for part in parts})
        self.assertEqual(round(sum(part["points"] for part in parts), 1), row["risk_score"])


class AxisSeparationTests(unittest.TestCase):
    def setUp(self):
        self.db = seeded_db()

    def test_tone_is_never_written_by_this_module(self):
        # The dry filing keeps its neutral tone while its impact stays negative.
        # Judging both here would collapse the split article_sentiment exists for.
        store(self.db, "e0", "cxmt", PAYLOAD, "model", "qwen-plus")
        row = self.db.execute("""SELECT article_sentiment, impact_direction
                                 FROM event_records WHERE event_id='e0'""").fetchone()
        self.assertEqual(row["article_sentiment"], "neutral")
        self.assertEqual(row["impact_direction"], "negative")

    def test_the_module_writes_no_sentiment_label_row(self):
        store(self.db, "e0", "cxmt", PAYLOAD, "model", "qwen-plus")
        tables = {row[0] for row in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("article_sentiment_labels", tables)


class MagnitudeBasisTests(unittest.TestCase):
    def test_a_basis_that_cannot_support_major_is_a_conflict(self):
        self.assertTrue(basis_conflicts("major", "routine_procedural"))
        self.assertFalse(basis_conflicts("major", "scale_threshold"))
        self.assertFalse(basis_conflicts("minor", "routine_procedural"))

    def test_a_missing_basis_is_not_treated_as_a_conflict(self):
        self.assertFalse(basis_conflicts("major", None))

    def test_a_conflict_is_stored_as_given_and_sent_to_review(self):
        # Not silently corrected in either direction: a model that says 「重大」 and
        # then points at a routine filing has told us something about that
        # judgement, and overwriting half of it destroys the only signal.
        db = seeded_db()
        store(db, "e0", "cxmt", dict(PAYLOAD, impact_magnitude="major",
                                     magnitude_basis="routine_procedural"),
              "model", "qwen-plus")
        row = db.execute("""SELECT e.impact_magnitude, e.review_reasons_json, l.magnitude_basis
                            FROM event_records e JOIN event_impact_labels l ON l.event_id=e.event_id
                            WHERE e.event_id='e0'""").fetchone()
        self.assertEqual(row["impact_magnitude"], "major")
        self.assertEqual(row["magnitude_basis"], "routine_procedural")
        self.assertIn("magnitude_basis_conflict", json.loads(row["review_reasons_json"]))


class NarrowWriteTests(unittest.TestCase):
    def setUp(self):
        self.db = seeded_db()
        self.db.execute("""INSERT INTO event_claims(claim_id,event_id,ordinal,text,issue_code,
            claim_type,source_start,source_end,cited_source,confidence)
            VALUES('c1','e0',1,'罚款 200 万元','DISCLOSURE','fact',10,20,'第一节',0.9)""")
        self.db.commit()

    def test_claims_and_extraction_provenance_are_untouched(self):
        before = [tuple(row) for row in self.db.execute(
            "SELECT claim_id,text,source_start,source_end FROM event_claims ORDER BY claim_id")]
        provenance = self.db.execute("""SELECT prompt_version, raw_label_json
                                        FROM event_records WHERE event_id='e0'""").fetchone()
        store(self.db, "e0", "cxmt", PAYLOAD, "model", "qwen-plus")
        after = [tuple(row) for row in self.db.execute(
            "SELECT claim_id,text,source_start,source_end FROM event_claims ORDER BY claim_id")]
        self.assertEqual(after, before)
        self.assertEqual(tuple(self.db.execute("""SELECT prompt_version, raw_label_json
                                                  FROM event_records WHERE event_id='e0'""").fetchone()),
                         tuple(provenance))


if __name__ == "__main__":
    unittest.main()
