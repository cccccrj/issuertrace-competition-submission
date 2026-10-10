"""研判助手的唯一入口。规则都在 ``api.assistant`` 里，这里只做三件事：收问题、认范围、留痕。"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterator, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api import assistant, platform_audit
from api.sources import name_sources
from api.routes.auth import client_of, current_session
from api.routes.events import db_session

router = APIRouter(prefix="/api/assistant", tags=["assistant"])


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=assistant.MAX_QUESTION_CHARS)
    scope: Literal["risk", "event"]
    id: str = Field(min_length=1, max_length=128)


technical_log = logging.getLogger("lcei.api")


def checked(body: Question, db: Any) -> None:
    if not body.question.strip():
        raise HTTPException(status_code=422, detail="问题不能为空")
    table, column = ("alerts", "alert_id") if body.scope == "risk" else ("event_records", "event_id")
    if not db.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (body.id,)).fetchone():
        raise HTTPException(status_code=404, detail="要找的内容不存在或已被移除")


def audit(body: Question, request: Request, session: dict[str, Any], result: dict[str, Any]) -> None:
    platform_audit.record("assistant_ask", actor=session["username"], role=session.get("role"),
                          object_kind=body.scope, object_id=body.id, client=client_of(request),
                          outcome="ok" if result.get("available") else "failed",
                          detail=assistant.audit_detail(result))


@router.post("/ask")
def ask(body: Question, request: Request, db=Depends(db_session),
        session=Depends(current_session)) -> dict[str, Any]:
    """一次拿到最终结果。POST 只是因为问题可能很长、不该进地址栏和访问日志；它不写任何业务数据。

    唯一的写入是审计表里的一行（``platform_actions``，不在分析库里）。
    """
    checked(body, db)
    result = assistant.ask(db, body.question, body.scope, body.id)
    audit(body, request, session, result)
    return result


@router.post("/stream")
def stream(body: Question, request: Request, db=Depends(db_session),
           session=Depends(current_session)) -> StreamingResponse:
    """边查边推（Server-Sent Events）：每做完一步推一条 ``step``，最后推 ``result``。

    页面因此能实时列出「第几步、查什么、为什么」。``text/event-stream`` 不经过 GZip
    （Starlette 默认排除），一步一推，不会被压缩缓冲攒成一整块。校验与 404 在开流之前做完，
    所以「这条不存在」仍然是一个普通的 404，而不是流里的一条消息。
    """
    checked(body, db)

    def events() -> Iterator[str]:
        result: dict[str, Any] = {"available": False, "reason": assistant.UNAVAILABLE, "steps": []}
        try:
            for event in assistant.ask_events(db, body.question, body.scope, body.id):
                if event["type"] == "result":
                    result = event["result"]
                # 出口与 JSON 接口一致：来源编码旁补上中文名（I-06）。
                yield f"data: {json.dumps(name_sources(event), ensure_ascii=False)}\n\n"
        except Exception:  # noqa: BLE001 —— 流已经开了，只能以一条降级结果收尾，原文进技术日志
            technical_log.exception("assistant stream failed scope=%s", body.scope)
            yield f"data: {json.dumps({'type': 'result', 'result': result}, ensure_ascii=False)}\n\n"
        finally:
            audit(body, request, session, result)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
