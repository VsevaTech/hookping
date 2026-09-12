from __future__ import annotations

from app.services.json_paths import extract_paths
from app.services.templating import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    render_message,
    truncate_for_telegram,
    validate_template,
)

PAYLOAD = {
    "customer": {"name": "Acme Ltd", "email": "john@acme.example"},
    "amount": 7500,
    "currency": "EUR",
    "plan": "Pro",
    "items": [{"sku": "A-1"}, {"sku": "B-2"}],
}


def test_simple_field_substitution() -> None:
    result = render_message("Amount: {{ amount }} {{ currency }}", PAYLOAD, "Payments")
    assert result.ok
    assert result.text == "Amount: 7500 EUR"


def test_nested_field_substitution() -> None:
    template = "💰 New payment\n\nCustomer: {{ customer.name }}\nAmount: {{ amount }} {{ currency }}\nPlan: {{ plan }}"
    result = render_message(template, PAYLOAD, "Payments")
    assert result.ok
    assert result.text == "💰 New payment\n\nCustomer: Acme Ltd\nAmount: 7500 EUR\nPlan: Pro"


def test_missing_field_renders_empty_without_error() -> None:
    result = render_message("Name: {{ name }} / City: {{ customer.address.city }}", PAYLOAD, "Payments")
    assert result.ok
    assert result.text == "Name:  / City:"


def test_invalid_template_returns_readable_error() -> None:
    result = render_message("Hello {{ name", PAYLOAD, "Payments")
    assert not result.ok
    assert result.text == ""
    assert result.error.startswith("Template syntax error on line 1")

    assert validate_template("{% if %}") is not None
    assert validate_template("{{ ok }}") is None


def test_empty_template_uses_default_with_payload() -> None:
    result = render_message("", PAYLOAD, "Sales Leads")
    assert result.ok
    assert result.text.startswith("📬 New event in Sales Leads")
    assert '"name": "Acme Ltd"' in result.text


def test_template_cannot_escape_sandbox() -> None:
    hostile = "{{ ''.__class__.__mro__[1].__subclasses__() }}"
    result = render_message(hostile, PAYLOAD, "Payments")
    assert not result.ok
    assert "Template error" in result.error

    hostile_attr = "{{ payload.__class__ }}"
    result = render_message(hostile_attr, PAYLOAD, "Payments")
    assert not result.ok


def test_completely_empty_output_is_reported() -> None:
    result = render_message("{{ missing }}", PAYLOAD, "Payments")
    assert not result.ok
    assert result.error == "Rendered message is empty"


def test_custom_filters() -> None:
    assert render_message("{{ amount | money }}", PAYLOAD, "P").text == "7,500.00"
    assert render_message("{{ 12900 | cents }}", PAYLOAD, "P").text == "129.00"
    assert render_message("{{ 'x' | money }}", PAYLOAD, "P").text == "x"
    result = render_message("{{ items | tojson_pretty }}", PAYLOAD, "P")
    assert result.ok and '"sku": "A-1"' in result.text


def test_loops_and_conditions_work() -> None:
    template = "{% for item in items %}- {{ item.sku }}\n{% endfor %}{% if amount > 1000 %}big{% endif %}"
    result = render_message(template, PAYLOAD, "P")
    assert result.text == "- A-1\n- B-2\nbig"


def test_non_object_payload_is_available_as_payload() -> None:
    result = render_message("Got {{ payload | length }} items: {{ payload[0] }}", [1, 2, 3], "P")
    assert result.text == "Got 3 items: 1"


def test_truncate_for_telegram() -> None:
    text = "x" * (TELEGRAM_MAX_MESSAGE_LENGTH + 500)
    truncated = truncate_for_telegram(text)
    assert len(truncated) == TELEGRAM_MAX_MESSAGE_LENGTH
    assert truncated.endswith("(truncated)")
    assert truncate_for_telegram("short") == "short"


def test_extract_paths() -> None:
    assert extract_paths({"customer": {"name": "Acme", "email": "a@b.com"}, "amount": 100}) == [
        "customer.name",
        "customer.email",
        "amount",
    ]
    assert extract_paths(PAYLOAD)[-1] == "items[0].sku"
    assert extract_paths({"weird key": 1, "ok": {"x-y": 2}}) == ['ok["x-y"]']
    assert extract_paths([{"id": 1}, {"id": 2}]) == ["payload[0].id"]
    assert extract_paths("scalar") == []
