"""Unit tests for the pure HMAC-SHA256 verifier."""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest
from starlette.datastructures import Headers

from app.services import signatures
from app.services.signatures import HmacScheme, RejectReason, SignatureError

SECRET = "whsec_unit-test-secret-0123456789"
BODY = b'{"amount": 7500, "currency": "EUR"}'
NOW = 1_767_225_600
SCHEME = HmacScheme()


def headers(signature: str | None, timestamp: str | None = str(NOW), **extra: str) -> Headers:
    raw: dict[str, str] = dict(extra)
    if signature is not None:
        raw["X-Signature"] = signature
    if timestamp is not None:
        raw["X-Timestamp"] = timestamp
    return Headers(raw)


def raw_digest(secret: str = SECRET, timestamp: str = str(NOW), body: bytes = BODY) -> bytes:
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).digest()


def reason_of(**kwargs) -> RejectReason:
    with pytest.raises(SignatureError) as info:
        signatures.verify(**{"scheme": SCHEME, "secret": SECRET, "body": BODY, "now": NOW, **kwargs})
    return info.value.reason


def test_valid_signature_is_accepted() -> None:
    sig = signatures.compute_signature(SECRET, str(NOW), BODY)
    assert sig == "sha256=" + raw_digest().hex()
    result = signatures.verify(SCHEME, SECRET, headers(sig), BODY, now=NOW)
    assert result.timestamp == NOW
    assert result.digest == raw_digest().hex()


@pytest.mark.parametrize(
    "encode",
    [
        lambda d: d.hex(),  # bare hex
        lambda d: "SHA256=" + d.hex().upper(),  # case-insensitive prefix and hex
        lambda d: base64.b64encode(d).decode(),  # base64 (Shopify-style)
        lambda d: base64.urlsafe_b64encode(d).decode().rstrip("="),  # url-safe, unpadded
    ],
)
def test_signature_encodings(encode) -> None:
    signatures.verify(SCHEME, SECRET, headers(encode(raw_digest())), BODY, now=NOW)


def test_any_of_several_signatures_matches_for_rotation() -> None:
    old = "sha256=" + raw_digest("whsec_old-secret-abcdefghijk").hex()
    new = "sha256=" + raw_digest().hex()
    signatures.verify(SCHEME, SECRET, headers(f"{old}, {new}"), BODY, now=NOW)


def test_header_names_are_case_insensitive_and_configurable() -> None:
    scheme = HmacScheme(signature_header="X-Acme-Signature", timestamp_header="X-Acme-Time")
    sig = signatures.compute_signature(SECRET, str(NOW), BODY)
    signatures.verify(scheme, SECRET, Headers({"x-acme-signature": sig, "X-ACME-TIME": str(NOW)}), BODY, now=NOW)


def test_millisecond_timestamps_are_accepted() -> None:
    ts = str(NOW * 1000)
    sig = signatures.compute_signature(SECRET, ts, BODY)
    assert signatures.verify(SCHEME, SECRET, headers(sig, ts), BODY, now=NOW).timestamp == NOW


def test_tampered_body_is_rejected() -> None:
    sig = signatures.compute_signature(SECRET, str(NOW), BODY)
    assert reason_of(headers=headers(sig), body=BODY.replace(b"7500", b"1")) is RejectReason.SIGNATURE_MISMATCH


def test_wrong_secret_is_rejected() -> None:
    sig = signatures.compute_signature("whsec_attacker-guess-000000", str(NOW), BODY)
    assert reason_of(headers=headers(sig)) is RejectReason.SIGNATURE_MISMATCH


def test_timestamp_is_part_of_the_signature() -> None:
    sig = signatures.compute_signature(SECRET, str(NOW), BODY)
    assert reason_of(headers=headers(sig, str(NOW + 1))) is RejectReason.SIGNATURE_MISMATCH


@pytest.mark.parametrize("offset", [-301, 301, -86_400])
def test_timestamp_outside_tolerance_is_rejected(offset: int) -> None:
    ts = str(NOW + offset)
    sig = signatures.compute_signature(SECRET, ts, BODY)
    assert reason_of(headers=headers(sig, ts)) is RejectReason.STALE_TIMESTAMP


@pytest.mark.parametrize("offset", [-300, 0, 300])
def test_timestamp_at_tolerance_edges_is_accepted(offset: int) -> None:
    ts = str(NOW + offset)
    signatures.verify(SCHEME, SECRET, headers(signatures.compute_signature(SECRET, ts, BODY), ts), BODY, now=NOW)


def test_missing_signature_header() -> None:
    assert reason_of(headers=headers(None)) is RejectReason.MISSING_SIGNATURE
    assert reason_of(headers=headers("   ")) is RejectReason.MISSING_SIGNATURE


def test_missing_timestamp_header() -> None:
    assert reason_of(headers=headers("sha256=00", timestamp=None)) is RejectReason.MISSING_TIMESTAMP


@pytest.mark.parametrize("value", ["-5", "1.5", "abc", "\\u00b2\\u00b3", "9" * 20, "2026-01-01T00:00:00Z"])
def test_invalid_timestamp(value: str) -> None:
    assert reason_of(headers=headers("sha256=" + "0" * 64, value)) is RejectReason.INVALID_TIMESTAMP


@pytest.mark.parametrize("value", ["sha256=", "sha256=zz", "sha256=abcd", "md5=" + "0" * 32, "!!!"])
def test_malformed_signature(value: str) -> None:
    assert reason_of(headers=headers(value)) is RejectReason.MALFORMED_SIGNATURE


def test_no_secret_fails_closed() -> None:
    sig = signatures.compute_signature(SECRET, str(NOW), BODY)
    assert reason_of(headers=headers(sig), secret=None) is RejectReason.NO_SECRET
    assert reason_of(headers=headers(sig), secret="") is RejectReason.NO_SECRET


def test_error_messages_never_contain_expected_signature_or_secret() -> None:
    wrong = "sha256=" + "0" * 64
    with pytest.raises(SignatureError) as info:
        signatures.verify(SCHEME, SECRET, headers(wrong), BODY, now=NOW)
    assert raw_digest().hex() not in info.value.message
    assert SECRET not in info.value.message


def test_generated_secrets_are_random_and_valid() -> None:
    generated = {signatures.generate_secret() for _ in range(100)}
    assert len(generated) == 100
    for secret in generated:
        assert secret.startswith("whsec_")
        assert signatures.validate_secret(secret) is None


@pytest.mark.parametrize("value", ["short", "x" * 257, "has space inside-123", "tab\tinside-1234567"])
def test_invalid_pasted_secrets(value: str) -> None:
    assert signatures.validate_secret(value) is not None


@pytest.mark.parametrize("value", ["X-Signature", "Stripe-Signature", "x-hub-signature-256"])
def test_valid_header_names(value: str) -> None:
    assert signatures.validate_header_name(value) is None


@pytest.mark.parametrize("value", ["", "X Signature", "X-Sig:", "-leading", "X" * 65, "X-Сигнатура"])
def test_invalid_header_names(value: str) -> None:
    assert signatures.validate_header_name(value) is not None
