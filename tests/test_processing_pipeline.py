import json
import sqlite3
import unittest
from pathlib import Path

from processor.pipeline import (
    BODY_TEXT_FLOOR, RawDocument, boilerplate_lines, build_duplicate_clusters, decode_html,
    ensure_schema, extract_amounts, extract_html, extract_ratios, normalize_title,
    EVENT_DEPENDENTS, FOREIGN_EVENT_ID_TABLES, HUMAN_RECORD_TABLES, STATE_DB, delete_event,
    is_paginated, listing_root, listing_roots, reclassify_listings, issue_adjustments,
    numeric_tokens, rebuild_issue_ledger, rank_codes, rule_label, set_review_reason, simhash,
    strip_boilerplate, tokenize, upsert_event,
)


class TextExtractionTests(unittest.TestCase):
    def test_declared_gb2312_charset_is_honoured(self):
        body = '<html><head><meta charset="gb2312"></head><body>长鑫科技招股说明书</body></html>'.encode("gb18030")
        self.assertIn("长鑫科技招股说明书", decode_html(body))

    def test_utf8_is_decoded_without_a_declaration(self):
        self.assertIn("药明康德", decode_html("<html><body>药明康德</body></html>".encode()))

    def test_undecodable_bytes_do_not_raise(self):
        self.assertIsInstance(decode_html(b"\xff\xfe\x00 <html>x</html>"), str)

    def test_article_container_wins_over_page_chrome(self):
        body = ("<html><body><div class='sidebar'>" + "无关推荐 " * 50 +
                "</div><article>" + "长鑫科技于今日完成注册。" * 20 + "</article></body></html>").encode()
        text, _ = extract_html(body)
        self.assertIn("长鑫科技于今日完成注册", text)
        self.assertNotIn("无关推荐", text)

    def test_short_article_container_falls_back_to_full_page(self):
        # A teaser-sized <article> must not hide the real body.
        body = ("<html><body><article>短</article><div>" + "正文内容。" * 60 +
                "</div></body></html>").encode()
        text, _ = extract_html(body)
        self.assertGreaterEqual(len(text), BODY_TEXT_FLOOR)

    def test_repeated_lines_are_learned_as_chrome_and_stripped(self):
        pages = [f"版权所有\n免责声明\n第{index}条独有内容" for index in range(6)]
        chrome = boilerplate_lines(pages)
        self.assertEqual(chrome, {"版权所有", "免责声明"})
        self.assertEqual(strip_boilerplate(pages[0], chrome), "第0条独有内容")

    def test_chrome_is_not_learned_from_too_few_pages(self):
        # Two pages about one event share real sentences; that is not chrome.
        self.assertEqual(boilerplate_lines(["共同的一句话", "共同的一句话"]), set())

    def test_line_repeated_inside_one_page_is_not_chrome(self):
        self.assertEqual(boilerplate_lines(["菜单\n菜单\n菜单\n甲"] + [f"乙{i}" for i in range(5)]), set())


