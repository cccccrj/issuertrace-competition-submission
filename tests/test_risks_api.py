"""风险事项库与研判档案 —— 预警加上「谁在管、管到哪一步了」。

这一层不新建聚合：一条风险事项就是一条 alert。它只做 ``/api/alerts`` 没做的事，
把人写进来的处置状态接上去。因此这里钉的都是「接上去之后仍然要成立」的性质：

* 没有人判过的事项是**待研判**，不是状态为空——两者在列表里长得一样，含义完全不同；
* 三个预置视图只是几组筛选的简写，不是三条各自实现一遍的查询；
* 研判档案一次把六段都给出来，而每条核验结论都指得出原文第几页；
* 合法去向由服务端算，界面不自己再实现一遍流程。
"""
from __future__ import annotations

import unittest

from tests.test_notify_api import NotifyTestCase


class ListTests(NotifyTestCase):
    def test_an_item_nobody_has_touched_is_pending_rather_than_blank(self):
        body = self.client.get("/api/risks").json()
        self.assertTrue(body["items"])
        first = body["items"][0]
        self.assertEqual(first["disposition"]["status"], "pending")
        self.assertEqual(first["disposition"]["status_label"], "待研判")

    def test_a_row_carries_what_the_list_promises_to_show(self):
        # 规格 §4.3 的十列。缺一列就要么留白、要么让读者回到详情页去找。
        row = self.client.get("/api/risks").json()["items"][0]
        for field in ("risk_level", "headline", "company_id", "issue_code", "credibility",
                      "latest_progress", "heat", "disposition"):
            self.assertIn(field, row, field)
        self.assertIn("event_count", row["heat"])
        self.assertIn("independent_sources", row["heat"])

    def test_the_issue_code_arrives_with_the_word_a_reader_knows(self):
        row = self.client.get("/api/risks").json()["items"][0]
        if row["issue_code"]:
            self.assertTrue(row["issue_label"], "议题码没有中文名，页面上会露出枚举")

    def test_an_unknown_status_is_refused_rather_than_silently_ignored(self):
        # 静默忽略会返回全量列表，而调用方以为自己筛过了。
        response = self.client.get("/api/risks", params={"status": "shredded"})
        self.assertEqual(response.status_code, 400)

    def test_the_archived_view_is_empty_until_something_is_archived(self):
        self.assertEqual(self.client.get("/api/risks", params={"view": "archived"}).json()["total"], 0)

    def test_the_preset_view_is_the_same_query_as_the_filter(self):
        # 视图是筛选的简写。如果两者会给出不同结果，侧栏那三个入口就是三条新查询。
        self.become("chen", "reviewer")
        alert_id = self.client.get("/api/risks").json()["items"][0]["alert_id"]
        self.client.post(f"/api/notify/dispositions/{alert_id}",
                         json={"chosen": "cold", "status": "archived"})
        by_view = self.client.get("/api/risks", params={"view": "archived"}).json()
        by_filter = self.client.get("/api/risks", params={"status": "archived"}).json()
        self.assertEqual(by_view["total"], 1)
        self.assertEqual([row["alert_id"] for row in by_view["items"]],
                         [row["alert_id"] for row in by_filter["items"]])

    def test_pending_items_sort_ahead_of_handled_ones(self):
        # 已经有人在管的那条不该再占着「今天先看哪条」的第一行。
        listing = self.client.get("/api/risks").json()["items"]
        if len(listing) < 2:
            self.skipTest("夹具里只有一条事项")
        self.become("wei", "analyst")
        self.client.post(f"/api/notify/dispositions/{listing[0]['alert_id']}",
                         json={"chosen": "cold", "status": "investigating"})
        again = self.client.get("/api/risks").json()["items"]
        self.assertNotEqual(again[0]["alert_id"], listing[0]["alert_id"])


