"""Corpus-level facts the UI needs to describe its own coverage honestly."""
from __future__ import annotations

from typing import Any

import yaml

from fastapi import APIRouter, Depends

from api.db import has_column, has_table, json_field, schema_gaps
from api.routes.claims import unverifiable_breakdown
# The display names live with the extractor, so the page and the parser
# cannot drift apart about what a stored metric name means.
from processor.financials import METRIC_LABELS as FINANCIAL_METRIC_LABELS
# 判据文本只有一份，就在处理层。api.labels 里放的是显示名（抄一份 + 漂移测试是合理的
# 交易），判据是承重文本，抄第二份正是这次要修的病。pipeline 只依赖标准库与 yaml，
# 不会把 openai 带进 web 进程——这也是 MODE_GUIDE 与 TONE_GUIDE 搬来这里的原因。
def _guide_lines(text: str) -> dict[str, str]:
    """Split a ``- CODE 定义`` guide block into per-code definitions.

    ``MODE_GUIDE`` and the two impact guides are written as prompt text because
    that is where they are used; the vocabulary page needs the same sentences per
    code, and re-typing them here is what put two copies of the issue-code rules
    in the tree for two days.
    """
    guides: dict[str, str] = {}
    code = None
    for line in text.splitlines():
        if line.startswith("- "):
            head, _, rest = line[2:].partition(" ")
            code, guides[head] = head, rest.strip()
        elif code and line.strip():
            guides[code] = f"{guides[code]} {line.strip()}"
    return guides


from processor.pipeline import (
    CODE_GUIDE, EVENT_TYPE_GUIDE, IMPACT_DIRECTION_GUIDE, IMPACT_MAGNITUDE_GUIDE,
    IMPACT_OBJECT_GROUPS, IMPACT_OBJECT_GUIDE, MODE_GUIDE, TONE_GUIDE,
    company_display_name, configured_companies, event_type_rules, impact_object_rules,
    issue_code_rules, load_config, risk_bearing_exists,
)

# vocabulary key -> (每个取值的定义, 判定规则, 分组)。判定规则是「什么情况下不要用
# 这个类」那一半，与定义分开写——这个双层写法是议题码那次量过收益的形态
# （交付物11 §4.5-4.6：0.1789 → 0.3997 → 0.4211）。
VOCABULARY_GUIDES: dict[str, tuple[dict[str, str] | None, str | None, tuple | None]] = {
    "event_type": (EVENT_TYPE_GUIDE, event_type_rules(max_types=4), None),
    "issue_code": (CODE_GUIDE, issue_code_rules(max_codes=3), None),
    "impact_object": (IMPACT_OBJECT_GUIDE, impact_object_rules(), IMPACT_OBJECT_GROUPS),
    "production_mode": (_guide_lines(MODE_GUIDE), None, None),
    "impact_direction": (_guide_lines(IMPACT_DIRECTION_GUIDE), None, None),
    "impact_magnitude": (_guide_lines(IMPACT_MAGNITUDE_GUIDE), None, None),
}
from api.labels import VOCABULARIES
from api.routes.events import db_session
from collector.collect import ROOT

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/health")
def health(db=Depends(db_session)) -> dict[str, Any]:
    """登录前就能访问的那一个接口：只说平台在不在、有哪几家公司。

    此前这里还带着模型网关的供应商与异常原文、缺表缺列和修复命令——不登录就能读到
    （I-06 全平台排查）。那些移到需要登录的 ``/api/system/status``，并且只给结论。
    """
    return {"status": "ok",
            "events": db.execute("SELECT count(*) FROM event_records").fetchone()[0],
            # 公司名从配置来，由这里交给前端。放在健康检查而不是 /companies，是因为登录页
            # 在登录之前就要用：它本来就把这两个名字公开印在封面上。
            "companies": [{"company_id": company, "name": company_display_name(company),
                           "canonical_name": ((load_config().get("entities") or {})
                                              .get(company) or {}).get("canonical_name")}
                          for company in configured_companies()]}


@router.get("/system/status")
def system_status(db=Depends(db_session)) -> dict[str, Any]:
    """登录后横幅用的系统状态：只有结论，不带供应商、异常文本、表名或命令。

    缺了哪张表、该跑哪条命令是运维的事，平台启动时已经打印在服务器日志里。
    """
    service = None
    if has_table(db, "model_gateway_state"):
        row = db.execute("SELECT checked_at, ok FROM model_gateway_state WHERE id=1").fetchone()
        service = {"ok": bool(row["ok"]), "checked_at": row["checked_at"]} if row else None
    return {"analysis_service": service, "needs_upgrade": bool(schema_gaps(db))}


