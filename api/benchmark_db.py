"""Read-only access to the evaluation database.

Mirrors ``api/db.py`` exactly, including the read-only URI: the evaluation
results are produced by ``processor.benchmark`` and the API's job is to show
them, never to recompute or amend one.

It is a *separate file* from the analytical database and may legitimately be
absent — the deploy package ships the corpus, not the benchmark. So every caller
has to cope with "not here", and `available()` is how they ask.
"""
from __future__ import annotations

import sqlite3

from processor.benchmark import BENCHMARK_DB

DB_PATH = BENCHMARK_DB


def available() -> bool:
    return DB_PATH.exists()


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db
