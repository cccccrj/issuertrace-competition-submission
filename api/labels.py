"""Chinese labels for the controlled vocabularies, for anything the reader sees.

The vocabularies themselves belong to the processing layer; these are only their
display names.  They live here rather than in the processor because the API
process must not import it, and a test keeps the two in step — a code that
reaches a report or a page without a label is a bug that shows as English
shorthand in a document meant for a committee.
"""
from __future__ import annotations

#: 传播边的九类，键取自 ``processor/propagation.py`` 的 EDGE_KINDS。前端
#: api.js 的同名表与它逐字一致，两侧由 test_labels 钉住；风险简报此前把
#: CORRECTION 原样印进了传播脉络那张表。
EDGE_KIND_LABELS = {
    "FROM_DISCLOSURE": "源于披露",
    "REPOST": "转载",
    "REWRITE": "改写",
    "RECURRING": "同源重复",
    "FOLLOW_UP": "后续报道",
    "RESPONSE": "公司回应",
    "CLARIFICATION": "澄清辟谣",
    "CORRECTION": "更正撤稿",
    "CONFLICT": "口径冲突",
}

#: 风险等级的三档。前端 api.js 的 RISK_LEVEL_LABELS 是同一张表，由 test_labels 钉住。
#: low 叫「一般关注」而不是「低风险」——「低风险」会被读成「已判断没风险」。
RISK_LEVEL_LABELS = {"low": "一般关注", "medium": "中风险", "high": "高风险"}

#: 事件可信度的三档。落在处置建议的「建议依据」里，那一行此前打的是 medium。
#: 前端 api.js 的 CREDIBILITY_SHORT 是同一张表；两处都只有三行，由 test_labels
#: 钉住不许各走各的。
CREDIBILITY_LABELS = {"high": "高", "medium": "中", "low": "低"}

IMPACT_OBJECT_LABELS = {
    "operations": "经营运作",
    "financial_performance": "财务表现",
    "technology_product": "技术与产品",
    "capacity_supply": "产能与供给",
    "governance_internal_control": "治理与内控",
    "shareholders_control": "股东与控制权",
    "executives_core_staff": "高管与核心人员",
    "employees": "员工",
    "subsidiaries_related_parties": "子公司与关联方",
    "customers": "客户",
    "suppliers": "供应商",
    "partners": "合作伙伴",
    "competitors": "竞争对手",
    "investors_stock_price": "投资者与股价",
    "creditors_financing": "债权人与融资",
    "regulatory_compliance": "监管与合规",
    "reputation_brand": "声誉与品牌",
    "industry": "行业",
    "supply_chain": "供应链",
    "regional_economy": "区域经济",
}

EVENT_TYPE_LABELS = {
    "REGULATORY_COMPLIANCE": "监管与合规",
    "LITIGATION_ARBITRATION": "诉讼与仲裁",
    "FINANCIAL_AUDIT": "财务与审计",
    "IPO_REFINANCING": "发行与再融资",
    "EQUITY_CONTROL": "股权与控制权",
    "EXECUTIVE_PERSONNEL": "高管与人事",
    "CUSTOMER_SUPPLIER_OPERATIONS": "客户、供应商与经营",
    "PRODUCT_TECH_IP": "产品、技术与知识产权",
    "SAFETY_QUALITY_RECALL": "安全、质量与召回",
    "M_AND_A_ASSET_TRANSACTION": "并购与资产交易",
    "ESG_REPUTATION": "ESG 与声誉",
    "RESPONSE_CLARIFICATION": "回应与澄清",
}

ISSUE_CODE_LABELS = {
    "FUND_RAISE": "募资", "VALUATION": "估值", "TECH_LEVEL": "技术水平",
    "YIELD_CAPACITY": "良率与产能", "CUSTOMER": "客户", "SUPPLY_CHAIN": "供应链",
    "LOCALIZATION": "国产化", "EXPORT_CTRL": "出口管制", "CYCLE_PRICE": "周期与价格",
    "COMPETITION": "竞争", "SHAREHOLDER": "股东", "RELATED_PARTY": "关联交易",
    "FINANCIAL": "财务", "IP_PATENT": "知识产权", "EXEC": "高管", "STOCK_PRICE": "股价",
    "LOCKUP": "限售与解禁", "FUND_USE": "募资用途", "DISCLOSURE": "信息披露",
    "AUDIT_STAGE": "审核进程", "DRUG_DEVELOPMENT": "药物研发", "CYBERSECURITY": "数据安全",
    # 词表 v2（2026-09-03）：官方数据集迁移到 84 家公司 + 17 个行业后补入的四维，
    # 见 config/event_taxonomy_map.yaml 的 changelog_v2。
    "INDUSTRY_POLICY": "行业政策", "INDUSTRY_TREND": "行业景气与产业发展",
    "BUSINESS_EXPANSION": "业务拓展", "STRATEGY_TRANSFORM": "战略调整与转型",
}