@router.get("/overview")
def overview(db=Depends(db_session)) -> dict[str, Any]:
    def scalar(query: str) -> int:
        return db.execute(query).fetchone()[0]

    companies = db.execute("""SELECT e.company_id,
               count(*) AS events,
               sum(CASE WHEN e.needs_review=1 THEN 1 ELSE 0 END) AS needs_review,
               (SELECT count(*) FROM evidence_paragraphs p WHERE p.company_id=e.company_id) AS evidence_paragraphs
        FROM event_records e GROUP BY e.company_id ORDER BY events DESC""").fetchall()
    # "New this round" only means something next to the round it refers to.
    round_row = db.execute("""SELECT ordinal, started_at, reference_time, event_count,
               new_event_count, is_baseline FROM timeline_rounds ORDER BY ordinal DESC LIMIT 1""").fetchone()
    return {
        "events": scalar("SELECT count(*) FROM event_records"),
        "claims": scalar("SELECT count(*) FROM event_claims"),
        "verdicts": scalar("SELECT count(*) FROM claim_verdicts"),
        "issues": scalar("SELECT count(*) FROM issue_ledger"),
        "diffs": scalar("SELECT count(*) FROM disclosure_diffs"),
        "documents": scalar("SELECT count(*) FROM document_versions"),
        "alerts": scalar("SELECT count(*) FROM alerts WHERE status='open'"),
        "high_alerts": scalar("SELECT count(*) FROM alerts WHERE status='open' AND risk_level='high'"),
        "propagation_chains": scalar(
            "SELECT count(*) FROM propagation_components WHERE member_count > 1"),
        # 复核队列此前在页面上写死成「300 条、人工判定 0 条」——队列实际是 297 条，而
        # 「0 条」一旦有人开判就成了假话。两个数都从表里读。
        "review": ({"open": scalar("SELECT count(*) FROM review_tasks WHERE status='open'"),
                    "decided": scalar("SELECT count(*) FROM review_tasks WHERE status<>'open'")}
                   if has_table(db, "review_tasks") else {"open": 0, "decided": 0}),
        "round": {**dict(round_row), "is_baseline": bool(round_row["is_baseline"])} if round_row else None,
        "companies": [{
            **dict(row),
            # A company with no indexed filings cannot have its claims checked;
            # saying so here stops the UI from implying a verification failure.
            "verifiable": bool(row["evidence_paragraphs"]),
        } for row in companies],
    }


