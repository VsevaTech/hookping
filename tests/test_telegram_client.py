from __future__ import annotations

import asyncio

import httpx

from app.services.telegram import TelegramClient
from tests.conftest import FAKE_BOT_TOKEN, FakeTelegram


def test_successful_delivery() -> None:
    fake = FakeTelegram()
    result = fake.make_client().send_message("42", "hello")
    assert result.ok
    assert result.message_id == 1
    assert fake.calls[0]["method"] == "sendMessage"
    assert fake.calls[0]["payload"] == {"chat_id": "42", "text": "hello", "disable_web_page_preview": True}


def test_telegram_400_is_reported() -> None:
    fake = FakeTelegram(ok=False, status_code=400, description="Bad Request: chat not found")
    result = fake.make_client().send_message("42", "hello")
    assert not result.ok
    assert result.status_code == 400
    assert result.error == "Telegram API error 400: Bad Request: chat not found"


def test_telegram_500_is_reported() -> None:
    fake = FakeTelegram(ok=False, status_code=500, description="Internal Server Error")
    result = fake.make_client().send_message("42", "hello")
    assert not result.ok
    assert result.status_code == 500
    assert "500" in result.error


def test_timeout_is_reported_without_raising() -> None:
    fake = FakeTelegram(raise_timeout=True)
    result = fake.make_client().send_message("42", "hello")
    assert not result.ok
    assert result.error == "Telegram request timed out after 1s"


def test_network_error_is_reported_without_raising() -> None:
    fake = FakeTelegram(raise_network_error=True)
    result = fake.make_client().send_message("42", "hello")
    assert not result.ok
    assert result.error.startswith("Telegram request failed")


def test_non_json_response_is_handled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    client = TelegramClient(FAKE_BOT_TOKEN, transport=httpx.MockTransport(handler))
    result = client.send_message("42", "hello")
    assert not result.ok
    assert result.error == "Telegram API error 502"


def test_missing_token_or_chat_id() -> None:
    assert not TelegramClient("").configured
    assert TelegramClient("").send_message("42", "x").error == "Telegram bot token is not configured"
    assert FakeTelegram().make_client().send_message("", "x").error == "Telegram chat id is empty"


def test_bot_token_is_redacted_from_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    client = TelegramClient(FAKE_BOT_TOKEN, transport=httpx.MockTransport(handler))
    result = client.send_message("42", "hello")
    assert not result.ok
    assert FAKE_BOT_TOKEN not in result.error
    assert "***" in result.error


def test_async_methods() -> None:
    fake = FakeTelegram(updates=[{"update_id": 7, "message": {"chat": {"id": 1}, "text": "/help"}}])
    client = fake.make_client()

    async def run() -> None:
        me = await client.get_me_async()
        assert me.ok and me.result["username"] == "hook_ping_bot"
        updates = await client.get_updates_async(offset=None, poll_timeout=1)
        assert updates.ok and updates.result[0]["update_id"] == 7
        none_left = await client.get_updates_async(offset=8, poll_timeout=1)
        assert none_left.result == []
        sent = await client.send_message_async("1", "hi")
        assert sent.ok

    asyncio.run(run())
    assert [c["method"] for c in fake.calls] == ["getMe", "getUpdates", "getUpdates", "sendMessage"]
