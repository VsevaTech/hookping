"""Inbox and event persistence helpers."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Event, Inbox
from app.models.inbox import new_hook_token
from app.services.delivery import initial_status


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


def store_event(db: Session, inbox: Inbox, payload: Any, method: str, content_type: str) -> Event:
    event = Event(
        inbox_id=inbox.id,
        method=method,
        content_type=content_type[:120],
        payload=json.dumps(payload, ensure_ascii=False),
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
