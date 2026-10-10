"""What a reader is told about a failure, and what stays in the technical log (I-06).

The audit page used to print the raw text a failure left behind — the model's
name, "no structured-output mode succeeded", a schema keyword, a proxy URL.  The
raw text is kept where it was written (the database row, the collector's log);
the platform only ever returns a category from this module.
"""
from __future__ import annotations

import hashlib
import re

#: (category, label, pattern) — first match wins.
ERROR_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (code, label, re.compile(pattern, re.I)) for code, label, pattern in (
        ("parse_failed", "智能研判结果格式不合规，已跳过", r"structured.?output|schema|validat|maxItems|minimum|json"),
        ("service_unavailable", "智能研判服务不可用", r"api.?key|not configured|gateway|unauthori[sz]ed|quota|rate.?limit|429"),
        ("timeout", "请求超时", r"timeout|timed out"),
        ("access_denied", "来源站点拒绝访问或需要人工验证", r"robots|403|forbidden|challenge|captcha|login|405|refused"),
        ("network", "网络连接异常", r"proxy|connection|transport|ssl|dns|max retries|stream error|resolve"),
        ("not_saved", "页面未能保存", r"not saved|no bytes|empty"),
        ("duplicate_run", "重复运行，已中止", r"duplicate|superseded"),
        ("target_rejected", "推送目标拒收", r"allowlist|status \d{3}|http ?error|rejected"),
    ))
FALLBACK = ("other", "处理失败")


def classify(raw: str | None) -> dict[str, str] | None:
    """A category and its label for a raw failure text; None when nothing failed."""
    if not raw or not str(raw).strip():
        return None
    for code, label, pattern in ERROR_RULES:
        if pattern.search(str(raw)):
            return {"category": code, "label": label}
    return {"category": FALLBACK[0], "label": FALLBACK[1]}


def reference(internal_id: str | None, prefix: str = "B") -> str | None:
    """An opaque short code a reader can quote and an operator can search for.

    The internal id (``ai-1788432593-event-label-v2-246c0eb9``) names the
    labeller and its prompt version; the reference names nothing.
    """
    if not internal_id:
        return None
    return f"{prefix}-{hashlib.sha256(internal_id.encode()).hexdigest()[:6]}"


#: The status a processing run ends in.
RUN_STATUS_LABELS = {"completed": "完成", "ai_label_failed": "智能研判失败", "running": "进行中",
                     "aborted_duplicate_run": "重复运行，已中止", "failed": "失败"}

# Collector and processor log outcomes: seventy codes, read by shape rather than
# listed one by one, so a new adapter's outcome is labelled without a code change.
OUTCOME_RESULT_RULES = (
    (re.compile(r"(error|failed|refused|unsafe)", re.I), "failure", "失败"),
    (re.compile(r"(challenge|suspended|unavailable|insufficient|disallowed|degraded)", re.I), "attention", "需关注"),
    (re.compile(r"(excluded|absent|skipped|cached)", re.I), "skipped", "跳过"),
)
OUTCOME_ACTIONS = (
    (r"^robots", "检查站点访问规则"), (r"sitemap|discovery", "发现文章"), (r"search", "检索来源站点"),
    (r"detail|document|page_success|^success$|archive", "抓取文章"), (r"manual", "人工导入"),
    (r"api_(backfill|incremental)", "接口同步"), (r"claim_verdict", "主张核验"),
    (r"ai_event_label|event_types|event_impact|production_mode|article_sentiment", "事件研判"),
    (r"causal", "因果关联"), (r"alert", "生成风险事项"), (r"timeline|phase", "构建时间线"),
    (r"propagation", "构建传播链"), (r"finaliz|processing|normaliz|repair|aligned|backfill|reindex", "数据整理"),
    (r"listing_pages", "剔除列表页"), (r"scheduler|campaign", "调度"), (r"market|financial", "行情与财务"),
    (r"inquiry|disclosure_diff|claim_index|pre_annotation", "披露分析"), (r"storage", "存储校验"),
    (r"transport|http_error", "网络请求"),
)


def outcome_label(outcome: str | None) -> dict[str, str]:
    """``cninfo_document_error`` → {action: 抓取文章, result: failure, result_label: 失败}."""
    code = outcome or ""
    action = next((label for pattern, label in OUTCOME_ACTIONS if re.search(pattern, code, re.I)), "其他操作")
    for pattern, result, label in OUTCOME_RESULT_RULES:
        if pattern.search(code):
            return {"action": action, "result": result, "result_label": label}
    return {"action": action, "result": "success", "result_label": "成功"}
