from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import Inbox, TelegramConnection
from app.services import telegram_linking as linking
from app.services.telegram_polling import TelegramPoller
from tests.conftest import FakeTelegram, create_inbox, telegram_update


def _connection_token(inbox_id: str) -> str:
    with SessionLocal() as db:
        connection = (
            db.query(TelegramConnection)
            .filter_by(inbox_id=inbox_id, used_at=None)
            .order_by(TelegramConnection.created_at.desc())
            .first()
        )
        assert connection is not None
        return connection.token


def _inbox(inbox_id: str) -> Inbox:
    with SessionLocal() as db:
        inbox = db.get(Inbox, inbox_id)
        assert inbox is not None
        return inbox


def test_connect_button_creates_deep_link(client: TestClient) -> None:
    inbox_id = create_inbox(client)
    page = client.get(f"/inboxes/{inbox_id}")
    assert "Connect Telegram" in page.text
    assert "Not connected" in page.text

    response = client.post(f"/inboxes/{inbox_id}/telegram/connect", follow_redirects=True)
    token = _connection_token(inbox_id)
    assert len(token) >= 32
    assert f"https://t.me/hook_ping_bot?start={token}" in response.text
    assert "Open Telegram" in response.text
    assert "press <strong>Start</strong>" in response.text


def test_new_link_invalidates_previous_token(client: TestClient) -> None:
    inbox_id = create_inbox(client)
    client.post(f"/inboxes/{inbox_id}/telegram/connect")
    first = _connection_token(inbox_id)
    client.post(f"/inboxes/{inbox_id}/telegram/connect")
    second = _connection_token(inbox_id)
    assert first != second

    with SessionLocal() as db:
        outcome = linking.process_start_command(db, first, {"id": 1})
        assert not outcome.connected
        assert outcome.reply == linking.INVALID_TOKEN_MESSAGE


def test_valid_start_token_links_chat_to_correct_inbox(client: TestClient) -> None:
    other_id = create_inbox(client, "Other")
    inbox_id = create_inbox(client, "Sales Leads")
    client.post(f"/inboxes/{inbox_id}/telegram/connect")
    token = _connection_token(inbox_id)

    with SessionLocal() as db:
        outcome = linking.process_start_command(
            db, token, {"id": 424242, "first_name": "Seva", "username": "seva_test", "type": "private"}
        )
    assert outcome.connected
    assert outcome.reply == "✅ HookPing connected\n\nThis chat will now receive notifications from:\nSales Leads"

    inbox = _inbox(inbox_id)
    assert inbox.telegram_chat_id == "424242"
    assert inbox.telegram_username == "seva_test"
    assert inbox.telegram_first_name == "Seva"
    assert inbox.telegram_connected_at is not None
    assert _inbox(other_id).telegram_chat_id is None

    status = client.get(f"/api/inboxes/{inbox_id}/telegram/status").json()
    assert status["connected"] is True
    assert status["username"] == "seva_test"
    assert "chat_id" not in status

    page = client.get(f"/inboxes/{inbox_id}")
    assert "✅ Connected" in page.text
    assert "@seva_test" in page.text
    assert "Send test notification" in page.text
    assert "Disconnect" in page.text


def test_invalid_token_is_rejected() -> None:
    with SessionLocal() as db:
        outcome = linking.process_start_command(db, "definitely-not-a-token", {"id": 1})
    assert not outcome.connected
    assert outcome.reply == linking.INVALID_TOKEN_MESSAGE


def test_expired_token_is_rejected(client: TestClient) -> None:
    inbox_id = create_inbox(client)
    client.post(f"/inboxes/{inbox_id}/telegram/connect")
    token = _connection_token(inbox_id)
    with SessionLocal() as db:
        connection = db.query(TelegramConnection).filter_by(token=token).one()
        connection.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    with SessionLocal() as db:
        outcome = linking.process_start_command(db, token, {"id": 1})
    assert not outcome.connected
    assert outcome.reply == linking.INVALID_TOKEN_MESSAGE
    assert _inbox(inbox_id).telegram_chat_id is None
    assert client.get(f"/api/inboxes/{inbox_id}/telegram/status").json()["connected"] is False


def test_used_token_cannot_be_reused(client: TestClient) -> None:
    inbox_id = create_inbox(client)
    client.post(f"/inboxes/{inbox_id}/telegram/connect")
    token = _connection_token(inbox_id)
    with SessionLocal() as db:
        assert linking.process_start_command(db, token, {"id": 111}).connected
    with SessionLocal() as db:
        second = linking.process_start_command(db, token, {"id": 222})
    assert not second.connected
    assert second.reply == linking.INVALID_TOKEN_MESSAGE
    assert _inbox(inbox_id).telegram_chat_id == "111"


def test_bare_start_and_help() -> None:
    with SessionLocal() as db:
        assert linking.process_update(db, telegram_update("/start")).reply == linking.WELCOME_MESSAGE
        assert linking.process_update(db, telegram_update("/start   ")).reply == linking.WELCOME_MESSAGE
        assert linking.process_update(db, telegram_update("/help")).reply == linking.HELP_MESSAGE
        assert linking.process_update(db, telegram_update("/help@hook_ping_bot")).reply == linking.HELP_MESSAGE
        assert linking.process_update(db, telegram_update("random text")).reply is None
        assert linking.process_update(db, {"update_id": 1, "edited_message": {}}).reply is None


