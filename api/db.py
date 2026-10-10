"""Read-only access to the analytical database.

The API never writes.  Every business judgement — eventhood, normalization,
clustering, adjudication — already happened in ``processor``; recomputing any of
it here would create a second definition of the same number.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Sequence

from zhconv import convert

from collector.collect import ROOT

DB_PATH = ROOT / "data" / "state" / "collector.sqlite3"


def connect() -> sqlite3.Connection:
    # A read-only URI makes the guarantee structural rather than a convention.
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


def json_field(value: Any, default: Any = None) -> Any:
    """Decode a stored JSON column, tolerating rows written before a field existed."""
    if not value:
        return default if default is not None else []
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default if default is not None else []


# SQLite has no default escape character for LIKE; a route that wants one has
# to say so on every pattern.
LIKE_ESCAPE = "\\"
_LIKE_SPECIAL = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})


def like_patterns(text: str) -> list[str]:
    """The patterns one search term needs: escaped, and in both Chinese scripts.

    Two defects sat in every search box on the platform until this existed.

    A term containing ``%`` or ``_`` was read as a wildcard, because no route
    passed an ESCAPE clause — searching 「50%」 returned 419 claims where 16
    actually contain that string, the other 403 being 「50」 followed by
    anything at all.

    And half the WuXi AppTec corpus is Hong Kong originals written in
    traditional Chinese: 785 document titles say 药明康德 and another 122 say
    藥明康德, and neither spelling could find the other.
    ``processor.claim_index.fold`` folds the scripts together on the indexing
    side; a LIKE has no index to fold, so it asks for both spellings instead.
    """
    escaped = text.translate(_LIKE_SPECIAL)
    # The term itself is kept alongside both folds: for mixed-script or partly
    # Latin input neither fold equals what was typed.
    variants = dict.fromkeys([escaped, convert(escaped, "zh-cn"), convert(escaped, "zh-hant")])
    return [f"%{variant}%" for variant in variants]


def like_clause(columns: Sequence[str], text: str) -> tuple[str, list[str]]:
    """One search term against several columns, as a WHERE fragment and its parameters."""
    patterns = like_patterns(text)
    tests = [f"{column} LIKE ? ESCAPE '{LIKE_ESCAPE}'" for _ in patterns for column in columns]
    return f"({' OR '.join(tests)})", [pattern for pattern in patterns for _ in columns]


def paginate(query: str, parameters: list[Any], limit: int, offset: int) -> tuple[str, list[Any]]:
    return f"{query} LIMIT ? OFFSET ?", [*parameters, limit, offset]


def total_of(db: sqlite3.Connection, query: str, parameters: list[Any]) -> int:
    """Count the rows a filtered query would return, without fetching them."""
    return db.execute(f"SELECT count(*) FROM ({query})", parameters).fetchone()[0]


# 平台以**只读**方式打开数据库（上面的 mode=ro），所以它永远不会、也不该自己建表建列。
# 代价是「新代码 + 旧库」这件事不会在部署时报错，只会在某个页面上 500——2026-09-07 实测：
# 拿改动前的库跑新代码，事件详情页直接抛 IndexError（缺 risk_breakdown_json 一列）。
# 因此把「这份代码要求库里有什么」写成一张明表，启动时查一遍、健康检查里也报，
# 让它在部署时就说话。新增列/表时要同步加进来。
SCHEMA_EXPECTATIONS: dict[str, tuple[str, ...]] = {
    "event_records": ("risk_breakdown_json", "label_source"),
    "issue_ledger": ("risk_bearing_event_count",),
    # 处置那两张表**不在**这里，尽管风险事项库要在只读连接上 JOIN 它们：这张表
    # 登记的是「只读路径修不了、必须由处理层写进来」的东西。处置表由
    # `dispatch_db.ensure_schema()` 自己建，启动时在 lifespan 里跑一次（main.py），
    # 于是一个全新的库不会在这里报一条会自愈的缺失。
}
#: 缺了不致命（页面显示「尚未覆盖」），但缺了就说明重标没跑过。
OPTIONAL_TABLES = ("event_type_labels", "event_impact_labels",
                   "production_mode_labels", "article_sentiment_labels")


def schema_gaps(db: sqlite3.Connection) -> list[str]:
    """The columns and tables this code reads that the database does not have."""
    gaps = []
    for table, columns in SCHEMA_EXPECTATIONS.items():
        if not has_table(db, table):
            gaps.append(f"缺表 {table}")
            continue
        present = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        gaps.extend(f"{table} 缺列 {column}" for column in columns if column not in present)
    return gaps


def has_table(db: sqlite3.Connection, name: str) -> bool:
    """Whether a table exists — the probe six route modules each wrote out.

    A stage that has not run yet leaves its tables absent, and the pages that
    read them owe the reader a sentence saying so rather than a 500. The check
    is the same everywhere, so it lives here; the *sentence* stays in each
    route, because what is missing differs.
    """
    return bool(db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                           (name,)).fetchone())


def has_column(db: sqlite3.Connection, table: str, column: str) -> bool:
    """Whether a table carries a column.

    A table existing is not the same as a table being current: a database can
    carry an older, narrower version written before a stage learned to record
    something. `api/routes/ingestion.py` knew this; the landing page did not,
    and read `inclusion_status` unguarded until a test asked it to.
    """
    if not has_table(db, table):
        return False
    return any(row["name"] == column for row in db.execute(f"PRAGMA table_info({table})"))


def event_clock_sql(db: sqlite3.Connection, event: str = "e", version: str = "v") -> tuple[str, str]:
    """SQL for an event's sort time and its scheduling role, guarded for old databases.

    A deadline or target still in the future when it was reported sorts by when
    it was reported (I-04): ordered by its own date, a 2032 contract deadline
    headed the event ledger above last week's news.
    """
    if not has_column(db, "event_records", "event_time_role"):
        return f"COALESCE({event}.event_time, {version}.published_at)", "NULL"
    return (f"CASE WHEN {event}.event_time_role='scheduled' THEN {version}.published_at "
            f"ELSE COALESCE({event}.event_time, {version}.published_at) END", f"{event}.event_time_role")
