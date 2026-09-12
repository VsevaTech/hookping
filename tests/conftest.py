"""Shared fixtures: isolated SQLite database, TestClient, mocked Telegram API."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

FAKE_BOT_TOKEN = "123456789:TEST-FAKE-TOKEN-never-real"
TELEGRAM_API_BASE = "http://telegram.test"

_TMP_DIR = tempfile.mkdtemp(prefix="hookping-tests-")
os.environ["HOOKPING_DATABASE_URL"] = f"sqlite:///{_TMP_DIR}/test.db"
os.environ["TELEGRAM_BOT_TOKEN"] = FAKE_BOT_TOKEN
os.environ["HOOKPING_TELEGRAM_API_BASE"] = TELEGRAM_API_BASE
os.environ["HOOKPING_TELEGRAM_POLLING"] = "false"
os.environ["HOOKPING_MAX_BODY_BYTES"] = str(64 * 1024)
os.environ["PUBLIC_BASE_URL"] = "http://hookping.test"

from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services.telegram import TelegramClient  # noqa: E402


@pytest.fixture(autouse=True)
def clean_database() -> Iterator[None]:
    import app.models  # noqa: F401 - register tables

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@dataclass
class FakeTelegram:
    """Programmable stand-in for api.telegram.org, wired in through httpx.MockTransport."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    status_code: int = 200
    ok: bool = True
    description: str = ""
    raise_timeout: bool = False
    raise_network_error: bool = False
    updates: list[dict[str, Any]] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content or b"{}")
        self.calls.append({"method": method, "payload": payload, "url": str(request.url)})
        if self.raise_timeout:
            raise httpx.ReadTimeout("simulated timeout", request=request)
        if self.raise_network_error:
            raise httpx.ConnectError("simulated connection failure", request=request)
        if not self.ok or self.status_code != 200:
            return httpx.Response(
                self.status_code,
                json={"ok": False, "error_code": self.status_code, "description": self.description or "Error"},
            )
        if method == "sendMessage":
            result: Any = {"message_id": len(self.calls), "chat": {"id": payload.get("chat_id")}}
        elif method == "getMe":
            result = {"id": 1, "is_bot": True, "username": "hook_ping_bot"}
        elif method == "getUpdates":
            offset = payload.get("offset")
            result = [u for u in self.updates if offset is None or u["update_id"] >= offset]
        else:
            result = True
        return httpx.Response(200, json={"ok": True, "result": result})

    def sent_messages(self) -> list[dict[str, Any]]:
        return [c["payload"] for c in self.calls if c["method"] == "sendMessage"]

    def make_client(self) -> TelegramClient:
        return TelegramClient(
            bot_token=FAKE_BOT_TOKEN,
            api_base=TELEGRAM_API_BASE,
            timeout=1.0,
            transport=httpx.MockTransport(self.handler),
            async_transport=httpx.MockTransport(self.handler),
        )


@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch) -> FakeTelegram:
    fake = FakeTelegram()
    monkeypatch.setattr("app.services.delivery.get_telegram_client", fake.make_client)
    monkeypatch.setattr("app.routers.ui.get_telegram_client", fake.make_client)
    return fake


@pytest.fixture
def no_bot_token(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Simulate a server started without TELEGRAM_BOT_TOKEN."""
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    yield


def create_inbox(client: TestClient, name: str = "Sales Leads") -> str:
    response = client.post("/inboxes", data={"name": name}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return response.headers["location"].split("/inboxes/")[1].split("?")[0]


def hook_token(inbox_id: str) -> str:
    from app.database import SessionLocal
    from app.models import Inbox

    with SessionLocal() as db:
        inbox = db.get(Inbox, inbox_id)
        assert inbox is not None
        return inbox.hook_token


def telegram_update(text: str, chat_id: int = 424242, update_id: int = 1, **chat_extra: Any) -> dict[str, Any]:
    chat = {"id": chat_id, "type": "private", "first_name": "Seva", "username": "seva_test", **chat_extra}
    return {"update_id": update_id, "message": {"message_id": update_id, "chat": chat, "text": text}}


REPO_ROOT = Path(__file__).resolve().parent.parent
