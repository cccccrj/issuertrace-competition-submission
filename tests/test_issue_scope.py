"""I-03: 药物研发 filed under a memory-chip company.

The industry each company is in comes from sources.yaml; these tests patch it so
they say what the rule does rather than what today's config happens to declare.
"""
import json
import sqlite3
import unittest
from unittest.mock import patch

from processor import issue_scope
from processor.pipeline import (
    ISSUE_RULES, RawDocument, code_strength, ensure_schema, load_config, out_of_scope_codes,
    rank_codes, scope_issue_codes, upsert_event,
)

INDUSTRIES = {"chipmaker": "semiconductor", "drugmaker": "pharma"}


def industry(company_id):
    return INDUSTRIES.get(company_id)


@patch("processor.pipeline.company_industry", side_effect=industry)
class ScopeRuleTests(unittest.TestCase):
    def test_drug_development_cannot_attach_to_a_chipmaker(self, _):
        self.assertEqual(out_of_scope_codes("chipmaker", ["FINANCIAL", "DRUG_DEVELOPMENT"]),
                         {"DRUG_DEVELOPMENT"})

    def test_a_drugmaker_keeps_it(self, _):
        self.assertEqual(out_of_scope_codes("drugmaker", ["DRUG_DEVELOPMENT"]), set())

    def test_a_company_without_a_declared_industry_is_not_filtered(self, _):
        # No evidence about the company, so nothing is removed on its behalf.
        self.assertEqual(out_of_scope_codes("undeclared", ["DRUG_DEVELOPMENT"]), set())

    def test_claims_lose_the_code_rather_than_gain_a_guess(self, _):
        claims = [{"text": "研发支出资本化", "issue_code": "DRUG_DEVELOPMENT"},
                  {"text": "DRAM 销售收入", "issue_code": "FINANCIAL"}]
        codes, scoped, dropped = scope_issue_codes("chipmaker", ["FINANCIAL", "DRUG_DEVELOPMENT"], claims)
        self.assertEqual(codes, ["FINANCIAL"])
        self.assertEqual([claim["issue_code"] for claim in scoped], [None, "FINANCIAL"])
        self.assertEqual(dropped, {"DRUG_DEVELOPMENT"})
        # The raw label is stored as the model gave it, so the input is untouched.
        self.assertEqual(claims[0]["issue_code"], "DRUG_DEVELOPMENT")


class ConfigTests(unittest.TestCase):
    def test_every_configured_company_declares_its_industry(self):
        # A company without one is silently exempt from the rule.
        entities = load_config()["entities"]
        for company_id in load_config()["scope"]["companies"]:
            self.assertTrue(entities[company_id].get("industry"), company_id)


class KeywordTests(unittest.TestCase):
    def test_micron_is_not_a_cro(self):
        blob = "美光（Micron）与长鑫存储的 DRAM 份额对比"
        self.assertEqual(code_strength("", blob, ISSUE_RULES["DRUG_DEVELOPMENT"]), 0)
        self.assertNotIn("DRUG_DEVELOPMENT", rank_codes("", blob, ISSUE_RULES))

    def test_macro_is_not_a_cro(self):
        self.assertEqual(code_strength("", "macro outlook", ("cro",)), 0)

    def test_a_standalone_cro_still_counts(self):
        self.assertEqual(code_strength("", "全球CRO龙头，CRO/CDMO 业务", ("cro",)), 2)

    def test_chinese_terms_keep_substring_matching(self):
        self.assertEqual(code_strength("", "临床试验进入临床三期", ("临床",)), 2)


@patch("processor.pipeline.company_industry", side_effect=industry)
class StorageTests(unittest.TestCase):
    def _db(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE document_versions(version_id TEXT PRIMARY KEY)")
        ensure_schema(db)
        db.execute("INSERT INTO document_versions(version_id) VALUES('v')")
        return db

    def _label(self):
        return {"eventhood": "EVENT", "event_types": [], "issue_codes": ["DRUG_DEVELOPMENT"],
                "production_mode": "BRIEF", "impact_direction": "neutral", "impact_magnitude": "minor",
                "article_sentiment": "neutral", "relevance_score": 0.5, "confidence": 0.9,
                "summary": "Counterpoint 预测 DRAM 出货量同比增 50%",
                "claims": [{"text": "DRAM 出货量同比增长50%", "issue_code": "DRUG_DEVELOPMENT",
                            "source_start": 0, "source_end": 10}]}

    def _doc(self, company):
        return RawDocument("v", "media", "S2", company, "t", "2026-01-01",
                           "https://example.test", "news", "h", "raw", "ok")

    def _stored(self, db):
        event = db.execute("SELECT issue_codes_json, review_reasons_json, raw_label_json FROM event_records").fetchone()
        claim = db.execute("SELECT issue_code FROM event_claims").fetchone()[0]
        return json.loads(event[0]), json.loads(event[1]), json.loads(event[2]), claim

    def test_new_labels_are_scoped_on_write(self, _):
        db = self._db()
        upsert_event(db, self._doc("chipmaker"), "r", self._label(), "ai")
        codes, reasons, raw, claim = self._stored(db)
        self.assertEqual(codes, [])
        self.assertIsNone(claim)
        self.assertIn("issue_out_of_scope", reasons)
        self.assertEqual(raw["issue_codes"], ["DRUG_DEVELOPMENT"])

    def test_a_drugmaker_event_is_stored_unchanged(self, _):
        db = self._db()
        upsert_event(db, self._doc("drugmaker"), "r", self._label(), "ai")
        codes, reasons, _, claim = self._stored(db)
        self.assertEqual((codes, claim, reasons), (["DRUG_DEVELOPMENT"], "DRUG_DEVELOPMENT", []))

    def test_backfill_repairs_stored_events_and_is_idempotent(self, _):
        db = self._db()
        # Stored as it was before the rule existed.
        upsert_event(db, self._doc("undeclared"), "r", self._label(), "ai")
        db.execute("UPDATE event_records SET company_id='chipmaker'")
        self.assertEqual(issue_scope.backfill(db, dry_run=True)["events"], 1)
        self.assertEqual(self._stored(db)[0], ["DRUG_DEVELOPMENT"])  # dry run wrote nothing
        result = issue_scope.backfill(db)
        self.assertEqual((result["events"], result["claims"]), (1, 1))
        codes, reasons, _, claim = self._stored(db)
        self.assertEqual((codes, claim), ([], None))
        self.assertIn("issue_out_of_scope", reasons)
        self.assertEqual(issue_scope.backfill(db)["events"], 0)


if __name__ == "__main__":
    unittest.main()