@router.get("/overview/pipeline")
def overview_pipeline(db=Depends(db_session)) -> dict[str, Any]:
    """The six tasks of the brief, each with the number that shows it ran.

    The landing page used to be eight counters and a paragraph, which says the
    platform holds data but not that it does anything with it.  This returns one
    card per task -- what went in, what came out, and the shape of the middle --
    so the first screen answers "what does this system do" rather than "how many
    rows does it have".

    Every figure is aggregated here in SQL.  The page's own promise is that it
    does no arithmetic of its own, and a number computed in the client would be
    a second definition of that number.
    """
    def rows(query: str, parameters: tuple = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in db.execute(query, parameters).fetchall()]

    def scalar(query: str, table: str | None = None) -> int:
        if table and not has_table(db, table):
            return 0
        return db.execute(query).fetchone()[0]

    def staged(table: str, query: str, column: str | None = None) -> list[dict[str, Any]]:
        """A breakdown, or nothing at all if the stage that writes it never ran.

        This is the landing page's main panel and it read six tables without
        checking that any of them existed. A stage that has not run leaves its
        table absent — and a table can also be present but older than the column
        being read, which is how `inclusion_status` broke it. A card with no
        number is a state the page can render; a 500 on the first screen is not.
        """
        if column and not has_column(db, table, column):
            return []
        if not column and not has_table(db, table):
            return []
        return rows(query)

    inclusion = staged("processed_documents", """SELECT inclusion_status AS value, count(*) AS count
        FROM processed_documents GROUP BY 1 ORDER BY count DESC""", column="inclusion_status")
    elements = staged("event_elements", """SELECT status AS value, count(*) AS count
        FROM event_elements GROUP BY 1 ORDER BY count DESC""")
    verdicts = staged("claim_verdicts", """SELECT verdict AS value, count(*) AS count
        FROM claim_verdicts GROUP BY 1 ORDER BY count DESC""")
    # Cluster sizes are a long tail -- 188 pairs against one cluster of 209 --
    # so the buckets are reported raw and the client decides how to band them.
    cluster_sizes = staged("duplicate_clusters", """SELECT member_count AS members,
        count(*) AS clusters FROM duplicate_clusters GROUP BY 1 ORDER BY members""")
    levels = staged("alerts", """SELECT risk_level AS value, count(*) AS count FROM alerts
        WHERE status='open' GROUP BY 1""")
    months = staged("event_timeline", """SELECT substr(timeline_at, 1, 7) AS month,
        count(*) AS count FROM event_timeline
        WHERE timeline_at >= date('now', '-24 months') GROUP BY 1 ORDER BY month""")

    documents = scalar("SELECT count(*) FROM document_versions", "document_versions")
    clustered = scalar("SELECT count(*) FROM duplicate_cluster_members",
                       "duplicate_cluster_members")
    clusters = scalar("SELECT count(*) FROM duplicate_clusters", "duplicate_clusters")
    # 议题热度 Top 10, ordered here rather than in the browser. The page fetched
    # a page of issues and sorted it client-side, which was correct only while
    # the ledger stayed smaller than the page size: /api/issues orders by risk
    # score, so past 100 issues the busiest ones could fall off the page before
    # the sort ever saw them. It is also the page's own stated rule — every
    # aggregate in SQL, because a number computed in the client is a second
    # definition of that number.
    evidence_expr = ("(SELECT count(*) FROM evidence_paragraphs p "
                     "WHERE p.company_id=e.company_id)"
                     if has_table(db, "evidence_paragraphs") else "0")
    high_alert_expr = ("(SELECT count(*) FROM alerts a WHERE a.company_id=e.company_id "
                       "AND a.status='open' AND a.risk_level='high')"
                       if has_table(db, "alerts") else "0")
    companies_query = (
        "SELECT e.company_id, count(*) AS events, "
        "sum(CASE WHEN e.needs_review=1 THEN 1 ELSE 0 END) AS needs_review, "
        f"{evidence_expr} AS evidence_paragraphs, {high_alert_expr} AS high_alerts "
        "FROM event_records e GROUP BY e.company_id ORDER BY events DESC")

    def rate(numerator: int, denominator: int, label: str, basis: str) -> dict[str, Any]:
        """一张卡的质量率：分子分母一起送出去，页面画条、写口径，但不自己除。

        分母为 0 时 value 是 None——「还没有数据」与「比例为 0」是两件事。
        """
        return {"value": round(numerator / denominator, 4) if denominator else None,
                "numerator": numerator, "denominator": denominator,
                "label": label, "basis": basis}

    def share(breakdown: list[dict[str, Any]], wanted) -> tuple[int, int]:
        total = sum(row["count"] for row in breakdown)
        return sum(row["count"] for row in breakdown if wanted(row["value"])), total

    events_total = scalar("SELECT count(*) FROM event_records")
    with_issue = scalar("""SELECT count(*) FROM event_records
        WHERE issue_codes_json IS NOT NULL AND issue_codes_json NOT IN ('[]', '')""")
    rates = {
        "identify": rate(*share(inclusion, lambda value: value == "included"),
                         "入库率", "有效入库 / 采集文档"),
        "extract": rate(*share(elements, lambda value: value == "normalized"),
                        "标准化率", "已标准化要素 / 全部要素"),
        "verify": rate(*share(verdicts, lambda value: value != "UNVERIFIABLE"),
                       "可核验率", "非「无法核验」裁决 / 全部裁决"),
        "dedupe": rate(clustered, documents, "归并率", "进入同源簇的版本 / 全部文档版本"),
        "trace": rate(with_issue, events_total, "专题覆盖", "挂到专题的事件 / 全部事件"),
        "alert": rate(*share(levels, lambda value: value == "high"),
                      "高风险占比", "高风险 / 未处置预警"),
    }

    top_issues = staged("issue_ledger",
        "SELECT issue_id, issue_code, company_id, event_count, risk_level, status "
        "FROM issue_ledger ORDER BY event_count DESC, issue_code LIMIT 10")
    result = {
        "top_issues": top_issues,
        # 流水线条上第一个节点「采集文档」的数；其余五个节点就是各卡的头条数字。
        "documents": documents,
        "tasks": [
            {"ordinal": 1, "key": "identify", "title": "多源金融事件识别", "link": "/ingestion",
             "headline": {"value": scalar("SELECT count(*) FROM event_records"), "label": "事件"},
             "breakdown": inclusion,
             "note": f"{documents} 份文档版本经入库筛选后产出事件"},
            {"ordinal": 2, "key": "extract", "title": "要素抽取与标准化", "link": "/elements",
             "headline": {"value": scalar("SELECT count(*) FROM event_elements", "event_elements"), "label": "要素"},
             "breakdown": elements,
             "note": "normalized 与 unresolved 的比例就是标准化覆盖率"},
            {"ordinal": 3, "key": "verify", "title": "可信度评估与辨伪", "link": "/claims",
             "headline": {"value": scalar("SELECT count(*) FROM event_claims"), "label": "原子主张"},
             "breakdown": verdicts,
             "note": "非「无法核验」的裁决都带页码"},
            {"ordinal": 4, "key": "dedupe", "title": "同源事件聚合与去重", "link": "/duplicates",
             "headline": {"value": clusters, "label": "同源簇"},
             "breakdown": [{"value": str(row["members"]), "count": row["clusters"]} for row in cluster_sizes],
             "note": f"{clustered} 份版本归并为 {clusters} 簇"},
            {"ordinal": 5, "key": "trace", "title": "事件脉络追踪", "link": "/timeline",
             "headline": {"value": scalar("SELECT count(*) FROM issue_ledger", "issue_ledger"), "label": "专题"},
             "breakdown": [{"value": row["month"], "count": row["count"]} for row in months],
             "note": "近 24 个月的事件分布"},
            {"ordinal": 6, "key": "alert", "title": "影响分级与预警推送", "link": "/risks",
             "headline": {"value": scalar("SELECT count(*) FROM alerts WHERE status='open'", "alerts"),
                          "label": "未处置预警"},
             "breakdown": levels,
             "note": "等级由风险分与披露阶段系数共同决定"},
        ],
        # Folded in from what the page used to fetch once per company. The two
        # correlated subqueries are substituted with 0 when their stage has not
        # run, rather than being dropped -- the column stays in the shape the
        # page reads, and 「还没有证据段落」 renders as 0 instead of as a gap.
        "companies": [{
            **dict(row),
            "verifiable": bool(row["evidence_paragraphs"]),
        } for row in db.execute(companies_query)],
        "tiers": staged("document_versions", """SELECT source_tier AS value, count(*) AS count
            FROM document_versions WHERE source_tier IS NOT NULL GROUP BY 1 ORDER BY value"""),
    }
    for task in result["tasks"]:
        task["rate"] = rates[task["key"]]
    # 「可信度评估」那张卡上，无法核验不再是一个数：它拆成设计边界与可改进两组，
    # 与主张核验页用的是同一个函数。
    by_key = {task["key"]: task for task in result["tasks"]}
    by_key["verify"]["unverifiable"] = unverifiable_breakdown(db, "", [])
    return result


