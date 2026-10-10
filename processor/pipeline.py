#!/usr/bin/env python3
"""Turn preserved collection evidence into reviewable event records.

The processor never mutates ``document_versions`` or the preserved raw files.  It
creates a separate, rerunnable analytical layer in the same SQLite database and
puts extracted text/exports under ``data/processed``.  Rules provide a complete
offline baseline; an optional AI pass can replace a rule label only after its JSON
output and source spans have been validated.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import yaml
from bs4 import BeautifulSoup

from collector.collect import ROOT, STATE_DB, audit, connect, now_iso

try:  # PyMuPDF is optional at import time so text-only installs remain useful.
    # The legacy ``fitz`` alias prints a deprecation banner on stdout, which
    # lands in the middle of the JSON these commands are piped for.
    import pymupdf as fitz  # type: ignore
except ImportError:  # pragma: no cover - exercised by runtime installations
    fitz = None

SOURCES_PATH = ROOT / "config" / "sources.yaml"
# PyYAML 的纯 Python 解析器读这份 53 KB 配置要 **215ms**，libyaml 的 CSafeLoader 读同一份只要
# 28.7ms——这台机器上它一直装着，只是没被用上。
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


@lru_cache(maxsize=8)
def _parsed_config(path: str, stamp: tuple[int, int]) -> dict[str, Any]:
    return yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_YAML_LOADER)


def load_config(path: Path = SOURCES_PATH) -> dict[str, Any]:
    """The parsed config, cached on the file's own mtime and size.

    ``/api/companies`` spent 215 of its 246 ms here, and ``company_display_name``
    re-parsed the file **once per event** inside the narrow sidecars.  Keying the
    cache on ``(mtime_ns, size)`` keeps an edited config visible: a cache that
    hides a configuration change would be worse than the parse it saves.

    The returned dict is the cached object and is **read-only** — copy it before
    mutating.
    """
    stat = path.stat()
    return _parsed_config(str(path), (stat.st_mtime_ns, stat.st_size))


def company_display_name(company_id: str | None) -> str:
    """The name a reader recognises, from configuration rather than from code.

    A model asked what an event did to a named company answers better than one
    asked about its internal identifier, but the mapping cannot live here: an
    identifier compiled into a module is precisely what E7's zero-code-change
    claim has to be false about, and ``tests.test_migration`` fails on any
    processor file that so much as mentions one -- including, as it turns out, in
    a docstring.  The shortest alias wins because the alias lists are written
    from the legal name outwards.
    """
    if not company_id:
        return "未指明"
    entity = (load_config().get("entities") or {}).get(company_id) or {}
    aliases = [str(alias) for alias in entity.get("include_aliases") or [] if str(alias).strip()]
    if aliases:
        return min(aliases, key=len)
    return str(entity.get("canonical_name") or company_id)


def company_aliases(company_id: str | None) -> tuple[str, ...]:
    """这家公司在正文里可能的自称，长的排前面。

    与 ``company_display_name`` 同一份配置、同一个理由：名字不进代码。长的排前面是
    为了让「无锡药明康德新药开发股份有限公司」先于「药明康德」被匹配掉——按短的先
    剥，剩下的半截会是「新药开发股份有限公司…」。
    """
    if not company_id:
        return ()
    entity = (load_config().get("entities") or {}).get(company_id) or {}
    names = [str(alias) for alias in entity.get("include_aliases") or [] if str(alias).strip()]
    canonical = str(entity.get("canonical_name") or "").strip()
    if canonical:
        names.append(canonical)
    return tuple(sorted(set(names), key=len, reverse=True))


def company_industry(company_id: str | None) -> str | None:
    """The industry a company is declared in, or None when the config says nothing."""
    if not company_id:
        return None
    entity = (load_config().get("entities") or {}).get(company_id) or {}
    return str(entity["industry"]) if entity.get("industry") else None


def out_of_scope_codes(company_id: str | None, codes: Iterable[str | None]) -> set[str]:
    """The issue codes that cannot apply to this company's industry."""
    industry = company_industry(company_id)
    if industry is None:
        return set()
    return {code for code in codes
            if code in ISSUE_INDUSTRY_SCOPE and industry not in ISSUE_INDUSTRY_SCOPE[code]}


def scope_issue_codes(company_id: str | None, issue_codes: list[str],
                      claims: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]], set[str]]:
    """Drop issue codes outside the company's industry, from the event and its claims.

    A claim whose code is dropped is left without one rather than given another:
    which code it *should* carry is a reading of the sentence, and guessing one
    here would replace a visible error with an invisible one.  The caller flags
    the event for review instead.  Neither input is mutated — the raw label keeps
    what the model actually said.
    """
    dropped = out_of_scope_codes(company_id, [*issue_codes, *(claim.get("issue_code") for claim in claims)])
    if not dropped:
        return issue_codes, claims, dropped
    kept = [code for code in issue_codes if code not in dropped]
    scoped = [{**claim, "issue_code": None} if claim.get("issue_code") in dropped else claim
              for claim in claims]
    return kept, scoped, dropped


@lru_cache(maxsize=1)
def configured_companies() -> tuple[str, ...]:
    """The companies this campaign covers, read from configuration.

    Compiling the list into ten argument parsers would make a third company a
    code change — which is precisely the claim E7 has to demonstrate is false.
    The processing layer therefore holds no company identifier at all; a test
    enforces that.
    """
    return tuple(load_config()["scope"]["companies"])



PROCESSOR_VERSION = "event-processing-v2"
TEXT_ROOT = ROOT / "data" / "processed" / "text"
EXPORT_ROOT = ROOT / "data" / "processed" / "exports"
EXCLUDED_DOCUMENT_TYPES = {
    "search_response", "landing_page", "lists", "api_response", "attachment",
    "investor_qa_feed",
}
# Query parameters that number a page of a list.  A URL carrying one is an
# index of articles, not an article.
PAGINATION_PARAM = re.compile(r"^(?:page|pageno|pagenum|pageindex|pg|p)$", re.I)
# Everything that hangs off an event.  Only two of these declare the foreign
# key, and SQLite is not running with cascades on in this layer anyway, so
# the deletion has to be written out.  The omission was not hypothetical:
# removing 85 events left 85 rows in ``event_timeline``, 85 in
# ``article_sentiment_labels`` and 15 in ``claim_verdicts`` pointing at
# nothing.  ``normalize.run`` already carried its own sweep for
# ``event_elements`` -- one table's worth of this bug, found earlier and
# patched where it was noticed rather than where it comes from.
EVENT_DEPENDENTS = ("event_elements", "event_timeline", "article_sentiment_labels",
                    "production_mode_labels", "event_type_labels", "event_impact_labels",
                    "event_market_reaction")
CLAIM_DEPENDENTS = ("claim_verdicts",)
# ``openfda_enforcement_items.event_id`` is the FDA's own recall identifier,
# a different namespace that happens to share a column name.  Listing it
# here keeps the completeness test from having to guess.
FOREIGN_EVENT_ID_TABLES = ("openfda_enforcement_items",)
# Tables that name an event and must **not** be cascaded: a person's judgement
# outlives the row it was about.  Deleting an issue reassignment along with its
# event would destroy the only record of why the ledger once looked different,
# and the reviewer who made the call would have no trace of having made it.
# They go inert on their own — the rebuild only sees events that still exist.
HUMAN_RECORD_TABLES = ("issue_adjustments",)
# Ordered from most to least specific; the first match with real text wins.
MAIN_CONTENT_SELECTORS = (
    "[itemprop=articleBody]", "article", "main", ".article-content", ".article_content",
    "#article_content", ".art_content", ".detail-content", ".news-content", ".content-article",
)
# A line repeated across this share of one source's pages is site chrome.  The
# bar is deliberately high: real sentences do not recur across most articles.
BOILERPLATE_LINE_SHARE = 0.6
BOILERPLATE_MIN_DOCUMENTS = 5
# Below this, an HTML page carries navigation and legal notices but no article.
BODY_TEXT_FLOOR = 200
# Near-duplicate gate.  A shared title lowers the bar because two copies of one
# story rarely share every token, but a title alone must never form a cluster --
# generic announcement headlines repeat across unrelated filings.
#
# Calibrated 2026-09-02 against the organiser's duplication gold; the record is
# docs/submission/04-模型与算法.md.  The two similarity bars turned out not to bind
# at all -- dropping the title bar to 0.50 recovers not one extra pair -- because
# recall was capped upstream, in candidate generation: at four bands only 80.8%
# of true duplicate pairs collide in any band, so the rest were never compared.
# Eight bands with a hamming ceiling of 16 lifts pair-recall 0.7875 -> 0.8899 for
# 1.1 points of precision, and is the knee: every looser setting measured gains
# under 0.001 F2 per step.
#
# Recalibrated 2026-09-05, same gold, train split.  The remaining recall loss was
# not the gates at all: brute-forcing every same-company pair with no banding and
# no hamming ceiling recovered *four* additional true pairs.  The 174 misses were
# 142 pairs whose token Jaccard is genuinely low -- median 0.243 -- yet 63 of
# them carry a byte-identical title.  Jaccard divides by the union, so a 200-token
# brief and the 3,000-token article that swallows it whole score 0.07 while one
# fully contains the other; the missed pairs' length ratio is 1.9x at the median
# and above 3x for 28% of them.  Containment (the intersection over the *smaller*
# side) reads that case correctly: median 0.885 on the very pairs Jaccard put at
# 0.311.  So an exact (company, normalised title) bucket runs beside the LSH one
# and admits on containment.  Pair-recall 0.8899 -> 0.9355, and precision does
# not pay for it -- 0.9771 -> 0.9782, because nearly every new edge is correct.
TITLE_MATCH_THRESHOLD = 0.72
BODY_ONLY_THRESHOLD = 0.86
SIMHASH_HAMMING_MAX = 16
SIMHASH_BANDS = 8
# The bar that keeps "a title alone must never form a cluster" true.  On the
# organiser's news gold a shared title is by itself 99.85% precise (1,337 pairs,
# 2 misses) and every bar from 0.95 down to 0.50 is equally clean, so the gold
# does not choose this number -- the platform's own corpus does.  A quarter of it
# is 巨潮 announcements, where 「H股公告」 is a heading reused across years for
# unrelated filings; those pairs sit at containment 0.084-0.095, as does the daily
# 「长鑫科技成交额超100亿元」 wire item.  Any bar in this range excludes them, but
# dropping to 0.50 would merge 8,310 further announcement pairs that no gold can
# adjudicate, to buy one point of recall on news.  0.80 is the knee, and it keeps
# the rule meaning "substantially the same text" rather than "the same headline".
TITLE_CONTAINMENT_THRESHOLD = 0.80
# A third gate, added 2026-09-05.  The two above ask "is this the same text";
# neither can ask "is this the same *filing*", and on this corpus that is the
# difference that matters most.  ``tokenize`` emits no token for a bare number
# -- ``tokenize("610,000.0000 231,920.2025")`` is the empty set -- so two 港交所
# 翌日披露報表 from different trading days, whose only difference is the figures
# in the form, are byte-equivalent as far as every gate above is concerned.  The
# largest near-duplicate cluster on the corpus was 210 such filings presented as
# one story's reprints, and 40% of the propagation layer's multi-document mass
# was that shape, daily 「北向资金今日净买入…」 and 「成交额超…」 wire templates
# included.
#
# Making ``tokenize`` emit numbers does not fix it and was measured before being
# discarded: intra-cluster Jaccard falls only 0.977 -> 0.896 and every pair still
# clears the bar, because the form is mostly boilerplate and the figures are a
# small share of it.  What separates them is the figures *on their own*: among
# pairs whose text Jaccard is >= 0.90, genuine reprints share a numeric Jaccard
# of 1.000 at the median while these filings share 0.288.  So the gate is a veto,
# not another similarity: near-identical prose plus divergent numbers is a
# template refiled, not a story reprinted.  Calibrated in 交付物7 §7ter.
# Measured and **not adopted**: every one of the seventeen grid points scored
# below leaving the veto off, and by F2 -- this gate's declared objective -- the
# best of them loses by 0.0097.  It halves the worst artifact (max cluster
# 210 -> 100) and cannot remove it, because clustering is a star: members are
# only ever compared with the representative, so the veto shrinks a cluster
# without splitting it.  Left in, defaulted off, and swept from the harness, so
# 交付物7 §7ter reproduces from the shipped code rather than from a description.
TEMPLATE_TEXT_BAR = None
TEMPLATE_NUMBER_BAR = 0.50
# Only the figures that carry identity.  A statutory return repeats its page
# count, its form version (1.3.0) and its form number in every filing a company
# ever makes, and those numbers say nothing about *which* filing this is; the
# share counts and amounts do.  Comparing the raw sets put the forms at a median
# numeric Jaccard of 0.298, which splits three quarters of them; dropping the
# numbers that recur across more than this share of one company's documents puts
# them at 0.000 and splits 90%, while genuine reprints stay at 1.000 and their
# false-split rate *falls* from 2.2% to 1.5%.  Same principle, and the same
# reason, as ``claim_index.prune_tokens``.
TEMPLATE_NUMBER_SHARE = 0.15
# Commas are stripped so 1,598,773,691 and 1598773691 are one number; a trailing
# separator is not part of it.
NUMBER_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")
# A reprint is contemporaneous with the story it reprints: on the gold every one
# of the 1,337 true same-title duplicate pairs is inside a week, the median gap
# is zero days and the widest is six.  A recurring statutory heading is not --
# 巨潮 files 「H股公告」 under the same words for years.  Dates are required, not
# assumed: a document with no publication date cannot be shown to be
# contemporaneous with anything, so it does not take this route.
REPRINT_GAP_DAYS = 7
EVENT_TYPES = (
    "REGULATORY_COMPLIANCE", "LITIGATION_ARBITRATION", "FINANCIAL_AUDIT",
    "IPO_REFINANCING", "EQUITY_CONTROL", "EXECUTIVE_PERSONNEL",
    "CUSTOMER_SUPPLIER_OPERATIONS", "PRODUCT_TECH_IP", "SAFETY_QUALITY_RECALL",
    "M_AND_A_ASSET_TRANSACTION", "ESG_REPUTATION", "RESPONSE_CLARIFICATION",
)
PRODUCTION_MODES = (
    "ORIGINAL_INVESTIGATION", "INTERPRETATION", "REPOST", "REWRITE",
    "AI_GENERATED", "SPONSORED", "UGC_EMOTIONAL", "BRIEF",
)
# Coverage that carries no reporting of its own scores lower, because the risk
# it describes was established somewhere else and this is an echo of it.  The
# set was written when BRIEF was the catch-all; AI_GENERATED and SPONSORED were
# unreachable labels then and had to be added once the classifier could produce
# them, or a template ticker would have outscored the brief it was generated
# from.  UGC_EMOTIONAL stays out: an emotional post is unreliable rather than
# second-hand, and the credibility layer is where unreliability is priced.
SECONDHAND_MODES = frozenset({"REPOST", "REWRITE", "BRIEF", "AI_GENERATED", "SPONSORED"})
IMPACT_OBJECTS = (
    "operations", "financial_performance", "technology_product", "capacity_supply",
    "governance_internal_control", "shareholders_control", "executives_core_staff",
    "employees", "subsidiaries_related_parties", "customers", "suppliers",
    "partners", "competitors", "investors_stock_price", "creditors_financing",
    "regulatory_compliance", "reputation_brand", "industry", "supply_chain",
    "regional_economy",
)

