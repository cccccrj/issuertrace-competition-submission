import sqlite3
import unittest

from processor.claim_index import ensure_schema as ensure_index_schema
from processor.inquiry_align import (align_question, alignable, discriminating,
                                     drop_stale_triplets, ensure_schema, parse_questions,
                                     question_for_paragraph, round_of, store, vocabulary)


def rows(*texts, start=1, page=1):
    """Paragraphs as the index stores them: ordered, numbered, with a page."""
    return [{"paragraph_id": start + index, "page": page + index, "section": "(前言)", "text": text}
            for index, text in enumerate(texts)]


class QuestionParsingTests(unittest.TestCase):
    document = {"version_id": "v1", "company_id": "cxmt", "title": "首轮问询回复"}

    def parse(self, *texts):
        return parse_questions(rows(*texts), self.document)

    def test_a_numbered_heading_starts_a_question(self):
        questions = self.parse("1．关于控制权根据申请材料：（1）公司无控股股东。",
                               "（一）清辉集电的股权架构与表决安排。")
        self.assertEqual((questions[0]["number"], questions[0]["topic"]), (1, "控制权"))

    def test_a_question_owns_everything_up_to_the_next_one(self):
        questions = self.parse("1．关于控制权根据申请材料：无控股股东。",
                               "（一）表决安排的实际履行情况。",
                               "2．关于募投项目根据申请材料：拟募集资金295亿元。",
                               "（一）募投项目的测算依据。")
        self.assertEqual((questions[0]["first_paragraph_id"], questions[0]["last_paragraph_id"]), (1, 2))
        self.assertEqual((questions[1]["first_paragraph_id"], questions[1]["last_paragraph_id"]), (3, 4))

    def test_the_reply_is_located_apart_from_the_question(self):
        # The citation has to point at the answer, not at the question restated.
        questions = self.parse("1．关于控制权根据申请材料：无控股股东。",
                               "请保荐机构发表明确意见。",
                               "（一）表决安排的实际履行情况。")
        self.assertEqual(questions[0]["answer_first_paragraph_id"], 3)
        self.assertNotIn("（一）", questions[0]["question_text"])

    def test_a_heading_the_extractor_lost_leaves_no_wrong_number(self):
        # Questions 4 and 6 of the real first round are absent from the text
        # layer.  The numbers that survive must stay attached to their own text
        # rather than renumbering the ones after the gap.
        questions = self.parse("3．关于尚未盈利根据申请材料：报告期亏损。",
                               "（一）亏损的原因。",
                               "5．关于客户和收入根据申请材料：前五大客户集中。",
                               "（一）客户集中度。")
        self.assertEqual([question["number"] for question in questions], [3, 5])

    def test_a_second_round_heading_written_with_a_space_is_read(self):
        questions = self.parse("1. 关于控制权和股权根据首轮问询回复：（1）清辉集电。",
                               "（一）合伙协议的约定。")
        self.assertEqual(questions[0]["topic"], "控制权和股权")

    def test_a_paragraph_that_is_not_a_heading_starts_nothing(self):
        self.assertEqual(self.parse("公司2025年营业收入为1,000万元，同比增长13．5％。"), [])

    def test_the_round_is_read_from_the_document_title(self):
        self.assertEqual(round_of("首轮问询回复"), "first")
        self.assertEqual(round_of("第二轮问询回复"), "second")
        self.assertEqual(round_of("审核中心意见落实函回复"), "implementation")
        self.assertEqual(round_of("招股说明书"), "other")


