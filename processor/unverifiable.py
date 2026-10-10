#!/usr/bin/env python3
"""把「无法核验」拆开：哪些是设计边界，哪些才是可以改进的。

``UNVERIFIABLE`` 占全部裁决的约三分之一，是系统最大的单一状态。它此前只有一个名字，
于是读者看到的是一个难看的比例，而不是它的构成。2026-09-23 按下面五条规则拆开后：

* 41% **出自权威披露本身**——主张是从交易所或公司的正式文件里抽出来的，它本身就是
  证据。再拿另一份披露去「旁证」它是循环论证，这类主张不是核验的对象；
* 17% **行情数据**——单日成交、涨跌、大宗交易明细，事件类型层已判为「一类都不贴合」；
* 9% **披露范围之外**——股价市值、指数与评级、境外政府动作，发行人的披露不覆盖；
* 27% **披露未提及此细节**——检索到相关披露段落，模型判它们既不支持也不否定；
* 6% **披露中未检索到**——证据库里一段相关的都找不到。

前三类是设计边界，后两类才是补料、改检索能够移动的部分。

规则**按顺序**判，第一条命中即定；每条都把命中依据写进 ``unverifiable_basis``，
于是读者可以不同意某一条规则，而不必不同意整个分类。全部由已有字段推出，不调模型，
所以可以在任何时候对全库重算，结果只取决于当时的裁决、事件与来源。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from typing import Any

# 这个模块会被 Web 进程引用（api/labels.py 取子类标签），所以顶层只放纯规则。
# claim_verdict 会把模型客户端一起拉进来——Web 进程不许加载它（test_api 的
# WebProcessBoundaryTests 钉着），因此写库用到的那几个在函数里再 import。

#: 子类，顺序即判定顺序。``group`` 是它回答的那个问题：这一条是设计边界，还是可以改进。
SUBTYPES: dict[str, dict[str, str]] = {
    "primary_disclosure": {
        "label": "出自权威披露", "group": "boundary",
        "detail": "主张本身出自交易所或公司正式文件，它就是证据，不需要另一份披露来旁证"},
    "market_data": {
        "label": "行情数据", "group": "boundary",
        "detail": "单日成交、涨跌、大宗交易等二级市场行情，披露文件不记录"},
    "outside_disclosure": {
        "label": "披露范围之外", "group": "boundary",
        "detail": "股价市值、指数与评级、境外政府动作等，发行人的披露不覆盖"},
    "no_corpus": {
        "label": "未采集权威语料", "group": "improvable",
        "detail": "该主体还没有可检索的披露语料，补采即可核验"},
    "not_specific": {
        "label": "披露未提及此细节", "group": "improvable",
        "detail": "检索到相关披露段落，但它们既不支持也不否定这一细节"},
    "not_found": {
        "label": "披露中未检索到", "group": "improvable",
        "detail": "证据库里检索不到任何相关段落"},
}
GROUPS = {"boundary": "设计边界", "improvable": "可改进"}

AUTHORITATIVE_TIERS = frozenset({"S0", "S1"})

#: 「披露范围之外」的三组标记词。来自 2026-09-03 的富集检验（平台深化路线 §3.2.1）：
#: 这几类词在无法核验中的命中率是在已核实主张中的数倍。故意只收**说的是市场或第三方**
#: 的词：「回购」「分红」这类公司自己会披露的动作不在里面。
#:
#: 每组都带英文说法：语料里有一批英文媒体稿（「…market cap of $524 billion…」），只收中文词
#: 会把它们漏进「可改进」。英文按整词匹配、不分大小写，免得「US」命中「USD」。
MARKERS: dict[str, str] = {
    "二级市场": r"股价|市值|成交额|成交量|换手率|涨停|跌停|涨超|跌超|涨幅|跌幅|收盘|盘中|开盘"
                r"|主力资金|净流入|净流出|融资余额|龙虎榜|大宗交易|ETF|北向资金|港股通"
                r"|\b(?:market (?:cap|capitali[sz]ation|value)|share price|stock price|shares? (?:rose|fell|jumped|slumped|surged|dropped)"
                r"|trading volume|closing price)\b",
    "指数与评级": r"纳入.{0,8}指数|指数.{0,4}(?:纳入|调入|调出)|评级|目标价|研报"
                  r"|券商.{0,4}(?:预计|预测|认为)"
                  r"|\b(?:price target|analysts? (?:expect|estimate|forecast)|(?:upgrade|downgrade)d?|index inclusion)\b",
    "境外政府与地缘": r"美国|五角大楼|国防部|商务部|白宫|国会|议员|法案|实体清单|制裁|1260H|欧盟"
                      r"|\b(?:U\.S\.|United States|Pentagon|Department of Defense|Commerce Department|White House"
                      r"|Congress(?:ional)?|lawmakers?|senators?|BIOSECURE|Entity List|sanctions?|Washington)\b",
}
_COMPILED = {name: re.compile(pattern, re.IGNORECASE) for name, pattern in MARKERS.items()}


def classify(row: dict[str, Any] | sqlite3.Row) -> tuple[str, str]:
    """一条无法核验的主张属于哪一类，以及凭什么。

    Args:
        row: 至少含 ``source_tier``（主张所在文档的来源层级）、``event_types_json``、
            ``text``（主张原文）、``method``（裁决方式）。

    Returns:
        ``(子类代码, 依据)``。依据是一句读者看得懂的话，写明命中了哪条规则的什么。
    """
    tier = row["source_tier"]
    if tier in AUTHORITATIVE_TIERS:
        return "primary_disclosure", f"主张出自 {tier} 级来源"
    if (row["event_types_json"] or "[]") in ("[]", ""):
        return "market_data", "所属事件被事件类型层判为纯行情数据"
    text = row["text"] or ""
    for name, pattern in _COMPILED.items():
        found = pattern.search(text)
        if found:
            return "outside_disclosure", f"命中{name}标记词「{found.group(0)}」"
    if row["method"] == "no_corpus":
        return "no_corpus", "该主体没有披露语料"
    if row["method"] == "no_evidence":
        return "not_found", "检索不到任何相关披露段落"
    return "not_specific", "检索到相关段落，模型判为既不支持也不否定"


def ensure_schema(db: sqlite3.Connection) -> None:
    """两列补在 ``claim_verdicts`` 上，与 ``partial_subtype`` 并排。"""
    from processor.claim_verdict import ensure_schema as ensure_verdict_schema

    ensure_verdict_schema(db)
    present = {row[1] for row in db.execute("PRAGMA table_info(claim_verdicts)")}
    for column in ("unverifiable_subtype", "unverifiable_basis"):
        if column not in present:
            db.execute(f"ALTER TABLE claim_verdicts ADD COLUMN {column} TEXT")


def backfill(db: sqlite3.Connection, *, dry_run: bool = False) -> dict[str, Any]:
    """对全库重算子类。幂等：同样的输入第二次运行改动 0 行。

    裁决改成别的值之后，旧的子类要清掉——否则一条后来被核实的主张还挂着
    「披露中未检索到」。
    """
    if not dry_run:
        ensure_schema(db)
    # dry-run 一个字节都不写，连补列也不做；列还不存在时按空值读。
    present = {row[1] for row in db.execute("PRAGMA table_info(claim_verdicts)")}
    stored = ("w.unverifiable_subtype, w.unverifiable_basis" if "unverifiable_subtype" in present
              else "NULL AS unverifiable_subtype, NULL AS unverifiable_basis")
    # 自己用 Row 游标：finalize 传进来的连接没有设 row_factory，不该要求调用方去设。
    cursor = db.cursor()
    cursor.row_factory = sqlite3.Row
    rows = cursor.execute(f"""SELECT w.claim_id, w.method, {stored},
                                c.text, e.event_types_json, v.source_tier
        FROM claim_verdicts w
        JOIN event_claims c ON c.claim_id=w.claim_id
        JOIN event_records e ON e.event_id=c.event_id
        JOIN document_versions v ON v.version_id=e.version_id
        WHERE w.verdict='UNVERIFIABLE'""").fetchall()
    counts: Counter[str] = Counter()
    changes = []
    for row in rows:
        subtype, basis = classify(row)
        counts[subtype] += 1
        if (row["unverifiable_subtype"], row["unverifiable_basis"]) != (subtype, basis):
            changes.append((subtype, basis, row["claim_id"]))
    stale = db.execute("""SELECT count(*) FROM claim_verdicts
        WHERE verdict<>'UNVERIFIABLE' AND unverifiable_subtype IS NOT NULL""").fetchone()[0] \
        if "unverifiable_subtype" in present else 0
    if not dry_run:
        db.executemany("""UPDATE claim_verdicts SET unverifiable_subtype=?, unverifiable_basis=?
            WHERE claim_id=?""", changes)
        db.execute("""UPDATE claim_verdicts SET unverifiable_subtype=NULL, unverifiable_basis=NULL
            WHERE verdict<>'UNVERIFIABLE' AND unverifiable_subtype IS NOT NULL""")
        db.commit()
    groups: Counter[str] = Counter()
    for subtype, count in counts.items():
        groups[SUBTYPES[subtype]["group"]] += count
    return {"unverifiable": len(rows), "changed": len(changes), "cleared": stale,
            "subtypes": dict(counts), "groups": dict(groups), "dry_run": dry_run}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = parser.parse_args()
    from collector.collect import audit, connect

    db = connect()
    try:
        result = backfill(db, dry_run=args.dry_run)
    finally:
        db.close()
    if not args.dry_run:
        audit({"source_id": "system", "outcome": "unverifiable_subtypes_backfilled",
               "changed": result["changed"], "unverifiable": result["unverifiable"]})
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
