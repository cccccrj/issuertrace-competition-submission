"""Subscriptions, the outbox, and disposition — the outward half of task six.

命题主要任务第 6 项是「事件影响分级与预警**推送**」。The grading half has been
there since the beginning; the pushing half was an in-app inbox and nothing
else. This is the rest of it.

Everything that can send is a POST behind a role. Everything that decides
whether to send is in `api/notify_rules.py`, which has no database and no
socket. Everything that actually sends is in `api/notify_delivery.py`, which
refuses any target outside an allowlist and resolves signing keys by the name of
an environment variable rather than storing them. The split is deliberate: the
dangerous part of this feature is small, and it is easier to keep small when it
is in its own file with its own tests.
"""
from __future__ import annotations

import hashlib
import json
from urllib.parse import urlparse
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api import auth, dispatch_db, notify_delivery, notify_rules, users_db
# 本文件里有一个叫 dispositions 的路由函数，会把同名模块遮掉——别名不是风格选择。
from api import dispositions as disposition_rules
from api.db import json_field
from api.errors import classify
from api.dispositions import ACTIONS, recommend
from api.routes.auth import current_session, requires
from api.routes.events import db_session
from processor.alerts import REASON_LABELS

router = APIRouter(prefix="/api/notify", tags=["notify"])


def notify_session():
    db = dispatch_db.connect()
    try:
        yield db
    finally:
        db.close()


class SubscriptionBody(BaseModel):
    name: str
    channel: str
    target: str | None = None
    company_id: str | None = None
    issue_code: str | None = None
    min_risk_level: str = "high"
    min_credibility: str | None = None
    phases: list[str] = []
    reason_codes: list[str] = []
    secret_ref: str | None = None
    quiet_hours: str | None = None


class Toggle(BaseModel):
    enabled: bool | None = None
    dry_run: bool | None = None


class Decision(BaseModel):
    # 不传 = 不动已选的对外口径。认领一条事项、保存一次研判结论，都不等于替它选了口径。
    chosen: str | None = None
    status: str = "investigating"
    note: str | None = None
    owner: str | None = None
    # 不传 = 不动；传 null 或 "" = 清空；传 YYYY-MM-DD = 设成这一天。三种意思必须分得开，
    # 否则每一次只改状态的提交，都会把之前定好的期限悄悄抹掉。
    due_at: str | None = None
    # 人工研判三样，同一个「不传 = 不动」的约定。
    conclusion: str | None = None
    level_override: str | None = None
    opinion: str | None = None


def public(row: dict[str, Any], admin: bool = False) -> dict[str, Any]:
    """A subscription as the API may show it.

    ``secret_ref`` is the *name* of an environment variable.  The value never
    passes through here; the name is operator configuration, so only an
    administrator sees it (I-06) — everyone else learns whether a key is set.
    """
    shown = {key: value for key, value in row.items() if admin or key != "secret_ref"}
    return {
        **shown,
        "enabled": bool(row["enabled"]),
        "dry_run": bool(row["dry_run"]),
        "phases": json.loads(row["phases_json"] or "[]"),
        "reason_codes": json.loads(row["reason_codes_json"] or "[]"),
        "secret_configured": bool(row.get("secret_ref")),
    }


@router.get("/subscriptions")
def list_subscriptions(db=Depends(notify_session), session=Depends(current_session)) -> dict[str, Any]:
    admin = session.get("role") == "admin"
    return {"items": [public(row, admin) for row in dispatch_db.subscriptions(db)],
            "channels": list(dispatch_db.CHANNELS),
            "actions": ACTIONS,
            # 「只在出现这些情况时提醒」的可选项：预警理由码与它们的说法，取自处理层那一份，
            # 订阅页不另存词表。
            "reasons": REASON_LABELS,
            # Shown in the UI so the operator can see what a target may point at
            # without reading the source.
            "allowlist": list(notify_delivery.allowlist()) if admin else []}


