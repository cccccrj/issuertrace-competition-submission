"""「无法核验」的子类：规则按顺序判、每条都说得出凭什么、全库重算幂等。

钉的是这几条性质：

* 出自权威披露的主张先判——它本身就是证据，不管它说的是什么；
* 行情数据先于标记词：事件类型层已经判过的，不必再靠关键词猜；
* 标记词只收市场与第三方的说法，公司自己会披露的动作（回购、分红）不算「范围之外」；
* 重算幂等，被改判为别的裁决之后旧子类会被清掉；
* dry-run 一个字节都不写，连补列也不做。
"""
from __future__ import annotations

import sqlite3
import unittest

from processor import unverifiable
from tests.test_api import ApiTestCase


def row(**overrides):
    base = {"source_tier": "S2", "event_types_json": '["CAPITAL"]', "text": "某主张",
            "method": "llm_adjudicated"}
    return {**base, **overrides}


class ClassifyTests(unittest.TestCase):
    def test_a_claim_from_an_authoritative_filing_is_the_evidence_itself(self):
        # 哪怕它说的是股价，也是先判来源：出自公告的主张不是核验对象。
        self.assertEqual(unverifiable.classify(row(source_tier="S1", text="收盘价 49 元"))[0],
                         "primary_disclosure")
        self.assertEqual(unverifiable.classify(row(source_tier="S0"))[0], "primary_disclosure")

    def test_market_data_is_read_from_the_event_type_layer(self):
        subtype, basis = unverifiable.classify(row(event_types_json="[]", text="成交额 200 亿元"))
        self.assertEqual(subtype, "market_data")
        self.assertIn("行情", basis)

    def test_markers_say_which_word_hit(self):
        subtype, basis = unverifiable.classify(row(text="被列入美国国防部 1260H 清单"))
        self.assertEqual(subtype, "outside_disclosure")
        self.assertIn("「美国」", basis)

    def test_english_press_is_caught_by_the_same_groups(self):
        for text in ("CXMT is now worth more, with a market cap of $524 billion",
                     "WuXi AppTec faces new concerns over the BIOSECURE Act",
                     "The Pentagon added the company to its list"):
            self.assertEqual(unverifiable.classify(row(text=text))[0], "outside_disclosure", text)
        # 整词匹配：USD 里的 US、Congressman 之外的词不误伤。
        self.assertNotEqual(unverifiable.classify(row(text="Revenue of USD 1.2 billion"))[0],
                            "outside_disclosure")

    def test_what_a_company_itself_discloses_is_not_outside_the_disclosure(self):
        # 回购、分红、募资是公司自己会公告的事——核验不到是可以改进的，不是设计边界。
        for text in ("公司累计回购股份 1,843 万股", "拟每 10 股派发现金红利 9.8 元", "拟募资 295 亿元"):
            self.assertNotEqual(unverifiable.classify(row(text=text))[0], "outside_disclosure", text)

    def test_the_improvable_remainder_is_split_by_how_the_verdict_was_reached(self):
        self.assertEqual(unverifiable.classify(row(method="no_evidence"))[0], "not_found")
        self.assertEqual(unverifiable.classify(row(method="no_corpus"))[0], "no_corpus")
        self.assertEqual(unverifiable.classify(row())[0], "not_specific")

    def test_every_subtype_belongs_to_a_named_group(self):
        for code, meta in unverifiable.SUBTYPES.items():
            self.assertIn(meta["group"], unverifiable.GROUPS, code)
            self.assertTrue(meta["label"] and meta["detail"], code)


