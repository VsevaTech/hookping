"""SQLAlchemy engine/session setup."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _ensure_sqlite_directory(database_url: str) -> None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return
    path = database_url[len(prefix) :]
    if not path or path == ":memory:":
        return
    Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)


def build_engine(database_url: str) -> Engine:
    _ensure_sqlite_directory(database_url)
    connect_args = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_engine(database_url, connect_args=connect_args, future=True)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


engine: Engine = build_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db(bind: Engine | None = None) -> None:
    """Create missing tables and add missing columns. Safe to call on every start."""
    # Import models so they are registered on the metadata.
    import app.models  # noqa: F401, PLC0415

    bind = bind or engine
    Base.metadata.create_all(bind=bind)
    add_missing_columns(bind)


def add_missing_columns(bind: Engine) -> list[str]:
    """Minimal forward-only migration for existing databases.

    ``create_all`` never alters existing tables, so columns added in newer versions are
    appended with ``ALTER TABLE ... ADD COLUMN``. New non-nullable columns must declare a
    ``server_default``. Returns the ``table.column`` names that were added.
    """
    added: list[str] = []
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    with bind.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column.type.compile(bind.dialect)}'
                if column.server_default is not None:
                    default = column.server_default.arg
                    literal = default if isinstance(default, str) else str(default)
                    ddl += " NOT NULL" if not column.nullable else ""
                    ddl += " DEFAULT '" + literal.replace("'", "''") + "'"
                elif not column.nullable:
                    raise RuntimeError(f"Column {table.name}.{column.name} needs a server_default to be added")
                connection.execute(text(ddl))
                added.append(f"{table.name}.{column.name}")
    return added


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
