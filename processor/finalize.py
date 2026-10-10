#!/usr/bin/env python3
"""Finalize derived ledgers and exports after all model writers have stopped."""
from __future__ import annotations

import json

from collector.collect import audit, connect
from processor import alerts, disclosure_phase, issue_scope, propagation, timeline, titles, unverifiable
from processor.normalize import run as normalize_elements
from processor.pipeline import export_data, rebuild_issue_ledger
from processor.repair_claims import repair


def main() -> None:
    claim_result = repair()
    # Labelling stores raw mentions; standardization must follow it so
    # ``event_time`` and the element ledger hold comparable values.
    element_result = normalize_elements()
    db = connect()
    # 标题在一切写快照的步骤之前修好：预警把引用文件的标题存进 evidence_json，
    # 修晚了快照里还是「China」和「H股公告」（I-05）。
    title_result = titles.repair(db)
    # 行业专属的专题码在专题台账重建之前剔除，否则「药物研发｜长鑫科技」这一行会被
    # 重新算出来（I-03）。
    scope_result = issue_scope.backfill(db)
    issue_count = rebuild_issue_ledger(db)
    # 无法核验的子类由裁决、事件类型与来源层级推出，任何一样变了都要重算；放在这里，
    # 是因为 finalize 是所有模型写入停下之后的那一步。
    unverifiable_result = unverifiable.backfill(db)
    # The analysis stage runs strictly in this order: phases date the calendar
    # the timeline scores against, propagation supplies the independence the
    # ledger and alerts report, and alerts read what the timeline decided.
    phase_result = disclosure_phase.build(db)
    propagation_result = propagation.build(db)
    timeline_result = timeline.build(db)
    alert_result = alerts.build(db)
    export_data(db)
    counts = {
        "events": db.execute("SELECT count(*) FROM event_records").fetchone()[0],
        "claims": db.execute("SELECT count(*) FROM event_claims").fetchone()[0],
        "issues": issue_count,
        "clusters": db.execute("SELECT count(*) FROM duplicate_clusters").fetchone()[0],
    }
    db.commit()
    db.close()
    summary = {"claim_repair": claim_result, "elements": element_result, **counts,
               "issue_scope": {key: scope_result[key] for key in ("events", "claims")},
               "titles": title_result["by_basis"],
               "disclosure_phases": phase_result["phases"],
               "propagation": {key: propagation_result[key] for key in ("edges", "components")},
               "timeline": {key: timeline_result[key] for key in
                            ("round_id", "baseline", "new_this_round", "time_basis")},
               "alerts": alert_result["alerts"],
               "unverifiable": unverifiable_result["groups"]}
    audit({"source_id": "system", "outcome": "processed_data_finalized",
           "events": counts["events"], "issues": issue_count, "alerts": alert_result["alerts"]})
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