def test_disconnect_clears_link_and_allows_reconnect(client: TestClient) -> None:
    inbox_id = create_inbox(client)
    client.post(f"/inboxes/{inbox_id}/telegram/connect")
    with SessionLocal() as db:
        linking.process_start_command(db, _connection_token(inbox_id), {"id": 5, "username": "u"})
    assert _inbox(inbox_id).telegram_configured

    response = client.post(f"/inboxes/{inbox_id}/telegram/disconnect", follow_redirects=True)
    assert "Telegram disconnected" in response.text
    inbox = _inbox(inbox_id)
    assert inbox.telegram_chat_id is None
    assert inbox.telegram_connected_at is None
    assert inbox.telegram_username is None

    client.post(f"/inboxes/{inbox_id}/telegram/connect")
    new_token = _connection_token(inbox_id)
    with SessionLocal() as db:
        assert linking.process_start_command(db, new_token, {"id": 6}).connected
    assert _inbox(inbox_id).telegram_chat_id == "6"


def test_test_notification_calls_telegram(client: TestClient, telegram: FakeTelegram) -> None:
    inbox_id = create_inbox(client)
    client.post(f"/inboxes/{inbox_id}/telegram/connect")
    with SessionLocal() as db:
        linking.process_start_command(db, _connection_token(inbox_id), {"id": 424242})

    response = client.post(f"/inboxes/{inbox_id}/telegram/test", follow_redirects=True)
    assert "Test notification sent" in response.text
    sent = telegram.sent_messages()
    assert len(sent) == 1
    assert sent[0]["chat_id"] == "424242"
    assert sent[0]["text"] == "✅ HookPing test\n\nTelegram notifications are configured correctly."


def test_test_notification_reports_telegram_error(client: TestClient, telegram: FakeTelegram) -> None:
    telegram.ok = False
    telegram.status_code = 403
    telegram.description = "Forbidden: bot was blocked by the user"
    inbox_id = create_inbox(client)
    client.post(f"/inboxes/{inbox_id}/telegram/connect")
    with SessionLocal() as db:
        linking.process_start_command(db, _connection_token(inbox_id), {"id": 424242})

    response = client.post(f"/inboxes/{inbox_id}/telegram/test", follow_redirects=True)
    assert response.status_code == 200
    assert "Telegram API error 403: Forbidden: bot was blocked by the user" in response.text
    assert "Traceback" not in response.text


def test_test_notification_without_connection(client: TestClient, telegram: FakeTelegram) -> None:
    inbox_id = create_inbox(client)
    response = client.post(f"/inboxes/{inbox_id}/telegram/test", follow_redirects=True)
    assert "Connect Telegram first" in response.text
    assert telegram.sent_messages() == []


def test_connect_without_bot_token_shows_error(client: TestClient, no_bot_token: None) -> None:
    inbox_id = create_inbox(client)
    page = client.get(f"/inboxes/{inbox_id}")
    assert "TELEGRAM_BOT_TOKEN" in page.text
    assert "Connect Telegram" not in page.text
    response = client.post(f"/inboxes/{inbox_id}/telegram/connect", follow_redirects=True)
    assert "TELEGRAM_BOT_TOKEN is not set" in response.text


def test_manual_chat_id_fallback(client: TestClient) -> None:
    inbox_id = create_inbox(client)
    bad = client.post(f"/inboxes/{inbox_id}/telegram/manual", data={"telegram_chat_id": "abc"}, follow_redirects=True)
    assert "Chat ID must look like" in bad.text
    good = client.post(
        f"/inboxes/{inbox_id}/telegram/manual", data={"telegram_chat_id": "-1001234567890"}, follow_redirects=True
    )
    assert "Telegram chat linked" in good.text
    assert _inbox(inbox_id).telegram_chat_id == "-1001234567890"


def test_poller_processes_start_command_end_to_end(client: TestClient) -> None:
    """getUpdates -> /start <token> -> inbox linked -> confirmation sent, all through the mocked API."""
    inbox_id = create_inbox(client, "Sales Leads")
    client.post(f"/inboxes/{inbox_id}/telegram/connect")
    token = _connection_token(inbox_id)

    fake = FakeTelegram(
        updates=[
            telegram_update("/start", update_id=1),
            telegram_update(f"/start {token}", update_id=2),
            {"update_id": 3, "message": {"chat": {"id": 1}}},  # no text -> ignored
        ]
    )
    poller = TelegramPoller(fake.make_client(), poll_timeout=0)

    async def run() -> None:
        await poller.start()
        assert poller.bot_username == "hook_ping_bot"
        for _ in range(50):
            await asyncio.sleep(0.02)
            if len(fake.sent_messages()) >= 2:
                break
        await poller.stop()

    asyncio.run(run())

    sent = fake.sent_messages()
    assert [m["text"] for m in sent[:2]] == [linking.WELCOME_MESSAGE, linking.connected_message("Sales Leads")]
    assert _inbox(inbox_id).telegram_chat_id == "424242"
    methods = [c["method"] for c in fake.calls]
    assert methods[:2] == ["getMe", "deleteWebhook"]
    # offset advanced past the last update, so later polls do not re-read them
    later_polls = [c["payload"].get("offset") for c in fake.calls if c["method"] == "getUpdates"][1:]
    assert later_polls and all(offset == 4 for offset in later_polls)


def test_poller_survives_api_errors() -> None:
    fake = FakeTelegram(ok=False, status_code=502, description="Bad Gateway")
    poller = TelegramPoller(fake.make_client(), poll_timeout=0)

    async def run() -> None:
        await poller.start()
        await asyncio.sleep(0.1)
        await poller.stop()

    asyncio.run(run())
    assert any(c["method"] == "getUpdates" for c in fake.calls)
