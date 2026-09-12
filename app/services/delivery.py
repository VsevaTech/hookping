"""Render + deliver pipeline for stored events."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import DeliveryStatus, Event, Inbox
from app.models.inbox import utcnow
from app.services.telegram import TelegramClient
from app.services.templating import render_message, truncate_for_telegram

logger = logging.getLogger(__name__)


def get_telegram_client() -> TelegramClient:
    settings = get_settings()
    return TelegramClient(
        bot_token=settings.telegram_bot_token,
        api_base=settings.telegram_api_base,
        timeout=settings.telegram_timeout_seconds,
    )


def initial_status(inbox: Inbox) -> DeliveryStatus:
    """Status an event gets the moment it is stored, before delivery runs."""
    if get_settings().telegram_enabled and inbox.telegram_configured:
        return DeliveryStatus.PENDING
    return DeliveryStatus.NOT_CONFIGURED


def deliver_event(db: Session, event: Event, client: TelegramClient | None = None) -> Event:
    """Render the inbox template for ``event`` and push it to Telegram.

    The event row is always updated with the outcome; this function never raises
    for template, network or Telegram API problems.
    """
    inbox = event.inbox
    rendered = render_message(inbox.message_template, event.payload_data, inbox.name, event.received_at)
    if rendered.ok:
        event.rendered_message = rendered.text
        event.delivery_error = ""
    else:
        event.rendered_message = ""
        event.delivery_error = rendered.error

    telegram_client = client or get_telegram_client()
    if not telegram_client.configured or not inbox.telegram_configured:
        event.delivery_status = DeliveryStatus.NOT_CONFIGURED.value
        if not rendered.ok:
            # Keep the render problem visible even without Telegram.
            event.delivery_error = rendered.error
        db.commit()
        return event

    if not rendered.ok:
        event.delivery_status = DeliveryStatus.FAILED.value
        db.commit()
        return event

    result = telegram_client.send_message(inbox.telegram_chat_id, truncate_for_telegram(rendered.text))
    if result.ok:
        event.delivery_status = DeliveryStatus.SENT.value
        event.delivery_error = ""
        event.delivered_at = utcnow()
    else:
        event.delivery_status = DeliveryStatus.FAILED.value
        event.delivery_error = result.error
    db.commit()
    return event


def deliver_event_by_id(event_id: str) -> None:
    """Background-task entry point: opens its own session."""
    db = SessionLocal()
    try:
        event = db.get(Event, event_id)
        if event is None:
            logger.warning("Event %s vanished before delivery", event_id)
            return
        deliver_event(db, event)
    except Exception:
        logger.exception("Unexpected error while delivering event %s", event_id)
        db.rollback()
        try:
            event = db.get(Event, event_id)
            if event is not None:
                event.delivery_status = DeliveryStatus.FAILED.value
                event.delivery_error = "Internal error during delivery"
                db.commit()
        except Exception:
            logger.exception("Could not record delivery failure for event %s", event_id)
    finally:
        db.close()
