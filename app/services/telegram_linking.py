"""Linking Telegram chats to inboxes through one-time /start tokens."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Inbox, TelegramConnection
from app.models.inbox import utcnow

logger = logging.getLogger(__name__)

WELCOME_MESSAGE = """👋 Welcome to HookPing.

To connect this chat, open HookPing and choose:
Connect Telegram"""

HELP_MESSAGE = """HookPing sends human-readable notifications from your webhooks.

To connect Telegram:
1. Open your HookPing inbox.
2. Click Connect Telegram.
3. Open the generated Telegram link.
4. Press Start."""

INVALID_TOKEN_MESSAGE = """❌ This connection link is invalid or expired.
Please generate a new link in HookPing."""

TEST_MESSAGE = """✅ HookPing test

Telegram notifications are configured correctly."""


def connected_message(inbox_name: str) -> str:
    return f"✅ HookPing connected\n\nThis chat will now receive notifications from:\n{inbox_name}"


def deep_link(token: str) -> str:
    return f"https://t.me/{get_settings().telegram_bot_username}?start={token}"


def create_connection(db: Session, inbox: Inbox) -> TelegramConnection:
    """Issue a fresh one-time token, invalidating any still-open ones for the inbox."""
    now = utcnow()
    db.execute(
        update(TelegramConnection)
        .where(TelegramConnection.inbox_id == inbox.id, TelegramConnection.used_at.is_(None))
        .values(expires_at=now)
    )
    connection = TelegramConnection(inbox_id=inbox.id)
    db.add(connection)
    db.commit()
    db.refresh(connection)
    return connection


def pending_connection(db: Session, inbox: Inbox) -> TelegramConnection | None:
    """The newest still-valid token for the inbox, if any."""
    stmt = (
        select(TelegramConnection)
        .where(TelegramConnection.inbox_id == inbox.id, TelegramConnection.used_at.is_(None))
        .order_by(TelegramConnection.created_at.desc())
        .limit(1)
    )
    connection = db.scalar(stmt)
    if connection is not None and connection.is_valid():
        return connection
    return None


def disconnect(db: Session, inbox: Inbox) -> None:
    inbox.telegram_chat_id = None
    inbox.telegram_connected_at = None
    inbox.telegram_username = None
    inbox.telegram_first_name = None
    db.commit()


@dataclass(frozen=True)
class StartOutcome:
    reply: str
    inbox: Inbox | None = None

    @property
    def connected(self) -> bool:
        return self.inbox is not None


def process_start_command(db: Session, token: str, chat: dict[str, Any]) -> StartOutcome:
    """Validate a /start token and attach the chat to its inbox."""
    token = token.strip()
    if not token:
        return StartOutcome(reply=WELCOME_MESSAGE)

    connection = db.scalar(select(TelegramConnection).where(TelegramConnection.token == token))
    if connection is None or not connection.is_valid():
        return StartOutcome(reply=INVALID_TOKEN_MESSAGE)

    inbox = connection.inbox
    now = utcnow()
    inbox.telegram_chat_id = str(chat["id"])
    inbox.telegram_connected_at = now
    inbox.telegram_username = _clip(chat.get("username"), 64)
    inbox.telegram_first_name = _clip(chat.get("title") or chat.get("first_name"), 128)
    connection.used_at = now
    db.commit()
    logger.info("Telegram chat linked to inbox %s", inbox.id)
    return StartOutcome(reply=connected_message(inbox.name), inbox=inbox)


def _clip(value: Any, limit: int) -> str | None:
    if not value:
        return None
    return str(value)[:limit]


@dataclass(frozen=True)
class UpdateOutcome:
    chat_id: str | None
    reply: str | None


def process_update(db: Session, update_data: dict[str, Any]) -> UpdateOutcome:
    """Turn one Telegram update into (chat_id, reply text). Unknown input yields no reply."""
    message = update_data.get("message")
    if not isinstance(message, dict):
        return UpdateOutcome(chat_id=None, reply=None)
    chat = message.get("chat")
    if not isinstance(chat, dict) or "id" not in chat:
        return UpdateOutcome(chat_id=None, reply=None)
    chat_id = str(chat["id"])
    text = message.get("text")
    if not isinstance(text, str):
        return UpdateOutcome(chat_id=chat_id, reply=None)

    command, _, argument = text.strip().partition(" ")
    command = command.split("@", 1)[0].lower()  # "/start@hook_ping_bot" in groups
    if command == "/start":
        return UpdateOutcome(chat_id=chat_id, reply=process_start_command(db, argument, chat).reply)
    if command == "/help":
        return UpdateOutcome(chat_id=chat_id, reply=HELP_MESSAGE)
    return UpdateOutcome(chat_id=chat_id, reply=None)
