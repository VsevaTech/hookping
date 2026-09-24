"""End-to-end HMAC verification through POST /h/{token} and the inbox UI."""

from __future__ import annotations

import json
import re
import time

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import Event, Inbox, InboxSigningSecret, SeenSignature
from app.services import signatures
from tests.conftest import FakeTelegram, create_inbox, hook_token

PASTED_SECRET = "whsec_pasted-from-provider-0123456789"
BODY = json.dumps({"customer": {"name": "Acme Ltd"}, "amount": 7500}).encode()


def signed_headers(secret: str, body: bytes, ts: int | None = None, **names: str) -> dict[str, str]:
    stamp = str(int(time.time()) if ts is None else ts)
    return {
        "Content-Type": "application/json",
        names.get("timestamp_header", "X-Timestamp"): stamp,
        names.get("signature_header", "X-Signature"): signatures.compute_signature(secret, stamp, body),
    }


def enable_hmac(client: TestClient, inbox_id: str, secret: str = PASTED_SECRET, **fields: str) -> None:
    saved = client.post(
        f"/inboxes/{inbox_id}/verification/secret",
        data={"action": "save", "secret": secret},
        follow_redirects=False,
    )
    assert saved.status_code == 303, saved.text
    form = {
        "verification_mode": "hmac_sha256",
        "signature_header": "X-Signature",
        "timestamp_header": "X-Timestamp",
        "timestamp_tolerance_seconds": "300",
        **fields,
    }
    enabled = client.post(f"/inboxes/{inbox_id}/verification", data=form, follow_redirects=False)
    assert enabled.status_code == 303, enabled.text


def event_count() -> int:
    with SessionLocal() as db:
        return db.query(Event).count()


@pytest.fixture
def signed_inbox(client: TestClient) -> tuple[str, str]:
    inbox_id = create_inbox(client, "Payments")
    enable_hmac(client, inbox_id)
    return inbox_id, hook_token(inbox_id)


def test_new_inbox_has_no_verification(client: TestClient) -> None:
    inbox_id = create_inbox(client)
    page = client.get(f"/inboxes/{inbox_id}")
    assert "Verification: None" in page.text
    assert client.post(f"/h/{hook_token(inbox_id)}", content=BODY).status_code == 200
    with SessionLocal() as db:
        assert db.query(Event).one().verification == "none"


def test_signed_request_is_accepted_and_marked_verified(client: TestClient, signed_inbox) -> None:
    inbox_id, token = signed_inbox
    response = client.post(f"/h/{token}", content=BODY, headers=signed_headers(PASTED_SECRET, BODY))
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        event = db.get(Event, response.json()["event_id"])
        assert event.verification == "hmac_sha256"
        assert json.loads(event.payload)["amount"] == 7500
    assert "🔏 signed" in client.get(f"/inboxes/{inbox_id}").text
    assert "🔏 verified" in client.get(f"/inboxes/{inbox_id}/events/{event.id}").text


