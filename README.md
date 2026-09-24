# HookPing

**Turn any webhook into a human-readable Telegram notification.**

HookPing is a small self-hosted service. It gives you a unique webhook URL, stores every
JSON event that arrives, renders it through a template you control and sends the result to
a Telegram chat. One Docker container, one SQLite file, no external services besides Telegram.

```
POST /h/<token>                 Telegram
{                               ┌──────────────────────────┐
  "customer": {"name": "Acme"}, │ 💰 New payment           │
  "amount": 7500,          ───▶ │                          │
  "currency": "EUR",            │ Customer: Acme           │
  "plan": "Pro"                 │ Amount: 7500 EUR         │
}                               │ Plan: Pro                │
                                └──────────────────────────┘
```

## Why

Your CRM, payment provider, form builder or backend can already send webhooks. But sometimes
all you need is a readable Telegram message when something happens — not a Zapier account,
not a Slack workspace, not another SaaS subscription.

HookPing gives you a webhook URL and converts incoming JSON into a message for humans.
Point Stripe, HubSpot, Tally, Sentry, a cron job or `curl` at it and get a notification
you actually want to read.

## Features

- **Inboxes** — each one has its own unguessable webhook URL (`/h/<random token>`).
- **Event history** — payloads are stored first, delivered second; a Telegram outage never loses an event.
- **Jinja2 templates** — `{{ customer.name }}`, loops, conditions, filters — rendered in a sandbox.
- **Live preview & field picker** — see the fields of your latest payload and click to insert them.
- **One-click Telegram linking** — *Connect Telegram* → open the bot → *Start*. No hunting for chat IDs.
- **Signature verification** — optional per-inbox HMAC-SHA256 with timestamp tolerance and replay protection; write-only secrets.
- **Delivery status per event** — `sent`, `failed` (with the real Telegram error), `pending`, `not_configured`; re-deliver from the UI.
- **Self-hosted & tiny** — FastAPI + SQLite in one container; `docker compose up` and you are done.

## Quick Start

