import sqlite3
import unittest

from processor.claim_index import (CHAIN_DOCUMENT_TYPES, bigrams, ensure_schema, evidence_versions,
                                   indexable, retokenize_into, search)


class BigramTokenizerTests(unittest.TestCase):
    def test_chinese_runs_become_overlapping_bigrams(self):
        self.assertEqual(bigrams("募集资金"), ["募集", "集资", "资金"])

    def test_latin_and_digits_stay_whole_tokens(self):
        # Splitting LPDDR6 into bigrams would match unrelated part numbers.
        self.assertEqual(bigrams("LPDDR6 内存"), ["lpddr6", "内存"])

    def test_mixed_text_keeps_both_forms(self):
        self.assertEqual(bigrams("募资295亿元"), ["募资", "295", "亿元"])

    def test_single_character_survives_as_one_token(self):
        self.assertEqual(bigrams("A股"), ["a", "股"])

    def test_whitespace_inside_pdf_text_is_ignored(self):
        # PDF extraction sprinkles spaces inside numbers and between characters.
        self.assertEqual(bigrams("投资 总额"), bigrams("投资总额"))

    def test_english_words_stay_separate(self):
        # Deleting every space once glued a whole English clause into one token.
        self.assertEqual(bigrams("WuXi AppTec announced revenue of RMB 39.2 billion"),
                         ["wuxi", "apptec", "announced", "revenue", "of", "rmb", "39.2", "billion"])

    def test_digits_broken_by_extraction_are_still_glued(self):
        self.assertEqual(bigrams("投资总额 1 234 万元"), bigrams("投资总额1234万元"))

    def test_space_between_chinese_and_latin_is_not_a_token(self):
        self.assertEqual(bigrams("长鑫 DRAM 产品"), ["长鑫", "dram", "产品"])

    def test_punctuation_produces_no_tokens(self):
        self.assertEqual(bigrams("——，。"), [])

    def test_traditional_and_simplified_tokenize_alike(self):
        # Half the WuXi corpus is Hong Kong originals; without the fold, a
        # simplified claim shares no bigram with the filing that answers it.
        self.assertEqual(bigrams("藥明康德回購股份"), bigrams("药明康德回购股份"))


class SearchTests(unittest.TestCase):
    def _db(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        ensure_schema(db)
        return db

    def _add(self, db, company, text, page=1, title="注册稿_招股说明书", version="v1"):
        cursor = db.execute("""INSERT INTO evidence_paragraphs
            (company_id,version_id,document_type,title,section,page,text) VALUES(?,?,?,?,?,?,?)""",
            (company, version, "prospectus", title, "第三节 风险因素", page, text))
        db.execute("INSERT INTO evidence_fts(rowid,body) VALUES(?,?)",
                   (cursor.lastrowid, " ".join(bigrams(text))))
        return cursor.lastrowid

    def test_a_paraphrased_claim_still_finds_its_evidence(self):
        # The claim shares no long substring with the filing, only vocabulary.
        db = self._db()
        self._add(db, "cxmt", "本次募投项目投资总额为345亿元，拟募集资金金额为295亿元。", page=164)
        self._add(db, "cxmt", "公司主要产品为动态随机存取存储器芯片，广泛应用于服务器领域。", page=20)
        hits = search(db, "cxmt", "长鑫本次拟募集的资金总额是295亿元")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["page"], 164)

    def test_results_are_ordered_best_first(self):
        db = self._db()
        self._add(db, "cxmt", "公司毛利率报告期内存在一定波动，主要受存储芯片价格影响。", page=34)
        self._add(db, "cxmt", "公司员工人数报告期内持续增长。", page=99)
        hits = search(db, "cxmt", "公司毛利率报告期内出现下滑")
        self.assertEqual(hits[0]["page"], 34)
        self.assertLessEqual(hits[0]["score"], hits[-1]["score"])

    def test_another_company_evidence_is_never_returned(self):
        db = self._db()
        self._add(db, "wuxi_apptec", "公司本次募集资金总额为295亿元。", page=5)
        self.assertEqual(search(db, "cxmt", "本次募集资金总额为295亿元"), [])

    def test_a_claim_with_no_matching_evidence_returns_empty(self):
        db = self._db()
        self._add(db, "cxmt", "公司主要产品为动态随机存取存储器芯片。")
        self.assertEqual(search(db, "cxmt", "董事长辞职"), [])

    def test_punctuation_only_query_returns_empty_instead_of_raising(self):
        db = self._db()
        self._add(db, "cxmt", "公司主要产品为动态随机存取存储器芯片。")
        self.assertEqual(search(db, "cxmt", "——？"), [])

    def test_evidence_carries_the_anchors_a_citation_needs(self):
        db = self._db()
        self._add(db, "cxmt", "公司存在未能持续获得先进制程设备的风险。", page=140)
        hit = search(db, "cxmt", "公司面临先进制程设备获取风险")[0]
        self.assertEqual(hit["version_id"], "v1")
        self.assertEqual(hit["page"], 140)
        self.assertEqual(hit["section"], "第三节 风险因素")

    def test_a_simplified_claim_finds_a_traditional_filing(self):
        db = self._db()
        self._add(db, "wuxi_apptec", "本公司於二零二四年首次以集中競價交易方式回購A股股份合共20,275,407股。",
                  page=1, title="有關首次回購本公司A股及回購完成的公告")
        hits = search(db, "wuxi_apptec", "药明康德首次以集中竞价交易方式回购A股股份")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["page"], 1)

    def test_an_english_claim_finds_its_english_filing(self):
        db = self._db()
        self._add(db, "wuxi_apptec", "The Company repurchased a total of 20,275,407 A Shares "
                  "through centralized price bidding.", page=3, title="Announcement")
        self._add(db, "wuxi_apptec", "公司主要从事医药研发外包服务。", page=9)
        hits = search(db, "wuxi_apptec", "WuXi AppTec repurchased A Shares via centralized bidding")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["page"], 3)

    def test_retokenize_rewrites_the_index_and_keeps_paragraph_ids(self):
        db = self._db()
        pid = self._add(db, "wuxi_apptec", "Revenue grew strongly in the period.", page=2)
        # Simulate an index written by the old tokenizer: one glued token.
        db.execute("DELETE FROM evidence_fts WHERE rowid=?", (pid,))
        db.execute("INSERT INTO evidence_fts(rowid,body) VALUES(?,?)", (pid, "revenuegrewstronglyintheperiod"))
        self.assertEqual(search(db, "wuxi_apptec", "revenue grew"), [])
        summary = retokenize_into(db)
        self.assertEqual(summary["paragraphs"], 1)
        hits = search(db, "wuxi_apptec", "revenue grew")
        self.assertEqual([hit["paragraph_id"] for hit in hits], [pid])
        self.assertTrue(db.execute("SELECT documents FROM evidence_terms WHERE token='revenue'").fetchone())

    def test_the_stored_passage_keeps_the_filing_wording(self):
        # Only the search body is folded: a citation has to quote what the
        # document actually says, not a transliteration of it.
        db = self._db()
        self._add(db, "wuxi_apptec", "本公司回購股份數目為20,275,407股。", title="回購公告")
        hit = search(db, "wuxi_apptec", "回购股份数目")[0]
        self.assertIn("回購股份數目", hit["text"])


