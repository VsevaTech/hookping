"""Secrets hygiene: the bot token never leaves the server, and never lands in the repository."""

from __future__ import annotations

import re
import subprocess

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import TelegramConnection
from app.models.telegram_connection import new_connection_token
from tests.conftest import FAKE_BOT_TOKEN, REPO_ROOT, create_inbox, hook_token

TOKEN_ASSIGNMENT = re.compile(r"TELEGRAM_BOT_TOKEN\s*[=:]\s*\S+")


def test_bot_token_not_exposed_by_http_surface(client: TestClient) -> None:
    inbox_id = create_inbox(client)
    client.post(f"/inboxes/{inbox_id}/telegram/connect")
    for path in (
        "/",
        f"/inboxes/{inbox_id}",
        f"/api/inboxes/{inbox_id}/telegram/status",
        "/openapi.json",
        "/health",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert FAKE_BOT_TOKEN not in response.text, path
        assert "123456789:" not in response.text, path


def test_connection_tokens_are_random_and_long() -> None:
    tokens = {new_connection_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(len(t) >= 32 for t in tokens)
    assert all(re.fullmatch(r"[A-Za-z0-9_-]+", t) for t in tokens)
    assert all(len(t) <= 64 for t in tokens)  # Telegram's /start parameter limit


def test_connection_token_has_ttl(client: TestClient) -> None:
    inbox_id = create_inbox(client)
    client.post(f"/inboxes/{inbox_id}/telegram/connect")
    with SessionLocal() as db:
        connection = db.query(TelegramConnection).filter_by(inbox_id=inbox_id).one()
        ttl = connection.expires_at - connection.created_at
        assert 9 * 60 <= ttl.total_seconds() <= 10 * 60 + 5
        assert connection.used_at is None


def _tracked_or_all_files() -> list[str]:
    try:
        output = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT, check=True, capture_output=True, text=True).stdout
        files = [line for line in output.splitlines() if line]
        if files:
            return files
    except (OSError, subprocess.CalledProcessError):
        pass
    skip = {".git", ".venv", "data", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}
    return [
        str(p.relative_to(REPO_ROOT))
        for p in REPO_ROOT.rglob("*")
        if p.is_file() and not (set(p.relative_to(REPO_ROOT).parts) & skip)
    ]


def test_repository_contains_no_bot_token() -> None:
    files = _tracked_or_all_files()
    assert ".env" not in files, ".env must never be committed"
    assert ".env.example" in files
    for relative in files:
        path = REPO_ROOT / relative
        if not path.exists() or path.suffix in {".png", ".jpg", ".ico", ".db"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if relative == ".env.example":
            assert "TELEGRAM_BOT_TOKEN=\n" in text
        for match in TOKEN_ASSIGNMENT.finditer(text):
            value = match.group(0).split("=", 1)[-1].split(":", 1)[-1].strip()
            assert not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{30,}", value), f"real-looking token in {relative}"
        assert not re.search(r"\b\d{8,10}:AA[A-Za-z0-9_-]{33}\b", text), f"Telegram token pattern in {relative}"


def test_gitignore_covers_secrets_and_artifacts() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text().splitlines()
    for required in (".env", "*.db", "__pycache__/", ".pytest_cache/", "data/"):
        assert required in gitignore, required


def test_signing_secret_never_exposed_by_http_surface(client: TestClient) -> None:
    secret = "whsec_never-show-me-again-0123456789"
    inbox_id = create_inbox(client)
    client.post(f"/inboxes/{inbox_id}/verification/secret", data={"action": "save", "secret": secret})
    client.post(
        f"/inboxes/{inbox_id}/verification",
        data={
            "verification_mode": "hmac_sha256",
            "signature_header": "X-Signature",
            "timestamp_header": "X-Timestamp",
            "timestamp_tolerance_seconds": "300",
        },
    )
    for path in ("/", f"/inboxes/{inbox_id}", f"/api/inboxes/{inbox_id}/telegram/status", "/openapi.json"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert secret not in response.text, path
    # Rejections do not echo anything secret-derived either.
    rejected = client.post(f"/h/{hook_token(inbox_id)}", json={"a": 1})
    assert rejected.status_code == 401
    assert secret not in rejected.text


def test_signing_secret_repr_is_redacted() -> None:
    from app.models import InboxSigningSecret

    assert "super-secret" not in repr(InboxSigningSecret(inbox_id="x", secret="super-secret"))
