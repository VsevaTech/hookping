"""Server-rendered management UI."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.models import DeliveryStatus, Event, Inbox
from app.models.inbox import utcnow
from app.services import inboxes as inbox_service
from app.services import telegram_linking as linking
from app.services.delivery import deliver_event, get_telegram_client
from app.services.json_paths import extract_paths
from app.services.templating import DEFAULT_TEMPLATE, render_message, validate_template

router = APIRouter(include_in_schema=False)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

SAMPLE_PAYLOAD: dict[str, Any] = {
    "customer": {"name": "Acme Ltd", "email": "john@acme.example"},
    "amount": 7500,
    "currency": "EUR",
    "plan": "Pro",
}

STATUS_LABELS = {
    DeliveryStatus.SENT.value: "sent",
    DeliveryStatus.FAILED.value: "failed",
    DeliveryStatus.PENDING.value: "pending",
    DeliveryStatus.NOT_CONFIGURED.value: "not configured",
}


def _format_dt(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    if value is None:
        return "—"
    return value.strftime(fmt) + " UTC"


templates.env.filters["dt"] = _format_dt
templates.env.filters["status_label"] = lambda s: STATUS_LABELS.get(s, s)


def wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def base_url(request: Request, settings: Settings) -> str:
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return f"{proto}://{host}"


def webhook_url(request: Request, settings: Settings, inbox: Inbox) -> str:
    return f"{base_url(request, settings)}/h/{inbox.hook_token}"


def render(request: Request, name: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
    settings = get_settings()
    context.setdefault("telegram_enabled", settings.telegram_enabled)
    context.setdefault("notice", request.query_params.get("notice"))
    context.setdefault("notice_kind", request.query_params.get("kind", "ok"))
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def render_error(request: Request, status_code: int, detail: str) -> HTMLResponse:
    return render(request, "error.html", {"status_code": status_code, "detail": detail}, status_code=status_code)


def redirect(path: str, notice: str | None = None, kind: str = "ok") -> Response:
    if notice:
        path = f"{path}?{urlencode({'notice': notice, 'kind': kind})}"
    return RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)


def _load_inbox(db: Session, inbox_id: str) -> Inbox:
    inbox = inbox_service.get_inbox(db, inbox_id)
    if inbox is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inbox not found")
    return inbox


DbDep = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: DbDep, settings: SettingsDep) -> HTMLResponse:
    inboxes = inbox_service.list_inboxes(db)
    counts = inbox_service.event_counts(db)
    rows = [
        {
            "inbox": inbox,
            "url": webhook_url(request, settings, inbox),
            "events": counts.get(inbox.id, 0),
            "latest": inbox_service.latest_event(db, inbox),
        }
        for inbox in inboxes
    ]
    return render(request, "dashboard.html", {"rows": rows})


@router.get("/inboxes/new", response_class=HTMLResponse)
def new_inbox_form(request: Request) -> HTMLResponse:
    return render(request, "inbox_new.html", {"error": None, "name": ""})


@router.post("/inboxes", response_model=None)
def create_inbox(request: Request, db: DbDep, name: Annotated[str, Form()] = "") -> Response:
    name = name.strip()
    if not name or len(name) > 120:
        return render(
            request,
            "inbox_new.html",
            {"error": "Name is required (max 120 characters).", "name": name},
            status_code=400,
        )
    inbox = inbox_service.create_inbox(db, name)
    return redirect(f"/inboxes/{inbox.id}", "Inbox created. Paste the webhook URL into the sender.")


def _inbox_context(
    request: Request,
    db: Session,
    settings: Settings,
    inbox: Inbox,
    *,
    template_draft: str | None = None,
    preview: dict[str, Any] | None = None,
    template_error: str | None = None,
) -> dict[str, Any]:
    events = inbox_service.recent_events(db, inbox)
    latest = events[0] if events else None
    sample_payload = latest.payload_data if latest is not None else SAMPLE_PAYLOAD
    draft = template_draft if template_draft is not None else inbox.message_template
    pending = None if inbox.telegram_configured else linking.pending_connection(db, inbox)
    if preview is None:
        result = render_message(draft, sample_payload, inbox.name, latest.received_at if latest else None)
        preview = {"ok": result.ok, "text": result.text, "error": result.error}
    return {
        "inbox": inbox,
        "url": webhook_url(request, settings, inbox),
        "events": events,
        "latest": latest,
        "paths": extract_paths(sample_payload),
        "sample_is_real": latest is not None,
        "template_draft": draft,
        "default_template": DEFAULT_TEMPLATE,
        "preview": preview,
        "template_error": template_error,
        "pending_connection": pending,
        "deep_link": linking.deep_link(pending.token) if pending else None,
        "bot_username": settings.telegram_bot_username,
    }


@router.get("/inboxes/{inbox_id}", response_class=HTMLResponse)
def inbox_detail(request: Request, inbox_id: str, db: DbDep, settings: SettingsDep) -> HTMLResponse:
    inbox = _load_inbox(db, inbox_id)
    return render(request, "inbox_detail.html", _inbox_context(request, db, settings, inbox))


@router.post("/inboxes/{inbox_id}/template", response_model=None)
def update_template(
    request: Request,
    inbox_id: str,
    db: DbDep,
    settings: SettingsDep,
    message_template: Annotated[str, Form()] = "",
    action: Annotated[str, Form()] = "save",
) -> Response:
    inbox = _load_inbox(db, inbox_id)
    draft = message_template.replace("\r\n", "\n")
    error = validate_template(draft) if draft.strip() else None

    if action == "preview" or error:
        context = _inbox_context(request, db, settings, inbox, template_draft=draft, template_error=error)
        return render(request, "inbox_detail.html", context, status_code=400 if error else 200)

    inbox.message_template = draft
    db.commit()
    return redirect(f"/inboxes/{inbox.id}", "Template saved.")


@router.post("/inboxes/{inbox_id}/telegram/connect", response_model=None)
def connect_telegram(inbox_id: str, db: DbDep, settings: SettingsDep) -> Response:
    inbox = _load_inbox(db, inbox_id)
    if not settings.telegram_enabled:
        return redirect(f"/inboxes/{inbox.id}", "TELEGRAM_BOT_TOKEN is not set on the server.", kind="error")
    linking.create_connection(db, inbox)
    return redirect(f"/inboxes/{inbox.id}")


@router.post("/inboxes/{inbox_id}/telegram/disconnect", response_model=None)
def disconnect_telegram(inbox_id: str, db: DbDep) -> Response:
    inbox = _load_inbox(db, inbox_id)
    linking.disconnect(db, inbox)
    return redirect(f"/inboxes/{inbox.id}", "Telegram disconnected.")


@router.post("/inboxes/{inbox_id}/telegram/manual", response_model=None)
def set_telegram_chat_manually(
    inbox_id: str,
    db: DbDep,
    telegram_chat_id: Annotated[str, Form()] = "",
) -> Response:
    """Fallback for groups/channels where the /start deep link is not practical."""
    inbox = _load_inbox(db, inbox_id)
    chat_id = telegram_chat_id.strip()
    if not _looks_like_chat_id(chat_id):
        return redirect(
            f"/inboxes/{inbox.id}",
            "Chat ID must look like 123456789, -1001234567890 or @public_channel.",
            kind="error",
        )
    linking.disconnect(db, inbox)
    inbox.telegram_chat_id = chat_id
    inbox.telegram_connected_at = utcnow()
    db.commit()
    return redirect(f"/inboxes/{inbox.id}", "Telegram chat linked.")


def _looks_like_chat_id(value: str) -> bool:
    if value.startswith("@"):
        return len(value) > 1 and value[1:].replace("_", "").isalnum()
    return value.lstrip("-").isdigit()


@router.post("/inboxes/{inbox_id}/telegram/test", response_model=None)
def send_test_notification(inbox_id: str, db: DbDep) -> Response:
    inbox = _load_inbox(db, inbox_id)
    client = get_telegram_client()
    if not client.configured:
        return redirect(f"/inboxes/{inbox.id}", "TELEGRAM_BOT_TOKEN is not set on the server.", kind="error")
    if not inbox.telegram_configured:
        return redirect(f"/inboxes/{inbox.id}", "Connect Telegram first.", kind="error")
    result = client.send_message(inbox.telegram_chat_id, linking.TEST_MESSAGE)
    if result.ok:
        return redirect(f"/inboxes/{inbox.id}", "Test notification sent.")
    return redirect(f"/inboxes/{inbox.id}", f"Telegram delivery failed: {result.error}", kind="error")


@router.post("/inboxes/{inbox_id}/delete", response_model=None)
def delete_inbox(inbox_id: str, db: DbDep) -> Response:
    inbox = _load_inbox(db, inbox_id)
    inbox_service.delete_inbox(db, inbox)
    return redirect("/", "Inbox deleted.")


@router.get("/inboxes/{inbox_id}/events/{event_id}", response_class=HTMLResponse)
def event_detail(request: Request, inbox_id: str, event_id: str, db: DbDep) -> HTMLResponse:
    inbox = _load_inbox(db, inbox_id)
    event = _load_event(db, inbox, event_id)
    return render(request, "event_detail.html", {"inbox": inbox, "event": event})


@router.post("/inboxes/{inbox_id}/events/{event_id}/redeliver", response_model=None)
def redeliver_event(inbox_id: str, event_id: str, db: DbDep) -> Response:
    inbox = _load_inbox(db, inbox_id)
    event = _load_event(db, inbox, event_id)
    deliver_event(db, event)
    label = STATUS_LABELS.get(event.delivery_status, event.delivery_status)
    kind = "ok" if event.delivery_status == DeliveryStatus.SENT.value else "error"
    return redirect(f"/inboxes/{inbox.id}/events/{event.id}", f"Re-delivered: {label}.", kind=kind)


def _load_event(db: Session, inbox: Inbox, event_id: str) -> Event:
    event = inbox_service.get_event(db, inbox, event_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
    return event