class SummaryTests(NotifyTestCase):
    def test_the_front_page_counts_what_a_person_has_to_do(self):
        body = self.client.get("/api/risks/summary").json()
        for key in ("pending", "high_open", "investigating", "reporting"):
            self.assertIn(key, body["indicators"], key)

    def test_the_front_page_does_not_report_back_office_volumes(self):
        # 规格 §3.5：首屏不出现文档总数、主张总数、标准化率。它们没被删，搬到了
        # 信息运营的数据质量页——首屏回答的是「今天该先处理哪几条」。
        body = self.client.get("/api/risks/summary").json()
        for absent in ("documents", "claims", "normalisation", "evidence_paragraphs"):
            self.assertNotIn(absent, body, absent)

    def test_every_anomaly_class_can_actually_be_counted(self):
        # 一个数不出来的分类在页面上只会是个空壳。
        for row in self.client.get("/api/risks/summary").json()["anomalies"]:
            self.assertIsInstance(row["count"], int)
            self.assertTrue(row["label"] and row["detail"])


    def test_each_anomaly_count_is_the_length_of_the_list_it_opens(self):
        # 首页四张异常卡片各自点进事项库；点之前的数必须等于点之后的条数。此前首页数四类、
        # 事项库只筛三类（漏了「仅转述或简讯来源」），两边对不上。
        body = self.client.get("/api/risks/summary").json()
        for row in body["anomalies"]:
            listed = self.client.get("/api/risks", params={"anomaly": row["key"]}).json()["total"]
            self.assertEqual(listed, row["count"], row["key"])
        union = self.client.get("/api/risks", params={"view": "anomaly"}).json()["total"]
        self.assertEqual(union, body["indicators"]["anomaly"])
        self.assertGreater(union, 0)

    def test_an_archived_item_is_no_longer_waiting_to_be_checked(self):
        before = self.client.get("/api/risks/summary").json()["indicators"]["anomaly"]
        self.become("chen", "reviewer")
        for alert_id in ("a1", "a2"):
            self.client.post(f"/api/notify/dispositions/{alert_id}", json={"status": "archived"})
        after = self.client.get("/api/risks/summary").json()["indicators"]["anomaly"]
        self.assertGreater(before, 0)
        self.assertEqual(after, 0)

    def test_an_unknown_anomaly_class_is_refused(self):
        self.assertEqual(self.client.get("/api/risks", params={"anomaly": "vibes"}).status_code, 400)

    def test_the_handling_view_holds_the_three_middle_steps(self):
        self.become("wei", "analyst")
        self.client.post("/api/notify/dispositions/a1", json={"status": "investigating"})
        items = self.client.get("/api/risks", params={"view": "handling"}).json()["items"]
        self.assertEqual([item["alert_id"] for item in items], ["a1"])

    def test_the_front_page_counts_what_is_mine(self):
        self.become("wei", "analyst")
        self.client.post("/api/notify/dispositions/a1", json={"status": "pending", "owner": "wei"})
        indicators = self.client.get("/api/risks/summary").json()["indicators"]
        self.assertEqual((indicators["mine_pending"], indicators["my_tasks"]), (1, 1))


class DossierTests(NotifyTestCase):
    def dossier(self):
        alert_id = self.client.get("/api/risks").json()["items"][0]["alert_id"]
        return self.client.get(f"/api/risks/{alert_id}").json()

    def test_the_six_sections_arrive_in_one_response(self):
        # 六个往返意味着六次转圈和六个各自可能失败的加载态。
        body = self.dossier()
        for section in ("evidence", "sources", "propagation", "changes", "history",
                        "transitions"):
            self.assertIn(section, body, section)

    def test_a_missing_item_is_a_404_not_an_empty_dossier(self):
        self.assertEqual(self.client.get("/api/risks/nope").status_code, 404)

    def test_every_verdict_that_has_evidence_can_be_traced_to_a_page(self):
        # 「非『无法核验』的裁决都指向某份文件的第几页；没有页码的结论不会被写入」——
        # 实测库里 608 条这样的条目，无页码的 0 条。
        for item in self.dossier()["evidence"]:
            if item["verdict"] == "UNVERIFIABLE":
                continue
            self.assertTrue(item.get("evidence_page"),
                            "非「无法核验」的结论必须能定位到某一页")

    def test_the_legal_next_steps_come_from_the_server(self):
        body = self.dossier()
        targets = {step["status"] for step in body["transitions"]}
        self.assertEqual(targets, {"investigating", "reporting", "tracking", "archived"})
        archive = next(step for step in body["transitions"] if step["status"] == "archived")
        self.assertEqual(archive["requires"], "reviewer")

    def test_a_terminal_item_offers_no_next_step(self):
        self.become("chen", "reviewer")
        alert_id = self.client.get("/api/risks").json()["items"][0]["alert_id"]
        self.client.post(f"/api/notify/dispositions/{alert_id}",
                         json={"chosen": "cold", "status": "archived"})
        self.assertEqual(self.client.get(f"/api/risks/{alert_id}").json()["transitions"], [])

    def test_sources_are_grouped_by_the_role_they_play(self):
        # 权威披露与独立报道的区别不是可信度高低，是它们在研判里承担的角色不同：
        # 一个建立事实，一个提供线索。
        for item in self.dossier()["sources"]:
            self.assertIn(item["group"], {"authoritative", "independent", "relay"})

    def test_the_spread_timeline_reads_its_origin_rather_than_guessing(self):
        nodes = self.dossier()["propagation"]["nodes"]
        for node in nodes:
            self.assertTrue(node["action"])

    def test_handling_a_step_shows_up_in_the_dossier_history(self):
        self.become("wei", "analyst")
        alert_id = self.client.get("/api/risks").json()["items"][0]["alert_id"]
        self.client.post(f"/api/notify/dispositions/{alert_id}",
                         json={"chosen": "formal_reply", "status": "investigating",
                               "owner": "wei", "note": "等公司回复"})
        body = self.client.get(f"/api/risks/{alert_id}").json()
        self.assertEqual(body["disposition"]["status"], "investigating")
        self.assertEqual(body["disposition"]["owner"], "wei")
        self.assertEqual(len(body["history"]), 1)
        self.assertEqual(body["history"][0]["to_label"], "核查中")