@pytest.mark.parametrize(
    ("mutate", "detail"),
    [
        (lambda h: h.pop("X-Signature"), "Missing X-Signature header"),
        (lambda h: h.pop("X-Timestamp"), "Missing X-Timestamp header"),
        (lambda h: h.update({"X-Signature": "sha256=" + "0" * 64}), "Invalid signature"),
        (lambda h: h.update({"X-Signature": "not-a-signature"}), "Malformed X-Signature header"),
        (lambda h: h.update({"X-Timestamp": "yesterday"}), "X-Timestamp must be a unix timestamp"),
    ],
)
def test_bad_signatures_return_401_and_store_nothing(client: TestClient, signed_inbox, mutate, detail) -> None:
    inbox_id, token = signed_inbox
    headers = signed_headers(PASTED_SECRET, BODY)
    mutate(headers)
    response = client.post(f"/h/{token}", content=BODY, headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"].startswith(detail)
    assert event_count() == 0
    page = client.get(f"/inboxes/{inbox_id}").text
    assert "Last rejected request" in page
    assert detail.split()[0] in page


def test_tampered_body_is_rejected(client: TestClient, signed_inbox) -> None:
    _, token = signed_inbox
    headers = signed_headers(PASTED_SECRET, BODY)
    response = client.post(f"/h/{token}", content=BODY.replace(b"7500", b"1"), headers=headers)
    assert response.status_code == 401
    assert event_count() == 0


def test_stale_timestamp_is_rejected(client: TestClient, signed_inbox) -> None:
    _, token = signed_inbox
    old = int(time.time()) - 301
    response = client.post(f"/h/{token}", content=BODY, headers=signed_headers(PASTED_SECRET, BODY, ts=old))
    assert response.status_code == 401
    assert "outside the allowed window of 300s" in response.json()["detail"]


def test_future_timestamp_beyond_tolerance_is_rejected(client: TestClient, signed_inbox) -> None:
    _, token = signed_inbox
    future = int(time.time()) + 3600
    response = client.post(f"/h/{token}", content=BODY, headers=signed_headers(PASTED_SECRET, BODY, ts=future))
    assert response.status_code == 401


def test_replayed_request_returns_409_and_is_stored_once(client: TestClient, signed_inbox) -> None:
    inbox_id, token = signed_inbox
    headers = signed_headers(PASTED_SECRET, BODY)
    assert client.post(f"/h/{token}", content=BODY, headers=headers).status_code == 200
    replay = client.post(f"/h/{token}", content=BODY, headers=headers)
    assert replay.status_code == 409
    assert "already accepted" in replay.json()["detail"]
    assert event_count() == 1
    assert "already accepted" in client.get(f"/inboxes/{inbox_id}").text


def test_same_body_with_new_timestamp_is_not_a_replay(client: TestClient, signed_inbox) -> None:
    _, token = signed_inbox
    now = int(time.time())
    for ts in (now - 1, now):
        response = client.post(f"/h/{token}", content=BODY, headers=signed_headers(PASTED_SECRET, BODY, ts=ts))
        assert response.status_code == 200
    assert event_count() == 2


def test_replay_guard_is_per_inbox(client: TestClient) -> None:
    first, second = create_inbox(client, "A"), create_inbox(client, "B")
    enable_hmac(client, first)
    enable_hmac(client, second)
    headers = signed_headers(PASTED_SECRET, BODY)
    assert client.post(f"/h/{hook_token(first)}", content=BODY, headers=headers).status_code == 200
    assert client.post(f"/h/{hook_token(second)}", content=BODY, headers=headers).status_code == 200


def test_invalid_json_with_valid_signature_does_not_burn_the_signature(client: TestClient, signed_inbox) -> None:
    _, token = signed_inbox
    bad = b"{not json"
    headers = signed_headers(PASTED_SECRET, bad)
    assert client.post(f"/h/{token}", content=bad, headers=headers).status_code == 400
    with SessionLocal() as db:
        assert db.query(SeenSignature).count() == 0


def test_expired_replay_records_are_pruned(client: TestClient, signed_inbox) -> None:
    inbox_id, token = signed_inbox
    from datetime import UTC, datetime, timedelta

    with SessionLocal() as db:
        db.add(SeenSignature(inbox_id=inbox_id, digest="a" * 64, expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        db.commit()
    assert client.post(f"/h/{token}", content=BODY, headers=signed_headers(PASTED_SECRET, BODY)).status_code == 200
    with SessionLocal() as db:
        digests = [row.digest for row in db.query(SeenSignature).all()]
        assert "a" * 64 not in digests
        assert len(digests) == 1


def test_custom_headers_and_tolerance(client: TestClient) -> None:
    inbox_id = create_inbox(client)
    enable_hmac(
        client,
        inbox_id,
        signature_header="X-Acme-Signature",
        timestamp_header="X-Acme-Timestamp",
        timestamp_tolerance_seconds="60",
    )
    token = hook_token(inbox_id)
    names = {"signature_header": "X-Acme-Signature", "timestamp_header": "X-Acme-Timestamp"}
    ok = client.post(f"/h/{token}", content=BODY, headers=signed_headers(PASTED_SECRET, BODY, **names))
    assert ok.status_code == 200
    stale = int(time.time()) - 61
    rejected = client.post(f"/h/{token}", content=BODY, headers=signed_headers(PASTED_SECRET, BODY, ts=stale, **names))
    assert rejected.status_code == 401
    # Default header names are no longer consulted.
    assert client.post(f"/h/{token}", content=BODY, headers=signed_headers(PASTED_SECRET, BODY)).status_code == 401


def test_generated_secret_is_shown_exactly_once(client: TestClient) -> None:
    inbox_id = create_inbox(client)
    response = client.post(f"/inboxes/{inbox_id}/verification/secret", data={"action": "generate"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    match = re.search(r'id="revealed-secret">(whsec_[A-Za-z0-9_-]+)<', response.text)
    assert match, "secret must be revealed on the generate response"
    secret = match.group(1)

    later = client.get(f"/inboxes/{inbox_id}")
    assert secret not in later.text
    assert "Secret set" in later.text

    enable_hmac_form = {
        "verification_mode": "hmac_sha256",
        "signature_header": "X-Signature",
        "timestamp_header": "X-Timestamp",
        "timestamp_tolerance_seconds": "300",
    }
    assert client.post(f"/inboxes/{inbox_id}/verification", data=enable_hmac_form).status_code == 200
    ok = client.post(f"/h/{hook_token(inbox_id)}", content=BODY, headers=signed_headers(secret, BODY))
    assert ok.status_code == 200


def test_rotating_the_secret_invalidates_the_old_one(client: TestClient, signed_inbox) -> None:
    inbox_id, token = signed_inbox
    new_secret = "whsec_rotated-secret-9876543210"
    client.post(f"/inboxes/{inbox_id}/verification/secret", data={"action": "save", "secret": new_secret})
    assert client.post(f"/h/{token}", content=BODY, headers=signed_headers(PASTED_SECRET, BODY)).status_code == 401
    assert client.post(f"/h/{token}", content=BODY, headers=signed_headers(new_secret, BODY)).status_code == 200


def test_cannot_enable_hmac_without_secret(client: TestClient) -> None:
    inbox_id = create_inbox(client)
    response = client.post(
        f"/inboxes/{inbox_id}/verification",
        data={
            "verification_mode": "hmac_sha256",
            "signature_header": "X-Signature",
            "timestamp_header": "X-Timestamp",
            "timestamp_tolerance_seconds": "300",
        },
    )
    assert response.status_code == 400
    assert "Set a signing secret" in response.text
    with SessionLocal() as db:
        assert db.get(Inbox, inbox_id).verification_mode == "none"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("verification_mode", "rsa", "Unknown verification mode"),
        ("signature_header", "X Bad", "Header names may contain"),
        ("timestamp_header", "X-Signature", "must be different"),
        ("timestamp_tolerance_seconds", "5", "between 30 and 3600"),
        ("timestamp_tolerance_seconds", "abc", "between 30 and 3600"),
        ("timestamp_tolerance_seconds", "7200", "between 30 and 3600"),
    ],
)
def test_verification_settings_are_validated(client: TestClient, field: str, value: str, message: str) -> None:
    inbox_id = create_inbox(client)
    client.post(f"/inboxes/{inbox_id}/verification/secret", data={"action": "save", "secret": PASTED_SECRET})
    form = {
        "verification_mode": "hmac_sha256",
        "signature_header": "X-Signature",
        "timestamp_header": "X-Timestamp",
        "timestamp_tolerance_seconds": "300",
        field: value,
    }
    response = client.post(f"/inboxes/{inbox_id}/verification", data=form)
    assert response.status_code == 400
    assert message in response.text


def test_invalid_pasted_secret_is_rejected(client: TestClient) -> None:
    inbox_id = create_inbox(client)
    response = client.post(f"/inboxes/{inbox_id}/verification/secret", data={"action": "save", "secret": "short"})
    assert response.status_code == 400
    with SessionLocal() as db:
        assert db.get(InboxSigningSecret, inbox_id) is None


def test_switching_back_to_none_accepts_unsigned_again(client: TestClient, signed_inbox) -> None:
    inbox_id, token = signed_inbox
    assert client.post(f"/h/{token}", content=BODY).status_code == 401
    client.post(
        f"/inboxes/{inbox_id}/verification",
        data={
            "verification_mode": "none",
            "signature_header": "X-Signature",
            "timestamp_header": "X-Timestamp",
            "timestamp_tolerance_seconds": "300",
        },
    )
    assert client.post(f"/h/{token}", content=BODY).status_code == 200
    with SessionLocal() as db:
        assert db.get(InboxSigningSecret, inbox_id) is not None  # kept for re-enabling


def test_removing_the_secret_turns_verification_off(client: TestClient, signed_inbox) -> None:
    inbox_id, token = signed_inbox
    response = client.post(f"/inboxes/{inbox_id}/verification/secret/delete", follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        inbox = db.get(Inbox, inbox_id)
        assert inbox.verification_mode == "none"
        assert inbox.signing_secret_set_at is None
        assert db.get(InboxSigningSecret, inbox_id) is None
    assert client.post(f"/h/{token}", content=BODY).status_code == 200


def test_deleting_inbox_deletes_secret_and_replay_records(client: TestClient, signed_inbox) -> None:
    inbox_id, token = signed_inbox
    client.post(f"/h/{token}", content=BODY, headers=signed_headers(PASTED_SECRET, BODY))
    client.post(f"/inboxes/{inbox_id}/delete")
    with SessionLocal() as db:
        assert db.query(InboxSigningSecret).count() == 0
        assert db.query(SeenSignature).count() == 0


def test_verified_event_is_delivered_to_telegram(client: TestClient, telegram: FakeTelegram, signed_inbox) -> None:
    inbox_id, token = signed_inbox
    client.post(f"/inboxes/{inbox_id}/telegram/manual", data={"telegram_chat_id": "424242"})
    response = client.post(f"/h/{token}", content=BODY, headers=signed_headers(PASTED_SECRET, BODY))
    assert response.status_code == 200
    assert len(telegram.sent_messages()) == 1
    client.post(f"/h/{token}", content=BODY, headers={"Content-Type": "application/json"})
    assert len(telegram.sent_messages()) == 1  # rejected requests never reach Telegram


def test_rejection_logs_reason_but_never_secret_or_signature(client: TestClient, signed_inbox, caplog) -> None:
    _, token = signed_inbox
    headers = signed_headers("whsec_wrong-secret-000000000000", BODY)
    with caplog.at_level("DEBUG"):
        client.post(f"/h/{token}", content=BODY, headers=headers)
    assert "signature_mismatch" in caplog.text
    assert PASTED_SECRET not in caplog.text
    assert headers["X-Signature"] not in caplog.text
