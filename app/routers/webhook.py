"""Public webhook receiver: POST /h/{hook_token}."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.schemas import ErrorResponse, WebhookAccepted
from app.services.delivery import deliver_event_by_id
from app.services.inboxes import get_inbox_by_token, store_event

logger = logging.getLogger(__name__)

HTTP_413_PAYLOAD_TOO_LARGE = 413

router = APIRouter(tags=["webhook"])


async def read_body_limited(request: Request, limit: int) -> bytes:
    """Read the request body, refusing to buffer more than ``limit`` bytes."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise HTTPException(
                    status_code=HTTP_413_PAYLOAD_TOO_LARGE,
                    detail=f"Payload too large (limit {limit} bytes)",
                )
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Content-Length") from None

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=HTTP_413_PAYLOAD_TOO_LARGE,
                detail=f"Payload too large (limit {limit} bytes)",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def parse_payload(body: bytes, content_type: str) -> Any:
    """Turn the raw body into a JSON-compatible Python value or raise 400."""
    media_type = content_type.split(";", maxsplit=1)[0].strip().lower()
    if not body.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty body")

    if media_type == "application/x-www-form-urlencoded":
        parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        return {key: values[0] if len(values) == 1 else values for key, values in parsed.items()}

    try:
        return json.loads(body)
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Body is not valid JSON") from None


@router.post(
    "/h/{hook_token}",
    response_model=WebhookAccepted,
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
    },
    summary="Receive a webhook",
)
async def receive_webhook(
    hook_token: str,
    request: Request,
    background: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> WebhookAccepted:
    inbox = get_inbox_by_token(db, hook_token)
    if inbox is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown hook token")

    body = await read_body_limited(request, settings.max_body_bytes)
    content_type = request.headers.get("content-type", "")
    payload = parse_payload(body, content_type)

    try:
        event = store_event(db, inbox, payload, request.method, content_type)
    except SQLAlchemyError:
        logger.exception("Database error while storing event for inbox %s", inbox.id)
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not store event, please retry",
        ) from None

    # Respond immediately; template rendering and Telegram delivery run afterwards.
    background.add_task(deliver_event_by_id, event.id)
    return WebhookAccepted(event_id=event.id)
