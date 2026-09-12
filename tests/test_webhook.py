from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import DeliveryStatus, Event
from tests.conftest import create_inbox, hook_token


def test_valid_json_is_accepted_and_stored(client: TestClient) -> None:
    inbox_id = create_inbox(client)
    payload = {"customer": {"name": "Acme Ltd"}, "amount": 7500, "currency": "EUR"}
    response = client.post(f"/h/{hook_token(inbox_id)}", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["received"] is True

    with SessionLocal() as db:
        event = db.get(Event, body["event_id"])
        assert event is not None
        assert event.inbox_id == inbox_id
        assert json.loads(event.payload) == payload
        assert event.method == "POST"
        assert event.content_type.startswith("application/json")
        # No Telegram chat linked yet -> nothing to deliver, but the payload is kept.
        assert event.delivery_status == DeliveryStatus.NOT_CONFIGURED.value
        assert "Acme Ltd" in event.rendered_message


def test_unknown_token_returns_404(client: TestClient) -> None:
    response = client.post("/h/not-a-real-token", json={"a": 1})
    assert response.status_code == 404
    assert response.json() == {"detail": "Unknown hook token"}


def test_malformed_json_returns_400(client: TestClient) -> None:
    inbox_id = create_inbox(client)
    response = client.post(
        f"/h/{hook_token(inbox_id)}", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "Body is not valid JSON"}
    with SessionLocal() as db:
        assert db.query(Event).count() == 0


def test_empty_body_returns_400(client: TestClient) -> None:
    inbox_id = create_inbox(client)
    response = client.post(f"/h/{hook_token(inbox_id)}", content=b"", headers={"Content-Type": "application/json"})
    assert response.status_code == 400


def test_too_large_payload_returns_413(client: TestClient) -> None:
    inbox_id = create_inbox(client)
    big = json.dumps({"blob": "x" * (70 * 1024)}).encode()
    response = client.post(f"/h/{hook_token(inbox_id)}", content=big, headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert "too large" in response.json()["detail"].lower()
    with SessionLocal() as db:
        assert db.query(Event).count() == 0


def test_too_large_payload_without_content_length_returns_413(client: TestClient) -> None:
    inbox_id = create_inbox(client)

    def chunks():
        for _ in range(80):
            yield b"x" * 1024

    response = client.post(
        f"/h/{hook_token(inbox_id)}",
        content=chunks(),
        headers={"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
    )
    assert response.status_code == 413


def test_form_encoded_body_is_accepted(client: TestClient) -> None:
    inbox_id = create_inbox(client)
    response = client.post(f"/h/{hook_token(inbox_id)}", data={"name": "Ivan", "plan": "Pro"})
    assert response.status_code == 200
    with SessionLocal() as db:
        event = db.get(Event, response.json()["event_id"])
        assert json.loads(event.payload) == {"name": "Ivan", "plan": "Pro"}


def test_non_object_json_is_stored(client: TestClient) -> None:
    inbox_id = create_inbox(client)
    response = client.post(f"/h/{hook_token(inbox_id)}", json=[1, 2, 3])
    assert response.status_code == 200
    with SessionLocal() as db:
        event = db.get(Event, response.json()["event_id"])
        assert json.loads(event.payload) == [1, 2, 3]
        assert "[" in event.rendered_message  # default template prints the payload


def test_events_appear_in_ui(client: TestClient) -> None:
    inbox_id = create_inbox(client)
    response = client.post(f"/h/{hook_token(inbox_id)}", json={"customer": {"name": "Acme"}, "amount": 100})
    event_id = response.json()["event_id"]

    inbox_page = client.get(f"/inboxes/{inbox_id}")
    assert event_id in inbox_page.text
    assert "customer.name" in inbox_page.text  # available fields
    assert "amount" in inbox_page.text

    event_page = client.get(f"/inboxes/{inbox_id}/events/{event_id}")
    assert event_page.status_code == 200
    assert "&#34;name&#34;: &#34;Acme&#34;" in event_page.text  # pretty-printed JSON
    assert "not configured" in event_page.text


def test_old_events_are_pruned(client: TestClient, monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "max_events_per_inbox", 5)
    inbox_id = create_inbox(client)
    token = hook_token(inbox_id)
    for i in range(8):
        client.post(f"/h/{token}", json={"n": i})
    with SessionLocal() as db:
        events = db.query(Event).order_by(Event.received_at.desc()).all()
        assert len(events) == 5
        assert json.loads(events[0].payload) == {"n": 7}
