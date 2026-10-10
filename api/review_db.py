"""The one place the platform is allowed to write, and the only tables it touches.

Everything else opens the database read-only on purpose (``api/db.py``), because
every business judgement belongs to ``processor`` and must stay reproducible from
it.  Human review is a different kind of fact: it is a second layer recorded
*about* those judgements, never an edit of them.  Keeping it in its own tables
means a reviewer can disagree with a verdict without the verdict changing under
the pipeline's feet, and re-running the pipeline never erases what a person
concluded.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from typing import Any

from api.db import DB_PATH

TASK_KINDS = ("event", "verdict", "diff")
TASK_STATUSES = ("open", "judged", "approved", "rejected")
JUDGEMENTS = ("correct", "wrong", "unsure")
# What the right answer could have been, per kind.  A bare "wrong" is a
# complaint, not a measurement: the confusion matrix and the macro-F1 the
# evaluation report has to contain need the class the system should have picked.
CORRECT_LABELS = {
    "verdict": ("SUPPORTED", "PARTIAL", "CONFLICT", "UNVERIFIABLE"),
    "event": ("low", "medium", "high"),
    # A diff can also be no diff at all — parsing noise read as a change.
    "diff": ("numeric", "modified", "added", "removed", "not_a_change"),
}
# Mirrors processor.claim_verdict.PARTIAL_SUBTYPES.  The API process must not
# import the processor (that would pull the model client into the web server),
# so the vocabulary is repeated here and a test keeps the two in step.
PARTIAL_SUBTYPES = ("timing_mismatch", "magnitude_off", "overgeneralized",
                    "wrong_entity", "missing_qualifier", "already_explained",
                    "causality_added")


def valid_label(kind: str, label: str) -> bool:
    """Whether ``label`` names a class this kind of task could have been.

    Written the way ``system_verdict`` is recorded — ``PARTIAL/timing_mismatch``
    — so the two sides of the confusion matrix compare as plain strings.
    """
    base, _, subtype = label.partition("/")
    if base not in CORRECT_LABELS.get(kind, ()):
        return False
    if not subtype:
        return True
    return kind == "verdict" and base == "PARTIAL" and subtype in PARTIAL_SUBTYPES


def connect() -> sqlite3.Connection:
    # This is the platform's only write path, and it shares the database with
    # the processing layer.  Without a busy timeout a judgement submitted while
    # a run holds the write lock fails instantly with "database is locked" —
    # the reviewer sees a 500 for something that only needed to wait its turn.
    db = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    db.execute("PRAGMA busy_timeout=30000")
    db.row_factory = sqlite3.Row
    ensure_schema(db)
    return db


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS review_tasks(
        task_id TEXT PRIMARY KEY, kind TEXT NOT NULL, target_id TEXT NOT NULL,
        company_id TEXT, origin TEXT NOT NULL, note TEXT,
        status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL,
        UNIQUE(kind, target_id))""")
    db.execute("CREATE INDEX IF NOT EXISTS review_tasks_status ON review_tasks(status, kind)")
    db.execute("""CREATE TABLE IF NOT EXISTS review_decisions(
        decision_id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, reviewer TEXT NOT NULL,
        role TEXT NOT NULL, action TEXT NOT NULL, judgement TEXT, note TEXT,
        system_verdict TEXT, decided_at TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS review_decisions_task ON review_decisions(task_id)")
    # Who is meant to judge what.  A table rather than an ``assignee`` column on
    # review_tasks because a task can have more than one owner on purpose:
    # 总体方案-Pro §11 puts part of the sample in front of two people so that
    # Cohen's κ has something to be computed over, and a single column would make
    # that slice unrepresentable.
    #
    # An assignment is a worklist, never a permission.  It answers 「这条该谁
    # 看」 so three people stop landing on the same first twenty-five rows; it
    # does not answer 「谁可以判」, which stays with the role.  Enforcing it
    # would turn a colleague covering for someone into a 403.
    db.execute("""CREATE TABLE IF NOT EXISTS review_assignments(
        task_id TEXT NOT NULL, username TEXT NOT NULL,
        assigned_at TEXT NOT NULL, assigned_by TEXT NOT NULL,
        PRIMARY KEY(task_id, username))""")
    db.execute("CREATE INDEX IF NOT EXISTS review_assignments_user "
               "ON review_assignments(username, task_id)")
    # CREATE TABLE IF NOT EXISTS skips a table that already exists, so a database
    # written by an earlier version needs the two evaluation columns added here.
    columns = {row[1] for row in db.execute("PRAGMA table_info(review_decisions)")}
    if "correct_label" not in columns:
        db.execute("ALTER TABLE review_decisions ADD COLUMN correct_label TEXT")
    if "evidence_ok" not in columns:
        db.execute("ALTER TABLE review_decisions ADD COLUMN evidence_ok INTEGER")
    db.commit()


# What each kind of task names, and where that row lives.  Used to find tasks
# whose target has been rebuilt out from under them.
TARGET_TABLES = {"diff": ("disclosure_diffs", "diff_id"),
                 "verdict": ("event_claims", "claim_id"),
                 "event": ("event_records", "event_id")}


def dangling_tasks(db: sqlite3.Connection) -> list[sqlite3.Row]:
    """Tasks naming a row that no longer exists.

    Analytical tables are rebuilt whenever their inputs change, and the ids are
    content hashes, so a rebuild retires ids rather than updating them.  A task
    left pointing at a retired id cannot be judged and still sits in the
    evaluation denominator.
    """
    clauses = " OR ".join(
        f"(t.kind='{kind}' AND NOT EXISTS(SELECT 1 FROM {table} x WHERE x.{column}=t.target_id))"
        for kind, (table, column) in sorted(TARGET_TABLES.items()))
    return db.execute(f"SELECT * FROM review_tasks t WHERE {clauses}").fetchall()


def drop_dangling(db: sqlite3.Connection) -> dict[str, int]:
    """Remove unjudgeable tasks, but never one somebody has already ruled on.

    A judgement is a person's work and outlives the row it was about; deleting
    it to tidy up the queue would destroy the only thing here that cannot be
    regenerated.
    """
    removed: dict[str, int] = {}
    kept = 0
    for task in dangling_tasks(db):
        judged = db.execute("SELECT count(*) FROM review_decisions WHERE task_id=?",
                            (task["task_id"],)).fetchone()[0]
        if judged:
            kept += 1
            continue
        db.execute("DELETE FROM review_tasks WHERE task_id=?", (task["task_id"],))
        removed[task["kind"]] = removed.get(task["kind"], 0) + 1
    db.commit()
    return {"removed": removed, "kept_because_judged": kept}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def task_id(kind: str, target_id: str) -> str:
    """Stable across re-seeding, so the same claim never gets two open tasks."""
    return hashlib.sha256(f"review\0{kind}\0{target_id}".encode()).hexdigest()[:20]
