"""The push policy, tested where it is decided rather than where it is sent.

A bug in retrieval shows a wrong number on a page. A bug here wakes someone at
three in the morning, or stays silent when it should not have. Both halves are
pure functions over dictionaries precisely so that neither needs a database, a
socket or a real clock to be checked.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from api.dispositions import recommend
from api.notify_rules import (
    DEDUPE_WINDOW, changes_since_last_delivery, matches, payload, suppression,
)

NOW = datetime(2026, 9, 4, 14, 0, tzinfo=timezone.utc)


def alert(**overrides):
    base = {
        "alert_id": "a1", "company_id": "cxmt", "issue_code": "FUND_RAISE",
        "risk_level": "high", "credibility_label": "high", "phase": "FILING",
        "phase_label": "申报受理与问询", "status": "open", "event_count": 3,
        "production_mode": "ORIGINAL_INVESTIGATION",
        "reasons_json": json.dumps(["unverified_major_rumour"]),
        "headline": "长鑫科技募资规模传闻", "summary": "媒体称募资 295 亿元",
        "subjects_json": json.dumps(["长鑫科技"]),
        "affected_objects_json": json.dumps(["investors_stock_price"]),
        "credibility": 0.81, "adjusted_risk_score": 82.5,
        "window_start": "2026-09-01", "window_end": "2026-09-03",
    }
    return {**base, **overrides}


def subscription(**overrides):
    base = {"subscription_id": "s1", "enabled": 1, "min_risk_level": "high",
            "company_id": None, "issue_code": None, "min_credibility": None,
            "phases_json": "[]", "reason_codes_json": "[]", "quiet_hours": None,
            "channel": "webhook"}
    return {**base, **overrides}


def delivered(minutes_ago: int, status: str = "sent"):
    return {"status": status,
            "delivered_at": (NOW - timedelta(minutes=minutes_ago)).isoformat()}


class MatchTests(unittest.TestCase):
    def test_an_empty_rule_matches_everything_at_or_above_its_floor(self):
        self.assertTrue(matches(subscription(), alert()))
        self.assertFalse(matches(subscription(), alert(risk_level="medium")))

    def test_the_floor_is_inclusive_and_ordered(self):
        rule = subscription(min_risk_level="medium")
        self.assertTrue(matches(rule, alert(risk_level="medium")))
        self.assertTrue(matches(rule, alert(risk_level="high")))
        self.assertFalse(matches(rule, alert(risk_level="low")))

    def test_a_disabled_rule_matches_nothing(self):
        self.assertFalse(matches(subscription(enabled=0), alert()))

    def test_unset_narrowings_do_not_narrow(self):
        # An unset company means every company, not no company — the failure
        # mode where a rule silently matches nothing.
        self.assertTrue(matches(subscription(company_id=None), alert(company_id="wuxi_apptec")))

    def test_company_and_issue_narrow_when_set(self):
        self.assertFalse(matches(subscription(company_id="cxmt"), alert(company_id="wuxi_apptec")))
        self.assertFalse(matches(subscription(issue_code="TECH_LEVEL"), alert()))

    def test_a_reason_filter_needs_only_one_of_the_alerts_reasons(self):
        rule = subscription(reason_codes_json=json.dumps(["unverified_major_rumour", "conflict"]))
        self.assertTrue(matches(rule, alert()))
        self.assertFalse(matches(rule, alert(reasons_json=json.dumps(["routine"]))))

    def test_json_columns_are_read_whether_text_or_list(self):
        rule = subscription(phases_json=["FILING"])
        self.assertTrue(matches(rule, alert()))


class ChangeTests(unittest.TestCase):
    def test_never_delivered_is_new(self):
        self.assertEqual(changes_since_last_delivery(None, alert()), "new")

    def test_an_unchanged_alert_is_not_worth_saying_again(self):
        previous = {"risk_level": "high", "status": "open", "event_count": 3}
        self.assertIsNone(changes_since_last_delivery(previous, alert()))

    def test_a_level_rise_is_an_escalation_and_a_fall_is_not(self):
        self.assertEqual(
            changes_since_last_delivery({"risk_level": "medium", "status": "open"}, alert()),
            "escalated")
        self.assertEqual(
            changes_since_last_delivery({"risk_level": "high", "status": "open"},
                                        alert(risk_level="medium")),
            "downgraded")

    def test_a_withdrawal_outranks_a_level_change(self):
        # Being told an alert no longer stands matters more than which way its
        # score moved on the way out.
        previous = {"risk_level": "medium", "status": "open"}
        self.assertEqual(
            changes_since_last_delivery(previous, alert(status="withdrawn")), "withdrawn")

    def test_more_events_in_the_same_window_is_an_update(self):
        previous = {"risk_level": "high", "status": "open", "event_count": 2}
        self.assertEqual(changes_since_last_delivery(previous, alert(event_count=5)), "updated")


class SuppressionTests(unittest.TestCase):
    def test_nothing_in_the_way_means_send(self):
        self.assertIsNone(suppression(subscription(), alert(), [], now=NOW))

    def test_the_same_issue_inside_the_window_is_merged(self):
        reason = suppression(subscription(), alert(), [delivered(60)], now=NOW)
        self.assertEqual(reason, "deduped_24h")

    def test_outside_the_window_it_is_a_new_day_and_a_new_alert(self):
        older = int(DEDUPE_WINDOW.total_seconds() // 60) + 60
        self.assertIsNone(suppression(subscription(), alert(), [delivered(older)], now=NOW))

    def test_a_failed_delivery_does_not_count_as_having_told_anyone(self):
        self.assertIsNone(suppression(subscription(), alert(), [delivered(60, "failed")], now=NOW))

    def test_a_dry_run_does_count_so_a_rehearsal_is_not_sent_twice(self):
        self.assertEqual(
            suppression(subscription(), alert(), [delivered(60, "dry_run")], now=NOW), "deduped_24h")

    def test_second_hand_coverage_is_not_pushed(self):
        # A repost of a story already pushed is that story arriving again
        # through another door.
        self.assertEqual(
            suppression(subscription(), alert(production_mode="REPOST"), [], now=NOW),
            "production_mode")

    def test_quiet_hours_hold_across_midnight(self):
        # The case that is always wrong when written the obvious way.
        rule = subscription(quiet_hours="22:00-07:00")
        night = datetime(2026, 9, 4, 23, 30, tzinfo=timezone.utc)
        dawn = datetime(2026, 9, 4, 3, 0, tzinfo=timezone.utc)
        noon = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(suppression(rule, alert(), [], now=night), "quiet_hours")
        self.assertEqual(suppression(rule, alert(), [], now=dawn), "quiet_hours")
        self.assertIsNone(suppression(rule, alert(), [], now=noon))

    def test_an_unparseable_timestamp_does_not_silence_a_push(self):
        # Failing open is the right direction here: a corrupt row should not be
        # able to stop an alert going out.
        self.assertIsNone(
            suppression(subscription(), alert(), [{"status": "sent", "delivered_at": "nonsense"}], now=NOW))


class PayloadTests(unittest.TestCase):
    def test_it_carries_the_seven_outputs_the_brief_asks_for(self):
        body = payload(alert(), [{"text": "募资 295 亿元", "verdict": "PARTIAL",
                                  "evidence_title": "招股说明书", "evidence_page": 164}])
        for key in ("summary", "subjects", "affected_objects", "credibility",
                    "evidence", "window", "reasons"):
            self.assertIn(key, body)
        self.assertEqual(body["evidence"][0]["page"], 164)

    def test_it_is_actionable_without_logging_in(self):
        # A push saying only 「有新预警」 forces a login to find out whether it
        # matters, which is how a notification becomes an interruption.
        body = payload(alert())
        self.assertTrue(body["summary"])
        self.assertEqual(body["risk_level"], "high")
        self.assertEqual(body["risk_level_label"], "高风险")
        # /alerts was retired; the link goes to the risk workbench.
        self.assertTrue(body["link"].startswith("/risks/"))

    def test_reasons_travel_as_codes_with_labels_not_as_dict_text(self):
        # I-06: each reason used to arrive as "{'code': ..., 'label': ...}".
        body = payload({**alert(), "reasons_json": '[{"code": "claim_conflict", "label": "报道与披露口径冲突"}]'})
        self.assertEqual(body["reasons"], [{"code": "claim_conflict", "label": "报道与披露口径冲突"}])

    def test_a_reason_filter_can_actually_match(self):
        # The same str() made every reason filter miss, silently.
        subscription = {"reason_codes_json": '["claim_conflict"]'}
        self.assertTrue(matches(subscription, {**alert(), "reasons_json": '[{"code": "claim_conflict", "label": "x"}]'}))
        self.assertFalse(matches(subscription, {**alert(), "reasons_json": '[{"code": "high_adjusted_risk", "label": "x"}]'}))

    def test_evidence_is_capped_so_a_push_stays_a_push(self):
        claims = [{"text": f"c{i}", "verdict": "SUPPORTED"} for i in range(20)]
        self.assertEqual(len(payload(alert(), claims)["evidence"]), 5)


class DispositionTests(unittest.TestCase):
    def test_a_conflict_with_credible_evidence_calls_for_a_clarification(self):
        result = recommend(alert(), [{"verdict": "CONFLICT"}])
        self.assertEqual(result["recommended"], "clarification")
        self.assertIn("冲突", result["why"])

    def test_high_risk_in_a_filing_window_is_escalated(self):
        result = recommend(alert(reasons=[]), [{"verdict": "SUPPORTED"}])
        self.assertEqual(result["recommended"], "escalate")

    def test_a_big_claim_on_thin_evidence_is_verified_before_replying(self):
        result = recommend(alert(phase="POST_LISTING", credibility_label="low", reasons=[]),
                           [{"verdict": "UNVERIFIABLE"}])
        self.assertEqual(result["recommended"], "formal_reply")

    def test_mostly_already_explained_is_a_downgrade_not_a_dismissal(self):
        result = recommend(
            alert(risk_level="medium", phase="POST_LISTING", reasons=[]),
            [{"verdict": "PARTIAL", "partial_subtype": "already_explained"},
             {"verdict": "PARTIAL", "partial_subtype": "already_explained"},
             {"verdict": "SUPPORTED"}])
        self.assertEqual(result["recommended"], "cold")
        self.assertIn("已问", result["why"])

    def test_every_recommendation_prints_what_produced_it(self):
        # Advice whose derivation cannot be shown is advice nobody can argue
        # with, and unarguable advice is either followed blindly or ignored.
        result = recommend(alert(), [{"verdict": "CONFLICT"}])
        codes = {item["code"] for item in result["basis"]}
        self.assertEqual(codes, {"verdicts", "credibility", "already_explained", "phase"})
        for item in result["basis"]:
            self.assertTrue(item["value"])

    def test_it_never_returns_nothing(self):
        self.assertEqual(
            recommend(alert(risk_level="low", phase="POST_LISTING", reasons=[]), [])["recommended"],
            "cold")


if __name__ == "__main__":
    unittest.main()