# The controlled vocabulary is deliberately compact and mapped to the labels in
# the product specification.  It can be expanded without changing stored data.
ISSUE_RULES: dict[str, tuple[str, ...]] = {
    "FUND_RAISE": ("募资", "募投", "融资", "发行价", "发行价格", "募集资金", "融资额"),
    "VALUATION": ("估值", "市值", "定价", "市盈率", "pe"),
    "TECH_LEVEL": ("技术", "制程", "dram", "芯片", "产品", "平台", "研发"),
    "YIELD_CAPACITY": ("良率", "产能", "扩产", "产线", "产量", "产能爬坡"),
    "CUSTOMER": ("客户", "供应链", "导入", "订单", "苹果", "合作"),
    "SUPPLY_CHAIN": ("供应商", "设备", "材料", "供应链", "采购"),
    "LOCALIZATION": ("国产替代", "自主可控", "国产化"),
    "EXPORT_CTRL": ("出口管制", "制裁", "实体清单", "禁令", "export control", "sanction"),
    "CYCLE_PRICE": ("价格", "周期", "涨价", "降价", "存储价格"),
    "COMPETITION": ("竞争", "竞品", "市场份额", "对手"),
    "SHAREHOLDER": ("股东", "实控人", "控股", "持股"),
    "RELATED_PARTY": ("关联交易", "关联方"),
    "FINANCIAL": ("营收", "收入", "利润", "亏损", "毛利", "业绩", "财报", "财务"),
    "IP_PATENT": ("专利", "知识产权", "侵权", "商标"),
    "EXEC": ("高管", "董事", "总经理", "核心技术人员", "辞任"),
    "STOCK_PRICE": ("股价", "成交额", "大宗交易", "涨停", "跌停", "两融"),
    "LOCKUP": ("解禁", "限售"),
    "FUND_USE": ("募资用途", "募集资金使用", "募投项目"),
    "DISCLOSURE": ("信息披露", "更正", "澄清", "公告"),
    "AUDIT_STAGE": ("受理", "问询", "上会", "注册", "发行", "上市", "招股书"),
    # Reusable drug-development themes for the transfer company.
    "DRUG_DEVELOPMENT": ("drug", "clinical", "临床", "药物", "cxo", "cro", "cdmo"),
    "CYBERSECURITY": ("网络安全", "cybersecurity", "数据安全", "ransomware"),
    # Vocabulary v2 (2026-09-03).  The twenty-two codes above were designed round
    # a single issuer's review-period concerns (方案 §8.2); run against 84
    # companies and 17 industries, 37 of the 64 official event names had no class
    # here at all -- 48.3% of gold rows by count.  Those 37 names do not split
    # into the two dimensions the roadmap named but into four, which is why there
    # are four codes and not two: policy and industry trend are the industry-level
    # pair, expansion and transformation the company-level pair, and entering new
    # ground is not the same event as reorganising ground already held.
    "INDUSTRY_POLICY": ("行业政策", "产业政策", "监管政策", "政策支持", "政策发布", "调控政策",
                        "补贴政策", "财政政策", "货币政策", "利率下调", "降准", "化债",
                        "稳增长", "指导意见", "实施方案", "政策出台"),
    "INDUSTRY_TREND": ("行业景气", "景气度", "产业升级", "产业发展", "新兴产业", "低空经济",
                       "行业需求", "市场表现"),
    "BUSINESS_EXPANSION": ("业务拓展", "新业务", "市场拓展", "市场扩张", "海外市场", "国际化",
                           "出海", "全球化", "新赛道", "品牌布局"),
    "STRATEGY_TRANSFORM": ("战略调整", "战略转型", "转型升级", "数字化转型", "战略规划",
                           "结构升级", "业务转型", "经营策略", "战略布局"),
}

#: 只适用于某些行业的议题码，及其适用的行业（取值见 sources.yaml 各主体的 ``industry``）。
#:
#: 词表是两家公司共用的一份，模型和关键词都可能把一个行业专属的码挂到另一行业的公司
#: 名下：I-03 里「研发支出资本化」和「DRAM 出货预测」被判成了药物研发。是不是药物研发
#: 要读文章，但一家存储芯片公司有没有药物研发不用读——这一条交给规则。未声明行业的
#: 主体不受限制：没有依据时不替它剔除任何东西。
ISSUE_INDUSTRY_SCOPE: dict[str, frozenset[str]] = {
    "DRUG_DEVELOPMENT": frozenset({"pharma"}),
}

#: 每个议题码的短名，给人看的那一个。
#:
#: ``CODE_GUIDE`` 是写给模型的排他式判据（80–150 字一条），当不了标题里的一个词；
#: 短名此前只存在于 ``api/labels.py``，而处理层不能 import api——预警标题要在
#: 处理层生成，于是这份表必须在这一侧也有一份。两侧逐字一致由
#: ``tests/test_labels.py`` 钉住；重复是自觉的取舍，那条测试是让这笔取舍成立的东西。
ISSUE_LABELS: dict[str, str] = {
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


# What each code above means, in the terms the issuer's own review turns on.
#
# ``ISSUE_RULES`` gives every code its keywords; this gives every code its
# *definition*, and the two belong together because the second is what any model
# path needs.  ``ai_label``'s schema offered these values as a bare enum its
# prompt never explained -- the same defect ``production_mode`` and
# ``article_sentiment`` were each repaired for, a value nothing defines being one
# the model fills in by guesswork.
#
# The evidence that supplying definitions works is measured, not asserted: on the
# organiser's gold (交付物11 §4.5) the issue-code layer went from micro-F1 0.1789
# to 0.3997 and ``INDUSTRY_POLICY`` from 0.093 to 0.701 on exactly these texts.
# Spot-checked against the production corpus, four of seven sampled documents got
# a visibly better code -- a stock-move story reading ``STOCK_PRICE`` instead of
# ``SHAREHOLDER``, a divestiture ``STRATEGY_TRANSFORM`` instead of ``EXPORT_CTRL``,
# an auditor-performance report ``DISCLOSURE`` instead of ``AUDIT_STAGE``, which
# means the IPO review stage and not an audit.
def issue_code_rules(max_codes: int) -> str:
    """The judging rules for ``issue_codes``, in one copy.

    They used to live twice — once in ``ai_label``'s extraction prompt and once
    in ``processor.issue_codes``'s benchmark sidecar — and they drifted.  The
    four-way precedence below was measured on held-out gold through the sidecar
    (test micro-F1 0.3997 -> 0.4211, 交付物11 §4.6) and for a while existed only
    there, so the path the platform actually labels with never got it.  Sharing
    the text is what stops the next measured improvement from landing in one
    prompt and not the other.

    ``max_codes`` is the one legitimate difference: the extraction schema allows
    up to three codes per document, the benchmark task never has more than two.

    The line wrapping is load-bearing and the wording is exact.  Extracting this
    function silently dropped 「二者不要同时给出」 and rewrapped the first bullet;
    ``PROMPT_VERSION`` stayed at v2 across the change, so two runs an hour apart
    carry the same label and different prompts -- and E1's train micro-F1 fell
    0.4429 -> 0.4286, seven times the noise floor, before anyone noticed.
    ``tests.test_issue_codes`` now pins the rendered prompt to a digest so the
    next such edit cannot ship without changing the version alongside it.
    """
    # The newline inside the two-code branch is not cosmetic: it restores the
    # exact wrapping every published E1 number was measured on.
    ceiling = ("确实并列讲两件事时才给 2 个，极少超过 3 个。"
               if max_codes >= 3 else
               "只有当稿件确实\n  并列在讲两件事、去掉任一件都会漏掉主题时，才给 2 个。"
               "**本任务不存在 3 个的情况。**")
    return f"""- **只给真正构成本文主题的议题码。** 绝大多数稿件只有 **1 个**议题码；{ceiling}
  顺带提到、只作背景铺垫、或仅在某一句里出现的话题**不是**本文议题——把它写进来是错误，
  不是稳妥。
- 判断依据是**这篇稿子在讲什么**，不是文中出现了哪些词。稿件可以通篇讲行业政策而从不出现
  「行业政策」四个字；也可以频繁出现「技术」二字而主题其实是产能扩建。
- **BUSINESS_EXPANSION 是最容易被过度使用的码。**「进入原本没有的地盘」这句话对
  建新产线、上新技术、调整战略同时成立，所以四个码之间**按下列顺序取第一个命中的**，
  不要因为「也算拓展」就退回 BUSINESS_EXPANSION：
  1. 产能、基地、工厂、重大项目的**建设与投资** → `YIELD_CAPACITY`，
     即使建在新地方、面向新业务。
  2. 技术、产品、工艺的**突破或迭代** → `TECH_LEVEL`，即使它打开了新市场。
  3. 战略调整、转型、结构升级、品牌与渠道重组 → `STRATEGY_TRANSFORM`，
     **即使伴随对外投资或新设合资**——主题是「怎么重新摆布自己」而不是「多做一门生意」。
  4. 以上都不是，主题确实是**进入原本没有的业务或市场** → `BUSINESS_EXPANSION`。
  二者不要同时给出，除非稿件确实两件事都在讲。
- 出口管制、实体清单、制裁、关税裁定 → `EXPORT_CTRL`，**即使稿件的落点在价格或供需**；
  `CYCLE_PRICE` 留给纯粹由周期与供需驱动的价格变动。
- INDUSTRY_POLICY 与 INDUSTRY_TREND 是**行业层面**的议题；若稿件讲的是某家公司自身的
  经营、技术或产能，即使背景提到行业，也不应给这两个码。
- 找不到任何一个议题码贴合本文时，返回空列表，不要硬凑。"""


CODE_GUIDE: dict[str, str] = {
    "FUND_RAISE": "募资与融资：IPO 募集、增发、配股、发行定价、融资额、募集资金总额。",
    "VALUATION": "估值：市值、发行市盈率、估值水平与其合理性的讨论。",
    "TECH_LEVEL": "技术水平与产品代际：制程、工艺、产品性能与迭代、研发进展、技术突破与技术路线。",
    "YIELD_CAPACITY": "良率与产能：良率爬坡、产线建设与投产、扩产、产量与装机容量。",
    "CUSTOMER": "客户与订单：客户导入与认证、在手订单、重要合同签订、下游需求变化。",
    "SUPPLY_CHAIN": "供应链与采购：供应商、原材料与设备采购、供应保障与断供风险。",
    "LOCALIZATION": "国产替代：自主可控、国产化率、替代进口。",
    "EXPORT_CTRL": "出口管制与制裁：实体清单、禁令、地缘政治导致的贸易限制。",
    "CYCLE_PRICE": "周期与价格：产品价格涨跌、大宗商品景气、行业周期与价格波动。",
    "COMPETITION": "竞争格局：竞争对手动向、市场份额争夺、竞品比较、竞争加剧。",
    "SHAREHOLDER": "股东与控制权：股东结构、实控人、控股权变动、增减持。",
    "RELATED_PARTY": "关联交易：与关联方之间的交易及其公允性。",
    "FINANCIAL": "财务表现：营收、利润、毛利率、亏损、业绩与财报口径。",
    "IP_PATENT": "知识产权：专利、商标、技术侵权与权属纠纷。",
    "EXEC": "高管与核心人员：董监高及核心技术人员的任免与离职。",
    "STOCK_PRICE": "股价与交易：股价涨跌、成交额、大宗交易、融资融券。",
    "LOCKUP": "限售与解禁：限售股安排与解禁节奏。",
    "FUND_USE": "募资用途：募集资金的投向、募投项目进展与变更。",
    "DISCLOSURE": "信息披露：公告、更正、澄清与披露合规本身。",
    "AUDIT_STAGE": "发行审核进程：受理、问询、上会、注册、发行、上市等审核环节的推进。",
    "DRUG_DEVELOPMENT": "药物研发：临床试验、药物管线、CRO/CDMO 业务进展。",
    "CYBERSECURITY": "网络与数据安全：网络攻击、数据泄露、勒索软件与安全合规。",
    "INDUSTRY_POLICY": "行业政策与监管导向：政府或监管机构发布的产业政策、调控政策、补贴、"
                       "财政与货币政策、利率调整、行业规划与指导意见。判据是**政策本身**，"
                       "不论稿件是否点名某家公司。",
    "INDUSTRY_TREND": "行业景气与产业发展：行业整体的需求与景气变化、产业升级、新兴产业的"
                      "兴起与发展、行业级的市场表现。判据是**行业层面**的态势，不是单个发行人的经营。",
    "BUSINESS_EXPANSION": "业务拓展与市场开拓：进入新业务、新赛道、新市场，海外与国际化布局、"
                          "出海、品牌与渠道扩张、基础设施铺开。判据是**进入原本没有的地盘**。",
    "STRATEGY_TRANSFORM": "战略调整与转型：既有业务的战略与定位调整、组织与业务结构升级、"
                          "数字化与产业转型、经营策略变更、对外投资引起的战略变化。"
                          "判据是**重组已经握有的地盘**，与 BUSINESS_EXPANSION 的区别在此。",
}
# The guides for the two vocabularies whose judging criteria live outside the
# extraction prompt.  They are here rather than in the modules that ask the
# question because ``/master-data`` has to display them, and ``api.routes.meta``
# importing ``processor.production_mode`` or ``processor.article_sentiment``
# would pull ``openai`` into the web process; ``pipeline`` needs only the
# standard library and yaml.  Each module below imports its own guide from here,
# so there is still exactly one copy of every judging text.
MODE_GUIDE = """- ORIGINAL_INVESTIGATION 原创调查：记者自行采访、取证、实地走访或自建数据核算，含独家信源或自采数据。
- INTERPRETATION 解读分析：基于已公开的文件、数据或事件展开分析、评论、行业判断，有作者观点或推演。
- REPOST 转载：与另一家来源的稿件实质相同，仅换标题、删节或加编者按。
- REWRITE 改写：基于同一素材重新组织表述，有实质性增删或重新采写，但不含独立取证。
- AI_GENERATED 机器生成：模板化的行情、公告或数据播报，句式高度固定、可批量套用（如"成交额超 N 亿元"）。
- SPONSORED 宣发：通篇正面、无质疑、无独立信源，带宣传口径、活动信息、产品推介或联系方式，读起来像公司或机构供稿。
- UGC_EMOTIONAL 用户情绪：论坛、自媒体或评论区式的情绪化表达，缺乏可核验的事实骨架。
- BRIEF 简讯：纯状态播报，一两句事实，无分析、无立场、无宣传。"""

TONE_GUIDE = """- positive 正面语气：行文带褒义措辞、赞许、乐观预期或宣传色彩——强调利好、成就、前景，用词偏正向。
- negative 负面语气：行文带贬义措辞、质疑、批评、警示或悲观论调——强调问题、风险、失败、争议，用词偏负向。
- neutral 中性语气：平铺直叙、就事论事、无明显褒贬——公告体、事实播报、正式陈述、数据罗列均属此类。"""


# What each of the twelve event types means, and when not to use it.
#
# ``TYPE_RULES`` below gives every type its keywords, which only the rule path can
# act on; this gives every type its *definition*, which is what a model path needs.
# ``ai_label``'s schema has offered ``event_types`` as a bare enum its prompt never
# mentioned -- the same defect ``CODE_GUIDE`` was written to repair for the issue
# codes, and the repair is measured: on the organiser's gold the issue-code layer
# went from micro-F1 0.1789 to 0.3997 on supplying definitions and to 0.4211 on
# adding a precedence chain (交付物11 §4.5-4.6).  Left unrepaired here, the symptom
# is exactly what you would predict of a model reading class names: on the gold's
# test split ``FINANCIAL_AUDIT`` has precision 0.033 (predicted 90 times against a
# support of 13), ``REGULATORY_COMPLIANCE`` recall 0.104, and three classes with no
# gold support at all were still predicted 87 times between them.
#
# These twelve are drawn up by **risk type**; the organiser's names are drawn up
# by **business action** (``config/event_taxonomy_map.yaml`` says so in its
# header).  Two different axes is why "return an empty list when nothing fits" belongs in the
# rules below rather than being an admission of failure.
EVENT_TYPE_GUIDE: dict[str, str] = {
    "REGULATORY_COMPLIANCE":
        "监管与合规：监管机构对本主体采取的行动、或对其提出的要求——行政处罚、立案调查、"
        "问询函与关注函、警示函、责令整改、出口管制与实体清单、制裁与关税裁定，以及行业主管"
        "部门发布的、直接约束本主体经营的政策与许可。判据是**监管方做了什么或要求了什么**；"
        "公司自称合规、或稿件只在背景里提到政策，都不属本类。",
    "LITIGATION_ARBITRATION":
        "诉讼与仲裁：已进入司法或仲裁程序的争议——起诉与应诉、立案、开庭、判决、裁决、执行、"
        "和解，含专利与商标侵权之诉。判据是**有一个第三方裁判机构正在处理这件事**；尚未起诉的"
        "纠纷、律师函、声明保留追诉权利，都不属本类。",
    "FINANCIAL_AUDIT":
        "财务与审计：已经形成的经营成果与财务状况本身——营业收入、利润、毛利率、亏损、现金流、"
        "资产减值、业绩预告与快报、会计政策与差错更正、审计意见与非标意见、内控审计。"
        "判据是**本文在讲一个已经形成的财务口径的数**；对未来业绩的展望、券商预测、估值讨论，"
        "以及只是顺带引用了一个财务数字的稿件，都不属本类。",
    "IPO_REFINANCING":
        "发行与再融资：证券发行本身及其审核程序——IPO 受理、问询与回复、上市委会议、注册、"
        "路演与询价、定价与配售、上市，以及上市后的增发、配股、可转债、募集资金用途变更。"
        "判据是**正在发行或审核一笔证券**；发行文件里披露的经营数据仍按其内容归类，"
        "不因为出自招股书就归本类。",
    "EQUITY_CONTROL":
        "股权与控制权：股份归属与控制关系的变化——控股股东与实际控制人变更、股份增减持与协议"
        "转让、一致行动与表决权委托、要约收购、股权质押与冻结、限售股解禁、员工持股与股权激励的"
        "授予与解锁。判据是**谁持有多少股、谁说话管用**发生了变化或首次被披露。"
        "**二级市场的日常成交不属本类**：成交额、换手率、涨跌停、资金流向，以及未涉及持股 5% 线、"
        "协议转让或控股股东与实控人的小额大宗交易，都只是交易数据，不是股权归属的变化。",
    "EXECUTIVE_PERSONNEL":
        "高管与人事：董监高及核心技术人员的任免、辞任、换届、因被留置或立案而无法履职、薪酬与"
        "任期安排，以及达到公司层面的人员规模变动。判据是**具体的人的职位或去留**；"
        "常规招聘与日常人事不构成事件。",
    "CUSTOMER_SUPPLIER_OPERATIONS":
        "客户、供应商与经营：公司与交易对手之间的经营活动及其变化——重要客户导入与认证、在手"
        "订单与重大合同签订、供应商与采购、供应保障与断供、产能建设与投产、产量与交付、价格与"
        "需求变化、市场竞争与份额、进入新业务或新市场、战略与业务结构调整。判据是**公司在市场上"
        "做生意的方式或结果发生了变化**，且**不落入下列任何一个更具体的类**——本类是判定规则里"
        "顺序最后的一类，不是最方便的一类。",
    "PRODUCT_TECH_IP":
        "产品、技术与知识产权：技术与产品本体的进展——研发与试产、工艺与制程突破、新产品发布与"
        "迭代、临床试验与注册申报、专利与商标的申请、获授、许可与权属安排。判据是**做出了什么"
        "东西、掌握了什么能力**，不是靠它卖了多少、也不是为它建了多少产能。",
    "SAFETY_QUALITY_RECALL":
        "安全、质量与召回：安全生产事故、火灾与爆炸、工伤、产品质量缺陷、抽检不合格、召回与停售、"
        "网络攻击与数据泄露，以及重大公共卫生或自然灾害对本主体设施的影响。"
        "判据是**有人或有物受到了实际损害、或面临即刻的损害风险**。",
    "M_AND_A_ASSET_TRANSACTION":
        "并购与资产交易：改变主体范围或资产权属的交易——收购与出售股权、吸收合并与分立、重大"
        "资产购买与处置、对外投资与新设合资、剥离与关停子公司、关联方之间的资产与股权交易。"
        "判据是**一笔改变合并范围或资产归属的交易**；采购设备、签供货合同不属本类。",
    "ESG_REPUTATION":
        "ESG 与声誉：环境、社会责任与治理形象层面的事项——排放与能耗表现、碳与绿电、环境与社会"
        "责任报告及评级、公益与捐赠、劳工与人权争议，以及**不指向具体违法违规**的负面舆情与品牌"
        "争议。判据是**被评价的是公司作为社会主体的形象与外部性**；一旦落到监管处罚或司法程序，"
        "归 REGULATORY_COMPLIANCE 或 LITIGATION_ARBITRATION。",
    "RESPONSE_CLARIFICATION":
        "回应与澄清：本主体针对外部传闻、报道或问询所作的公开答复**本身**——澄清公告、澄清说明、"
        "辟谣、更正与补充披露、业绩说明会与投资者问答中的正式回应。判据是**这篇稿件的内容是一次"
        "「说话」**，而不是被说的那件事。它与上面十一类不在同一根轴上，用法见判定规则；"
        "**它不是兜底类。**",
}


def event_type_rules(max_types: int) -> str:
    """The judging rules for ``event_types``, in one copy.

    Same shape and same reason as ``issue_code_rules``: a prompt in the extractor
    and a prompt in a sidecar drift, and ``pipeline`` already records what that
    costs.  ``max_types`` is the one legitimate difference -- the extraction schema
    allows four, the narrow sidecar three (two substantive types plus the speech-act
    code).

    The precedence chain is written against measured failures rather than taste.
    ``FINANCIAL_AUDIT`` precision 0.033 is a model reporting "financial" whenever a
    financial number appears, so rule 6 says a number is not a subject.
    ``REGULATORY_COMPLIANCE`` recall 0.104 is a model reading the class name as
    "处罚/合规" and missing 15 industry-policy names the frozen map folds into it, so
    rule 1 names policy and licensing.  And 22 official event names fold into
    ``CUSTOMER_SUPPLIER_OPERATIONS`` (``event_taxonomy_map.yaml``), which is why the
    chain ends there explicitly rather than letting it be the easy answer.
    """
    ceiling = ("确实并列讲两件事时才给 2 个；加上 RESPONSE_CLARIFICATION 这个言语行为码后"
               "最多 3 个，极少出现。" if max_types <= 3 else
               "确实并列讲两件事时才给 2 个，极少超过 3 个。")
    return f"""- **只给真正构成本文主题的事件类型。** 绝大多数稿件只有 **1 个**类型；{ceiling}
  顺带提到、只作背景铺垫、或仅在某一句里出现的事项**不是**本文的事件类型。
- 判断依据是**这篇稿子在讲什么**，不是文中出现了哪些词，也不是类名读起来像不像。一篇通篇讲
  产能建设的稿件，不因为提到「股东大会审议通过」就变成 EQUITY_CONTROL；一篇讲技术突破的稿件，
  不因为引用了去年营收就变成 FINANCIAL_AUDIT。
- **十一个实质类目按下列顺序取第一个命中的**，不要因为「也沾一点」就退回更笼统的那一类：
  1. 监管机构或司法/仲裁机构正在对本主体采取行动 → `REGULATORY_COMPLIANCE`；
     其中已由第三方裁判机构受理的争议 → `LITIGATION_ARBITRATION`。
     **即使稿件的落点在业绩或股价上，仍归此处。**但**发行审核过程中的问询函与回复不算**
     ——它是发行程序的一环，归第 3 条；本条说的是**上市后**的监管问询、关注函与处罚。
  2. 有人身、设施、产品或数据受到实际损害 → `SAFETY_QUALITY_RECALL`，即使它随后引出了监管
     调查；但当稿件的主题已经是那份处罚决定本身时，回到第 1 条。
  3. 稿件在讲一笔证券的发行或其审核进度 → `IPO_REFINANCING`，**即使正文大段引用招股书里的
     经营数据**——数据是材料，主题是发行。
  4. 稿件在讲一笔改变合并范围或资产归属的交易 → `M_AND_A_ASSET_TRANSACTION`，即使它同时被
     描述为战略转型或产能布局。
  5. 稿件在讲股份归属或控制关系的变化 → `EQUITY_CONTROL`；只讲具体的人的职位与去留 →
     `EXECUTIVE_PERSONNEL`；两件事都在讲（如实控人同时辞任董事长）才同时给出。
  6. 稿件在讲一个**已经形成**的财务口径的数 → `FINANCIAL_AUDIT`。**文中出现财务数字不等于
     本文在讲财务**：一篇讲产能扩建、顺带给出上年营收的稿件不属本类。
  7. 稿件在讲技术、产品、工艺、临床或知识产权本体 → `PRODUCT_TECH_IP`，即使它打开了新市场。
  8. 稿件在讲环境、社会责任、治理形象，或不指向具体违法的品牌争议 → `ESG_REPUTATION`。
  9. 以上都不是，而稿件确实在讲公司与客户、供应商、订单、产能、价格、竞争、新业务或战略调整之间的
     经营活动 → `CUSTOMER_SUPPLIER_OPERATIONS`。**这是顺序上的最后一类，不是省事的一类。**
- **RESPONSE_CLARIFICATION 与上面九条不在同一根轴上。** 它说的是「这篇稿件是一次公开答复」，
  上面九条说的是「被答复的是什么事」。因此：
  - 稿件是一次澄清、辟谣、更正或正式回应，且**能判断它在回应什么事**时，**同时给出**
    `RESPONSE_CLARIFICATION` 与那件事的实质类目（例：澄清一则减持传闻 →
    `RESPONSE_CLARIFICATION` + `EQUITY_CONTROL`）。
  - 只有当稿件通篇只是「公司表示不予置评/以公告为准」、无法判断所回应之事时，才只给它一个。
  - **不得把它当兜底**：稿件不是一次公开答复时，无论其他类是否命中，都不要给这个码。
    公司在采访里说了一句话不构成澄清。
- **纯二级市场行情数据不构成任何一类事件类型**：单日成交额、换手率、涨跌停、大宗交易明细、
  资金流向、股价涨跌本身，返回空列表。它们在议题码层由 `STOCK_PRICE` 承担，事件类型这一层
  按风险类型立目，行情数据不落进其中任何一格——**尤其不要因为「股份成交了」就给
  EQUITY_CONTROL**。
- 找不到任何一类贴合本文时，**返回空列表，不要硬凑**，并在理由中写清这件事实际是什么。
  这 12 类按风险类型划分，与「公司做了什么业务动作」不是同一根轴，确有事件落不进任何一类。"""


# 影响对象的五组，来自总体方案 §8.4。分组本身是判据：一件事先影响公司内部的什么，
# 再影响它与谁的关系，最后才谈外溢。
IMPACT_OBJECT_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("公司内部", ("operations", "financial_performance", "technology_product",
                  "capacity_supply", "governance_internal_control")),
    ("主体关系", ("shareholders_control", "executives_core_staff", "employees",
                  "subsidiaries_related_parties")),
    ("市场关系", ("customers", "suppliers", "partners", "competitors")),
    ("外部关系", ("investors_stock_price", "creditors_financing",
                  "regulatory_compliance", "reputation_brand")),
    ("外溢", ("industry", "supply_chain", "regional_economy")),
)
IMPACT_OBJECT_GUIDE: dict[str, str] = {
    "operations": "经营与业务：日常经营活动本身——业务开展、订单履行、生产运转、渠道与门店运营的连续性与效率。",
    "financial_performance": "财务与业绩：收入、利润、毛利、现金流、资产质量与偿债能力等财务口径的结果。",
    "technology_product": "技术与产品：技术能力、产品性能与代际、研发进度、专利与技术权属。",
    "capacity_supply": "产能与供给：产能规模、产线与基地、良率与产量、交付与供给保障能力。",
    "governance_internal_control": "治理与内控：三会运作、信息披露质量、内控有效性、合规体系与审计意见。",
    "shareholders_control": "控股股东与实际控制人：持股结构、控制权归属、股东之间的关系及其自身权益。"
                            "**公司经营变化间接影响股东价值不算**，只有股东自身的持股或控制关系被触动才给。",
    "executives_core_staff": "董监高与核心技术人员：具体高管与核心人员的职位、履职与去留。",
    "employees": "员工：一般员工的岗位、薪酬、安全与规模。",
    "subsidiaries_related_parties": "子公司与关联方：并表范围内的子公司、参股公司与关联方主体。",
    "customers": "客户：下游客户及其订单与需求。",
    "suppliers": "供应商：上游供应商及其供货与价格。",
    "partners": "合作伙伴：合资方、联合研发方、渠道与生态伙伴。",
    "competitors": "竞争对手：同业竞争者，以及本主体在竞争格局中的相对位置。",
    "investors_stock_price": "投资者与股价：二级市场投资者的持仓价值、股价与成交、估值、摊薄与分红。",
    "creditors_financing": "债权人与融资：银行与债券持有人、融资成本与可得性、担保与质押。",
    "regulatory_compliance": "监管关系与合规：与监管机构之间的关系、许可与资质、合规状态与处罚记录。"
                             "与事件类型 REGULATORY_COMPLIANCE 不是一回事：此处是这件事改变了什么，"
                             "那里是这篇稿件在讲一次监管行动。",
    "reputation_brand": "声誉与品牌：公众与市场对本主体的评价、品牌与信任。",
    "industry": "所属行业：本主体所在行业整体的格局、规则或景气。",
    "supply_chain": "产业链上下游：本主体之外的上下游环节整体。",
    "regional_economy": "区域经济：项目或事件所在地的地方经济、就业与财政。",
}


