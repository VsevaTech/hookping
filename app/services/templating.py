"""Safe Jinja2 rendering of message templates."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from jinja2 import ChainableUndefined, TemplateError, TemplateSyntaxError
from jinja2.sandbox import ImmutableSandboxedEnvironment

DEFAULT_TEMPLATE = """📬 New event in {{ inbox.name }}

{{ payload | tojson_pretty }}"""

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
MAX_RENDER_OUTPUT = 20_000


def _tojson_pretty(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _money(value: Any, digits: int = 2) -> str:
    """Format a number with thousands separators: 7500 -> '7,500.00'."""
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _cents(value: Any, digits: int = 2) -> str:
    """Convert minor units to major units: 12900 -> '129.00'."""
    try:
        return f"{int(value) / 100:,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def build_environment() -> ImmutableSandboxedEnvironment:
    env = ImmutableSandboxedEnvironment(
        undefined=ChainableUndefined,
        autoescape=False,
        trim_blocks=False,
        lstrip_blocks=False,
        keep_trailing_newline=False,
    )
    env.filters["tojson_pretty"] = _tojson_pretty
    env.filters["money"] = _money
    env.filters["cents"] = _cents
    return env


_env = build_environment()


@dataclass(frozen=True)
class RenderResult:
    ok: bool
    text: str = ""
    error: str = ""


def validate_template(template_source: str) -> str | None:
    """Return a human-readable syntax error, or None if the template parses."""
    try:
        _env.parse(template_source)
    except TemplateSyntaxError as exc:
        return f"Template syntax error on line {exc.lineno}: {exc.message}"
    return None


def build_context(payload: Any, inbox_name: str, received_at: Any = None) -> dict[str, Any]:
    context: dict[str, Any] = {}
    if isinstance(payload, dict):
        # Top-level payload keys are exposed directly: {{ customer.name }}.
        for key, value in payload.items():
            if isinstance(key, str) and key.isidentifier():
                context[key] = value
    context["payload"] = payload
    context["inbox"] = {"name": inbox_name}
    context["event"] = {"received_at": received_at}
    return context


def render_message(template_source: str, payload: Any, inbox_name: str, received_at: Any = None) -> RenderResult:
    """Render the template for a payload. Never raises."""
    source = template_source.strip() or DEFAULT_TEMPLATE
    context = build_context(payload, inbox_name, received_at)
    try:
        template = _env.from_string(source)
        text = template.render(context)
    except TemplateSyntaxError as exc:
        return RenderResult(ok=False, error=f"Template syntax error on line {exc.lineno}: {exc.message}")
    except TemplateError as exc:
        return RenderResult(ok=False, error=f"Template error: {exc}")
    except Exception as exc:
        return RenderResult(ok=False, error=f"Template error: {exc.__class__.__name__}: {exc}")

    text = text.strip()
    if len(text) > MAX_RENDER_OUTPUT:
        text = text[:MAX_RENDER_OUTPUT]
    if not text:
        return RenderResult(ok=False, error="Rendered message is empty")
    return RenderResult(ok=True, text=text)


def truncate_for_telegram(text: str) -> str:
    if len(text) <= TELEGRAM_MAX_MESSAGE_LENGTH:
        return text
    marker = "\n… (truncated)"
    return text[: TELEGRAM_MAX_MESSAGE_LENGTH - len(marker)] + marker
