from __future__ import annotations

import enum
import json
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.inbox import Inbox, new_id, utcnow


class DeliveryStatus(enum.StrEnum):
    NOT_CONFIGURED = "not_configured"
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_inbox_received", "inbox_id", "received_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    inbox_id: Mapped[str] = mapped_column(ForeignKey("inboxes.id", ondelete="CASCADE"), nullable=False, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    method: Mapped[str] = mapped_column(String(10), nullable=False, default="POST")
    content_type: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    # Raw payload stored as JSON text so any JSON value (object, array, scalar) fits.
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="null")
    delivery_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=DeliveryStatus.NOT_CONFIGURED.value
    )
    delivery_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rendered_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    inbox: Mapped[Inbox] = relationship(back_populates="events")

    @property
    def payload_data(self) -> Any:
        try:
            return json.loads(self.payload)
        except (TypeError, ValueError):
            return None

    @property
    def payload_pretty(self) -> str:
        data = self.payload_data
        if data is None:
            return self.payload
        return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False)