@router.get("/overview/trend")
def overview_trend(months: int = 24, company: str | None = None,
                   db=Depends(db_session)) -> dict[str, Any]:
    """Events per month by risk level, plus the disclosure phases beneath them.

    The window is deliberate and its cost is reported rather than hidden.  The
    corpus runs from 2015 to 2032 -- the far end being a parse artefact, not a
    forecast -- so a chart drawn over the full range is one unreadable spike and
    seventeen years of nothing.  ``out_of_window`` names exactly what a reader
    is not seeing, which is the difference between a window and a silent trim.
    """
    clause, parameters = ("AND t.company_id=?", [company]) if company else ("", [])
    # Bounded at both ends. A lower bound alone lets the 2032 row through, and
    # the chart then draws six empty months at its right edge because of one
    # mis-parsed date -- which reads as "nothing happened recently" rather than
    # as the artefact it is.
    grid = db.execute(f"""SELECT substr(t.timeline_at, 1, 7) AS month,
               t.adjusted_risk_level AS level, count(*) AS count
        FROM event_timeline t
        WHERE t.timeline_at >= date('now', ?) AND t.timeline_at <= date('now') {clause}
          AND {risk_bearing_exists('t')}
        GROUP BY 1, 2 ORDER BY month""", [f"-{int(months)} months", *parameters]).fetchall()

    periods = sorted({row["month"] for row in grid})
    counts: dict[str, dict[str, int]] = {}
    for row in grid:
        counts.setdefault(row["level"] or "unknown", {})[row["month"]] = row["count"]

    outside = db.execute(f"""SELECT
               sum(CASE WHEN t.timeline_at < date('now', ?) THEN 1 ELSE 0 END) AS before,
               sum(CASE WHEN t.timeline_at > date('now') THEN 1 ELSE 0 END) AS after,
               min(t.timeline_at) AS earliest, max(t.timeline_at) AS latest
        FROM event_timeline t WHERE 1=1 {clause}""",
        [f"-{int(months)} months", *parameters]).fetchone()

    market_data = db.execute(f"""SELECT count(*) FROM event_timeline t
        WHERE t.timeline_at >= date('now', ?) AND t.timeline_at <= date('now') {clause}
          AND NOT {risk_bearing_exists('t')}""",
        [f"-{int(months)} months", *parameters]).fetchone()[0]

    phases = [dict(row) for row in db.execute(f"""SELECT company_id, phase, phase_label,
               coefficient, started_at, ended_at FROM disclosure_phases
        WHERE 1=1 {clause.replace('t.company_id', 'company_id')}
        ORDER BY started_at""", parameters).fetchall()]

    return {
        "months": periods,
        "series": [{"key": level, "points": [counts.get(level, {}).get(month, 0) for month in periods]}
                   for level in ("high", "medium", "low") if level in counts],
        "phases": phases,
        # Named rather than trimmed: a reader is owed the count of what the
        # window excludes, and the far end of the range is a parse artefact the
        # interface should point at instead of quietly drawing.
        "out_of_window": {"before": outside["before"] or 0, "after": outside["after"] or 0,
                          "earliest": outside["earliest"], "latest": outside["latest"]},
        # 同样是「命名而不是裁掉」：纯行情数据（事件类型层判为一类都不贴合）不进风险分布，
        # 但读者有权知道被排除了多少条，否则这条曲线的分母是悄悄变过的。
        "excluded_market_data": market_data,
    }


