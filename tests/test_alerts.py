import json
import sqlite3
import unittest

from processor.alerts import (MODE_CREDIBILITY, build, credibility, evidence_for, reasons,
                              window_of, within_window)


def event(**overrides):
    base = {"adjusted_risk_score": 30.0, "verdicts": {}, "impact_direction": "neutral",
            "phase_coefficient": 1.0, "new_fact_count": 0, "issue_status": "active",
            "source_tier": "S2", "production_mode": "ORIGINAL"}
    base.update(overrides)
    return base


class CredibilityTests(unittest.TestCase):
    def test_nothing_checkable_scores_below_a_checked_and_supported_report(self):
        # "We could not verify this" must never read like "this checks out".
        unchecked, *_ = credibility(event(), {}, 1)
        checked, *_ = credibility(event(), {"SUPPORTED": 4}, 1)
        self.assertLess(unchecked, checked)

    def test_a_contradicted_report_scores_below_a_supported_one(self):
        contradicted, *_ = credibility(event(), {"CONFLICT": 3}, 1)
        supported, *_ = credibility(event(), {"SUPPORTED": 3}, 1)
        self.assertLess(contradicted, supported)

    def test_corroboration_by_independent_stories_raises_the_score(self):
        alone, *_ = credibility(event(), {"SUPPORTED": 2}, 1)
        corroborated, *_ = credibility(event(), {"SUPPORTED": 2}, 4)
        self.assertGreater(corroborated, alone)

    def test_an_official_filing_scores_above_an_anonymous_source(self):
        official, *_ = credibility(event(source_tier="S0"), {"SUPPORTED": 1}, 1)
        weak, *_ = credibility(event(source_tier="S3"), {"SUPPORTED": 1}, 1)
        self.assertGreater(official, weak)

    def test_a_reprint_scores_below_an_original_report(self):
        reprint, *_ = credibility(event(production_mode="REPOST"), {"SUPPORTED": 1}, 1)
        original, *_ = credibility(event(production_mode="ORIGINAL"), {"SUPPORTED": 1}, 1)
        self.assertGreater(original, reprint)

    def test_the_score_stays_inside_the_scale(self):
        best, _, _ = credibility(event(source_tier="S0"), {"SUPPORTED": 10}, 99)
        worst, _, _ = credibility(event(source_tier="S3", production_mode="REPOST"), {"CONFLICT": 10}, 1)
        self.assertTrue(0.0 <= worst < best <= 1.0)

    def test_the_workings_are_recorded_so_the_number_can_be_argued_with(self):
        _, _, basis = credibility(event(), {"SUPPORTED": 1, "CONFLICT": 1}, 2)
        self.assertEqual(basis["independent_components"], 2)
        self.assertIn("冲突 1", basis["verdict_note"])


class ReasonTests(unittest.TestCase):
    def test_a_contradicted_claim_always_triggers(self):
        self.assertIn("claim_conflict", reasons(event(verdicts={"CONFLICT": 1})))

    def test_a_high_phase_adjusted_score_triggers(self):
        self.assertIn("high_adjusted_risk", reasons(event(adjusted_risk_score=71.5)))

    def test_a_deviation_only_triggers_when_the_report_is_negative(self):
        self.assertNotIn("partial_negative", reasons(event(verdicts={"PARTIAL": 2})))
        self.assertIn("partial_negative",
                      reasons(event(verdicts={"PARTIAL": 2}, impact_direction="negative")))

    def test_a_new_fact_in_a_sensitive_phase_triggers(self):
        self.assertIn("sensitive_phase_new_fact",
                      reasons(event(phase_coefficient=1.3, new_fact_count=2, impact_direction="negative")))

    def test_the_same_new_fact_outside_a_sensitive_phase_does_not(self):
        self.assertNotIn("sensitive_phase_new_fact",
                         reasons(event(phase_coefficient=1.0, new_fact_count=2, impact_direction="negative")))

    def test_an_ordinary_positive_event_produces_no_alert(self):
        self.assertEqual(reasons(event(impact_direction="positive")), [])


