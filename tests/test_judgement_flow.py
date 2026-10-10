"""研判闭环：人工结论、人工定级、认领、上报给谁、退回给谁（修改思路 v2 §五—§七、§二十四）。

此前处置流程能记下「走到哪一步」，记不下两件事：这条风险**成不成立**，以及
**上报之后轮到谁**。这里钉的是补上之后必须成立的性质：

* 流程表可穷举：每个状态能去哪、哪几步要核查负责人；
* 上报前必须有结论，上报对象必须是有复核权的人；
* 事项在上级手里时，提交人改不了它；
* 退回必须写意见，负责人自动回到提交人，提交人打开就看得到那句意见；
* 人的定级与机器的并排存着，不覆盖机器的；
* 相似事项给依据，不给相似度小数；
* 旧库升上来，人写过的处置行原样不动。
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from api import dispatch_db, dispositions
from tests.test_notify_api import NotifyTestCase


class TransitionTableTests(unittest.TestCase):
    def test_every_target_is_a_known_status_and_archived_is_terminal(self):
        for current, targets in dispositions.TRANSITIONS.items():
            self.assertIn(current, dispositions.STATUSES)
            for target in targets:
                self.assertIn(target, dispositions.STATUSES, f"{current} → {target}")
        self.assertEqual(dispositions.TRANSITIONS["archived"], ())

    def test_every_status_is_reachable_from_pending(self):
        seen, frontier = {"pending"}, ["pending"]
        while frontier:
            for target in dispositions.TRANSITIONS[frontier.pop()]:
                if target not in seen:
                    seen.add(target)
                    frontier.append(target)
        self.assertEqual(seen, set(dispositions.STATUSES))

    def test_only_a_reviewer_can_return_and_only_from_review(self):
        # 退回是复核的一个出口：没上报过就没有可退回的东西。
        sources = {current for current, targets in dispositions.TRANSITIONS.items()
                   if "returned" in targets}
        self.assertEqual(sources, {"reporting"})

    def test_every_step_out_of_review_needs_a_reviewer(self):
        for step in dispositions.transitions_from("reporting"):
            self.assertEqual(step["requires"], "reviewer", step["status"])
        # 其余状态只有归档要核查负责人。
        for current in ("pending", "investigating", "returned", "tracking"):
            gated = {step["status"] for step in dispositions.transitions_from(current)
                     if step["requires"]}
            self.assertEqual(gated, {"archived"} & set(dispositions.TRANSITIONS[current]), current)

    def test_the_step_rules_say_what_is_missing(self):
        self.assertIn("人工结论", dispositions.step_problem("pending", "reporting",
                                                         conclusion=None, note=None))
        self.assertIsNone(dispositions.step_problem("pending", "reporting",
                                                    conclusion="confirmed", note=None))
        self.assertIn("退回意见", dispositions.step_problem("reporting", "returned",
                                                         conclusion="confirmed", note="  "))


class JudgementFlowTests(NotifyTestCase):
    def post(self, alert_id="a1", **body):
        return self.client.post(f"/api/notify/dispositions/{alert_id}", json=body)

    def dossier(self, alert_id="a1"):
        return self.client.get(f"/api/risks/{alert_id}").json()

    def setUp(self):
        super().setUp()
        # 先把两个人都建出来：上报对象要求是一个真实存在的核查负责人账号。
        self.become("chen", "reviewer")
        self.become("wei", "analyst")

    # ——— 认领与结论 ————————————————————————————————————————————

    def test_claiming_sets_the_owner_without_choosing_a_public_line(self):
        # 认领一条事项不等于替它选了对外口径：chosen 必须仍是空的，分歧也不成立。
        response = self.post(status="pending", owner="wei")
        self.assertEqual(response.status_code, 200, response.text)
        body = self.dossier()
        self.assertEqual(body["disposition"]["status"], "pending")
        self.assertEqual(body["disposition"]["owner"], "wei")
        self.assertIsNone(body["disposition"]["chosen"])
        self.assertFalse(body["disposition"]["diverged"])
        self.assertEqual([(step["from_status"], step["to_status"]) for step in body["history"]],
                         [("pending", "pending")])

    def test_a_human_level_sits_beside_the_machine_level_rather_than_replacing_it(self):
        self.post(status="pending", conclusion="partial", level_override="medium",
                  opinion="募资口径有变化，但已在问询回复中解释。")
        body = self.dossier()
        self.assertEqual(body["risk_level"], "high")                 # 机器的
        self.assertEqual(body["disposition"]["level_override"], "medium")   # 人的
        self.assertEqual(body["disposition"]["conclusion_label"], "部分成立")
        self.assertEqual(body["opinion"], "募资口径有变化，但已在问询回复中解释。")
        self.assertEqual(body["history"][0]["conclusion_label"], "部分成立")

    def test_fields_not_sent_are_left_alone(self):
        self.post(status="pending", conclusion="confirmed", opinion="依据 A")
        self.post(status="investigating", owner="wei")
        body = self.dossier()
        self.assertEqual(body["disposition"]["conclusion"], "confirmed")
        self.assertEqual(body["opinion"], "依据 A")

    def test_unknown_conclusions_and_levels_are_refused(self):
        self.assertEqual(self.post(status="pending", conclusion="maybe").status_code, 400)
        self.assertEqual(self.post(status="pending", level_override="extreme").status_code, 400)

    def test_marking_a_false_positive_is_visible_in_the_list(self):
        self.post(status="pending", conclusion="false_positive")
        row = next(item for item in self.client.get("/api/risks").json()["items"]
                   if item["alert_id"] == "a1")
        self.assertTrue(row["disposition"]["false_positive"])

    # ——— 上报 ————————————————————————————————————————————————

    def test_reporting_needs_a_conclusion(self):
        refused = self.post(status="reporting", owner="chen")
        self.assertEqual(refused.status_code, 422)
        self.assertIn("人工结论", refused.json()["detail"])

    def test_reporting_needs_someone_who_can_review(self):
        for owner in (None, "wei", "nobody"):
            refused = self.post(status="reporting", conclusion="confirmed", owner=owner)
            self.assertEqual(refused.status_code, 422, owner)
            self.assertIn("核查负责人", refused.json()["detail"])

    def test_reporting_hands_the_item_to_the_reviewer(self):
        accepted = self.post(status="reporting", conclusion="confirmed", owner="chen",
                             note="高风险且处于发行阶段，请确认是否对外澄清")
        self.assertEqual(accepted.status_code, 200, accepted.text)
        body = self.dossier()
        self.assertEqual(body["disposition"]["status_label"], "待上级复核")
        self.assertEqual(body["disposition"]["owner"], "chen")

    def test_once_reported_the_submitter_cannot_move_or_rejudge_it(self):
        self.post(status="reporting", conclusion="confirmed", owner="chen")
        self.assertEqual(self.post(status="tracking").status_code, 403)
        self.assertEqual(self.post(status="reporting", conclusion="false_positive").status_code, 403)

    # ——— 退回 ————————————————————————————————————————————————

    def test_returning_needs_a_note_and_goes_back_to_the_submitter(self):
        self.post(status="reporting", conclusion="insufficient", owner="chen")
        self.become("chen", "reviewer")
        refused = self.post(status="returned")
        self.assertEqual(refused.status_code, 422)
        self.assertIn("退回意见", refused.json()["detail"])

        returned = self.post(status="returned", note="缺公司正式回应，补一条权威出处再报")
        self.assertEqual(returned.status_code, 200, returned.text)
        body = self.dossier()
        self.assertEqual(body["disposition"]["status_label"], "退回补证")
        self.assertEqual(body["disposition"]["owner"], "wei")
        self.assertEqual(body["returned_by"]["actor"], "chen")
        self.assertEqual(body["returned_by"]["note"], "缺公司正式回应，补一条权威出处再报")

    def test_the_whole_loop_closes(self):
        # 分析师判 → 上报 → 上级退回 → 分析师补证后再报 → 上级归档。每一步都有下一个人。
        self.post(status="pending", owner="wei")
        self.post(status="reporting", conclusion="insufficient", owner="chen")
        self.become("chen", "reviewer")
        self.post(status="returned", note="补证")
        self.become("wei", "analyst")
        self.assertEqual(self.post(status="returned", conclusion="confirmed",
                                   opinion="公司已发澄清公告").status_code, 200)
        self.assertEqual(self.post(status="reporting", owner="chen").status_code, 200)
        self.become("chen", "reviewer")
        self.assertEqual(self.post(status="archived").status_code, 200)
        body = self.dossier()
        self.assertEqual(body["disposition"]["status"], "archived")
        self.assertEqual(body["disposition"]["conclusion"], "confirmed")
        self.assertIsNone(body["returned_by"])     # 已归档，不再显示退回意见
        self.assertEqual([step["to_status"] for step in reversed(body["history"])],
                         ["pending", "reporting", "returned", "returned", "reporting", "archived"])

    # ——— 相似事项与上报对象 ————————————————————————————————————————

    def test_similar_items_carry_their_basis_and_no_score(self):
        with sqlite3.connect(self.fixture_path) as db:
            db.row_factory = sqlite3.Row
            row = dict(db.execute("SELECT * FROM alerts WHERE alert_id='a1'").fetchone())
            row.update(alert_id="a3", company_id="wuxi_apptec", window_end="2026-07-01")
            columns = ",".join(row)
            db.execute(f"INSERT INTO alerts({columns}) VALUES({','.join('?' * len(row))})",
                       list(row.values()))
        similar = self.dossier()["similar"]
        self.assertEqual([item["alert_id"] for item in similar], ["a3"])
        self.assertIn("同一专题：募资", similar[0]["basis"])
        self.assertNotIn("score", similar[0])
        self.assertNotIn("similarity", similar[0])
        self.assertIn("status_label", similar[0]["disposition"])

    def test_the_reviewer_list_is_readable_by_an_analyst_and_holds_only_reviewers(self):
        names = {row["username"] for row in self.client.get("/api/users/reviewers").json()["items"]}
        self.assertIn("chen", names)
        self.assertNotIn("wei", names)
        self.become("looker", "viewer")
        self.assertEqual(self.client.get("/api/users/reviewers").status_code, 403)


class TaskAndHandoffTests(NotifyTestCase):
    """我的任务与交接通知（修改思路 v2 §七、§八、§二十四）：上报之后必须有下一个人，
    而且那个人**知道**轮到自己了。"""

    def setUp(self):
        super().setUp()
        import api.notify_db as notify_db
        import api.review_db as review_db
        self._paths = (notify_db.DB_PATH, review_db.DB_PATH)
        notify_db.DB_PATH = review_db.DB_PATH = self.fixture_path
        review_db.connect().close()
        self.addCleanup(self._restore)
        self.become("chen", "reviewer")
        self.become("wei", "analyst")

    def _restore(self):
        import api.notify_db as notify_db
        import api.review_db as review_db
        notify_db.DB_PATH, review_db.DB_PATH = self._paths

    def post(self, alert_id="a1", **body):
        response = self.client.post(f"/api/notify/dispositions/{alert_id}", json=body)
        self.assertEqual(response.status_code, 200, response.text)

    def tasks(self):
        return self.client.get("/api/risks/tasks").json()

    def handoffs(self):
        return [item for item in self.client.get("/api/notifications").json()["items"]
                if item["kind"] == "handoff"]

    def test_a_claimed_item_is_my_task_and_says_nobody_handed_it_over(self):
        self.post(status="pending", owner="wei")
        body = self.tasks()
        self.assertEqual([item["alert_id"] for item in body["items"]], ["a1"])
        self.assertEqual(body["items"][0]["task"]["label"], "待研判")
        self.assertIsNone(body["items"][0]["handed_by"])
        self.assertEqual(body["counts"]["pending"], 1)
        # a2 还没人认领，它在公共队列里。
        self.assertEqual(body["unclaimed"], 1)

    def test_a_report_lands_in_the_reviewers_tasks_with_who_and_why(self):
        self.post(status="reporting", conclusion="confirmed", owner="chen", note="请确认是否澄清")
        self.assertEqual(self.tasks()["items"], [])          # 已交出去，不再是我的
        self.become("chen", "reviewer")
        item = self.tasks()["items"][0]
        self.assertEqual((item["task"]["label"], item["task"]["action"]), ("上级复核", "查看并复核"))
        self.assertEqual((item["handed_by"]["actor"], item["handed_by"]["note"]), ("wei", "请确认是否澄清"))

    def test_returned_items_come_first(self):
        self.post(alert_id="a2", status="pending", owner="wei")
        self.post(status="reporting", conclusion="insufficient", owner="chen")
        self.become("chen", "reviewer")
        self.post(status="returned", note="补证")
        self.become("wei", "analyst")
        kinds = [item["task"]["kind"] for item in self.tasks()["items"]]
        self.assertEqual(kinds, ["returned", "pending"])

    def test_each_handoff_notifies_only_the_person_who_receives_it(self):
        self.post(status="reporting", conclusion="confirmed", owner="chen", note="请复核")
        self.assertEqual(self.handoffs(), [])                  # 提交人自己收不到
        self.become("chen", "reviewer")
        received = self.handoffs()
        self.assertEqual(len(received), 1)
        self.assertTrue(received[0]["title"].startswith("上报给你复核"))
        self.assertEqual(received[0]["link"], "/risks/a1")
        self.assertIn("请复核", received[0]["body"])

        self.post(status="returned", note="缺出处")
        self.become("wei", "analyst")
        titles = [item["title"] for item in self.handoffs()]
        self.assertEqual(len(titles), 1)
        self.assertTrue(titles[0].startswith("退回给你补证"))

    def test_claiming_for_yourself_is_not_a_handoff_and_syncing_twice_adds_nothing(self):
        self.post(status="pending", owner="wei")
        self.post(status="investigating", owner="chen")         # 分派给别人
        self.assertEqual(self.handoffs(), [])
        self.become("chen", "reviewer")
        self.assertEqual([item["title"][:4] for item in self.handoffs()], ["分派给你"])
        self.assertEqual(len(self.handoffs()), 1)

    def test_an_alert_notification_opens_the_item_rather_than_the_whole_library(self):
        items = self.client.get("/api/notifications").json()["items"]
        alert = next(item for item in items if item["kind"] == "alert")
        self.assertEqual(alert["link"], f"/risks/{alert['ref_id']}")

    def test_the_report_log_lists_who_handed_what_to_whom(self):
        # 上报与推送记录页的上半页：只取走进上报 / 退回的那几步，带着事项标题。
        self.post(status="pending", owner="wei")                 # 认领，不是上报
        self.post(status="reporting", conclusion="confirmed", owner="chen", note="请复核")
        self.become("chen", "reviewer")
        self.post(status="returned", note="补证")
        rows = self.client.get("/api/audit/dispositions",
                               params={"to_status": ["reporting", "returned"]}).json()["items"]
        self.assertEqual([(row["to_status"], row["actor"], row["owner"]) for row in rows],
                         [("returned", "chen", "wei"), ("reporting", "wei", "chen")])
        self.assertTrue(all(row["headline"] for row in rows))
        self.assertEqual(rows[1]["conclusion_label"], "风险成立")

    def test_marking_all_read_does_not_touch_other_peoples_handoffs(self):
        self.post(status="reporting", conclusion="confirmed", owner="chen")
        self.client.post("/api/notifications/read", json={})
        self.become("chen", "reviewer")
        self.assertEqual(sum(1 for item in self.handoffs() if item["read_at"] is None), 1)


class MigrationTests(unittest.TestCase):
    def test_an_old_database_gains_the_columns_and_keeps_every_human_row(self):
        with tempfile.TemporaryDirectory() as folder:
            db = sqlite3.connect(Path(folder) / "old.sqlite3")
            db.row_factory = sqlite3.Row
            db.execute("""CREATE TABLE alert_dispositions(
                disposition_id TEXT PRIMARY KEY, alert_id TEXT NOT NULL UNIQUE, company_id TEXT,
                recommended TEXT NOT NULL, recommended_basis_json TEXT NOT NULL,
                chosen TEXT, status TEXT NOT NULL DEFAULT 'pending',
                note TEXT, actor TEXT, role TEXT, decided_at TEXT, created_at TEXT NOT NULL)""")
            db.execute("""INSERT INTO alert_dispositions VALUES('d1','a1','cxmt','cold','[]',
                'escalate','done','已上报项目组','chen','reviewer','2026-09-01T00:00:00+00:00',
                '2026-09-01T00:00:00+00:00')""")
            db.commit()
            dispatch_db.ensure_schema(db)
            row = dict(db.execute("SELECT * FROM alert_dispositions").fetchone())
            db.close()
        self.assertEqual((row["chosen"], row["note"], row["actor"]), ("escalate", "已上报项目组", "chen"))
        self.assertEqual(row["status"], "reporting")        # 旧码迁移仍然生效
        self.assertIsNone(row["conclusion"])
        self.assertIsNone(row["level_override"])
        self.assertIsNone(row["opinion"])


if __name__ == "__main__":
    unittest.main()