@router.get("/evidence/{version_id}/page/{page}")
def evidence_page(version_id: str, page: int, db=Depends(db_session)) -> dict[str, Any]:
    """Every paragraph indexed from one page, so a citation can be opened in place."""
    version = db.execute("""SELECT v.title, v.document_type, v.canonical_url, p.page_count
        FROM document_versions v JOIN processed_documents p ON p.version_id=v.version_id
        WHERE v.version_id=?""", (version_id,)).fetchone()
    paragraphs = db.execute("""SELECT paragraph_id, section, page, text FROM evidence_paragraphs
        WHERE version_id=? AND page=? ORDER BY paragraph_id""", (version_id, page)).fetchall()
    return {"version_id": version_id, "page": page,
            "document": dict(version) if version else None,
            "paragraphs": [dict(row) for row in paragraphs]}


@router.get("/vocabularies")
def vocabularies(db=Depends(db_session)) -> dict[str, Any]:
    """The four controlled vocabularies, with how often each code is actually used.

    M0-4 asked for vocabulary management; this is the read-only half of it, and
    the half that matters for reading the platform: a code nobody uses and a
    code used 900 times look identical in a configuration file, and a value in
    the data that is not in the vocabulary is a defect that has to be visible
    rather than rendered as itself.
    """
    def usage(table: str, column: str) -> dict[str, int]:
        if column.endswith("_json"):
            counts: dict[str, int] = {}
            for (blob,) in db.execute(f"SELECT {column} FROM {table}"):
                for value in json_field(blob) or []:
                    counts[value] = counts.get(value, 0) + 1
            return counts
        return {str(row[0]): row[1] for row in db.execute(
            f"SELECT {column}, count(*) FROM {table} WHERE {column} IS NOT NULL GROUP BY 1")}

    result = {}
    for key, vocabulary in VOCABULARIES.items():
        counts = usage(*vocabulary["used_in"])
        declared = vocabulary["labels"]
        definitions, judging_rules, groups = VOCABULARY_GUIDES.get(key, (None, None, None))
        definitions = definitions or {}
        group_of = {name: group for group, members in (groups or ()) for name in members}
        result[key] = {
            "title": vocabulary["title"],
            # 每个码的定义照给——读者要弄清「技术水平和业务拓展怎么分」。但存在哪张表哪一列、
            # 写给研判程序的判定规则原文不对外（I-06）。
            "groups": [{"group": group, "codes": list(members)} for group, members in (groups or ())] or None,
            "terms": [{"code": code, "label": label, "used": counts.get(code, 0),
                       "definition": definitions.get(code), "group": group_of.get(code)}
                      for code, label in declared.items()],
            # A value in the data outside the vocabulary is a defect: either the
            # extractor invented a code or the vocabulary changed under stored
            # rows.  Either way it is shown, not silently displayed as itself.
            "undeclared": sorted({code: count for code, count in counts.items()
                                  if code not in declared}.items()),
        }
    return result


