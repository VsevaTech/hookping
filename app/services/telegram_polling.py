"""Background long-polling of Telegram getUpdates for /start and /help commands."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from app.database import SessionLocal
from app.services.telegram import TelegramClient
from app.services.telegram_linking import UpdateOutcome, process_update

logger = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 60.0


class TelegramPoller:
    def __init__(self, client: TelegramClient, poll_timeout: int = 25):
        self._client = client
        self._poll_timeout = poll_timeout
        self._offset: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self.bot_username: str | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if not self._client.configured:
            logger.info("Telegram polling disabled: TELEGRAM_BOT_TOKEN is not set")
            return
        me = await self._client.get_me_async()
        if me.ok and isinstance(me.result, dict):
            self.bot_username = me.result.get("username")
            logger.info("Telegram bot authenticated as @%s", self.bot_username)
        else:
            logger.error("Telegram getMe failed (%s); polling will keep retrying", me.error)
        # Long polling and webhooks are mutually exclusive on Telegram's side.
        await self._client.delete_webhook_async()
        self._task = asyncio.create_task(self._run(), name="telegram-poller")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self._task = None

    # -- polling loop ------------------------------------------------------

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stopping.is_set():
            result = await self._client.get_updates_async(self._offset, self._poll_timeout)
            if not result.ok:
                if result.status_code == 409:
                    logger.error("Telegram getUpdates conflict: another poller or webhook is active (%s)", result.error)
                else:
                    logger.warning("Telegram getUpdates failed: %s (retry in %.0fs)", result.error, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue
            backoff = 1.0
            updates = result.result if isinstance(result.result, list) else []
            for update_data in updates:
                await self._handle_update(update_data)
            # Yield to the event loop even when Telegram answered instantly.
            await asyncio.sleep(0)

    async def _handle_update(self, update_data: Any) -> None:
        if not isinstance(update_data, dict):
            return
        update_id = update_data.get("update_id")
        if isinstance(update_id, int):
            self._offset = update_id + 1
        try:
            outcome = await asyncio.to_thread(self._process_in_db, update_data)
        except Exception:
            logger.exception("Failed to process Telegram update %s", update_id)
            return
        if outcome.reply and outcome.chat_id:
            sent = await self._client.send_message_async(outcome.chat_id, outcome.reply)
            if not sent.ok:
                logger.warning("Could not reply to Telegram chat: %s", sent.error)

    @staticmethod
    def _process_in_db(update_data: dict[str, Any]) -> UpdateOutcome:
        db = SessionLocal()
        try:
            return process_update(db, update_data)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
