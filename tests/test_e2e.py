"""The acceptance scenario: create inbox -> connect Telegram -> template -> webhook -> Telegram message."""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import DeliveryStatus, Event, TelegramConnection
from app.services import telegram_linking as linking
from tests.conftest import FakeTelegram, create_inbox, hook_token

TEMPLATE = "🚀 New lead\n\nName: {{ name }}\nPlan: {{ plan }}\nValue: ${{ amount }}"


def _connect(client: TestClient, inbox_id: str, chat_id: int = 424242) -> None:
    client.post(f"/inboxes/{inbox_id}/telegram/connect")
    with SessionLocal() as db:
        token = db.query(TelegramConnection).filter_by(inbox_id=inbox_id, used_at=None).one().token
        assert linking.process_start_command(db, token, {"id": chat_id, "first_name": "Seva"}).connected


def test_full_flow_delivers_rendered_message(client: TestClient, telegram: FakeTelegram) -> None:
    inbox_id = create_inbox(client, "Sales Leads")
    _connect(client, inbox_id)

    saved = client.post(
        f"/inboxes/{inbox_id}/template", data={"message_template": TEMPLATE, "action": "save"}, follow_redirects=False
    )
    assert saved.status_code == 303

    response = client.post(f"/h/{hook_token(inbox_id)}", json={"name": "Ivan", "plan": "Pro", "amount": 250})
    assert response.status_code == 200
    event_id = response.json()["event_id"]

    sent = telegram.sent_messages()
    assert len(sent) == 1
    assert sent[0]["chat_id"] == "424242"
    assert sent[0]["text"] == "🚀 New lead\n\nName: Ivan\nPlan: Pro\nValue: $250"

    with SessionLocal() as db:
        event = db.get(Event, event_id)
        assert event.delivery_status == DeliveryStatus.SENT.value
        assert event.rendered_message == sent[0]["text"]
        assert event.delivery_error == ""
        assert event.delivered_at is not None

    page = client.get(f"/inboxes/{inbox_id}/events/{event_id}")
    assert "Name: Ivan" in page.text
    assert 'badge-sent">sent' in page.text


def test_telegram_failure_keeps_event_and_marks_failed(client: TestClient, telegram: FakeTelegram) -> None:
    telegram.ok = False
    telegram.status_code = 400
    telegram.description = "Bad Request: chat not found"
    inbox_id = create_inbox(client)
    _connect(client, inbox_id)

    response = client.post(f"/h/{hook_token(inbox_id)}", json={"name": "Ivan"})
    assert response.status_code == 200  # the sender must never see Telegram problems

    with SessionLocal() as db:
        event = db.get(Event, response.json()["event_id"])
        assert event.delivery_status == DeliveryStatus.FAILED.value
        assert event.delivery_error == "Telegram API error 400: Bad Request: chat not found"
        assert '"name": "Ivan"' in event.payload


def test_telegram_timeout_keeps_event(client: TestClient, telegram: FakeTelegram) -> None:
    telegram.raise_timeout = True
    inbox_id = create_inbox(client)
    _connect(client, inbox_id)
    response = client.post(f"/h/{hook_token(inbox_id)}", json={"name": "Ivan"})
    assert response.status_code == 200
    with SessionLocal() as db:
        event = db.get(Event, response.json()["event_id"])
        assert event.delivery_status == DeliveryStatus.FAILED.value
        assert "timed out" in event.delivery_error


def test_invalid_template_does_not_break_webhook(client: TestClient, telegram: FakeTelegram) -> None:
    inbox_id = create_inbox(client)
    _connect(client, inbox_id)
    # The UI rejects invalid templates, so write one directly to simulate a corrupted template.
    with SessionLocal() as db:
        from app.models import Inbox

        db.get(Inbox, inbox_id).message_template = "{{ broken"
        db.commit()

    response = client.post(f"/h/{hook_token(inbox_id)}", json={"name": "Ivan"})
    assert response.status_code == 200
    with SessionLocal() as db:
        event = db.get(Event, response.json()["event_id"])
        assert event.delivery_status == DeliveryStatus.FAILED.value
        assert event.delivery_error.startswith("Template syntax error")
    assert telegram.sent_messages() == []

    # Fixing the template and re-delivering from the UI works.
    client.post(f"/inboxes/{inbox_id}/template", data={"message_template": "Hi {{ name }}", "action": "save"})
    redeliver = client.post(
        f"/inboxes/{inbox_id}/events/{response.json()['event_id']}/redeliver", follow_redirects=True
    )
    assert "Re-delivered: sent" in redeliver.text
    assert telegram.sent_messages()[-1]["text"] == "Hi Ivan"


def test_invalid_template_is_rejected_by_ui(client: TestClient) -> None:
    inbox_id = create_inbox(client)
    response = client.post(f"/inboxes/{inbox_id}/template", data={"message_template": "{% if %}", "action": "save"})
    assert response.status_code == 400
    assert "Template syntax error" in response.text
    with SessionLocal() as db:
        from app.models import Inbox

        assert db.get(Inbox, inbox_id).message_template == ""


def test_template_preview_does_not_save(client: TestClient) -> None:
    inbox_id = create_inbox(client)
    client.post(f"/h/{hook_token(inbox_id)}", json={"customer": {"name": "Acme"}})
    response = client.post(
        f"/inboxes/{inbox_id}/template",
        data={"message_template": "Customer: {{ customer.name }}", "action": "preview"},
    )
    assert response.status_code == 200
    assert "Customer: Acme" in response.text
    with SessionLocal() as db:
        from app.models import Inbox

        assert db.get(Inbox, inbox_id).message_template == ""


def test_no_bot_token_stores_events_as_not_configured(client: TestClient, telegram: FakeTelegram, no_bot_token) -> None:
    inbox_id = create_inbox(client)
    response = client.post(f"/h/{hook_token(inbox_id)}", json={"name": "Ivan"})
    with SessionLocal() as db:
        event = db.get(Event, response.json()["event_id"])
        assert event.delivery_status == DeliveryStatus.NOT_CONFIGURED.value
        assert "Ivan" in event.rendered_message


def test_bot_token_never_appears_in_logs(client: TestClient, telegram: FakeTelegram, caplog) -> None:
    from tests.conftest import FAKE_BOT_TOKEN

    telegram.raise_network_error = True
    inbox_id = create_inbox(client)
    _connect(client, inbox_id)
    with caplog.at_level(logging.DEBUG):
        client.post(f"/h/{hook_token(inbox_id)}", json={"name": "Ivan"})
        client.post(f"/inboxes/{inbox_id}/telegram/test")
    assert caplog.text  # something was logged about the failure
    assert FAKE_BOT_TOKEN not in caplog.text