@router.get("/financials")
def financials(db=Depends(db_session)) -> dict[str, Any]:
    """主要会计数据, lifted out of the periodic reports the corpus already held.

    可信度评估 has four inputs and every one is about the document — who
    published it, how independent the sources are, how the piece was produced,
    what the verdicts said.  Nothing in the system knew anything about the
    company underneath, while 27 periodic reports sat in the corpus with their
    headline figures on page three.

    Each figure carries the filing it came from and the period *that filing*
    covers, never the date it was published: an annual report is filed the
    following March.
    """
    if not has_table(db, "financial_indicators"):
        return {"available": False,
                "reason": "尚未抽取定期报告的主要会计数据"}
    rows = [dict(row) for row in db.execute("""SELECT company_id, period, period_type, metric,
        value, unit, source_title, published_at FROM financial_indicators
        ORDER BY company_id, period, metric""")]
    periods: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["company_id"], row["period"])
        entry = periods.setdefault(key, {
            "company_id": row["company_id"], "period": row["period"],
            "period_type": row["period_type"], "source_title": row["source_title"],
            "published_at": row["published_at"], "metrics": {}})
        entry["metrics"][row["metric"]] = {"value": row["value"], "unit": row["unit"]}
    return {
        "available": True,
        "labels": FINANCIAL_METRIC_LABELS,
        # Sorted so a year sits before the periods inside it — 2025, 2025Q1,
        # 2025H1, 2025Q3 — which is how the table reads down the page.
        "items": sorted(periods.values(), key=lambda row: (row["company_id"], row["period"])),
        "periods": len(periods),
        "figures": len(rows),
    }


@router.get("/companies")
def companies(db=Depends(db_session)) -> dict[str, Any]:
    """Company master data as configured, next to what was actually collected.

    M0-3 wanted a管理界面; what a reader needs first is the truth about scope —
    which companies this deployment covers, which sources are configured for
    each, and how much of it reached the corpus.
    """
    # 解析后的配置来自 pipeline 的缓存：这一句此前占了本接口 246ms 中的 215ms。
    config = load_config()
    configured = config["scope"]["companies"]
    sources: dict[str, list[dict[str, Any]]] = {company: [] for company in configured}
    for source in config.get("sources", []):
        for company in source.get("company_scope") or []:
            sources.setdefault(company, []).append(
                {"source_id": source.get("source_id"), "name": source.get("name"),
                 "priority": source.get("priority"), "source_tier": source.get("source_tier"),
                 "document_types": source.get("document_types") or []})
    rows = {row["company_id"]: dict(row) for row in db.execute("""SELECT company_id,
            count(*) AS events,
            (SELECT count(*) FROM evidence_paragraphs p WHERE p.company_id=e.company_id)
                AS evidence_paragraphs
        FROM event_records e GROUP BY company_id""")}
    return {"last_reviewed": config.get("last_reviewed"),
            "companies": [{"company_id": company,
                           "sources": sorted(sources.get(company, []),
                                             key=lambda item: str(item["source_id"])),
                           **rows.get(company, {"events": 0, "evidence_paragraphs": 0})}
                          for company in configured]}


