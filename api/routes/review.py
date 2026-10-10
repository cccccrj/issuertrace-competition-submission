"""Human review: the queue, the judgements and the accuracy they add up to.

This is the only part of the platform that writes, and it writes only to its own
two tables.  A judgement here never rewrites a verdict — it records what a person
concluded about one, so the pipeline stays reproducible and the disagreement
stays visible.  Accuracy is reported with its sample size for the same reason:
"92%" over eleven claims is not a measurement.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api import review_db
from api.db import has_column, has_table, json_field
from api.labels import public_method, public_numeric_check
from api.auth import allows
from api.metrics import class_scores, cohen_kappa
from api.routes.auth import current_session, requires
from api.routes.events import db_session

router = APIRouter(prefix="/api/review", tags=["review"])

# How a queue is filled.  Each origin is a defensible reason to look at
# something, not a random sample dressed up as one.
ORIGINS = {
    "needs_review": "处理层标记需复核",
    "unverified_major_rumour": "高影响低可信预警",
    "evaluation_sample": "人工评测抽样",
}


class Judgement(BaseModel):
    judgement: str
    note: str | None = None
    # Only meaningful on a "wrong": what the system should have said, and
    # whether the page it cited was the right one.  Both stay optional — a
    # reviewer may be sure a verdict is wrong without being sure what is right,
    # and metrics reports how much of the sample carries a label.
    correct_label: str | None = None
    evidence_ok: bool | None = None


class Note(BaseModel):
    """Rejecting is not judging: it carries a reason, not a judgement.  Reusing
    the Judgement model here made ``judgement`` a required field on a request
    that has none, so every rejection from the page was refused as malformed."""
    note: str | None = None


class Seed(BaseModel):
    kind: str
    origin: str
    count: int = 50
    company: str | None = None


class Assignment(BaseModel):
    """把队列切给具体的人。

    ``overlap`` 是双标那一份：前 N 条同时发给每个人，其余的才平分。它默认是 0，
    因为绝大多数任务只需要一个人看；但一致性（κ）只能在两个人判过的同一条上算，
    所以不留出这一份，「标注质量」就没有任何证据。
    """
    assignees: list[str]
    kind: str | None = None
    company: str | None = None
    # 只分派待复核的：已经判过的任务重新分派，等于把别人做完的活挪到另一个人
    # 名下，而判定记录仍写着原来那个人。
    status: str | None = "open"
    overlap: int = 0


def review_session():
    db = review_db.connect()
    try:
        yield db
    finally:
        db.close()


def targets(db, kind: str, ids: list[str]) -> dict[str, dict[str, Any]]:
    """What each task is about, read from the analytical tables."""
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    if kind == "event":
        rows = db.execute(f"""SELECT e.event_id AS id, e.summary, e.company_id, e.risk_level,
                                     e.review_reasons_json, v.title, v.canonical_url, v.published_at
            FROM event_records e JOIN document_versions v ON v.version_id=e.version_id
            WHERE e.event_id IN ({placeholders})""", ids).fetchall()
        return {row["id"]: {**dict(row), "review_reasons": json_field(row["review_reasons_json"])}
                for row in rows}
    if kind == "verdict":
        numeric = ("w.numeric_check" if has_column(db, "claim_verdicts", "numeric_check")
                   else "NULL AS numeric_check")
        rows = db.execute(f"""SELECT c.claim_id AS id, c.text, c.issue_code, e.event_id, e.company_id,
                                     {numeric},
                                     e.summary AS event_summary, w.verdict, w.partial_subtype,
                                     w.method, w.confidence, w.reason, w.evidence_page,
                                     w.evidence_section, w.evidence_text, w.evidence_version_id,
                                     d.title AS evidence_title,
                                     d.canonical_url AS evidence_url
            FROM event_claims c
            JOIN event_records e ON e.event_id=c.event_id
            LEFT JOIN claim_verdicts w ON w.claim_id=c.claim_id
            LEFT JOIN document_versions d ON d.version_id=w.evidence_version_id
            WHERE c.claim_id IN ({placeholders})""", ids).fetchall()
        return {row["id"]: {**dict(row), "method": public_method(row["method"]),
                            "numeric_check": public_numeric_check(json_field(row["numeric_check"]))}
                for row in rows}
    rows = db.execute(f"""SELECT diff_id AS id, company_id, pair_key, section, diff_kind, metric,
                                 old_value, new_value, left_text, right_text, left_page, right_page
        FROM disclosure_diffs WHERE diff_id IN ({placeholders})""", ids).fetchall()
    return {row["id"]: dict(row) for row in rows}


#: 被预警层判为「重大传闻未核实」的事项里的全部事件 id。用 json_each 拆开数组、只算一次；
#: 此前是拿每条事件（或主张所属事件）去 LIKE '%"id"%' 整段 event_ids_json，逐行扫字符串，
#: 两处各要 0.4s 左右。改后抽出的候选逐条、按序相同。
RUMOUR_EVENT_IDS = ("SELECT j.value FROM alerts a, json_each(a.event_ids_json) j"
                    " WHERE a.reasons_json LIKE '%unverified_major_rumour%'")


def candidates(db, kind: str, origin: str, company: str | None, count: int) -> list[tuple[str, str]]:
    """Pick what to review, stratified and deterministic.

    Deterministic because an evaluation set that changes every time it is drawn
    cannot be re-run; stratified because a random draw over 2,853 claims would
    return almost only the majority verdict and measure nothing about the rest.
    """
    parameters: list[Any] = []
    if kind == "event":
        # Every origin is handled here.  Dispatching on kind *and* origin left
        # the unmatched combinations falling through to the diff query below,
        # which filled an event queue with diff ids — tasks that resolve to
        # nothing and still count in the metrics.
        filters = []
        if origin == "needs_review":
            filters.append("e.needs_review=1")
        elif origin == "unverified_major_rumour":
            filters.append(f"e.event_id IN ({RUMOUR_EVENT_IDS})")
        if company:
            filters.append("e.company_id=?")
            parameters.append(company)
        where = f" WHERE {' AND '.join(filters)}" if filters else ""
        # A worklist leads with the riskiest; an evaluation sample orders by
        # event_id, which is a hash, so the draw is arbitrary but repeatable.
        order = "e.event_id" if origin == "evaluation_sample" else "e.risk_score DESC, e.event_id"
        rows = db.execute(f"""SELECT e.event_id, e.company_id FROM event_records e{where}
            ORDER BY {order} LIMIT ?""", [*parameters, count]).fetchall()
        return [(row[0], row[1]) for row in rows]

    if kind == "verdict":
        buckets = ["CONFLICT", "PARTIAL", "SUPPORTED", "UNVERIFIABLE"]
        share = max(1, count // len(buckets))
        found: list[tuple[str, str]] = []
        for verdict in buckets:
            clause, bound = "WHERE w.verdict=?", [verdict]
            if company:
                clause += " AND e.company_id=?"
                bound.append(company)
            if origin == "unverified_major_rumour":
                # Only claims the alert layer already called a rumour.
                clause += f" AND e.event_id IN ({RUMOUR_EVENT_IDS})"
            rows = db.execute(f"""SELECT c.claim_id, e.company_id FROM claim_verdicts w
                JOIN event_claims c ON c.claim_id=w.claim_id
                JOIN event_records e ON e.event_id=c.event_id {clause}
                ORDER BY c.claim_id LIMIT ?""", [*bound, share]).fetchall()
            found.extend((row[0], row[1]) for row in rows)
        return found[:count]

    if kind == "diff":
        # A diff has no needs_review flag and belongs to no alert, so all three
        # origins draw the same way: numeric differences first, since those are
        # the ones a claim can actually be checked against.
        clause, parameters = ("WHERE company_id=?", [company]) if company else ("", [])
        rows = db.execute(f"""SELECT diff_id, company_id FROM disclosure_diffs {clause}
            ORDER BY CASE diff_kind WHEN 'numeric' THEN 0 WHEN 'modified' THEN 1 ELSE 2 END, diff_id
            LIMIT ?""", [*parameters, count]).fetchall()
        return [(row[0], row[1]) for row in rows]
    return []


def independent(decisions: list[dict[str, Any]], assignees: list[str],
                viewer: str) -> tuple[list[dict[str, Any]], bool]:
    """双标任务上，先别让第二个人看见第一个人的答案。

    卡片一直把一条任务上的所有判定都列出来，这在单标时是对的——那是这条任务的
    全部历史。分派留出双标那一份之后它就不对了：第二个人在判之前就看得见第一个
    人怎么判的，两份判定不再独立，κ 会被锚定效应抬上去。而抬上去的 κ 恰恰是在说
    「标注很可靠」，正好是它该说的反面。

    只挡住该挡的那一格：**自己是这条双标任务的复核人、且还没判过**。判完之后照
    常全看——那时候分歧正是要看的东西。不是这条的复核人（比如事后来仲裁的核查
    负责人）不受影响，通过与退回也照常显示，它们是审批不是标注。

    过滤必须在服务端。前端把它藏起来，数据仍然发到了浏览器里。
    """
    if len(assignees) < 2 or viewer not in assignees:
        return decisions, False
    if any(entry["action"] == "judge" and entry["reviewer"] == viewer for entry in decisions):
        return decisions, False
    kept = [entry for entry in decisions
            if entry["action"] != "judge" or entry["reviewer"] == viewer]
    return kept, len(kept) != len(decisions)


def informativeness(db) -> dict[str, tuple[int, float]]:
    """How much each open task can still teach, from the pre-annotation pass.

    The queue was ordered by ``created_at``, which carries no signal at all,
    and 300 tasks is more than anybody sits down and finishes.  Which hundred
    gets judged first therefore decides what the measurement is worth.

    Two things are already recorded and were never read: whether the
    pre-annotating model **disagreed** with the system (138 of the 300 do), and
    how confident it was.  A disagreement is a case where the two available
    opinions differ and only a person can break the tie; a confident agreement
    is the one a reviewer is most likely to spend a minute confirming and learn
    nothing from.

    Ordering is a presentation choice, not a sampling one: every task stays in
    the queue and the denominator is unchanged.  Judging the informative ones
    first does bias a *partial* result toward the hard cases, which is worth
    saying out loud — it makes an early accuracy a lower bound rather than a
    flattering one.
    """
    if not has_table(db, "review_pre_annotations"):
        return {}
    return {row["task_id"]: (0 if row["agrees"] == 0 else 1, row["confidence"] or 0.0)
            for row in db.execute(
                "SELECT task_id, agrees, confidence FROM review_pre_annotations")}


@router.get("/tasks")
def list_tasks(kind: str | None = None, status: str | None = None, company: str | None = None,
               assignee: str | None = None, order: str = "informative",
               limit: int = Query(25, ge=1, le=200), offset: int = Query(0, ge=0),
               session=Depends(current_session),
               review=Depends(review_session), db=Depends(db_session)) -> dict[str, Any]:
    filters, parameters = [], []
    for column, value in (("kind", kind), ("status", status), ("company_id", company)):
        if value:
            filters.append(f"{column}=?")
            parameters.append(value)
    if assignee:
        # "me" rather than the caller spelling out their own name: the page asks
        # for 我的 without needing to know who it is signed in as, and nobody can
        # be locked out of their own queue by a typo.
        filters.append("task_id IN (SELECT task_id FROM review_assignments WHERE username=?)")
        parameters.append(session["username"] if assignee == "me" else assignee)
    where = f" WHERE {' AND '.join(filters)}" if filters else ""
    total = review.execute(f"SELECT count(*) FROM review_tasks{where}", parameters).fetchone()[0]
    if order == "informative":
        # The two tables live in separate databases — that is why this was
        # ordered by a column that meant nothing. The queue is 300 rows, so
        # sorting it here costs nothing and needs no cross-database join.
        priority = informativeness(db)
        every = review.execute(f"""SELECT * FROM review_tasks{where}
            ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'judged' THEN 1 ELSE 2 END,
                     created_at""", parameters).fetchall()
        ranked = sorted(every, key=lambda row: (
            0 if row["status"] == "open" else 1,
            *priority.get(row["task_id"], (2, 1.0)),
            row["created_at"] or "",
        ))
        rows = ranked[offset:offset + limit]
    else:
        rows = review.execute(f"""SELECT * FROM review_tasks{where}
            ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'judged' THEN 1 ELSE 2 END, created_at
            LIMIT ? OFFSET ?""", [*parameters, limit, offset]).fetchall()
    by_kind: dict[str, list[str]] = {}
    for row in rows:
        by_kind.setdefault(row["kind"], []).append(row["target_id"])
    resolved = {group: targets(db, group, ids) for group, ids in by_kind.items()}
    decisions: dict[str, list[dict[str, Any]]] = {}
    assignments: dict[str, list[str]] = {}
    if rows:
        placeholders = ",".join("?" for _ in rows)
        keys = [row["task_id"] for row in rows]
        for decision in review.execute(f"""SELECT * FROM review_decisions
                WHERE task_id IN ({placeholders}) ORDER BY decided_at""", keys):
            decisions.setdefault(decision["task_id"], []).append(dict(decision))
        # Shown on the card even when the queue is not filtered by owner, so that
        # picking up somebody else's row is a visible choice rather than an
        # accident — the split is a worklist, and the server does not refuse it.
        for held in review.execute(f"""SELECT task_id, username FROM review_assignments
                WHERE task_id IN ({placeholders}) ORDER BY username""", keys):
            assignments.setdefault(held["task_id"], []).append(held["username"])
    items = []
    for row in rows:
        owners = assignments.get(row["task_id"], [])
        shown, blinded = independent(decisions.get(row["task_id"], []), owners,
                                     session["username"])
        items.append({**dict(row), "origin_label": ORIGINS.get(row["origin"], row["origin"]),
                      "target": resolved.get(row["kind"], {}).get(row["target_id"]),
                      "assignees": owners, "decisions": shown,
                      # 说出来而不是静静地少一行：页面得能解释为什么这里是空的。
                      "blinded": blinded})
    return {"total": total, "limit": limit, "offset": offset, "items": items}


def wilson(correct: int, total: int, z: float = 1.96) -> dict[str, float] | None:
    """A 95% interval for a rate, so a small sample cannot read as a big one.

    「准确率 80%」 off ten judgements and off three hundred are different
    statements, and the page showed the same two characters for both.  Wilson
    rather than the normal approximation because the samples here are small and
    the rates are near 1, exactly where the normal interval runs past 100%.

    This is also the answer to 「判多少条才够」: the interval is the stopping
    rule, not a target anybody picked.  Computed at an 80% rate, the half-width
    is ±22.7 points at n=10, ±12.1 at n=40, ±7.8 at n=100, ±5.5 at n=200 and
    ±4.5 at n=300.  The first hundred judgements buy two-thirds of the
    precision the whole queue can offer; the last hundred buy one point.
    """
    if total <= 0:
        return None
    rate = correct / total
    denominator = 1 + z * z / total
    centre = (rate + z * z / (2 * total)) / denominator
    spread = z * ((rate * (1 - rate) / total + z * z / (4 * total * total)) ** 0.5) / denominator
    return {"low": round(max(0.0, centre - spread), 4), "high": round(min(1.0, centre + spread), 4),
            "half_width": round(spread, 4)}


@router.get("/metrics")
def metrics(review=Depends(review_session)) -> dict[str, Any]:
    """Human-judged accuracy, always next to the sample size that produced it."""
    # Only each task's latest judgement counts.  A reviewer who corrects an
    # earlier call must not leave the task counted under both answers, and the
    # superseded decision still stays in the log.
    rows = review.execute("""SELECT t.kind, d.judgement, d.system_verdict, d.correct_label,
                                    d.evidence_ok
        FROM review_tasks t JOIN review_decisions d ON d.decision_id=(
            SELECT decision_id FROM review_decisions
            WHERE task_id=t.task_id AND judgement IS NOT NULL
            ORDER BY decided_at DESC, decision_id DESC LIMIT 1)""").fetchall()
    by_kind: dict[str, dict[str, int]] = {}
    pairs: dict[str, list[tuple[str, str]]] = {}
    evidence: dict[str, dict[str, int]] = {}
    for row in rows:
        kind, judgement = row["kind"], row["judgement"]
        counts = by_kind.setdefault(kind, {})
        counts[judgement] = counts.get(judgement, 0) + 1
        # A "correct" says the system's own class was right, which is the pair
        # (said, said); a "wrong" contributes only once the reviewer names what
        # it should have been.  Everything else stays out of the matrix.
        if row["system_verdict"]:
            if judgement == "correct":
                pairs.setdefault(kind, []).append((row["system_verdict"], row["system_verdict"]))
            elif judgement == "wrong" and row["correct_label"]:
                pairs.setdefault(kind, []).append((row["system_verdict"], row["correct_label"]))
        if judgement == "wrong" and not row["correct_label"]:
            counts["wrong_without_label"] = counts.get("wrong_without_label", 0) + 1
        if row["evidence_ok"] is not None:
            seen = evidence.setdefault(kind, {"judged": 0, "correct": 0})
            seen["judged"] += 1
            seen["correct"] += 1 if row["evidence_ok"] else 0
    summary = {}
    for kind, counts in by_kind.items():
        decided = counts.get("correct", 0) + counts.get("wrong", 0)
        located = evidence.get(kind)
        summary[kind] = {
            **counts,
            "judged": decided + counts.get("unsure", 0),
            # "unsure" is excluded from the denominator but reported, so the rate
            # never quietly counts an undecided case as a success.
            "accuracy": round(counts.get("correct", 0) / decided, 4) if decided else None,
            "accuracy_interval": wilson(counts.get("correct", 0), decided),
            "sample_size": decided,
            **class_scores(pairs.get(kind, [])),
            # Whether the cited page was the right one is a second question with
            # its own target (§11.3: ≥ 0.90), so it gets its own rate instead of
            # being folded into the four-value one.
            "evidence_location": {**located,
                                  "accuracy": round(located["correct"] / located["judged"], 4),
                                  "accuracy_interval": wilson(located["correct"], located["judged"])}
            if located and located["judged"] else None,
        }
    queue = {row[0]: row[1] for row in review.execute(
        "SELECT status, count(*) FROM review_tasks GROUP BY status")}
    return {"by_kind": summary, "queue": queue,
            **per_reviewer(review),
            "tasks": review.execute("SELECT count(*) FROM review_tasks").fetchone()[0]}


def per_reviewer(review) -> dict[str, Any]:
    """每个账号自己那一份，以及两个人判过同一条时他们有多一致。

    这里取的是**每个人对每条任务的最后一次判定**，而 ``metrics`` 上面那段取的是
    每条任务的最后一次。差别只在双标的那一份里显现，但在那里是决定性的：按任务
    取，后判的人会把先判的人整条盖掉——分歧不是被记下来，是根本看不见。判定行
    一直都还在库里，只是从来没有人读过第二行。

    ``system_accuracy`` 这个名字是刻意的。它是**这个人判出来的系统准确率**，不是
    这个人的准确率——没有金标，就没有谁能给复核者打分。它值得并排列出来的理由
    是另一个：三个人判同一批东西却得出差很远的比率，说明词表在他们之间的理解不
    一致，该去对判据，而不是继续判下去。
    """
    latest = review.execute("""SELECT t.kind, d.reviewer, d.task_id, d.judgement
        FROM review_decisions d JOIN review_tasks t ON t.task_id=d.task_id
        WHERE d.judgement IS NOT NULL AND d.decision_id=(
            SELECT decision_id FROM review_decisions e
            WHERE e.task_id=d.task_id AND e.reviewer=d.reviewer AND e.judgement IS NOT NULL
            ORDER BY e.decided_at DESC, e.decision_id DESC LIMIT 1)""").fetchall()
    tally: dict[tuple[str, str], dict[str, int]] = {}
    ballots: dict[tuple[str, str], dict[str, str]] = {}
    for row in latest:
        counts = tally.setdefault((row["reviewer"], row["kind"]), {})
        counts[row["judgement"]] = counts.get(row["judgement"], 0) + 1
        ballots.setdefault((row["kind"], row["task_id"]), {})[row["reviewer"]] = row["judgement"]
    by_reviewer = []
    for (person, kind), counts in sorted(tally.items()):
        decided = counts.get("correct", 0) + counts.get("wrong", 0)
        by_reviewer.append({
            "reviewer": person, "kind": kind, **counts,
            "judged": decided + counts.get("unsure", 0), "sample_size": decided,
            "system_accuracy": round(counts.get("correct", 0) / decided, 4) if decided else None,
            "accuracy_interval": wilson(counts.get("correct", 0), decided)})
    # κ is a two-rater statistic, so three reviewers give three pairs.  Reporting
    # one averaged number instead would hide which two of them read a rule
    # differently, which is the only part anybody can go and fix.
    pairs: dict[str, dict[tuple[str, str], list[tuple[str, str]]]] = {}
    for (kind, _task), votes in ballots.items():
        people = sorted(votes)
        for index, first in enumerate(people):
            for second in people[index + 1:]:
                pairs.setdefault(kind, {}).setdefault((first, second), []).append(
                    (votes[first], votes[second]))
    agreement = {kind: [{"pair": [first, second], **cohen_kappa(judged)}
                        for (first, second), judged in sorted(rows.items())]
                 for kind, rows in pairs.items()}
    return {"by_reviewer": by_reviewer, "agreement": agreement,
            "double_judged": sum(1 for votes in ballots.values() if len(votes) > 1),
            # 两个人判过同一条、且判得不一样。共用账号时这个数永远是 0——不是
            # 没有分歧，是分歧被写成了同一个人改主意。
            "contested": sum(1 for votes in ballots.values() if len(set(votes.values())) > 1)}


@router.post("/seed")
def seed(request: Seed, session=Depends(requires("reviewer")),
         review=Depends(review_session), db=Depends(db_session)) -> dict[str, Any]:
    if request.kind not in review_db.TASK_KINDS:
        raise HTTPException(status_code=400, detail=f"未知的复核类型：{request.kind}")
    if request.origin not in ORIGINS:
        raise HTTPException(status_code=400, detail=f"未知的入列理由：{request.origin}")
    count = max(1, min(request.count, 500))
    created = 0
    for target_id, company_id in candidates(db, request.kind, request.origin, request.company, count):
        # INSERT OR IGNORE on (kind, target_id): re-seeding must not open a
        # second task for something already judged.
        cursor = review.execute("""INSERT OR IGNORE INTO review_tasks
            (task_id,kind,target_id,company_id,origin,status,created_at) VALUES(?,?,?,?,?,'open',?)""",
            (review_db.task_id(request.kind, target_id), request.kind, target_id, company_id,
             request.origin, review_db.now()))
        created += cursor.rowcount
    review.commit()
    return {"kind": request.kind, "origin": request.origin, "created": created,
            "requested": count, "by": session["username"]}


def judges() -> list[dict[str, Any]]:
    """The accounts that can actually judge: active, and analyst or above.

    Listed to anyone who reviews, unlike ``/api/users``, which is admin-only —
    but only the three fields a worklist needs.  Who your colleagues are is not
    a secret from them; when they last signed in, and who created them, is a
    different question and stays where it was.
    """
    from api import users_db

    db = users_db.connect()
    try:
        return [{"username": row["username"], "role": row["role"],
                 "role_label": row["role_label"]}
                for row in users_db.listing(db)
                if row["active"] and allows(row["role"], "analyst")]
    finally:
        db.close()


@router.get("/reviewers")
def reviewers(session=Depends(requires("analyst"))) -> dict[str, Any]:
    return {"items": judges()}


def partition(task_ids: list[str], people: list[str], overlap: int) -> dict[str, list[str]]:
    """Who judges what.  Deterministic, and balanced to within one task.

    Deterministic for the same reason the sampling in ``candidates`` is: a split
    that comes out differently every time it is run cannot be re-run, and the
    evaluation set has to be reproducible from the record.  Sorting by task_id
    and dealing round-robin gives that and an even share at once — and task_id is
    a content hash, so the order carries no company, verdict or risk structure
    that could quietly hand one person all the hard cases.

    The first ``overlap`` tasks go to everybody rather than being dealt.  That is
    the double-annotated slice: without it every task carries exactly one
    judgement, and 「标注一致性」 is not a number that got worse — it is a number
    that cannot be computed at all.
    """
    ordered = sorted(task_ids)
    shared, dealt = ordered[:overlap], ordered[overlap:]
    plan = {person: list(shared) for person in people}
    for index, task in enumerate(dealt):
        plan[people[index % len(people)]].append(task)
    return plan


@router.post("/assign")
def assign(request: Assignment, session=Depends(requires("reviewer")),
           review=Depends(review_session)) -> dict[str, Any]:
    """Split the queue between named accounts.

    This is the answer to three people sharing one login.  The queue is ordered
    deterministically, so three reviewers opening it saw the same first page and
    judged the same rows three times while the tail went untouched; and every
    judgement they recorded carried the same username, which makes the audit
    trail useless and κ impossible.  One account each plus a split fixes both,
    and it costs nothing at judging time — an assignment is only a filter.
    """
    people = [name.strip() for name in request.assignees if name and name.strip()]
    if not people:
        raise HTTPException(status_code=400, detail="至少要指定一个复核人")
    if len(set(people)) != len(people):
        raise HTTPException(status_code=400, detail="同一个账号不能重复指定")
    known = {row["username"]: row for row in judges()}
    for name in people:
        if name not in known:
            raise HTTPException(status_code=400,
                                detail=f"没有这个账号、已停用，或权限不足以做判定：{name}")
    filters, parameters = [], []
    for column, value in (("kind", request.kind), ("company_id", request.company),
                          ("status", request.status)):
        if value:
            filters.append(f"{column}=?")
            parameters.append(value)
    where = f" WHERE {' AND '.join(filters)}" if filters else ""
    ids = [row[0] for row in review.execute(
        f"SELECT task_id FROM review_tasks{where}", parameters)]
    if not ids:
        raise HTTPException(status_code=400, detail="没有符合条件的任务可以分派")
    overlap = max(0, min(request.overlap, len(ids)))
    plan = partition(ids, people, overlap)
    stamp = review_db.now()
    placeholders = ",".join("?" for _ in ids)
    # Re-dealing replaces the previous split over exactly these tasks.  Layering
    # a second deal on top of the first would leave people holding rows nobody
    # ever told them were theirs, which is the failure this endpoint exists to
    # end.  Tasks outside the filter keep the owners they had.
    review.execute(f"DELETE FROM review_assignments WHERE task_id IN ({placeholders})", ids)
    for person, tasks in plan.items():
        review.executemany("""INSERT INTO review_assignments
            (task_id, username, assigned_at, assigned_by) VALUES(?,?,?,?)""",
            [(task, person, stamp, session["username"]) for task in tasks])
    review.commit()
    return {"tasks": len(ids), "overlap": overlap, "by": session["username"],
            "assigned": {person: len(tasks) for person, tasks in sorted(plan.items())}}


def system_verdict_of(db, kind: str, target_id: str) -> str | None:
    """What the pipeline said about this target, as of now.

    Recorded with the judgement because "the human said wrong" is not a
    measurement on its own — reporting accuracy per verdict class needs to know
    which class the system had claimed, and a later re-run may change it.
    """
    if kind == "verdict":
        row = db.execute("""SELECT verdict, partial_subtype FROM claim_verdicts
            WHERE claim_id=?""", (target_id,)).fetchone()
        if not row:
            return None
        return f"{row['verdict']}/{row['partial_subtype']}" if row["partial_subtype"] else row["verdict"]
    if kind == "event":
        row = db.execute("SELECT risk_level FROM event_records WHERE event_id=?",
                         (target_id,)).fetchone()
        return row["risk_level"] if row else None
    row = db.execute("SELECT diff_kind FROM disclosure_diffs WHERE diff_id=?",
                     (target_id,)).fetchone()
    return row["diff_kind"] if row else None


def load_task(review, task_id: str):
    task = review.execute("SELECT * FROM review_tasks WHERE task_id=?", (task_id,)).fetchone()
    if not task:
        raise HTTPException(status_code=404, detail="review task not found")
    return task


def act(review, db, task, session: dict[str, str], action: str, judgement: str | None,
        note: str | None, status: str, correct_label: str | None = None,
        evidence_ok: bool | None = None) -> dict[str, Any]:
    review.execute("""INSERT INTO review_decisions
        (task_id,reviewer,role,action,judgement,note,system_verdict,correct_label,evidence_ok,
         decided_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (task["task_id"], session["username"], session["role"], action, judgement, note,
         system_verdict_of(db, task["kind"], task["target_id"]), correct_label,
         None if evidence_ok is None else int(evidence_ok), review_db.now()))
    review.execute("UPDATE review_tasks SET status=? WHERE task_id=?", (status, task["task_id"]))
    review.commit()
    return {"task_id": task["task_id"], "status": status, "by": session["username"],
            "action": action}


