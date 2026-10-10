"""风险事项：预警加上「谁在管、管到哪一步了」。

一条风险事项就是一条 alert——``processor.alerts`` 按（公司，议题，7 天发布窗口）
把事件合并成的那个对象。这里不新建第二层聚合，只做一件 ``/api/alerts`` 没做的事：
把处置状态接上去。

为什么值得单开一个前缀而不是给 ``/api/alerts`` 加参数：``/api/alerts`` 回答的是
「系统判出了什么」，纯处理层产物，重跑一次就全换一遍；这里回答的是「人拿它怎么办
了」，是人写进来的第二层事实，重跑处理层不能碰。两者的生命周期不同，混在一个端点
里迟早要在参数上打架。数据层是共用的——``BASE_QUERY``、``alert_row``、
``filter_clause`` 都直接从 ``api.routes.alerts`` 取，一份查询不写两遍。

``alert_dispositions`` 由 ``api/dispatch_db.py`` 以可写连接创建（启动时在 lifespan
里建一次），这里以只读连接读它：同一个库文件，ro 连接读得到。表不在时整条链路退回
「全部待研判」而不是 500——一个还没有人处置过的平台，本来就该长这样。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query

from api import dispositions
from api.db import has_column, has_table, json_field, paginate, total_of
from api.labels import ISSUE_CODE_LABELS
from api.routes.alerts import alert_row, filter_clause, public_evidence
from api.sources import source_name
from api.routes.auth import current_session
from api.routes.events import db_session

router = APIRouter(prefix="/api/risks", tags=["risks"])

#: 「异常待核查」的四类，按规格 §3.4。每一类都落到一条已经存在的规则码或字段上，
#: 没有一类是为了凑满四格新造出来的——一个数不出来的分类在页面上只会是个空壳。
ANOMALY_KINDS = {
    "conflict": {"label": "多源证据冲突", "reason": "claim_conflict", "action": "查看冲突事项",
                 "detail": "报道里至少有一条主张与权威披露直接矛盾"},
    "unverified": {"label": "高影响但证据不足", "reason": "unverified_major_rumour", "action": "补充核验",
                   "detail": "风险高、可信度低，且没有独立信源佐证"},
    "thin_source": {"label": "仅转述或简讯来源", "reason": None, "action": "查找首发",
                    "detail": "首发为简讯或转载，且来源等级不属于权威披露"},
    "low_confidence": {"label": "研判置信度偏低", "reason": None, "action": "送标注复核",
                       "detail": "合并进来的事件里有被标为需人工复核的"},
}

#: 每一类异常落到 SQL 上的那一句。首页的计数与事项库的筛选**读同一张表**：此前首页数了
#: 四类，事项库的「核验异常」只筛三类（漏了 thin_source），点进去的条数对不上点之前的数。
#: 用 EXISTS 而不是 JOIN，否则一条命中两类的事项会在列表里出现两次。
#:
#: low_confidence 先用 json_each 拆开事件 id 数组，再按主键查事件。此前写的是拿每条事件去
#: LIKE '%"id"%' 整段 JSON——281 条事项 × 1907 条事件逐个扫字符串，单句 1.5s，首屏连同
#: 「异常合计」要算两遍，风险预警中心因此要等 3 秒多才出内容。改后 8ms，命中的事项逐条相同。
ANOMALY_CLAUSES = {
    "conflict": ("a.reasons_json LIKE ?", ('%"code": "claim_conflict"%',)),
    "unverified": ("a.reasons_json LIKE ?", ('%"code": "unverified_major_rumour"%',)),
    "thin_source": ("(a.credibility_basis_json LIKE ? OR a.credibility_basis_json LIKE ?)",
                    ('%"production_mode": "BRIEF"%', '%"production_mode": "REPOST"%')),
    "low_confidence": ("EXISTS(SELECT 1 FROM json_each(a.event_ids_json) j"
                       " JOIN event_records e ON e.event_id=j.value WHERE e.needs_review=1)", ()),
}

#: 「处理中」：已经有人接手、还没到持续跟踪或归档的三步。事项库的一个页签。
HANDLING = ("investigating", "reporting", "returned")


def has_dispositions(db) -> bool:
    return has_table(db, "alert_dispositions")


#: 处置表上后补的列。只读连接补不了列，所以每一列都只在已经补上时才选——旧库上照常能读，
#: 缺的那几项在 ``disposition_of`` 里就是 None。
LATE_COLUMNS = ("due_at", "conclusion", "level_override", "opinion")


def due_column(db) -> str:
    """`, d.due_at, d.conclusion, …`：后补的列里库上已经有的那些。"""
    if not has_dispositions(db):
        return ""
    return "".join(f", d.{column}" for column in LATE_COLUMNS
                   if has_column(db, "alert_dispositions", column))


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def overdue(due_at: str | None, status: str) -> bool:
    """截止日**之后**才算逾期，当天不算；没有截止时间的不算；归档了的不算。

    「没有截止时间」与「已逾期」必须分开：把前者也算进逾期，逾期数就成了「没人定过期限」
    的数，读者会以为有一堆事项拖着没办。
    """
    return bool(due_at) and status != "archived" and due_at < today()


def disposition_join(db) -> str:
    """LEFT JOIN，且只在表已经建起来时加。"""
    return (" LEFT JOIN alert_dispositions d ON d.alert_id=a.alert_id"
            if has_dispositions(db) else "")


def disposition_of(row: Any) -> dict[str, Any]:
    """一行事项的处置态，缺行时是「待研判」而不是空。

    没有人判过的事项不是「状态未知」，它就是待研判——把这两件事说成一件，
    列表里会出现一格什么也没有的状态列，而读者读不出那是「还没轮到」还是「坏了」。
    """
    status = (row["status_d"] if "status_d" in row.keys() else None) or "pending"
    meta = dispositions.STATUSES.get(status, {})
    chosen = row["chosen"] if "chosen" in row.keys() else None
    recommended = row["recommended"] if "recommended" in row.keys() else None
    conclusion = row["conclusion"] if "conclusion" in row.keys() else None
    level_override = row["level_override"] if "level_override" in row.keys() else None
    return {
        "status": status, "status_label": meta.get("label", status),
        "chosen": chosen, "chosen_label": dispositions.ACTIONS.get(chosen, {}).get("label"),
        "recommended": recommended,
        "recommended_label": dispositions.ACTIONS.get(recommended, {}).get("label"),
        "owner": row["owner"] if "owner" in row.keys() else None,
        "decided_at": row["decided_at"] if "decided_at" in row.keys() else None,
        "due_at": row["due_at"] if "due_at" in row.keys() else None,
        "overdue": overdue(row["due_at"] if "due_at" in row.keys() else None, status),
        # 这一列是整份流水存在的理由：人有没有偏离系统的建议。
        "diverged": bool(chosen) and chosen != recommended,
        # 人工研判。定级与处理层的 risk_level 并排给出，不替换它：「机器判高、人判中」
        # 本身就是读者要看的东西。
        "conclusion": conclusion,
        "conclusion_label": dispositions.CONCLUSIONS.get(conclusion, {}).get("label"),
        "level_override": level_override,
        "false_positive": conclusion == "false_positive",
    }


def risk_row(row: Any) -> dict[str, Any]:
    """列表里的一行。字段按规格 §4.3，技术字段不在默认列里。"""
    item = alert_row(row)
    basis = item.get("credibility_basis") or {}
    item["issue_label"] = ISSUE_CODE_LABELS.get(item["issue_code"])
    item["disposition"] = disposition_of(row)
    # 传播热度不是新造的指标：合并进来的事件数，加上可信度分母里那个独立信源数。
    # 「多少人在说」和「多少个不同的声音在说」是两个量，列表把两个都给出来。
    item["heat"] = {"event_count": item["event_count"],
                    "independent_sources": basis.get("independent_components")}
    item["latest_progress"] = item["disposition"]["decided_at"] or item["updated_at"]
    return item


@router.get("")
def list_risks(company: str | None = None, level: str | None = None,
               credibility: str | None = None, issue: str | None = None,
               phase: str | None = None, reason: str | None = None,
               search: str | None = None, status: str | None = None,
               owner: str | None = None, view: str | None = None, anomaly: str | None = None,
               conclusion: str | None = None,
               limit: int = Query(25, ge=1, le=200), offset: int = Query(0, ge=0),
               db=Depends(db_session), session=Depends(current_session)) -> dict[str, Any]:
    """风险事项库。

    ``view`` 是预置视图的名字（``open`` / ``handling`` / ``anomaly`` / ``archived``），它只是
    几个筛选条件的简写——所以页内的视图切换和筛选器里手动选出来的是同一个东西，
    不是几条各自实现一遍的查询。``anomaly`` 把异常收窄到其中一类，首页四张异常卡片各自
    点进来的就是它。
    """
    if status and status not in dispositions.STATUSES:
        raise HTTPException(status_code=400, detail=f"未知处置状态：{status}")
    if anomaly and anomaly not in ANOMALY_CLAUSES:
        raise HTTPException(status_code=400, detail=f"未知异常类别：{anomaly}")
    if conclusion and conclusion not in dispositions.CONCLUSIONS:
        raise HTTPException(status_code=400, detail=f"未知研判结论：{conclusion}")
    where, parameters = filter_clause(company, level, credibility, issue, phase, reason,
                                      None, search)
    joined = has_dispositions(db)
    extra: list[str] = []

    def narrow(clause: str, *values: Any) -> None:
        extra.append(clause)
        parameters.extend(values)

    if status:
        narrow("coalesce(d.status,'pending')=?" if joined
               else ("1=1" if status == "pending" else "1=0"), *([status] if joined else []))
    if owner and joined:
        narrow("d.owner=?", owner)
    if conclusion:
        # 列还没补上的旧库：一条都没有，而不是「筛不了，于是全给你」。
        if joined and has_column(db, "alert_dispositions", "conclusion"):
            narrow("d.conclusion=?", conclusion)
        else:
            narrow("1=0")
    if view == "archived":
        # 表还没建起来时一条都没有，而不是「筛不了，于是全都给你」——后者会把
        # 「已归档」这个入口变成第二个全量列表，读者却以为自己筛过了。
        narrow("coalesce(d.status,'pending')='archived'" if joined else "1=0")
    elif view == "open" and joined:
        narrow("coalesce(d.status,'pending')<>'archived'")
    elif view == "handling":
        narrow(f"coalesce(d.status,'pending') IN ({','.join('?' * len(HANDLING))})" if joined
               else "1=0", *(HANDLING if joined else ()))
    elif view == "anomaly" or anomaly:
        # 「待核查」：已归档的不算，它的问题已经有人处理完了。首页的计数同一口径。
        kinds = [anomaly] if anomaly else list(ANOMALY_CLAUSES)
        narrow("(" + " OR ".join(ANOMALY_CLAUSES[kind][0] for kind in kinds) + ")",
               *[value for kind in kinds for value in ANOMALY_CLAUSES[kind][1]])
        if joined:
            narrow("coalesce(d.status,'pending')<>'archived'")

    where = (f"{where} AND {' AND '.join(extra)}" if where and extra
             else (f" WHERE {' AND '.join(extra)}" if extra else where))
    selected = (f"SELECT a.*, v.source_id AS lead_source, v.source_tier AS lead_source_tier,"
                f" v.canonical_url AS lead_url"
                + (", d.status AS status_d, d.chosen, d.recommended, d.owner, d.decided_at"
                   if joined else "") + due_column(db)
                + " FROM alerts a JOIN document_versions v ON v.version_id=a.lead_version_id"
                + disposition_join(db))
    # 待研判的高风险排在最前，其余仍按修正后风险分——规格 §3.3 的默认排序。
    order = (" ORDER BY CASE WHEN coalesce(d.status,'pending')='pending' THEN 0 ELSE 1 END,"
             " a.adjusted_risk_score DESC, a.window_end DESC") if joined else \
            " ORDER BY a.adjusted_risk_score DESC, a.window_end DESC"
    query, bound = paginate(f"{selected}{where}{order}", parameters, limit, offset)
    return {"total": total_of(db, f"{selected}{where}", parameters),
            "limit": limit, "offset": offset, "view": view,
            "statuses": dispositions.STATUSES,
            # 两张词表随列表一起给：筛选器画下拉框、「只看某一类异常」写出那一类的名字，
            # 都不在前端另存一份。
            "conclusions": dispositions.CONCLUSIONS,
            "anomaly_kinds": ANOMALY_KINDS,
            "items": [risk_row(row) for row in db.execute(query, bound).fetchall()]}


@router.get("/summary")
def summary(company: str | None = None, db=Depends(db_session),
            session=Depends(current_session)) -> dict[str, Any]:
    """风险预警中心的首屏。

    刻意**不**返回文档总数、主张总数、要素标准化率这一类后台指标（规格 §3.5）：
    它们没有消失，搬到了「数据与模型」组的数据质量总览——首屏回答的是「今天该先处理哪几条」，
    而不是「库里有多少东西」。
    """
    clause, parameters = ((" WHERE a.company_id=?", [company]) if company else ("", []))
    joined = has_dispositions(db)
    status_expr = "coalesce(d.status,'pending')" if joined else "'pending'"
    base = ("SELECT a.*, " + (status_expr + " AS effective_status, d.owner" if joined else
                              "'pending' AS effective_status")
            + " FROM alerts a" + disposition_join(db) + clause)
    rows = db.execute(base, parameters).fetchall()

    indicators = {name: 0 for name in dispositions.STATUSES}
    high_open = mine_pending = my_tasks = 0
    me = session["username"]
    companies: dict[str, dict[str, Any]] = {}
    for row in rows:
        state = row["effective_status"]
        indicators[state] = indicators.get(state, 0) + 1
        if row["risk_level"] == "high" and state != "archived":
            high_open += 1
        owner = row["owner"] if joined and "owner" in row.keys() else None
        if owner == me and state != "archived":
            my_tasks += 1
            mine_pending += state == "pending"
        seen = companies.setdefault(row["company_id"],
                                    {"company_id": row["company_id"], "open": 0, "high": 0,
                                     "latest_at": None})
        if state != "archived":
            seen["open"] += 1
            if row["risk_level"] == "high":
                seen["high"] += 1
        if not seen["latest_at"] or (row["updated_at"] or "") > seen["latest_at"]:
            seen["latest_at"] = row["updated_at"]

    # 异常按类计数，与事项库 ``?anomaly=`` 筛选用的是**同一句 SQL**（``ANOMALY_CLAUSES``），
    # 已归档的不算。此前这里用 Python 另写了一遍判断，于是首页的数与点进去的列表可以各说各话。
    unarchived = f"{status_expr}<>'archived'"
    where = f"{clause} AND {unarchived}" if clause else f" WHERE {unarchived}"

    def count(condition: str, values: tuple) -> int:
        return db.execute(f"SELECT count(*) FROM alerts a{disposition_join(db)}{where} AND {condition}",
                          [*parameters, *values]).fetchone()[0]

    anomalies = {key: count(*ANOMALY_CLAUSES[key]) for key in ANOMALY_KINDS}
    anomaly_any = count("(" + " OR ".join(sql for sql, _ in ANOMALY_CLAUSES.values()) + ")",
                        tuple(value for _, values in ANOMALY_CLAUSES.values() for value in values))

    return {
        "total": len(rows),
        "indicators": {"pending": indicators.get("pending", 0), "high_open": high_open,
                       "anomaly": anomaly_any, "mine_pending": mine_pending, "my_tasks": my_tasks,
                       "investigating": indicators.get("investigating", 0),
                       "reporting": indicators.get("reporting", 0),
                       "returned": indicators.get("returned", 0),
                       "tracking": indicators.get("tracking", 0),
                       "archived": indicators.get("archived", 0)},
        "anomalies": [{"key": key, **meta, "count": anomalies[key]}
                      for key, meta in ANOMALY_KINDS.items()],
        "companies": sorted(companies.values(), key=lambda item: -item["open"]),
        "statuses": dispositions.STATUSES,
    }


#: 专项核查工作视图收的四个状态：发起了核查、还没归档的。待研判的还没开始查，
#: 已归档的查完了——两者都不在「谁在查什么」这个问题里。退回补证在里面：它就是
#: 「上级要求再查一轮」。
INVESTIGATING = ("investigating", "reporting", "returned", "tracking")


@router.get("/investigations")
def investigations(owner: str | None = None, db=Depends(db_session),
                   session=Depends(current_session)) -> dict[str, Any]:
    """专项核查：谁在查什么、查到哪一步、什么时候到期、逾期几条。

    **不建第二套任务实体**。一条核查的状态、责任人、系统建议、人工选择、说明、流水全都已经
    在 `alert_dispositions` 里，只差一个截止时间；再建一张「核查任务」表，就是给同一件事
    第二套状态机——任务完成了而事项还在核查中，谁说了算？

    与 `/review` 不是一回事：那是**模型标注**的复核队列（判得对不对），这是**业务事项**的
    核查（这条风险后来怎么办了）。

    排序：逾期的在最前（按截止日从早到晚），然后是有期限的，最后是没定期限的；同一档里
    风险分高的在前。
    """
    empty = {"items": [], "today": today(), "summary": {
        "total": 0, "overdue": 0, "no_due": 0,
        "by_status": {s: {"label": dispositions.STATUSES[s]["label"], "count": 0} for s in INVESTIGATING},
        "by_owner": []}}
    if not has_dispositions(db):
        return empty
    placeholders = ",".join("?" * len(INVESTIGATING))
    sql = (f"SELECT a.*, v.source_id AS lead_source, v.source_tier AS lead_source_tier,"
           f" v.canonical_url AS lead_url, d.status AS status_d, d.chosen, d.recommended,"
           f" d.owner, d.decided_at{due_column(db)}"
           f" FROM alerts a JOIN document_versions v ON v.version_id=a.lead_version_id"
           f" JOIN alert_dispositions d ON d.alert_id=a.alert_id"
           f" WHERE d.status IN ({placeholders})")
    parameters: list[Any] = list(INVESTIGATING)
    if owner:
        sql += " AND d.owner=?"
        parameters.append(owner)
    items = [risk_row(row) for row in db.execute(sql, parameters).fetchall()]

    def rank(item: dict[str, Any]) -> tuple:
        d = item["disposition"]
        tier = 0 if d["overdue"] else (1 if d["due_at"] else 2)
        return (tier, d["due_at"] or "", -(item.get("adjusted_risk_score") or 0))

    items.sort(key=rank)
    summary = empty["summary"]
    summary["total"] = len(items)
    owners: dict[str | None, dict[str, Any]] = {}
    for item in items:
        d = item["disposition"]
        summary["by_status"][d["status"]]["count"] += 1
        summary["overdue"] += d["overdue"]
        summary["no_due"] += not d["due_at"]
        slot = owners.setdefault(d["owner"], {"owner": d["owner"], "count": 0, "overdue": 0})
        slot["count"] += 1
        slot["overdue"] += d["overdue"]
    # 逾期多的人排前面；没指派的那一栏永远放最后，它不是一个人。
    summary["by_owner"] = sorted(owners.values(),
                                 key=lambda o: (o["owner"] is None, -o["overdue"], -o["count"]))
    return {"items": items, "today": empty["today"], "summary": summary}


#: 任务类型由处置状态推出（修改思路 v2 §七）。**不建第二套任务实体**：一条任务就是一条
#: 负责人是我、还没归档的事项；它是什么任务，取决于它现在走到哪一步。
#: 顺序即排序：被退回的最急（有人在等你补证），其次是等你复核的，最后是持续跟踪。
TASK_KINDS = {
    "returned": {"label": "退回补证", "action": "补充核验", "detail": "上级退回，按退回意见补证后再上报"},
    "reporting": {"label": "上级复核", "action": "查看并复核", "detail": "下级已给出结论并上报给你"},
    "investigating": {"label": "专项核查", "action": "进入核查", "detail": "你负责的专项核查"},
    "pending": {"label": "待研判", "action": "进入研判", "detail": "已分派给你，还没给出结论"},
    "tracking": {"label": "持续跟踪", "action": "查看新进展", "detail": "结论已定，按复核日回来看"},
}


@router.get("/tasks")
def tasks(db=Depends(db_session), session=Depends(current_session)) -> dict[str, Any]:
    """我的任务：负责人是我、还没归档的事项，每条带上「谁交来的、说了什么」。

    「谁交来的」取最近一次**把负责人换成我**、且不是我自己做的那一步——认领是自己交给
    自己，不算有人交来。一条都没有时 ``handed_by`` 为空，页面照实说「自己认领」。

    另给一个数：还没人认领的待研判事项有几条。个人任务清空了不等于组里没活，这个数
    让「我的任务」页能指回公共队列。
    """
    me = session["username"]
    empty = {"items": [], "kinds": TASK_KINDS, "counts": {key: 0 for key in TASK_KINDS},
             "unclaimed": 0, "today": today()}
    if not has_dispositions(db):
        empty["unclaimed"] = db.execute("SELECT count(*) FROM alerts").fetchone()[0]
        return empty
    rows = db.execute(
        "SELECT a.*, v.source_id AS lead_source, v.source_tier AS lead_source_tier,"
        " v.canonical_url AS lead_url, d.status AS status_d, d.chosen, d.recommended,"
        f" d.owner, d.decided_at{due_column(db)}"
        " FROM alerts a JOIN document_versions v ON v.version_id=a.lead_version_id"
        " JOIN alert_dispositions d ON d.alert_id=a.alert_id"
        " WHERE d.owner=? AND d.status<>'archived'", (me,)).fetchall()
    handed: dict[str, dict[str, Any]] = {}
    if rows and has_table(db, "alert_disposition_events"):
        ids = [row["alert_id"] for row in rows]
        for step in db.execute(
                f"""SELECT alert_id, actor, note, occurred_at, from_status, to_status
                    FROM alert_disposition_events
                    WHERE owner=? AND actor<>? AND alert_id IN ({",".join("?" * len(ids))})
                    ORDER BY event_seq""", (me, me, *ids)):
            handed[step["alert_id"]] = dict(step)     # 按序覆盖，留下的是最近一次
    items = []
    for row in rows:
        item = risk_row(row)
        status = item["disposition"]["status"]
        item["task"] = {"kind": status, **TASK_KINDS.get(status, {"label": status, "action": "进入研判"})}
        item["handed_by"] = handed.get(row["alert_id"])
        items.append(item)
    order = list(TASK_KINDS)

    def rank(item: dict[str, Any]) -> tuple:
        d = item["disposition"]
        return (order.index(d["status"]) if d["status"] in order else len(order),
                not d["overdue"], d["due_at"] or "9999", -(item.get("adjusted_risk_score") or 0))

    items.sort(key=rank)
    counts = {key: sum(1 for item in items if item["disposition"]["status"] == key) for key in TASK_KINDS}
    unclaimed = db.execute("""SELECT count(*) FROM alerts a
        LEFT JOIN alert_dispositions d ON d.alert_id=a.alert_id
        WHERE coalesce(d.status,'pending')='pending' AND (d.owner IS NULL OR d.owner='')""").fetchone()[0]
    return {"items": items, "kinds": TASK_KINDS, "counts": counts, "unclaimed": unclaimed,
            "today": today()}


@router.get("/{alert_id}")
def dossier(alert_id: str, db=Depends(db_session),
            session=Depends(current_session)) -> dict[str, Any]:
    """研判档案的六个页签，一次取回。

    宁可一个大响应也不要六个往返：这一页是演示动线的核心，而六个请求意味着六次
    「转圈」和六个各自可能失败的加载态。性能预算 2,000ms，当前最慢的页面 P95 是
    57.6ms——余量够，不够时再拆。
    """
    joined = has_dispositions(db)
    row = db.execute(f"SELECT a.*, v.source_id AS lead_source, v.source_tier AS lead_source_tier,"
                     f" v.canonical_url AS lead_url"
                     + (", d.status AS status_d, d.chosen, d.recommended, d.owner,"
                        " d.decided_at, d.note, d.recommended_basis_json" if joined else "")
                     + due_column(db)
                     + " FROM alerts a JOIN document_versions v ON v.version_id=a.lead_version_id"
                     + disposition_join(db)
                     + " WHERE a.alert_id=?", (alert_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="风险事项不存在")

    item = risk_row(row)
    item["recommended_basis"] = json_field(
        row["recommended_basis_json"], []) if joined and "recommended_basis_json" in row.keys() else []
    item["note"] = row["note"] if joined and "note" in row.keys() else None
    item["opinion"] = row["opinion"] if "opinion" in row.keys() else None
    # 合法去向由服务端算并送出，界面不自己再实现一遍流程：两份实现迟早各说各话。
    item["transitions"] = dispositions.transitions_from(item["disposition"]["status"])
    # 结论与定级的可选值也由服务端给：界面画下拉框，不另存一份词表。
    item["conclusions"] = dispositions.CONCLUSIONS

    item["evidence"] = evidence_of(db, row)
    item["timeline"] = json_field(row["timeline_json"], {})
    event_ids = json_field(row["event_ids_json"])
    item["sources"] = sources_of(db, event_ids)
    item["source_basis"] = source_basis(item["sources"], item["evidence"])
    item["propagation"] = propagation_of(db, item["sources"])
    item["changes"] = changes_of(db, row)
    item["history"] = history_of(db, alert_id)
    item["similar"] = similar_of(db, row)
    # 退回补证时，提交人一打开就要看到上级说了什么——取最近一次「→ 退回补证」那一步的说明。
    returned = next((step for step in item["history"]
                     if step["to_status"] == "returned" and step["from_status"] != "returned"), None)
    item["returned_by"] = ({"actor": returned["actor"], "note": returned["note"],
                            "occurred_at": returned["occurred_at"]}
                           if returned and item["disposition"]["status"] == "returned" else None)
    return item


def similar_of(db, row: Any, limit: int = 8) -> list[dict[str, Any]]:
    """相似事项：同一专题，或主理由相同的其他事项，附上它们后来被怎么判、怎么办的。

    规则式，**不给相似度小数**：一个 0.83 印在结论旁边没人反驳得了，「同一专题：出口管制」
    可以。匹配依据随每一条返回（``basis``）。同一专题的排在前面，其中已有人工结论的再往前——
    读者翻历史案例，要看的是「当时怎么判的」，还没人判过的参考价值低一档。
    """
    reasons = json_field(row["reasons_json"])
    lead_reason = reasons[0]["code"] if reasons else None
    lead_label = reasons[0].get("label") if reasons else None
    joined = has_dispositions(db)
    rows = db.execute(
        "SELECT a.alert_id, a.company_id, a.issue_code, a.headline, a.risk_level, a.window_start,"
        " a.window_end, a.reasons_json"
        + (", d.status AS status_d, d.chosen, d.recommended, d.owner, d.decided_at" if joined else "")
        + due_column(db)
        + " FROM alerts a" + disposition_join(db)
        + " WHERE a.alert_id<>? AND (a.issue_code=?"
        + (" OR a.reasons_json LIKE ?)" if lead_reason else ")"),
        (row["alert_id"], row["issue_code"],
         *([f'%"code": "{lead_reason}"%'] if lead_reason else []))).fetchall()
    found = []
    for other in rows:
        same_issue = bool(row["issue_code"]) and other["issue_code"] == row["issue_code"]
        other_reasons = json_field(other["reasons_json"])
        same_reason = bool(lead_reason) and bool(other_reasons) and other_reasons[0]["code"] == lead_reason
        if not (same_issue or same_reason):
            continue
        basis = []
        if same_issue:
            basis.append(f"同一专题：{ISSUE_CODE_LABELS.get(other['issue_code'], other['issue_code'])}")
        if same_reason:
            basis.append(f"主理由相同：{lead_label or lead_reason}")
        disposition = disposition_of(other)
        found.append({"alert_id": other["alert_id"], "company_id": other["company_id"],
                      "issue_code": other["issue_code"], "headline": other["headline"],
                      "risk_level": other["risk_level"], "window_start": other["window_start"],
                      "window_end": other["window_end"], "basis": basis,
                      "same_issue": same_issue, "disposition": disposition})
    # 两趟稳定排序：先按时间从近到远，再按「同专题 / 有结论 / 两条依据都中」分档。
    found.sort(key=lambda item: item["window_end"] or "", reverse=True)
    found.sort(key=lambda item: (not item["same_issue"], item["disposition"]["conclusion"] is None,
                                 -len(item["basis"])))
    return found[:limit]


def evidence_of(db, row: Any) -> list[dict[str, Any]]:
    """证据核验点，读取时补上「去哪儿看原件」。

    ``evidence_json`` 是处理层写下的快照，里面没有 URL；不改处理层是因为那要重跑
    全量预警，而这只是展示字段。补的是地址，不是结论——快照本身仍是当时那一版。
    """
    evidence = public_evidence(json_field(row["evidence_json"]) or [])
    version_ids = [item.get("evidence_version_id") for item in evidence
                   if item.get("evidence_version_id")]
    if not version_ids:
        return evidence
    placeholders = ",".join("?" for _ in version_ids)
    urls = {r["version_id"]: r["canonical_url"] for r in db.execute(
        f"SELECT version_id, canonical_url FROM document_versions "
        f"WHERE version_id IN ({placeholders})", version_ids)}
    for item in evidence:
        item["evidence_url"] = urls.get(item.get("evidence_version_id"))
    return evidence


#: 来源等级到「这算哪一类信息」的归组。权威披露与独立报道的区别不是可信度高低，
#: 是它们在研判里承担的角色不同：一个建立事实，一个提供线索。
SOURCE_GROUPS = {"S0": "authoritative", "S1": "authoritative"}
RELAY_MODES = frozenset({"REPOST", "REWRITE", "BRIEF", "SPONSORED"})


def sources_of(db, event_ids: list[str]) -> list[dict[str, Any]]:
    if not event_ids:
        return []
    placeholders = ",".join("?" for _ in event_ids)
    rows = db.execute(f"""SELECT e.event_id, e.summary, e.production_mode, e.impact_direction,
                                 e.needs_review, t.timeline_at, t.new_fact_count,
                                 v.version_id, v.title, v.source_id, v.source_tier,
                                 v.canonical_url, v.published_at, v.document_type
        FROM event_records e
        LEFT JOIN event_timeline t ON t.event_id=e.event_id
        JOIN document_versions v ON v.version_id=e.version_id
        WHERE e.event_id IN ({placeholders})
        ORDER BY v.published_at""", event_ids).fetchall()
    found = []
    for row in rows:
        item = dict(row)
        tier = row["source_tier"]
        item["group"] = (SOURCE_GROUPS.get(tier)
                         or ("relay" if row["production_mode"] in RELAY_MODES else "independent"))
        item["source_name"] = source_name(row["source_id"])
        # 发布方的域名随标题一起给：标题即便读不出是哪篇，读者也能看出它来自哪里（I-05）。
        item["domain"] = (urlparse(row["canonical_url"]).hostname or "").removeprefix("www.") or None
        found.append(item)
    return found


def source_basis(sources: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """这条事项的结论立在什么上面（I-05）。

    「权威披露 · 0 篇」只说来源文章里没有公告，不说结论没有权威依据：上面的核验点
    可能已经把媒体说的话逐条对到了招股书第几页。三种情况要分开说——来源里就有
    权威披露；来源全是媒体，但事实已对过披露原文；来源全是媒体，也没有一条对过。
    最后一种才是「结论只来自二手报道」，而它此前和第二种在页面上长得一模一样。
    """
    authoritative = sum(1 for item in sources if item.get("group") == "authoritative")
    checked = [item for item in evidence
               if item.get("evidence_page") is not None and item.get("verdict") != "UNVERIFIABLE"]
    filings = sorted({item["evidence_title"] for item in checked if item.get("evidence_title")})
    kind = ("authoritative" if authoritative
            else "secondhand_checked" if checked else "secondhand_only")
    return {"kind": kind, "authoritative_sources": authoritative, "media_sources": len(sources) - authoritative,
            "checked_claims": len(checked), "filings_cited": filings}


def propagation_of(db, sources: list[dict[str, Any]]) -> dict[str, Any]:
    """纵向时间线优先，关系图是它的另一种读法。

    规格 §5.3-D：默认给时间线而不是关系网络。一张关系图回答「谁抄了谁」，但读者
    在研判档案里先问的是「这件事先后发生了什么」——后者是一条线，画成网会把顺序
    这个最重要的信息藏起来。图仍然给，折在「查看关系图」后面。
    """
    version_ids = [item["version_id"] for item in sources if item.get("version_id")]
    if not version_ids or not has_table(db, "propagation_component_members"):
        return {"components": [], "nodes": [], "edges": []}
    placeholders = ",".join("?" for _ in version_ids)
    components = [row[0] for row in db.execute(
        f"""SELECT DISTINCT component_id FROM propagation_component_members
            WHERE version_id IN ({placeholders})""", version_ids)]
    if not components:
        return {"components": [], "nodes": [], "edges": []}
    slots = ",".join("?" for _ in components)
    nodes = [dict(row) for row in db.execute(
        f"""SELECT m.component_id, m.version_id, m.depth, v.title, v.source_id, v.source_tier,
                   v.published_at, v.canonical_url, v.document_type
            FROM propagation_component_members m
            JOIN document_versions v ON v.version_id=m.version_id
            WHERE m.component_id IN ({slots})
            ORDER BY v.published_at, m.depth""", components)]
    edges = [dict(row) for row in db.execute(
        f"""SELECT edge_kind, from_version_id, to_version_id, similarity, lag_hours, basis
            FROM propagation_edges
            WHERE to_version_id IN (SELECT version_id FROM propagation_component_members
                                    WHERE component_id IN ({slots}))""", components)]
    incoming = {edge["to_version_id"]: edge["edge_kind"] for edge in edges}
    for node in nodes:
        # 没有入边的就是这条链的起点——「首发」是读出来的，不是猜的。
        node["action"] = incoming.get(node["version_id"]) or "ORIGIN"
        node["source_name"] = source_name(node.get("source_id"))
    return {"components": components, "nodes": nodes, "edges": edges}


def changes_of(db, row: Any) -> list[dict[str, Any]]:
    """同一份权威文件两个版本之间的关键变化。

    按公司取，不按窗口截断：一条 2026 年的报道要对的可能是 2025 年那一稿里的数字，
    按发布窗口去卡会正好把最该看的那一条卡掉。
    """
    if not has_table(db, "disclosure_diffs"):
        return []
    return [dict(item) for item in db.execute(
        """SELECT * FROM disclosure_diffs WHERE company_id=?
           ORDER BY CASE diff_kind WHEN 'numeric' THEN 0 WHEN 'modified' THEN 1 ELSE 2 END,
                    section, right_page LIMIT 40""", (row["company_id"],))]


def history_of(db, alert_id: str) -> list[dict[str, Any]]:
    if not has_table(db, "alert_disposition_events"):
        return []
    rows = [dict(item) for item in db.execute(
        """SELECT * FROM alert_disposition_events WHERE alert_id=?
           ORDER BY event_seq DESC""", (alert_id,))]
    for item in rows:
        item["from_label"] = dispositions.STATUSES.get(item["from_status"], {}).get("label")
        item["to_label"] = dispositions.STATUSES.get(item["to_status"], {}).get("label")
        item["chosen_label"] = dispositions.ACTIONS.get(item["chosen"], {}).get("label")
        item["conclusion_label"] = dispositions.CONCLUSIONS.get(item.get("conclusion"), {}).get("label")
    return rows