def impact_object_rules() -> str:
    """Judging rules for ``affected_objects``, aimed at two measured abuses.

    One thing here generalises beyond this vocabulary, and it was measured twice.
    **A rule written as an outcome binds; the same rule written as an exclusion
    does not.**

    ``shareholders_control`` was written as a mechanism from the start — 「只有股东
    自身的持股或控制关系被触动才给」 — and fell from 50.7% of events to 6.9%.
    ``investors_stock_price`` was written as a topic list and rose 33.0% -> 56.8%,
    because this corpus's filings *are* dividends, conversions, placements and
    turnover flashes, all of which were on that list.  Rewriting it as a mechanism
    (v2) took only 47 of 120 target rows off it, and two near-identical
    「成交额超100亿元」 flashes came back judged differently — the exclusion was not
    binding.  Adding one sentence that states the *outcome* (「稿件本身就是一条行情
    数据时，affected_objects 返回空列表」, v3) took the same population from 28 to 1,
    with 70 of 71 returning an empty list, while the 60 dividend/conversion rows
    that should keep the object kept it 60/60.

    ``event_types`` had shown the same thing a day earlier: 「纯二级市场行情数据不构成
    任何一类事件类型，返回空列表」 emptied 167 of 175 flashes, where the preceding
    「不属本类」 wording had left 64 of them labelled 股权与控制权.

    The starting distribution both rules were written against:
    ``shareholders_control`` on 50.7% of events, ``investors_stock_price`` on
    33.0%, ``regional_economy`` never used once, and 52 events carrying no object
    at all -- which is what a bare enum gets you.
    """
    return """- **只给这件事实际改变了、或直接指向的对象**，通常 1—3 个。把「可以想象也会受影响」的对象写进来是
  错误，不是稳妥。
- 影响对象不是稿件里出现过的名词。稿件提到客户名单不等于客户是本次影响对象；只有当这件事改变了与
  客户的关系、订单或需求时才给 `customers`。
- 先在**公司内部**与**主体关系**两组里找最贴近的那一个，再考虑**外部关系**；**外溢三项
  （industry / supply_chain / regional_economy）只在稿件本身把影响面写到行业或地方层面时才给**，
  不要因为公司大就外推。
- `shareholders_control` 是最容易被滥用的取值——「股东」两个字几乎每份公告都出现。只有当**股东自身的
  持股、控制关系或其权益**被触动时才给它；公司经营的好坏间接影响股东价值，不给。
- `investors_stock_price` 是第二个容易滥用的取值——**任何披露都可能影响股价**。判据不是「稿件在讲
  股价」，而是**这件事改变了每股的价值，或改变了投资者可得的现金与股份**：分红与回购（现金）、
  增发、配售、可转债转股、行权与限售解禁（份额与摊薄）、要约价与交易对价（可得的价格）、除权除息。
  **二级市场自身的交易结果不算**——成交额、换手率、涨跌停、大宗交易明细、股价涨跌本身，那是市场对
  已有信息的定价，不是这件事改变了什么；只提供沟通渠道的公告（业绩说明会、路演与调研安排）同样不算。
- **稿件本身就是一条行情数据时，affected_objects 返回空列表**——单日成交额、换手率、涨跌停、
  大宗交易明细、资金流向、股价涨跌本身。它没有改变任何对象的处境，市场只是在给已有信息定价；
  事件类型层对同一类稿件也返回空列表，两层口径一致。
- 一个对象也判断不出来时给空列表，不要拿「经营与业务」顶替。"""


