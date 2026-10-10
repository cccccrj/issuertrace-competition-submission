"""The push routes, checked for who may do what.

The rules layer decides whether to send and the delivery layer decides where to;
this layer decides *who can ask*. Getting that wrong is how a read-only platform
grows a way for an anonymous request to make it send things.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from tests.test_api import ApiTestCase, build_fixture  # reuses the fixture harness


class NotifyTestCase(ApiTestCase):
    """The notify routes write, so they need their own database file too."""

    def setUp(self):
        super().setUp()
        import api.dispatch_db as notify_module
        self._original_connect = notify_module.connect

        path = self.fixture_path

        def connect():
            db = sqlite3.connect(path, timeout=30, check_same_thread=False)
            db.execute("PRAGMA busy_timeout=30000")
            db.row_factory = sqlite3.Row
            notify_module.ensure_schema(db)
            return db

        notify_module.connect = connect
        self.addCleanup(lambda: setattr(notify_module, "connect", self._original_connect))


class RoleTests(NotifyTestCase):
    def test_reading_subscriptions_needs_only_a_session(self):
        self.assertEqual(self.client.get("/api/notify/subscriptions").status_code, 200)

    def test_the_allowlist_is_shown_to_an_administrator_only(self):
        # I-06: the allowlist is operator configuration, not something every role reads.
        self.assertEqual(self.client.get("/api/notify/subscriptions").json()["allowlist"], [])
        self.become("ops", "admin")
        body = self.client.get("/api/notify/subscriptions").json()
        self.assertTrue(any("127.0.0.1" in pattern for pattern in body["allowlist"]))


class SubscriptionTests(NotifyTestCase):
    def create(self, **overrides):
        body = {"name": "高风险", "channel": "webhook",
                "target": "http://127.0.0.1:8787/hook", **overrides}
        return self.client.post("/api/notify/subscriptions", json=body)

    def test_a_new_rule_starts_in_dry_run(self):
        # The safe default for a feature that can reach the network is one that
        # cannot, until somebody says otherwise.
        created = self.create().json()
        self.assertTrue(created["dry_run"])
        self.assertTrue(created["enabled"])

    def test_a_target_outside_the_allowlist_is_refused_at_creation(self):
        # Refusing at send time would leave a rule somebody believes is working.
        response = self.create(target="https://example.test/hook")
        self.assertEqual(response.status_code, 400)
        detail = response.json()["detail"]
        self.assertIn("允许推送的范围", detail)
        # The rule itself and the variable that holds it stay on the server.
        self.assertNotIn("NOTIFY_WEBHOOK_ALLOWLIST", detail)
        self.assertNotIn("example.test", detail)

    def test_an_unknown_channel_is_refused(self):
        self.assertEqual(self.create(channel="carrier_pigeon").status_code, 400)

    def test_reminder_conditions_are_offered_in_words_and_unknown_ones_refused(self):
        # 一个永远匹配不上的条件，会让这条订阅安静得像是在正常工作。
        reasons = self.client.get("/api/notify/subscriptions").json()["reasons"]
        self.assertIn("claim_conflict", reasons)
        self.assertTrue(all(label for label in reasons.values()))
        self.assertEqual(self.create(channel="inapp", reason_codes=["claim_conflict"]).status_code, 200)
        refused = self.create(channel="inapp", reason_codes=["bad_vibes"])
        self.assertEqual(refused.status_code, 400)
        self.assertIn("bad_vibes", refused.json()["detail"])

    def test_the_secret_reference_is_a_name_and_the_value_never_appears(self):
        with mock.patch.dict("os.environ", {"DEMO_KEY": "s3cret"}):
            created = self.create(secret_ref="DEMO_KEY").json()
            listing = self.client.get("/api/notify/subscriptions").text
        self.assertTrue(created["secret_configured"])
        self.assertNotIn("s3cret", listing)
        # Even the variable's name is operator configuration (I-06).
        self.assertNotIn("DEMO_KEY", listing)
        self.become("ops", "admin")
        self.assertIn("DEMO_KEY", self.client.get("/api/notify/subscriptions").text)


class WriteGuardTests(NotifyTestCase):
    """Every route that can cause a send is a POST behind a role."""

    def test_no_get_route_can_send(self):
        # A send reachable by GET is a send reachable from a link in an email.
        from api.routes import notify
        for route in notify.router.routes:
            if "GET" in route.methods:
                self.assertNotIn(route.path.rsplit("/", 1)[-1], {"flush", "evaluate"})

    def test_a_get_cannot_reach_evaluate_or_flush(self):
        # The property is that a GET cannot cause a send, not which refusal code
        # says so: 404 and 405 both satisfy it, and 404 is marginally better
        # because it does not confirm the endpoint exists.
        for path in ("/api/notify/evaluate", "/api/notify/flush"):
            self.assertIn(self.client.get(path).status_code, {404, 405}, path)


class OutboxTests(NotifyTestCase):
    def test_the_outbox_reports_what_did_not_go_out_as_well_as_what_did(self):
        # A delivery that never happened and nobody can explain is
        # indistinguishable from a bug.
        body = self.client.get("/api/notify/outbox").json()
        self.assertIn("suppressed_reasons", body)
        self.assertIn("by_status", body)
        self.assertEqual(body["items"], [])


class DispositionTests(NotifyTestCase):
    def test_every_recommendation_carries_its_derivation(self):
        body = self.client.get("/api/notify/dispositions").json()
        self.assertIn("actions", body)
        for item in body["items"]:
            self.assertTrue(item["basis"])
            self.assertIn(item["recommended"], body["actions"])

    def test_the_sentence_that_explains_the_choice_reaches_the_client(self):
        # `recommend()` returns a `why` and a label; `upsert_disposition` stores
        # neither, because both are derived from rules that can be edited. They
        # were therefore missing from the response for as long as the endpoint
        # existed, which left the interface with an action code and no argument
        # for it — exactly the thing dispositions.py says it must never be.
        items = self.client.get("/api/notify/dispositions").json()["items"]
        self.assertTrue(items, "夹具里应当至少有一条 open 预警，否则这条断言是空转的")
        for item in items:
            self.assertTrue(item["why"])
            self.assertTrue(item["recommended_label"])
            self.assertTrue(item["recommended_detail"])

    def test_an_unknown_action_is_refused(self):
        response = self.client.post("/api/notify/dispositions/whatever",
                                    json={"chosen": "burn_it_down"})
        self.assertEqual(response.status_code, 400)


class DecisionTests(NotifyTestCase):
    """M7-9 处置状态流转 — the four role and bookkeeping rules behind the control.

    The endpoint existed for as long as the recommendation did and had no caller
    in the client, so none of this was exercised end to end. It is now, because
    the alert drawer records a decision.
    """

    def an_alert(self):
        items = self.client.get("/api/notify/dispositions").json()["items"]
        self.assertTrue(items, "夹具里应当至少有一条 open 预警")
        return items[0]

    def test_a_viewer_cannot_record_a_disposition(self):
        alert = self.an_alert()
        self.become("looker", "viewer")
        response = self.client.post(f"/api/notify/dispositions/{alert['alert_id']}",
                                    json={"chosen": "cold", "status": "acting"})
        self.assertEqual(response.status_code, 403)

    def test_the_choice_never_overwrites_the_recommendation(self):
        # 「系统建议 A，人选了 B」 has to stay answerable, so the two columns are
        # separate and only one of them is written by a person.
        alert = self.an_alert()
        recommended = alert["recommended"]
        other = next(code for code in ("cold", "escalate", "clarification", "formal_reply")
                     if code != recommended)
        self.become("wei", "analyst")
        body = self.client.post(f"/api/notify/dispositions/{alert['alert_id']}",
                                json={"chosen": other, "status": "investigating",
                                      "note": "口径待核实"}).json()
        self.assertEqual(body["chosen"], other)
        self.assertEqual(body["recommended"], recommended)
        self.assertEqual((body["actor"], body["role"]), ("wei", "analyst"))
        self.assertEqual(body["note"], "口径待核实")
        self.assertIsNotNone(body["decided_at"])

    def test_recomputing_the_recommendation_leaves_a_human_choice_alone(self):
        # The GET upserts the recommendation on every call. A rebuilt alert must
        # not quietly revert someone's decision to the machine's.
        alert = self.an_alert()
        self.become("wei", "analyst")
        self.client.post(f"/api/notify/dispositions/{alert['alert_id']}",
                         json={"chosen": "escalate", "status": "investigating"})
        again = {row["alert_id"]: row
                 for row in self.client.get("/api/notify/dispositions").json()["items"]}
        self.assertEqual(again[alert["alert_id"]]["chosen"], "escalate")

    def test_archiving_needs_the_reviewer_role(self):
        alert = self.an_alert()
        self.become("wei", "analyst")
        refused = self.client.post(f"/api/notify/dispositions/{alert['alert_id']}",
                                   json={"chosen": "cold", "status": "archived"})
        self.assertEqual(refused.status_code, 403)
        self.become("chen", "reviewer")
        allowed = self.client.post(f"/api/notify/dispositions/{alert['alert_id']}",
                                   json={"chosen": "cold", "status": "archived"})
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json()["status"], "archived")

    def test_a_step_the_flow_does_not_allow_is_refused_with_its_alternatives(self):
        # 待研判不能直接「退回补证」：没有上报过，就没有可退回的东西。
        # 拒绝里必须写清能去哪，否则读者只知道不行、不知道该怎么办。
        # 409 先于 422：走不通的路，不该先被告知「缺了退回意见」。
        alert = self.an_alert()
        self.become("wei", "analyst")
        refused = self.client.post(f"/api/notify/dispositions/{alert['alert_id']}",
                                   json={"chosen": "cold", "status": "returned"})
        self.assertEqual(refused.status_code, 409)
        self.assertIn("核查中", refused.json()["detail"])

    def test_an_admin_can_archive(self):
        # 角色是逐级包含的。此前这里写的是 ``role != "reviewer"``，于是权限最高的
        # 那个角色反而被挡在外面。
        alert = self.an_alert()
        self.become("guan", "admin")
        allowed = self.client.post(f"/api/notify/dispositions/{alert['alert_id']}",
                                   json={"chosen": "cold", "status": "archived"})
        self.assertEqual(allowed.status_code, 200)

    def test_every_step_leaves_a_row_that_says_who_and_when(self):
        # 状态列只记得最后一次覆盖。「这条事项经历过什么」必须能读出来，而不是靠猜。
        alert = self.an_alert()
        self.become("wei", "analyst")
        self.client.post(f"/api/notify/dispositions/{alert['alert_id']}",
                         json={"chosen": "formal_reply", "status": "investigating"})
        self.become("chen", "reviewer")
        self.client.post(f"/api/notify/dispositions/{alert['alert_id']}",
                         json={"chosen": "formal_reply", "status": "archived"})
        trail = self.client.get("/api/audit/dispositions").json()["items"]
        self.assertEqual([(row["from_status"], row["to_status"]) for row in trail],
                         [("investigating", "archived"), ("pending", "investigating")])
        self.assertEqual([row["actor"] for row in trail], ["chen", "wei"])
        self.assertTrue(all(row["to_label"] for row in trail))

    def test_an_unknown_status_is_refused(self):
        alert = self.an_alert()
        self.become("wei", "analyst")
        response = self.client.post(f"/api/notify/dispositions/{alert['alert_id']}",
                                    json={"chosen": "cold", "status": "shredded"})
        self.assertEqual(response.status_code, 400)

    def test_the_log_reads_without_upserting_and_keeps_archived_rows(self):
        # /dispositions upserts on every GET and filters to open alerts. The
        # audit log must do neither: an archived decision is precisely the kind
        # a reader comes looking for, and a log that writes is not a log.
        alert = self.an_alert()
        # The recommendation is derived, so the test picks a different action
        # rather than asserting which one the rules produced for this fixture.
        other = next(code for code in ("cold", "escalate", "clarification", "formal_reply")
                     if code != alert["recommended"])
        self.become("chen", "reviewer")
        self.client.post(f"/api/notify/dispositions/{alert['alert_id']}",
                         json={"chosen": other, "status": "archived", "note": "已上报"})
        entry = self.client.get("/api/notify/dispositions/log").json()["items"][0]
        self.assertEqual(entry["status"], "archived")
        self.assertEqual((entry["actor"], entry["role"]), ("chen", "reviewer"))
        self.assertEqual(entry["chosen"], other)
        self.assertTrue(entry["chosen_label"])
        self.assertTrue(entry["diverged"])
        self.assertEqual(entry["note"], "已上报")

    def test_the_log_lists_only_decisions_a_person_made(self):
        # Every open alert has a recommendation row; only some have a decision.
        # A log padded with the machine's own rows answers nothing.
        self.assertEqual(self.client.get("/api/notify/dispositions/log").json()["items"], [])

    def test_the_status_filter_selects_what_is_still_open(self):
        alert = self.an_alert()
        self.become("wei", "analyst")
        self.client.post(f"/api/notify/dispositions/{alert['alert_id']}",
                         json={"chosen": "cold", "status": "investigating"})
        done = self.client.get("/api/notify/dispositions",
                               params={"status": "investigating"}).json()
        self.assertEqual([row["alert_id"] for row in done["items"]], [alert["alert_id"]])


class StaleRecommendationTests(NotifyTestCase):
    """id 漂移留下的空建议行会被收走，人碰过的那种永远不会。

    孤儿的成因已经在 `processor.alerts.carry_forward` 修掉了；这一组管的是**留下的
    那批**怎么处理，以及边界在哪：删掉一条没人看过的建议是打扫，删掉一条有人判过
    却找不到事项的记录是掩盖。
    """

    def _orphan(self, alert_id, **columns):
        import api.dispatch_db as dispatch
        db = dispatch.connect()
        keys = ("disposition_id", "alert_id", "company_id", "recommended",
                "recommended_basis_json", "status", "created_at")
        values = {"disposition_id": f"d-{alert_id}", "alert_id": alert_id, "company_id": "cxmt",
                  "recommended": "cold", "recommended_basis_json": "[]", "status": "pending",
                  "created_at": "2026-09-01T00:00:00+00:00"}
        columns_sql = ",".join(keys) + ("," + ",".join(columns) if columns else "")
        placeholders = ",".join("?" for _ in range(len(keys) + len(columns)))
        db.execute(f"INSERT INTO alert_dispositions({columns_sql}) VALUES({placeholders})",
                   [values[key] for key in keys] + list(columns.values()))
        db.commit()
        db.close()

    def _count(self, alert_id):
        import api.dispatch_db as dispatch
        db = dispatch.connect()
        found = db.execute("SELECT count(*) FROM alert_dispositions WHERE alert_id=?",
                           (alert_id,)).fetchone()[0]
        db.close()
        return found

    def test_a_recommendation_nobody_touched_is_swept_up(self):
        self._orphan("gone-nobody-touched")
        self.client.get("/api/notify/dispositions")
        self.assertEqual(self._count("gone-nobody-touched"), 0)

    def test_a_decision_a_person_made_is_never_swept_up(self):
        # 判过、而事项找不到了——这是要有人来看的问题，不是垃圾。
        self._orphan("gone-but-decided", chosen="escalate", actor="analyst",
                     decided_at="2026-09-02T03:00:00+00:00")
        self.client.get("/api/notify/dispositions")
        self.assertEqual(self._count("gone-but-decided"), 1)

    def test_a_live_alert_keeps_its_recommendation(self):
        items = self.client.get("/api/notify/dispositions").json()["items"]
        self.assertTrue(items)
        self.client.get("/api/notify/dispositions")
        self.assertEqual(self._count(items[0]["alert_id"]), 1)


class DispositionScopeTests(NotifyTestCase):
    """一个页面只要一条建议时，不该让服务端把全部未处置预警重算一遍。

    事件详情页此前按公司取回 182 条再在前端筛出一条，服务端为此逐条跑三表 JOIN、逐条
    commit——这一页因此要 1.24 秒。``alert_id`` 可重复传入，页面把自己那几条报上来。
    """

    def test_alert_id_narrows_the_answer(self):
        every = self.client.get("/api/notify/dispositions").json()["items"]
        self.assertGreater(len(every), 0)
        one = every[0]["alert_id"]
        narrowed = self.client.get("/api/notify/dispositions", params={"alert_id": one}).json()
        self.assertEqual([row["alert_id"] for row in narrowed["items"]], [one])

    def test_several_ids_come_back_in_one_request(self):
        ids = [row["alert_id"] for row in
               self.client.get("/api/notify/dispositions").json()["items"]][:2]
        if len(ids) < 2:
            self.skipTest("fixture 只有一条预警")
        body = self.client.get("/api/notify/dispositions", params=[("alert_id", i) for i in ids]).json()
        self.assertEqual(sorted(row["alert_id"] for row in body["items"]), sorted(ids))


if __name__ == "__main__":
    unittest.main()
