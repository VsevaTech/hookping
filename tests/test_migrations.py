"""Existing databases created by older versions get the new columns on start."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text

from app.database import build_engine, init_db


def test_init_db_adds_missing_columns_to_old_schema(tmp_path: Path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path}/old.db")
    with engine.begin() as connection:
        # Schema of v0.1.0 (only the columns that matter here).
        connection.execute(
            text(
                "CREATE TABLE inboxes (id VARCHAR(32) PRIMARY KEY, name VARCHAR(120) NOT NULL, "
                "hook_token VARCHAR(64) NOT NULL UNIQUE, message_template TEXT NOT NULL, "
                "telegram_chat_id VARCHAR(64), telegram_connected_at DATETIME, telegram_username VARCHAR(64), "
                "telegram_first_name VARCHAR(128), created_at DATETIME, updated_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE events (id VARCHAR(32) PRIMARY KEY, inbox_id VARCHAR(32) NOT NULL "
                "REFERENCES inboxes(id) ON DELETE CASCADE, received_at DATETIME, method VARCHAR(10) NOT NULL, "
                "content_type VARCHAR(120) NOT NULL, payload TEXT NOT NULL, delivery_status VARCHAR(20) NOT NULL, "
                "delivery_error TEXT NOT NULL, rendered_message TEXT NOT NULL, delivered_at DATETIME)"
            )
        )
        connection.execute(
            text("INSERT INTO inboxes (id, name, hook_token, message_template) VALUES ('i1','A','t','')")
        )
        connection.execute(
            text(
                "INSERT INTO events (id, inbox_id, method, content_type, payload, delivery_status, delivery_error, "
                "rendered_message) VALUES ('e1','i1','POST','','{}','sent','','')"
            )
        )

    init_db(engine)
    init_db(engine)  # idempotent

    inspector = inspect(engine)
    inbox_columns = {c["name"] for c in inspector.get_columns("inboxes")}
    assert {"verification_mode", "signature_header", "timestamp_header", "timestamp_tolerance_seconds"} <= inbox_columns
    assert {"signing_secret_set_at", "last_rejected_at", "last_rejection_reason"} <= inbox_columns
    assert "verification" in {c["name"] for c in inspector.get_columns("events")}
    assert {"inbox_signing_secrets", "seen_signatures"} <= set(inspector.get_table_names())

    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT verification_mode, signature_header, timestamp_header, timestamp_tolerance_seconds FROM inboxes"
            )
        ).one()
        assert tuple(row) == ("none", "X-Signature", "X-Timestamp", 300)
        assert connection.execute(text("SELECT verification FROM events")).scalar_one() == "none"
    engine.dispose()
