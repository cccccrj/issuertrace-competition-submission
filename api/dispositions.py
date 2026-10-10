"""What to do about an alert, derived from fields the platform already has.

M6-11 是功能大纲里唯一一项「业务价值最高、且不需要新数据」的未做功能：处置方式
由裁决状态、可信度、是否已解释、披露阶段四项决定，全部已在库中。

The recommendation is deliberately shallow — four rules over four fields, in a
stated order — because a recommendation whose derivation cannot be printed
beside it is advice nobody can argue with, and an analyst who cannot argue with
it will either follow it blindly or ignore it entirely. Every result carries its
`basis`, and the interface labels the whole thing 「建议，非结论」.

The same discipline as `credibility_basis`: the number is less useful than the
four things that made it.
"""
from __future__ import annotations

from typing import Any

from .labels import CREDIBILITY_LABELS

#: The action set a financial-communications desk actually has.
ACTIONS = {
    "cold": {"label": "冷处理", "detail": "记录并观察，不主动回应"},
    "formal_reply": {"label": "正式回复", "detail": "先核实事实，再由公司口径统一回应"},
    "clarification": {"label": "澄清公告", "detail": "存在与披露冲突的主张，需公告澄清"},
    "escalate": {"label": "上报项目组", "detail": "处于审核关键阶段且风险高，超出日常处置"},
}

#: Phases where a high-risk item stops being a communications question and
#: becomes a filing question.
CRITICAL_PHASES = frozenset({"FILING", "MEETING", "REGISTRATION", "ISSUANCE"})

#: 处置状态。与 ACTIONS 是两个量：动作回答「要不要回应、怎么回应」，状态回答
#: 「这条现在走到哪一步了」。此前只有动作，于是「已经有人在查了」这件事在界面上
#: 无处可写——一条预警要么没人动过，要么被记了一个动作然后仍旧躺在列表里。
#:
#: ``reporting`` 的键没改，含义改了（修改思路 v2）：此前是「核查完成，等上报」——
#: 上报给谁、上报之后谁接着办，都不在流程里。现在它就是「已上报，等核查负责人
#: 复核」，负责人换成那位核查负责人；``returned`` 是复核的另一个出口。键不改是
#: 因为库里、流水里、审计页里都存着它，换键就得迁移一遍人写下的历史。
STATUSES = {
    "pending": {"label": "待研判", "detail": "系统已给出建议，还没有人判过"},
    "investigating": {"label": "核查中", "detail": "已发起专项核查，等核查结论"},
    "reporting": {"label": "待上级复核", "detail": "已给出人工结论并上报，等核查负责人复核"},
    "returned": {"label": "退回补证", "detail": "核查负责人退回，提交人按退回意见补充核验"},
    "tracking": {"label": "持续跟踪", "detail": "结论已定，但事项未了，继续观察"},
    "archived": {"label": "已归档", "detail": "本轮处置结束，不再出现在待办里"},
}

#: 合法的状态转移。**这张表就是流程**——写成数据而不是写成 if，是为了让
#: 「从这里能去哪」既能被测试穷举，也能直接送给界面当按钮用（界面因此不必
#: 自己再实现一遍流程，两份实现迟早会各说各话）。
#:
#: 几条看起来多余、实际必须有的边：``reporting -> investigating`` 是上级认为要
#: 正式查一轮；``tracking -> investigating`` 是「跟踪期间出了新料」；
#: ``pending -> reporting`` 是分析师判完即上报——证据足够时不必为了走流程先
#: 「核查」一遍。没有这些边，人只能把事项归档再重开，而重开会丢掉此前的全部历史。
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "pending": ("investigating", "reporting", "tracking", "archived"),
    "investigating": ("reporting", "tracking", "archived"),
    "reporting": ("returned", "investigating", "tracking", "archived"),
    "returned": ("reporting", "investigating"),
    "tracking": ("investigating", "reporting", "archived"),
    "archived": (),
}

#: 哪些转移要更高的角色。归档要：它把事项从所有人的待办里拿走。
TRANSITION_ROLE = {"archived": "reviewer"}

#: 从这些状态出发的**每一步**都要核查负责人：事项已经交到上级手里，下一步是上级
#: 的决定。提交人不能自己把上报撤回来改成跟踪——那等于绕过了复核。
REVIEW_GATE = {"reporting": "reviewer"}


def required_role(current: str | None, target: str) -> str | None:
    """这一步要什么角色；两条规则取较高的那个（二者目前都是 reviewer）。"""
    return REVIEW_GATE.get(current or "pending") or TRANSITION_ROLE.get(target)


