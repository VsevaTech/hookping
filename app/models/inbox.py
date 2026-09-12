from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.event import Event
    from app.models.telegram_connection import TelegramConnection


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


def new_hook_token() -> str:
    """Cryptographically secure, URL-safe, 32 bytes of entropy."""
    return secrets.token_urlsafe(32)


class Inbox(Base):
    __tablename__ = "inboxes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    hook_token: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False, default=new_hook_token)
    message_template: Mapped[str] = mapped_column(Text, nullable=False, default="")

    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    telegram_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    telegram_first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    events: Mapped[list[Event]] = relationship(
        back_populates="inbox",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="desc(Event.received_at)",
    )
    telegram_connections: Mapped[list[TelegramConnection]] = relationship(
        back_populates="inbox",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_chat_id)

    @property
    def telegram_display_name(self) -> str:
        if self.telegram_username:
            return f"@{self.telegram_username}"
        if self.telegram_first_name:
            return self.telegram_first_name
        return self.telegram_chat_id or ""
