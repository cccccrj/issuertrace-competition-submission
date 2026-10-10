#!/usr/bin/env python3
"""把已存的读者可见标签对齐到当前的标签表。**只改文字，不重算任何东西。**

2026-09-23 平台把「议题」统一改叫「专题」（侧栏、检索、预警标题早就叫专题，只有专题页自己和
几处标签还叫议题——一个量两个名字）。处理层有两处文字是写进库里的：

* 风险事项的预警理由 ``alerts.reasons_json``——每条理由是 ``{code, label}``，label 完全由 code
  决定（``alerts.REASON_LABELS``），按表重写即可；
* 专题当前状态的原因 ``issue_ledger.status_reason``——由 ``timeline`` 按模板生成。

为什么不跑 ``processor.finalize``：它会把专题台账、阶段、传播、时间线、风险事项整条链重算一遍。
离上次运行隔了一天，「最近 30 天」的窗口也挪了，会产生真实的状态变化——为了改几十行文字去
改动业务结论，范围不对。下一次正常重建时这两处会自然用上新文字，这里只是不让旧文字在此之前
挂在页面上。

``issue_status_history`` 不动：那是「当时说了什么」的记录，改写历史里的原话等于篡改记录。

用法：``python -m processor.relabel``（幂等，第二次运行改动 0 行）。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from collector.collect import audit, connect
from processor.alerts import REASON_LABELS

#: 旧模板 → 新模板。只替换这一段固定前缀，数字部分原样保留。
STATUS_REASON_RENAMES = {"议题首次出现在最近": "专题首次出现在最近"}


def relabel(db: sqlite3.Connection) -> dict[str, Any]:
    alerts_changed = 0
    for alert_id, raw in db.execute("SELECT alert_id, reasons_json FROM alerts").fetchall():
        reasons = json.loads(raw or "[]")
        changed = False
        for reason in reasons:
            label = REASON_LABELS.get(reason.get("code"))
            if label and reason.get("label") != label:
                reason["label"] = label
                changed = True
        if changed:
            db.execute("UPDATE alerts SET reasons_json=? WHERE alert_id=?",
                       (json.dumps(reasons, ensure_ascii=False), alert_id))
            alerts_changed += 1
    status_changed = 0
    for old, new in STATUS_REASON_RENAMES.items():
        status_changed += db.execute(
            "UPDATE issue_ledger SET status_reason=replace(status_reason, ?, ?) "
            "WHERE status_reason LIKE ?", (old, new, f"%{old}%")).rowcount
    db.commit()
    return {"alerts_reasons": alerts_changed, "issue_status_reason": status_changed}


def main() -> None:
    db = connect()
    result = relabel(db)
    db.close()
    audit({"source_id": "system", "outcome": "processed_labels_aligned", **result})
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