ARTICLE_SENTIMENT_LABELS = {
    "positive": "正面",
    "neutral": "中性",
    "negative": "负面",
}

PRODUCTION_MODE_LABELS = {
    "ORIGINAL_INVESTIGATION": "原创调查",
    "INTERPRETATION": "解读评论",
    "REPOST": "转载",
    "REWRITE": "改写",
    "AI_GENERATED": "疑似机器生成",
    "SPONSORED": "疑似宣发",
    "UGC_EMOTIONAL": "情绪化 UGC",
    "BRIEF": "简讯",
}

# 原子主张的性质。它决定读者怎么取信一条主张，此前既没有判据也没有中文名。
CLAIM_TYPE_LABELS = {
    "fact": "事实陈述",
    "inference": "推断",
    "rumor": "传言",
    "opinion": "观点",
}

IMPACT_DIRECTION_LABELS = {
    "positive": "正面",
    "negative": "负面",
    "neutral": "中性",
    "uncertain": "待定",
}

IMPACT_MAGNITUDE_LABELS = {
    "major": "重大",
    "moderate": "一般",
    "minor": "轻微",
}

# 「凭什么算重大」的受控取值，见 processor.pipeline.MAGNITUDE_BASES。
MAGNITUDE_BASIS_LABELS = {
    "scale_threshold": "达到 10% / 单独披露门槛",
    "entity_continuity": "触及主体资格或持续经营",
    "forward_commitment": "改变跨期承诺",
    "single_scope": "限于单个项目或报告期",
    "routine_procedural": "程序性或例行事项",
}

# 风险分七项加减的项名，见 processor.pipeline.risk_parts。分数一直以一个数字示人，
# 而旁边的可信度从写下第一天起就展示它的四项加权贡献。
RISK_PART_LABELS = {
    "base": "基线分",
    "eventhood": "构成事件",
    "direction": "影响方向为负面",
    "magnitude": "影响强度",
    "sensitive_type": "敏感事件类型",
    "sensitive_issue": "敏感专题",
    "secondhand": "二手稿件（不计独立信源）",
    "capped": "分数封顶",
}

# 复核原因。ReviewTask.vue 此前直接把 low_confidence 这类英文码 join('、') 打给读者。
REVIEW_REASON_LABELS = {
    "low_confidence": "抽取置信度偏低",
    "no_claims": "没有可定位的原子主张",
    "unsupported_claim": "有主张无法在原文中定位",
    "unresolved_elements": "有要素未能归一",
    "issue_out_of_scope": "专题不适用于该公司所属行业，已剔除，待确认归属",
}

# The controlled vocabularies M0-4 asks for, in one place, so a page can show
# them and a test can check none of them has drifted from the processor's.
# 影响方向与影响强度是命题明确要求抽取并标准化的要素，却一直不在词表页上。
VOCABULARIES = {
    "event_type": {"title": "事件类型", "labels": EVENT_TYPE_LABELS,
                   "used_in": ("event_records", "event_types_json")},
    "issue_code": {"title": "行业专题", "labels": ISSUE_CODE_LABELS,
                   "used_in": ("event_claims", "issue_code")},
    "impact_object": {"title": "影响对象", "labels": IMPACT_OBJECT_LABELS,
                      "used_in": ("event_records", "affected_objects_json")},
    "production_mode": {"title": "稿件生产方式", "labels": PRODUCTION_MODE_LABELS,
                        "used_in": ("event_records", "production_mode")},
    "impact_direction": {"title": "影响方向", "labels": IMPACT_DIRECTION_LABELS,
                         "used_in": ("event_records", "impact_direction")},
    "impact_magnitude": {"title": "影响强度", "labels": IMPACT_MAGNITUDE_LABELS,
                         "used_in": ("event_records", "impact_magnitude")},
}