# 六步各自在做什么，以及读者在那一页最容易读错的那件事。文本放在这里而不是页面上，
# 与上面六张任务卡的 title/note 同一处：同一句话在前后端各存一份，就是下一次改口径
# 时只改到其中一份的那个缺陷。
GUIDE_STEPS: tuple[dict[str, str], ...] = (
    {"key": "identify", "title": "多源金融事件识别",
     "does": "一篇公开稿件经相关性与事件性筛选后成为一条事件；原文与它的 SHA-256 留在证据层，可以重算但不可覆盖。",
     "caution": "被筛掉的部分同样计数并可查，不是静默丢弃。"},
    {"key": "extract", "title": "要素抽取与标准化",
     "does": "把主体、时间、金额、比例这些字段从正文里取出来，再归一成可比的值。",
     "caution": "每个要素都记着归一依据；没解析成功的那些照样留在表里，不拿默认值顶替。"},
    {"key": "verify", "title": "可信度评估与辨伪",
     "does": "事件拆成能单独判真伪的原子主张，逐条与权威披露原文比对，给出四值裁决。",
     "caution": "非「无法核验」的裁决都指向某份文件的第几页；没有页码的结论不会被写入。"},
    {"key": "dedupe", "title": "同源事件聚合与去重",
     "does": "同一篇稿的多处出现先归并成一个簇，转载与改写不再各算一次。",
     "caution": "独立信源数是可信度评估的分母，所以它算的是声音数，不是文章数。"},
    {"key": "trace", "title": "事件脉络追踪",
     "does": "这条事件挂进它所属的专题，与此前同专题的事件排成一条时间线。",
     "caution": "专题是（公司，专题码）二元组，是平台的核心业务对象，不是工单。"},
    {"key": "alert", "title": "影响分级与预警推送",
     "does": "风险分乘披露阶段系数得到修正分，越过闸门才成为一条预警。",
     "caution": "风险与可信度是两个数，不合成一个——高风险、低可信正是最该先去核实的那一类。"},
)


