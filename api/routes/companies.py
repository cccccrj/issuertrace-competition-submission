"""公司档案：一家公司现在有哪些事、风险往哪走、最近正式说了什么、在哪几个专题里、跟谁有关。

**只做详情页，不做列表页。** 库里只有两家公司，一个两行的列表是空壳，还要多点一次才进得去；
入口是事项库、事件库、研判档案里那个公司名。

**不新建聚合**：五块全部从已有的表现取——预警与处置、权威披露（S0 来源的文档）、议题台账、
事件要素里解析出来的机构。

**这个文件里不出现任何一家公司的标识。** 公司是谁、叫什么，只从 `config/sources.yaml` 来；
一个写死的标识，就会让「加第三家公司不改代码」这句话变成假的（`tests/test_migration`
的扫描覆盖到这里）。
"""
from __future__ import annotations

from typing import Any

from api.sources import source_name
from fastapi import APIRouter, Depends, HTTPException

from api import dispositions
from api.db import has_table
from api.labels import ISSUE_CODE_LABELS, ORG_KIND_LABELS
from api.routes.auth import current_session
from api.routes.events import db_session
from api.routes.risks import disposition_join, due_column, has_dispositions, risk_row
from processor.normalize import load_organizations
from processor.pipeline import company_display_name, configured_companies, load_config

router = APIRouter(prefix="/api/companies", tags=["companies"])

#: 档案页每块列几条。多了就不是「概览」，是把事项库、专题台账各抄一遍。
TOP = 8


@router.get("/{company_id}")
def company(company_id: str, db=Depends(db_session),
            session=Depends(current_session)) -> dict[str, Any]:
    if company_id not in configured_companies():
        raise HTTPException(status_code=404, detail="没有这家公司")
    entity = (load_config().get("entities") or {}).get(company_id) or {}
    return {
        "company_id": company_id,
        "name": company_display_name(company_id),
        "canonical_name": entity.get("canonical_name"),
        "stock_codes": entity.get("stock_codes") or [],
        "open": _open_items(db, company_id),
        "trend": _trend(db, company_id),
        "disclosures": _disclosures(db, company_id),
        "issues": _issues(db, company_id),
        "related": _related(db, company_id),
    }


def _open_items(db, company_id: str) -> dict[str, Any]:
    """未结事项：没归档的都算，包括还没人判过的——那些恰恰是最该先看的。"""
    joined = has_dispositions(db)
    status = "coalesce(d.status,'pending')" if joined else "'pending'"
    rows = db.execute(
        "SELECT a.*, v.source_id AS lead_source, v.source_tier AS lead_source_tier,"
        " v.canonical_url AS lead_url"
        + (", d.status AS status_d, d.chosen, d.recommended, d.owner, d.decided_at" if joined else "")
        + due_column(db)
        + " FROM alerts a JOIN document_versions v ON v.version_id=a.lead_version_id"
        + disposition_join(db)
        + f" WHERE a.company_id=? AND {status}<>'archived'"
        + " ORDER BY a.adjusted_risk_score DESC, a.window_end DESC",
        (company_id,)).fetchall()
    items = [risk_row(row) for row in rows]
    by_status: dict[str, int] = {}
    for item in items:
        key = item["disposition"]["status"]
        by_status[key] = by_status.get(key, 0) + 1
    return {
        "total": len(items),
        "high": sum(item["risk_level"] == "high" for item in items),
        "overdue": sum(bool(item["disposition"].get("overdue")) for item in items),
        "by_status": [{"status": key, "label": dispositions.STATUSES[key]["label"],
                       "count": by_status.get(key, 0)}
                      for key in dispositions.STATUSES if key != "archived"],
        "items": items[:TOP],
    }


def _trend(db, company_id: str) -> list[dict[str, Any]]:
    """按窗口结束所在的月份、分风险等级计数。只返回有事项的月份——补零由画图的一侧决定。"""
    rows = db.execute("""SELECT substr(coalesce(window_end, window_start), 1, 7) AS month,
            risk_level, count(*) AS n
        FROM alerts WHERE company_id=? AND coalesce(window_end, window_start) IS NOT NULL
        GROUP BY 1, 2 ORDER BY 1""", (company_id,)).fetchall()
    months: dict[str, dict[str, Any]] = {}
    for row in rows:
        slot = months.setdefault(row["month"], {"month": row["month"], "high": 0, "medium": 0, "low": 0})
        slot[row["risk_level"]] = slot.get(row["risk_level"], 0) + row["n"]
    return list(months.values())


def _disclosures(db, company_id: str) -> dict[str, Any]:
    """最新的权威披露：S0 来源（交易所、监管、公司正式文件）——事实基座，不是媒体。"""
    total = db.execute("SELECT count(*) FROM document_versions WHERE company_id=? AND source_tier='S0'",
                       (company_id,)).fetchone()[0]
    rows = db.execute("""SELECT version_id, title, published_at, source_id, document_type, canonical_url
        FROM document_versions WHERE company_id=? AND source_tier='S0'
        ORDER BY published_at DESC LIMIT ?""", (company_id, TOP)).fetchall()
    # 来源的名字也从配置来。第一版直接印 source_id，读者看到的是「cninfo_announcements」。
    return {"total": total, "items": [{**dict(row), "source_name": source_name(row["source_id"])}
                                      for row in rows]}


def _issues(db, company_id: str) -> list[dict[str, Any]]:
    """重点专题：风险分最高的几个议题。"""
    rows = db.execute("""SELECT issue_id, issue_code, risk_level, risk_score, status,
            event_count, independent_source_count, last_seen_at
        FROM issue_ledger WHERE company_id=? ORDER BY risk_score DESC, event_count DESC LIMIT ?""",
        (company_id, TOP)).fetchall()
    return [{**dict(row), "issue_label": ISSUE_CODE_LABELS.get(row["issue_code"])} for row in rows]


def _related(db, company_id: str) -> list[dict[str, Any]]:
    """关联主体：这家公司的事件里反复出现的**其他**机构——监管、交易所、券商、同业、股东。

    取的是**已解析到机构主数据的事件主体**（`subject` 要素，`unit='org_id'`），不是
    `organization` 要素。第一版取的是后者，截图一看就错了：那一类存的是原文写法、从没做过
    归一——公司自己以三种写法排在前三（「长鑫科技」「长鑫科技集团股份有限公司」「长鑫存储」），
    中金公司和「中国国际金融股份有限公司」各占一行。归一只发生在事件主体上。

    数的是「出现在几个事件里」，不是出现几次：一篇公告里把证监会写十遍，它也只是一件事。
    """
    if not has_table(db, "event_elements"):
        return []  # 要素抽取没跑过的库：没有就是没有，不报错
    names = load_organizations()
    rows = db.execute("""SELECT el.normalized_value AS org_id, count(DISTINCT el.event_id) AS events
        FROM event_elements el JOIN event_records e ON e.event_id=el.event_id
        WHERE e.company_id=? AND el.element_type='subject' AND el.unit='org_id'
          AND el.normalized_value IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT ?""", (company_id, TOP)).fetchall()
    out = []
    for row in rows:
        record = names.get(row["org_id"]) or {}
        out.append({"org_id": row["org_id"], "name": record.get("canonical_name") or row["org_id"],
                    "kind": record.get("kind"), "kind_label": ORG_KIND_LABELS.get(record.get("kind")),
                    "events": row["events"]})
    return out