class EvidenceSelectionTests(unittest.TestCase):
    def _db(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE document_versions(version_id TEXT PRIMARY KEY, company_id TEXT,
                document_type TEXT, title TEXT, published_at TEXT);
            CREATE TABLE processed_documents(version_id TEXT PRIMARY KEY, text_path TEXT,
                page_count INTEGER, extraction_status TEXT);
        """)
        return db

    def _add(self, db, version, document_type, title, published_at):
        db.execute("INSERT INTO document_versions VALUES(?,?,?,?,?)",
                   (version, "wuxi_apptec", document_type, title, published_at))
        db.execute("INSERT INTO processed_documents VALUES(?,?,?,?)",
                   (version, f"data/{version}.txt", 10, "ok"))

    def test_a_chain_type_keeps_only_its_newest_version(self):
        db = self._db()
        self._add(db, "p1", "prospectus", "申报稿_招股说明书", "2025-12-30")
        self._add(db, "p2", "prospectus", "注册稿_招股说明书", "2026-05-27")
        selected = evidence_versions(db, "wuxi_apptec")
        self.assertEqual([row["version_id"] for _, row in selected], ["p2"])
        self.assertEqual(CHAIN_DOCUMENT_TYPES, ("prospectus",))

    def test_a_collection_type_keeps_every_document(self):
        # Each announcement states different facts on a different date, so
        # keeping only the newest would drop the page earlier claims need.
        db = self._db()
        self._add(db, "a1", "announcements", "关于以集中竞价交易方式回购公司股份的进展公告", "2024-04-02")
        self._add(db, "a2", "announcements", "2025年年度报告", "2026-03-24")
        selected = evidence_versions(db, "wuxi_apptec")
        self.assertEqual(sorted(row["version_id"] for _, row in selected), ["a1", "a2"])

    def test_forms_and_allocation_tables_are_left_out(self):
        db = self._db()
        self._add(db, "f1", "attachment", "於2026年臨時股東會代表委任表格", "2026-08-10")
        self._add(db, "f2", "attachment", "FORM OF PROXY FOR THE 2025 AGM", "2025-05-20")
        self._add(db, "k1", "attachment", "海外監管公告 - 出售部分股票資產", "2026-06-25")
        selected = evidence_versions(db, "wuxi_apptec")
        self.assertEqual([row["version_id"] for _, row in selected], ["k1"])
        self.assertFalse(indexable({"title": "代表委任表格"}))
        self.assertTrue(indexable({"title": "2025年年度报告"}))


if __name__ == "__main__":
    unittest.main()