@router.get("/guide")
def guide(db=Depends(db_session)) -> dict[str, Any]:
    """一条六步都走得通的样例事件，请求时从库里当场挑，不写死 id。

    写死 id 的代价这个仓库付过：`screenshots.mjs` 的注释记着「写死的 id 会随下一次
    重跑处理层失效，那时脚本会静默截出一张 404」。指南页比截图脚本更经不起这个——
    它是第一次来的人唯一会把每条链接都点一遍的页面。

    挑选从 `alerts`（277 行）驱动，不从 `event_records`（1,907 行）。后者写成相关子
    查询时 SQLite 会为每条候选重扫 `claim_verdicts` 里 1,762 行 SUPPORTED，而那张表
    带着 evidence_text 这列胖字段：实测 14.6 秒，物化成 CTE 后 92 毫秒。

    每一步各自守住自己的表。某个阶段没跑过时，是**那一步**降级成不可点并说明原因，
    整条路径照常返回——与 `overview_pipeline` 的 `staged()` 同一条规矩：一张缺表可以
    渲染成一句话，不可以渲染成首屏 500。
    """
    have = {name: has_table(db, name) for name in (
        "alerts", "event_records", "event_elements", "event_claims", "claim_verdicts",
        "duplicate_cluster_members", "issue_ledger", "propagation_component_members")}

    # 「这一步有东西可看」对核验而言是：既判了 SUPPORTED，又拿得出页码。
    grounded = ("""SELECT c.event_id, min(cv.claim_id) AS claim_id
                   FROM claim_verdicts cv JOIN event_claims c ON c.claim_id=cv.claim_id
                   WHERE cv.verdict='SUPPORTED' AND cv.evidence_page IS NOT NULL
                   GROUP BY c.event_id"""
                if have["claim_verdicts"] and have["event_claims"]
                else "SELECT NULL AS event_id, NULL AS claim_id WHERE 0")
    cluster_expr = ("""(SELECT m.cluster_id FROM duplicate_cluster_members m
                        WHERE m.version_id=e.version_id ORDER BY m.cluster_id LIMIT 1)"""
                    if have["duplicate_cluster_members"] else "NULL")
    element_expr = ("(SELECT count(*) FROM event_elements x WHERE x.event_id=e.event_id)"
                    if have["event_elements"] else "0")
    issue_expr = ("""(SELECT i.issue_id FROM issue_ledger i
                      WHERE i.company_id=a.company_id AND i.issue_code=a.issue_code)"""
                  if have["issue_ledger"] else "NULL")

    # 缺表的那一步不参与筛选：否则「传播这一步没跑过」会让整条路径一条都挑不出来。
    wants = []
    if have["event_elements"]:
        wants.append("EXISTS (SELECT 1 FROM event_elements x WHERE x.event_id=e.event_id)")
    if have["duplicate_cluster_members"]:
        wants.append("EXISTS (SELECT 1 FROM duplicate_cluster_members m "
                     "WHERE m.version_id=e.version_id)")
    if have["propagation_component_members"]:
        wants.append("EXISTS (SELECT 1 FROM propagation_component_members p "
                     "WHERE p.version_id=e.version_id)")
    if have["claim_verdicts"] and have["event_claims"]:
        wants.append("g.claim_id IS NOT NULL")

    row, candidates = None, 0
    if have["alerts"] and have["event_records"]:
        where = f" WHERE {' AND '.join(wants)}" if wants else ""
        body = f"""FROM alerts a JOIN event_records e ON e.event_id=a.lead_event_id
                   LEFT JOIN ({grounded}) g ON g.event_id=e.event_id{where}"""
        # 排序只用内容决定的两列。`updated_at` 会随 processor.alerts 每次重跑而变，
        # 拿它排序等于让「同一份库」在两次刷新之间换掉样例。
        row = db.execute(f"""SELECT a.alert_id, a.company_id, a.issue_code, a.headline,
                   a.risk_level, e.event_id, e.version_id, e.summary,
                   g.claim_id, {element_expr} AS element_count,
                   {cluster_expr} AS cluster_id, {issue_expr} AS issue_id
                   {body} ORDER BY a.adjusted_risk_score DESC, a.alert_id LIMIT 1""").fetchone()
        candidates = db.execute(f"SELECT count(*) {body}").fetchone()[0]

    def step(index: int, available: Any, to: str, fallback: str, reason: str) -> dict[str, Any]:
        """一步。走得通就给它的落点，走不通就给整页的入口并说出为什么。

        降级成列表页而不是消掉这一步：读者数得出来这条路径有六步，缺哪一步是信息。
        """
        text = GUIDE_STEPS[index]
        return {"ordinal": index + 1, **text,
                "available": bool(available), "to": to if available else fallback,
                "unavailable_reason": None if available else reason}

    steps: list[dict[str, Any]] = []
    if row is not None:
        steps = [
            step(0, True, f"/events/{row['event_id']}", "/events", ""),
            step(1, row["element_count"], f"/elements?event={row['event_id']}", "/elements",
                 "要素抽取尚未在这条事件上产出结果。"),
            step(2, row["claim_id"], f"/claims?event={row['event_id']}", "/claims",
                 "这条事件还没有带页码的裁决。"),
            step(3, row["cluster_id"], f"/duplicates?cluster={row['cluster_id']}", "/duplicates",
                 "同源聚类尚未运行，或这份文档不在任何簇里。"),
            step(4, row["issue_id"], f"/issues/{row['issue_id']}", "/issues",
                 "专题台账尚未运行。"),
            step(5, True, f"/risks/{row['alert_id']}", "/risks", ""),
        ]
    else:
        # 没有样例时六步仍然列出，只是每一步都指向它自己的整页。读者仍然读得到这套
        # 系统分几步、每步在做什么——那部分不依赖任何一条记录。
        pages = ("/events", "/elements", "/claims", "/duplicates", "/issues", "/alerts")
        steps = [step(index, False, "", pages[index],
                      "本部署还没有一条六步齐全的事件可用作样例。")
                 for index in range(len(GUIDE_STEPS))]

    config = load_config()
    # 语料上限晚于今天是发布时间解析异常，不是预告。驾驶舱的趋势脚注已经点名了这
    # 件事；这一页把同一个区间当成事实平铺出去，就等于替它作了担保。
    span = db.execute("SELECT min(timeline_at) AS earliest, max(timeline_at) AS latest, "
                      "max(timeline_at) > date('now') AS latest_is_future "
                      "FROM event_timeline").fetchone() if has_table(db, "event_timeline") else None
    return {
        "sample": ({"event_id": row["event_id"], "summary": row["summary"],
                    "company_id": row["company_id"], "issue_code": row["issue_code"],
                    "risk_level": row["risk_level"], "headline": row["headline"]}
                   if row is not None else None),
        # 样例是从多少条里挑出来的。一条被挑中的记录不说明选材有多宽，这个数说明。
        "candidates": candidates,
        "steps": steps,
        "boundary": {
            "companies": config["scope"]["companies"],
            "events": db.execute("SELECT count(*) FROM event_records").fetchone()[0],
            "earliest": span["earliest"] if span else None,
            "latest": span["latest"] if span else None,
            "latest_is_future": bool(span["latest_is_future"]) if span else False,
        },
    }
