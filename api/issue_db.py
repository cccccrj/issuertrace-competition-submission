"""Human corrections to 议题归并, and the only place they are written.

M4-6 says 「归并错了要能救」.  What needs rescuing is specific: an issue here is
``(company, issue_code)``, and every event lands in one because the labelling
pass put that code on it.  When that is wrong the ledger is wrong, and nothing
could say so.

The correction is one primitive — move an event out of an issue code, into
another one or out of the ledger entirely.  合并 is every event of A moved to B;
拆分 is some of them moved.  Both stay inside the controlled vocabulary, which
is deliberate: inventing an issue name to hold a split would put a code on the
page that ``config/sources.yaml`` has never heard of, and 主数据与词表 exists to
say that such a value is a defect rather than a new entry.

Why a table of its own.  ``rebuild_issue_ledger`` deletes the ledger and builds
it again from ``event_records`` on every run, and ``upsert_event`` overwrites
``issue_codes_json`` on every relabel.  A correction written into either would
survive until the next pipeline run and then vanish without trace — the same
shape as the eligibility floor that a model verdict used to overwrite.  Kept
here, a person's judgement outlives the pipeline, which is the same rule the
review tables follow.

Append-only.  An undo writes ``undone_at`` rather than deleting the row: what
somebody decided, and later changed their mind about, is itself a fact worth
keeping — and it is the only record of why the ledger looks the way it does.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

from api.db import DB_PATH


def connect() -> sqlite3.Connection:
    # Shares the database with the processing layer, so it waits for the write
    # lock rather than failing instantly — same reasoning as review_db.
    db = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    db.execute("PRAGMA busy_timeout=30000")
    db.row_factory = sqlite3.Row
    ensure_schema(db)
    return db


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS issue_adjustments(
        adjustment_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, event_id TEXT NOT NULL,
        from_code TEXT NOT NULL, to_code TEXT, reason TEXT NOT NULL,
        actor TEXT NOT NULL, role TEXT NOT NULL, decided_at TEXT NOT NULL,
        undone_at TEXT, undone_by TEXT)""")
    db.execute("""CREATE INDEX IF NOT EXISTS issue_adjustments_event
                  ON issue_adjustments(event_id, from_code)""")
    db.commit()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def adjustment_id(event_id: str, from_code: str, ordinal: int) -> str:
    """Stable, and unique per decision about the same event and code.

    Derived from a timestamp at first, which collides: ``decided_at`` has second
    precision, so a reviewer who moved an event and immediately corrected
    themselves got a UNIQUE violation and a 500.  The ordinal is how many
    decisions have already been made about this pair, which is both collision-
    free and readable — 「这是关于这条事件的第 2 个决定」.
    """
    return hashlib.sha256(
        f"issue-adj\0{event_id}\0{from_code}\0{ordinal}".encode()).hexdigest()[:20]


def record(db: sqlite3.Connection, *, company_id: str, event_id: str, from_code: str,
           to_code: str | None, reason: str, actor: str, role: str) -> str:
    """Log one reassignment and retire whatever was active for the same event.

    Superseding rather than editing keeps the chain readable: an event moved
    twice has two rows, and the ledger is explained by the last of them.

    **Does not commit.**  A 合并 is many of these plus a ledger rebuild, and all
    of it has to land or none of it: the first version committed per row, and a
    rebuild that then failed left seven events moved, the ledger describing the
    world before the move, and no error the reviewer could act on.
    """
    decided_at = now()
    db.execute("""UPDATE issue_adjustments SET undone_at=?, undone_by=?
                  WHERE event_id=? AND from_code=? AND undone_at IS NULL""",
               (decided_at, actor, event_id, from_code))
    ordinal = db.execute("""SELECT count(*) FROM issue_adjustments
                            WHERE event_id=? AND from_code=?""",
                         (event_id, from_code)).fetchone()[0]
    identifier = adjustment_id(event_id, from_code, ordinal)
    db.execute("""INSERT INTO issue_adjustments
        (adjustment_id,company_id,event_id,from_code,to_code,reason,actor,role,decided_at)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        (identifier, company_id, event_id, from_code, to_code, reason, actor, role, decided_at))
    return identifier


def undo(db: sqlite3.Connection, adjustment_id_value: str, actor: str) -> bool:
    changed = db.execute("""UPDATE issue_adjustments SET undone_at=?, undone_by=?
                            WHERE adjustment_id=? AND undone_at IS NULL""",
                         (now(), actor, adjustment_id_value)).rowcount
    return bool(changed)


def active(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute("""SELECT * FROM issue_adjustments WHERE undone_at IS NULL
                         ORDER BY decided_at DESC""").fetchall()


def history(db: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM issue_adjustments ORDER BY decided_at DESC LIMIT ?",
                      (limit,)).fetchall()