#: 人工研判结论。与状态又是两个量：状态回答「走到哪一步」，结论回答「这件事成不成立」。
#: 此前平台只有前者——一条事项可以被归档，却说不出归档时认为风险成立还是误报。
CONCLUSIONS = {
    "confirmed": {"label": "风险成立", "detail": "核心事实有权威或独立证据支持"},
    "partial": {"label": "部分成立", "detail": "部分主张成立，部分被披露否定或无法核实"},
    "insufficient": {"label": "证据不足", "detail": "影响可能较大，但现有证据撑不起结论"},
    "undetermined": {"label": "暂无法判断", "detail": "需要等待新的披露或回应"},
    "false_positive": {"label": "误报", "detail": "不构成风险：事实被否定、主体错配或同源重复"},
}

#: 人工定级可取的三档，与处理层 ``risk_level`` 同一套码。
LEVELS = ("high", "medium", "low")


def step_problem(current: str | None, target: str, *, conclusion: str | None,
                 note: str | None) -> str | None:
    """这一步缺了什么；缺就说缺，不缺返回 None。

    与 ``required_role`` 分开：那是「谁能走」，这是「走之前要带上什么」。两条规则
    都写在这里而不是界面上——界面会把它们画成必填项，但拦住请求的是服务端。
    """
    current = current or "pending"
    if target == "reporting" and current != "reporting" and not conclusion:
        return "上报前先给出人工结论：上级复核的是结论，不是一条还没人判过的事项。"
    if target == "returned" and current != "returned" and not (note or "").strip():
        return "退回必须写明退回意见：提交人要知道补什么。"
    return None


def can_transition(current: str | None, target: str) -> bool:
    """Whether the flow allows this step.

    ``current`` may be ``None`` for a row that predates the status column; it is
    treated as ``pending``, which is what an undecided row is.
    """
    return target in TRANSITIONS.get(current or "pending", ())


def transitions_from(current: str | None) -> list[dict[str, Any]]:
    """每个合法去向，带上它的标签和所需角色。

    角色只作为数据返回，不在这里判定——判定要读会话，而这个模块刻意不认识会话。
    调用方用 ``api.auth.allows`` 过滤，并把不允许的那些**置灰而不是隐藏**：
    看不见一项能力和没有这项能力，在页面上长得一模一样。
    """
    return [{"status": target, **STATUSES[target],
             "requires": required_role(current, target)}
            for target in TRANSITIONS.get(current or "pending", ())]


def recommend(alert: dict[str, Any], claims: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """One action, with the fields that produced it.

    Rules are evaluated in order and the first match wins. The order is the
    policy: a conflict with the company's own disclosure outranks everything,
    because a public contradiction does not wait for the审核 calendar; a
    high-risk item in a filing window outranks routine handling; an item the
    exchange has already asked about and the company already answered is a
    downgrade rather than a dismissal, which is the distinction the whole
    already_explained subtype exists to draw.
    """
    claims = claims or []
    verdicts = [claim.get("verdict") for claim in claims]
    subtypes = [claim.get("partial_subtype") for claim in claims]
    level = alert.get("risk_level")
    credibility = alert.get("credibility_label")
    phase = alert.get("phase")
    reasons = alert.get("reasons") or []
    explained = sum(1 for subtype in subtypes if subtype == "already_explained")

    basis = [
        {"code": "verdicts", "label": "主张裁决",
         "value": f"冲突 {verdicts.count('CONFLICT')} · 部分支持 {verdicts.count('PARTIAL')} · "
                  f"支持 {verdicts.count('SUPPORTED')} · 无法核验 {verdicts.count('UNVERIFIABLE')}"},
        {"code": "credibility", "label": "可信度",
         "value": CREDIBILITY_LABELS.get(credibility, credibility) or "—"},
        {"code": "already_explained", "label": "已解释主张", "value": f"{explained} / {len(claims)}"},
        {"code": "phase", "label": "披露阶段",
         "value": alert.get("phase_label") or phase or "—"},
    ]

    def result(action: str, why: str) -> dict[str, Any]:
        return {"recommended": action, **ACTIONS[action], "why": why, "basis": basis}

    if "CONFLICT" in verdicts and credibility == "high":
        return result("clarification",
                      "存在与披露原文冲突的主张，且证据可信度高——沉默会被读作默认。")
    if level == "high" and phase in CRITICAL_PHASES:
        return result("escalate",
                      "处于审核关键阶段且风险为高，处置后果超出日常口径范围。")
    if "unverified_major_rumour" in reasons or (level == "high" and credibility == "low"):
        return result("formal_reply",
                      "影响大但证据薄：先核实，再以统一口径正式回复，不在未核实前表态。")
    if claims and explained * 2 >= len(claims):
        return result("cold",
                      "过半主张交易所已问、公司已答，附已解释出处即可，不必重复回应。")
    return result("cold", "未触发需要主动回应的条件，记录并观察。")