@router.post("/tasks/{task_id}/judge")
def judge(task_id: str, body: Judgement, session=Depends(requires("analyst")),
          review=Depends(review_session), db=Depends(db_session)) -> dict[str, Any]:
    task = load_task(review, task_id)
    if body.judgement not in review_db.JUDGEMENTS:
        raise HTTPException(status_code=400, detail=f"未知的判定：{body.judgement}")
    if body.correct_label is not None and not review_db.valid_label(task["kind"], body.correct_label):
        raise HTTPException(status_code=400, detail=f"该类型没有这个正确答案：{body.correct_label}")
    # A right answer is only meaningful against a wrong one; recording it beside
    # "correct" would put the same class on both sides of the matrix twice.
    if body.correct_label is not None and body.judgement != "wrong":
        raise HTTPException(status_code=400, detail="只有判定为错误时才需要给出正确答案")
    if body.evidence_ok is not None and task["kind"] != "verdict":
        raise HTTPException(status_code=400, detail="证据定位只适用于主张裁决")
    return act(review, db, task, session, "judge", body.judgement, body.note, "judged",
               body.correct_label, body.evidence_ok)


@router.post("/tasks/{task_id}/approve")
def approve(task_id: str, session=Depends(requires("reviewer")),
            review=Depends(review_session), db=Depends(db_session)) -> dict[str, Any]:
    return act(review, db, load_task(review, task_id), session, "approve", None, None, "approved")


@router.post("/tasks/{task_id}/reject")
def reject(task_id: str, body: Note | None = None, session=Depends(requires("reviewer")),
           review=Depends(review_session), db=Depends(db_session)) -> dict[str, Any]:
    return act(review, db, load_task(review, task_id), session, "reject", None,
               body.note if body else None, "rejected")



@router.get("/whoami")
def whoami(session=Depends(current_session)) -> dict[str, str]:
    """What the client is allowed to do, so it can hide what it cannot."""
    return session