def fixture() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE document_versions(version_id TEXT PRIMARY KEY, source_tier TEXT);
        CREATE TABLE event_records(event_id TEXT PRIMARY KEY, version_id TEXT, event_types_json TEXT);
        CREATE TABLE event_claims(claim_id TEXT PRIMARY KEY, event_id TEXT, text TEXT);
        INSERT INTO document_versions VALUES('v1','S1'),('v2','S2');
        INSERT INTO event_records VALUES('e1','v1','["CAPITAL"]'),('e2','v2','[]'),('e3','v2','["CAPITAL"]');
        INSERT INTO event_claims VALUES('c1','e1','公告原文'),('c2','e2','成交额'),('c3','e3','拟募资 295 亿元');
    """)
    from processor.claim_verdict import ensure_schema
    ensure_schema(db)
    for claim_id, verdict, method in (("c1", "UNVERIFIABLE", "llm_adjudicated"),
                                      ("c2", "UNVERIFIABLE", "llm_adjudicated"),
                                      ("c3", "UNVERIFIABLE", "no_evidence")):
        db.execute("""INSERT INTO claim_verdicts(claim_id,company_id,verdict,method,adjudicated_at)
                      VALUES(?,?,?,?,?)""", (claim_id, "cxmt", verdict, method, "2026-09-23"))
    db.commit()
    return db


class BackfillTests(unittest.TestCase):
    def stored(self, db):
        return dict(db.execute("SELECT claim_id, unverifiable_subtype FROM claim_verdicts"))

    def test_dry_run_writes_nothing_not_even_the_columns(self):
        db = fixture()
        result = unverifiable.backfill(db, dry_run=True)
        self.assertEqual(result["subtypes"], {"primary_disclosure": 1, "market_data": 1, "not_found": 1})
        columns = {row[1] for row in db.execute("PRAGMA table_info(claim_verdicts)")}
        self.assertNotIn("unverifiable_subtype", columns)

    def test_backfill_stores_subtype_and_basis_and_is_idempotent(self):
        db = fixture()
        first = unverifiable.backfill(db)
        self.assertEqual(first["changed"], 3)
        self.assertEqual(first["groups"], {"boundary": 2, "improvable": 1})
        self.assertEqual(self.stored(db), {"c1": "primary_disclosure", "c2": "market_data", "c3": "not_found"})
        self.assertTrue(all(basis for (basis,) in db.execute("SELECT unverifiable_basis FROM claim_verdicts")))
        self.assertEqual(unverifiable.backfill(db)["changed"], 0)

    def test_a_claim_later_verified_loses_its_old_subtype(self):
        db = fixture()
        unverifiable.backfill(db)
        db.execute("UPDATE claim_verdicts SET verdict='SUPPORTED' WHERE claim_id='c3'")
        result = unverifiable.backfill(db)
        self.assertEqual(result["cleared"], 1)
        self.assertIsNone(self.stored(db)["c3"])




class ApiTests(ApiTestCase):
    """接口一侧：库还没拆分时照实说「未拆分」，拆分之后能按子类与分组筛。"""

    def backfill(self):
        db = sqlite3.connect(self.fixture_path)
        try:
            return unverifiable.backfill(db)
        finally:
            db.close()

    def test_before_the_backfill_nothing_pretends_to_be_split(self):
        stats = self.client.get("/api/claims/stats").json()
        self.assertFalse(stats["unverifiable"]["available"])
        # 筛不了就一条都没有，而不是「筛不了，于是全给你」。
        body = self.client.get("/api/claims", params={"unverifiable_group": "boundary"}).json()
        self.assertEqual(body["total"], 0)
        self.assertIsNone(self.client.get("/api/claims/c2").json()["unverifiable_subtype"])

    def test_after_the_backfill_each_claim_says_which_kind_and_why(self):
        self.backfill()
        claim = self.client.get("/api/claims/c2").json()
        self.assertEqual(claim["verdict"], "UNVERIFIABLE")
        self.assertIn(claim["unverifiable_subtype"], unverifiable.SUBTYPES)
        self.assertTrue(claim["unverifiable_label"] and claim["unverifiable_basis"])
        self.assertIn(claim["unverifiable_group"], unverifiable.GROUPS)
        # 已核实的主张不挂无法核验子类。
        self.assertIsNone(self.client.get("/api/claims/c1").json()["unverifiable_subtype"])

    def test_filters_and_stats_agree(self):
        self.backfill()
        stats = self.client.get("/api/claims/stats").json()["unverifiable"]
        self.assertTrue(stats["available"])
        for group in stats["groups"]:
            listed = self.client.get("/api/claims", params={"unverifiable_group": group["code"]}).json()
            self.assertEqual(listed["total"], group["count"], group["code"])
        for row in stats["subtypes"]:
            listed = self.client.get("/api/claims", params={"unverifiable_subtype": row["code"]}).json()
            self.assertEqual(listed["total"], row["count"], row["code"])

    def test_unknown_subtypes_and_groups_are_refused(self):
        self.assertEqual(self.client.get("/api/claims", params={"unverifiable_subtype": "vibes"}).status_code, 400)
        self.assertEqual(self.client.get("/api/claims", params={"unverifiable_group": "vibes"}).status_code, 400)

    def test_the_overview_card_carries_the_same_split(self):
        self.backfill()
        verify = next(task for task in self.client.get("/api/overview/pipeline").json()["tasks"]
                      if task["key"] == "verify")
        stats = self.client.get("/api/claims/stats").json()["unverifiable"]
        self.assertEqual(verify["unverifiable"]["groups"], stats["groups"])


if __name__ == "__main__":
    unittest.main()