@router.post("/subscriptions")
def create_subscription(body: SubscriptionBody, session=Depends(requires("analyst")),
                        db=Depends(notify_session)) -> dict[str, Any]:
    if body.channel not in dispatch_db.CHANNELS:
        raise HTTPException(status_code=400, detail=f"未知渠道：{body.channel}")
    unknown = sorted(set(body.reason_codes) - set(REASON_LABELS))
    if unknown:
        # 一个永远匹配不上的条件，会让这条订阅安静得像是在正常工作。
        raise HTTPException(status_code=400, detail=f"未知提醒条件：{'、'.join(unknown)}")
    if body.channel == "webhook":
        # Refuse at creation, not at send: a rule that can never fire is a rule
        # somebody will believe is working.
        try:
            notify_delivery.check_target(body.target)
        except notify_delivery.Refused as refusal:
            raise HTTPException(status_code=400, detail=str(refusal)) from refusal
    identifier = hashlib.sha256(
        f"{session['username']}\0{body.name}\0{dispatch_db.utc_now()}".encode()).hexdigest()[:24]
    now = dispatch_db.utc_now()
    db.execute("""INSERT INTO alert_subscriptions
        (subscription_id, owner, name, company_id, issue_code, min_risk_level, min_credibility,
         phases_json, reason_codes_json, channel, target, secret_ref, enabled, dry_run,
         quiet_hours, created_at, updated_at, created_by)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,1,?,?,?,?)""",
        (identifier, session["username"], body.name, body.company_id, body.issue_code,
         body.min_risk_level, body.min_credibility,
         json.dumps(body.phases, ensure_ascii=False),
         json.dumps(body.reason_codes, ensure_ascii=False),
         body.channel, body.target, body.secret_ref, body.quiet_hours,
         now, now, session["username"]))
    db.commit()
    row = db.execute("SELECT * FROM alert_subscriptions WHERE subscription_id=?",
                     (identifier,)).fetchone()
    return public(dict(row))


