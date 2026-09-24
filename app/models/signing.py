"""Signing secrets and replay-protection records for webhook signature verification."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.inbox import new_id, utcnow


class InboxSigningSecret(Base):
    """HMAC key of an inbox.

    Kept in its own table, with no ORM relationship from ``Inbox``, so the secret is never
    loaded implicitly (templates, API responses, debug output). HMAC needs the raw key, so it
    cannot be hashed; it is write-only from the UI's point of view: after it is saved the only
    thing ever shown is *when* it was set.
    """

    __tablename__ = "inbox_signing_secrets"

    inbox_id: Mapped[str] = mapped_column(ForeignKey("inboxes.id", ondelete="CASCADE"), primary_key=True)
    secret: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def __repr__(self) -> str:  # never leak the key through logs / tracebacks
        return f"InboxSigningSecret(inbox_id={self.inbox_id!r}, secret=<redacted>)"


class SeenSignature(Base):
    """A signature that was already accepted — rejecting it again blocks replays.

    A valid signature binds ``timestamp + body``; once the timestamp falls outside the tolerance
    window the request is rejected anyway, so rows only need to live that long and are pruned.
    """

    __tablename__ = "seen_signatures"
    __table_args__ = (
        UniqueConstraint("inbox_id", "digest", name="uq_seen_signatures_inbox_digest"),
        Index("ix_seen_signatures_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    inbox_id: Mapped[str] = mapped_column(ForeignKey("inboxes.id", ondelete="CASCADE"), nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
