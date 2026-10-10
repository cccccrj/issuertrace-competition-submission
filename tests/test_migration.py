import json
import sqlite3
import unittest
from pathlib import Path

import yaml

from processor.migration import UNMEASURABLE, comparison, profile

ROOT = Path(__file__).resolve().parents[1]

SCHEMA = """
CREATE TABLE document_versions(version_id TEXT PRIMARY KEY, canonical_url TEXT, language TEXT);
CREATE TABLE discovered_metadata(canonical_url TEXT, company_id TEXT);
CREATE TABLE evidence_paragraphs(paragraph_id INTEGER PRIMARY KEY, company_id TEXT,
    version_id TEXT, document_type TEXT, text TEXT);
CREATE TABLE event_records(event_id TEXT PRIMARY KEY, version_id TEXT, company_id TEXT,
    needs_review INTEGER, model_name TEXT);
CREATE TABLE processed_documents(version_id TEXT PRIMARY KEY, processor_version TEXT);
CREATE TABLE event_claims(claim_id TEXT PRIMARY KEY, event_id TEXT, issue_code TEXT);
CREATE TABLE event_elements(element_id TEXT PRIMARY KEY, event_id TEXT, element_type TEXT,
    status TEXT);
CREATE TABLE claim_verdicts(claim_id TEXT PRIMARY KEY, company_id TEXT, verdict TEXT,
    method TEXT, prompt_version TEXT, inquiry_question_id TEXT);
CREATE TABLE duplicate_clusters(cluster_id TEXT PRIMARY KEY, company_id TEXT);
CREATE TABLE propagation_edges(edge_id TEXT PRIMARY KEY, company_id TEXT, edge_kind TEXT);
CREATE TABLE issue_ledger(issue_id TEXT PRIMARY KEY, company_id TEXT);
CREATE TABLE disclosure_diffs(diff_id TEXT PRIMARY KEY, company_id TEXT);
CREATE TABLE alerts(alert_id TEXT PRIMARY KEY, company_id TEXT);
"""


