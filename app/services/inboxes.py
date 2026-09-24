"""Inbox and event persistence helpers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Event, Inbox, InboxSigningSecret, SeenSignature
from app.models.inbox import new_hook_token, utcnow
from app.services.delivery import initial_status
from app.services.signatures import MAX_TOLERANCE_SECONDS, VerificationMode, Verified


class ReplayDetectedError(Exception):
    """The same signed request was already accepted."""


def create_inbox(db: Session, name: str) -> Inbox:
    inbox = Inbox(name=name.strip())
    # Collisions are astronomically unlikely, but the loop keeps the invariant explicit.
    while db.scalar(select(Inbox.id).where(Inbox.hook_token == inbox.hook_token)) is not None:
        inbox.hook_token = new_hook_token()
    db.add(inbox)
    db.commit()
    db.refresh(inbox)
    return inbox


def list_inboxes(db: Session) -> list[Inbox]:
    return list(db.scalars(select(Inbox).order_by(Inbox.created_at.desc())).all())


def event_counts(db: Session) -> dict[str, int]:
    rows = db.execute(select(Event.inbox_id, func.count(Event.id)).group_by(Event.inbox_id)).all()
    return {inbox_id: count for inbox_id, count in rows}


def get_inbox(db: Session, inbox_id: str) -> Inbox | None:
    return db.get(Inbox, inbox_id)


def get_inbox_by_token(db: Session, hook_token: str) -> Inbox | None:
    return db.scalar(select(Inbox).where(Inbox.hook_token == hook_token))


def delete_inbox(db: Session, inbox: Inbox) -> None:
    db.delete(inbox)
    db.commit()


def recent_events(db: Session, inbox: Inbox, limit: int | None = None) -> list[Event]:
    limit = limit or get_settings().recent_events_limit
    stmt = (
        select(Event).where(Event.inbox_id == inbox.id).order_by(Event.received_at.desc(), Event.id.desc()).limit(limit)
    )
    return list(db.scalars(stmt).all())


def latest_event(db: Session, inbox: Inbox) -> Event | None:
    events = recent_events(db, inbox, limit=1)
    return events[0] if events else None


def get_event(db: Session, inbox: Inbox, event_id: str) -> Event | None:
    event = db.get(Event, event_id)
    if event is None or event.inbox_id != inbox.id:
        return None
    return event


def store_event(
    db: Session,
    inbox: Inbox,
    payload: Any,
    method: str,
    content_type: str,
    *,
    verified: Verified | None = None,
) -> Event:
    """Persist an event. With ``verified``, the signature is recorded in the same transaction,
    so a replayed request raises :class:`ReplayDetectedError` and stores nothing."""
    if verified is not None:
        _remember_signature(db, inbox, verified)
    event = Event(
        inbox_id=inbox.id,
        method=method,
        content_type=content_type[:120],
        payload=json.dumps(payload, ensure_ascii=False),
        verification=(VerificationMode.HMAC_SHA256 if verified else VerificationMode.NONE).value,
        delivery_status=initial_status(inbox).value,
    )
    db.add(event)
    db.flush()
    _prune_old_events(db, inbox)
    db.commit()
    db.refresh(event)
    return event


def _prune_old_events(db: Session, inbox: Inbox) -> None:
    keep = get_settings().max_events_per_inbox
    if keep <= 0:
        return
    cutoff_ids = (
        select(Event.id)
        .where(Event.inbox_id == inbox.id)
        .order_by(Event.received_at.desc(), Event.id.desc())
        .offset(keep)
    )
    db.execute(delete(Event).where(Event.id.in_(cutoff_ids)))


def _remember_signature(db: Session, inbox: Inbox, verified: Verified) -> None:
    now = utcnow()
    db.execute(delete(SeenSignature).where(SeenSignature.expires_at < now))
    # Keep the record for the largest possible window, so raising the tolerance later
    # cannot re-open a replay window for signatures that were already accepted.
    expires_at = datetime.fromtimestamp(verified.timestamp, UTC) + timedelta(seconds=MAX_TOLERANCE_SECONDS)
    db.add(SeenSignature(inbox_id=inbox.id, digest=verified.digest, expires_at=max(expires_at, now)))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ReplayDetectedError from None


# --- signing secrets ------------------------------------------------------------------------


def get_signing_secret(db: Session, inbox: Inbox) -> str | None:
    row = db.get(InboxSigningSecret, inbox.id)
    return row.secret if row is not None else None


def set_signing_secret(db: Session, inbox: Inbox, secret: str) -> None:
    row = db.get(InboxSigningSecret, inbox.id)
    now = utcnow()
    if row is None:
        db.add(InboxSigningSecret(inbox_id=inbox.id, secret=secret, created_at=now))
    else:
        row.secret = secret
        row.created_at = now
    inbox.signing_secret_set_at = now
    db.commit()


def delete_signing_secret(db: Session, inbox: Inbox) -> None:
    db.execute(delete(InboxSigningSecret).where(InboxSigningSecret.inbox_id == inbox.id))
    inbox.signing_secret_set_at = None
    inbox.verification_mode = VerificationMode.NONE.value
    db.commit()


def record_rejection(db: Session, inbox: Inbox, reason: str) -> None:
    """Remember the last rejected request so the UI can explain why webhooks are not arriving."""
    inbox.last_rejected_at = utcnow()
    inbox.last_rejection_reason = reason[:200]
    db.commit()