Requirements: Docker with Compose, and a Telegram bot token from [@BotFather](https://t.me/BotFather)
(optional — without it HookPing still receives and stores webhooks).

```bash
git clone https://github.com/VsevaTech/hookping.git
cd hookping
cp .env.example .env          # put your TELEGRAM_BOT_TOKEN in .env
docker compose up --build
```

Open <http://localhost:8000>.

Data lives in the `hookping-data` Docker volume, so `docker compose down && docker compose up`
keeps your inboxes and events.

## Create an Inbox

1. Click **Create Inbox**, name it (for example `Sales Leads`).
2. Copy the webhook URL shown on the inbox page — something like
   `http://localhost:8000/h/GkAFWnJjwkfJZ6x0loYBvt2JuxEbhVqxz4dgoLSkhTo`.
3. Paste it into the system that should notify you.

The token is the only credential, so treat the URL like a password. You can delete an inbox
at any time; its URL stops working immediately.

## Send your first webhook

```bash
curl -X POST \
  http://localhost:8000/h/<TOKEN> \
  -H "Content-Type: application/json" \
  -d '{
    "customer": "Acme Ltd",
    "amount": 129,
    "currency": "USD"
  }'
```

Response:

```json
{"received": true, "event_id": "9cc7d728efa94cb28ac12ba7b41fafa4"}
```

The event appears on the inbox page with its pretty-printed payload. `examples/` contains a
few realistic payloads and `examples/send.sh <TOKEN> examples/lead.json` posts one for you.

Status codes: `200` accepted · `400` body is not valid JSON (or is empty) · `401` signature
verification failed · `404` unknown token · `409` replayed signed request · `413` body over the
limit (256 KB by default). Form-encoded bodies (`application/x-www-form-urlencoded`)
are accepted too and converted to a JSON object.

## Signature verification

By default the unguessable URL is the only credential. For anything that matters (payments,
production alerts) turn on **HMAC SHA-256** in the inbox's *Signature verification* card:

1. **Generate secret** — HookPing creates `whsec_…` and shows it **once**. Or paste the secret
   your provider gave you under *Use a secret from your provider*.
2. Choose **Verification: HMAC SHA-256**, optionally change the header names and the timestamp
   tolerance (default 300 s, 30–3600 s), and save.

From then on every request must carry:

```
X-Timestamp: 1767225600                         # unix seconds (milliseconds also accepted)
X-Signature: sha256=<hex HMAC-SHA256 of "<timestamp>.<raw body>">
```

```bash
TS=$(date +%s)
BODY='{"customer": "Acme Ltd", "amount": 129}'
SIG=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$HOOKPING_SECRET" | sed 's/^.* //')
curl -X POST http://localhost:8000/h/<TOKEN> \
  -H "Content-Type: application/json" \
  -H "X-Timestamp: $TS" -H "X-Signature: sha256=$SIG" \
  --data-binary "$BODY"
```

`HOOKPING_SECRET=whsec_… examples/send-signed.sh <TOKEN> examples/payment.json` does the same for a file.

What HookPing checks, in order, on the **raw bytes** before parsing anything:

| Check | Failure |
| --- | --- |
| Signature and timestamp headers present | `401 Missing X-Signature header` |
| Timestamp is an integer within ± tolerance of server time | `401 Timestamp outside the allowed window of 300s` |
| Signature is `sha256=<hex>` (bare hex or base64 also accepted) | `401 Malformed X-Signature header` |
| HMAC matches (constant-time compare) | `401 Invalid signature` |
| This exact signature was not accepted before | `409 Duplicate request` |

Notes:

- The timestamp is part of the signed string, so an attacker cannot refresh an old request.
- **Replay protection**: accepted signatures are remembered per inbox until their timestamp can no
  longer pass the window, then pruned. A replay is rejected and stored nowhere. A request that
  fails JSON parsing does not "use up" its signature, so a fixed retry still goes through.
- **Rotation**: the header may hold several comma-separated signatures; any match is accepted.
  *Rotate* replaces the secret immediately.
- **The secret is write-only.** It lives in a separate `inbox_signing_secrets` table with no ORM
  relationship from `Inbox`, is never rendered, returned by an API or logged (its `repr` is
  redacted). The UI only shows when it was set. HMAC needs the raw key, so it is not hashed —
  protect the SQLite file like any other credential store.
- Rejections are logged with a reason code only (never headers or body), and the inbox page
  shows the *Last rejected request* to debug a misconfigured sender.
- Events show a 🔏 *signed* badge when they passed verification.

The scheme is intentionally generic; provider presets (GitHub `X-Hub-Signature-256`, Stripe
`Stripe-Signature`, Slack, Shopify…) can be added as configurations of the same verifier.

## Configure Telegram

HookPing uses a bot you own. Put its token in `.env`:

```env
TELEGRAM_BOT_TOKEN=            # paste the token from @BotFather here; never commit .env
HOOKPING_TELEGRAM_BOT_USERNAME=hook_ping_bot
```

Then, on the inbox page:

1. Click **Connect Telegram**. HookPing creates a one-time link (valid for 10 minutes).
2. Click **Open Telegram** — it opens `https://t.me/<bot>?start=<token>`.
3. Press **Start** in Telegram. The bot replies:

   ```
   ✅ HookPing connected

   This chat will now receive notifications from:
   Sales Leads
   ```

4. The inbox page switches to **✅ Connected** by itself (it polls `/api/inboxes/{id}/telegram/status`).
5. Click **Send test notification** to receive `✅ HookPing test`.

**Disconnect** clears the link; **Connect Telegram** again issues a fresh token. Tokens are
single-use and expire, so a leaked link cannot be replayed.

HookPing receives `/start` and `/help` through Telegram long polling (`getUpdates`), so no
public HTTPS endpoint is needed — it works on `localhost`. Run only one HookPing instance per
bot, or set `HOOKPING_TELEGRAM_POLLING=false` on the extra instances.

**Groups and channels.** The deep link connects private chats. To notify a group or channel,
add the bot to it and use *Link a group or channel instead* on the inbox page with the chat
ID (e.g. `-1001234567890`) or a public `@channel` handle.

## Message templates

Templates are [Jinja2](https://jinja.palletsprojects.com/). Top-level payload keys are variables;
nested objects use dots; the whole payload is available as `payload`.

```
🚀 New lead

Name: {{ name }}
Email: {{ email }}
Plan: {{ plan }}
```

```
🚨 {{ level | upper }} in {{ service }} ({{ environment }})

{{ message }}
{{ error.type }} × {{ error.count_last_5m }} in the last 5 minutes
```

Behaviour worth knowing:

- Missing fields render as empty strings — a payload without `plan` does not break delivery.
- Syntax errors are rejected when you save, and shown as a readable message.
- The sandbox forbids attribute tricks like `__class__`; templates cannot run Python.
- An empty template sends the default message: inbox name plus the pretty-printed payload.
- Messages longer than Telegram's 4096-character limit are truncated with a marker.
- Extra filters: `tojson_pretty` (pretty JSON), `money` (`7500` → `7,500.00`), `cents` (`12900` → `129.00`).
- Extra variables: `inbox.name`, `event.received_at`.

Use **Preview** to render the draft against your latest event without saving.
More examples: [`examples/templates.md`](examples/templates.md).

## Example use cases

- **Sales leads** — website form → `🚀 New lead: Ivan, Pro plan, $250`.
- **Payments** — Stripe / your billing service → `💰 Payment paid: Acme Ltd, 129 USD`.
- **Signups** — auth backend → `👋 New signup: maria@example.org (producthunt)`.
- **Production alerts** — Sentry, Grafana, a cron job → `🚨 ERROR in checkout-api`.
- **CRM events** — HubSpot / Pipedrive deal stage changes.

## Architecture

```
Webhook sender (Stripe, CRM, your backend, curl)
      │  POST /h/<token>  (JSON)
      ▼
HookPing (FastAPI)
      │  1. look up inbox by token
      │  2. verify HMAC signature + timestamp (if enabled), reject replays
      │  3. store Event  ──────────────▶  SQLite (/data/hookping.db)
      │  4. respond {"received": true}
      ▼
Background task
      │  5. render Jinja2 template (sandboxed)
      │  6. sendMessage via Telegram Bot API
      │  7. update delivery_status / delivery_error
      ▼
Telegram chat

Telegram long polling (getUpdates) ──▶ /start <token> ──▶ link chat to inbox
```

```
app/
├── main.py                 app factory, lifespan (DB init, Telegram poller), error handlers
├── config.py               pydantic-settings; everything comes from env / .env
├── database.py             SQLAlchemy engine/session, create_all + add-missing-columns on start
├── models/                 Inbox, Event, TelegramConnection, InboxSigningSecret, SeenSignature
├── routers/
│   ├── webhook.py          POST /h/{token}  (public)
│   ├── ui.py               server-rendered management UI
│   ├── api.py              GET /api/inboxes/{id}/telegram/status
│   └── health.py           GET /health
├── services/
│   ├── signatures.py       HMAC-SHA256 verifier (pure, no I/O), secret/header validation
│   ├── templating.py       sandboxed Jinja2 rendering
│   ├── delivery.py         render + send + status bookkeeping
│   ├── telegram.py         Bot API client (timeouts, structured results, token redaction)
│   ├── telegram_linking.py one-time /start tokens, /help, chat ↔ inbox linking
│   ├── telegram_polling.py background getUpdates loop with backoff
│   ├── inboxes.py          persistence helpers, event pruning
│   └── json_paths.py       payload → dotted field list for the editor
├── templates/              Jinja2 HTML
└── static/                 CSS + a little vanilla JS (copy, field chips, status polling)
```

## Security notes

- **Webhook tokens** are 32 random bytes (`secrets.token_urlsafe`), never sequential IDs.
- **Signature verification** (optional, per inbox): HMAC-SHA256 over `timestamp.body`, timestamp
  tolerance, replay protection, write-only secrets — see [Signature verification](#signature-verification).
- **Connection tokens** for Telegram are random, single-use and expire after 10 minutes.
- **The bot token** is read only from the environment. It is never rendered, returned by any
  endpoint or written to logs (HTTP client logging is muted and error strings are redacted).
- **Body size limit** (`HOOKPING_MAX_BODY_BYTES`, default 256 KB) → `413`.
- **Templates** run in Jinja2's `ImmutableSandboxedEnvironment`.
- **Errors** are returned as short JSON messages; stack traces stay in server logs.
- The management UI has **no authentication**. Run it on a private network or behind a reverse
  proxy with auth (basic auth, Tailscale, Cloudflare Access…). Only `/h/<token>` and `/health`
  should be reachable from the internet. Set `PUBLIC_BASE_URL` so displayed webhook URLs use
  your public hostname.

## Development

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Configuration (all optional, see `.env.example`):

| Variable | Default | Purpose |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | – | Bot token; empty disables Telegram delivery |
| `HOOKPING_TELEGRAM_BOT_USERNAME` | `hook_ping_bot` | Used for the `t.me/...?start=` deep link |
| `PUBLIC_BASE_URL` | derived from request | Base for displayed webhook URLs |
| `HOOKPING_DATABASE_URL` | `sqlite:///./data/hookping.db` | SQLAlchemy URL (Docker: `/data/hookping.db`) |
| `HOOKPING_MAX_BODY_BYTES` | `262144` | Webhook body limit |
| `HOOKPING_MAX_EVENTS_PER_INBOX` | `200` | Older events are pruned |
| `HOOKPING_TELEGRAM_POLLING` | `true` | Disable on secondary instances |
| `HOOKPING_LOG_LEVEL` | `INFO` | Logging level |

Interactive API docs: <http://localhost:8000/docs>.

## Tests

```bash
pytest
ruff check .
ruff format --check .
```

Tests use an isolated SQLite file and never call Telegram — the Bot API is replaced with an
`httpx.MockTransport`. They cover inbox creation and token uniqueness, the webhook endpoint
(valid/malformed/oversized/unknown), HMAC verification (encodings, rotation, tampering, stale and
future timestamps, replays, secret shown once and never again), schema upgrade of an older
database, template rendering (nested, missing, invalid, sandbox
escape attempts), the Telegram client (200/400/500/timeout/network error), the `/start`
linking flow (valid, invalid, expired, reused, bare `/start`, `/help`, poller end-to-end)
and the full acceptance scenario. CI runs the same plus a Docker build and smoke test.

## License

[MIT](LICENSE)