class DuplicateClusteringTests(unittest.TestCase):
    def _db(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE document_versions(version_id TEXT PRIMARY KEY)")
        ensure_schema(db)
        return db

    def _doc(self, version_id, title, published_at):
        return RawDocument(version_id, "media", "S2", "cxmt", title, published_at,
                           f"https://example.test/{version_id}", "independent_news", version_id,
                           f"data/{version_id}.html", "ok")

    def _clusters(self, db):
        return {row[0]: row[1] for row in
                db.execute("""SELECT cluster_id,count(*) FROM duplicate_cluster_members GROUP BY cluster_id""")}

    def test_reprints_of_one_article_cluster_together(self):
        db = self._db()
        body = "长鑫科技于2026年7月27日在上交所科创板上市，募集资金总额295亿元。" * 8
        candidates = [(self._doc(f"v{i}", "长鑫科技上市", f"2026-07-2{i}"), body) for i in range(3)]
        for doc, _ in candidates:
            db.execute("INSERT INTO document_versions(version_id) VALUES(?)", (doc.version_id,))
            db.execute("""INSERT INTO processed_documents(version_id,run_id,content_hash,extraction_status,
                inclusion_status,processor_version,processed_at) VALUES(?,'r',?,'ok','included','v','t')""",
                (doc.version_id, doc.version_id))
        build_duplicate_clusters(db, candidates)
        self.assertEqual(list(self._clusters(db).values()), [3])

    def test_unrelated_articles_are_not_chained_into_one_cluster(self):
        # A~B and B~C must not put A and C together: that transitive merge is
        # what previously collapsed 107 unrelated articles into one cluster.
        db = self._db()
        shared = "版权声明 本网站提供之资料仅供投资者参考，不构成投资建议。" * 12
        texts = {
            "a": shared + "长鑫科技公布上市首日成交额。" * 6,
            "b": shared + "长鑫科技公布上市首日成交额。" * 3 + "药明康德发布中报业绩。" * 3,
            "c": shared + "药明康德发布中报业绩。" * 6,
        }
        candidates = [(self._doc(key, f"标题{key}", f"2026-07-2{index}"), text)
                      for index, (key, text) in enumerate(texts.items())]
        for doc, _ in candidates:
            db.execute("INSERT INTO document_versions(version_id) VALUES(?)", (doc.version_id,))
            db.execute("""INSERT INTO processed_documents(version_id,run_id,content_hash,extraction_status,
                inclusion_status,processor_version,processed_at) VALUES(?,'r',?,'ok','included','v','t')""",
                (doc.version_id, doc.version_id))
        build_duplicate_clusters(db, candidates)
        members = db.execute("""SELECT m.cluster_id,m.version_id FROM duplicate_cluster_members m""").fetchall()
        by_cluster = {}
        for cluster_id, version_id in members:
            by_cluster.setdefault(cluster_id, set()).add(version_id)
        for group in by_cluster.values():
            self.assertNotEqual(group, {"a", "b", "c"}, "unrelated documents were chained together")
            self.assertFalse({"a", "c"} <= group, "a and c share only boilerplate")

    def _cluster(self, candidates, **params):
        db = self._db()
        for doc, _ in candidates:
            db.execute("INSERT INTO document_versions(version_id) VALUES(?)", (doc.version_id,))
            db.execute("""INSERT INTO processed_documents(version_id,run_id,content_hash,extraction_status,
                inclusion_status,processor_version,processed_at) VALUES(?,'r',?,'ok','included','v','t')""",
                (doc.version_id, doc.version_id))
        build_duplicate_clusters(db, candidates, **params)
        groups = {}
        for cluster_id, version_id in db.execute(
                "SELECT cluster_id,version_id FROM duplicate_cluster_members"):
            groups.setdefault(cluster_id, set()).add(version_id)
        return groups

    # The brief and the long piece that swallows it.  Jaccard divides by the
    # union and so reads this pair as unrelated; containment divides by the
    # smaller side and reads it correctly.  This shape is 63 of the 174 pairs
    # the gold said were duplicates and the old gate missed.
    LEAD = "长鑫科技于2026年7月27日在上交所科创板上市，募集资金总额295亿元。" * 4
    # Varied rather than one sentence repeated, because repetition adds no new
    # bigrams and so leaves the two signatures close.  As written this pair is
    # 6.4x apart in length, shares *no* band, sits at hamming 20, and scores
    # Jaccard 0.156 against containment 1.000 -- the gold's shape exactly, and
    # unreachable by any setting of bands, hamming_max or the two Jaccard bars.
    TAIL = ("公司主营动态随机存取存储器的设计制造与销售，产能位居国内首位。"
            "本次募集资金将用于合肥十二英寸晶圆产线的扩产及先进制程研发。"
            "报告期内研发投入四十七亿元，占营业收入比重达到百分之十八点六。"
            "公司与多家服务器厂商签署长期供货协议，覆盖数据中心与消费终端。") * 6

    def test_a_brief_joins_the_long_article_that_contains_it(self):
        candidates = [(self._doc("brief", "长鑫科技上市", "2026-07-27"), self.LEAD),
                      (self._doc("full", "长鑫科技上市", "2026-07-28"), self.LEAD + self.TAIL)]
        self.assertEqual(list(self._cluster(candidates).values()), [{"brief", "full"}])

    def test_jaccard_alone_would_have_missed_it(self):
        # Not a tautology check: it pins the reason the pair needs the new route
        # at all, so if tokenisation ever changes enough to lift the Jaccard the
        # fixture stops proving anything and says so here first.
        candidates = [(self._doc("brief", "长鑫科技上市", "2026-07-27"), self.LEAD),
                      (self._doc("full", "长鑫科技上市", "2026-07-28"), self.LEAD + self.TAIL)]
        self.assertEqual(self._cluster(candidates, containment_threshold=None), {})

    def test_a_reused_announcement_heading_still_does_not_merge(self):
        # 「H股公告」 is a heading 巨潮 reuses across years for unrelated filings;
        # on the corpus those pairs sit at containment 0.084-0.095.  An identical
        # title is not on its own evidence of anything.
        candidates = [(self._doc("q1", "H股中期报告", "2026-03-31"),
                       "本公司董事会宣布截至三月三十一日止季度营业收入为一百二十亿元。" * 12),
                      (self._doc("q2", "H股中期报告", "2026-09-30"),
                       "药明康德就美国生物安全法案的进展向全体股东作出澄清说明。" * 12)]
        self.assertEqual(self._cluster(candidates), {})

    def test_an_untitled_pair_cannot_take_the_title_route(self):
        # normalize_title strips 「公告」 and every non-word character, so a
        # document headed 「公告」 and one with no title at all both reduce to
        # "" -- which must not count as a shared title.
        # Deliberately a pair the LSH route *does* reach -- one shared band,
        # hamming 16, right at the ceiling -- so the bands cannot be what keeps
        # them apart.  Jaccard is 0.245, under even the title bar, and
        # containment is 1.000: only the empty-title guard is left.
        body = "本公司现将有关事项公告如下，具体内容详见附件所载文本。" * 4
        candidates = [(self._doc("bare", "公告", "2026-03-31"), body
                       + "另经董事会审议通过关于变更募集资金投资项目的相关议案。"
                       + "公司拟以自有资金回购部分社会公众股份用于员工持股计划。"
                       + "监事会认为本次交易的审议程序符合法律法规及公司章程规定。"),
                      (self._doc("none", None, "2026-09-30"), body)]
        self.assertEqual(self._cluster(candidates), {})

    def test_the_same_heading_a_month_later_is_a_different_filing(self):
        # 巨潮 files 「H股公告」 under those same three characters for years, and
        # two Next Day Disclosure Returns differ only in their numbers -- which
        # tokenize does not emit at all -- so on text alone they are identical.
        # Contemporaneity is what separates a reprint from a recurring form.
        candidates = [(self._doc("july", "长鑫科技上市", "2026-07-27"), self.LEAD),
                      (self._doc("august", "长鑫科技上市", "2026-08-27"), self.LEAD + self.TAIL)]
        self.assertEqual(self._cluster(candidates), {})

    def test_a_document_with_no_date_cannot_be_shown_contemporaneous(self):
        candidates = [(self._doc("dated", "长鑫科技上市", "2026-07-27"), self.LEAD),
                      (self._doc("undated", "长鑫科技上市", None), self.LEAD + self.TAIL)]
        self.assertEqual(self._cluster(candidates), {})

    # 港交所翌日披露報表 in miniature: one form, filed twice, identical but for
    # the figures.  `tokenize` emits no token for a bare number, so to every
    # gate above these two documents are the same document.
    FORM = ("翌日披露報表　股份發行人及根據上市規則第十九B章上市的香港預託證券發行人的證券變動月報表。"
            "呈交日期。狀態：新提交。公司名稱：無錫藥明康德新藥開發股份有限公司。"
            "本月底結存的法定股本及已發行股份數目，及本月內的變動詳情如下所載。") * 4

    def test_one_form_filed_twice_is_two_filings_not_a_reprint(self):
        candidates = [
            (self._doc("june", "翌日披露報表", "2026-06-30"),
             self.FORM + "610,000.0000 231,920.2025 1,598,773,691 62,072,977"),
            (self._doc("july", "翌日披露報表", "2026-07-31"),
             self.FORM + "884,113.5500 407,662.8100 1,592,430,946 55,730,232"),
        ]
        self.assertEqual(self._cluster(candidates, **self.VETO), {})

    def test_the_same_form_with_the_same_figures_is_still_one_document(self):
        # The veto must key on the numbers diverging, not on the document being
        # a form.  Two copies of one filing still belong together.
        figures = "610,000.0000 231,920.2025 1,598,773,691 62,072,977"
        candidates = [(self._doc("filed", "翌日披露報表", "2026-06-30"), self.FORM + figures),
                      (self._doc("mirror", "翌日披露報表", "2026-06-30"), self.FORM + figures)]
        self.assertEqual(list(self._cluster(candidates).values()), [{"filed", "mirror"}])

    def test_a_reprint_whose_prose_differs_is_not_judged_on_its_numbers(self):
        # The veto only looks at pairs whose prose is near-identical.  A genuine
        # rewrite shares fewer words and must stay on the ordinary gates, or the
        # numbers would start deciding cases they know nothing about.  The long
        # side carries figures the short one does not, so dropping the prose bar
        # would veto a pair the containment route is there to recover.
        brief = self.LEAD
        full = self.LEAD + ("公司披露2027年半年度研发投入487亿元，产能较上年提升63%，"
                            "在建产线合计9条，员工总数18426人。") * 6 + self.TAIL
        self.assertLess(len(numeric_tokens(brief) & numeric_tokens(full))
                        / len(numeric_tokens(brief) | numeric_tokens(full)), 0.5)
        candidates = [(self._doc("brief", "长鑫科技上市", "2026-07-27"), brief),
                      (self._doc("full", "长鑫科技上市", "2026-07-28"), full)]
        self.assertEqual(list(self._cluster(candidates).values()), [{"brief", "full"}])

    def test_the_same_figure_written_two_ways_is_one_number(self):
        # Filings write 1,598,773,691 and tables write 1598773691.  Left
        # unnormalised they are two numbers, every form pair looks divergent,
        # and the veto starts rejecting genuine duplicates.
        self.assertEqual(numeric_tokens("持股 1,598,773,691 股"),
                         numeric_tokens("持股 1598773691 股"))
        self.assertEqual(numeric_tokens("610,000.0000"), {"610000.0000"})

    def test_documents_carrying_no_numbers_are_not_vetoed(self):
        # An empty numeric set on both sides says nothing about whether they are
        # the same filing; dividing by it would reject every prose duplicate.
        body = "公司就近期市场传闻作出澄清，并提醒投资者注意投资风险。" * 12
        candidates = [(self._doc("a", "澄清公告", "2026-06-30"), body),
                      (self._doc("b", "澄清公告", "2026-06-30"), body + "特此公告。")]
        self.assertEqual(list(self._cluster(candidates).values()), [{"a", "b"}])

    # Page counts, form versions and form numbers recur in every filing a company
    # makes.  Left in they lift two different filings' numeric similarity above
    # the bar and the veto stops firing; pruning them took the corpus's
    # 翌日披露報表 from a median numeric Jaccard of 0.298 to 0.000.  Twenty
    # filings, because the share is 0.15 and below about seven documents every
    # figure looks common (see the fallback below).
    BOILERPLATE_FIGURES = "表格編號 305 版本 1.3.0 共 9 頁。"
    # The veto ships disabled (交付物7 §7ter); these tests describe what it does
    # when switched on, which is how the calibration record stays reproducible.
    VETO = {"template_text_bar": 0.90, "template_number_bar": 0.50,
            "template_number_share": 0.15}

    def test_the_template_veto_is_off_by_default(self):
        # Seventeen grid points, none better than leaving it off.  Defaulting it
        # on would quietly cost 0.0097 F2 on the gold to halve an artifact it
        # cannot remove.
        self.assertEqual(self._cluster(self._filings(20)) != {}, True)

    def _filings(self, count, distinct=True):
        out = []
        for index in range(count):
            figures = (f" {600000 + index * 7777}.25 {1500000000 + index * 13}"
                       if distinct else " 888888.25 1500000000")
            out.append((self._doc(f"f{index}", "翌日披露報表", f"2026-06-{index + 1:02d}"),
                        self.FORM + self.BOILERPLATE_FIGURES + figures))
        return out

    def test_a_figure_this_company_repeats_everywhere_identifies_nothing(self):
        # Every filing shares 305 / 1.3 / 9 and carries two figures of its own.
        # Only the pruning can tell them apart: on the raw sets they overlap on
        # three of five numbers, comfortably above the bar.
        self.assertEqual(self._cluster(self._filings(20), **self.VETO), {})

    def test_filings_carrying_the_same_figures_are_still_one_document(self):
        # The veto keys on the figures diverging, not on the document being a
        # form.  Twenty copies of one filing still belong together.
        groups = self._cluster(self._filings(20, distinct=False), **self.VETO)
        self.assertEqual([len(members) for members in groups.values()], [20])

    def test_pruning_never_empties_a_document(self):
        # Two filings only: the share leaves a ceiling below one, so every figure
        # is pruned.  Comparing two empty sets would pass the veto silently, so
        # the unpruned sets are used instead and the pair is still rejected.
        candidates = [(self._doc("june", "翌日披露報表", "2026-06-30"),
                       self.FORM + "共 9 頁 1,598,773,691 62,072,977 305"),
                      (self._doc("july", "翌日披露報表", "2026-07-31"),
                       self.FORM + "共 9 頁 1,592,430,946 55,730,232 305")]
        self.assertEqual(self._cluster(candidates, **self.VETO), {})

    def test_containment_alone_is_not_enough_without_the_shared_title(self):
        # Measured on the gold: opening the containment route to differing
        # titles buys 19 true pairs and costs 82 false ones.  The shared title
        # is not decoration on this rule, it is most of the evidence.
        candidates = [(self._doc("brief", "长鑫科技上市", "2026-07-27"), self.LEAD),
                      (self._doc("full", "科创板迎来新股", "2026-07-28"), self.LEAD + self.TAIL)]
        self.assertEqual(self._cluster(candidates), {})

    def test_the_containment_bar_is_a_bar(self):
        candidates = [(self._doc("brief", "长鑫科技上市", "2026-07-27"), self.LEAD),
                      (self._doc("full", "长鑫科技上市", "2026-07-28"), self.LEAD + self.TAIL)]
        self.assertEqual(self._cluster(candidates, containment_threshold=1.01), {})


class ProcessingPipelineTests(unittest.TestCase):
    def test_title_normalization_and_token_fingerprint_are_stable(self):
        self.assertEqual(normalize_title("【公告】 长鑫科技：上市！"), "长鑫科技上市")
        self.assertEqual(simhash(tokenize("长鑫科技 DRAM 产品")), simhash(tokenize("长鑫科技 DRAM 产品")))

    def test_rule_label_keeps_traceable_claim(self):
        doc = RawDocument("v", "media", "S2", "cxmt", "长鑫科技完成上市发行", "2026-07-22",
                          "https://example.test/a", "independent_news", "h", "data/a.html", "ok")
        text = "长鑫科技于2026年7月22日完成上市发行，募集资金100亿元。"
        label = rule_label(doc, text)
        self.assertEqual(label["eventhood"], "EVENT")
        self.assertIn("IPO_REFINANCING", label["event_types"])
        self.assertGreaterEqual(len(label["claims"]), 1)
        self.assertEqual(label["claims"][0]["source_start"], 0)

    def test_numeric_extractors_do_not_guess_units(self):
        self.assertEqual(extract_amounts("募资100亿元"), [{"raw": "100亿元", "value": "100", "unit": "亿元"}])
        self.assertEqual(extract_ratios("收入增长12.5%"), [{"raw": "12.5%", "value": "12.5", "unit": "%"}])

    def test_downgrade_to_no_event_removes_child_claims(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE document_versions(version_id TEXT PRIMARY KEY)")
        ensure_schema(db)
        doc = RawDocument("v", "media", "S2", "cxmt", "title", "2026-01-01",
                          "https://example.test", "news", "hash", "raw", "ok")
        db.execute("INSERT INTO document_versions(version_id) VALUES('v')")
        db.execute("""INSERT INTO event_records(event_id,version_id,run_id,company_id,event_types_json,
            issue_codes_json,production_mode,event_subjects_json,affected_objects_json,impact_direction,
            impact_magnitude,article_sentiment,relevance_score,eventhood,summary,amounts_json,ratios_json,
            persons_json,organizations_json,products_json,risk_score,risk_level,recommended_action,label_source,
            extraction_confidence,raw_label_json,created_at,updated_at)
            VALUES('e','v','r','cxmt','[]','[]','BRIEF','[]','[]','neutral','minor','neutral',.5,
            'EVENT','s','[]','[]','[]','[]','[]',1,'low','archive','rule',.5,'{}','t','t')""")
        db.execute("""INSERT INTO event_claims(claim_id,event_id,ordinal,text,claim_type,source_start,
            source_end,confidence) VALUES('c','e',1,'claim','fact',0,5,.5)""")
        upsert_event(db, doc, "r2", {"eventhood": "NO_EVENT"}, "ai")
        self.assertEqual(db.execute("SELECT count(*) FROM event_records").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT count(*) FROM event_claims").fetchone()[0], 0)


class LedgerTests(unittest.TestCase):
    """The 议题台账 feeds M4-4 and M5-2, so its counts have to mean what they say."""

    def _db(self):
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE document_versions(version_id TEXT PRIMARY KEY, source_id TEXT,
            source_tier TEXT, published_at TEXT)""")
        ensure_schema(db)
        return db

    def _event(self, db, version_id, source_id, tier="S2", published_at="2026-07-20",
               event_time=None, issues=("TECH_LEVEL",)):
        db.execute("INSERT INTO document_versions VALUES(?,?,?,?)",
                   (version_id, source_id, tier, published_at))
        db.execute("""INSERT INTO event_records(event_id,version_id,run_id,company_id,event_types_json,
            issue_codes_json,production_mode,event_subjects_json,event_time,affected_objects_json,
            impact_direction,impact_magnitude,article_sentiment,relevance_score,eventhood,summary,
            amounts_json,ratios_json,persons_json,organizations_json,products_json,risk_score,risk_level,
            recommended_action,label_source,extraction_confidence,raw_label_json,created_at,updated_at)
            VALUES(?,?,'r','cxmt','[]',?,'BRIEF','[]',?,'[]','neutral','minor','neutral',.5,'EVENT','s',
            '[]','[]','[]','[]','[]',10,'low','archive','ai',.9,'{}','t','t')""",
            (f"e{version_id}", version_id, json.dumps(list(issues)), event_time))

    def _cluster(self, db, cluster_id, representative, members):
        db.execute("""INSERT INTO duplicate_clusters(cluster_id,company_id,cluster_kind,
            representative_version_id,member_count,created_at) VALUES(?,'cxmt','near_duplicate',?,?,'t')""",
            (cluster_id, representative, len(members)))
        for version_id in members:
            db.execute("INSERT INTO duplicate_cluster_members VALUES(?,?,1.0)", (cluster_id, version_id))

    def _ledger(self, db):
        rebuild_issue_ledger(db)
        return db.execute("""SELECT event_count,document_count,independent_source_count,
            first_seen_at,last_seen_at,event_time_earliest,event_time_latest FROM issue_ledger""").fetchone()

    def test_reprints_of_one_story_count_as_one_independent_source(self):
        # 转载 of a single article is not corroboration; counting each carrier
        # separately is what inflated 独立信源数.
        db = self._db()
        for index, source in enumerate(("origin", "reprint_a", "reprint_b")):
            self._event(db, f"v{index}", source)
        self._cluster(db, "c1", "v0", ["v0", "v1", "v2"])
        self.assertEqual(self._ledger(db)[2], 1)

    def test_genuinely_separate_reports_still_count_separately(self):
        db = self._db()
        for index, source in enumerate(("outlet_a", "outlet_b")):
            self._event(db, f"v{index}", source)
        self.assertEqual(self._ledger(db)[2], 2)

    def test_document_count_includes_folded_exact_duplicates(self):
        db = self._db()
        self._event(db, "v0", "origin")
        db.execute("""INSERT INTO processed_documents(version_id,run_id,content_hash,extraction_status,
            inclusion_status,exact_duplicate_of,processor_version,processed_at)
            VALUES('v9','r','h','ok','duplicate','v0','v','t')""")
        event_count, document_count = self._ledger(db)[:2]
        self.assertEqual((event_count, document_count), (1, 2))

    def test_first_seen_falls_back_to_event_time_when_publication_date_is_missing(self):
        # 839 versions carry no published_at; without the fallback they vanish
        # from the issue's date range instead of being dated by their event.
        db = self._db()
        self._event(db, "v0", "a", published_at=None, event_time="2026-03-05")
        self._event(db, "v1", "b", published_at="2026-07-20", event_time="2026-07-19")
        first_seen, last_seen = self._ledger(db)[3:5]
        self.assertEqual((first_seen, last_seen), ("2026-03-05", "2026-07-20"))

    def test_event_time_range_is_recorded_separately_from_reporting_dates(self):
        db = self._db()
        self._event(db, "v0", "a", published_at="2026-07-20", event_time="2026-01-10")
        self._event(db, "v1", "b", published_at="2026-07-21", event_time="2026-02-01/2026-02-28")
        row = self._ledger(db)
        self.assertEqual((row[3], row[4]), ("2026-07-20", "2026-07-21"))
        self.assertEqual((row[5], row[6]), ("2026-01-10", "2026-02-28"))

    def test_unparseable_event_time_is_kept_out_of_the_range(self):
        db = self._db()
        self._event(db, "v0", "a", published_at="2026-07-20", event_time="截至11:00")
        self.assertEqual(self._ledger(db)[5], None)


class ReviewReasonTests(unittest.TestCase):
    def _db(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE document_versions(version_id TEXT PRIMARY KEY)")
        ensure_schema(db)
        db.execute("INSERT INTO document_versions(version_id) VALUES('v')")
        return db

    def _label(self, confidence):
        return {"eventhood": "EVENT", "event_types": [], "issue_codes": [], "production_mode": "BRIEF",
                "impact_direction": "neutral", "impact_magnitude": "minor", "article_sentiment": "neutral",
                "relevance_score": 0.5, "summary": "s", "confidence": confidence, "claims": []}

    def _flag(self, db):
        return db.execute("SELECT needs_review,review_reasons_json FROM event_records").fetchone()

    def test_being_ai_labelled_is_not_by_itself_a_review_reason(self):
        # Flagging on label_source marked all 952 events and left no signal.
        db = self._db()
        doc = RawDocument("v", "media", "S2", "cxmt", "t", "2026-01-01",
                          "https://example.test", "news", "h", "raw", "ok")
        upsert_event(db, doc, "r", self._label(0.9), "ai")
        self.assertEqual(self._flag(db), (0, "[]"))

    def test_low_confidence_still_flags(self):
        db = self._db()
        doc = RawDocument("v", "media", "S2", "cxmt", "t", "2026-01-01",
                          "https://example.test", "news", "h", "raw", "ok")
        upsert_event(db, doc, "r", self._label(0.4), "ai")
        self.assertEqual(self._flag(db), (1, '["low_confidence"]'))

    def test_reasons_accumulate_and_clear_independently(self):
        db = self._db()
        doc = RawDocument("v", "media", "S2", "cxmt", "t", "2026-01-01",
                          "https://example.test", "news", "h", "raw", "ok")
        event_id = upsert_event(db, doc, "r", self._label(0.4), "ai")
        set_review_reason(db, event_id, "unresolved_elements", True)
        self.assertEqual(self._flag(db), (1, '["low_confidence", "unresolved_elements"]'))
        # Resolving the date must not clear a flag raised for another cause.
        set_review_reason(db, event_id, "unresolved_elements", False)
        self.assertEqual(self._flag(db), (1, '["low_confidence"]'))
        set_review_reason(db, event_id, "low_confidence", False)
        self.assertEqual(self._flag(db), (0, "[]"))


if __name__ == "__main__":
    unittest.main()


class SecondhandModeTests(unittest.TestCase):
    def test_every_secondhand_mode_is_a_real_production_mode(self):
        from processor.pipeline import PRODUCTION_MODES, SECONDHAND_MODES
        self.assertTrue(SECONDHAND_MODES <= set(PRODUCTION_MODES))

    def test_a_template_ticker_scores_below_the_brief_it_echoes(self):
        # Before AI_GENERATED joined the set a template ticker kept the full
        # score while the brief it was generated from lost ten points.
        from processor.pipeline import risk
        label = {"event_types": [], "issue_codes": [], "eventhood": "EVENT",
                 "impact_direction": "negative", "impact_magnitude": "moderate",
                 "production_mode": "AI_GENERATED"}
        generated, _, _ = risk(label)
        original, _, _ = risk({**label, "production_mode": "ORIGINAL_INVESTIGATION"})
        self.assertEqual(original - generated, 10)


class RiskBreakdownTests(unittest.TestCase):
    """The score has to be the sum of the terms shown beside it, and re-derivable.

    Both properties were absent until 2026-09-06: the seven terms were an inline
    sum, so the pages could only show the total, and a pass that rewrote a risk
    input left the score describing the value it replaced -- 296 events, 52 of
    them in the wrong band.
    """

    LABEL = {"event_types": ["REGULATORY_COMPLIANCE"], "issue_codes": ["FINANCIAL"],
             "eventhood": "EVENT", "impact_direction": "negative", "impact_magnitude": "major",
             "production_mode": "BRIEF"}

    def test_the_parts_add_up_to_the_score(self):
        from processor.pipeline import risk, risk_parts
        score, _, _ = risk(self.LABEL)
        self.assertEqual(round(sum(part.points for part in risk_parts(self.LABEL)), 1), score)

    def test_each_part_names_the_value_that_earned_it(self):
        from processor.pipeline import risk_parts
        parts = {part.key: part for part in risk_parts(self.LABEL)}
        self.assertEqual(parts["sensitive_type"].detail, "REGULATORY_COMPLIANCE")
        self.assertEqual(parts["secondhand"].detail, "BRIEF")
        self.assertEqual(parts["magnitude"].detail, "major")

    def test_the_clamp_is_a_term_so_a_capped_score_still_adds_up(self):
        from processor.pipeline import risk, risk_parts
        # 15 + 20 + 20 + 20 + 20 + 10 = 105 before the ceiling.
        label = {**self.LABEL, "production_mode": "ORIGINAL_INVESTIGATION"}
        parts = risk_parts(label)
        score, _, _ = risk(label)
        self.assertEqual(score, 100.0)
        self.assertEqual(round(sum(part.points for part in parts), 1), score)
        self.assertIn("capped", {part.key for part in parts})

    def _event_db(self):
        import sqlite3
        from processor.pipeline import ensure_schema
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        ensure_schema(db)
        return db

    def _insert(self, db, **overrides):
        from processor.pipeline import risk
        values = {"event_types_json": "[]", "issue_codes_json": "[]", "eventhood": "EVENT",
                  "impact_direction": "negative", "impact_magnitude": "major",
                  "production_mode": "ORIGINAL_INVESTIGATION", **overrides}
        score, level, action = risk({"event_types": [], "issue_codes": [], "eventhood": "EVENT",
                                     "impact_direction": "negative", "impact_magnitude": "major",
                                     "production_mode": "ORIGINAL_INVESTIGATION"})
        db.execute("""INSERT INTO event_records(event_id,version_id,run_id,company_id,event_types_json,
            issue_codes_json,production_mode,event_subjects_json,affected_objects_json,impact_direction,
            impact_magnitude,article_sentiment,relevance_score,eventhood,summary,amounts_json,ratios_json,
            persons_json,organizations_json,products_json,risk_score,risk_level,recommended_action,
            label_source,extraction_confidence,needs_review,raw_label_json,created_at,updated_at)
            VALUES('e1','v1','r1','cxmt',?,?,?,'[]','[]',?,?,'neutral',0.9,?,'s','[]','[]','[]','[]','[]',
                   ?,?,?,'ai',0.9,0,'{}','now','now')""",
            (values["event_types_json"], values["issue_codes_json"], values["production_mode"],
             values["impact_direction"], values["impact_magnitude"], values["eventhood"],
             score, level, action))
        return score

    def test_refresh_risk_replays_a_score_left_behind_by_a_sidecar(self):
        from processor.pipeline import refresh_risk
        db = self._event_db()
        before = self._insert(db)
        # What production_mode's re-classification pass does: rewrite the column
        # and nothing else.  BRIEF is a secondhand mode, worth -10.
        db.execute("UPDATE event_records SET production_mode='BRIEF' WHERE event_id='e1'")
        self.assertEqual(db.execute("SELECT risk_score FROM event_records").fetchone()[0], before)
        refresh_risk(db, "e1")
        row = db.execute("SELECT risk_score,risk_level,risk_breakdown_json FROM event_records").fetchone()
        self.assertEqual(row["risk_score"], before - 10)
        self.assertEqual(row["risk_level"], "medium")
        parts = json.loads(row["risk_breakdown_json"])
        self.assertEqual(round(sum(part["points"] for part in parts), 1), row["risk_score"])
        self.assertIn("secondhand", {part["key"] for part in parts})

    def test_refresh_risk_reads_the_row_not_the_stored_raw_label(self):
        # raw_label_json still holds what the extractor first guessed; trusting it
        # would reproduce the stale answer this function exists to correct.
        from processor.pipeline import refresh_risk
        db = self._event_db()
        self._insert(db)
        db.execute("""UPDATE event_records SET production_mode='BRIEF',
                      raw_label_json='{"production_mode":"ORIGINAL_INVESTIGATION"}' WHERE event_id='e1'""")
        score, _, _ = refresh_risk(db, "e1")
        self.assertEqual(score, 65.0)


class LedgerRiskDenominatorTests(unittest.TestCase):
    """一条成交额快讯是这个议题的一次报道，但不是它的一份风险。

    所以 2026-09-07 起议题台账的风险均值只取承载风险的成员（事件类型层给出了至少一类），
    而报道数、首现时间与独立信源仍取全部。整行排除是过度排除：那会让 STOCK_PRICE 这类议题
    的报道数凭空少掉一截。
    """

    def _db(self, rows):
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE document_versions(version_id TEXT PRIMARY KEY, source_id TEXT,
                      source_tier TEXT, company_id TEXT, canonical_url TEXT, content_hash TEXT,
                      raw_path TEXT, published_at TEXT)""")
        db.execute("CREATE TABLE duplicate_clusters(cluster_id TEXT, representative_version_id TEXT)")
        db.execute("CREATE TABLE duplicate_cluster_members(cluster_id TEXT, version_id TEXT)")
        ensure_schema(db)
        for index, (types, risk) in enumerate(rows, start=1):
            version = f"v{index}"
            db.execute("""INSERT INTO document_versions(version_id,source_id,source_tier,company_id,
                          published_at) VALUES(?,?,?,?,?)""", (version, f"s{index}", "S2", "c",
                                                               f"2026-08-0{index}"))
            db.execute("""INSERT INTO event_records(event_id,version_id,run_id,company_id,
                event_types_json,issue_codes_json,production_mode,event_subjects_json,
                affected_objects_json,impact_direction,impact_magnitude,article_sentiment,
                relevance_score,eventhood,summary,amounts_json,ratios_json,persons_json,
                organizations_json,products_json,risk_score,risk_level,recommended_action,
                label_source,extraction_confidence,needs_review,review_reasons_json,
                raw_label_json,created_at,updated_at)
                VALUES(?,?,'r','c',?,'["STOCK_PRICE"]','BRIEF','[]','[]','neutral','minor',
                       'neutral',0.9,'EVENT','摘要','[]','[]','[]','[]','[]',?,'medium','monitor',
                       'ai',0.9,0,'[]','{}','now','now')""",
                       (f"e{index}", version, types, risk))
        db.commit()
        return db

    def ledger(self, db):
        rebuild_issue_ledger(db)
        return db.execute("""SELECT event_count, risk_bearing_event_count, risk_score, risk_level
                             FROM issue_ledger""").fetchone()

    def test_a_flash_counts_as_coverage_but_not_as_risk(self):
        # 一条 80 分的真事件 + 一条 20 分的行情快讯：报道数 2，风险均值仍是 80 而不是 50。
        row = self.ledger(self._db([('["EQUITY_CONTROL"]', 80.0), ("[]", 20.0)]))
        self.assertEqual((row[0], row[1]), (2, 1))
        self.assertEqual(row[2], 80.0)
        self.assertEqual(row[3], "high")

    def test_an_issue_made_only_of_flashes_carries_no_risk(self):
        row = self.ledger(self._db([("[]", 90.0), ("[]", 95.0)]))
        self.assertEqual((row[0], row[1]), (2, 0))
        self.assertEqual(row[2], 0.0)
        self.assertEqual(row[3], "low")


class ConfigLoadingTests(unittest.TestCase):
    """配置解析是热路径，而这一点是被一次真实回归教出来的。

    2026-09-06 往 pipeline 里加 ``company_display_name`` 时，新函数插在了
    ``@lru_cache(maxsize=1)`` 与 ``def configured_companies`` 之间——**缓存被静默移到了新函数
    上**，``configured_companies()`` 从此每次调用都重新解析 53 KB 的 sources.yaml（223ms）。
    它被检索、议题与每个命令行入口调用，而没有任何测试会因此变红。现在有了。
    """

    def test_configured_companies_is_still_the_cached_one(self):
        from processor.pipeline import configured_companies
        self.assertTrue(hasattr(configured_companies, "cache_info"),
                        "configured_companies 的 lru_cache 掉了——多半是有人在装饰器和 def 之间插了东西")

    def test_the_config_is_parsed_once_per_file_version(self):
        from processor.pipeline import load_config
        self.assertIs(load_config(), load_config())

    def test_an_edited_config_is_not_hidden_by_the_cache(self):
        # 缓存按 (mtime_ns, size) 做键：省下解析可以，让人改了配置看不到不行。
        import tempfile
        from pathlib import Path
        from processor.pipeline import load_config
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "c.yaml"
            path.write_text("scope:\n  companies: [a]\n", encoding="utf-8")
            self.assertEqual(load_config(path)["scope"]["companies"], ["a"])
            path.write_text("scope:\n  companies: [a, b]\n", encoding="utf-8")
            self.assertEqual(load_config(path)["scope"]["companies"], ["a", "b"])

    def test_the_fast_parser_is_used_when_libyaml_is_present(self):
        # 纯 Python 解析器读这份配置 215ms，CSafeLoader 28.7ms；装了却不用是白付。
        import yaml
        from processor.pipeline import _YAML_LOADER
        if hasattr(yaml, "CSafeLoader"):
            self.assertIs(_YAML_LOADER, yaml.CSafeLoader)


class RuleProductionModeFallbackTests(unittest.TestCase):
    def test_the_rule_path_falls_back_to_brief_like_the_model_path(self):
        # The default used to be ORIGINAL_INVESTIGATION -- the highest weight in
        # alerts.MODE_CREDIBILITY (1.0) -- while the model path's fallback is
        # BRIEF (0.6) and its prompt warns against overusing it.  Two paths
        # feeding one credibility formula must not default in opposite directions.
        from processor.pipeline import production_mode
        self.assertEqual(production_mode("independent_news", "某公司召开业绩说明会", "正文与判据无关。"), "BRIEF")


class CodeRankingTests(unittest.TestCase):
    """A code is emitted for what a document is about, not for a passing mention.

    Matching on any keyword hit put 4.56 issue codes on the average benchmark
    article against a gold of 1.07, and the excess reached the issue ledger and
    the risk score, not only E1.
    """

    RULES = {"MAIN": ("产能", "扩产"), "ASIDE": ("估值",), "OTHER": ("专利",)}

    def test_a_passing_mention_loses_to_the_theme_the_text_returns_to(self):
        blob = "公司扩产。产能爬坡，产能利用率提升，产能规划明确。文末提了一句估值。"
        self.assertEqual(rank_codes("", blob, self.RULES), ["MAIN"])

    def test_a_title_hit_counts_for_more_than_a_body_mention(self):
        # Equal body evidence; only the title separates them.
        blob = "估值与产能各出现一次。"
        self.assertEqual(rank_codes("估值调整", blob, self.RULES), ["ASIDE"])

    def test_themes_of_comparable_weight_are_all_kept(self):
        blob = "产能扩产产能。估值估值估值。"
        self.assertEqual(sorted(rank_codes("", blob, self.RULES)), ["ASIDE", "MAIN"])

    def test_no_more_than_three_codes_survive(self):
        rules = {name: (name.lower(),) for name in ("A", "B", "C", "D", "E")}
        blob = " ".join(name.lower() for name in ("A", "B", "C", "D", "E"))
        self.assertEqual(len(rank_codes("", blob, rules)), 3)

    def test_a_document_that_matched_still_matches(self):
        # The event-hood gate reads event_types; ranking must never empty a set
        # that had a hit, or a document would silently stop being an event.
        self.assertEqual(rank_codes("", "只提到估值一次。", self.RULES), ["ASIDE"])

    def test_no_match_stays_empty(self):
        self.assertEqual(rank_codes("", "与词表完全无关的一句话。", self.RULES), [])

    def test_a_tie_breaks_on_the_code_name_so_a_run_repeats(self):
        blob = "估值。专利。"
        self.assertEqual(rank_codes("", blob, self.RULES), ["ASIDE", "OTHER"])

    def test_the_ledger_no_longer_inherits_every_incidental_theme(self):
        # The end-to-end shape of the fix, on a document that reads like the
        # benchmark ones: one subject, several words that used to each emit.
        doc = RawDocument("v", "media", "S2", "cxmt", "公司产能扩产项目投产", "2026-01-01",
                          "https://example.test/a", "independent_news", "h", "data/a.html", "ok")
        text = ("公司宣布产能扩产项目正式投产，新产线产能爬坡顺利，产能利用率持续提升。"
                "分析师同时提到了公司估值与在手专利情况。") * 3
        label = rule_label(doc, text)
        self.assertIn("YIELD_CAPACITY", label["issue_codes"])
        self.assertLessEqual(len(label["issue_codes"]), 3)



class ListingPageTests(unittest.TestCase):
    """An index of articles is not an article.

    ``BODY_TEXT_FLOOR`` exists to stop 「labelling chrome invents events out of
    headlines」, but it can only catch a page whose body never arrived.  A news
    index arrives complete: a thousand characters of genuine headlines, well
    over a floor of 200.  96 of them reached the labelling pool and 85 produced
    events, including one dated 2015 assembled out of a page-32 listing.
    """

    def test_a_page_number_marks_a_list(self):
        for url in ("https://x.test/news?page=2", "https://x.test/a?pageNo=10",
                    "https://x.test/b?pageIndex=3", "https://x.test/c?type=1&p=7"):
            self.assertTrue(is_paginated(url), url)

    def test_an_ordinary_article_is_not_paginated(self):
        for url in ("https://x.test/news/some-story", "https://x.test/a?id=2",
                    "https://x.test/a?page=next", "https://x.test/a?pagesize=10"):
            self.assertFalse(is_paginated(url), url)

    def test_the_root_is_the_same_url_without_its_page_number(self):
        # Page one of a list is the list, and carries no page number at all --
        # which is why it cannot be recognised without seeing page two.
        self.assertEqual(listing_root("https://x.test/news/wuxi-news?page=32"),
                         "https://x.test/news/wuxi-news")

    def test_other_query_parameters_survive_the_stripping(self):
        # sns.sseinfo.com numbers its feed with `page` beside four parameters
        # that identify *which* feed; dropping those would merge unrelated lists.
        self.assertEqual(listing_root("https://x.test/f.do?uid=1&type=11&page=3"),
                         "https://x.test/f.do?uid=1&type=11")

    def test_a_trailing_slash_is_not_a_different_list(self):
        self.assertEqual(listing_root("https://x.test/news/"), listing_root("https://x.test/news"))

    def test_roots_are_learned_from_the_corpus_not_configured_per_site(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE document_versions(canonical_url TEXT)")
        db.executemany("INSERT INTO document_versions VALUES(?)", [
            ("https://x.test/news",), ("https://x.test/news?page=2",),
            ("https://x.test/news/a-real-story",), ("https://y.test/only-ever-one-page",)])
        roots = listing_roots(db)
        self.assertEqual(roots, {"https://x.test/news"})
        # Page one is caught by its root; the article beneath it is not.
        self.assertIn(listing_root("https://x.test/news"), roots)
        self.assertNotIn(listing_root("https://x.test/news/a-real-story"), roots)
        self.assertNotIn(listing_root("https://y.test/only-ever-one-page"), roots)

    def test_a_url_never_paginated_is_left_alone(self):
        # The rule needs evidence that a site paginates that path.  Without it
        # every section landing page in the corpus would be a candidate.
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE document_versions(canonical_url TEXT)")
        db.execute("INSERT INTO document_versions VALUES('https://x.test/investors')")
        self.assertEqual(listing_roots(db), set())


class ReclassifyListingsTests(unittest.TestCase):
    """Applying the gate afterwards, without paying the price of ``--force``.

    ``run(force=True)`` re-labels everything from the rule layer, and
    ``upsert_event`` writes ``label_source='rule'`` through an unconditional
    upsert -- so a forced pass to fix 96 documents would silently replace every
    AI label in the corpus.  These hold the narrower path.
    """

    def _db(self):
        db = sqlite3.connect(":memory:")
        # document_versions belongs to the collection layer, so the processing
        # schema does not create it; only the two columns this reads are needed.
        db.execute("""CREATE TABLE document_versions(version_id TEXT PRIMARY KEY, source_id TEXT,
                      company_id TEXT, canonical_url TEXT, content_hash TEXT, raw_path TEXT)""")
        ensure_schema(db)
        rows = [
            ("v-list", "https://x.test/news", "included"),
            ("v-list-2", "https://x.test/news?page=2", "included"),
            ("v-story", "https://x.test/news/a-real-story", "included"),
        ]
        for version_id, url, _ in rows:
            db.execute("""INSERT INTO document_versions(version_id,source_id,company_id,canonical_url,
                          content_hash,raw_path) VALUES(?,?,?,?,?,?)""",
                       (version_id, "s", "c", url, version_id, "raw"))
            db.execute("""INSERT INTO processed_documents(version_id,run_id,content_hash,text_chars,
                          extraction_status,inclusion_status,processor_version,processed_at,eventhood)
                          VALUES(?,?,?,?,?,?,?,?,?)""",
                       (version_id, "r", version_id, 900, "ok", "included", "v", "now", "EVENT"))
            db.execute("""INSERT INTO event_records(event_id,version_id,run_id,company_id,
                          event_types_json,issue_codes_json,production_mode,event_subjects_json,
                          affected_objects_json,impact_direction,impact_magnitude,article_sentiment,
                          relevance_score,eventhood,summary,amounts_json,ratios_json,persons_json,
                          organizations_json,products_json,risk_score,risk_level,recommended_action,
                          label_source,extraction_confidence,needs_review,review_reasons_json,
                          raw_label_json,created_at,updated_at)
                          VALUES(?,?,?,?,'[]','[]','BRIEF','[]','[]','neutral','minor','neutral',
                                 0.9,'EVENT',?, '[]','[]','[]','[]','[]',10,'low','monitor','ai',
                                 0.9,0,'[]','{}','now','now')""",
                       (f"e-{version_id}", version_id, "r", "c", f"事件 {version_id}"))
        db.commit()
        return db

    def test_only_the_index_pages_are_excluded(self):
        db = self._db()
        counts = reclassify_listings(db)
        self.assertEqual(counts["excluded"], 2)
        statuses = dict(db.execute("SELECT version_id, inclusion_status FROM processed_documents"))
        self.assertEqual(statuses["v-list"], "excluded_listing_page")
        self.assertEqual(statuses["v-list-2"], "excluded_listing_page")
        self.assertEqual(statuses["v-story"], "included")

    def test_events_assembled_from_an_index_go_with_it(self):
        db = self._db()
        counts = reclassify_listings(db)
        self.assertEqual(counts["events_removed"], 2)
        remaining = [row[0] for row in db.execute("SELECT version_id FROM event_records")]
        self.assertEqual(remaining, ["v-story"])

    def test_the_article_keeps_its_ai_label(self):
        # The whole reason this exists instead of a forced rerun.
        db = self._db()
        reclassify_listings(db)
        source = db.execute("SELECT label_source FROM event_records WHERE version_id='v-story'").fetchone()
        self.assertEqual(source[0], "ai")

    def test_the_exclusion_reason_is_recorded_on_the_document(self):
        # The funnel page reads this column; an unexplained exclusion is worse
        # than none, because the count moves and nothing says why.
        db = self._db()
        reclassify_listings(db)
        reasons = db.execute("""SELECT rejection_reasons_json FROM processed_documents
                                WHERE version_id='v-list'""").fetchone()[0]
        self.assertIn("excluded_listing_page", json.loads(reasons))

    def test_running_it_twice_changes_nothing_the_second_time(self):
        db = self._db()
        reclassify_listings(db)
        again = reclassify_listings(db)
        self.assertEqual(again["excluded"], 0)
        self.assertEqual(again["events_removed"], 0)


class EventCascadeTests(unittest.TestCase):
    """Deleting an event has to take its children with it.

    Only ``event_elements`` and ``event_claims`` declare the foreign key, and
    this layer does not run SQLite with cascades on, so the deletion is written
    out by hand — which means it can fall behind the schema.  It already had:
    removing 85 events left 85 rows in ``event_timeline``, 85 in
    ``article_sentiment_labels`` and 15 in ``claim_verdicts`` pointing at
    nothing.  ``normalize.run`` carried a sweep for ``event_elements`` alone —
    one table's worth of the same bug, patched where it was noticed rather than
    where it came from.
    """

    def test_the_cascade_list_covers_every_table_keyed_on_an_event(self):
        # The guard against the list going stale.  A table added later with an
        # `event_id` column must be classified deliberately — as a child, or as
        # a different namespace — rather than left to dangle.
        if not STATE_DB.is_file():
            # A source-only checkout has no copyrighted corpus. Use the frozen
            # field inventory captured from the deployed schema, not an empty
            # newly created database that would make this check vacuous.
            inventory=Path(__file__).resolve().parents[1]/'docs/submission/evidence/data-inventory.json'
            schema=json.loads(inventory.read_text())['collector']['tables']
            keyed=[name for name,table in schema.items()
                   if any(field['name']=='event_id' for field in table['fields'])]
        else:
            keyed=None
        db = sqlite3.connect(f'file:{STATE_DB}?mode=ro',uri=True) if keyed is None else sqlite3.connect(':memory:')
        try:
            tables = [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            keyed = [] if keyed is None else keyed
            for table in tables:
                columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
                if "event_id" in columns:
                    keyed.append(table)
        finally:
            db.close()
        accounted = (set(EVENT_DEPENDENTS) | set(FOREIGN_EVENT_ID_TABLES)
                     | set(HUMAN_RECORD_TABLES) | {"event_records", "event_claims"})
        self.assertEqual(set(keyed) - accounted, set(),
                         "a table keyed on event_id is not declared as a child, a foreign "
                         "namespace, or a human record that outlives its event")

    def test_a_human_judgement_is_not_cascaded_away_with_its_event(self):
        # An issue reassignment is a person's work about an event, not a part of
        # it — the same rule review decisions follow. Deleting it with the event
        # would destroy the only record of why the ledger once looked different.
        self.assertIn("issue_adjustments", HUMAN_RECORD_TABLES)
        self.assertNotIn("issue_adjustments", EVENT_DEPENDENTS)

    def test_the_fda_recall_table_is_not_treated_as_a_child(self):
        # Its `event_id` is the FDA's recall number. Cascading into it would
        # delete an unrelated regulator's record on an id collision.
        self.assertNotIn("openfda_enforcement_items", EVENT_DEPENDENTS)

    def test_children_go_even_when_their_table_arrived_after_the_pipeline(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        db.execute("CREATE TABLE event_timeline(event_id TEXT, note TEXT)")
        db.execute("INSERT INTO event_timeline VALUES('e1','x')")
        db.execute("""INSERT INTO event_records(event_id,version_id,run_id,company_id,
            event_types_json,issue_codes_json,production_mode,event_subjects_json,
            affected_objects_json,impact_direction,impact_magnitude,article_sentiment,
            relevance_score,eventhood,summary,amounts_json,ratios_json,persons_json,
            organizations_json,products_json,risk_score,risk_level,recommended_action,
            label_source,extraction_confidence,needs_review,review_reasons_json,
            raw_label_json,created_at,updated_at)
            VALUES('e1','v1','r','c','[]','[]','BRIEF','[]','[]','neutral','minor','neutral',
                   0.9,'EVENT','s','[]','[]','[]','[]','[]',10,'low','monitor','ai',
                   0.9,0,'[]','{}','now','now')""")
        removed = delete_event(db, "e1")
        self.assertEqual(removed["event_timeline"], 1)
        self.assertEqual(db.execute("SELECT count(*) FROM event_timeline").fetchone()[0], 0)

    def test_a_table_that_does_not_exist_yet_is_a_state_not_a_crash(self):
        # A fresh install has run the pipeline and nothing else.
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        self.assertNotIn("event_timeline", delete_event(db, "absent"))


class IssueAdjustmentTests(unittest.TestCase):
    """M4-6 — 「归并错了要能救」, and the救 has to survive the rebuild.

    ``rebuild_issue_ledger`` deletes the ledger and builds it again from
    ``event_records`` on every run, and ``upsert_event`` overwrites
    ``issue_codes_json`` on every relabel.  A correction stored in either would
    live until the next pipeline run and then vanish without trace — the same
    shape as the eligibility floor a model verdict used to overwrite.  These
    hold that it survives, and that an issue assembled by hand can be told
    apart from one the pipeline produced.
    """

    def _db(self, adjustments=()):
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE document_versions(version_id TEXT PRIMARY KEY, source_id TEXT,
                      source_tier TEXT, company_id TEXT, canonical_url TEXT, content_hash TEXT,
                      raw_path TEXT, published_at TEXT)""")
        db.execute("""CREATE TABLE duplicate_clusters(cluster_id TEXT, representative_version_id TEXT)""")
        db.execute("""CREATE TABLE duplicate_cluster_members(cluster_id TEXT, version_id TEXT)""")
        ensure_schema(db)
        db.execute("""CREATE TABLE IF NOT EXISTS issue_adjustments(
            adjustment_id TEXT PRIMARY KEY, company_id TEXT, event_id TEXT, from_code TEXT,
            to_code TEXT, reason TEXT, actor TEXT, role TEXT, decided_at TEXT,
            undone_at TEXT, undone_by TEXT)""")
        for index, codes in enumerate(('["FUND_RAISE"]', '["FUND_RAISE"]', '["TECH_LEVEL"]'), start=1):
            version = f"v{index}"
            db.execute("""INSERT INTO document_versions(version_id,source_id,source_tier,company_id,
                          published_at) VALUES(?,?,?,?,?)""",
                       (version, f"s{index}", "S2", "c", f"2026-08-0{index}"))
            db.execute("""INSERT INTO event_records(event_id,version_id,run_id,company_id,
                event_types_json,issue_codes_json,production_mode,event_subjects_json,
                affected_objects_json,impact_direction,impact_magnitude,article_sentiment,
                relevance_score,eventhood,summary,amounts_json,ratios_json,persons_json,
                organizations_json,products_json,risk_score,risk_level,recommended_action,
                label_source,extraction_confidence,needs_review,review_reasons_json,
                raw_label_json,created_at,updated_at)
                VALUES(?,?,'r','c','[]',?,'BRIEF','[]','[]','neutral','minor','neutral',
                       0.9,'EVENT',?, '[]','[]','[]','[]','[]',50,'medium','monitor','ai',
                       0.9,0,'[]','{}','now','now')""",
                       (f"e{index}", version, codes, f"事件 {index}"))
        for event_id, from_code, to_code, undone in adjustments:
            db.execute("""INSERT INTO issue_adjustments(adjustment_id,company_id,event_id,from_code,
                to_code,reason,actor,role,decided_at,undone_at)
                VALUES(?,'c',?,?,?,'因为',?,'reviewer','2026-09-05T00:00:00+00:00',?)""",
                       (f"a-{event_id}-{from_code}", event_id, from_code, to_code, "someone", undone))
        db.commit()
        return db

    def counts(self, db):
        return {row[0]: (row[1], row[2]) for row in db.execute(
            "SELECT issue_code, event_count, manual_event_count FROM issue_ledger")}

    def test_without_corrections_the_ledger_is_what_the_labels_say(self):
        db = self._db()
        rebuild_issue_ledger(db)
        self.assertEqual(self.counts(db), {"FUND_RAISE": (2, 0), "TECH_LEVEL": (1, 0)})

    def test_moving_every_event_merges_one_issue_into_another(self):
        db = self._db([("e1", "FUND_RAISE", "TECH_LEVEL", None),
                       ("e2", "FUND_RAISE", "TECH_LEVEL", None)])
        rebuild_issue_ledger(db)
        # The emptied issue is gone rather than left at zero: an issue with no
        # events is not an issue.
        self.assertEqual(self.counts(db), {"TECH_LEVEL": (3, 2)})

    def test_moving_some_events_splits_an_issue(self):
        db = self._db([("e1", "FUND_RAISE", "TECH_LEVEL", None)])
        rebuild_issue_ledger(db)
        self.assertEqual(self.counts(db), {"FUND_RAISE": (1, 0), "TECH_LEVEL": (2, 1)})

    def test_a_correction_survives_a_rebuild_which_is_the_whole_point(self):
        db = self._db([("e1", "FUND_RAISE", "TECH_LEVEL", None)])
        for _ in range(3):
            rebuild_issue_ledger(db)
        self.assertEqual(self.counts(db)["TECH_LEVEL"], (2, 1))

    def test_an_event_can_be_taken_out_of_the_ledger_entirely(self):
        # The honest answer when a code was applied to something that is not an
        # issue at all. It leaves the ledger; it does not become a new issue.
        db = self._db([("e3", "TECH_LEVEL", None, None)])
        rebuild_issue_ledger(db)
        self.assertEqual(self.counts(db), {"FUND_RAISE": (2, 0)})

    def test_an_undone_correction_stops_applying(self):
        db = self._db([("e1", "FUND_RAISE", "TECH_LEVEL", "2026-09-05T01:00:00+00:00")])
        rebuild_issue_ledger(db)
        self.assertEqual(self.counts(db), {"FUND_RAISE": (2, 0), "TECH_LEVEL": (1, 0)})

    def test_the_hand_placed_count_names_only_what_was_moved(self):
        # An issue the pipeline assembled and one a reviewer repaired are
        # identical in a bare count, which is the thing worth being able to see.
        db = self._db([("e1", "FUND_RAISE", "TECH_LEVEL", None)])
        rebuild_issue_ledger(db)
        self.assertEqual(self.counts(db)["TECH_LEVEL"], (2, 1))
        self.assertEqual(self.counts(db)["FUND_RAISE"], (1, 0))

    def test_a_database_that_has_never_been_corrected_needs_no_such_table(self):
        db = self._db()
        db.execute("DROP TABLE issue_adjustments")
        db.commit()
        rebuild_issue_ledger(db)
        self.assertEqual(self.counts(db), {"FUND_RAISE": (2, 0), "TECH_LEVEL": (1, 0)})

    def test_two_codes_reassigned_to_the_same_target_count_the_event_once(self):
        db = sqlite3.connect(":memory:")
        db.close()
        db = self._db()
        db.execute("""UPDATE event_records SET issue_codes_json='["FUND_RAISE","EXPORT_CTRL"]'
                      WHERE event_id='e1'""")
        db.execute("""INSERT INTO issue_adjustments(adjustment_id,company_id,event_id,from_code,
            to_code,reason,actor,role,decided_at) VALUES
            ('x1','c','e1','FUND_RAISE','TECH_LEVEL','因为','a','reviewer','2026-09-05T00:00:00+00:00'),
            ('x2','c','e1','EXPORT_CTRL','TECH_LEVEL','因为','a','reviewer','2026-09-05T00:00:00+00:00')""")
        db.commit()
        rebuild_issue_ledger(db)
        # One event, two codes, both moved to the same place: it is in that
        # issue once, not twice.
        self.assertEqual(self.counts(db)["TECH_LEVEL"][0], 2)
