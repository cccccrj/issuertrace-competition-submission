"""The inquiry chain, read for whichever page needs it.

Question, answer and disclosure change live in three tables because they are
three different documents.  Joining them belongs in one place: both the claim
page and the event page have to show the same chain, and a reader who is being
told "this was already explained" is owed all three links, not a summary of
them.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from api.db import json_field


def chain(db: sqlite3.Connection, question_id: str | None) -> dict[str, Any] | None:
    if not question_id:
        return None
    question = db.execute("SELECT * FROM inquiry_questions WHERE question_id=?",
                          (question_id,)).fetchone()
    if not question:
        return None
    changes = db.execute("""SELECT t.response_page, t.response_excerpt, t.match_score,
                                   t.shared_terms, d.diff_id, d.section, d.diff_kind, d.pair_key,
                                   d.left_page, d.right_page, d.left_text, d.right_text,
                                   d.old_value, d.new_value, d.metric
        FROM inquiry_triplets t JOIN disclosure_diffs d ON d.diff_id=t.diff_id
        WHERE t.question_id=? ORDER BY t.match_score DESC, d.diff_id""", (question_id,)).fetchall()
    return {**dict(question),
            "changes": [{**dict(change), "shared_terms": json_field(change["shared_terms"])}
                        for change in changes]}
