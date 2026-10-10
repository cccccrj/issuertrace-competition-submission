import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from processor.claim_index import bigrams, ensure_schema as ensure_index_schema
from processor.claim_verdict import (
    PARTIAL_SUBTYPES, annotate_inquiries, backfill_numeric, canonical_figures, claim_context, ensure_schema,
    group_by_text, make_prompt, pending_claims, rule_verdict, store_verdict, verdict_schema,
)
from processor.inquiry_align import ensure_schema as ensure_inquiry_schema, parse_questions, store


class FigureNormalisationTests(unittest.TestCase):
    def test_unit_shorthand_matches_its_full_form(self):
        # 「295亿」 in a headline and 「295亿元」 in a filing are the same figure.
        self.assertEqual(canonical_figures("募资295亿"), canonical_figures("募集资金295亿元"))

    def test_trailing_zeros_do_not_split_a_figure(self):
        self.assertEqual(canonical_figures("295.00亿元"), canonical_figures("295亿元"))

    def test_percentages_are_captured(self):
        self.assertEqual(canonical_figures("毛利率为20.5%"), {("20.5", "%")})

    def test_text_without_figures_yields_nothing(self):
        self.assertEqual(canonical_figures("公司完成上市"), set())


class RuleVerdictTests(unittest.TestCase):
    def _candidate(self, text, page=164):
        return {"version_id": "v1", "title": "首轮问询回复", "section": "(前言)",
                "page": page, "text": text, "score": -40.0}

    def test_filing_restating_every_figure_supports_the_claim(self):
        result = rule_verdict("本次拟募集资金295亿元",
                              self._candidate("投资总额为345亿元，本次拟募集资金金额为295亿元。"))
        self.assertEqual(result["verdict"], "SUPPORTED")
        self.assertEqual(result["method"], "rule_numeric")

    def test_a_figure_the_filing_omits_falls_through_to_the_model(self):
        # Silence is not contradiction; the rule path must not decide this.
        self.assertIsNone(rule_verdict("本次拟募集资金500亿元",
                                       self._candidate("本次拟募集资金金额为295亿元。")))

    def test_claim_without_figures_is_not_handled_by_the_rule_path(self):
        self.assertIsNone(rule_verdict("公司技术水平国内领先",
                                       self._candidate("本次拟募集资金金额为295亿元。")))

    def test_all_claim_figures_must_appear_not_just_one(self):
        self.assertIsNone(rule_verdict("投资总额345亿元，募资500亿元",
                                       self._candidate("投资总额为345亿元，募集资金295亿元。")))


class DeduplicationTests(unittest.TestCase):
    def _row(self, claim_id, text, company="cxmt", issue="FINANCIAL"):
        return {"claim_id": claim_id, "text": text, "company_id": company, "issue_code": issue}

    def test_the_same_sentence_under_two_issues_is_one_adjudication(self):
        rows = [self._row("c1", "长鑫募资295亿元", issue="FINANCIAL"),
                self._row("c2", "长鑫募资295亿元", issue="FUND_RAISE")]
        grouped = group_by_text(rows)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(len(next(iter(grouped.values()))), 2)

    def test_whitespace_differences_do_not_create_a_second_group(self):
        grouped = group_by_text([self._row("c1", "长鑫 募资295亿元"), self._row("c2", "长鑫募资295亿元")])
        self.assertEqual(len(grouped), 1)

    def test_identical_text_from_two_companies_stays_separate(self):
        grouped = group_by_text([self._row("c1", "募资295亿元", company="cxmt"),
                                 self._row("c2", "募资295亿元", company="wuxi_apptec")])
        self.assertEqual(len(grouped), 2)


