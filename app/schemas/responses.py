from datetime import datetime

from pydantic import BaseModel


class WebhookAccepted(BaseModel):
    received: bool = True
    event_id: str


class HealthResponse(BaseModel):
    status: str = "ok"


class ErrorResponse(BaseModel):
    detail: str


class TelegramStatus(BaseModel):
    """Connection state of an inbox. Deliberately excludes chat id and any secrets."""

    connected: bool
    username: str | None = None
    first_name: str | None = None
    connected_at: datetime | None = None
