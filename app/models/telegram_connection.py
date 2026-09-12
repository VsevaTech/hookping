from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.inbox import Inbox, new_id, utcnow

CONNECTION_TOKEN_TTL = timedelta(minutes=10)


def new_connection_token() -> str:
    """URL-safe token that fits Telegram's 64-char ``start`` parameter limit."""
    return secrets.token_urlsafe(24)  # 32 chars, 192 bits of entropy


class TelegramConnection(Base):
    """One-time, short-lived token that links a Telegram chat to an inbox via /start."""

    __tablename__ = "telegram_connections"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    inbox_id: Mapped[str] = mapped_column(ForeignKey("inboxes.id", ondelete="CASCADE"), nullable=False, index=True)
    token: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False, default=new_connection_token
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: utcnow() + CONNECTION_TOKEN_TTL
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    inbox: Mapped[Inbox] = relationship(back_populates="telegram_connections")

    def is_valid(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return self.used_at is None and as_utc(self.expires_at) > now


def as_utc(value: datetime) -> datetime:
    """SQLite drops tzinfo on read; treat naive values as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
