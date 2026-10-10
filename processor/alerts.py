#!/usr/bin/env python3
"""Turn adjudicated events into alert records a person can act on.

An alert here is not a risk score with a headline attached.  It carries the
seven things the brief asks for — 摘要、主体、影响对象、可信度、证据、时间线入口、
预警理由 — and every one of them is read from a table that already holds the
finding, never recomputed in the presentation layer.

Two properties matter more than the trigger rules.  **Credibility is separate
from severity**: a report contradicted by the prospectus is highly alert-worthy
and barely credible, and collapsing the two into one number hides exactly the
case worth reading.  **One issue produces one alert per window**: without that,
a story carried by eleven outlets in a day arrives as eleven alerts and the
reader learns to ignore all of them.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import timedelta
import hashlib
import json
import re
import sqlite3
from typing import Any

from collector.collect import audit, connect
from processor.pipeline import (ISSUE_LABELS, company_aliases, company_display_name,
                                configured_companies, risk_bearing_clause, utc_now)
from processor.propagation import as_moment

ALERT_RISK_THRESHOLD = 60.0
# Long enough that a story running for a few days stays one alert, short enough
# that a fresh flare-up on an old issue is not swallowed by it.
MERGE_WINDOW_DAYS = 7
EVIDENCE_LIMIT = 3
EXCERPT_CHARS = 300
TIER_CREDIBILITY = {"S0": 1.0, "S1": 0.85, "S2": 0.7, "S3": 0.5}
# How much the way a piece was produced should count towards believing it.
# Every value in ``PRODUCTION_MODES`` needs a weight here: an unmapped mode falls
# to the default and silently stops carrying information, which is how the old
# table -- whose "ORIGINAL" key matched no valid mode at all -- came to be a
# constant in practice.
MODE_CREDIBILITY = {
    "ORIGINAL_INVESTIGATION": 1.0,  # 记者自采、独家信源
    "INTERPRETATION": 0.75,         # 基于公开材料的分析，有判断无取证
    "REWRITE": 0.65,                # 转述改写
    "BRIEF": 0.6,                   # 纯状态播报
    "AI_GENERATED": 0.5,            # 模板化播报，事实浅但无人核
    "REPOST": 0.4,                  # 转载，不增加独立性
    "SPONSORED": 0.25,              # 宣发稿：利益相关且无质疑
    "UGC_EMOTIONAL": 0.2,           # 情绪表达，缺事实骨架
}
REASON_LABELS = {
    "high_adjusted_risk": "阶段修正后风险分达到预警线",
    "claim_conflict": "存在与权威披露直接冲突的主张",
    "partial_negative": "负面报道中存在与披露口径有偏差的主张",
    "sensitive_phase_new_fact": "敏感披露阶段出现新增事实",
    "issue_escalating": "所属专题处于升温状态",
    "unverified_major_rumour": "高影响低可信：重大传闻待核实",
}


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS alerts(
        alert_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, issue_code TEXT, issue_id TEXT,
        lead_event_id TEXT NOT NULL, lead_version_id TEXT NOT NULL,
        window_start TEXT, window_end TEXT,
        headline TEXT NOT NULL, lead_title TEXT, summary TEXT NOT NULL,
        subjects_json TEXT NOT NULL, affected_objects_json TEXT NOT NULL,
        credibility REAL NOT NULL, credibility_label TEXT NOT NULL, credibility_basis_json TEXT NOT NULL,
        evidence_json TEXT NOT NULL, timeline_json TEXT NOT NULL,
        risk_score REAL NOT NULL, adjusted_risk_score REAL NOT NULL, risk_level TEXT NOT NULL,
        phase TEXT NOT NULL, phase_label TEXT NOT NULL, phase_coefficient REAL NOT NULL,
        issue_status TEXT, reasons_json TEXT NOT NULL,
        event_count INTEGER NOT NULL, event_ids_json TEXT NOT NULL,
        new_this_round INTEGER NOT NULL, round_id TEXT NOT NULL, status TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    # CREATE TABLE IF NOT EXISTS 对一张已经存在的表什么也不做，所以新列要单独补。
    # 不补的后果不是启动报错，而是下一次重建预警时 INSERT 当场失败——处理层是唯一
    # 能改这个库结构的地方，那就在这里补。
    present = {row[1] for row in db.execute("PRAGMA table_info(alerts)")}
    if "lead_title" not in present:
        db.execute("ALTER TABLE alerts ADD COLUMN lead_title TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS alerts_company ON alerts(company_id,adjusted_risk_score)")
    db.execute("CREATE INDEX IF NOT EXISTS alerts_issue ON alerts(company_id,issue_code,window_start)")


def credibility(event: dict[str, Any], verdicts: dict[str, int],
                independent_components: int) -> tuple[float, str, dict[str, Any]]:
    """Score how much the report itself can be relied on, and show the workings.

    Adjudication carries the most weight because it is the only component tied
    to authoritative text.  Where nothing could be checked the component sits at
    a deliberately unflattering 0.3: "we could not verify this" must not read
    like "this checks out".

    ``verdicts`` covers every event merged into the alert, so the score reflects
    everything checked in the window rather than one article's share of it.
    """
    checked = verdicts.get("SUPPORTED", 0) + verdicts.get("PARTIAL", 0) + verdicts.get("CONFLICT", 0)
    if checked:
        verdict_part = (verdicts.get("SUPPORTED", 0) + 0.5 * verdicts.get("PARTIAL", 0)) / checked
        verdict_note = f"{checked} 条主张可核验，支持 {verdicts.get('SUPPORTED', 0)}、" \
                       f"部分支持 {verdicts.get('PARTIAL', 0)}、冲突 {verdicts.get('CONFLICT', 0)}"
    else:
        verdict_part = 0.3
        verdict_note = "没有可与权威披露比对的主张"
    source_part = TIER_CREDIBILITY.get(str(event["source_tier"]), 0.5)
    # Corroboration counts independent stories inside this alert's own window.
    # Using the issue's lifetime component count instead would credit a single
    # unsourced report with every unrelated story ever filed under that issue.
    components = max(1, independent_components)
    independence_part = min(1.0, (components - 1) / 3)
    mode_part = MODE_CREDIBILITY.get(str(event["production_mode"]), 0.6)
    score = round(0.40 * verdict_part + 0.30 * source_part + 0.15 * independence_part + 0.15 * mode_part, 3)
    label = "high" if score >= 0.7 else "medium" if score >= 0.45 else "low"
    basis = {"verdict": round(verdict_part, 3), "verdict_note": verdict_note,
             "source_tier": event["source_tier"], "source": round(source_part, 3),
             "independent_components": components, "independence": round(independence_part, 3),
             "production_mode": event["production_mode"], "production": round(mode_part, 3)}
    return score, label, basis


def reasons(event: dict[str, Any]) -> list[str]:
    """Why this event is worth a person's attention, as named rules.

    A stored reason code is what makes an alert arguable: a reader can reject
    ``issue_escalating`` on this issue without having to distrust the whole
    scoring scheme.
    """
    found: list[str] = []
    if float(event["adjusted_risk_score"]) >= ALERT_RISK_THRESHOLD:
        found.append("high_adjusted_risk")
    if event["verdicts"].get("CONFLICT"):
        found.append("claim_conflict")
    if event["verdicts"].get("PARTIAL") and event["impact_direction"] == "negative":
        found.append("partial_negative")
    if (float(event["phase_coefficient"]) > 1.0 and int(event["new_fact_count"]) > 0
            and event["impact_direction"] == "negative"):
        found.append("sensitive_phase_new_fact")
    if event["issue_status"] == "escalating" and event["impact_direction"] == "negative":
        found.append("issue_escalating")
    return found


def load_events(db: sqlite3.Connection, companies: set[str] | None) -> list[dict[str, Any]]:
    query = """SELECT e.event_id,e.company_id,e.version_id,e.summary,e.event_subjects_json,
                      e.affected_objects_json,e.impact_direction,e.production_mode,e.risk_score,
                      t.timeline_at,t.time_basis,t.time_precision,t.phase,t.phase_label,
                      t.phase_coefficient,t.adjusted_risk_score,t.adjusted_risk_level,
                      t.new_fact_count,t.is_new_this_round,t.claim_count,e.issue_codes_json,
                      v.title,v.canonical_url,v.source_id,v.source_tier,v.published_at
               FROM event_records e
               JOIN event_timeline t ON t.event_id=e.event_id
               JOIN document_versions v ON v.version_id=e.version_id
               WHERE t.claim_count > 0
                 AND """ + risk_bearing_clause("e")
    parameters: list[Any] = []
    if companies:
        query += f" AND e.company_id IN ({','.join('?' for _ in companies)})"
        parameters = sorted(companies)
    verdicts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for event_id, verdict, count in db.execute("""SELECT c.event_id,w.verdict,count(*)
            FROM claim_verdicts w JOIN event_claims c ON c.claim_id=w.claim_id GROUP BY 1,2"""):
        verdicts[event_id][verdict] = count
    statuses = {(row[0], row[1]): row[2] for row in db.execute(
        "SELECT company_id,issue_code,status FROM issue_ledger")}
    components = dict(db.execute("SELECT version_id,component_id FROM propagation_component_members"))
    events: list[dict[str, Any]] = []
    for row in db.execute(query, parameters):
        base = dict(row)
        base["verdicts"] = dict(verdicts.get(base["event_id"], {}))
        # A version nothing copied is still its own independent story, so it
        # falls back to standing for itself rather than counting as nothing.
        base["component_id"] = components.get(base["version_id"], base["version_id"])
        issues = json.loads(base.pop("issue_codes_json") or "[]") or [None]
        for issue in issues:
            event = dict(base)
            event["issue_code"] = issue
            event["issue_status"] = statuses.get((event["company_id"], issue))
            events.append(event)
    return events


VERDICT_ORDER = {"CONFLICT": 0, "PARTIAL": 1, "SUPPORTED": 2, "UNVERIFIABLE": 3}


def evidence_for(db: sqlite3.Connection, event_ids: list[str]) -> list[dict[str, Any]]:
    """The claim-level citations behind the alert, strongest finding first.

    Citations are drawn from every event merged into the alert, not only the
    one whose headline is shown.  An alert that reports a conflict has to be
    able to show that conflict, even when the loudest article in the window is
    not the one that contained it.
    """
    placeholders = ",".join("?" for _ in event_ids)
    rows = db.execute(f"""SELECT c.event_id,c.text,c.issue_code,w.verdict,w.partial_subtype,
                                w.evidence_version_id,w.evidence_page,w.evidence_section,
                                w.evidence_text,w.method,w.reason,d.title AS evidence_title
        FROM event_claims c LEFT JOIN claim_verdicts w ON w.claim_id=c.claim_id
        LEFT JOIN document_versions d ON d.version_id=w.evidence_version_id
        WHERE c.event_id IN ({placeholders}) ORDER BY c.event_id,c.ordinal""", event_ids).fetchall()
    found = [{"event_id": row["event_id"], "claim": row["text"], "issue_code": row["issue_code"],
              "verdict": row["verdict"] or "UNVERIFIABLE", "partial_subtype": row["partial_subtype"],
              "method": row["method"], "reason": row["reason"],
              "evidence_version_id": row["evidence_version_id"], "evidence_title": row["evidence_title"],
              "evidence_page": row["evidence_page"], "evidence_section": row["evidence_section"],
              "evidence_excerpt": (row["evidence_text"] or "")[:EXCERPT_CHARS] or None}
             for row in rows]
    found.sort(key=lambda item: (VERDICT_ORDER.get(item["verdict"], 4),
                                 item["evidence_page"] is None))
    return found[:EVIDENCE_LIMIT]


#: 标题里的议题名。缺码时说「未归专题」而不是留空——一条没有专题的风险事项
#: 仍然是一条风险事项，标题里少一截只会让人以为是渲染坏了。
UNFILED_ISSUE = "未归专题"


#: 事由最多占标题的这么宽。按显示宽度算而不是按字符数：一个汉字约等于两个西文
#: 字符的宽度，也约等于两个西文字符的信息量。按字符数一刀切时，26 个字的中文事由
#: 说得清一件事，26 个字符的英文只够四个单词——「U.S. government reportedly…」，
#: 两条不同的事项又长得一样了。
GIST_WIDTH = 52

#: 摘要开头这些说法是主语，不是事由。公司名已经写在标题最前面了。
GIST_LEADINS = ("本公司", "该公司", "公司")

#: 可以在这里断句的标点。ASCII 的逗号和句点不在其中——它们更常出现在
#: 「432,200」和「Co., Ltd.」中间，按它们断句会把数字拦腰截断。
GIST_STOPS = ("。", "；", ";", "，", "、")

#: 掐掉主语之后剩下的时间状语。日期在列表里自己有一列（最新进展），标题里再写
#: 一遍会把真正区分这一条的那几个字挤出去。
#: 开头的括注：（美国时间）、(Reuters) 之类。
GIST_BRACKET_HEAD = re.compile(r"^[（(][^）)]{1,12}[）)]\s*")

GIST_TIME_HEAD = re.compile(r"^[于自在]\s*\d{4}\s*年[\d\s年月日至到\-—~]*[日月年]?\s*")


def display_width(text: str) -> int:
    """按显示宽度数长度：汉字与全角标点算两格，其余算一格。"""
    return sum(2 if ord(char) > 0x2E80 else 1 for char in text)


def clip_to_width(text: str, width: int) -> str:
    """截到这么宽，截过就带上省略号——省略号本身也占它的位置。"""
    if display_width(text) <= width:
        return text.rstrip()
    kept = ""
    for char in text:
        if display_width(kept) + display_width(char) > width - 2:
            break
        kept += char
    return kept.rstrip() + "…"


def event_gist(summary: str | None, company_id: str | None = None) -> str | None:
    """从事件摘要里取一句「这一条具体说的是什么事」。

    标题此前只说了**这是哪一类问题**（口径冲突／传闻待核实／专题升温），没说**这一条
    是什么**——于是 277 条里 162 条与另一条逐字相同，最狠的一条重复 10 次
    （「药明康德｜股东：报道与披露口径冲突（1 条核验点）」）。十条长得一样的待办
    等于没有排序：读者只能一条条点进去看。

    **第一个试的是主张原文，不成立**：``event_claims.text`` 是抽取出来的原始片段，
    带换行、带表格残骸、中英繁简混杂——「Total number of securities \npurchased
    \n432,200」。把它塞进标题比现在更糟。

    摘要不一样，它本来就是写给人读的一句话：「……于2024年5月13日在上海证券交易所
    回购了432,200股A股，总金额为人民币20,046,852元。」——掐掉主语、时间状语和第一个
    句读之后，剩下的正好是标题缺的那一截。

    取不到就返回 None，标题退回只说类别：宁可少一截，也不要拿半个词充数。
    """
    text = " ".join((summary or "").split())
    if not text:
        return None
    # 开头要剥三样东西：括注（美国时间）、主语（公司名）、时间状语（于2024年5月13日）。
    # 循环剥而不是各剥一次——「药明康德（美国时间）在美国……」里，括注是在主语被
    # 剥掉之后才露到开头的。
    for _round in range(4):
        before = text
        text = GIST_BRACKET_HEAD.sub("", text).lstrip("：:，,、 ")
        for lead in (*company_aliases(company_id), *GIST_LEADINS):
            if lead and text.startswith(lead):
                rest = text[len(lead):].lstrip("：:，,、 ")
                # 英文句子里公司名是主语，掐掉它剩下的是「claims its '17nm' DRAM…」——
                # 一个以小写动词开头的残句。中文没有这个问题，主语掐掉照样成话。
                if not re.match(r"^[a-z]", rest):
                    text = rest
                break
        text = GIST_TIME_HEAD.sub("", text).lstrip("：:，,、 ")
        if text == before:
            break
    # 第一个够长的句读处断开。太短的那一段多半是「自2025年起」这类状语，
    # 断在那里等于什么也没说。
    cut = len(text)
    for mark in GIST_STOPS:
        at = text.find(mark)
        if 8 <= at < cut:
            cut = at
    gist = clip_to_width(text[:cut], GIST_WIDTH)
    gist = gist.strip(" ：:，,、")
    return gist if len(gist) >= 6 else None


def headline(*, company_id: str | None, issue_code: str | None, reasons: set[str] | list[str],
             verdicts: dict[str, int], phase_label: str | None,
             event_count: int, adjusted_risk_score: float, summary: str | None = None) -> str:
    """这条风险事项叫什么——由平台自己命名，不照搬某一家媒体的标题。

    改之前这里是 ``event["title"]``：277 条预警里 265 条的标题与首发报道逐字相同，
    于是列表里出现了「China」「VOLUNTARY ANNOUNCEMENT」这种条目。那一句是某家媒体
    的编辑决定，回答的是「这篇稿子讲什么」；风险事项的标题要回答的是另一个问题——
    「这条为什么会进到我的待办里」。两个问题的答案不该共用一句话。

    原标题没有丢：``lead_version_id`` 指向那篇稿子，``lead_title`` 把它原样存下来，
    界面在事项标题下方以「首发报道」呈现。

    规则按顺序取第一条命中的，顺序即政策——与 ``api/dispositions.recommend`` 同一
    路数：能印在结论旁边的推导过程，才是可以被反驳的推导过程。冲突排在最前，因为
    「和公司自己披露的对不上」是这条链路唯一能自证的强结论。

    公司名走 ``pipeline.company_display_name``：处理层里出现任何一个公司标识都会被
    ``tests.test_migration`` 判负——E7 的零改代码迁移声明正是靠那条测试成立的。
    """
    subject = f"{company_display_name(company_id)}｜{ISSUE_LABELS.get(issue_code, UNFILED_ISSUE)}"
    reasons = set(reasons)
    conflicts = int(verdicts.get("CONFLICT", 0))
    partials = int(verdicts.get("PARTIAL", 0))

    if conflicts:
        core = f"报道与披露口径冲突（{conflicts} 条核验点）"
    elif "unverified_major_rumour" in reasons:
        core = "高影响传闻待核实，暂无独立信源佐证"
    elif "sensitive_phase_new_fact" in reasons:
        core = f"{phase_label or '敏感'}阶段出现新增事实"
    elif partials:
        core = f"报道与披露存在偏差（{partials} 条核验点）"
    elif "issue_escalating" in reasons:
        # 「集中」要有东西可集中。一篇报道说「1 篇报道集中」，读起来像模板没填完。
        core = ("专题升温，本窗口新增 1 篇报道" if event_count == 1
                else f"专题升温，{event_count} 篇报道集中")
    else:
        # 风险分不再进标题：它在列表里自己就是一列，写两遍不会让谁更知道该点哪一条。
        volume = "1 篇报道" if event_count == 1 else f"{event_count} 篇报道集中"
        core = f"待核验，{volume}"
    gist = event_gist(summary, company_id)
    return (f"{subject}：{gist}，{core}" if gist else f"{subject}：{core}")[:120]


def window_of(event: dict[str, Any]) -> str | None:
    """The day an alert window is keyed on: when the report arrived.

    Alerting is about incoming information, so windows group by publication
    rather than by event time.  Grouping by event time would put a report about
    planned 2032 capacity into a 2032 alert window, and would separate two
    articles published the same morning about events years apart.
    """
    moment = as_moment(event["published_at"]) or as_moment(event["timeline_at"])
    return moment.date().isoformat() if moment else None


def within_window(start: str | None, current: str | None) -> bool:
    first, later = as_moment(start), as_moment(current)
    if not first or not later:
        return start == current
    return (later - first) <= timedelta(days=MERGE_WINDOW_DAYS)


def previous_alerts(db: sqlite3.Connection,
                    companies: set[str] | None) -> list[dict[str, Any]]:
    """上一轮的预警，连同它们各自合并了哪些事件。必须在 DELETE 之前读。"""
    sql = "SELECT alert_id, company_id, issue_code, event_ids_json, created_at FROM alerts"
    parameters: tuple[str, ...] = ()
    if companies:
        sql += f" WHERE company_id IN ({','.join('?' for _ in companies)})"
        parameters = tuple(sorted(companies))
    found = []
    for row in db.execute(sql, parameters):
        try:
            events = set(json.loads(row["event_ids_json"]) or [])
        except (TypeError, ValueError):
            events = set()
        found.append({"alert_id": row["alert_id"], "created_at": row["created_at"],
                      "key": (row["company_id"], str(row["issue_code"] or "__none__")),
                      "events": events})
    return found


def carry_forward(grouped: dict[tuple[str, str, str], list[dict[str, Any]]],
                  previous: list[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, Any]]:
    """把上一轮的 alert_id 认回来：同公司同议题、且共享事件最多的那一条就是前身。

    在此之前 ``alert_id = sha256(公司‖议题‖窗口起点)``，而窗口起点是「窗口里最早
    那篇报道的发布日」——回填一篇更早的报道、或者合并窗口滑动一天，它就变了，id
    跟着换一个，挂在旧 id 上的处置当场成为孤儿。库里那 125 条孤儿就是这么来的，
    它们此刻还没伤到人只是因为**一条人工结论都还没有**：`chosen` 全是 NULL。

    所以 id 不能再是内容的哈希。一条风险事项的身份是**延续**出来的，不是算出来
    的：只要这一轮的窗口和上一轮的某条事项共享事件，它就还是那一条，哪怕起止日、
    标题、等级、甚至议题下的成员全变了。重叠多的先认领，一条旧事项只被认领一次，
    一个新窗口也只认一个前身——分裂出来的那半边拿新 id，这是对的，它确实是新的
    一条。

    认不回来的情况有两种，都留新 id：一条事项这一轮不再触发（它的行会被删掉），
    以及它下一轮又回来了。后者拿新 id 是刻意的——中间断过的那一段，谁也说不清
    人在归档时想的是不是同一件事。
    """
    claims = []
    for group_key, members in grouped.items():
        company_id, issue_key, _window_start = group_key
        events = {member["event_id"] for member in members}
        for old in previous:
            if old["key"] != (company_id, issue_key):
                continue
            shared = len(events & old["events"])
            if shared:
                claims.append((shared, group_key, old["alert_id"], old))
    carried: dict[tuple[str, str, str], dict[str, Any]] = {}
    taken: set[str] = set()
    for _shared, group_key, alert_id, old in sorted(claims, key=lambda c: (-c[0], c[1], c[2])):
        if group_key in carried or alert_id in taken:
            continue
        carried[group_key] = old
        taken.add(alert_id)
    return carried


def build(db: sqlite3.Connection, companies: set[str] | None = None) -> dict[str, Any]:
    db.row_factory = sqlite3.Row
    ensure_schema(db)
    if not db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='event_timeline'").fetchone():
        raise RuntimeError("event_timeline is missing; run processor.timeline first")
    round_row = db.execute("SELECT round_id FROM timeline_rounds ORDER BY ordinal DESC LIMIT 1").fetchone()
    round_id = round_row["round_id"] if round_row else "unknown"

    triggered = []
    for event in load_events(db, companies):
        why = reasons(event)
        if why:
            event["reasons"] = why
            triggered.append(event)
    triggered.sort(key=lambda item: (item["company_id"], str(item["issue_code"] or ""),
                                     str(window_of(item) or "9999"), item["event_id"]))

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    open_window: dict[tuple[str, str], str] = {}
    for event in triggered:
        key = (event["company_id"], str(event["issue_code"] or "__none__"))
        stamp = window_of(event) or "unknown"
        start = open_window.get(key)
        if start is None or not within_window(start, stamp):
            start = stamp
            open_window[key] = start
        grouped.setdefault((*key, start), []).append(event)

    # 先认前身再删：认领读的是即将被删掉的那批行。
    carried = carry_forward(grouped, previous_alerts(db, companies))

    if companies:
        placeholders = ",".join("?" for _ in companies)
        db.execute(f"DELETE FROM alerts WHERE company_id IN ({placeholders})", sorted(companies))
    else:
        db.execute("DELETE FROM alerts")

    now = utc_now()
    written = 0
    merged_events = 0
    levels: dict[str, int] = defaultdict(int)
    for (company_id, issue_key, window_start), members in grouped.items():
        # The event that can back the alert up speaks for it: a contradicted
        # claim first, then whatever was actually checked, and only then the
        # risk score.  Leading on score alone produced alerts that announced a
        # conflict while showing an article that had nothing to cite.
        lead = max(members, key=lambda item: (item["verdicts"].get("CONFLICT", 0),
                                              sum(item["verdicts"].get(name, 0)
                                                  for name in ("SUPPORTED", "PARTIAL", "CONFLICT")),
                                              float(item["adjusted_risk_score"])))
        issue_code = lead["issue_code"]
        window_verdicts: dict[str, int] = defaultdict(int)
        for member in members:
            for verdict, count in member["verdicts"].items():
                window_verdicts[verdict] += count
        independent = len({member["component_id"] for member in members})
        score, label, basis = credibility(lead, dict(window_verdicts), independent)
        found = {reason for member in members for reason in member["reasons"]}
        # 重大传闻待核实.  A severe story nobody could corroborate is the case a
        # reader most needs named, and it cannot be detected in reasons(): that
        # runs per event, while credibility is only known once the window has
        # been merged and its independent sources counted.
        if lead["adjusted_risk_level"] == "high" and label == "low":
            found.add("unverified_major_rumour")
        all_reasons = sorted(found)
        window_end = max((str(window_of(member) or "") for member in members), default="") or None
        # 新的一条才现算 id；认得回前身的沿用它的 id 与首次出现时间。created_at
        # 因此第一次真的是「这条事项是什么时候被发现的」——在此之前它是「上一次
        # 重建预警的时刻」，277 行全都一样。
        inherited = carried.get((company_id, issue_key, window_start))
        alert_id = inherited["alert_id"] if inherited else hashlib.sha256(
            f"alert\0{company_id}\0{issue_key}\0{window_start}".encode()).hexdigest()[:24]
        created_at = (inherited["created_at"] if inherited else None) or now
        issue_id = hashlib.sha256(f"issue\0{company_id}\0{issue_code}".encode()).hexdigest() if issue_code else None
        timeline_entry = {"company_id": company_id, "issue_code": issue_code,
                          "anchor_event_id": lead["event_id"], "timeline_at": lead["timeline_at"],
                          "time_basis": lead["time_basis"], "time_precision": lead["time_precision"],
                          "window_start": window_start, "window_end": window_end}
        db.execute("""INSERT OR REPLACE INTO alerts(alert_id,company_id,issue_code,issue_id,lead_event_id,
            lead_version_id,window_start,window_end,headline,lead_title,summary,subjects_json,affected_objects_json,
            credibility,credibility_label,credibility_basis_json,evidence_json,timeline_json,risk_score,
            adjusted_risk_score,risk_level,phase,phase_label,phase_coefficient,issue_status,reasons_json,
            event_count,event_ids_json,new_this_round,round_id,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (alert_id, company_id, issue_code, issue_id, lead["event_id"], lead["version_id"],
             window_start, window_end,
             headline(company_id=company_id, issue_code=issue_code, reasons=found,
                      verdicts=dict(window_verdicts), phase_label=lead["phase_label"],
                      event_count=len(members),
                      adjusted_risk_score=float(lead["adjusted_risk_score"]),
                      summary=lead["summary"]),
             (lead["title"] or "").strip() or None, lead["summary"],
             lead["event_subjects_json"], lead["affected_objects_json"],
             score, label, json.dumps(basis, ensure_ascii=False),
             json.dumps(evidence_for(db, sorted({member["event_id"] for member in members})),
                        ensure_ascii=False),
             json.dumps(timeline_entry, ensure_ascii=False),
             float(lead["risk_score"]), float(lead["adjusted_risk_score"]), lead["adjusted_risk_level"],
             lead["phase"], lead["phase_label"], float(lead["phase_coefficient"]), lead["issue_status"],
             json.dumps([{"code": code, "label": REASON_LABELS.get(code, code)} for code in all_reasons],
                        ensure_ascii=False),
             len(members), json.dumps([member["event_id"] for member in members], ensure_ascii=False),
             int(any(int(member["is_new_this_round"]) for member in members)), round_id, "open",
             created_at, now))
        written += 1
        merged_events += len(members)
        levels[lead["adjusted_risk_level"]] += 1
    reason_counts: dict[str, int] = defaultdict(int)
    for event in triggered:
        for reason in event["reasons"]:
            reason_counts[reason] += 1
    # Events with no locatable atomic claim can never carry a citation, so they
    # are excluded from alerting and counted here rather than silently dropped.
    without_claims = db.execute("SELECT count(*) FROM event_timeline WHERE claim_count=0").fetchone()[0]
    return {"round_id": round_id, "triggered_event_issues": len(triggered), "alerts": written,
            "merged_events": merged_events, "excluded_events_without_claims": without_claims,
            "by_level": dict(sorted(levels.items())),
            "by_reason": dict(sorted(reason_counts.items()))}


def run(companies: set[str] | None = None) -> dict[str, Any]:
    db = connect()
    db.row_factory = sqlite3.Row
    summary = build(db, companies)
    db.commit()
    db.close()
    audit({"source_id": "system", "outcome": "alerts_built", "alerts": summary["alerts"]})
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Build alert records from adjudicated events")
    parser.add_argument("--company", action="append", choices=configured_companies())
    args = parser.parse_args()
    print(json.dumps(run(set(args.company) if args.company else None), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
