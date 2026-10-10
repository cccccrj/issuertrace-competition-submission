"""平台动作留痕：登录、导出、权限变更——审计表里此前一行都没有的那三类。

方案 §9.2 列了九类要留痕的动作，其中六类本来就有自己的表：处理轮次、复核判定、
采集日志、模型版本、投递台账、处置流水。它们的共同点是**留痕不是它们存在的理由**
——「谁在什么时候把这条事项转成了核查中」是处置这件事的本体记录，顺带能当审计看。

剩下这三类没有这样的本体记录：登录成功不改变任何业务数据，导出一份 Word 也不改，
把某人从分析师改成核查负责人只在账号表上留下一个当前值、不留下「是谁改的」。
所以它们需要一张自己的表，而这张表存在的唯一理由就是留痕。

**三条边界：**

1. **只追加。** 这个模块没有 UPDATE，也没有 DELETE，导出去也只有 ``recent()`` 一条读路。
   一份能被改的审计记录不是审计记录。
2. **不复用 ``collector.collect.audit``。** 那是采集日志——机器抓到了什么、哪条源被隔离了。
   两种日志混在一个文件里，谁也说不清某一行是机器写的还是人触发的。
3. **留痕的对象是动作，不是浏览。** 方案里还有一条「查看敏感事项也留痕」，没有做：
   每打开一页写一行，审计表会变成访问日志，在一个只读平台上既拖慢读路径、又把真正的
   动作记录淹掉（理由记在 docs/方案剩余项评估与任务顺序.md）。

**写在路由层，不写在 ``users_db``/``auth`` 里。** 一条留痕要回答「谁、从哪里、对谁做了
什么」，而 ``users_db.set_role()`` 这一层既不认识会话也不认识来源 IP——真要在那里写，
就得把 actor 和 client 一路当参数传进去，等于把「谁在操作」这件事复制到每一个函数签名上。
路由是人的动作进入系统的那道边界，留痕就写在边界上。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from api.review_db import connect as write_connect, now as utc_now

#: 动作词表。键入库，值上页面——又一个「一个量只有一个名字」的地方：
#: 前端不许自己再抄一份，``/api/audit/actions`` 把这张表一起返回。
ACTION_LABELS = {
    "login": "登录",
    "logout": "登出",
    "report_export": "导出报告",
    "account_create": "新建账号",
    "role_change": "变更角色",
    "active_change": "启用／停用账号",
    "password_reset": "重置他人口令",
    "password_change": "修改本人口令",
    "assistant_ask": "研判助手提问",
}

#: 结果词表。``refused`` 与 ``failed`` 分开：前者是系统按规则拒绝（口令不对、角色不够），
#: 后者是系统自己没做成。把两者并成一个「失败」，就看不出「有人在试口令」和「导出坏了」
#: 的区别，而这两件事要找的人不是同一个。
OUTCOME_LABELS = {"ok": "成功", "refused": "被拒绝", "failed": "失败"}


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS platform_actions(
        action_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        occurred_at TEXT NOT NULL,
        actor TEXT NOT NULL,
        role TEXT,
        action TEXT NOT NULL,
        object_kind TEXT,
        object_id TEXT,
        outcome TEXT NOT NULL,
        client TEXT,
        detail TEXT)""")
    db.execute("""CREATE INDEX IF NOT EXISTS platform_actions_recent
        ON platform_actions(occurred_at DESC, action_seq DESC)""")
    db.commit()


def connect() -> sqlite3.Connection:
    db = write_connect()
    ensure_schema(db)
    return db


def record(action: str, *, actor: str, outcome: str = "ok", role: str | None = None,
           object_kind: str | None = None, object_id: str | None = None,
           client: str | None = None, detail: str | None = None) -> None:
    """记一行。**这个函数不抛异常。**

    留痕失败不该把它记录的那个动作一起弄失败：一次写不进日志的登录仍然是一次成功的
    登录，把它变成 500 只会让人重试，而重试同样写不进去。写不进去的代价是少一行记录，
    写崩了的代价是这个动作从此不能用——后者更贵。

    ``detail`` 只收结论性的短句。**口令、令牌、Cookie 一个字都不进这里**：一份
    记下了口令的审计日志，本身就是那个口令的第二个存放处。
    """
    try:
        db = connect()
        try:
            db.execute("""INSERT INTO platform_actions(
                occurred_at, actor, role, action, object_kind, object_id, outcome, client, detail)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (utc_now(), actor, role, action, object_kind, object_id, outcome, client, detail))
            db.commit()
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 —— 见上
        print(f"（平台动作未能留痕：{action} / {exc}）", flush=True)


def recent(db: sqlite3.Connection, *, action: str | None = None, outcome: str | None = None,
           limit: int = 100) -> list[dict[str, Any]]:
    """最近的在前。两个筛子都可空——「今天谁登录过」与「有谁被拒过」是两个问法。"""
    clauses, params = [], []
    if action:
        clauses.append("action = ?")
        params.append(action)
    if outcome:
        clauses.append("outcome = ?")
        params.append(outcome)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = db.execute(f"""SELECT * FROM platform_actions {where}
        ORDER BY occurred_at DESC, action_seq DESC LIMIT ?""", params).fetchall()
    return [dict(row) for row in rows]


def counts(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """每类动作各有多少行，多的在前。空表返回空列表——**不补零**：
    「这一类从来没发生过」和「这一类发生过 0 次」在页面上该长得一样，都是没有这一行。
    """
    return [dict(row) for row in db.execute(
        "SELECT action, count(*) AS rows FROM platform_actions GROUP BY 1 ORDER BY rows DESC")]