class StoreVerdictTests(unittest.TestCase):
    def _db(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        ensure_schema(db)
        return db

    def _candidate(self):
        return {"version_id": "v1", "title": "注册稿_招股说明书", "section": "第三节 风险因素",
                "page": 140, "text": "公司存在出口管制风险。", "score": -30.0}

    def test_a_verdict_is_written_for_every_duplicate_claim(self):
        db = self._db()
        store_verdict(db, ["c1", "c2"], "cxmt",
                      {"verdict": "SUPPORTED", "partial_subtype": None, "method": "rule_numeric",
                       "confidence": 0.9, "reason": "数字一致", "candidate": self._candidate()}, None)
        self.assertEqual(db.execute("SELECT count(*) FROM claim_verdicts").fetchone()[0], 2)

    def test_the_citation_anchors_are_persisted(self):
        db = self._db()
        store_verdict(db, ["c1"], "cxmt",
                      {"verdict": "CONFLICT", "partial_subtype": None, "method": "llm_adjudicated",
                       "confidence": 0.7, "reason": "与披露矛盾", "candidate": self._candidate()}, "qwen-plus")
        row = db.execute("SELECT * FROM claim_verdicts WHERE claim_id='c1'").fetchone()
        self.assertEqual(row["evidence_page"], 140)
        self.assertEqual(row["evidence_version_id"], "v1")
        self.assertEqual(row["model_name"], "qwen-plus")

    def test_a_supported_verdict_without_a_page_is_rejected(self):
        db = self._db()
        with self.assertRaises(ValueError):
            store_verdict(db, ["c1"], "cxmt",
                          {"verdict": "SUPPORTED", "partial_subtype": None, "method": "llm_adjudicated",
                           "confidence": 0.9, "reason": "无证据", "candidate": None}, "qwen-plus")
        self.assertEqual(db.execute("SELECT count(*) FROM claim_verdicts").fetchone()[0], 0)

    def test_unverifiable_may_be_stored_without_evidence(self):
        db = self._db()
        store_verdict(db, ["c1"], "wuxi_apptec",
                      {"verdict": "UNVERIFIABLE", "partial_subtype": None, "method": "no_corpus",
                       "confidence": 1.0, "reason": "尚未采集权威披露语料", "candidate": None}, None)
        row = db.execute("SELECT * FROM claim_verdicts WHERE claim_id='c1'").fetchone()
        self.assertEqual(row["method"], "no_corpus")
        self.assertIsNone(row["evidence_page"])

    def test_re_adjudicating_replaces_rather_than_duplicates(self):
        db = self._db()
        for verdict in ("UNVERIFIABLE", "SUPPORTED"):
            store_verdict(db, ["c1"], "cxmt",
                          {"verdict": verdict, "partial_subtype": None, "method": "llm_adjudicated",
                           "confidence": 0.8, "reason": "r", "candidate": self._candidate()}, "qwen-plus")
        rows = db.execute("SELECT verdict FROM claim_verdicts").fetchall()
        self.assertEqual([row["verdict"] for row in rows], ["SUPPORTED"])


class PendingClaimTests(unittest.TestCase):
    """Which claims a run still owes an answer for."""

    def _db(self, *, indexed: bool):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        ensure_schema(db)
        ensure_index_schema(db)
        db.execute("""CREATE TABLE event_records(event_id TEXT, company_id TEXT, summary TEXT,
                      event_subjects_json TEXT)""")
        db.execute("""CREATE TABLE event_claims(claim_id TEXT, event_id TEXT, text TEXT,
                      issue_code TEXT)""")
        db.execute("INSERT INTO event_records VALUES('e1','wuxi_apptec','摘要','[]')")
        for claim in ("settled", "stale"):
            db.execute("INSERT INTO event_claims VALUES(?,'e1',?,'FINANCIAL')", (claim, claim))
        store_verdict(db, ["settled"], "wuxi_apptec",
                      {"verdict": "SUPPORTED", "partial_subtype": None, "method": "llm_adjudicated",
                       "confidence": 0.9, "reason": "有据", "candidate":
                       {"version_id": "v1", "title": "公告", "section": "正文", "page": 2,
                        "text": "原文", "score": -10.0}}, "qwen-plus")
        store_verdict(db, ["stale"], "wuxi_apptec",
                      {"verdict": "UNVERIFIABLE", "partial_subtype": None, "method": "no_corpus",
                       "confidence": 1.0, "reason": "尚未采集权威披露语料", "candidate": None}, None)
        if indexed:
            db.execute("""INSERT INTO evidence_paragraphs VALUES(1,'wuxi_apptec','v1',
                'announcements','公告','正文',2,'原文')""")
        return db

    def test_a_no_corpus_verdict_is_pending_again_once_the_filings_are_indexed(self):
        # It recorded that there was nothing to check against, not a judgement.
        pending = pending_claims(self._db(indexed=True), {"wuxi_apptec"}, False)
        self.assertEqual([row["claim_id"] for row in pending], ["stale"])

    def test_a_no_corpus_verdict_stays_settled_while_there_is_still_no_corpus(self):
        pending = pending_claims(self._db(indexed=False), {"wuxi_apptec"}, False)
        self.assertEqual(pending, [])

    def test_force_still_takes_everything(self):
        pending = pending_claims(self._db(indexed=True), {"wuxi_apptec"}, True)
        self.assertEqual({row["claim_id"] for row in pending}, {"settled", "stale"})


class PromptTests(unittest.TestCase):
    def test_candidates_are_numbered_and_carry_their_page(self):
        prompt = make_prompt("长鑫募资295亿元", "FUND_RAISE", [
            {"title": "首轮问询回复", "page": 164, "section": "(前言)", "text": "募集资金295亿元。"},
            {"title": "注册稿_招股说明书", "page": 32, "section": "第二节 概览", "text": "募集资金运用。"}])
        self.assertIn("[1] 《首轮问询回复》第164页", prompt)
        self.assertIn("[2] 《注册稿_招股说明书》第32页", prompt)

    def test_schema_bounds_evidence_index_to_the_candidates_offered(self):
        properties = verdict_schema()["properties"]
        self.assertEqual(properties["evidence_index"]["minimum"], 0)
        self.assertEqual(properties["verdict"]["enum"],
                         ["SUPPORTED", "PARTIAL", "CONFLICT", "UNVERIFIABLE"])

    def test_schema_has_no_nested_objects_the_gateway_would_reject(self):
        for definition in verdict_schema()["properties"].values():
            self.assertIn(definition["type"], {"string", "integer", "number"})

    def test_article_context_is_carried_into_the_prompt(self):
        # A bare 「公司」 in a claim means the article's subject, not the filing's
        # issuer; without this context the adjudicator confuses the two.
        prompt = make_prompt("公司未持有长鑫科技股份", "RELATED_PARTY",
                             [{"title": "注册稿_招股说明书", "page": 82, "section": "第五节", "text": "股权结构。"}],
                             {"summary": "某上市公司回复投资者提问", "subjects": ["兆易创新", "长鑫科技"]})
        self.assertIn("某上市公司回复投资者提问", prompt)
        self.assertIn("兆易创新、长鑫科技", prompt)

    def test_prompt_without_context_still_renders(self):
        prompt = make_prompt("长鑫募资295亿元", None,
                             [{"title": "首轮问询回复", "page": 164, "section": "(前言)", "text": "募资295亿元。"}])
        self.assertIn("【议题】未分类", prompt)
        self.assertNotIn("主张出处", prompt)


class DegradationTests(unittest.TestCase):
    """With no reachable model, a run does what rules can and says the rest is
    still pending — it never writes a conclusion no model produced."""

    def test_offline_mode_needs_no_provider_and_reports_why(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = self._run(directory, offline=True)
        self.assertTrue(summary["degraded"])
        self.assertEqual(summary["degraded_reason"], "offline mode requested")
        self.assertEqual(summary["provider"], "degraded")
        self.assertIsNone(summary["model"])

    def test_claims_that_need_the_model_are_left_pending_not_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = self._run(directory, offline=True)
            self.assertEqual(summary["left_pending_for_model"], 1)
            db = sqlite3.connect(Path(directory) / "collector.sqlite3")
            self.assertEqual(db.execute("SELECT count(*) FROM claim_verdicts").fetchone()[0], 0)

    def _run(self, directory, **kwargs):
        """One claim with no numeric rule to settle it, in a throwaway database."""
        import collector.collect as collect
        import processor.claim_verdict as verdict
        path = Path(directory) / "collector.sqlite3"
        original = collect.STATE_DB
        collect.STATE_DB = path
        try:
            db = collect.connect()
            db.row_factory = sqlite3.Row
            verdict.ensure_schema(db)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS event_records(event_id TEXT PRIMARY KEY, company_id TEXT,
                    summary TEXT, event_subjects_json TEXT);
                CREATE TABLE IF NOT EXISTS event_claims(claim_id TEXT PRIMARY KEY, event_id TEXT,
                    text TEXT, issue_code TEXT);""")
            ensure_index_schema(db)
            db.execute("""INSERT INTO evidence_paragraphs
                (company_id,version_id,document_type,title,section,page,text)
                VALUES('cxmt','v1','prospectus','招股说明书','第七节',10,'公司持续投入研发。')""")
            db.execute("INSERT INTO evidence_fts(rowid,body) VALUES(1,?)",
                       (" ".join(bigrams("公司持续投入研发。")),))
            db.execute("INSERT INTO event_records VALUES('e1','cxmt','摘要','[]')")
            db.execute("INSERT INTO event_claims VALUES('c1','e1','公司研发投入持续增加','FINANCIAL')")
            db.commit()
            db.close()
            return verdict.run({"cxmt"}, limit=5, **kwargs)
        finally:
            collect.STATE_DB = original


class InquiryRoutingTests(unittest.TestCase):
    """A claim about something already asked, answered and changed."""

    def _db(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        ensure_schema(db)
        ensure_inquiry_schema(db)
        questions = parse_questions(
            [{"paragraph_id": 10, "page": 164, "section": "(前言)",
              "text": "13．关于募投项目根据申请材料：本次拟募集资金295亿元。"},
             {"paragraph_id": 11, "page": 165, "section": "(前言)",
              "text": "（一）募投项目的测算依据与资金缺口安排。"}],
            {"version_id": "v1", "company_id": "cxmt", "title": "首轮问询回复"})
        store(db, questions, [])
        return db

    def _candidates(self):
        return [{"version_id": "v1", "paragraph_id": 11, "title": "首轮问询回复",
                 "section": "(前言)", "page": 165, "text": "募投项目测算依据如下。", "score": -30.0},
                {"version_id": "v2", "paragraph_id": 88, "title": "注册稿_招股说明书",
                 "section": "第七节", "page": 300, "text": "募集资金运用。", "score": -20.0}]

    def test_a_reply_paragraph_is_marked_with_the_question_it_answers(self):
        candidates = annotate_inquiries(self._db(), self._candidates())
        self.assertEqual(candidates[0]["inquiry"]["number"], 13)
        self.assertEqual(candidates[0]["inquiry"]["topic"], "募投项目")
        self.assertNotIn("inquiry", candidates[1])

    def test_the_prompt_tells_the_model_the_paragraph_is_an_answer(self):
        candidates = annotate_inquiries(self._db(), self._candidates())
        prompt = make_prompt("长鑫募投项目资金缺口巨大", "FINANCE", candidates)
        self.assertIn("首轮问询第13问", prompt)
        self.assertIn("关于募投项目", prompt)

    def test_a_verdict_on_a_reply_records_which_question_settles_it(self):
        db = self._db()
        candidate = annotate_inquiries(db, self._candidates())[0]
        store_verdict(db, ["c1"], "cxmt",
                      {"verdict": "PARTIAL", "partial_subtype": "already_explained",
                       "method": "llm_adjudicated", "confidence": 0.8,
                       "reason": "首轮第13问已就募投测算作出说明", "candidate": candidate}, "qwen-plus")
        row = db.execute("SELECT * FROM claim_verdicts WHERE claim_id='c1'").fetchone()
        self.assertEqual(row["partial_subtype"], "already_explained")
        self.assertIsNotNone(row["inquiry_question_id"])

    def test_a_verdict_off_an_ordinary_paragraph_records_no_question(self):
        db = self._db()
        candidate = annotate_inquiries(db, self._candidates())[1]
        store_verdict(db, ["c2"], "cxmt",
                      {"verdict": "SUPPORTED", "partial_subtype": None, "method": "rule_numeric",
                       "confidence": 0.9, "reason": "数字一致", "candidate": candidate}, None)
        self.assertIsNone(db.execute(
            "SELECT inquiry_question_id FROM claim_verdicts WHERE claim_id='c2'").fetchone()[0])

    def test_candidates_survive_a_database_without_the_alignment(self):
        # The alignment is a separate pass; adjudication must not require it.
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        ensure_schema(db)
        self.assertEqual(annotate_inquiries(db, self._candidates()), self._candidates())

    def test_the_two_new_subtypes_are_offered_to_the_model(self):
        self.assertIn("already_explained", PARTIAL_SUBTYPES)
        self.assertIn("causality_added", PARTIAL_SUBTYPES)
        self.assertIn("already_explained", verdict_schema()["properties"]["partial_subtype"]["enum"])


class NumericCheckTests(unittest.TestCase):
    """I-02: conversions are computed, handed to the model, and checked against its verdict."""

    CLAIM = "长鑫科技发行后总股本不超708.15亿股"
    FILING = "发行后总股本不超过7,081,505.7468 万股（行使超额配售选择权之前），本次发行后为6,688,088.6077万股"

    def _candidate(self, text=FILING):
        return {"version_id": "v1", "title": "招股说明书", "section": "本次发行概况",
                "page": 2, "text": text, "score": -30.0}

    def _db(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        ensure_schema(db)
        return db

    def _store(self, db, verdict, subtype=None, claim_text=CLAIM):
        store_verdict(db, ["c1"], "cxmt",
                      {"verdict": verdict, "partial_subtype": subtype, "method": "llm_adjudicated",
                       "confidence": 1.0, "reason": "…", "candidate": self._candidate()},
                      "qwen-plus", claim_text)
        raw = db.execute("SELECT numeric_check FROM claim_verdicts WHERE claim_id='c1'").fetchone()[0]
        return json.loads(raw) if raw else None

    def test_ten_thousand_and_hundred_million_are_one_figure_on_the_rule_path(self):
        self.assertEqual(canonical_figures("募资2,950,000万元"), canonical_figures("募资295亿元"))

    def test_share_claims_are_left_to_the_model(self):
        # The filing repeating a share count does not make it the same count.
        self.assertIsNone(rule_verdict(self.CLAIM, self._candidate()))

    def test_the_prompt_carries_the_computed_conversion(self):
        prompt = make_prompt(self.CLAIM, "FUND_RAISE", [self._candidate()])
        self.assertIn("〔系统换算〕", prompt)
        self.assertIn("70,815,000,000股", prompt)
        self.assertIn("差 57,468股", prompt)
        self.assertNotIn("7,081,500,000", prompt)

    def test_a_magnitude_verdict_on_agreeing_figures_is_marked_disputed(self):
        check = self._store(self._db(), "PARTIAL", "magnitude_off")
        self.assertEqual(check["status"], "agree")
        self.assertTrue(check["model_disputed"])
        self.assertEqual(check["dispute"], "agree_vs_magnitude")

    def test_a_conflict_is_not_disputed_by_agreeing_figures(self):
        # 已转换 against 未偿还: the number is right and the sentence is wrong.
        self.assertFalse(self._store(self._db(), "CONFLICT")["model_disputed"])

    def test_plain_yuan_figures_are_left_to_the_model(self):
        # Once decided by the rule path, this wording conflict came out SUPPORTED.
        self.assertIsNone(rule_verdict("於本公告日期已轉換債券的本金總額：人民幣1,807,000,000元",
                                       self._candidate("人民币1,807,000,000元於本公告日期未償還債券的本金總額")))

    def test_other_partial_subtypes_are_not_disputed_by_figures(self):
        # A single day quoted as a whole programme has the right number and the
        # wrong meaning; agreeing figures say nothing against it.
        self.assertFalse(self._store(self._db(), "PARTIAL", "overgeneralized")["model_disputed"])

    def test_a_supported_verdict_on_a_unit_slip_is_marked_disputed(self):
        # 668,808.8608万股 against 6,688,088.6077万股 was once judged SUPPORTED.
        check = self._store(self._db(), "SUPPORTED", claim_text="发行后总股本为668,808.8608万股")
        self.assertEqual(check["status"], "slip")
        self.assertEqual(check["dispute"], "slip_vs_supported")

    def test_the_verdict_itself_is_never_rewritten(self):
        db = self._db()
        self._store(db, "PARTIAL", "magnitude_off")
        row = db.execute("SELECT verdict, partial_subtype FROM claim_verdicts").fetchone()
        self.assertEqual(tuple(row), ("PARTIAL", "magnitude_off"))

    def test_no_figures_no_check(self):
        self.assertIsNone(self._store(self._db(), "CONFLICT", claim_text="公司技术领先"))

    def test_backfill_fills_old_verdicts_from_their_stored_excerpt(self):
        db = self._db()
        db.execute("CREATE TABLE event_claims(claim_id TEXT, text TEXT)")
        db.execute("INSERT INTO event_claims VALUES('c1', ?)", (self.CLAIM,))
        store_verdict(db, ["c1"], "cxmt",
                      {"verdict": "PARTIAL", "partial_subtype": "magnitude_off", "method": "llm_adjudicated",
                       "confidence": 1.0, "reason": "…", "candidate": self._candidate()}, "qwen-plus")
        self.assertIsNone(db.execute("SELECT numeric_check FROM claim_verdicts").fetchone()[0])
        summary = backfill_numeric(db)
        self.assertEqual(summary["model_disputed"], 1)
        self.assertEqual(summary["numeric_check"], {"agree": 1})


class ClaimContextTests(unittest.TestCase):
    def test_subjects_are_parsed_from_json(self):
        context = claim_context({"summary": "摘要", "event_subjects_json": '["长鑫科技", "小米"]'})
        self.assertEqual(context["subjects"], ["长鑫科技", "小米"])

    def test_malformed_subject_json_does_not_break_adjudication(self):
        context = claim_context({"summary": "摘要", "event_subjects_json": "not json"})
        self.assertEqual(context["subjects"], [])

    def test_non_string_subjects_are_discarded(self):
        context = claim_context({"summary": None, "event_subjects_json": '["长鑫科技", 42, null]'})
        self.assertEqual(context["subjects"], ["长鑫科技"])


if __name__ == "__main__":
    unittest.main()