#: 机构主数据（config/organizations.yaml）的 kind。只在接口里译一次，公司档案页读
#: `kind_label`——前端不再抄一份，一个量只有一个名字。
ORG_KIND_LABELS = {
    "broker": "券商", "peer": "同业", "shareholder": "股东", "affiliate": "关联方",
    "subsidiary": "子公司", "regulator": "监管", "legislature": "立法机构",
    "rating_agency": "评级机构", "index_provider": "指数公司", "foreign_government": "外国政府",
    "exchange": "交易所", "court": "法院", "bank": "银行", "auditor": "审计机构",
}


# 「无法核验」的子类。定义在处理层（``processor.unverifiable``，规则与词表都在那里），
# 这里只取标签，前端 api.js 的同名表由 test_labels 钉住。
from processor.unverifiable import GROUPS as UNVERIFIABLE_GROUPS  # noqa: E402
from processor.unverifiable import SUBTYPES as _UNVERIFIABLE_SUBTYPES  # noqa: E402

UNVERIFIABLE_SUBTYPE_LABELS = {code: meta["label"] for code, meta in _UNVERIFIABLE_SUBTYPES.items()}
UNVERIFIABLE_SUBTYPE_GROUPS = {code: meta["group"] for code, meta in _UNVERIFIABLE_SUBTYPES.items()}


# ——— 对外口径（I-06 全平台排查）———————————————————————————————————————————
# 库里的方法码写着实现方式（llm_adjudicated、model、ai）。接口一律换成下面的中性码
# 再出去，筛选参数在服务端换回来：浏览器的开发者工具里也不该读出平台用了什么。
PUBLIC_METHODS = {"llm_adjudicated": "system_review", "rule_numeric": "numeric_rule",
                  "no_evidence": "no_evidence", "no_corpus": "no_corpus"}
INTERNAL_METHODS = {public: internal for internal, public in PUBLIC_METHODS.items()}
METHOD_LABELS = {"system_review": "系统研判", "numeric_rule": "规则比对（数值）",
                 "no_evidence": "检索无结果", "no_corpus": "尚无权威语料"}
PUBLIC_CAUSAL_METHODS = {"model": "system_review", "model_unquoted": "system_review_unquoted",
                         "rule_inquiry_alignment": "rule_inquiry_alignment",
                         "rule_institutional_sequence": "rule_institutional_sequence"}
PUBLIC_LABEL_SOURCES = {"ai": "system", "rule": "rule"}
PUBLIC_BASES = {"model": "system", "propagation_edge": "propagation_edge"}


def public_method(code: str | None) -> str | None:
    return PUBLIC_METHODS.get(code, code) if code else code


def internal_method(code: str | None) -> str | None:
    return INTERNAL_METHODS.get(code, code) if code else code


def public_causal_method(code: str | None) -> str | None:
    return PUBLIC_CAUSAL_METHODS.get(code, code) if code else code


def public_label_source(code: str | None) -> str | None:
    return PUBLIC_LABEL_SOURCES.get(code, code) if code else code


VERDICT_LABELS = {"SUPPORTED": "支持", "PARTIAL": "部分支持", "CONFLICT": "冲突",
                  "UNVERIFIABLE": "无法核验"}
ISSUE_STATUS_LABELS = {"emerging": "新出现", "escalating": "升温", "active": "活跃",
                       "cooling": "降温", "dormant": "沉寂"}
SOURCE_TIER_LABELS = {"S0": "权威披露", "S1": "官方发布", "S2": "独立媒体", "S3": "转载与线索"}
DIFF_KIND_LABELS = {"numeric": "数值变化", "modified": "表述修改", "added": "新增段落",
                    "removed": "删除段落", "not_a_change": "不是真实变化"}
TIME_BASIS_LABELS = {"event_time": "事件时间", "published_at": "发布时间", "fetched_at": "抓取时间",
                     "none": "时间不详"}
CHANNEL_LABELS = {"inapp": "站内通知", "webhook": "外部推送", "email": "邮件"}


def public_numeric_check(check: dict | None) -> dict | None:
    """数值核对的对外形状：存库时的键名带着「模型」，出接口前换掉。"""
    if not check:
        return check
    public = {key: value for key, value in check.items() if key not in ("model_disputed", "dispute")}
    public["disputed"] = bool(check.get("model_disputed"))
    public["dispute"] = {"agree_vs_magnitude": "figures_agree",
                         "slip_vs_supported": "unit_slip"}.get(check.get("dispute"), check.get("dispute"))
    return public
