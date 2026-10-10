"""The write path for M4-6, and the guards on the route that uses it.

The rebuild's side of this is in ``test_processing_pipeline``; what is here is
the log itself — which must be append-only — and the refusals, which all happen
before any rebuild is attempted.
"""
from __future__ import annotations

import sqlite3
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import api.issue_db as issue_db
from tests.test_api import ApiTestCase


class LogTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        issue_db.ensure_schema(self.db)

    def move(self, event_id="e1", from_code="A", to_code="B", actor="r"):
        return issue_db.record(self.db, company_id="c", event_id=event_id, from_code=from_code,
                               to_code=to_code, reason="因为这样", actor=actor, role="reviewer")

    def test_a_move_is_recorded_with_who_made_it(self):
        self.move()
        self.db.commit()
        row = issue_db.active(self.db)[0]
        self.assertEqual((row["from_code"], row["to_code"], row["actor"], row["role"]),
                         ("A", "B", "r", "reviewer"))

    def test_moving_the_same_event_again_supersedes_rather_than_duplicates(self):
        # Two rows in the log, one in force. An event moved twice has a chain,
        # and the ledger is explained by the last link of it.
        self.move(to_code="B")
        self.move(to_code="C")
        self.db.commit()
        self.assertEqual(len(issue_db.active(self.db)), 1)
        self.assertEqual(issue_db.active(self.db)[0]["to_code"], "C")
        self.assertEqual(len(issue_db.history(self.db)), 2)

    def test_an_undo_keeps_the_row_and_names_who_undid_it(self):
        # Deleting would erase the only record of why the ledger once differed.
        identifier = self.move()
        self.db.commit()
        self.assertTrue(issue_db.undo(self.db, identifier, "someone-else"))
        self.db.commit()
        self.assertEqual(issue_db.active(self.db), [])
        row = issue_db.history(self.db)[0]
        self.assertEqual(row["undone_by"], "someone-else")
        self.assertTrue(row["undone_at"])

    def test_undoing_twice_reports_that_there_was_nothing_to_undo(self):
        identifier = self.move()
        self.db.commit()
        issue_db.undo(self.db, identifier, "r")
        self.assertFalse(issue_db.undo(self.db, identifier, "r"))

    def test_correcting_yourself_within_the_same_second_works(self):
        # decided_at has second precision. Deriving the id from it meant a
        # reviewer who moved an event and immediately changed their mind got a
        # UNIQUE violation and a 500.
        first, second = self.move(to_code="B"), self.move(to_code="C")
        self.db.commit()
        self.assertNotEqual(first, second)
        self.assertEqual(len(issue_db.history(self.db)), 2)

    def test_recording_does_not_commit_by_itself(self):
        # A 合并 is many moves plus a ledger rebuild, and all of it has to land
        # or none of it. The first version committed per row, so a rebuild that
        # then failed left the events moved and the ledger describing the world
        # before the move.
        self.move()
        self.db.rollback()
        self.assertEqual(issue_db.history(self.db), [])


class RouteGuardTests(ApiTestCase):
    """Everything the route refuses, all of it before a rebuild is attempted."""

    def setUp(self):
        super().setUp()
        self.adjust_db = sqlite3.connect(":memory:", check_same_thread=False)
        self.adjust_db.row_factory = sqlite3.Row
        issue_db.ensure_schema(self.adjust_db)
        patch = mock.patch.object(issue_db, "connect", lambda: self.adjust_db)
        patch.start()
        self.addCleanup(patch.stop)

    def reassign(self, **body):
        payload = {"company_id": "cxmt", "from_code": "FUND_RAISE",
                   "to_code": "TECH_LEVEL", "reason": "因为这样"}
        payload.update(body)
        return self.client.post("/api/issues/reassign", json=payload)

    def test_a_viewer_cannot_move_anything(self):
        self.become("v", "viewer")
        self.assertEqual(self.reassign().status_code, 403)

    def test_an_analyst_cannot_either(self):
        # Reassigning changes what every reader of the ledger sees; that is the
        # reviewer's authority, the same as judging a task.
        self.become("a", "analyst")
        self.assertEqual(self.reassign().status_code, 403)

    def test_an_unknown_target_code_is_refused(self):
        # Inventing an issue name to hold a split would put a code on the page
        # that config/sources.yaml has never heard of, and 主数据与词表 exists to
        # call such a value a defect rather than a new entry.
        self.become("r", "reviewer")
        response = self.reassign(to_code="NOT_A_REAL_CODE")
        self.assertEqual(response.status_code, 400)
        self.assertIn("未知的专题码", response.json()["detail"])

    def test_moving_a_code_onto_itself_is_refused(self):
        self.become("r", "reviewer")
        self.assertEqual(self.reassign(to_code="FUND_RAISE").status_code, 400)

    def test_naming_an_event_that_is_not_in_the_issue_is_an_error(self):
        # Silently skipping it would mean the reviewer selected five events,
        # four moved, and nothing said which one did not.
        self.become("r", "reviewer")
        response = self.reassign(event_ids=["e1", "not-an-event"])
        self.assertEqual(response.status_code, 400)
        self.assertIn("不在该专题下", response.json()["detail"])

    def test_an_issue_with_no_events_is_refused_rather_than_silently_doing_nothing(self):
        self.become("r", "reviewer")
        response = self.reassign(from_code="CYBERSECURITY")
        self.assertEqual(response.status_code, 400)

    def test_a_reason_is_required(self):
        # The log is the only explanation of why the ledger looks as it does.
        self.become("r", "reviewer")
        self.assertEqual(self.reassign(reason="").status_code, 422)

    def test_undoing_something_that_does_not_exist_is_a_404(self):
        self.become("r", "reviewer")
        self.assertEqual(
            self.client.post("/api/issues/adjustments/nope/undo").status_code, 404)

    def test_the_exports_are_rebuilt_alongside_the_ledger(self):
        # processor.verify compares issues.jsonl against the table row for row.
        # Rebuilding one and not the other left the integrity check failing
        # after every legitimate reassignment, and a check that always reports
        # a problem stops being read.
        import api.routes.issues as route
        calls = []
        with mock.patch.object(route, "rebuild_issue_ledger", lambda db: calls.append("ledger") or 7), \
             mock.patch.object(route, "export_data", lambda db: calls.append("export")):
            self.assertEqual(route.republish(self.adjust_db), 7)
        self.assertEqual(calls, ["ledger", "export"])

    def test_the_log_is_readable_without_being_a_reviewer(self):
        # Why the ledger looks the way it does is not privileged information.
        self.become("v", "viewer")
        self.assertEqual(self.client.get("/api/issues/adjustments/log").status_code, 200)


if __name__ == "__main__":
    unittest.main()
