#!/usr/bin/env sh
# Send an example payload to a HookPing inbox.
#
#   ./examples/send.sh <HOOK_TOKEN> [payload.json] [base_url]
#
# Example:
#   ./examples/send.sh 9x2K...token examples/payment.json http://localhost:8000
set -eu

TOKEN="${1:?usage: send.sh <HOOK_TOKEN> [payload.json] [base_url]}"
PAYLOAD="${2:-examples/payment.json}"
BASE_URL="${3:-http://localhost:8000}"

curl -sS -X POST "$BASE_URL/h/$TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary "@$PAYLOAD"
echo
