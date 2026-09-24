"""Webhook signature verification: generic HMAC-SHA256 with timestamp tolerance.

Scheme ("Generic HMAC-SHA256")::

    signed_payload = f"{timestamp}." + raw_body          # bytes, body exactly as received
    signature      = HMAC_SHA256(secret, signed_payload)

    X-Timestamp: 1767225600                              # unix seconds (milliseconds accepted)
    X-Signature: sha256=<hex>                            # bare hex / base64 accepted too

Several signatures may be sent comma- or space-separated (useful while rotating secrets);
the request is valid if any of them matches. Header names and tolerance are per inbox, and
the whole scheme is a plain dataclass so provider presets can be added as data later.

Everything here is pure (no DB, no HTTP) and takes ``now`` explicitly, so it is easy to test.
"""

from __future__ import annotations

import base64
import binascii
import enum
import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass

from app.models import Inbox

MIN_TOLERANCE_SECONDS = 30
MAX_TOLERANCE_SECONDS = 3600
DEFAULT_TOLERANCE_SECONDS = 300
MIN_SECRET_LENGTH = 16
MAX_SECRET_LENGTH = 256
GENERATED_SECRET_PREFIX = "whsec_"
MAX_SIGNATURES_PER_HEADER = 5

_HEADER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
_HEX_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_MILLIS_THRESHOLD = 10**11  # anything above is a millisecond timestamp (year > 5138 in seconds)


class VerificationMode(enum.StrEnum):
    NONE = "none"
    HMAC_SHA256 = "hmac_sha256"


VERIFICATION_LABELS = {
    VerificationMode.NONE.value: "None",
    VerificationMode.HMAC_SHA256.value: "HMAC SHA-256",
}


class RejectReason(enum.StrEnum):
    NO_SECRET = "no_secret"
    MISSING_SIGNATURE = "missing_signature"
    MISSING_TIMESTAMP = "missing_timestamp"
    INVALID_TIMESTAMP = "invalid_timestamp"
    STALE_TIMESTAMP = "stale_timestamp"
    MALFORMED_SIGNATURE = "malformed_signature"
    SIGNATURE_MISMATCH = "signature_mismatch"
    REPLAY = "replay"


class SignatureError(Exception):
    """Verification failed. ``message`` is safe to return to the sender (no secrets, no expected values)."""

    def __init__(self, reason: RejectReason, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class HmacScheme:
    signature_header: str = "X-Signature"
    timestamp_header: str = "X-Timestamp"
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS
    signature_prefix: str = "sha256="

    @classmethod
    def for_inbox(cls, inbox: Inbox) -> HmacScheme:
        return cls(
            signature_header=inbox.signature_header,
            timestamp_header=inbox.timestamp_header,
            tolerance_seconds=inbox.timestamp_tolerance_seconds,
        )


@dataclass(frozen=True, slots=True)
class Verified:
    timestamp: int
    """Unix seconds taken from the timestamp header."""
    digest: str
    """Hex digest of the matching signature — the replay-protection key."""


def generate_secret() -> str:
    return GENERATED_SECRET_PREFIX + secrets.token_urlsafe(32)


def signed_payload(timestamp: str, body: bytes) -> bytes:
    return timestamp.encode("ascii") + b"." + body


def compute_signature(secret: str, timestamp: str, body: bytes) -> str:
    """Header value a sender should put into the signature header."""
    mac = hmac.new(secret.encode("utf-8"), signed_payload(timestamp, body), hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify(
    scheme: HmacScheme,
    secret: str | None,
    headers: Mapping[str, str],
    body: bytes,
    now: float | None = None,
) -> Verified:
    """Verify ``body`` against the headers or raise :class:`SignatureError`.

    ``headers`` must be case-insensitive (Starlette's ``Headers`` is).
    """
    if not secret:
        raise SignatureError(RejectReason.NO_SECRET, "Signature verification is enabled but no secret is configured")

    raw_signature = headers.get(scheme.signature_header)
    if not raw_signature or not raw_signature.strip():
        raise SignatureError(RejectReason.MISSING_SIGNATURE, f"Missing {scheme.signature_header} header")

    raw_timestamp = headers.get(scheme.timestamp_header)
    if raw_timestamp is None or not raw_timestamp.strip():
        raise SignatureError(RejectReason.MISSING_TIMESTAMP, f"Missing {scheme.timestamp_header} header")
    raw_timestamp = raw_timestamp.strip()

    timestamp = _parse_timestamp(raw_timestamp, scheme.timestamp_header)
    now = time.time() if now is None else now
    if abs(now - timestamp) > scheme.tolerance_seconds:
        raise SignatureError(
            RejectReason.STALE_TIMESTAMP,
            f"Timestamp outside the allowed window of {scheme.tolerance_seconds}s",
        )

    candidates = _parse_signatures(raw_signature, scheme.signature_prefix)
    if not candidates:
        raise SignatureError(
            RejectReason.MALFORMED_SIGNATURE,
            f"Malformed {scheme.signature_header} header: expected sha256=<hex>",
        )

    expected = hmac.new(secret.encode("utf-8"), signed_payload(raw_timestamp, body), hashlib.sha256).digest()
    # Compare against every candidate without short-circuiting on the first mismatch.
    matched = False
    for candidate in candidates:
        matched |= hmac.compare_digest(candidate, expected)
    if not matched:
        raise SignatureError(RejectReason.SIGNATURE_MISMATCH, "Invalid signature")

    return Verified(timestamp=timestamp, digest=expected.hex())


def _parse_timestamp(value: str, header: str) -> int:
    if not value.isascii() or not value.isdigit() or len(value) > 16:
        raise SignatureError(RejectReason.INVALID_TIMESTAMP, f"{header} must be a unix timestamp in seconds")
    timestamp = int(value)
    if timestamp > _MILLIS_THRESHOLD:
        timestamp //= 1000
    return timestamp


def _parse_signatures(value: str, prefix: str) -> list[bytes]:
    """Split a header into raw digests; silently skip parts that cannot be a SHA-256 digest."""
    digests: list[bytes] = []
    for part in re.split(r"[\s,]+", value.strip())[:MAX_SIGNATURES_PER_HEADER]:
        token = part[len(prefix) :] if part.lower().startswith(prefix.lower()) else part
        digest = _decode_digest(token)
        if digest is not None:
            digests.append(digest)
    return digests


def _decode_digest(token: str) -> bytes | None:
    if _HEX_SHA256.fullmatch(token):
        return bytes.fromhex(token)
    normalized = token.replace("-", "+").replace("_", "/").rstrip("=")  # accept url-safe, unpadded
    try:
        decoded = base64.b64decode(normalized + "=" * (-len(normalized) % 4), validate=True)
    except (binascii.Error, ValueError):
        return None
    return decoded if len(decoded) == hashlib.sha256().digest_size else None


# --- settings validation (used by the UI) -------------------------------------------------


def validate_header_name(value: str) -> str | None:
    if not _HEADER_NAME.fullmatch(value):
        return "Header names may contain only letters, digits and dashes (max 64 characters)."
    return None


def validate_secret(value: str) -> str | None:
    if not (MIN_SECRET_LENGTH <= len(value) <= MAX_SECRET_LENGTH):
        return f"Secret must be {MIN_SECRET_LENGTH} to {MAX_SECRET_LENGTH} characters long."
    if any(ch.isspace() for ch in value) or not value.isprintable():
        return "Secret must not contain spaces or control characters."
    return None