@router.post("/subscriptions/{subscription_id}/toggle")
def toggle(subscription_id: str, body: Toggle, session=Depends(requires("analyst")),
           db=Depends(notify_session)) -> dict[str, Any]:
    row = db.execute("SELECT * FROM alert_subscriptions WHERE subscription_id=?",
                     (subscription_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="subscription not found")
    # Leaving dry-run is the one change that lets this feature reach the network,
    # so it needs the higher role even though everything else here does not.
    if body.dry_run is False and not auth.allows(session["role"], "reviewer"):
        raise HTTPException(status_code=403, detail="关闭演示模式需要核查负责人角色")
    db.execute("""UPDATE alert_subscriptions SET enabled=?, dry_run=?, updated_at=?
        WHERE subscription_id=?""",
        (int(row["enabled"] if body.enabled is None else body.enabled),
         int(row["dry_run"] if body.dry_run is None else body.dry_run),
         dispatch_db.utc_now(), subscription_id))
    db.commit()
    return public(dict(db.execute("SELECT * FROM alert_subscriptions WHERE subscription_id=?",
                                  (subscription_id,)).fetchone()))


def _alerts(analysis, company: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    clause, parameters = ("WHERE company_id=?", [company]) if company else ("", [])
    return [dict(row) for row in analysis.execute(
        f"""SELECT alert_id, company_id, issue_code, status, headline, summary,
                   window_start, window_end, subjects_json, affected_objects_json,
                   credibility, credibility_label, risk_score, adjusted_risk_score,
                   risk_level, phase, phase_label, reasons_json, event_count, updated_at
            FROM alerts {clause} ORDER BY adjusted_risk_score DESC LIMIT ?""",
        [*parameters, limit])]


@router.post("/evaluate")
def evaluate(company: str | None = None, session=Depends(requires("reviewer")),
             db=Depends(notify_session), analysis=Depends(db_session)) -> dict[str, Any]:
    """Work out what each rule would send, and record it — including the silences.

    Nothing leaves the machine here. Queuing and sending are separate on purpose:
    a person can read the whole queue, including what was suppressed and why,
    before anything is attempted.
    """
    summary = {"queued": 0, "suppressed": 0, "unchanged": 0, "rules": 0}
    for subscription in dispatch_db.subscriptions(db, enabled_only=True):
        summary["rules"] += 1
        for alert in _alerts(analysis, company):
            if not notify_rules.matches(subscription, alert):
                continue
            history = dispatch_db.recent_deliveries(db, subscription["subscription_id"],
                                                  alert.get("issue_code"))
            previous = next((json.loads(row["payload_json"]) for row in history
                             if row["alert_id"] == alert["alert_id"]
                             and row["status"] in {"sent", "dry_run"}), None)
            reason = notify_rules.changes_since_last_delivery(previous, alert)
            if reason is None:
                summary["unchanged"] += 1
                continue
            claims = [dict(row) for row in analysis.execute(
                """SELECT c.text, v.verdict, v.partial_subtype, d.title AS evidence_title,
                          v.evidence_page
                   FROM event_claims c
                   JOIN claim_verdicts v ON v.claim_id=c.claim_id
                   LEFT JOIN document_versions d ON d.version_id=v.evidence_version_id
                   JOIN event_records e ON e.event_id=c.event_id
                   WHERE e.company_id=? AND c.issue_code=? LIMIT 5""",
                (alert.get("company_id"), alert.get("issue_code")))]
            body = notify_rules.payload(alert, claims)
            suppressed = notify_rules.suppression(subscription, alert, history)
            if dispatch_db.queue(db, subscription, alert, reason, body, suppressed=suppressed):
                summary["suppressed" if suppressed else "queued"] += 1
    return summary


@router.post("/flush")
def flush(limit: int = Query(50, ge=1, le=500), session=Depends(requires("reviewer")),
          db=Depends(notify_session)) -> dict[str, Any]:
    """Attempt what is queued. In dry-run this renders and stores; it sends nothing."""
    rows = db.execute("""SELECT d.*, s.channel AS sub_channel, s.target AS sub_target,
                                s.secret_ref, s.dry_run
        FROM alert_deliveries d JOIN alert_subscriptions s
          ON s.subscription_id = d.subscription_id
        WHERE d.status IN ('queued','dry_run','failed') ORDER BY d.queued_at LIMIT ?""",
        (limit,)).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        subscription = {"subscription_id": row["subscription_id"], "channel": row["sub_channel"],
                        "target": row["sub_target"], "secret_ref": row["secret_ref"],
                        "dry_run": row["dry_run"]}
        result = notify_delivery.deliver(subscription, json.loads(row["payload_json"]))
        dispatch_db.record_attempt(db, row["delivery_id"], status=result["status"],
                                 http_status=result.get("http_status"),
                                 response=result.get("response"), error=result.get("error"))
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    return {"attempted": len(rows), "by_status": counts}


def public_delivery(row: Any) -> dict[str, Any]:
    """一条投递记录的对外形状（I-06）。

    对方服务器的原始回包、带签名的响应、内部摘要和异常原文都只留在库里；读者看到
    状态、HTTP 状态码、失败类别和推送到了哪个站点。
    """
    hidden = {"response_excerpt", "digest", "error", "payload_json", "target"}
    item = {key: row[key] for key in row.keys() if key not in hidden}
    target = row["target"] or ""
    item["target_host"] = urlparse(target).hostname if target.startswith("http") else (target or None)
    item["error"] = classify(row["error"])
    item["payload"] = json.loads(row["payload_json"])
    return item


@router.get("/outbox")
def outbox(subscription: str | None = None, status: str | None = None,
           channel: str | None = None, reason: str | None = None, alert: str | None = None,
           limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
           db=Depends(notify_session)) -> dict[str, Any]:
    filters, parameters = [], []
    for column, value in (("subscription_id", subscription), ("status", status),
                          ("channel", channel), ("reason", reason), ("alert_id", alert)):
        if value:
            filters.append(f"d.{column}=?")
            parameters.append(value)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    total = db.execute(f"SELECT count(*) FROM alert_deliveries d {where}", parameters).fetchone()[0]
    rows = db.execute(f"""SELECT d.*, s.name AS subscription_name FROM alert_deliveries d
        LEFT JOIN alert_subscriptions s ON s.subscription_id=d.subscription_id
        {where} ORDER BY d.queued_at DESC LIMIT ? OFFSET ?""",
        [*parameters, limit, offset]).fetchall()
    return {
        "total": total, "limit": limit, "offset": offset,
        "items": [public_delivery(row) for row in rows],
        # What did not go out, grouped — the number a reader needs to trust the
        # queue rather than wonder what it swallowed.
        "suppressed_reasons": [dict(row) for row in db.execute(
            """SELECT suppressed_reason AS value, count(*) AS count FROM alert_deliveries
               WHERE status='suppressed' AND suppressed_reason IS NOT NULL
               GROUP BY 1 ORDER BY count DESC""")],
        "by_status": [dict(row) for row in db.execute(
            "SELECT status AS value, count(*) AS count FROM alert_deliveries GROUP BY 1")],
    }


def _claims_by_issue(analysis, pairs: set[tuple[str, str]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Every (公司, 议题) pair's verdicts in one read instead of one read each.

    ``recommend`` only counts verdicts and partial subtypes, so the rows can be
    fetched grouped and handed out.  One query per alert cost this endpoint 277
    round trips on a page that displays one alert's advice.
    """
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {key: [] for key in pairs}
    if not pairs:
        return grouped
    companies = sorted({company for company, _ in pairs})
    issues = sorted({issue for _, issue in pairs if issue})
    if not issues:
        return grouped
    rows = analysis.execute(
        f"""SELECT e.company_id, c.issue_code, v.verdict, v.partial_subtype
            FROM event_claims c
            JOIN claim_verdicts v ON v.claim_id=c.claim_id
            JOIN event_records e ON e.event_id=c.event_id
            WHERE e.company_id IN ({','.join('?' * len(companies))})
              AND c.issue_code IN ({','.join('?' * len(issues))})""",
        [*companies, *issues])
    for row in rows:
        key = (row["company_id"], row["issue_code"])
        if key in grouped:
            grouped[key].append({"verdict": row["verdict"], "partial_subtype": row["partial_subtype"]})
    return grouped


@router.get("/dispositions")
def dispositions(company: str | None = None, status: str | None = None,
                 alert_id: list[str] | None = Query(None),
                 db=Depends(notify_session), analysis=Depends(db_session)) -> dict[str, Any]:
    """Recommended handling for every open alert, with what produced it.

    ``alert_id`` narrows it to one.  The event detail page needs exactly one
    alert's advice and used to fetch every open alert of that company to find it,
    which is most of what made that page cost 1.24 s.
    """
    wanted = set(alert_id or ())
    alerts = [alert for alert in _alerts(analysis, company, limit=200)
              if alert.get("status") == "open" and (not wanted or alert["alert_id"] in wanted)]
    claims_by_issue = _claims_by_issue(
        analysis, {(alert.get("company_id"), alert.get("issue_code")) for alert in alerts})
    # 打开这一页时顺手收走漂移留下的空建议行。放在这里是因为这条路径本来就要写库，
    # 而且它是唯一一处「手上同时有全部事项和全部处置」的地方。
    dispatch_db.prune_stale_recommendations(db, commit=False)
    stored_by_alert = dispatch_db.dispositions_by_alert(db)
    items = []
    for alert in alerts:
        claims = claims_by_issue.get((alert.get("company_id"), alert.get("issue_code")), [])
        alert_view = {**alert, "reasons": json_field(alert.get("reasons_json"))}
        recommendation = recommend(alert_view, claims)
        stored = dispatch_db.upsert_disposition(
            db, alert["alert_id"], alert.get("company_id"), recommendation,
            existing=stored_by_alert.get(alert["alert_id"]) or {}, commit=False)
        if status and stored["status"] != status:
            continue
        # `label`, `detail` and `why` are derived, not stored: the rules can be
        # edited and a stored sentence would then describe a rule that no longer
        # exists. `basis` is stored because it is a snapshot of the fields as
        # they were when the recommendation was made.
        items.append({**stored,
                      "basis": json.loads(stored["recommended_basis_json"]),
                      "recommended_label": recommendation["label"],
                      "recommended_detail": recommendation["detail"],
                      "why": recommendation["why"],
                      "headline": alert.get("headline"),
                      "risk_level": alert.get("risk_level"),
                      "issue_code": alert.get("issue_code")})
    # One commit for the loop rather than one per alert: 277 fsyncs is not a
    # read-shaped cost, and this is a GET.
    db.commit()
    return {"items": items, "actions": ACTIONS}


@router.get("/dispositions/log")
def disposition_log(limit: int = 100, db=Depends(notify_session)) -> dict[str, Any]:
    """Decisions people actually recorded, newest first.

    The write path stores `actor`, `role` and `decided_at` and nothing read
    them, which made three audit columns decorative. The audit page claims all
    five kinds of record live in one place; a disposition is a human decision
    with a name and a time on it, so it belongs there.

    Unlike `/dispositions`, this reads and does not upsert, and it does not
    filter to open alerts — an archived decision is exactly the kind a reader
    comes here looking for.
    """
    rows = [dict(row) for row in db.execute(
        """SELECT alert_id, company_id, recommended, chosen, status, note, actor, role,
                  decided_at, recommended_basis_json
           FROM alert_dispositions WHERE decided_at IS NOT NULL
           ORDER BY decided_at DESC LIMIT ?""", (min(limit, 500),))]
    for row in rows:
        row["basis"] = json.loads(row.pop("recommended_basis_json"))
        row["recommended_label"] = ACTIONS.get(row["recommended"], {}).get("label")
        row["chosen_label"] = ACTIONS.get(row["chosen"], {}).get("label")
        # The one question this log exists to answer at a glance.
        row["diverged"] = bool(row["chosen"]) and row["chosen"] != row["recommended"]
    return {"items": rows, "actions": ACTIONS}


@router.post("/dispositions/{alert_id}")
def decide(alert_id: str, body: Decision, session=Depends(requires("analyst")),
           db=Depends(notify_session)) -> dict[str, Any]:
    if body.chosen is not None and body.chosen not in ACTIONS:
        raise HTTPException(status_code=400, detail=f"未知处置方式：{body.chosen}")
    if body.status not in dispatch_db.DISPOSITION_STATUSES:
        raise HTTPException(status_code=400, detail=f"未知状态：{body.status}")
    given = body.model_fields_set
    if "conclusion" in given and body.conclusion and body.conclusion not in disposition_rules.CONCLUSIONS:
        raise HTTPException(status_code=400, detail=f"未知研判结论：{body.conclusion}")
    if "level_override" in given and body.level_override and body.level_override not in disposition_rules.LEVELS:
        raise HTTPException(status_code=400, detail=f"未知风险等级：{body.level_override}")
    stored = dispatch_db.disposition_for(db, alert_id) or {}
    current = stored.get("status") or "pending"
    # 角色门槛跟着转移走（``required_role``），而不是跟着某一个状态名写死在这里。
    # 此前这里是 ``session["role"] != "reviewer"``——角色是逐级包含的，所以那行把
    # admin 也一起挡在了外面。停在原地的一步（认领、保存结论）也要过同一道门：
    # 待上级复核的事项在上级手里，提交人不能再改它的结论。
    required = (disposition_rules.required_role(current, body.status) if body.status != current
                else disposition_rules.REVIEW_GATE.get(current))
    if required and not auth.allows(session["role"], required):
        raise HTTPException(
            status_code=403,
            detail=f"转为「{disposition_rules.STATUSES[body.status]['label']}」需要"
                   f"{users_db.ROLE_LABELS.get(required, required)}角色")
    # 先问「能不能走」，再问「带齐了没有」：一条走不通的路，告诉读者缺了退回意见只会误导他。
    if body.status != current and not disposition_rules.can_transition(current, body.status):
        refused = dispatch_db.IllegalTransition(
            current, body.status, disposition_rules.TRANSITIONS.get(current, ()))
        raise HTTPException(status_code=409, detail=str(refused))
    # 「走之前要带上什么」：422 而不是 409——事项在对的位置上，是请求缺了东西。
    conclusion = body.conclusion if "conclusion" in given else stored.get("conclusion")
    problem = disposition_rules.step_problem(current, body.status, conclusion=conclusion, note=body.note)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    if body.status == "reporting" and current != "reporting":
        # 上报给谁必须是一个此刻有效、且有复核权的人：上报给一个复核不了的人，等于没上报。
        account = users_db.lookup(body.owner or "")
        if not account or not auth.allows(account["role"], "reviewer"):
            raise HTTPException(status_code=422,
                                detail="上报对象必须是一位核查负责人（或管理员）账号。")
    alert = db.execute("SELECT issue_code, window_start FROM alerts WHERE alert_id=?",
                       (alert_id,)).fetchone()
    # 建议行此前只由 GET /dispositions 顺带写出来，于是「先打开档案页、再点一个动作」
    # 这条路会撞上 404——研判档案走 /api/risks/{id}，那是只读连接，不写任何东西。
    # 写入路径不该依赖读取路径先跑过一遍：这里缺了就当场补一条。
    if not dispatch_db.disposition_for(db, alert_id):
        row = db.execute("SELECT * FROM alerts WHERE alert_id=?", (alert_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="风险事项不存在")
        alert_view = {**dict(row), "reasons": json_field(row["reasons_json"])}
        claims = _claims_by_issue(db, {(row["company_id"], row["issue_code"])}).get(
            (row["company_id"], row["issue_code"]), [])
        dispatch_db.upsert_disposition(db, alert_id, row["company_id"],
                                       recommend(alert_view, claims), existing={})
    due_at: Any = dispatch_db.KEEP
    if "due_at" in body.model_fields_set:
        due_at = (body.due_at or "").strip() or None
        if due_at is not None:
            try:
                due_at = date.fromisoformat(due_at).isoformat()
            except ValueError as bad:
                raise HTTPException(status_code=400,
                                    detail=f"截止日期要写成 YYYY-MM-DD：{body.due_at}") from bad
    judged = {field: ((getattr(body, field) or "").strip() or None)
              for field in ("conclusion", "level_override", "opinion") if field in given}
    try:
        updated = dispatch_db.decide(
            db, alert_id, chosen=body.chosen, status=body.status, note=body.note,
            owner=body.owner, actor=session["username"], role=session["role"],
            context=dict(alert) if alert else None, due_at=due_at, **judged)
    except dispatch_db.IllegalTransition as refused:
        # 409 而不是 400：请求本身没写错，是这条事项现在不在能做这一步的位置上。
        raise HTTPException(status_code=409, detail=str(refused)) from refused
    if not updated:
        raise HTTPException(status_code=404, detail="disposition not found")
    return {**updated, "basis": json.loads(updated["recommended_basis_json"]),
            "transitions": disposition_rules.transitions_from(updated["status"])}
