"""When an alert should be pushed, when it should not, and why.

The brief's sixth task is 事件影响分级与**预警推送**, and the outward half of it
was never built.  This is the decision layer of that half, kept as pure
functions over plain dictionaries: no database, no clock of its own, no sockets.
Everything that decides whether a person's phone buzzes is therefore testable
without any of those things, which matters more here than anywhere else in the
platform — a bug in retrieval shows a wrong number, a bug here wakes someone at
three in the morning or, worse, stays silent when it should not have.

Two ideas run through it:

* **Suppression is a decision, not an absence.**  Every rule that stops a push
  returns a reason, and the caller records it.  A delivery that never happened
  and no one can explain is indistinguishable from a bug; one with
  ``deduped_24h`` beside it is a policy working.
* **A change is a reason to speak again.**  An alert that was pushed at medium
  and is now high is new information; one that has been withdrawn is more
  urgent than one that has not.  ``changes_since_last_delivery`` is what makes
  M6-10 (预警撤销与更新通知) possible without a second pipeline.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

CHANNELS = ("inapp", "email", "webhook")
DELIVERY_STATUSES = ("queued", "dry_run", "sent", "failed", "suppressed")

#: Why a delivery was queued.  ``new`` is the first time; the rest are M6-10.
DELIVERY_REASONS = ("new", "escalated", "downgraded", "withdrawn", "updated")

#: Risk levels in ascending severity, so a subscription's floor is comparable.
LEVELS = ("low", "medium", "high")

#: Second-hand production modes are not pushed: a repost of a story already
#: pushed is the same story arriving again through another door.
QUIET_PRODUCTION_MODES = frozenset({"BRIEF", "REPOST"})

#: How long the same issue stays merged. Long enough that a story developing
#: over an afternoon arrives once; short enough that a genuinely new day is a
#: genuinely new alert.
DEDUPE_WINDOW = timedelta(hours=24)


def _levels_at_least(floor: str) -> frozenset[str]:
    if floor not in LEVELS:
        return frozenset(LEVELS)
    return frozenset(LEVELS[LEVELS.index(floor):])


def _listed(value: Any) -> list[str]:
    """A JSON array column, read whether it arrives as text or as a list."""
    if isinstance(value, list):
        return [str(item) for item in value]
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _reasons(value: Any) -> list[dict[str, str]]:
    """预警理由，读成 {code, label}。

    ``reasons_json`` 存的是对象数组；``_listed`` 把每个对象 ``str()`` 成了 Python 字典
    文本——推送载荷里因此印着 ``{'code': …}``，订阅的「只在这些情况时提醒」也因此从未
    匹配上过任何一条（I-06 排查时发现）。
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    found = []
    for item in value or []:
        if isinstance(item, dict) and item.get("code"):
            found.append({"code": str(item["code"]), "label": str(item.get("label") or item["code"])})
        elif isinstance(item, str):
            found.append({"code": item, "label": item})
    return found


def matches(subscription: dict[str, Any], alert: dict[str, Any]) -> bool:
    """Whether this subscription is interested in this alert at all.

    Every clause is an *optional* narrowing: an unset company means all
    companies.  A subscription with nothing set therefore matches everything at
    or above its risk floor, which is the sensible default for a first rule and
    the reason the floor has no "all" value.
    """
    if not subscription.get("enabled", 1):
        return False
    if subscription.get("company_id") and subscription["company_id"] != alert.get("company_id"):
        return False
    if subscription.get("issue_code") and subscription["issue_code"] != alert.get("issue_code"):
        return False
    if alert.get("risk_level") not in _levels_at_least(subscription.get("min_risk_level") or "high"):
        return False
    if subscription.get("min_credibility"):
        wanted = _levels_at_least(subscription["min_credibility"])
        if alert.get("credibility_label") not in wanted:
            return False
    phases = _listed(subscription.get("phases_json"))
    if phases and alert.get("phase") not in phases:
        return False
    wanted_reasons = set(_listed(subscription.get("reason_codes_json")))
    if wanted_reasons and not wanted_reasons & {item["code"] for item in _reasons(alert.get("reasons_json"))}:
        return False
    return True


