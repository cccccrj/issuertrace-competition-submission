"""已存标签对齐到当前标签表：只改文字，不重算；历史记录不动；幂等。"""
from __future__ import annotations

import json
import sqlite3
import unittest

from processor.alerts import REASON_LABELS
from processor.relabel import relabel


class RelabelTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE alerts(alert_id TEXT, reasons_json TEXT, adjusted_risk_score REAL)")
        self.db.execute("CREATE TABLE issue_ledger(issue_id TEXT, status_reason TEXT)")
        self.db.execute("CREATE TABLE issue_status_history(issue_id TEXT, reason TEXT)")
        stale = [{"code": "issue_escalating", "label": "所属议题处于升温状态"},
                 {"code": "claim_conflict", "label": REASON_LABELS["claim_conflict"]}]
        self.db.execute("INSERT INTO alerts VALUES('a1', ?, 88.5)", (json.dumps(stale, ensure_ascii=False),))
        self.db.execute("INSERT INTO issue_ledger VALUES('i1', '议题首次出现在最近 30 天内')")
        self.db.execute("INSERT INTO issue_status_history VALUES('i1', '议题首次出现在最近 30 天内')")

    def test_labels_follow_the_current_table_and_nothing_else_moves(self):
        result = relabel(self.db)
        self.assertEqual(result, {"alerts_reasons": 1, "issue_status_reason": 1})
        reasons = json.loads(self.db.execute("SELECT reasons_json FROM alerts").fetchone()[0])
        self.assertEqual([r["label"] for r in reasons],
                         [REASON_LABELS["issue_escalating"], REASON_LABELS["claim_conflict"]])
        self.assertNotIn("议题", REASON_LABELS["issue_escalating"])
        self.assertEqual(self.db.execute("SELECT adjusted_risk_score FROM alerts").fetchone()[0], 88.5)
        self.assertEqual(self.db.execute("SELECT status_reason FROM issue_ledger").fetchone()[0],
                         "专题首次出现在最近 30 天内", "数字部分原样保留")

    def test_history_keeps_what_was_said_at_the_time(self):
        relabel(self.db)
        self.assertEqual(self.db.execute("SELECT reason FROM issue_status_history").fetchone()[0],
                         "议题首次出现在最近 30 天内")

    def test_a_second_run_changes_nothing(self):
        relabel(self.db)
        self.assertEqual(relabel(self.db), {"alerts_reasons": 0, "issue_status_reason": 0})


if __name__ == "__main__":
    unittest.main()
