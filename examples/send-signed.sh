#!/usr/bin/env sh
# Send an HMAC-SHA256 signed payload to a HookPing inbox with signature verification enabled.
#
#   HOOKPING_SECRET=whsec_... ./examples/send-signed.sh <HOOK_TOKEN> [payload.json] [base_url]
#
# Signs "<unix timestamp>.<raw body>" and sends X-Timestamp / X-Signature: sha256=<hex>.
# Override header names with SIGNATURE_HEADER / TIMESTAMP_HEADER if the inbox uses custom ones.
set -eu

TOKEN="${1:?usage: HOOKPING_SECRET=... send-signed.sh <HOOK_TOKEN> [payload.json] [base_url]}"
PAYLOAD="${2:-examples/payment.json}"
BASE_URL="${3:-http://localhost:8000}"
SECRET="${HOOKPING_SECRET:?set HOOKPING_SECRET to the inbox signing secret}"
SIGNATURE_HEADER="${SIGNATURE_HEADER:-X-Signature}"
TIMESTAMP_HEADER="${TIMESTAMP_HEADER:-X-Timestamp}"

TS=$(date +%s)
# Sign the file's exact bytes: printf + cat avoids any shell re-encoding of the body.
SIG=$( { printf '%s.' "$TS"; cat "$PAYLOAD"; } | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.* //')

curl -sS -X POST "$BASE_URL/h/$TOKEN" \
  -H "Content-Type: application/json" \
  -H "$TIMESTAMP_HEADER: $TS" \
  -H "$SIGNATURE_HEADER: sha256=$SIG" \
  --data-binary "@$PAYLOAD"
echo
