"""The notification inbox — alerts and review tasks, and who has read them."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from api import notify_db
from api.routes.auth import current_session
from api.routes.events import db_session

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


class ReadRequest(BaseModel):
    # Absent means "everything currently in the inbox"; a list means those rows.
    notification_ids: list[str] | None = None


def notify_session():
    db = notify_db.connect()
    try:
        yield db
    finally:
        db.close()


@router.get("")
def list_notifications(unread: bool = False, limit: int = Query(50, ge=1, le=200),
                       offset: int = Query(0, ge=0), session=Depends(current_session),
                       notify=Depends(notify_session), db=Depends(db_session)) -> dict[str, Any]:
    created = notify_db.sync(notify, db)
    return {**notify_db.inbox(notify, session["username"], unread_only=unread,
                              limit=limit, offset=offset),
            "new_since_last_read": created}


@router.post("/read")
def mark_read(body: ReadRequest | None = None, session=Depends(current_session),
              notify=Depends(notify_session)) -> dict[str, Any]:
    marked = notify_db.mark_read(notify, session["username"],
                                 body.notification_ids if body else None)
    return {"marked": marked, "unread": notify_db.inbox(notify, session["username"])["unread"]}
