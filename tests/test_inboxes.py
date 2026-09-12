from __future__ import annotations

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import Inbox
from tests.conftest import create_inbox, hook_token


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_inbox_and_show_webhook_url(client: TestClient) -> None:
    inbox_id = create_inbox(client, "Payments")
    page = client.get(f"/inboxes/{inbox_id}")
    assert page.status_code == 200
    assert "Payments" in page.text
    assert f"http://hookping.test/h/{hook_token(inbox_id)}" in page.text
    assert 'data-copy="http://hookping.test/h/' in page.text


def test_create_inbox_requires_name(client: TestClient) -> None:
    response = client.post("/inboxes", data={"name": "   "})
    assert response.status_code == 400
    assert "Name is required" in response.text


def test_hook_tokens_are_unique_and_long(client: TestClient) -> None:
    for i in range(25):
        create_inbox(client, f"Inbox {i}")
    with SessionLocal() as db:
        tokens = [inbox.hook_token for inbox in db.query(Inbox).all()]
    assert len(tokens) == 25
    assert len(set(tokens)) == 25
    assert all(len(token) >= 40 for token in tokens)
    assert all(token.replace("-", "").replace("_", "").isalnum() for token in tokens)


def test_dashboard_lists_inboxes(client: TestClient) -> None:
    create_inbox(client, "Sales Leads")
    create_inbox(client, "Production Alerts")
    page = client.get("/")
    assert page.status_code == 200
    assert "Sales Leads" in page.text
    assert "Production Alerts" in page.text
    assert "Create Inbox" in page.text


def test_delete_inbox(client: TestClient) -> None:
    inbox_id = create_inbox(client)
    response = client.post(f"/inboxes/{inbox_id}/delete", follow_redirects=False)
    assert response.status_code == 303
    assert client.get(f"/inboxes/{inbox_id}").status_code == 404


def test_unknown_inbox_renders_html_404(client: TestClient) -> None:
    response = client.get("/inboxes/doesnotexist", headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert "Inbox not found" in response.text