class AlignmentTests(unittest.TestCase):
    def _db(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        ensure_index_schema(db)
        ensure_schema(db)
        db.execute("""CREATE TABLE disclosure_diffs(diff_id TEXT PRIMARY KEY, company_id TEXT,
            pair_key TEXT, section TEXT, diff_kind TEXT, left_text TEXT, right_text TEXT,
            left_page INTEGER, right_page INTEGER, metric TEXT, old_value TEXT, new_value TEXT)""")
        return db

    def _diff(self, db, diff_id, section, text, kind="modified"):
        db.execute("""INSERT INTO disclosure_diffs
            (diff_id,company_id,pair_key,section,diff_kind,left_text,right_text,left_page,right_page)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (diff_id, "cxmt", "申报稿→上会稿", section, kind, "", text, 10, 11))
        return db.execute("SELECT * FROM disclosure_diffs WHERE diff_id=?", (diff_id,)).fetchone()

    def _question(self, text="1．关于研发支出资本化根据申请材料：资本化金额为170,860.40万元。",
                  answer="（一）研发支出资本化的会计政策与资本化金额构成。"):
        return parse_questions(rows(text, answer),
                               {"version_id": "v1", "company_id": "cxmt", "title": "首轮问询回复"})[0]

    def test_a_change_about_the_same_subject_is_paired_with_the_question(self):
        db = self._db()
        diff = self._diff(db, "d1", "第六节 财务会计信息",
                          "报告期各期，公司满足资本化条件的研发支出分别为170,860.40万元，资本化率逐年下降。")
        vocab = vocabulary(db, "cxmt")
        triplets = align_question(self._question(), [(diff, diff["right_text"])], vocab)
        self.assertEqual(len(triplets), 1)
        self.assertEqual(triplets[0]["diff_id"], "d1")

    def test_a_change_about_something_else_is_not_paired(self):
        db = self._db()
        diff = self._diff(db, "d2", "第八节 公司治理",
                          "公司董事会由九名董事组成，其中独立董事三名，任期三年。")
        vocab = vocabulary(db, "cxmt")
        self.assertEqual(align_question(self._question(), [(diff, diff["right_text"])], vocab), [])

    def test_the_reply_paragraph_is_cited_not_the_question(self):
        db = self._db()
        diff = self._diff(db, "d1", "第六节 财务会计信息",
                          "报告期各期，公司满足资本化条件的研发支出分别为170,860.40万元。")
        vocab = vocabulary(db, "cxmt")
        triplet = align_question(self._question(), [(diff, diff["right_text"])], vocab)[0]
        self.assertEqual(triplet["response_paragraph_id"], 2)
        self.assertEqual(triplet["response_page"], 2)

    def test_one_passage_changed_in_two_drafts_is_listed_once(self):
        db = self._db()
        text = "报告期各期，公司满足资本化条件的研发支出分别为170,860.40万元，资本化率逐年下降。"
        pairs = [(self._diff(db, "d1", "第六节 财务会计信息", text), text),
                 (self._diff(db, "d2", "第六节 财务会计信息", text), text)]
        triplets = align_question(self._question(), pairs, vocabulary(db, "cxmt"))
        self.assertEqual(len(triplets), 1)

    def test_the_glossary_is_never_aligned(self):
        # It restates every defined term, so it matches every question.
        db = self._db()
        self.assertFalse(alignable(self._diff(db, "d3", "第一节 释义", "一、一般释义")))
        self.assertTrue(alignable(self._diff(db, "d4", "第五节 业务与技术", "产品组合")))

    def test_a_row_number_is_not_evidence_of_a_match(self):
        # Table extraction leaves "4" and "11" everywhere; two passages sharing
        # them share nothing, and printing that as the reason looks arbitrary.
        db = self._db()
        vocab = vocabulary(db, "cxmt")
        terms = discriminating("第4项，第11条，募资295亿元，研发投入", vocab)
        self.assertNotIn("4", terms)
        self.assertNotIn("11", terms)
        self.assertIn("295", terms)

    def test_boilerplate_terms_are_not_evidence_of_a_match(self):
        db = self._db()
        for index in range(30):
            db.execute("""INSERT INTO evidence_paragraphs
                (company_id,version_id,document_type,title,section,page,text)
                VALUES('cxmt','v9','prospectus','招股说明书','第三节',?,?)""",
                (index, "公司公司"))
        db.execute("INSERT INTO evidence_terms(company_id,token,documents) VALUES('cxmt','公司',30)")
        vocab = vocabulary(db, "cxmt")
        self.assertNotIn("公司", discriminating("公司研发", vocab))
        self.assertIn("研发", discriminating("公司研发", vocab))


class QuestionLookupTests(unittest.TestCase):
    def test_a_cited_paragraph_names_the_question_it_answers(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        ensure_schema(db)
        questions = parse_questions(rows("1．关于控制权根据申请材料：无控股股东。",
                                         "（一）表决安排。",
                                         "2．关于募投项目根据申请材料：拟募集295亿元。",
                                         "（一）募投测算。"),
                                    {"version_id": "v1", "company_id": "cxmt", "title": "首轮问询回复"})
        store(db, questions, [])
        self.assertEqual(question_for_paragraph(db, "v1", 2)["number"], 1)
        self.assertEqual(question_for_paragraph(db, "v1", 4)["number"], 2)
        self.assertIsNone(question_for_paragraph(db, "v1", 99))
        self.assertIsNone(question_for_paragraph(db, "other", 2))


if __name__ == "__main__":
    unittest.main()


class StaleTripletTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        ensure_schema(self.db)

    def question(self, question_id, version_id):
        return {"question_id": question_id, "company_id": "cxmt", "version_id": version_id,
                "round": "rfirst", "document_title": "首轮问询回复", "number": 1,
                "topic": "控制权", "question_text": "问题", "question_page": 4,
                "first_paragraph_id": 1, "last_paragraph_id": 9,
                "answer_first_paragraph_id": 5, "answer_page": 5, "aligned_at": "2026-09-01"}

    def triplet(self, triplet_id, question_id, diff_id):
        return {"triplet_id": triplet_id, "question_id": question_id, "company_id": "cxmt",
                "response_paragraph_id": 5, "response_page": 5, "response_excerpt": "回复",
                "diff_id": diff_id, "diff_section": "第二节 概览", "diff_kind": "added",
                "shared_terms": "[]", "match_score": 0.1, "created_at": "2026-09-01"}

    def test_a_rebuild_does_not_leave_triplets_citing_deleted_diffs(self):
        # A diff rebuild changes every diff_id, so an upsert alone would leave the
        # previous rows pointing at rows that no longer exist.
        store(self.db, [self.question("q1", "v1")], [self.triplet("t-old", "q1", "diff-old")])
        removed = drop_stale_triplets(self.db, "v1")
        store(self.db, [self.question("q1", "v1")], [self.triplet("t-new", "q1", "diff-new")])
        rows = self.db.execute("SELECT triplet_id, diff_id FROM inquiry_triplets").fetchall()
        self.assertEqual(removed, 1)
        self.assertEqual([tuple(row) for row in rows], [("t-new", "diff-new")])

    def test_another_document_keeps_its_triplets(self):
        store(self.db, [self.question("q1", "v1")], [self.triplet("t1", "q1", "d1")])
        store(self.db, [self.question("q2", "v2")], [self.triplet("t2", "q2", "d2")])
        drop_stale_triplets(self.db, "v1")
        remaining = self.db.execute("SELECT triplet_id FROM inquiry_triplets").fetchall()
        self.assertEqual([row["triplet_id"] for row in remaining], ["t2"])