# 原子主张的四种性质。它是 ``event_claims.claim_type`` 的取值，也是第五个「列在 schema 里、
# 提示词从不解释」的受控词表——而它比其余几个更直接地影响读者对一条主张的信任：
# 一句被标成「传言」的话和一句被标成「事实」的话，在页面上并列出现时读者只能按标签取信。
# 规则路径把所有主张一律记作 fact（pipeline 的 rule_label），这是它自己的口径缺陷。
CLAIM_TYPES = ("fact", "inference", "rumor", "opinion")
CLAIM_TYPE_GUIDE = """- fact 事实陈述：原文以确定语气陈述的、可与权威文件核对的内容——已发生的动作、已披露的数字、
  已生效的状态。判据是**原文把它当作已经成立的事**在说。
- inference 推断：由已知事实推出的结论、测算或预期——「预计」「有望」「意味着」「据此推算」。
  判据是**这句话的成立取决于一段推理**，而不是取决于一份文件。
- rumor 传言：来源不具名或未经证实的说法——「据接近交易人士」「市场传闻」「未获官方确认」。
  判据是**原文自己都没有把它当作已确认的事**。
- opinion 观点：评价、判断或立场表达——分析师看法、评论、褒贬性论断。判据是**它没有可核对的
  内容**，只有说话人的态度。
- 拿不准 inference 与 opinion 时看有没有可核对的前提：有前提、只是往前推一步的是 inference，
  纯粹表态的是 opinion。被公司或监管正式确认过的说法不再是 rumor，即使它最初来自传闻。"""


# 影响方向与影响强度的取值。它们此前只以字面量存在于 ``ai_label.schema()`` 与
# ``verify`` 两处，没有一处定义，而 ``risk_parts`` 吃它们：negative +20、
# major +20 / moderate +10，四条预警规则也读它们。
IMPACT_DIRECTIONS = ("positive", "negative", "neutral", "uncertain")
IMPACT_MAGNITUDES = ("major", "moderate", "minor")
# 「凭什么算重大」的受控取值。它把一个散文式的理由变成可核对的短标签，
# 也让界面用一个词而不是一段话来展示。
MAGNITUDE_BASES: dict[str, str] = {
    "scale_threshold": "金额或比例达到营收/净资产/市值的 10%，或达到单独披露门槛",
    "entity_continuity": "触及主体资格或持续经营",
    "forward_commitment": "改变了跨期承诺（业绩指引、产能规划、募投安排）",
    "single_scope": "限于单个项目、客户或报告期",
    "routine_procedural": "程序性或例行事项",
}
MAJOR_BASES = ("scale_threshold", "entity_continuity", "forward_commitment")

# 与 ``event_emotion.EMOTION_GUIDE`` 是同一个判断的两种枚举（那边是主办方金标的中文三值，
# 这边是本系统的英文四值）。三个共有取值的措辞与那份已在金标上测过的保持同义，
# 第四值 uncertain 是本系统独有、且在规则路径 ``rule_label`` 里根本不可达（库里 18 条）。
IMPACT_DIRECTION_GUIDE = """- positive 正面：这件事使本主体的处境变好——增加收入、利润、产能、订单，提升技术或市场地位，
  取得资质或政策支持，或**消除了一项已知的不利因素**（问询获通过、诉讼撤诉、风险警示解除）。
- negative 负面：这件事使本主体的处境变差——造成损失、下滑、处罚、诉讼、订单或产能流失、
  地位削弱，或**新增了一项风险**。
- neutral 中性：事件确实与本主体相关，但方向不明或影响可忽略——例行披露、程序性进展、
  利弊并存且无法判断净方向。
- uncertain 待定：这件事对本主体是好是坏**取决于尚未确定的后续**——结果未定的调查、尚未定价的
  发行、尚未生效的协议、未经证实的传闻。与 neutral 的区别是：neutral 是「影响很小」，
  uncertain 是「影响可能很大，但方向现在看不出来」。"""

IMPACT_MAGNITUDE_GUIDE = """- major 重大：满足以下**任一条**——(1) 涉及金额或比例达到公司同期营业收入、净资产或总市值的
  **10% 以上**，或达到披露规则要求单独披露的门槛；(2) 触及主体资格或持续经营——控制权变更、
  退市风险、停产、资质吊销、被立案调查；(3) 改变了**跨期承诺**——全年业绩指引、产能规划、
  募投项目安排。
- moderate 一般：确有可度量的影响，但限于单个项目、单个客户或单个报告期，或金额在上述门槛以下；
  公司经营的方向不因此改变。
- minor 轻微：程序性、例行或象征性事项——按期披露、日常人事、会议通知、单笔小额合同、
  对旧信息的重复表述。"""


def impact_rules() -> str:
    """Judging rules for the affected objects, direction and magnitude together.

    They are asked as one question because they are one vector's target, sign and
    length: ``event_emotion`` established that polarity without a named subject has
    no meaning, so splitting them would reproduce a failure this project has already
    diagnosed.  Tone stays out of it -- ``article_sentiment`` owns that, and the two
    are meant to disagree.
    """
    return f"""影响对象（affected_objects）的判据：
{_impact_object_guide_text()}
{impact_object_rules()}

影响方向（impact_direction）四个取值的判据：
{IMPACT_DIRECTION_GUIDE}

影响强度（impact_magnitude）三个取值的判据：
{IMPACT_MAGNITUDE_GUIDE}

方向与强度的判定规则：
- 判断的是**事件对本主体的实际影响**，不是稿件的语气，也不是记者的态度。一份措辞平实、只陈述
  事实与法条的监管处罚公告，影响方向仍是 negative——**行文语气由 article_sentiment 另行承担，
  两者允许不一致，也本该不一致**。
- 「风险」「调查」「下滑」等词可能出现在与本主体无关的背景描述里，**不得仅凭出现这类词就判 negative**。
- 程序性进展按它推进了什么来判：问询回复被受理是 positive，被中止审核是 negative，
  按期披露一份没有异常的报告是 neutral。
- 强度的判据是**这件事对本主体的影响面，不是稿件写得有多重要**，也不是标题里有没有「重大」二字：
  一份名为《重大事项进展公告》而内容只说「仍在推进」的稿件是 minor。
- 拿不准 major 与 moderate 时选 moderate。**major 是需要举证的判断**：选 major 时，
  magnitude_basis 必须指出它满足上面三条中的哪一条，理由里必须写出对应的原文事实。"""


def _impact_object_guide_text() -> str:
    """The twenty objects, grouped, as the prompts render them."""
    lines = []
    for group, members in IMPACT_OBJECT_GROUPS:
        lines.append(f"〔{group}〕")
        lines.extend(f"- {name} {IMPACT_OBJECT_GUIDE[name]}" for name in members)
    return "\n".join(lines)


TYPE_RULES: dict[str, tuple[str, ...]] = {
    "REGULATORY_COMPLIANCE": ("监管", "处罚", "行政", "合规", "调查", "制裁", "实体清单"),
    "LITIGATION_ARBITRATION": ("诉讼", "仲裁", "起诉", "法院", "判决", "侵权"),
    "FINANCIAL_AUDIT": ("营收", "收入", "利润", "亏损", "毛利", "审计", "业绩", "财报"),
    "IPO_REFINANCING": ("ipo", "招股", "发行", "募资", "问询", "上会", "注册", "上市"),
    "EQUITY_CONTROL": ("股东", "实控人", "控股", "增持", "减持", "持股"),
    "EXECUTIVE_PERSONNEL": ("董事", "高管", "总经理", "核心技术人员", "辞任", "任命"),
    "CUSTOMER_SUPPLIER_OPERATIONS": ("客户", "订单", "供应商", "供应链", "合作", "运营"),
    "PRODUCT_TECH_IP": ("技术", "产品", "芯片", "专利", "研发", "平台", "临床"),
    "SAFETY_QUALITY_RECALL": ("事故", "安全", "召回", "质量", "网络安全", "cybersecurity"),
    "M_AND_A_ASSET_TRANSACTION": ("收购", "并购", "出售", "资产交易", "投资"),
    "ESG_REPUTATION": ("esg", "环保", "声誉", "碳", "污染"),
    "RESPONSE_CLARIFICATION": ("澄清", "回应", "说明", "辟谣", "更正"),
}
NEGATIVE_TERMS = ("处罚", "诉讼", "亏损", "下滑", "制裁", "安全事件", "事故", "风险", "调查", "攻击")
POSITIVE_TERMS = ("增长", "突破", "获批", "合作", "上市", "盈利", "提升", "交付", "strong results")
EVENT_TERMS = tuple(sorted({item for values in TYPE_RULES.values() for item in values} | {
    "发布", "宣布", "完成", "签署", "披露", "推出", "测试", "成交", "获准", "通过",
}))