def changes_since_last_delivery(previous: dict[str, Any] | None,
                                alert: dict[str, Any]) -> str | None:
    """Why this alert is worth saying again — or ``None`` if it is not.

    ``previous`` is the payload of the last delivery for this subscription and
    alert.  Comparing payloads rather than timestamps is deliberate: it is the
    thing the recipient actually saw, so "has anything they were told changed"
    is answerable without a second source of truth.
    """
    if previous is None:
        return "new"
    if alert.get("status") in {"withdrawn", "resolved", "closed"} and previous.get("status") not in {
            "withdrawn", "resolved", "closed"}:
        return "withdrawn"
    was, now = previous.get("risk_level"), alert.get("risk_level")
    if was in LEVELS and now in LEVELS and was != now:
        return "escalated" if LEVELS.index(now) > LEVELS.index(was) else "downgraded"
    # A new event folded into the same alert window is new information even when
    # the level did not move.
    if (alert.get("event_count") or 0) > (previous.get("event_count") or 0):
        return "updated"
    return None


def _in_quiet_hours(window: str | None, at: datetime) -> bool:
    """``"22:00-07:00"`` — inclusive of the start, exclusive of the end, and
    correct across midnight, which is the case that is always wrong."""
    if not window or "-" not in window:
        return False
    start, end = (part.strip() for part in window.split("-", 1))
    now = at.strftime("%H:%M")
    return start <= now or now < end if start > end else start <= now < end


def suppression(subscription: dict[str, Any], alert: dict[str, Any],
                recent: list[dict[str, Any]], now: datetime | None = None) -> str | None:
    """The reason not to send, or ``None`` to go ahead.

    ``recent`` is this subscription's deliveries for the same issue, most recent
    first.  The caller passes them rather than querying here so this stays a
    function of its arguments — the property that makes the policy testable.
    """
    now = now or datetime.now(timezone.utc)
    if alert.get("production_mode") in QUIET_PRODUCTION_MODES:
        return "production_mode"
    if _in_quiet_hours(subscription.get("quiet_hours"), now):
        return "quiet_hours"
    for delivery in recent:
        if delivery.get("status") not in {"sent", "dry_run"}:
            continue
        stamp = delivery.get("delivered_at") or delivery.get("queued_at")
        if not stamp:
            continue
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if now - when < DEDUPE_WINDOW:
            return "deduped_24h"
    return None


def payload(alert: dict[str, Any], claims: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """What actually goes out: the brief's seven required outputs, and nothing
    the recipient cannot act on.

    A push that says only 「有新预警」 forces a login to find out whether it
    matters.  A push carrying the summary, the subjects, the credibility and the
    reason is one a person can triage from a phone, which is the difference
    between a notification and an interruption.
    """
    # 机器字段（码）保留，是对接方已经在用的契约；每个码旁边补上给人读的标签。
    from api.labels import (CREDIBILITY_LABELS, IMPACT_OBJECT_LABELS, ISSUE_CODE_LABELS,
                            RISK_LEVEL_LABELS, VERDICT_LABELS)
    from processor.pipeline import company_display_name
    affected = _listed(alert.get("affected_objects_json") or alert.get("affected_objects"))
    return {
        "alert_id": alert.get("alert_id"),
        "company_id": alert.get("company_id"),
        "company_name": company_display_name(alert.get("company_id")),
        "issue_code": alert.get("issue_code"),
        "issue_label": ISSUE_CODE_LABELS.get(alert.get("issue_code")),
        "summary": alert.get("summary") or alert.get("headline"),
        "subjects": _listed(alert.get("subjects_json") or alert.get("subjects")),
        "affected_objects": affected,
        "affected_object_labels": [IMPACT_OBJECT_LABELS.get(item, item) for item in affected],
        "credibility": alert.get("credibility"),
        "credibility_label": alert.get("credibility_label"),
        "credibility_text": CREDIBILITY_LABELS.get(alert.get("credibility_label")),
        "evidence": [{
            "claim": claim.get("text"),
            "verdict": claim.get("verdict"),
            "verdict_label": VERDICT_LABELS.get(claim.get("verdict")),
            "document": claim.get("evidence_title"),
            "page": claim.get("evidence_page"),
        } for claim in (claims or [])[:5]],
        "window": {"start": alert.get("window_start"), "end": alert.get("window_end")},
        "reasons": _reasons(alert.get("reasons_json")),
        "risk_level": alert.get("risk_level"),
        "risk_level_label": RISK_LEVEL_LABELS.get(alert.get("risk_level")),
        "risk_score": alert.get("adjusted_risk_score"),
        "phase": alert.get("phase_label") or alert.get("phase"),
        "status": alert.get("status"),
        "event_count": alert.get("event_count"),
        # /alerts 早已下线，指向研判工作台。
        "link": f"/risks/{alert.get('alert_id')}",
    }
