"""Thin, defensive client for the Telegram Bot API.

Only the handful of methods HookPing needs are wrapped. Every public method
returns a structured result and never raises for network or API problems.
The bot token is never logged; any error text that could contain it is redacted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelegramResult:
    ok: bool
    error: str = ""
    status_code: int | None = None
    result: Any = None

    @property
    def message_id(self) -> int | None:
        if isinstance(self.result, dict):
            value = self.result.get("message_id")
            return int(value) if isinstance(value, int) else None
        return None


class TelegramClient:
    def __init__(
        self,
        bot_token: str,
        api_base: str = "https://api.telegram.org",
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        async_transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._token = bot_token
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout
        self._transport = transport
        self._async_transport = async_transport

    @property
    def configured(self) -> bool:
        return bool(self._token)

    def _url(self, method: str) -> str:
        return f"{self._api_base}/bot{self._token}/{method}"

    def redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    # -- response handling -------------------------------------------------

    def _handle_response(self, method: str, response: httpx.Response) -> TelegramResult:
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code == 200 and isinstance(body, dict) and body.get("ok"):
            return TelegramResult(ok=True, status_code=200, result=body.get("result"))

        description = body.get("description") if isinstance(body, dict) else None
        error = f"Telegram API error {response.status_code}"
        if description:
            error = f"{error}: {description}"
        error = self.redact(error)
        logger.warning("Telegram %s failed: %s", method, error)
        return TelegramResult(ok=False, error=error, status_code=response.status_code)

    def _handle_exception(self, method: str, exc: Exception, timeout: float) -> TelegramResult:
        if isinstance(exc, httpx.TimeoutException):
            error = f"Telegram request timed out after {timeout:g}s"
        else:
            error = f"Telegram request failed: {self.redact(str(exc)) or exc.__class__.__name__}"
        logger.warning("Telegram %s failed: %s", method, error)
        return TelegramResult(ok=False, error=error)

    # -- transport ---------------------------------------------------------

    def call(self, method: str, payload: dict[str, Any], timeout: float | None = None) -> TelegramResult:
        if not self.configured:
            return TelegramResult(ok=False, error="Telegram bot token is not configured")
        effective_timeout = timeout or self._timeout
        try:
            with httpx.Client(timeout=effective_timeout, transport=self._transport) as client:
                response = client.post(self._url(method), json=payload)
        except httpx.HTTPError as exc:
            return self._handle_exception(method, exc, effective_timeout)
        return self._handle_response(method, response)

    async def call_async(self, method: str, payload: dict[str, Any], timeout: float | None = None) -> TelegramResult:
        if not self.configured:
            return TelegramResult(ok=False, error="Telegram bot token is not configured")
        effective_timeout = timeout or self._timeout
        try:
            async with httpx.AsyncClient(timeout=effective_timeout, transport=self._async_transport) as client:
                response = await client.post(self._url(method), json=payload)
        except httpx.HTTPError as exc:
            return self._handle_exception(method, exc, effective_timeout)
        return self._handle_response(method, response)

    # -- API methods -------------------------------------------------------

    @staticmethod
    def _send_payload(chat_id: str, text: str) -> dict[str, Any]:
        return {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}

    def send_message(self, chat_id: str | None, text: str) -> TelegramResult:
        if not chat_id:
            return TelegramResult(ok=False, error="Telegram chat id is empty")
        return self.call("sendMessage", self._send_payload(chat_id, text))

    async def send_message_async(self, chat_id: str | None, text: str) -> TelegramResult:
        if not chat_id:
            return TelegramResult(ok=False, error="Telegram chat id is empty")
        return await self.call_async("sendMessage", self._send_payload(chat_id, text))

    def get_me(self) -> TelegramResult:
        return self.call("getMe", {})

    async def get_me_async(self) -> TelegramResult:
        return await self.call_async("getMe", {})

    async def delete_webhook_async(self) -> TelegramResult:
        return await self.call_async("deleteWebhook", {"drop_pending_updates": False})

    async def get_updates_async(self, offset: int | None, poll_timeout: int) -> TelegramResult:
        payload: dict[str, Any] = {
            "timeout": poll_timeout,
            "allowed_updates": ["message"],
            "limit": 100,
        }
        if offset is not None:
            payload["offset"] = offset
        # The HTTP timeout must exceed Telegram's long-poll timeout.
        return await self.call_async("getUpdates", payload, timeout=poll_timeout + 10)
