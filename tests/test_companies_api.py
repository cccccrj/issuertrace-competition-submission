"""公司档案：一家公司现在有哪些事、风险往哪走、最近正式说了什么、在哪几个专题里、跟谁有关。

钉的是清单第 4 项的验收：换一个 company_id，返回的是另一家的数据；以及「加第三家公司不改
代码」——公司是谁、叫什么只从配置来。测试夹具里写公司标识没问题，产品代码里不行。
"""
from __future__ import annotations

import unittest

from tests.test_notify_api import NotifyTestCase


class CompanyDossierTests(NotifyTestCase):
    def dossier(self, company_id):
        response = self.client.get(f"/api/companies/{company_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_another_company_id_returns_another_companys_data(self):
        a, b = self.dossier("cxmt"), self.dossier("wuxi_apptec")
        self.assertNotEqual(a["name"], b["name"])
        self.assertEqual([i["alert_id"] for i in a["open"]["items"]], ["a1"])
        self.assertEqual([i["alert_id"] for i in b["open"]["items"]], ["a2"])
        self.assertTrue(all(i["company_id"] == "cxmt" for i in a["open"]["items"]))

    def test_the_name_comes_from_configuration(self):
        from processor.pipeline import company_display_name, load_config
        body = self.dossier("cxmt")
        self.assertEqual(body["name"], company_display_name("cxmt"))
        self.assertEqual(body["canonical_name"], load_config()["entities"]["cxmt"]["canonical_name"])

    def test_an_unknown_company_has_no_dossier(self):
        self.assertEqual(self.client.get("/api/companies/nobody").status_code, 404)

    def test_all_five_sections_are_there_even_when_empty(self):
        body = self.dossier("wuxi_apptec")
        for key in ("open", "trend", "disclosures", "issues", "related"):
            self.assertIn(key, body)
        self.assertIsInstance(body["related"], list)

    def test_open_items_leave_out_what_is_archived(self):
        self.become("chen", "reviewer")
        self.client.post("/api/notify/dispositions/a1", json={"chosen": "cold", "status": "archived"})
        self.assertEqual(self.dossier("cxmt")["open"]["total"], 0)

    def test_the_trend_is_counted_by_month_and_level(self):
        trend = self.dossier("cxmt")["trend"]
        self.assertEqual(trend, [{"month": "2026-08", "high": 1, "medium": 0, "low": 0}])

    def test_authoritative_disclosures_are_only_the_formal_sources(self):
        items = self.dossier("cxmt")["disclosures"]["items"]
        self.assertTrue(items)
        self.assertEqual({i["source_id"] for i in items}, {"sse"})


class RelatedOrganizationsTests(NotifyTestCase):
    """第一版取的是 `organization` 要素——原文写法、从没归一过。截图上公司自己以三种写法
    排在前三，中金公司与它的全称各占一行。归一只发生在事件主体（`subject`，unit=org_id）上。"""

    def setUp(self):
        super().setUp()
        import sqlite3
        db = sqlite3.connect(self.fixture_path)
        db.execute("""CREATE TABLE event_elements(element_id TEXT, event_id TEXT, element_type TEXT,
            ordinal INT, raw_text TEXT, normalized_value TEXT, unit TEXT, basis TEXT, status TEXT,
            extra_json TEXT)""")
        rows = [
            ("x1", "e1", "subject", 0, "中金公司", "cicc", "org_id", "organization_registry", "normalized"),
            ("x2", "e1", "subject", 1, "中国国际金融股份有限公司", "cicc", "org_id", "organization_registry", "normalized"),
            ("x3", "e1", "subject", 2, "长鑫存储", "cxmt", None, "alias", "normalized"),
            ("x4", "e1", "organization", 0, "长鑫科技", "长鑫科技", None, None, "normalized"),
        ]
        db.executemany("INSERT INTO event_elements VALUES(?,?,?,?,?,?,?,?,?,'{}')", rows)
        db.commit()
        db.close()

    def test_the_company_is_not_its_own_related_party_and_one_org_is_one_row(self):
        related = self.client.get("/api/companies/cxmt").json()["related"]
        self.assertEqual([r["org_id"] for r in related], ["cicc"])
        self.assertEqual(related[0]["events"], 1, "数的是出现在几个事件里，不是出现几次")
        self.assertEqual(related[0]["kind_label"], "券商")


class CompanyNamesTests(NotifyTestCase):
    def test_health_hands_the_frontend_the_configured_names(self):
        # 登录页在登录之前就要用，所以放在公开的健康检查里。
        from processor.pipeline import configured_companies
        self.client.cookies.clear()
        body = self.client.get("/api/health").json()
        self.assertEqual([c["company_id"] for c in body["companies"]], list(configured_companies()))
        self.assertTrue(all(c["name"] for c in body["companies"]))


if __name__ == "__main__":
    unittest.main()