class MigrationProfileTests(unittest.TestCase):
    def _db(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        for company, event, version, language in (("cxmt", "e1", "v1", "zh-CN"),
                                                  ("wuxi_apptec", "e2", "v2", "zh-HK")):
            db.execute("INSERT INTO document_versions VALUES(?,?,?)", (version, f"u/{version}", language))
            db.execute("INSERT INTO discovered_metadata VALUES(?,?)", (f"u/{version}", company))
            db.execute("INSERT INTO processed_documents VALUES(?,?)", (version, "event-processing-v2"))
            db.execute("INSERT INTO event_records VALUES(?,?,?,?,?)", (event, version, company, 0, "qwen"))
            db.execute("INSERT INTO evidence_paragraphs VALUES(NULL,?,?,?,?)",
                       (company, version, "prospectus", "文本"))
        db.execute("INSERT INTO event_claims VALUES('c1','e1','FUND_RAISE')")
        db.execute("INSERT INTO event_claims VALUES('c2','e2','FUND_RAISE')")
        db.execute("INSERT INTO event_claims VALUES('c3','e2','CYBERSECURITY')")
        db.execute("INSERT INTO event_elements VALUES('x1','e1','subject','normalized')")
        db.execute("INSERT INTO event_elements VALUES('x2','e1','subject','unresolved')")
        db.execute("INSERT INTO event_elements VALUES('x3','e2','subject','normalized')")
        db.execute("""INSERT INTO claim_verdicts VALUES('c1','cxmt','SUPPORTED','llm_adjudicated',
            'claim-verdict-v2','q1')""")
        db.execute("""INSERT INTO claim_verdicts VALUES('c2','wuxi_apptec','UNVERIFIABLE','no_corpus',
            'claim-verdict-v1',NULL)""")
        db.execute("""INSERT INTO claim_verdicts VALUES('c3','wuxi_apptec','CONFLICT','llm_adjudicated',
            'claim-verdict-v1',NULL)""")
        return db

    def test_a_no_corpus_verdict_is_kept_out_of_the_citation_rate(self):
        # It records that there was nothing to check against, so counting it
        # would make an uncollected corpus look like a failure to cite.
        db = self._db()
        target = profile(db, "wuxi_apptec")["verdicts"]
        self.assertEqual((target["attempted"], target["cited_rate"]), (1, 1.0))

    def test_subject_resolution_is_reported_as_coverage_not_accuracy(self):
        db = self._db()
        subjects = profile(db, "cxmt")["subjects"]
        self.assertEqual(subjects["resolution_coverage"], 0.5)
        self.assertNotIn("accuracy", json.dumps(subjects))

    def test_reuse_is_measured_over_the_second_company_s_own_labels(self):
        db = self._db()
        result = comparison(profile(db, "cxmt"), profile(db, "wuxi_apptec"))
        self.assertEqual(result["issue_labels_reused"], ["FUND_RAISE"])
        self.assertEqual(result["issue_labels_new_for_target"], ["CYBERSECURITY"])
        self.assertEqual(result["issue_reuse_rate"], 0.5)

    def test_the_same_processor_version_on_both_is_stated_as_a_fact(self):
        db = self._db()
        self.assertTrue(comparison(profile(db, "cxmt"), profile(db, "wuxi_apptec"))
                        ["same_processor_version"])

    def test_a_mixed_prompt_pool_is_reported_rather_than_averaged_over(self):
        # The fixture has one company on v2 and the other on v1, which is what a
        # partially re-adjudicated pool looks like.
        db = self._db()
        self.assertFalse(comparison(profile(db, "cxmt"), profile(db, "wuxi_apptec"))
                         ["same_verdict_prompt"])

    def test_a_capability_with_no_material_is_a_regime_difference(self):
        # Hong Kong filings have no review-letter replies, so the inquiry route
        # has nothing to work on — not a pipeline that got worse.
        db = self._db()
        result = comparison(profile(db, "cxmt"), profile(db, "wuxi_apptec"), "cxmt", "wuxi_apptec")
        self.assertEqual(result["inquiry_alignment_available"],
                         {"cxmt": False, "wuxi_apptec": False})

    def test_a_company_with_nothing_adjudicated_yields_no_rate(self):
        db = self._db()
        db.execute("DELETE FROM claim_verdicts")
        self.assertIsNone(profile(db, "cxmt")["verdicts"]["cited_rate"])

    def test_what_cannot_be_measured_is_named_rather_than_estimated(self):
        # The two accuracy figures E7 asks for need labelled samples.  Reporting
        # a coverage number under their names is the failure mode being avoided.
        metrics = {gap["metric"] for gap in UNMEASURABLE}
        self.assertIn("主体消歧准确率", metrics)
        self.assertIn("中英文/AH 文档配对准确率", metrics)
        self.assertTrue(all(gap["needs"] and gap["available"] for gap in UNMEASURABLE))


class ZeroCodeChangeTests(unittest.TestCase):
    """The migration claim, enforced instead of asserted."""

    def test_the_processing_layer_holds_no_company_specific_branch(self):
        companies = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text())["scope"]["companies"]
        offenders = []
        for path in sorted((ROOT / "processor").glob("*.py")):
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if not any(company in line for company in companies):
                    continue
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
        self.assertEqual(offenders, [],
                         "company identifiers belong in config/sources.yaml, not in processor/")

    def test_the_api_and_the_pages_hold_no_company_constant_either(self):
        """「加第三家公司要改什么」的答案必须是「只改配置」。

        清单第 4 项做公司档案页时扫出来三处：前端 `api.js` 一张写死的公司名表、后端 Word 报告
        里另一张、登录页封面上一行字。前两张让第三家公司在全站显示成内部标识，第三处让登录页
        永远只说两家。公司名现在只从配置来，经 `/api/health` 交给前端。

        扫标识，也扫配置里的显示名（登录页那一行就是显示名）。注释行不算：讲历史的句子不是
        常量，文档串同理；这一点和上面那条对 processor/ 的扫描不同——那边连注释都不许出现。
        """
        from processor.pipeline import company_display_name
        config = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text())
        companies = config["scope"]["companies"]
        needles = set(companies) | {company_display_name(c) for c in companies}
        comment = ("#", "//", "*", "/*", "<!--")
        paths = [*sorted((ROOT / "api").rglob("*.py")),
                 *[p for p in sorted((ROOT / "web" / "src").rglob("*"))
                   if p.suffix in {".js", ".vue"} and "__tests__" not in p.parts]]
        def docstring_lines(path):
            if path.suffix != ".py":
                return set()
            import ast
            lines = set()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and node.body and isinstance(node.body[0], ast.Expr) \
                        and isinstance(getattr(node.body[0], "value", None), ast.Constant) \
                        and isinstance(node.body[0].value.value, str):
                    lines.update(range(node.body[0].lineno, node.body[0].end_lineno + 1))
            return lines

        offenders = []
        for path in paths:
            skip = docstring_lines(path)
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if number in skip or line.strip().startswith(comment):
                    continue
                if any(needle in line for needle in needles):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:80]}")
        self.assertEqual(offenders, [], "公司是谁、叫什么只从 config/sources.yaml 来")


if __name__ == "__main__":
    unittest.main()