class InvestigationTests(NotifyTestCase):
    """专项核查工作视图：谁在查什么、查到哪一步、什么时候到期。

    不建第二套任务实体，全部挂在处置记录上，只多一个截止时间。钉的是清单第 3 项的验收：
    逾期的排在前面、没有截止时间的不算逾期。
    """

    def setUp(self):
        super().setUp()
        self.become("chen", "analyst")

    def start(self, alert_id, **extra):
        response = self.client.post(f"/api/notify/dispositions/{alert_id}",
                                    json={"chosen": "cold", "status": "investigating", **extra})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def board(self, **params):
        return self.client.get("/api/risks/investigations", params=params).json()

    def test_a_deadline_is_stored_and_shown_where_the_item_is_shown(self):
        self.start("a1", due_at="2999-12-31", owner="chen")
        row = next(r for r in self.client.get("/api/risks").json()["items"] if r["alert_id"] == "a1")
        self.assertEqual(row["disposition"]["due_at"], "2999-12-31")
        self.assertEqual(self.client.get("/api/risks/a1").json()["disposition"]["due_at"], "2999-12-31")

    def test_changing_only_the_status_keeps_the_deadline(self):
        # 否则每一次只改状态的提交，都会把之前定好的期限悄悄抹掉。
        self.start("a1", due_at="2999-12-31")
        moved = self.client.post("/api/notify/dispositions/a1", json={"status": "tracking"})
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(self.client.get("/api/risks/a1").json()["disposition"]["due_at"], "2999-12-31")

    def test_an_explicit_null_clears_the_deadline(self):
        self.start("a1", due_at="2999-12-31")
        self.start("a1", due_at=None)
        self.assertIsNone(self.client.get("/api/risks/a1").json()["disposition"]["due_at"])

    def test_a_malformed_deadline_is_refused(self):
        response = self.client.post("/api/notify/dispositions/a1",
                                    json={"chosen": "cold", "status": "investigating",
                                          "due_at": "next friday"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("YYYY-MM-DD", response.json()["detail"])

    def test_overdue_items_come_first(self):
        self.start("a2", due_at="2999-12-31")
        self.start("a1", due_at="2020-01-01")
        board = self.board()
        self.assertEqual([i["alert_id"] for i in board["items"]], ["a1", "a2"])
        self.assertTrue(board["items"][0]["disposition"]["overdue"])
        self.assertEqual(board["summary"]["overdue"], 1)

    def test_no_deadline_is_not_overdue_and_sorts_after_a_deadline(self):
        # 把「没定期限」也算进逾期，逾期数就成了「没人定过期限」的数。
        self.start("a1")
        self.start("a2", due_at="2999-12-31")
        board = self.board()
        self.assertEqual([i["alert_id"] for i in board["items"]], ["a2", "a1"])
        self.assertEqual(board["summary"]["overdue"], 0)
        self.assertEqual(board["summary"]["no_due"], 1)

    def test_due_today_is_not_yet_overdue(self):
        from api.routes.risks import today
        self.start("a1", due_at=today())
        self.assertFalse(self.board()["items"][0]["disposition"]["overdue"])

    def test_only_items_under_investigation_are_on_the_board(self):
        # 待研判的还没开始查，已归档的查完了——都不在「谁在查什么」里。
        self.assertEqual(self.board()["items"], [])
        self.start("a1")
        self.assertEqual([i["alert_id"] for i in self.board()["items"]], ["a1"])
        self.become("lee", "reviewer")
        self.client.post("/api/notify/dispositions/a1", json={"chosen": "cold", "status": "archived"})
        self.assertEqual(self.board()["items"], [])

    def test_the_board_says_who_is_holding_what(self):
        self.start("a1", owner="chen", due_at="2020-01-01")
        self.start("a2", owner="wang")
        owners = {o["owner"]: o for o in self.board()["summary"]["by_owner"]}
        self.assertEqual((owners["chen"]["count"], owners["chen"]["overdue"]), (1, 1))
        self.assertEqual(owners["wang"]["overdue"], 0)
        self.assertEqual(self.board(owner="wang")["summary"]["total"], 1)

    def test_a_deadline_change_is_in_the_trail(self):
        # 「谁把期限推后了」与「谁改了状态」是同一类事实。
        self.start("a1", due_at="2999-12-31")
        events = self.client.get("/api/audit/dispositions", params={"alert_id": "a1"}).json()["items"]
        self.assertEqual(events[0]["due_at"], "2999-12-31")

if __name__ == "__main__":
    unittest.main()
