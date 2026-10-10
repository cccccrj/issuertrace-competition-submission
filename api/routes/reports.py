"""Event and issue reports as a Word file, written to the brief's seven outputs.

The platform could already print a page to PDF from the browser, which is not
the same thing as a report: the brief names seven outputs a result must carry,
and a screen layout can satisfy a reader's eye while quietly dropping one of
them.  Composing the document here makes the seven sections a structure the code
holds rather than something the page happens to show, and a missing section says
it is missing instead of vanishing.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response

from api.labels import (CREDIBILITY_LABELS, DIFF_KIND_LABELS, EDGE_KIND_LABELS, IMPACT_OBJECT_LABELS,
                        ISSUE_CODE_LABELS, ISSUE_STATUS_LABELS, RISK_LEVEL_LABELS, SOURCE_TIER_LABELS,
                        TIME_BASIS_LABELS, VERDICT_LABELS)
from api.sources import source_name
from api import platform_audit
from api.report_docx import render
from api.routes.auth import client_of, current_session
from api.routes.events import db_session, get_event
from api.routes.issues import get_issue
from api.routes.risks import dossier
# 公司名只从配置来：写死一张表，加第三家公司时下发的 Word 里就会印出内部标识。
from processor.pipeline import company_display_name

router = APIRouter(prefix="/api/reports", tags=["reports"])

DIRECTION_LABELS = {"positive": "正面", "negative": "负面", "neutral": "中性"}
MAGNITUDE_LABELS = {"major": "重大", "moderate": "中等", "minor": "轻微"}
ACTION_LABELS = {"immediate_review": "立即复核", "monitor": "持续监测", "none": "无需动作",
                 "clarify": "建议澄清"}
MISSING = "（本条尚无数据）"
#: 多源信息的三组。与研判档案上的分组同名同义：权威披露建立事实，独立报道提供
#: 线索，转述只说明传播范围——把三者并成一个「信源数」，正是这套平台一开始就
#: 拒绝做的事。
SOURCE_GROUP_LABELS = {"authoritative": "权威披露", "independent": "独立报道",
                       "relay": "传播与转述"}


def label(mapping: dict[str, str], key: Any) -> str:
    return mapping.get(key, key if key else MISSING)


def when(event: dict[str, Any]) -> str | None:
    """An event's date as a reader should take it.

    A deadline or target still in the future when reported is printed as such,
    beside the day it was reported — printed bare, 2032-01-01 reads as the day
    something happened (I-04).
    """
    if event.get("target_time"):
        return f'发布于 {event.get("published_at") or "-"} · 计划/截止 {event["target_time"]}'
    return event.get("event_time") or event.get("published_at")


def event_blocks(detail: dict[str, Any]) -> list[tuple[str, Any]]:
    claims = detail.get("claims") or []
    cited = [claim for claim in claims if claim.get("evidence_page")]
    alert = (detail.get("alerts") or [None])[0]
    propagation = (detail.get("propagation") or [None])[0]
    timeline = detail.get("timeline") or {}
    blocks: list[tuple[str, Any]] = [
        ("title", detail["summary"]),
        ("note", f'{company_display_name(detail.get("company_id"))} · '
                 f'{when(detail) or "时间不详"} · '
                 f'来源 {source_name(detail.get("source_id")) or "-"}'),
        ("h1", "一、事件摘要"), ("p", detail["summary"]),
        ("h1", "二、事件主体"),
        ("p", "、".join(detail.get("event_subjects") or []) or MISSING),
        ("h1", "三、影响对象"),
        ("p", "、".join(label(IMPACT_OBJECT_LABELS, item)
                        for item in (detail.get("affected_objects") or [])) or MISSING),
        ("h1", "四、影响方向与程度"),
        ("p", f'{label(DIRECTION_LABELS, detail.get("impact_direction"))}·'
              f'{label(MAGNITUDE_LABELS, detail.get("impact_magnitude"))}'
              f'（风险分 {detail.get("risk_score")}，'
              f'等级 {label(RISK_LEVEL_LABELS, detail.get("risk_level"))}）'),
        ("h1", "五、可信度与信源"),
    ]
    if alert:
        blocks.append(("p", f'可信度 {alert.get("credibility")}（{alert.get("credibility_label")}）'))
        blocks += [("bullet", f'{item.get("label", item)}')
                   for item in (alert.get("credibility_basis") or []) if isinstance(item, dict)]
    else:
        blocks.append(("p", "该事件未触发预警，因此没有预警层计算的可信度。" + MISSING))
    if propagation:
        blocks.append(("p", f'独立信源 {propagation.get("source_count")} 家，'
                            f'同源报道 {propagation.get("member_count")} 篇'
                            f'（{propagation.get("first_published_at")} 起）'))
    counts = {}
    for claim in claims:
        counts[claim.get("verdict") or "PENDING"] = counts.get(claim.get("verdict") or "PENDING", 0) + 1
    blocks.append(("p", "主张裁决：" + ("、".join(
        f'{label(VERDICT_LABELS, key)} {value} 条' for key, value in sorted(counts.items()))
        or MISSING)))

    blocks.append(("h1", "六、证据来源"))
    if cited:
        blocks.append(("table", [["主张", "裁决", "出处", "页码"]] + [
            [(claim["text"] or "")[:60], label(VERDICT_LABELS, claim.get("verdict")),
             claim.get("evidence_section") or "-", str(claim.get("evidence_page"))]
            for claim in cited[:20]]))
        for claim in cited[:20]:
            inquiry = claim.get("inquiry")
            if inquiry:
                blocks.append(("bullet", f'该主张所涉事项见问询第 {inquiry["number"]} 问'
                                         f'「关于{inquiry["topic"]}」，回复第 {inquiry["answer_page"]} 页，'
                                         f'其后披露修改 {len(inquiry["changes"])} 处'))
    else:
        blocks.append(("p", "本事件的主张均未取得可定位的披露证据，因此不列证据表。" + MISSING))

    blocks.append(("h1", "七、事件时间线与阶段"))
    if timeline:
        blocks.append(("p", f'{timeline.get("timeline_at")}（{label(TIME_BASIS_LABELS, timeline.get("time_basis"))}）· '
                            f'披露阶段 {timeline.get("phase_label")}，系数 {timeline.get("phase_coefficient")} · '
                            f'阶段修正后风险 {timeline.get("adjusted_risk_score")}'))
        blocks.append(("p", f'本轮新增：{"是" if timeline.get("is_new_this_round") else "否"} · '
                            f'主张 {timeline.get("claim_count")} 条，其中新事实 '
                            f'{timeline.get("new_fact_count")} 条'))
    else:
        blocks.append(("p", MISSING))

    blocks.append(("h1", "八、预警理由与建议动作"))
    if alert:
        blocks += [("bullet", reason.get("label", str(reason)))
                   for reason in (alert.get("reasons") or [])]
    blocks.append(("p", f'建议动作：{label(ACTION_LABELS, detail.get("recommended_action"))}'))
    blocks.append(("note", "本报告由平台按存储结论生成，未作任何人工改写；"
                           "缺数据的条目直接标注，不以叙述填补。"))
    return blocks


def issue_blocks(detail: dict[str, Any]) -> list[tuple[str, Any]]:
    issue = detail
    events = detail.get("timeline") or []
    return [
        ("title", f'专题报告：{label(ISSUE_CODE_LABELS, issue.get("issue_code"))}'),
        ("note", f'{company_display_name(issue.get("company_id"))} · '
                 f'首现 {issue.get("first_seen_at") or "-"} · 最近 {issue.get("last_seen_at") or "-"}'),
        ("h1", "一、专题状态"),
        ("p", f'状态 {label(ISSUE_STATUS_LABELS, issue.get("status"))} · 风险分 {issue.get("risk_score")} '
              f'（等级 {label(RISK_LEVEL_LABELS, issue.get("risk_level"))}）'),
        ("h1", "二、规模"),
        ("p", f'事件 {issue.get("event_count")} 起 · '
              f'独立信源 {issue.get("independent_source_count")} 家 · '
              f'报道 {issue.get("document_count")} 篇 · '
              f'近 30 天新增 {issue.get("recent_event_count")} 起'),
        ("p", "主张裁决：" + ("、".join(f'{label(VERDICT_LABELS, key)} {value} 条'
                                        for key, value in sorted((issue.get("verdicts") or {}).items()))
                              or MISSING)),
        ("h1", "三、状态变化"),
        # Stored history, not a derived recommendation: the trail belongs in a
        # printed record because it will read the same a year from now.
        ("table", [["时间", "由", "至", "事件数", "理由"]] + [
            [row.get("changed_at") or "-", label(ISSUE_STATUS_LABELS, row.get("previous_status")),
             label(ISSUE_STATUS_LABELS, row.get("status")),
             f'{row.get("previous_event_count")} → {row.get("recent_event_count")}',
             (row.get("reason") or "")[:40]]
            for row in (detail.get("status_history") or [])[:20]])
        if detail.get("status_history") else ("p", "状态自首次判定以来没有变过。"),
        ("h1", "四、事件清单"),
        ("table", [["时间", "摘要", "风险"]] + [
            [when(event) or "-",
             (event.get("summary") or "")[:60], label(RISK_LEVEL_LABELS, event.get("risk_level"))]
            for event in events[:30]]) if events else ("p", MISSING),
        ("note", f'共 {len(events)} 起事件，表中列出前 {min(len(events), 30)} 起。'),
        ("note", "专题报告的每一行都来自专题台账与事件台账，未作合并改写。"),
    ]


def risk_blocks(detail: dict[str, Any]) -> list[tuple[str, Any]]:
    """风险简报：一条风险事项可以下发的那一份。

    它不是研判档案的截图，也不是事件报告的改名。事件报告回答「这一篇稿子讲了什么、
    七项输出是什么」；风险简报回答的是另一个问题——**这条风险现在怎么样了，谁在管，
    依据是什么**。所以它以结论与处置开头，证据与传播在后面支撑，而不是反过来。

    每一节都可能为空，而空要说出来。缺数据的条目直接标注，不以叙述填补——这条规矩
    与事件报告同源：一份读起来完整、实则少了一节的材料，比一份承认自己缺东西的材料
    更危险，因为它不会引起复核。
    """
    disposition = detail.get("disposition") or {}
    sources = detail.get("sources") or []
    evidence = detail.get("evidence") or []
    cited = [item for item in evidence if item.get("evidence_page")]
    propagation = detail.get("propagation") or {}
    nodes = propagation.get("nodes") or []
    origins = [node for node in nodes if node.get("action") == "ORIGIN"]
    history = detail.get("history") or []
    changes = detail.get("changes") or []

    blocks: list[tuple[str, Any]] = [
        ("title", detail.get("headline") or MISSING),
        ("note", f'{company_display_name(detail.get("company_id"))} · '
                 f'{detail.get("issue_label") or "未归专题"} · '
                 f'{label(RISK_LEVEL_LABELS, detail.get("risk_level"))} '
                 f'{detail.get("adjusted_risk_score")} · '
                 f'{disposition.get("status_label") or "待研判"}'),
        ("h1", "一、研判结论"),
        ("p", detail.get("summary") or MISSING),
        ("p", f'窗口 {detail.get("window_start") or "-"} — {detail.get("window_end") or "-"} · '
              f'合并 {detail.get("event_count")} 个事件 · '
              f'事件可信度 {float(detail.get("credibility") or 0):.2f}'
              f'（{label(CREDIBILITY_LABELS, detail.get("credibility_label"))}）'),
        ("h1", "二、处置与责任人"),
        ("p", f'当前状态：{disposition.get("status_label") or "待研判"} · '
              f'责任人：{disposition.get("owner") or "未指派"} · '
              f'系统建议：{disposition.get("recommended_label") or MISSING} · '
              f'人工选择：{disposition.get("chosen_label") or "尚未选择"}'),
    ]
    if disposition.get("diverged"):
        # 人机分歧是这份材料最该被看见的一行，不是脚注。
        blocks.append(("p", "人工选择与系统建议不同，以人工结论为准。"))
    blocks += [("bullet", f'{item.get("label")}：{item.get("value")}')
               for item in (detail.get("recommended_basis") or []) if isinstance(item, dict)]
    if detail.get("note"):
        blocks.append(("p", f'处置说明：{detail["note"]}'))

    blocks.append(("h1", "三、预警理由"))
    reasons = detail.get("reasons") or []
    blocks += ([("bullet", reason.get("label", str(reason))) for reason in reasons]
               or [("p", MISSING)])

    blocks.append(("h1", "四、证据核验"))
    if cited:
        blocks.append(("table", [["主张", "裁决", "出处", "页码"]] + [
            [(item.get("claim") or "")[:60], label(VERDICT_LABELS, item.get("verdict")),
             item.get("evidence_section") or "-", str(item.get("evidence_page"))]
            for item in cited[:20]]))
    else:
        blocks.append(("p", "本事项的主张均未取得可定位的披露证据，因此不列证据表。" + MISSING))

    blocks.append(("h1", "五、多源信息"))
    if sources:
        counted = {key: sum(1 for item in sources if item.get("group") == key)
                   for key in SOURCE_GROUP_LABELS}
        blocks.append(("p", " · ".join(f'{name} {counted[key]} 篇'
                                       for key, name in SOURCE_GROUP_LABELS.items())))
        blocks.append(("table", [["时间", "来源", "等级", "类别", "标题"]] + [
            [(item.get("published_at") or "-")[:10], source_name(item.get("source_id")) or "-",
             label(SOURCE_TIER_LABELS, item.get("source_tier")),
             SOURCE_GROUP_LABELS.get(item.get("group"), "-"), (item.get("title") or "")[:40]]
            for item in sources[:20]]))
    else:
        blocks.append(("p", MISSING))

    blocks.append(("h1", "六、传播脉络"))
    if nodes:
        blocks.append(("p", f'传播节点 {len(nodes)} 个，其中首发 {len(origins)} 个。'))
        blocks.append(("table", [["时间", "角色", "来源", "标题"]] + [
            [(node.get("published_at") or "-")[:10],
             ("首发" if node.get("action") == "ORIGIN"
              else label(EDGE_KIND_LABELS, node.get("action"))),
             source_name(node.get("source_id")) or "-", (node.get("title") or "")[:40]]
            for node in nodes[:15]]))
    else:
        blocks.append(("p", "这条事项的报道没有进入任何同源簇，因此没有传播链。" + MISSING))

    blocks.append(("h1", "七、披露变化"))
    if changes:
        blocks.append(("table", [["章节", "类型", "页", "变化"]] + [
            [(item.get("section") or "-")[:20], label(DIFF_KIND_LABELS, item.get("diff_kind")),
             str(item.get("right_page") or "-"), (item.get("right_text") or "")[:40]]
            for item in changes[:15]]))
    else:
        blocks.append(("p", MISSING))

    blocks.append(("h1", "八、处置流水"))
    if history:
        blocks.append(("table", [["时间", "由", "至", "操作人", "说明"]] + [
            [item.get("occurred_at") or "-", item.get("from_label") or "-",
             item.get("to_label") or "-", item.get("actor") or "-",
             (item.get("note") or "")[:40]]
            for item in history[:20]]))
    else:
        blocks.append(("p", "这条事项还没有人处置过，因此没有流水。"))

    blocks.append(("note", "本简报由平台按存储结论生成，未作任何人工改写；缺数据的条目直接标注，"
                           "不以叙述填补。事件可信度衡量的是这条信息本身的证据强度，"
                           "与系统对自身抽取结果的置信度是两个数，不合成一个。"))
    return blocks


def report_name(company_id: str | None, when: str | None, kind: str) -> str:
    """「长鑫科技_2026-08-29_风险事项报告」：文件名不再带内部编号（I-06）。"""
    parts = [company_display_name(company_id) if company_id else None, (when or "")[:10] or None, kind]
    return re.sub(r'[\\/:*?"<>|\s]+', "", "_".join(part for part in parts if part))


def as_docx(blocks: list[tuple[str, Any]], name: str, fallback: str = "report") -> Response:
    return Response(
        content=render(blocks),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        # ASCII in the plain parameter, percent-encoded UTF-8 in the starred one,
        # which is what browsers actually read for a Chinese name.
        headers={"Content-Disposition": f"attachment; filename={fallback}.docx; "
                                        f"filename*=UTF-8''{quote(name)}.docx"})


def exported(kind: str, object_id: str, session: dict[str, Any], request: Request) -> None:
    """一份材料离开平台，留一行。

    方案 §7.2：导出或上报行为须记录操作者、时间、版本和目标。三个端点各调一次而不是
    塞进 ``as_docx``——那个函数只认识一串 blocks，它不知道这份东西是哪条事项的，
    而「导出过什么」里最要紧的恰恰是那个 id。

    **留痕写在成文之后。** 取数失败的请求会在上面几行就抛掉，不会在这里留下一条
    「导出成功」；而一份已经生成、正要发出去的材料，无论下游怎样，它都已经被导出了。
    """
    platform_audit.record("report_export", actor=session["username"], role=session.get("role"),
                          object_kind=kind, object_id=object_id, client=client_of(request))


@router.get("/events/{event_id}.docx")
def event_report(event_id: str, request: Request, db=Depends(db_session),
                 session=Depends(current_session)) -> Response:
    detail = get_event(event_id, db)
    document = as_docx(event_blocks(detail),
                       report_name(detail.get("company_id"), detail.get("event_time") or detail.get("published_at"),
                                   "事件报告"), "event-report")
    exported("event", event_id, session, request)
    return document


@router.get("/issues/{issue_id}.docx")
def issue_report(issue_id: str, request: Request, db=Depends(db_session),
                 session=Depends(current_session)) -> Response:
    detail = get_issue(issue_id, db)
    document = as_docx(issue_blocks(detail),
                       report_name(detail.get("company_id"), detail.get("last_seen_at"),
                                   f'{label(ISSUE_CODE_LABELS, detail.get("issue_code"))}专题报告'), "issue-report")
    exported("issue", issue_id, session, request)
    return document


@router.get("/risks/{alert_id}.docx")
def risk_report(alert_id: str, request: Request, db=Depends(db_session),
                session=Depends(current_session)) -> Response:
    """研判档案的可下发版本。演示动线第 6 步要的就是这一份。"""
    detail = dossier(alert_id, db, session)
    document = as_docx(risk_blocks(detail),
                       report_name(detail.get("company_id"), detail.get("window_end"), "风险事项报告"),
                       "risk-report")
    exported("risk", alert_id, session, request)
    return document
