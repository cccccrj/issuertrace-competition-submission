import json
import sqlite3
import unittest

from processor import gold_examples
from processor.gold_examples import (
    QUERY_CHARS, TRAIN_SPLIT, ensure_schema, examples_for, indexed_splits,
    query_text, rebuild, render, searchable,
)


class Fixture(unittest.TestCase):
    """A two-split gold in miniature, indexed exactly as the real one is."""

    ARTICLES = [
        ("train-a", "train", "个股新闻", "长鑫科技扩建十二英寸晶圆产线",
         "公司公告将在合肥扩建十二英寸晶圆产线，规划月产能三万片。", "产能布局调整", "正面"),
        ("train-b", "train", "个股新闻", "长鑫科技发布新一代存储芯片",
         "公司发布新一代动态随机存取存储器产品，制程较上代提升一个节点。", "关键技术突破", "正面"),
        ("train-c", "train", "行业新闻", "存储行业迎来政策支持",
         "工业和信息化部发布集成电路产业专项政策，支持存储芯片本土制造。", "行业政策", "中性"),
        ("test-x", "test", "个股新闻", "长鑫科技扩建十二英寸晶圆产线",
         "公司公告将在合肥扩建十二英寸晶圆产线，规划月产能三万片。", "产能布局调整", "正面"),
    ]
    # 「公共卫生事件」 is the one name the frozen map sends nowhere; it stands in
    # for any row that can demonstrate an emotion but not an issue code.
    TAXONOMY = {
        "_version_label": "test-map",
        "event_name_to_issue_codes": {"产能布局调整": ["YIELD_CAPACITY"],
                                      "关键技术突破": ["TECH_LEVEL"]},
        "industry_event_name_to_issue_codes": {"行业政策": ["INDUSTRY_POLICY"],
                                               "公共卫生事件": []},
        "event_name_to_event_types": {}, "industry_event_name_to_event_types": {},
    }

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE gold_articles(article_file_id TEXT PRIMARY KEY, split TEXT, sheet TEXT,
                entity_kind TEXT, entity_id TEXT, article_source TEXT, title TEXT,
                published_at TEXT, content_hash TEXT, version_id TEXT);
            CREATE TABLE gold_article_text(article_file_id TEXT PRIMARY KEY, text TEXT);
            CREATE TABLE gold_event_labels(article_file_id TEXT PRIMARY KEY, event_name TEXT,
                event_emotion TEXT, impact_analysis_hash TEXT);""")
        for article_id, split, sheet, title, text, event, emotion in self.ARTICLES:
            self.db.execute("""INSERT INTO gold_articles(article_file_id,split,sheet,entity_kind,
                entity_id,article_source,title,published_at,content_hash,version_id)
                VALUES(?,?,?,'company','e1','媒体',?,'2026-02-01','h',?)""",
                (article_id, split, sheet, title, article_id))
            self.db.execute("INSERT INTO gold_article_text VALUES(?,?)", (article_id, text))
            self.db.execute("INSERT INTO gold_event_labels VALUES(?,?,?,'h')",
                            (article_id, event, emotion))
        ensure_schema(self.db)

    def build(self):
        return rebuild(self.db, self.TAXONOMY)

    def ids(self, examples):
        return [example["article_file_id"] for example in examples]


class PoolIsTrainOnly(Fixture):
    """Invariant 1.  The pool decides what every score in E1 and E3 means."""

    def test_the_index_holds_only_train(self):
        self.build()
        self.assertEqual(indexed_splits(self.db), {TRAIN_SPLIT})

    def test_a_test_article_is_never_retrievable(self):
        # 'test-x' is a byte-for-byte copy of 'train-a', so any query that finds
        # one would find the other if the pool were not restricted.
        self.build()
        found = examples_for(self.db, "扩建十二英寸晶圆产线 月产能三万片", 4, "issue_codes")
        self.assertTrue(found, "the query must retrieve something for this to prove anything")
        self.assertNotIn("test-x", self.ids(found))

    def test_rebuild_takes_no_split_argument(self):
        # A split parameter is one distracted afternoon away from being passed
        # "test", and nothing downstream would notice.
        import inspect
        self.assertNotIn("split", inspect.signature(rebuild).parameters)


class LeaveOneOut(Fixture):
    """Invariant 2.  Judging train while retrieving train is copying the answer."""

    def test_an_article_does_not_retrieve_itself(self):
        self.build()
        query = query_text("长鑫科技扩建十二英寸晶圆产线", "公司公告将在合肥扩建十二英寸晶圆产线。")
        with_self = self.ids(examples_for(self.db, query, 4, "issue_codes"))
        without = self.ids(examples_for(self.db, query, 4, "issue_codes",
                                        exclude_article_id="train-a"))
        self.assertIn("train-a", with_self)
        self.assertNotIn("train-a", without)


class OffMeansOff(Fixture):
    """Invariant 3.  k=0 has to be provably inert, not promised to be."""

    def test_zero_shots_returns_nothing_and_runs_no_query(self):
        self.build()
        for shots in (0, -1):
            self.assertEqual(examples_for(self.db, "晶圆产线", shots, "issue_codes"), [])

    def test_zero_shots_needs_no_index_at_all(self):
        # Not merely empty: it must not touch the tables, so a harness that
        # never built the index still runs the zero-shot path.
        db = sqlite3.connect(":memory:")
        self.assertEqual(examples_for(db, "晶圆产线", 0, "issue_codes"), [])


class FieldFiltering(Fixture):
    def test_a_row_the_map_cannot_code_is_not_offered_as_a_code_example(self):
        self.db.execute("UPDATE gold_event_labels SET event_name='公共卫生事件' WHERE article_file_id='train-c'")
        self.db.execute("UPDATE gold_articles SET sheet='行业新闻' WHERE article_file_id='train-c'")
        self.build()
        stored = self.db.execute(
            "SELECT issue_codes_json FROM gold_examples WHERE article_file_id='train-c'").fetchone()
        self.assertEqual(json.loads(stored[0]), [])
        found = examples_for(self.db, "集成电路产业专项政策 支持存储芯片本土制造", 4, "issue_codes")
        self.assertNotIn("train-c", self.ids(found))

    def test_the_same_row_still_serves_as_an_emotion_example(self):
        self.db.execute("UPDATE gold_event_labels SET event_name='公共卫生事件' WHERE article_file_id='train-c'")
        self.db.execute("UPDATE gold_articles SET sheet='行业新闻' WHERE article_file_id='train-c'")
        self.build()
        found = examples_for(self.db, "集成电路产业专项政策 支持存储芯片本土制造", 4, "event_emotion")
        self.assertIn("train-c", self.ids(found))


class TokenAgreement(Fixture):
    def test_index_and_query_are_filtered_through_one_function(self):
        # bigrams emits Latin tokens carrying '.' and '%'; FTS5's unicode61
        # splits on exactly those, so a term table recording the pre-split form
        # lists terms the index does not hold -- and being the rarest terms in
        # any article, they crowded out every usable one.
        self.assertIn("17.26%", __import__("processor.claim_index", fromlist=["bigrams"]).bigrams("17.26%"))
        self.assertNotIn("17.26%", searchable("17.26%"))

    def test_every_indexed_term_is_reachable_by_a_query(self):
        self.build()
        for (token,) in self.db.execute("SELECT token FROM gold_example_terms"):
            self.assertTrue(token.isalnum(), token)


class Rendering(Fixture):
    def test_an_example_shows_the_answer_and_no_invented_rationale(self):
        self.build()
        found = examples_for(self.db, "扩建十二英寸晶圆产线", 1, "issue_codes")
        block = render(found, "issue_codes")
        self.assertIn("答案：YIELD_CAPACITY", block)
        for word in ("理由", "reason", "因为"):
            self.assertNotIn(word, block)

    def test_the_query_is_the_lead_not_the_whole_article(self):
        long_text = "首段讲扩产。" + "尾段讲别的。" * 500
        self.assertEqual(len(query_text("标题", long_text)), len("标题") + 1 + QUERY_CHARS)


if __name__ == "__main__":
    unittest.main()