class WindowTests(unittest.TestCase):
    def test_the_window_follows_when_the_report_arrived_not_when_the_event_happened(self):
        # Grouping by event time would file a report about planned 2032 capacity
        # into a 2032 alert window.
        self.assertEqual(window_of({"published_at": "2026-07-15", "timeline_at": "2032-01-01"}),
                         "2026-07-15")

    def test_an_undated_publication_falls_back_to_the_timeline_position(self):
        self.assertEqual(window_of({"published_at": None, "timeline_at": "2026-07-15"}), "2026-07-15")

    def test_reports_days_apart_stay_in_one_window(self):
        self.assertTrue(within_window("2026-07-10", "2026-07-14"))

    def test_a_flare_up_weeks_later_opens_a_new_window(self):
        self.assertFalse(within_window("2026-07-10", "2026-08-20"))


class AlertFixture(unittest.TestCase):
    """建库与造事件的脚手架。两组测试共用它：一组测「预警是怎么攒出来的」，
    另一组测「重算之后它还是不是同一条」。"""

    def _db(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        # event_types_json 有默认值：预警闸门 2026-09-07 起排除「事件类型层判为一类都不
        # 贴合」的稿件（纯行情数据），而这个 fixture 里的事件都是要参与闸门的真事件。
        db.execute("""CREATE TABLE event_records(event_id TEXT PRIMARY KEY, company_id TEXT,
            version_id TEXT, summary TEXT, event_subjects_json TEXT, affected_objects_json TEXT,
            impact_direction TEXT, production_mode TEXT, risk_score REAL, issue_codes_json TEXT,
            event_types_json TEXT NOT NULL DEFAULT '["REGULATORY_COMPLIANCE"]')""")
        db.execute("""CREATE TABLE event_timeline(event_id TEXT PRIMARY KEY, company_id TEXT,
            version_id TEXT, timeline_at TEXT, time_basis TEXT, time_precision TEXT, phase TEXT,
            phase_label TEXT, phase_coefficient REAL, adjusted_risk_score REAL,
            adjusted_risk_level TEXT, new_fact_count INTEGER, is_new_this_round INTEGER,
            claim_count INTEGER)""")
        db.execute("""CREATE TABLE document_versions(version_id TEXT PRIMARY KEY, title TEXT,
            canonical_url TEXT, source_id TEXT, source_tier TEXT, published_at TEXT)""")
        db.execute("CREATE TABLE event_claims(claim_id TEXT PRIMARY KEY, event_id TEXT, ordinal INTEGER, text TEXT, issue_code TEXT)")
        db.execute("""CREATE TABLE claim_verdicts(claim_id TEXT PRIMARY KEY, verdict TEXT,
            partial_subtype TEXT, evidence_version_id TEXT, evidence_page INTEGER,
            evidence_section TEXT, evidence_text TEXT, method TEXT, reason TEXT)""")
        db.execute("CREATE TABLE issue_ledger(company_id TEXT, issue_code TEXT, status TEXT)")
        db.execute("CREATE TABLE propagation_component_members(component_id TEXT, version_id TEXT)")
        db.execute("CREATE TABLE timeline_rounds(round_id TEXT, ordinal INTEGER)")
        db.execute("INSERT INTO timeline_rounds VALUES('r1',0)")
        db.execute("INSERT INTO issue_ledger VALUES('cxmt','FUND_RAISE','active')")
        return db

    def _event(self, db, event_id, published, risk, claims, component="c1", subjects='["长鑫科技"]',
               event_types='["IPO_REFINANCING"]'):
        db.execute("""INSERT INTO event_records(event_id,company_id,version_id,summary,
            event_subjects_json,affected_objects_json,impact_direction,production_mode,risk_score,
            issue_codes_json,event_types_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                   (event_id, "cxmt", f"v-{event_id}", f"{event_id} 摘要", subjects,
                    '["investors_stock_price"]', "negative", "ORIGINAL", risk, '["FUND_RAISE"]',
                    event_types))
        db.execute("INSERT INTO event_timeline VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (event_id, "cxmt", f"v-{event_id}", published, "published_at", "day", "ISSUANCE",
                    "发行与询价", 1.3, round(risk * 1.3, 1), "high", 1, 0, len(claims)))
        db.execute("INSERT INTO document_versions VALUES(?,?,?,?,?,?)",
                   (f"v-{event_id}", f"{event_id} 标题", "https://example.test", "securities_times",
                    "S2", published))
        db.execute("INSERT INTO propagation_component_members VALUES(?,?)", (component, f"v-{event_id}"))
        for ordinal, (text, verdict, page) in enumerate(claims):
            claim_id = f"{event_id}-{ordinal}"
            db.execute("INSERT INTO event_claims VALUES(?,?,?,?,?)",
                       (claim_id, event_id, ordinal, text, "FUND_RAISE"))
            db.execute("INSERT INTO claim_verdicts VALUES(?,?,?,?,?,?,?,?,?)",
                       (claim_id, verdict, None, "filing-1" if page else None, page,
                        "第八节 财务会计信息", "披露原文摘录", "llm_adjudicated", "裁决理由"))


class AlertBuildTests(AlertFixture):
    def test_reports_in_one_window_on_one_issue_become_a_single_alert(self):
        # Eleven outlets in a day must not produce eleven alerts.
        db = self._db()
        self._event(db, "e1", "2026-07-10", 60.0, [("募资295亿元", "SUPPORTED", 24)])
        self._event(db, "e2", "2026-07-11", 62.0, [("募资295亿元", "SUPPORTED", 24)], component="c2")
        summary = build(db)
        self.assertEqual(summary["alerts"], 1)
        self.assertEqual(db.execute("SELECT event_count FROM alerts").fetchone()[0], 2)

    def test_pure_market_data_never_leads_an_alert(self):
        """A turnover flash is not a risk, and 2026-09-07 stopped it being one.

        The event-type layer judging 「一类都不贴合」 is what marks it: the twelve
        classes are drawn up by risk type and a single day's turnover falls into
        none of them.  175 such flashes were relabelled that day and 167 came back
        empty, 122 of them machine-written tickers.  They stay in the ledger and in
        the propagation graph — 469 causal edges hang off them — but they no longer
        reach the alert gate.
        """
        db = self._db()
        self._event(db, "e1", "2026-07-10", 90.0, [("募资295亿元", "CONFLICT", 24)],
                    event_types="[]")
        self.assertEqual(build(db)["alerts"], 0)

    def test_the_same_event_with_a_type_still_alerts(self):
        # The contrast that makes the exclusion a rule rather than a coincidence.
        db = self._db()
        self._event(db, "e1", "2026-07-10", 90.0, [("募资295亿元", "CONFLICT", 24)])
        self.assertEqual(build(db)["alerts"], 1)

    def test_a_flare_up_after_the_window_becomes_a_second_alert(self):
        db = self._db()
        self._event(db, "e1", "2026-07-10", 60.0, [("募资295亿元", "SUPPORTED", 24)])
        self._event(db, "e2", "2026-08-20", 62.0, [("募资295亿元", "SUPPORTED", 24)])
        self.assertEqual(build(db)["alerts"], 2)

    def test_an_alert_reporting_a_conflict_leads_with_the_event_that_has_it(self):
        # Leading on risk score alone produced alerts announcing a conflict
        # while showing an article that had nothing to cite.
        db = self._db()
        self._event(db, "loud", "2026-07-10", 70.0, [("无法核验的说法", "UNVERIFIABLE", None)])
        self._event(db, "cited", "2026-07-11", 50.0, [("发行价8.66元", "CONFLICT", 24)])
        build(db)
        row = db.execute("SELECT lead_event_id,evidence_json FROM alerts").fetchone()
        self.assertEqual(row["lead_event_id"], "cited")
        evidence = json.loads(row["evidence_json"])
        self.assertEqual(evidence[0]["verdict"], "CONFLICT")
        self.assertEqual(evidence[0]["evidence_page"], 24)

    def test_an_event_with_no_atomic_claim_never_raises_an_alert(self):
        # It could not carry a citation, so it has nothing to show a reader.
        db = self._db()
        self._event(db, "e1", "2026-07-10", 90.0, [])
        summary = build(db)
        self.assertEqual(summary["alerts"], 0)
        self.assertEqual(summary["excluded_events_without_claims"], 1)

    def test_reprints_of_one_story_do_not_count_as_corroboration(self):
        db = self._db()
        self._event(db, "e1", "2026-07-10", 60.0, [("募资295亿元", "SUPPORTED", 24)])
        self._event(db, "e2", "2026-07-11", 60.0, [("募资295亿元", "SUPPORTED", 24)], component="c1")
        build(db)
        basis = json.loads(db.execute("SELECT credibility_basis_json FROM alerts").fetchone()[0])
        self.assertEqual(basis["independent_components"], 1)

    def test_every_alert_carries_the_seven_outputs_the_brief_requires(self):
        db = self._db()
        self._event(db, "e1", "2026-07-10", 60.0, [("募资295亿元", "CONFLICT", 24)])
        build(db)
        row = db.execute("SELECT * FROM alerts").fetchone()
        self.assertTrue(row["summary"])
        self.assertEqual(json.loads(row["subjects_json"]), ["长鑫科技"])
        self.assertEqual(json.loads(row["affected_objects_json"]), ["investors_stock_price"])
        self.assertGreater(row["credibility"], 0)
        self.assertTrue(json.loads(row["evidence_json"]))
        self.assertEqual(json.loads(row["timeline_json"])["time_basis"], "published_at")
        self.assertTrue(json.loads(row["reasons_json"])[0]["label"])

    def test_a_severe_story_nobody_could_corroborate_is_named_as_a_rumour(self):
        # The backstop the brief asks for: high impact plus low credibility must
        # not read like an established fact, and no event-level rule can see it
        # because credibility is only known once the window is merged.
        db = self._db()
        self._event(db, "e1", "2026-07-10", 70.0, [("长鑫产能远低于披露", "UNVERIFIABLE", None)])
        # An uncorroborated brief: nothing checkable, and not an original report.
        db.execute("UPDATE event_records SET production_mode='BRIEF' WHERE event_id='e1'")
        build(db)
        row = db.execute("SELECT risk_level,credibility_label,reasons_json FROM alerts").fetchone()
        self.assertEqual((row["risk_level"], row["credibility_label"]), ("high", "low"))
        self.assertIn("unverified_major_rumour",
                      [reason["code"] for reason in json.loads(row["reasons_json"])])

    def test_a_severe_story_that_checks_out_is_not_called_a_rumour(self):
        db = self._db()
        self._event(db, "e1", "2026-07-10", 70.0,
                    [("募资295亿元", "SUPPORTED", 24), ("发行价8.66元", "SUPPORTED", 25)])
        build(db)
        codes = [reason["code"] for reason in
                 json.loads(db.execute("SELECT reasons_json FROM alerts").fetchone()[0])]
        self.assertNotIn("unverified_major_rumour", codes)

    def test_evidence_puts_the_contradicted_claim_before_the_supported_one(self):
        db = self._db()
        self._event(db, "e1", "2026-07-10", 60.0,
                    [("募资295亿元", "SUPPORTED", 24), ("发行价8.66元", "CONFLICT", 25)])
        found = evidence_for(db, ["e1"])
        self.assertEqual([item["verdict"] for item in found], ["CONFLICT", "SUPPORTED"])


if __name__ == "__main__":
    unittest.main()


class AlertIdentityTests(AlertFixture):
    """一条风险事项的身份，在重算之后还是不是同一条。

    这组测试盯的是「人工处置会被重算冲掉」：处置挂在 alert_id 上，而 alert_id 曾经
    是 sha256(公司‖议题‖窗口起点) —— 回填一篇更早的报道，窗口起点就往前挪，id 换一个，
    判过的那一行当场变成孤儿。库里 125 条孤儿是这件事已经发生过的证据。
    """

    def _ids(self, db):
        return {row["alert_id"]: dict(row) for row in db.execute(
            "SELECT alert_id, window_start, created_at, event_count FROM alerts")}

    def test_backfilling_an_earlier_report_moves_the_window_but_not_the_identity(self):
        db = self._db()
        self._event(db, "e1", "2026-07-10", 60.0, [("募资295亿元", "SUPPORTED", 24)])
        build(db)
        before = self._ids(db)
        (first_id,) = before

        # 同一件事，一篇此前没采到的更早报道补了进来。
        self._event(db, "e0", "2026-07-08", 55.0, [("募资295亿元", "SUPPORTED", 24)],
                    component="c2")
        build(db)
        after = self._ids(db)

        self.assertEqual(list(after), [first_id], "窗口挪了一下，这条事项就换了身份")
        self.assertNotEqual(after[first_id]["window_start"], before[first_id]["window_start"],
                            "这个 fixture 没能让窗口起点真的漂移，测试就没在测东西")
        self.assertEqual(after[first_id]["created_at"], before[first_id]["created_at"],
                         "首次出现时间被重建时刻覆盖了")
        self.assertEqual(after[first_id]["event_count"], 2)

    def test_a_disposition_keyed_on_the_alert_survives_the_rebuild(self):
        # 上一条测的是 id 没变；这一条测的是这件事为什么重要。
        db = self._db()
        self._event(db, "e1", "2026-07-10", 60.0, [("募资295亿元", "SUPPORTED", 24)])
        build(db)
        alert_id = db.execute("SELECT alert_id FROM alerts").fetchone()[0]
        db.execute("CREATE TABLE alert_dispositions(alert_id TEXT PRIMARY KEY, chosen TEXT)")
        db.execute("INSERT INTO alert_dispositions VALUES(?,?)", (alert_id, "escalate"))

        self._event(db, "e0", "2026-07-08", 55.0, [("募资295亿元", "SUPPORTED", 24)],
                    component="c2")
        build(db)
        orphans = db.execute("""SELECT count(*) FROM alert_dispositions d
            WHERE NOT EXISTS (SELECT 1 FROM alerts a WHERE a.alert_id = d.alert_id)""").fetchone()[0]
        self.assertEqual(orphans, 0, "有人判过的那一行在重算后指向了一条不存在的事项")

    def test_a_flare_up_weeks_later_is_a_new_item_not_the_old_one(self):
        # 认领只在共享事件时发生：第二个窗口一个成员都不重叠，它确实是新的一条。
        db = self._db()
        self._event(db, "e1", "2026-07-10", 60.0, [("募资295亿元", "SUPPORTED", 24)])
        build(db)
        (first_id,) = self._ids(db)
        self._event(db, "e2", "2026-08-20", 62.0, [("募资295亿元", "SUPPORTED", 24)],
                    component="c2")
        build(db)
        after = self._ids(db)
        self.assertEqual(len(after), 2)
        self.assertIn(first_id, after, "旧窗口原封不动，它的 id 不该动")

    def test_one_previous_item_is_claimed_by_at_most_one_window(self):
        # 一条事项分裂成两段时，只有重叠多的那一段是它的延续；另一段拿新 id，
        # 因为它确实是新的一条——而不是让两条挂在同一个 id 上互相覆盖。
        db = self._db()
        self._event(db, "e1", "2026-07-10", 60.0, [("募资295亿元", "SUPPORTED", 24)])
        self._event(db, "e2", "2026-07-11", 61.0, [("募资295亿元", "SUPPORTED", 24)],
                    component="c2")
        build(db)
        (merged_id,) = self._ids(db)

        # 把第二篇挪到窗口之外：一条事项裂成两条。
        db.execute("UPDATE event_timeline SET timeline_at='2026-09-30' WHERE event_id='e2'")
        db.execute("UPDATE document_versions SET published_at='2026-09-30' WHERE version_id='v-e2'")
        build(db)
        after = self._ids(db)
        self.assertEqual(len(after), 2)
        self.assertEqual(sum(1 for alert_id in after if alert_id == merged_id), 1,
                         "分裂后应当只有一条继承旧身份")


class ProductionModeWeightTests(unittest.TestCase):
    def test_every_production_mode_carries_a_weight(self):
        # An unmapped mode falls to the default and stops carrying information,
        # which is how the previous table became a constant in practice.
        from processor.pipeline import PRODUCTION_MODES
        self.assertEqual(set(MODE_CREDIBILITY), set(PRODUCTION_MODES))

    def test_a_promotional_piece_is_believed_less_than_original_reporting(self):
        self.assertLess(MODE_CREDIBILITY["SPONSORED"],
                        MODE_CREDIBILITY["ORIGINAL_INVESTIGATION"])