@dataclass(frozen=True)
class RawDocument:
    version_id: str
    source_id: str
    source_tier: str
    company_id: str | None
    title: str | None
    published_at: str | None
    canonical_url: str
    document_type: str | None
    content_hash: str
    raw_path: str
    review_status: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def column_exists(db: sqlite3.Connection, table: str, column: str) -> bool:
    return column in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def add_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """Add a column to an existing table, once.

    ``CREATE TABLE IF NOT EXISTS`` silently skips a table that already exists,
    so a new column never reaches a database built by an earlier version
    without this.
    """
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def ensure_schema(db: sqlite3.Connection) -> None:
    """Create the analytical layer without changing collection tables."""
    db.execute("""CREATE TABLE IF NOT EXISTS processing_runs (
        run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT,
        status TEXT NOT NULL, processor_version TEXT NOT NULL, options_json TEXT NOT NULL,
        documents_seen INTEGER NOT NULL DEFAULT 0, documents_processed INTEGER NOT NULL DEFAULT 0,
        ai_documents INTEGER NOT NULL DEFAULT 0, error_message TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS processed_documents (
        version_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, content_hash TEXT NOT NULL,
        text_path TEXT, text_hash TEXT, text_chars INTEGER NOT NULL DEFAULT 0,
        page_count INTEGER, extraction_status TEXT NOT NULL, inclusion_status TEXT NOT NULL,
        relevance_score REAL NOT NULL DEFAULT 0, eventhood TEXT NOT NULL DEFAULT 'IRRELEVANT',
        rejection_reasons_json TEXT NOT NULL DEFAULT '[]', exact_duplicate_of TEXT,
        near_duplicate_cluster_id TEXT, processor_version TEXT NOT NULL, processed_at TEXT NOT NULL,
        FOREIGN KEY(version_id) REFERENCES document_versions(version_id),
        FOREIGN KEY(run_id) REFERENCES processing_runs(run_id))""")
    # The rule layer's own reading, kept where a model cannot reach it.  Labelling
    # writes its verdict over ``eventhood``/``relevance_score``, and eligibility
    # for labelling is decided from those same fields -- so one NO_EVENT from a
    # model dropped a document below the floor and out of the candidate set for
    # good.  Two BIOSECURE Act filings naming the company that way, and only a
    # database snapshot got them back.  The floor has to survive the model.
    processed_columns = {row[1] for row in db.execute("PRAGMA table_info(processed_documents)")}
    if "rule_eventhood" not in processed_columns:
        db.execute("ALTER TABLE processed_documents ADD COLUMN rule_eventhood TEXT")
        db.execute("ALTER TABLE processed_documents ADD COLUMN rule_relevance_score REAL")
        # Rows written before the split carry the rule reading unless a model has
        # already overwritten it; those are repaired from the labelling history.
        db.execute("""UPDATE processed_documents SET rule_eventhood=eventhood,
            rule_relevance_score=relevance_score WHERE rule_eventhood IS NULL""")
    db.execute("""CREATE TABLE IF NOT EXISTS event_records (
        event_id TEXT PRIMARY KEY, version_id TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL,
        company_id TEXT NOT NULL, event_types_json TEXT NOT NULL, issue_codes_json TEXT NOT NULL,
        production_mode TEXT NOT NULL, event_subjects_json TEXT NOT NULL,
        event_time TEXT, event_time_precision TEXT, affected_objects_json TEXT NOT NULL,
        impact_direction TEXT NOT NULL, impact_magnitude TEXT NOT NULL,
        article_sentiment TEXT NOT NULL, relevance_score REAL NOT NULL,
        eventhood TEXT NOT NULL, summary TEXT NOT NULL, amounts_json TEXT NOT NULL,
        ratios_json TEXT NOT NULL, persons_json TEXT NOT NULL, organizations_json TEXT NOT NULL,
        products_json TEXT NOT NULL, risk_score REAL NOT NULL, risk_level TEXT NOT NULL,
        recommended_action TEXT NOT NULL, label_source TEXT NOT NULL,
        model_name TEXT, prompt_version TEXT, extraction_confidence REAL NOT NULL,
        needs_review INTEGER NOT NULL DEFAULT 0, raw_label_json TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        FOREIGN KEY(version_id) REFERENCES document_versions(version_id),
        FOREIGN KEY(run_id) REFERENCES processing_runs(run_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS event_claims (
        claim_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
        text TEXT NOT NULL, issue_code TEXT, claim_type TEXT NOT NULL,
        source_start INTEGER NOT NULL, source_end INTEGER NOT NULL, cited_source TEXT,
        confidence REAL NOT NULL, FOREIGN KEY(event_id) REFERENCES event_records(event_id),
        UNIQUE(event_id, ordinal))""")
    db.execute("""CREATE TABLE IF NOT EXISTS duplicate_clusters (
        cluster_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, cluster_kind TEXT NOT NULL,
        representative_version_id TEXT NOT NULL, member_count INTEGER NOT NULL,
        similarity_threshold REAL, created_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS duplicate_cluster_members (
        cluster_id TEXT NOT NULL, version_id TEXT NOT NULL, similarity REAL NOT NULL,
        PRIMARY KEY(cluster_id, version_id), FOREIGN KEY(cluster_id) REFERENCES duplicate_clusters(cluster_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS issue_ledger (
        issue_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, issue_code TEXT NOT NULL,
        first_seen_at TEXT, last_seen_at TEXT, event_count INTEGER NOT NULL,
        document_count INTEGER NOT NULL, independent_source_count INTEGER NOT NULL,
        risk_score REAL NOT NULL, risk_level TEXT NOT NULL, status TEXT NOT NULL,
        updated_at TEXT NOT NULL, UNIQUE(company_id, issue_code))""")
    db.execute("""CREATE TABLE IF NOT EXISTS disclosure_diffs (
        diff_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, pair_key TEXT NOT NULL,
        left_version_id TEXT NOT NULL, right_version_id TEXT NOT NULL,
        left_label TEXT, right_label TEXT, section TEXT, diff_kind TEXT NOT NULL,
        left_text TEXT, right_text TEXT, left_page INTEGER, right_page INTEGER,
        metric TEXT, old_value TEXT, new_value TEXT, delta REAL, delta_ratio REAL,
        similarity REAL, created_at TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_diffs_pair ON disclosure_diffs(company_id,pair_key,diff_kind)")
    # Added after the table shipped: a figure that moved because the reporting
    # period moved is a different fact from one restated under the same period.
    if "period_rollover" not in {row[1] for row in db.execute("PRAGMA table_info(disclosure_diffs)")}:
        db.execute("ALTER TABLE disclosure_diffs ADD COLUMN period_rollover INTEGER NOT NULL DEFAULT 0")
    db.execute("""CREATE TABLE IF NOT EXISTS event_elements (
        element_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, element_type TEXT NOT NULL,
        ordinal INTEGER NOT NULL, raw_text TEXT NOT NULL, normalized_value TEXT,
        unit TEXT, basis TEXT, status TEXT NOT NULL, extra_json TEXT NOT NULL DEFAULT '{}',
        FOREIGN KEY(event_id) REFERENCES event_records(event_id),
        UNIQUE(event_id, element_type, ordinal))""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_elements_event ON event_elements(event_id,element_type)")
    db.execute("""CREATE TABLE IF NOT EXISTS source_boilerplate (
        source_id TEXT PRIMARY KEY, lines_json TEXT NOT NULL,
        document_count INTEGER NOT NULL, computed_at TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_processed_inclusion ON processed_documents(inclusion_status,eventhood)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_events_company ON event_records(company_id,eventhood,risk_score)")
    add_column(db, "event_records", "review_reasons_json", "TEXT NOT NULL DEFAULT '[]'")
    # 风险分的七项加减。1:1 于事件、只读、总与 risk_score 一同被读，所以是一列而不是一张表。
    add_column(db, "event_records", "risk_breakdown_json", "TEXT NOT NULL DEFAULT '[]'")
    # 事件时间是已发生，还是报道发布时尚在未来的计划/截止日期（I-04）。由 normalize 写入。
    add_column(db, "event_records", "event_time_role", "TEXT")
    # 议题的风险均值取自哪几条成员：其余是纯行情数据，计入报道数而不计入风险。
    add_column(db, "issue_ledger", "risk_bearing_event_count", "INTEGER NOT NULL DEFAULT 0")
    # 要素页的四条聚合各扫一遍全表；按分组列建索引后它们走索引扫描而不是全表扫描。
    db.execute("""CREATE INDEX IF NOT EXISTS idx_elements_type_status
                  ON event_elements(element_type, status)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_elements_unit ON event_elements(unit)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_elements_basis ON event_elements(basis)")
    add_column(db, "issue_ledger", "event_time_earliest", "TEXT")
    add_column(db, "issue_ledger", "event_time_latest", "TEXT")
    # How many of an issue's events a person put there by hand (M4-6). An issue
    # the pipeline assembled and one a reviewer repaired are indistinguishable
    # in a count, and that difference is the point of being able to repair it.
    add_column(db, "issue_ledger", "manual_event_count", "INTEGER NOT NULL DEFAULT 0")
    db.commit()


def text_file_path(content_hash: str) -> Path:
    return TEXT_ROOT / content_hash[:2] / f"{content_hash}.txt"


def clean_text(value: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t\u00a0]+", " ", html.unescape(value))).strip()


def decode_html(body: bytes) -> str:
    """Honour the page's declared charset before falling back.

    Chinese financial portals still serve GB2312/GBK.  Decoding those as UTF-8
    or Latin-1 yields mojibake that no amount of downstream parsing recovers,
    so the declared charset is tried first and ``gb18030`` (a GBK superset)
    backs up UTF-8.
    """
    declared = re.search(rb"""charset=["']?\s*([\w-]+)""", body[:4096], re.I)
    candidates: list[str] = []
    if declared:
        candidates.append(declared.group(1).decode("ascii", "ignore"))
    candidates += ["utf-8", "gb18030"]
    for encoding in candidates:
        try:
            return body.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


def extract_html(body: bytes) -> tuple[str, int | None]:
    """Prefer the article container so navigation and related-story feeds stay out.

    Keeping a page's other stories would let a claim be "located" in an article
    the document is not about, which silently breaks evidence citation.
    """
    soup = BeautifulSoup(decode_html(body), "html.parser")
    for element in soup(["script", "style", "noscript", "svg", "iframe", "form"]):
        element.decompose()
    for element in soup.select("nav, footer, header, aside"):
        element.decompose()
    main = None
    for selector in MAIN_CONTENT_SELECTORS:
        try:
            found = soup.select_one(selector)
        except Exception:  # noqa: BLE001 - a malformed selector must not stop extraction
            continue
        if found and len(found.get_text(strip=True)) >= BODY_TEXT_FLOOR:
            main = found
            break
    return clean_text((main or soup).get_text("\n")), None


def boilerplate_lines(texts: Sequence[str]) -> set[str]:
    """Learn one source's repeated chrome from its own pages."""
    if len(texts) < BOILERPLATE_MIN_DOCUMENTS:
        return set()
    counts: Counter[str] = Counter()
    for text in texts:
        # Count each distinct line once per document, so a menu repeated inside
        # a single page cannot reach the threshold on its own.
        counts.update({line.strip() for line in text.splitlines() if line.strip()})
    limit = len(texts) * BOILERPLATE_LINE_SHARE
    return {line for line, seen in counts.items() if seen >= limit}


def strip_boilerplate(text: str, lines: set[str]) -> str:
    if not lines:
        return text
    return clean_text("\n".join(line for line in text.splitlines() if line.strip() not in lines))


def extract_jsonish(body: bytes) -> tuple[str, int | None]:
    try:
        value = json.loads(body.decode("utf-8", errors="replace"))
        return clean_text(json.dumps(value, ensure_ascii=False, indent=2)), None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return clean_text(body.decode("utf-8", errors="replace")), None


def extract_pdf(body: bytes) -> tuple[str, int | None]:
    if fitz is None:
        return "", None
    try:
        pdf = fitz.open(stream=body, filetype="pdf")
        chunks = [f"[PAGE {page.number + 1}]\n{clean_text(page.get_text('text'))}" for page in pdf]
        return "\n\n".join(chunks).strip(), len(pdf)
    except Exception as exc:
        return "", None


def extract_text(raw_path: Path) -> tuple[str, int | None, str, str]:
    """Return parsed text, page count, status and the media kind it came from.

    The media kind matters downstream: boilerplate learning applies to HTML
    pages only, since a PDF's repetition is part of the document itself.
    """
    try:
        body = raw_path.read_bytes()
    except OSError:
        return "", None, "missing_raw", "unknown"
    if body.startswith(b"%PDF"):
        text, pages = extract_pdf(body)
        return text, pages, "ok" if text else "pdf_no_text", "pdf"
    lowered = raw_path.suffix.lower()
    if lowered in {".html", ".htm"} or b"<html" in body[:1000].lower():
        text, pages = extract_html(body)
        return text, pages, "ok" if text else "empty", "html"
    text, pages = extract_jsonish(body)
    return text, pages, "ok" if text else "empty", "data"


def numeric_tokens(text: str) -> set[str]:
    """Every number in the text, normalised.

    Kept apart from ``tokenize`` on purpose.  Folding numbers into the main token
    set was the obvious repair and it does not work (see ``TEMPLATE_TEXT_BAR``);
    what works is comparing the numbers *by themselves*, which needs them in
    their own set.
    """
    return {match.group().replace(",", "") for match in NUMBER_TOKEN.finditer(text)}


def within_days(left: str | None, right: str | None, days: int) -> bool:
    """Both dates present and no further apart than ``days``.

    Missing is not near.  Publication dates are absent on 4.1% of the corpus and
    an absent date is not evidence of contemporaneity, so the pair simply does
    not qualify for the route that depends on it.
    """
    try:
        # Publication timestamps arrive both as bare dates and as full ISO
        # instants; the first ten characters are the calendar date in either.
        first = date.fromisoformat((left or "").strip()[:10])
        second = date.fromisoformat((right or "").strip()[:10])
    except ValueError:
        return False
    return abs((first - second).days) <= days


def normalize_title(title: str | None) -> str:
    value = unicodedata.normalize("NFKC", title or "").lower()
    value = re.sub(r"\b(公告|announcement|news|新闻稿)\b", "", value)
    return re.sub(r"[^\w\u4e00-\u9fff]", "", value)


def tokenize(text: str) -> set[str]:
    lowered = text.lower()
    english = re.findall(r"[a-z][a-z0-9_-]{1,}", lowered)
    chinese = re.findall(r"[\u4e00-\u9fff]{2}", lowered)
    # Bigrams preserve Chinese content without an external segmenter.
    cjk_bigrams = [piece[i:i + 2] for piece in re.findall(r"[\u4e00-\u9fff]{2,}", lowered)
                  for i in range(len(piece) - 1)]
    return set(english + chinese + cjk_bigrams)


def simhash(tokens: Iterable[str]) -> int:
    weights = [0] * 64
    for token in tokens:
        value = int(hashlib.sha256(token.encode("utf-8")).hexdigest()[:16], 16)
        for bit in range(64):
            weights[bit] += 1 if value & (1 << bit) else -1
    return sum(1 << bit for bit, weight in enumerate(weights) if weight >= 0)


def hamming(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def source_locator(text: str, start: int, end: int) -> str:
    prefix = text[:max(0, start)]
    match = list(re.finditer(r"\[PAGE (\d+)\]", prefix))
    return f"page:{match[-1].group(1)}" if match else "text"


def sentence_candidates(text: str, limit: int = 3) -> list[tuple[str, int, int]]:
    found: list[tuple[str, int, int]] = []
    for match in re.finditer(r"[^。！？.!?\n]{12,260}[。！？.!?]", text):
        raw_sentence = match.group(0)
        leading = len(raw_sentence) - len(raw_sentence.lstrip())
        sentence = raw_sentence.strip()
        if any(term.lower() in sentence.lower() for term in EVENT_TERMS):
            start = match.start() + leading
            found.append((sentence, start, start + len(sentence)))
        if len(found) >= limit:
            break
    return found


def has_term(text: str, terms: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


# A keyword hit anywhere in the document used to be enough to emit a code, which
# put 4.56 issue codes and 3.74 event types on the average document where the
# official gold carries 1.07 and 1.03.  The excess is not harmless: SUPPLY_CHAIN
# fired on 482 of 1,057 scored articles and SAFETY_QUALITY_RECALL on 971 of
# 2,046, and every one of those flows into the issue ledger, the affected-object
# list and the risk score of a document labelled on the offline path.
#
# A document is *about* the theme its wording keeps returning to; a theme
# mentioned once in passing is not a second subject.  So codes are ranked by how
# often their terms occur -- a hit in the title counting for more, a title being
# written to say what the piece is about -- and only those within DOMINANCE of
# the strongest survive, at most CODE_CAP of them.  The threshold is a judgement
# about what "equally present" means, not a tuned constant; it is stated here
# because E1 is computed on this path and the number moves with it.
CODE_DOMINANCE = 2 / 3
CODE_CAP = 3
TITLE_HIT_WEIGHT = 3


@lru_cache(maxsize=512)
def _latin_term(term: str) -> re.Pattern[str] | None:
    """A whole-word pattern for a Latin-script term; None for a Chinese one.

    Counted as substrings, ``cro`` is found inside "micron" and "macro" — a
    memory-chip report naming 美光 scored as drug development — and ``pe``
    inside "operating".  Chinese has no word boundaries to respect, so Chinese
    terms keep substring matching.  On the organiser's gold this moved 40 of
    3,043 rule predictions and nudged test micro-F1 from 0.2358 to 0.2368.
    """
    if not re.fullmatch(r"[a-z0-9 ]+", term):
        return None
    return re.compile(rf"(?<![a-z]){re.escape(term)}(?![a-z])")


def code_strength(title: str, blob: str, terms: Iterable[str]) -> int:
    lowered, titled = blob.lower(), title.lower()
    strength = 0
    for term in terms:
        term = term.lower()
        pattern = _latin_term(term)
        if pattern is None:
            occurrences, in_title = lowered.count(term), term in titled
        else:
            occurrences, in_title = len(pattern.findall(lowered)), bool(pattern.search(titled))
        if occurrences:
            strength += occurrences + (TITLE_HIT_WEIGHT if in_title else 0)
    return strength


def rank_codes(title: str, blob: str, rules: dict[str, tuple[str, ...]]) -> list[str]:
    """The codes a document is actually about, strongest first.

    Never empties a set that had a match: the strongest code always survives, so
    the event-hood gate that reads ``event_types`` sees exactly what it saw
    before.  Ties break on the code name so a run is reproducible.
    """
    scored = sorted(((code_strength(title, blob, terms), name)
                     for name, terms in rules.items()),
                    key=lambda pair: (-pair[0], pair[1]))
    scored = [(strength, name) for strength, name in scored if strength]
    if not scored:
        return []
    floor = scored[0][0] * CODE_DOMINANCE
    return [name for strength, name in scored if strength >= floor][:CODE_CAP]


def production_mode(document_type: str | None, title: str, text: str) -> str:
    """The rule path's production mode: three of the eight values, by keyword.

    The formal criteria for all eight are ``MODE_GUIDE`` above, which only the
    model path can act on.  What matters here is the fallback: it used to be
    ``ORIGINAL_INVESTIGATION`` -- the highest weight in ``alerts.MODE_CREDIBILITY``
    (1.0) -- while the model path falls back to ``BRIEF`` (0.6) and its prompt
    warns explicitly against overusing it.  Two paths feeding one credibility
    formula must not default in opposite directions, and of the two, defaulting a
    piece nobody has classified to "a reporter's own investigation" is the claim
    that needs the evidence.
    """
    blob = f"{title}\n{text[:3000]}".lower()
    if document_type in {"news_brief", "market_newsflash", "announcements", "listing_documents", "review_responses"}:
        return "BRIEF"
    if any(token in blob for token in ("转载", "转自", "来源：", "source:")):
        return "REPOST"
    if any(token in blob for token in ("公司回应", "本公司", "news release", "press release")):
        return "INTERPRETATION"
    return "BRIEF"


def rule_label(doc: RawDocument, text: str) -> dict[str, Any]:
    title = doc.title or ""
    blob = f"{title}\n{text[:12000]}"
    # A market flash can be a single but still meaningful sentence; only discard
    # truly empty/snippet-level bodies here.
    relevant = bool(doc.company_id) and len(text) >= 20
    reasons: list[str] = []
    if not doc.company_id:
        reasons.append("no_linked_company")
    if len(text) < 20:
        reasons.append("insufficient_text")
    if not relevant:
        return {"relevance_score": 0.0, "eventhood": "IRRELEVANT", "reasons": reasons,
                "event_types": [], "issue_codes": [], "production_mode": "BRIEF",
                "summary": "", "claims": [], "confidence": 0.9}
    event_types = rank_codes(title, blob, TYPE_RULES)
    issues = rank_codes(title, blob, ISSUE_RULES)
    candidates = sentence_candidates(text)
    eventhood = "EVENT" if event_types and candidates else "NO_EVENT"
    if not event_types:
        reasons.append("no_event_trigger")
    elif not candidates:
        reasons.append("no_traceable_event_sentence")
    direction = "negative" if has_term(blob, NEGATIVE_TERMS) else "positive" if has_term(blob, POSITIVE_TERMS) else "neutral"
    magnitude = "major" if any(term in blob.lower() for term in ("重大", "major", "100亿元", "billion", "处罚", "制裁")) else "moderate" if eventhood == "EVENT" else "minor"
    affected: list[str] = []
    if any(issue in issues for issue in ("FINANCIAL", "FUND_RAISE", "VALUATION", "STOCK_PRICE")):
        affected.append("investors_stock_price")
    if any(issue in issues for issue in ("TECH_LEVEL", "IP_PATENT", "DRUG_DEVELOPMENT")):
        affected.append("technology_product")
    if any(issue in issues for issue in ("YIELD_CAPACITY", "SUPPLY_CHAIN", "CUSTOMER")):
        affected.append("operations")
    if "REGULATORY_COMPLIANCE" in event_types:
        affected.append("regulatory_compliance")
    claims = [{"text": sentence, "issue_code": issues[0] if issues else None, "claim_type": "fact",
               "source_start": start, "source_end": end, "cited_source": source_locator(text, start, end),
               "confidence": 0.62} for sentence, start, end in candidates]
    return {
        "relevance_score": 0.92 if eventhood == "EVENT" else 0.70,
        "eventhood": eventhood, "reasons": reasons, "event_types": event_types,
        "issue_codes": issues, "production_mode": production_mode(doc.document_type, title, text),
        "event_subjects": [doc.company_id], "event_time": None, "event_time_precision": None,
        "affected_objects": sorted(set(affected)), "impact_direction": direction,
        "impact_magnitude": magnitude, "article_sentiment": direction if direction != "uncertain" else "neutral",
        "summary": candidates[0][0] if candidates else (title or "未识别到可验证事件"),
        "amounts": extract_amounts(blob), "ratios": extract_ratios(blob), "persons": [],
        "organizations": [], "products": [], "claims": claims, "confidence": 0.64 if eventhood == "EVENT" else 0.78,
    }


def extract_amounts(text: str) -> list[dict[str, str]]:
    values = []
    for match in re.finditer(r"(?<!\d)(\d+(?:\.\d+)?)\s*(亿元|亿|万元|万|人民币|rmb|usd|美元)", text, re.I):
        values.append({"raw": match.group(0), "value": match.group(1), "unit": match.group(2)})
    return values[:12]


def extract_ratios(text: str) -> list[dict[str, str]]:
    return [{"raw": match.group(0), "value": match.group(1), "unit": "%"}
            for match in re.finditer(r"(?<!\d)(\d+(?:\.\d+)?)\s*%", text)][:12]


# 纯行情数据不进风险与预警的分母。
#
# 判定不是一个新标签，而是一个可推导的事实：**事件类型层判为「一类都不贴合」**。
# 12 类按风险类型立目，而单日成交额、换手率、大宗交易明细、涨跌停落不进其中任何一格
# ——2026-09-07 的重标里 175 条行情快讯有 167 条被判为空列表，其中 122 条的生产方式是
# AI_GENERATED（模板化播报）。
#
# 排除的范围刻意窄：**事件台账与传播图保留**。这些稿件是真实文档，469 条因果边与传播
# 独立分量都靠它们；删掉会连证据一起删。排除的只是「它算不算一条风险」——预警闸门、
# 议题台账与风险分布。
def risk_bearing_clause(alias: str = "e") -> str:
    """SQL 片段：该事件是否承载风险（即事件类型层给出了至少一类）。"""
    return f"COALESCE({alias}.event_types_json,'[]') NOT IN ('[]','')"


def risk_bearing_exists(alias: str = "t") -> str:
    """同一判定，供只连了 event_timeline 一类表的查询使用。"""
    return (f"EXISTS(SELECT 1 FROM event_records e WHERE e.event_id={alias}.event_id"
            f" AND {risk_bearing_clause('e')})")


RISK_BASELINE = 15.0
SENSITIVE_EVENT_TYPES = frozenset({"REGULATORY_COMPLIANCE", "LITIGATION_ARBITRATION", "SAFETY_QUALITY_RECALL"})
SENSITIVE_ISSUE_CODES = frozenset({"EXPORT_CTRL", "CYBERSECURITY", "FINANCIAL", "IP_PATENT"})


@dataclass(frozen=True)
class RiskPart:
    """One term of the risk score, with the value that earned it."""
    key: str
    points: float
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "points": self.points, "detail": self.detail}


def risk_parts(label: dict[str, Any]) -> list[RiskPart]:
    """The terms the risk score is made of — the score's only definition.

    They were an inline sum until 2026-09-06, which had two consequences worth
    naming.  The score reaches the pages as a bare number: ``LevelBadge`` shows
    「高 85」 and nothing about it can be checked, while the credibility figure
    beside it has shown its four weighted parts since it was written.  And the
    seven terms could not be recomputed from a row, so ``production_mode``'s
    re-classification pass — which moves ``SECONDHAND_MODES`` membership, worth
    −10 — left 296 events carrying a score derived from a production mode they no
    longer have, 52 of them in the wrong band.  ``refresh_risk`` closes that by
    replaying this function; both fixes need the terms as data, not as a sum.

    The clamp is a term of its own: without it the capped rows carry a breakdown
    that does not add up to the score beside it.
    """
    parts = [RiskPart("base", RISK_BASELINE)]
    types = set(label["event_types"])
    issues = set(label["issue_codes"])
    if label["eventhood"] == "EVENT":
        parts.append(RiskPart("eventhood", 20.0, "EVENT"))
    if label["impact_direction"] == "negative":
        parts.append(RiskPart("direction", 20.0, "negative"))
    if label["impact_magnitude"] == "major":
        parts.append(RiskPart("magnitude", 20.0, "major"))
    elif label["impact_magnitude"] == "moderate":
        parts.append(RiskPart("magnitude", 10.0, "moderate"))
    if types & SENSITIVE_EVENT_TYPES:
        parts.append(RiskPart("sensitive_type", 20.0, "、".join(sorted(types & SENSITIVE_EVENT_TYPES))))
    if issues & SENSITIVE_ISSUE_CODES:
        parts.append(RiskPart("sensitive_issue", 10.0, "、".join(sorted(issues & SENSITIVE_ISSUE_CODES))))
    if label["production_mode"] in SECONDHAND_MODES:
        parts.append(RiskPart("secondhand", -10.0, label["production_mode"]))
    raw = sum(part.points for part in parts)
    clamped = max(0.0, min(100.0, raw))
    if clamped != raw:
        parts.append(RiskPart("capped", round(clamped - raw, 1), "上限 100" if raw > 100 else "下限 0"))
    return parts


def risk(label: dict[str, Any]) -> tuple[float, str, str]:
    """Score, band and recommended action — summed from ``risk_parts``, not recomputed.

    The bands (75 / 45) and this function's 3-tuple signature are unchanged; only
    the sum moved out.  Adding a term in two places is how a breakdown starts
    disagreeing with the number beside it.
    """
    score = round(sum(part.points for part in risk_parts(label)), 1)
    level = "high" if score >= 75 else "medium" if score >= 45 else "low"
    action = "immediate_review" if level == "high" else "monitor" if level == "medium" else "archive"
    return score, level, action


# The six fields ``risk_parts`` reads, as stored on ``event_records``.  The narrow
# sidecars overwrite some of them long after extraction, so the score has to be
# derivable from the row rather than from ``raw_label_json`` — that column still
# holds the production mode, direction and magnitude the extractor first guessed,
# which is exactly the stale answer.
RISK_INPUT_COLUMNS = ("eventhood", "event_types_json", "issue_codes_json",
                      "production_mode", "impact_direction", "impact_magnitude")


def risk_label_from_row(row: Sequence[Any]) -> dict[str, Any]:
    """Build the label ``risk_parts`` needs from a ``RISK_INPUT_COLUMNS`` row."""
    eventhood, types_json, issues_json, production, direction, magnitude = row
    return {"eventhood": eventhood, "event_types": json.loads(types_json or "[]"),
            "issue_codes": json.loads(issues_json or "[]"), "production_mode": production,
            "impact_direction": direction, "impact_magnitude": magnitude}


def refresh_risk(db: sqlite3.Connection, event_id: str) -> tuple[float, str, str] | None:
    """Recompute one event's risk score from its current columns.

    Every pass that rewrites one of ``RISK_INPUT_COLUMNS`` after extraction has to
    end here, or it leaves the score describing the value it replaced.
    """
    row = db.execute(f"SELECT {','.join(RISK_INPUT_COLUMNS)} FROM event_records WHERE event_id=?",
                     (event_id,)).fetchone()
    if row is None:
        return None
    label = risk_label_from_row(tuple(row))
    score, level, action = risk(label)
    db.execute("""UPDATE event_records SET risk_score=?,risk_level=?,recommended_action=?,
                  risk_breakdown_json=?,updated_at=? WHERE event_id=?""",
               (score, level, action,
                json.dumps([part.as_dict() for part in risk_parts(label)], ensure_ascii=False),
                utc_now(), event_id))
    return score, level, action


def resolve_boilerplate(db: sqlite3.Connection, parsed: Sequence[tuple[RawDocument, str, Any, str, str]]) -> dict[str, set[str]]:
    """Learn chrome for every source parsed in this run; reuse stored sets otherwise.

    Persisting the result keeps incremental runs consistent with the full run
    that established a source's chrome, instead of re-learning it from a
    handful of new pages.
    """
    by_source: dict[str, list[str]] = defaultdict(list)
    for doc, text, _pages, status, media in parsed:
        if media == "html" and status == "ok" and text:
            by_source[doc.source_id].append(text)
    resolved: dict[str, set[str]] = {}
    for source_id, texts in by_source.items():
        if len(texts) >= BOILERPLATE_MIN_DOCUMENTS:
            lines = boilerplate_lines(texts)
            db.execute("""INSERT INTO source_boilerplate(source_id,lines_json,document_count,computed_at)
                VALUES(?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET lines_json=excluded.lines_json,
                document_count=excluded.document_count,computed_at=excluded.computed_at""",
                (source_id, json.dumps(sorted(lines), ensure_ascii=False), len(texts), utc_now()))
            resolved[source_id] = lines
        else:
            stored = db.execute("SELECT lines_json FROM source_boilerplate WHERE source_id=?", (source_id,)).fetchone()
            resolved[source_id] = set(json.loads(stored[0])) if stored else set()
    return resolved


def load_documents(db: sqlite3.Connection, companies: set[str] | None) -> list[RawDocument]:
    query = """SELECT version_id,source_id,source_tier,company_id,title,published_at,canonical_url,
                      document_type,content_hash,raw_path,review_status
               FROM document_versions WHERE review_status NOT LIKE '%rejected%'"""
    params: list[str] = []
    if companies:
        query += " AND company_id IN (%s)" % ",".join("?" * len(companies))
        params = sorted(companies)
    return [RawDocument(*row) for row in db.execute(query, params).fetchall()]


def persist_processed(db: sqlite3.Connection, doc: RawDocument, run_id: str, text_path: Path | None,
                      text: str, pages: int | None, status: str, inclusion: str, label: dict[str, Any],
                      duplicate_of: str | None) -> None:
    relative = str(text_path.relative_to(ROOT)) if text_path else None
    db.execute("""INSERT INTO processed_documents
        (version_id,run_id,content_hash,text_path,text_hash,text_chars,page_count,extraction_status,
         inclusion_status,relevance_score,eventhood,rejection_reasons_json,exact_duplicate_of,
         processor_version,processed_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(version_id) DO UPDATE SET run_id=excluded.run_id,text_path=excluded.text_path,
         text_hash=excluded.text_hash,text_chars=excluded.text_chars,page_count=excluded.page_count,
         extraction_status=excluded.extraction_status,inclusion_status=excluded.inclusion_status,
         relevance_score=excluded.relevance_score,eventhood=excluded.eventhood,
         rejection_reasons_json=excluded.rejection_reasons_json,exact_duplicate_of=excluded.exact_duplicate_of,
         processor_version=excluded.processor_version,processed_at=excluded.processed_at""",
        (doc.version_id, run_id, doc.content_hash, relative,
         hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None, len(text), pages, status,
         inclusion, label["relevance_score"], label["eventhood"], json.dumps(label["reasons"], ensure_ascii=False),
         duplicate_of, PROCESSOR_VERSION, utc_now()))
    db.execute("""UPDATE processed_documents SET rule_eventhood=?,
        rule_relevance_score=? WHERE version_id=?""",
        (label["eventhood"], label["relevance_score"], doc.version_id))


def set_review_reason(db: sqlite3.Connection, event_id: str, reason: str, active: bool) -> None:
    """Add or clear one review reason and recompute ``needs_review`` from the set.

    Three passes flag an event for different causes — low model confidence, an
    element that would not standardize, a claim whose quote could not be located.
    A bare boolean cannot record which of them still applies, so none of them
    could ever safely clear it and every AI-labelled event stayed flagged
    forever.  Keeping the reasons makes the flag derivable, and therefore
    clearable once the cause is actually gone.
    """
    row = db.execute("SELECT review_reasons_json FROM event_records WHERE event_id=?", (event_id,)).fetchone()
    if row is None:
        return
    reasons = set(json.loads(row[0] or "[]"))
    reasons.add(reason) if active else reasons.discard(reason)
    db.execute("UPDATE event_records SET review_reasons_json=?,needs_review=?,updated_at=? WHERE event_id=?",
               (json.dumps(sorted(reasons)), int(bool(reasons)), utc_now(), event_id))


def table_exists(db: sqlite3.Connection, name: str) -> bool:
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                           (name,)).fetchone())


def delete_event(db: sqlite3.Connection, event_id: str) -> dict[str, int]:
    """Remove an event and everything that hangs off it.

    Half the dependent tables are created by later passes (timeline, sentiment,
    production mode, verdicts), so an installation that has only run the
    pipeline does not have them yet.  Each is checked rather than wrapped in a
    bare ``except``: a missing table is a state, a broken statement is a bug,
    and swallowing both together is how the second one goes unnoticed.
    """
    removed: dict[str, int] = {}
    claims = [row[0] for row in
              db.execute("SELECT claim_id FROM event_claims WHERE event_id=?", (event_id,))]
    for table in CLAIM_DEPENDENTS:
        if not table_exists(db, table):
            continue
        for claim_id in claims:
            removed[table] = removed.get(table, 0) + db.execute(
                f"DELETE FROM {table} WHERE claim_id=?", (claim_id,)).rowcount
    removed["event_claims"] = db.execute(
        "DELETE FROM event_claims WHERE event_id=?", (event_id,)).rowcount
    for table in EVENT_DEPENDENTS:
        if not table_exists(db, table):
            continue
        removed[table] = db.execute(f"DELETE FROM {table} WHERE event_id=?", (event_id,)).rowcount
    removed["event_records"] = db.execute(
        "DELETE FROM event_records WHERE event_id=?", (event_id,)).rowcount
    return removed


def upsert_event(db: sqlite3.Connection, doc: RawDocument, run_id: str, label: dict[str, Any],
                 label_source: str, model_name: str | None = None, prompt_version: str | None = None,
                 max_claims: int = 8) -> str | None:
    if not doc.company_id or label["eventhood"] != "EVENT":
        old_event = db.execute("SELECT event_id FROM event_records WHERE version_id=?", (doc.version_id,)).fetchone()
        if old_event:
            delete_event(db, old_event[0])
        db.execute("DELETE FROM event_records WHERE version_id=?", (doc.version_id,))
        return None
    event_id = hashlib.sha256(f"event\0{doc.version_id}".encode()).hexdigest()
    score, level, action = risk(label)
    breakdown = json.dumps([part.as_dict() for part in risk_parts(label)], ensure_ascii=False)
    # Being AI-labelled is not by itself a defect — flagging on it marked every
    # event and left the queue with no signal in it.  Only low confidence is a
    # cause here; the standardization and claim passes add their own reasons.
    reasons = ["low_confidence"] if label["confidence"] < 0.75 else []
    issue_codes, claims, dropped = scope_issue_codes(doc.company_id, list(label["issue_codes"]),
                                                     list(label.get("claims", [])))
    if dropped:
        reasons.append("issue_out_of_scope")
    needs_review = int(bool(reasons))
    now = utc_now()
    values = (
        event_id, doc.version_id, run_id, doc.company_id,
        json.dumps(label["event_types"], ensure_ascii=False), json.dumps(issue_codes, ensure_ascii=False),
        label["production_mode"], json.dumps(label.get("event_subjects", []), ensure_ascii=False),
        label.get("event_time"), label.get("event_time_precision"),
        json.dumps(label.get("affected_objects", []), ensure_ascii=False), label["impact_direction"],
        label["impact_magnitude"], label["article_sentiment"], label["relevance_score"], label["eventhood"],
        label["summary"], json.dumps(label.get("amounts", []), ensure_ascii=False),
        json.dumps(label.get("ratios", []), ensure_ascii=False), json.dumps(label.get("persons", []), ensure_ascii=False),
        json.dumps(label.get("organizations", []), ensure_ascii=False), json.dumps(label.get("products", []), ensure_ascii=False),
        score, level, action, breakdown, label_source, model_name, prompt_version,
        label["confidence"], needs_review,
        json.dumps(reasons), json.dumps(label, ensure_ascii=False), now, now,
    )
    db.execute("""INSERT INTO event_records
        (event_id,version_id,run_id,company_id,event_types_json,issue_codes_json,production_mode,
         event_subjects_json,event_time,event_time_precision,affected_objects_json,impact_direction,
         impact_magnitude,article_sentiment,relevance_score,eventhood,summary,amounts_json,ratios_json,
         persons_json,organizations_json,products_json,risk_score,risk_level,recommended_action,
         risk_breakdown_json,label_source,
         model_name,prompt_version,extraction_confidence,needs_review,review_reasons_json,raw_label_json,
         created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(version_id) DO UPDATE SET run_id=excluded.run_id,event_types_json=excluded.event_types_json,
         issue_codes_json=excluded.issue_codes_json,production_mode=excluded.production_mode,
         event_subjects_json=excluded.event_subjects_json,event_time=excluded.event_time,
         event_time_precision=excluded.event_time_precision,affected_objects_json=excluded.affected_objects_json,
         impact_direction=excluded.impact_direction,impact_magnitude=excluded.impact_magnitude,
         article_sentiment=excluded.article_sentiment,relevance_score=excluded.relevance_score,
         eventhood=excluded.eventhood,summary=excluded.summary,amounts_json=excluded.amounts_json,
         ratios_json=excluded.ratios_json,persons_json=excluded.persons_json,organizations_json=excluded.organizations_json,
         products_json=excluded.products_json,risk_score=excluded.risk_score,risk_level=excluded.risk_level,
         recommended_action=excluded.recommended_action,risk_breakdown_json=excluded.risk_breakdown_json,
         label_source=excluded.label_source,model_name=excluded.model_name,
         prompt_version=excluded.prompt_version,extraction_confidence=excluded.extraction_confidence,
         needs_review=excluded.needs_review,review_reasons_json=excluded.review_reasons_json,
         raw_label_json=excluded.raw_label_json,updated_at=excluded.updated_at""", values)
    db.execute("DELETE FROM event_claims WHERE event_id=?", (event_id,))
    for ordinal, claim in enumerate(claims[:max_claims], start=1):
        db.execute("""INSERT INTO event_claims
            (claim_id,event_id,ordinal,text,issue_code,claim_type,source_start,source_end,cited_source,confidence)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (hashlib.sha256(f"{event_id}\0{ordinal}".encode()).hexdigest(), event_id, ordinal,
             claim["text"], claim.get("issue_code"), claim.get("claim_type", "fact"),
             int(claim["source_start"]), int(claim["source_end"]), claim.get("cited_source"),
             float(claim.get("confidence", label["confidence"]))))
    return event_id


def build_duplicate_clusters(db: sqlite3.Connection, candidates: list[tuple[RawDocument, str]],
                             *, title_threshold: float = TITLE_MATCH_THRESHOLD,
                             body_threshold: float = BODY_ONLY_THRESHOLD,
                             hamming_max: int = SIMHASH_HAMMING_MAX,
                             bands: int = SIMHASH_BANDS,
                             containment_threshold: float | None = TITLE_CONTAINMENT_THRESHOLD,
                             max_reprint_gap_days: int | None = REPRINT_GAP_DAYS,
                             template_text_bar: float | None = TEMPLATE_TEXT_BAR,
                             template_number_bar: float = TEMPLATE_NUMBER_BAR,
                             template_number_share: float = TEMPLATE_NUMBER_SHARE,
                             tokens_by_version: dict[str, set[str]] | None = None) -> int:
    """Cluster only strong near-duplicates; generic announcement titles cannot join alone.

    The five gate parameters default to the shipped values, so calling this with
    no keywords behaves exactly as it always has.  They are parameters at all so
    that the benchmark harness can sweep them against the labelled duplication
    gold and produce a calibration record instead of an argument.

    Candidates reach the gate down two independent routes: the LSH bands, which
    find near-duplicates whatever their titles, and an exact (company, normalised
    title) bucket, which finds the ones simhash cannot -- a brief and the long
    piece that absorbs it have genuinely different signatures, so no banding or
    hamming ceiling will ever bring them together.  The second route admits on
    containment rather than Jaccard, and only ever on an identical title; passing
    ``containment_threshold=None`` disables it and restores the older behaviour.

    Whichever route admits a candidate, one veto applies last: prose that is
    near-identical while the *numbers* diverge is a template refiled, not a story
    reprinted.  ``template_text_bar=None`` disables that too.

    ``tokens_by_version`` is the same accommodation for cost rather than
    behaviour: tokenising 3,000 documents of CJK bigrams costs seconds per grid
    point and the tokens do not depend on any gate, so a sweep would otherwise
    re-derive an identical answer once per point.  Omitted, the function
    tokenises exactly as before.
    """
    band_width = 64 // bands
    band_mask = (1 << band_width) - 1
    db.execute("DELETE FROM duplicate_cluster_members")
    db.execute("DELETE FROM duplicate_clusters")
    by_company: dict[str, list[tuple[RawDocument, str, set[str], int, set[str]]]] = defaultdict(list)
    for doc, text in candidates:
        if not doc.company_id or len(text) < 100:
            continue
        tokens = (tokens_by_version or {}).get(doc.version_id)
        if tokens is None:
            tokens = tokenize(f"{doc.title or ''}\n{text[:12000]}")
        if len(tokens) >= 8:
            numbers = numeric_tokens(text[:12000]) if template_text_bar is not None else set()
            by_company[doc.company_id].append((doc, text, tokens, simhash(tokens), numbers))
    made = 0
    for company, entries in by_company.items():
        # Keep only the figures that distinguish one filing from another; the
        # ones this company repeats everywhere identify nothing.  Done per
        # company because that is the scope every comparison runs in.
        if template_text_bar is not None:
            seen: Counter = Counter()
            for _, _, _, _, numbers in entries:
                seen.update(numbers)
            ceiling = len(entries) * template_number_share
            # Pruning must never empty a document.  Below a few dozen documents
            # per company every figure looks common and the filter would discard
            # all of them, silently disabling the veto -- and the same is true of
            # a filing whose only numbers are page counts.  ``prune_tokens`` keeps
            # the rarest terms for the same reason.
            entries = [(doc, text, tokens, signature,
                        {number for number in numbers if seen[number] <= ceiling} or numbers)
                       for doc, text, tokens, signature, numbers in entries]
        buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
        by_title: dict[str, list[int]] = defaultdict(list)
        for index, (doc, _, _, signature, _) in enumerate(entries):
            for band in range(bands):
                buckets[(band, (signature >> (band * band_width)) & band_mask)].append(index)
            # The empty key is skipped for cost, not for correctness -- untitled
            # documents would otherwise all pile into one bucket that the guard
            # below never consults.  Correctness is that guard's job.
            if containment_threshold is not None and (key := normalize_title(doc.title)):
                by_title[key].append(index)
        # Earliest publication first, so a cluster is represented by the version
        # everything else plausibly derives from.
        order = sorted(range(len(entries)), key=lambda i: entries[i][0].published_at or "9999")
        assigned: set[int] = set()
        for representative in order:
            if representative in assigned:
                continue
            rep_doc, _, rep_tokens, rep_hash, rep_numbers = entries[representative]
            members = [representative]
            bars_applied: list[float] = []
            considered: set[int] = set()
            # Empty is not a title.  ``normalize_title`` strips 「公告」 and every
            # non-word character, so an untitled filing and one headed 「公告」
            # both reduce to "" -- and the containment rule, which trusts an
            # identical title to carry most of the evidence, must never fire on
            # that.  (The Jaccard route has always given such a pair the looser
            # title bar; it is left as calibrated, since 0.72 token overlap is
            # still substantial evidence on its own.)
            rep_title = normalize_title(rep_doc.title)
            neighbours = [buckets[(band, (rep_hash >> (band * band_width)) & band_mask)]
                          for band in range(bands)]
            if containment_threshold is not None and rep_title:
                neighbours.append(by_title[rep_title])
            for group in neighbours:
                for candidate in group:
                    if candidate == representative or candidate in assigned or candidate in considered:
                        continue
                    considered.add(candidate)
                    doc_c, _, tokens_c, hash_c, numbers_c = entries[candidate]
                    candidate_title = normalize_title(doc_c.title)
                    same_title = rep_title == candidate_title
                    overlap = len(rep_tokens & tokens_c)
                    union_size = len(rep_tokens | tokens_c)
                    similarity = overlap / union_size if union_size else 0.0
                    # Exact title needs a lower bar, but still requires substantial text overlap.
                    threshold = title_threshold if same_title else body_threshold
                    # The veto, applied to whichever route admits: near-identical
                    # prose whose numbers diverge is one template refiled, not one
                    # story reprinted.  It sits before both admissions rather than
                    # inside either, because the shape it rejects arrives down both.
                    if template_text_bar is not None and similarity >= template_text_bar:
                        shared = rep_numbers | numbers_c
                        if shared and len(rep_numbers & numbers_c) / len(shared) < template_number_bar:
                            continue
                    # Every member is compared against the representative rather
                    # than against whichever neighbour it happened to match.
                    # Chaining A~B~C previously merged documents that share only
                    # boilerplate and are about entirely different events.
                    if hamming(rep_hash, hash_c) <= hamming_max and similarity >= threshold:
                        members.append(candidate)
                        bars_applied.append(threshold)
                        continue
                    # The containment route.  It ignores the hamming ceiling on
                    # purpose: the pairs it exists for are the ones whose
                    # signatures are far apart *because* one side is many times
                    # longer, which is the same fact that sinks their Jaccard.
                    if not (same_title and rep_title) or containment_threshold is None:
                        continue
                    if max_reprint_gap_days is not None and not within_days(
                            rep_doc.published_at, doc_c.published_at, max_reprint_gap_days):
                        continue
                    smaller = min(len(rep_tokens), len(tokens_c))
                    if smaller and overlap / smaller >= containment_threshold:
                        members.append(candidate)
                        bars_applied.append(containment_threshold)
            if len(members) < 2:
                continue
            assigned.update(members)
            cluster_id = hashlib.sha256(f"near\0{company}\0{rep_doc.version_id}".encode()).hexdigest()
            similarities = []
            for index in members:
                tokens = entries[index][2]
                rep_tokens = entries[representative][2]
                similarities.append(len(tokens & rep_tokens) / len(tokens | rep_tokens))
            db.execute("""INSERT INTO duplicate_clusters
                (cluster_id,company_id,cluster_kind,representative_version_id,member_count,similarity_threshold,created_at)
                VALUES(?,?,?,?,?,?,?)""", (cluster_id, company, "near_duplicate", rep_doc.version_id,
                                                len(members), min(bars_applied), utc_now()))
            for index, similarity in zip(members, similarities):
                version_id = entries[index][0].version_id
                db.execute("INSERT INTO duplicate_cluster_members(cluster_id,version_id,similarity) VALUES(?,?,?)",
                           (cluster_id, version_id, round(similarity, 4)))
                db.execute("UPDATE processed_documents SET near_duplicate_cluster_id=? WHERE version_id=?",
                           (cluster_id, version_id))
            made += 1
    return made


def originating_sources(db: sqlite3.Connection) -> dict[str, str]:
    """Map every clustered document to the source its cluster representative came from.

    A near-duplicate cluster is one story carried by many outlets.  Counting each
    carrier as its own source is what let a 转载 chain manufacture corroboration,
    so every member reports the representative's source and the reprints collapse
    to one.
    """
    mapping: dict[str, str] = {}
    for cluster_id, source_id in db.execute("""SELECT c.cluster_id,v.source_id
            FROM duplicate_clusters c JOIN document_versions v ON v.version_id=c.representative_version_id"""):
        for (version_id,) in db.execute("SELECT version_id FROM duplicate_cluster_members WHERE cluster_id=?", (cluster_id,)):
            mapping[version_id] = source_id
    return mapping


def event_time_bounds(event_time: str | None) -> tuple[str, str] | None:
    """Split a normalized ``event_time`` into its first and last day."""
    value = (event_time or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(/\d{4}-\d{2}-\d{2})?", value):
        return None
    return value[:10], value[-10:]


#: How far past its publication day an event time may start and still count as
#: having happened.  Publication times are stored in UTC, so a report written on
#: the evening of the 10th in Beijing can carry the 9th.
SCHEDULED_SLACK_DAYS = 1


def event_time_role(event_time: str | None, published_at: str | None) -> str | None:
    """Whether an event time had happened when the report was written.

    The labeller is asked for when the event happened and copies whatever date
    the article gives — and for 「将于 9 月 29 日召开股东大会」, a 2032 contract
    deadline or a 2027 production target, that date is in the article's future
    (I-04).  Such a date is a plan, a deadline or a schedule: ``scheduled``.  A
    range that spans the publication day is ``occurred``.  Telling a deadline
    from an effective date from a target needs the sentence; telling any of them
    from something that already happened needs only the two dates.
    """
    bounds = event_time_bounds(event_time)
    if bounds is None:
        return None
    try:
        published = date.fromisoformat(str(published_at or "")[:10])
        start = date.fromisoformat(bounds[0])
    except ValueError:
        return "occurred"
    return "scheduled" if (start - published).days > SCHEDULED_SLACK_DAYS else "occurred"


def issue_adjustments(db: sqlite3.Connection) -> dict[tuple[str, str], str | None]:
    """Human reassignments of an event's issue code, keyed by (event, old code).

    Written by ``api.issue_db`` and read here, because this function deletes the
    ledger and builds it again on every run: a correction stored in the ledger
    itself would live until the next pipeline run and then vanish.  A value of
    ``None`` means the event was taken out of the ledger rather than moved.

    Read defensively — an installation that has never corrected anything has no
    such table, and that is a state rather than a fault.
    """
    if not table_exists(db, "issue_adjustments"):
        return {}
    return {(row[0], row[1]): row[2] for row in db.execute(
        """SELECT event_id, from_code, to_code FROM issue_adjustments
           WHERE undone_at IS NULL ORDER BY decided_at""")}


def rebuild_issue_ledger(db: sqlite3.Connection) -> int:
    db.execute("DELETE FROM issue_ledger")
    # 「事件发生区间」只收已发生的事件时间：计划/截止日期（I-04）会把一个专题的区间
    # 拉到 2032 年。没有 event_time_role 列的旧库按全部已发生读。
    role = "e.event_time_role" if column_exists(db, "event_records", "event_time_role") else "NULL"
    rows = db.execute(f"""SELECT e.company_id,e.issue_codes_json,e.risk_score,
               CASE WHEN {role}='scheduled' THEN NULL ELSE e.event_time END,e.version_id,
               v.published_at,v.source_id,v.source_tier,e.event_id,e.event_types_json
        FROM event_records e JOIN document_versions v ON v.version_id=e.version_id
        WHERE e.eventhood='EVENT'""").fetchall()
    adjustments = issue_adjustments(db)
    representative = originating_sources(db)
    # Exact duplicates never get an event of their own, so they are counted here
    # to keep 报道数 a count of documents rather than a second copy of 事件数.
    reprints: Counter[str] = Counter()
    for (target,) in db.execute("SELECT exact_duplicate_of FROM processed_documents WHERE exact_duplicate_of IS NOT NULL"):
        reprints[target] += 1
    buckets: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    corrected: Counter[tuple[str, str]] = Counter()
    for row in rows:
        # A set, because two codes on one event can be reassigned to the same
        # target and the event must not then be counted twice in it.
        targets: dict[str, bool] = {}
        for issue in json.loads(row[1]):
            key = (row[8], issue)
            target = adjustments.get(key, issue) if key in adjustments else issue
            if target is None:
                continue
            targets[target] = targets.get(target, False) or key in adjustments
        for target, by_hand in targets.items():
            buckets[(row[0], target)].append(row)
            if by_hand:
                corrected[(row[0], target)] += 1
    for (company, issue), values in buckets.items():
        bounds = [event_time_bounds(value[3]) for value in values]
        occurred = [pair for pair in bounds if pair]
        # 首现时间 stays "first reported".  Where a version has no publication
        # date, its event time stands in rather than dropping the event out of
        # the range silently.
        reported = sorted(str(value[5]) if value[5] else pair[0]
                          for value, pair in zip(values, bounds) if value[5] or pair)
        independent = {representative.get(value[4], value[6]) for value in values if value[7] in {"S0", "S1", "S2"}}
        documents = len(values) + sum(reprints.get(value[4], 0) for value in values)
        # 风险均值只取承载风险的成员，计数、首现时间与独立信源仍取全部：一条成交额快讯
        # 确实是这个议题的一次报道（报道数该算它），但它不是这个议题的一份风险
        # （均值不该被它拉平）。见 risk_bearing_clause 的注释。
        bearing = [value for value in values if json.loads(value[9] or "[]")]
        average = (round(sum(float(value[2]) for value in bearing) / len(bearing), 1)
                   if bearing else 0.0)
        level = "high" if average >= 75 else "medium" if average >= 45 else "low"
        issue_id = hashlib.sha256(f"issue\0{company}\0{issue}".encode()).hexdigest()
        db.execute("""INSERT INTO issue_ledger
            (issue_id,company_id,issue_code,first_seen_at,last_seen_at,event_count,document_count,
             independent_source_count,risk_score,risk_level,status,updated_at,
             event_time_earliest,event_time_latest,risk_bearing_event_count)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (issue_id, company, issue, reported[0] if reported else None, reported[-1] if reported else None,
             len(values), documents, len(independent), average, level, "open", utc_now(),
             min(bounds[0] for bounds in occurred) if occurred else None,
             max(bounds[1] for bounds in occurred) if occurred else None, len(bearing)))
        # How much of this issue a person placed here. An issue assembled by
        # hand and one the pipeline produced look identical in a count, and the
        # difference is exactly what a reader of the ledger needs.
        db.execute("UPDATE issue_ledger SET manual_event_count=? WHERE issue_id=?",
                   (corrected.get((company, issue), 0), issue_id))
    return len(buckets)


def export_data(db: sqlite3.Connection) -> None:
    EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
    exports = {
        "events.jsonl": "SELECT e.*,v.title,v.published_at,v.canonical_url,v.source_id,v.source_tier FROM event_records e JOIN document_versions v ON v.version_id=e.version_id ORDER BY e.risk_score DESC,e.updated_at DESC",
        "issues.jsonl": "SELECT * FROM issue_ledger ORDER BY risk_score DESC,last_seen_at DESC",
        "processed_documents.jsonl": "SELECT * FROM processed_documents ORDER BY processed_at DESC",
        "claims.jsonl": "SELECT c.*,e.version_id,e.company_id,e.risk_level,v.title,v.published_at,v.canonical_url,v.source_id,v.source_tier FROM event_claims c JOIN event_records e ON e.event_id=c.event_id JOIN document_versions v ON v.version_id=e.version_id ORDER BY e.risk_score DESC,e.event_id,c.ordinal",
        "duplicate_members.jsonl": "SELECT c.cluster_id,c.company_id,c.cluster_kind,c.representative_version_id,c.member_count,m.version_id,m.similarity,v.title,v.published_at,v.source_id,v.canonical_url FROM duplicate_clusters c JOIN duplicate_cluster_members m ON m.cluster_id=c.cluster_id JOIN document_versions v ON v.version_id=m.version_id ORDER BY c.company_id,c.cluster_id,m.similarity DESC",
    }
    # Written by the analysis stage, which runs after the pipeline; a database
    # that has not reached that stage yet exports what it has instead of failing.
    for filename, (table, query) in {
        "propagation_edges.jsonl": ("propagation_edges", "SELECT e.*,fv.title AS from_title,fv.source_id AS from_source,tv.title AS to_title,tv.source_id AS to_source FROM propagation_edges e LEFT JOIN document_versions fv ON fv.version_id=e.from_version_id LEFT JOIN document_versions tv ON tv.version_id=e.to_version_id ORDER BY e.company_id,e.edge_kind,e.to_version_id"),
        "timeline.jsonl": ("event_timeline", "SELECT t.*,e.summary,v.title,v.canonical_url,v.source_id FROM event_timeline t JOIN event_records e ON e.event_id=t.event_id JOIN document_versions v ON v.version_id=t.version_id ORDER BY t.company_id,t.timeline_at"),
        "alerts.jsonl": ("alerts", "SELECT * FROM alerts ORDER BY adjusted_risk_score DESC,window_start DESC"),
    }.items():
        if db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            exports[filename] = query
    for filename, query in exports.items():
        cursor = db.execute(query)
        rows = cursor.fetchall()
        columns = [item[0] for item in cursor.description]
        with (EXPORT_ROOT / filename).open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(dict(zip(columns, row)), ensure_ascii=False) + "\n")
    for filename, query in {
        "events.csv": exports["events.jsonl"],
        "issues.csv": exports["issues.jsonl"],
    }.items():
        cursor = db.execute(query)
        rows = cursor.fetchall()
        columns = [item[0] for item in cursor.description]
        with (EXPORT_ROOT / filename).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            writer.writerows(rows)
    metrics = {
        "generated_at": utc_now(),
        "processed_documents": db.execute("SELECT count(*) FROM processed_documents").fetchone()[0],
        "events": db.execute("SELECT count(*) FROM event_records").fetchone()[0],
        "issues": db.execute("SELECT count(*) FROM issue_ledger").fetchone()[0],
        "claims": db.execute("SELECT count(*) FROM event_claims").fetchone()[0],
        "duplicate_clusters": db.execute("SELECT count(*) FROM duplicate_clusters").fetchone()[0],
        "ai_processed_documents": db.execute("SELECT count(*) FROM processed_documents WHERE run_id LIKE 'ai-%'").fetchone()[0],
        "events_without_claims": db.execute("""SELECT count(*) FROM event_records e
            LEFT JOIN event_claims c ON c.event_id=e.event_id WHERE c.claim_id IS NULL""").fetchone()[0],
        "events_needing_review": db.execute("SELECT count(*) FROM event_records WHERE needs_review=1").fetchone()[0],
        # Broken out because "needs review" alone says nothing about what to do
        # with the event, and the 核验 views need to exclude no_claims events.
        "events_by_review_reason": dict(Counter(
            reason for (raw,) in db.execute("SELECT review_reasons_json FROM event_records")
            for reason in json.loads(raw or "[]")).most_common()),
        "events_with_comparable_event_time": db.execute("""SELECT count(*) FROM event_records
            WHERE event_time GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'""").fetchone()[0],
        "by_eventhood": dict(db.execute("SELECT eventhood,count(*) FROM processed_documents GROUP BY eventhood").fetchall()),
        "by_risk": dict(db.execute("SELECT risk_level,count(*) FROM event_records GROUP BY risk_level").fetchall()),
    }
    for name, table, query in (
        ("propagation_edges", "propagation_edges", "SELECT count(*) FROM propagation_edges"),
        ("propagation_components", "propagation_components", "SELECT count(*) FROM propagation_components"),
        ("alerts", "alerts", "SELECT count(*) FROM alerts"),
        ("timeline_time_basis", "event_timeline", "SELECT time_basis,count(*) FROM event_timeline GROUP BY 1"),
    ):
        if db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            rows = db.execute(query).fetchall()
            metrics[name] = dict(rows) if len(rows[0]) > 1 else rows[0][0]
    (EXPORT_ROOT / "quality_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    company_rows = db.execute("""SELECT company_id,count(*) events,
        sum(CASE WHEN risk_level='high' THEN 1 ELSE 0 END) high_risk,
        round(avg(risk_score),1) average_risk,
        sum(CASE WHEN label_source='ai' THEN 1 ELSE 0 END) ai_events
        FROM event_records GROUP BY company_id ORDER BY company_id""").fetchall()
    issue_rows = db.execute("""SELECT company_id,issue_code,event_count,independent_source_count,risk_score,risk_level
        FROM issue_ledger ORDER BY risk_score DESC,event_count DESC LIMIT 20""").fetchall()
    lines = ["# 数据处理结果汇总", "", f"生成时间：{metrics['generated_at']}", "",
             f"处理文档：{metrics['processed_documents']}；事件：{metrics['events']}；主张：{metrics['claims']}；议题：{metrics['issues']}；近重复簇：{metrics['duplicate_clusters']}。", "",
             f"待人工复核事件：{metrics['events_needing_review']}；其中无可定位原子主张：{metrics['events_without_claims']}。", "",
             "## 公司事件概况", "", "| 公司 | 事件数 | 高风险 | 平均风险分 | AI 事件 |", "|---|---:|---:|---:|---:|"]
    lines.extend(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]} |" for row in company_rows)
    lines.extend(["", "## 优先议题", "", "| 公司 | 议题码 | 事件数 | 独立信源 | 风险分 | 等级 |", "|---|---|---:|---:|---:|---|"])
    lines.extend(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]} | {row[5]} |" for row in issue_rows)
    lines.extend(["", "> 风险等级只用于内部排序，模型结果默认需要人工复核，不构成监管、司法或投资结论。", ""])
    (EXPORT_ROOT / "processing_summary.md").write_text("\n".join(lines), encoding="utf-8")


def listing_root(url: str) -> str:
    """The URL with its page number removed -- page 1 of whatever this lists."""
    parts = urlsplit(url or "")
    kept = [(key, value) for key, value in parse_qsl(parts.query)
            if not PAGINATION_PARAM.match(key)]
    query = "&".join(f"{key}={value}" for key, value in kept)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), query, ""))


def is_paginated(url: str) -> bool:
    return any(PAGINATION_PARAM.match(key) and value.isdigit()
               for key, value in parse_qsl(urlsplit(url or "").query))


def listing_roots(db: sqlite3.Connection) -> set[str]:
    """Every URL in the corpus that some page number was appended to.

    ``BODY_TEXT_FLOOR`` was built to stop exactly this -- its comment says
    「labelling chrome invents events out of headlines」 -- but it only catches
    a page whose body never arrived.  A news index arrives complete, with a
    thousand characters of real headlines, and sails over a floor of 200.
    96 of them reached the labelling pool and 85 produced events, one of them
    an event dated 2015 assembled out of a page-32 listing.

    Length cannot separate the two, and the source cannot either -- these
    arrived typed ``announcements`` and ``company_news`` like the articles
    beside them.  The URL can: a site that serves ``…/news?page=2`` is
    serving a list, and ``…/news`` is page one of that same list.  Deriving
    the roots from the corpus keeps the rule out of the source configuration,
    where it would have to be maintained per site forever.

    Corroborated by a signal the rule does not look at: **not one** of the 96
    carries a publication date, and every dated page is left alone.  A list
    has no publication because it is not a publication.
    """
    return {listing_root(row[0]) for row in
            db.execute("SELECT canonical_url FROM document_versions WHERE canonical_url IS NOT NULL")
            if is_paginated(row[0])}


def run(companies: set[str] | None = None, *, force: bool = False) -> dict[str, int]:
    db = connect()
    ensure_schema(db)
    docs = load_documents(db, companies)
    indexes = listing_roots(db)
    run_id = hashlib.sha256(f"{PROCESSOR_VERSION}\0{utc_now()}".encode()).hexdigest()[:24]
    options = {"companies": sorted(companies) if companies else "all", "force": force}
    db.execute("INSERT INTO processing_runs(run_id,started_at,status,processor_version,options_json,documents_seen) VALUES(?,?,?,?,?,?)",
               (run_id, utc_now(), "running", PROCESSOR_VERSION, json.dumps(options), len(docs)))
    db.commit()
    text_by_version: dict[str, str] = {}
    first_by_hash: dict[str, str] = {}
    processed = 0
    try:
        # Pass 1 parses raw evidence.  Nothing is written yet: a source's chrome
        # can only be recognised by comparing its pages against each other.
        parsed: list[tuple[RawDocument, str, int | None, str, str]] = []
        for doc in docs:
            existing = db.execute("SELECT processor_version FROM processed_documents WHERE version_id=?", (doc.version_id,)).fetchone()
            if existing and existing[0] == PROCESSOR_VERSION and not force:
                text_path = text_file_path(doc.content_hash)
                text_by_version[doc.version_id] = text_path.read_text(encoding="utf-8") if text_path.exists() else ""
                continue
            parsed.append((doc, *extract_text(ROOT / doc.raw_path)))
        boilerplate = resolve_boilerplate(db, parsed)

        # Pass 2 strips that chrome, then labels and stores what remains.
        for index, (doc, raw_text, pages, status, media) in enumerate(parsed, start=1):
            duplicate_of = first_by_hash.setdefault(doc.content_hash, doc.version_id)
            duplicate_of = None if duplicate_of == doc.version_id else duplicate_of
            text = strip_boilerplate(raw_text, boilerplate.get(doc.source_id, set())) if media == "html" else raw_text
            if media == "html" and status == "ok" and len(text) < BODY_TEXT_FLOOR:
                # The page was retrieved, but its article body never arrived —
                # typically rendered client-side.  Keep the evidence, refuse the
                # content: labelling chrome invents events out of headlines.
                status = "body_missing"
            if doc.document_type in EXCLUDED_DOCUMENT_TYPES:
                inclusion = "excluded_document_type"
            elif is_paginated(doc.canonical_url) or listing_root(doc.canonical_url) in indexes:
                # A separate bar on the funnel rather than a quiet retype:
                # a reader should be able to see how many index pages the
                # gate turned away, not have them folded into the type rule.
                inclusion = "excluded_listing_page"
            elif status == "body_missing":
                inclusion = "excluded_no_body"
            else:
                inclusion = "included"
            path: Path | None = None
            if text:
                path = text_file_path(doc.content_hash)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            label = rule_label(doc, text) if inclusion == "included" else {
                "relevance_score": 0.0, "eventhood": "IRRELEVANT", "reasons": [inclusion],
                "event_types": [], "issue_codes": [], "production_mode": "BRIEF", "summary": "",
                "claims": [], "confidence": 1.0,
            }
            persist_processed(db, doc, run_id, path, text, pages, status, inclusion, label, duplicate_of)
            upsert_event(db, doc, run_id, label, "rule")
            text_by_version[doc.version_id] = text
            processed += 1
            if index % 25 == 0:
                db.commit()
        cluster_count = build_duplicate_clusters(db, [(doc, text_by_version.get(doc.version_id, "")) for doc in docs])
        issue_count = rebuild_issue_ledger(db)
        export_data(db)
        db.execute("UPDATE processing_runs SET completed_at=?,status='completed',documents_processed=? WHERE run_id=?",
                   (utc_now(), processed, run_id))
        db.commit()
        summary = {"run_id": run_id, "documents_seen": len(docs), "documents_processed": processed,
                   "clusters": cluster_count, "issues": issue_count,
                   "events": db.execute("SELECT count(*) FROM event_records").fetchone()[0]}
        audit({"source_id": "system", "outcome": "event_processing_completed", **summary})
        return summary
    except Exception as exc:
        db.execute("UPDATE processing_runs SET completed_at=?,status='failed',error_message=? WHERE run_id=?",
                   (utc_now(), str(exc)[:1000], run_id))
        db.commit()
        raise
    finally:
        db.close()


def sweep_orphans(db: sqlite3.Connection) -> int:
    """Remove dependent rows whose event or claim is already gone.

    ``delete_event`` keeps the cascade honest from here on, but rows orphaned
    before it existed are still in the database, and no query joins them away:
    the timeline and sentiment tables are read by event id, so an orphan is
    invisible until it is counted as a denominator.
    """
    removed = 0
    for table in EVENT_DEPENDENTS:
        if not table_exists(db, table):
            continue
        removed += db.execute(f"""DELETE FROM {table} WHERE event_id NOT IN
                                  (SELECT event_id FROM event_records)""").rowcount
    for table in CLAIM_DEPENDENTS:
        if not table_exists(db, table):
            continue
        removed += db.execute(f"""DELETE FROM {table} WHERE claim_id NOT IN
                                  (SELECT claim_id FROM event_claims)""").rowcount
    return removed


def reclassify_listings(db: sqlite3.Connection) -> dict[str, int]:
    """Apply the listing-page gate to documents already processed.

    ``run(force=True)`` would do this too, and would also relabel the whole
    corpus from the rule layer: ``upsert_event`` writes
    ``label_source='rule'`` through an unconditional ``ON CONFLICT DO
    UPDATE``, so a forced pass silently replaces every AI label, every claim
    hanging off it, and everything downstream of those.  A gate that was
    wrong about 96 documents is not worth paying for the other 2,803 twice
    and losing their labels in the bargain.

    So this walks the same rule over the stored rows and changes only what
    the rule newly excludes.  Events built from an index page go with it:
    they were assembled out of a table of contents, they carry no
    publication date, and most of them restate an article that was collected
    separately and correctly.
    """
    indexes = listing_roots(db)
    rows = db.execute("""SELECT p.version_id, v.canonical_url FROM processed_documents p
                         JOIN document_versions v USING(version_id)
                         WHERE p.inclusion_status='included'""").fetchall()
    targets = [row[0] for row in rows
               if is_paginated(row[1]) or listing_root(row[1]) in indexes]
    counts = {"examined": len(rows), "excluded": len(targets), "events_removed": 0,
              "claims_removed": 0, "orphans_removed": 0}
    for version_id in targets:
        event = db.execute("SELECT event_id FROM event_records WHERE version_id=?",
                           (version_id,)).fetchone()
        if event:
            removed = delete_event(db, event[0])
            counts["claims_removed"] += removed["event_claims"]
            counts["events_removed"] += removed["event_records"]
            for table in EVENT_DEPENDENTS + CLAIM_DEPENDENTS:
                counts[table] = counts.get(table, 0) + removed.get(table, 0)
        db.execute("""UPDATE processed_documents
                      SET inclusion_status='excluded_listing_page', eventhood='IRRELEVANT',
                          relevance_score=0, rejection_reasons_json=?
                      WHERE version_id=?""",
                   (json.dumps(["excluded_listing_page"]), version_id))
    counts["orphans_removed"] = sweep_orphans(db)
    db.commit()
    audit({"source_id": "system", "outcome": "listing_pages_excluded", **counts})
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline-first event processing over preserved raw evidence")
    parser.add_argument("--company", action="append", choices=configured_companies(),
                        help="limit to a company; repeatable (default: all linked companies)")
    parser.add_argument("--force", action="store_true", help="re-extract even if this processor version has run")
    parser.add_argument("--reclassify-listings", action="store_true",
                        help="apply the index-page gate to already-processed documents, "
                             "without relabelling anything else")
    args = parser.parse_args()
    if args.reclassify_listings:
        db = connect()
        ensure_schema(db)
        print(json.dumps(reclassify_listings(db), ensure_ascii=False))
        db.close()
        return
    summary = run(set(args.company) if args.company else None, force=args.force)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
