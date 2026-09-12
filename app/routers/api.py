"""Small JSON API used by the UI (status polling)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.telegram_connection import as_utc
from app.schemas import TelegramStatus
from app.services import inboxes as inbox_service

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/inboxes/{inbox_id}/telegram/status", response_model=TelegramStatus)
def telegram_status(inbox_id: str, db: Annotated[Session, Depends(get_db)]) -> TelegramStatus:
    inbox = inbox_service.get_inbox(db, inbox_id)
    if inbox is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inbox not found")
    return TelegramStatus(
        connected=inbox.telegram_configured,
        username=inbox.telegram_username,
        first_name=inbox.telegram_first_name,
        connected_at=as_utc(inbox.telegram_connected_at) if inbox.telegram_connected_at else None,
    )
