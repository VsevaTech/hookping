"""HookPing — turn any webhook into a human-readable Telegram notification."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.database import init_db
from app.routers import api, health, ui, webhook
from app.services.delivery import get_telegram_client
from app.services.telegram_polling import TelegramPoller

BASE_DIR = Path(__file__).resolve().parent


def configure_logging() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs full request URLs at INFO, which would include the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    init_db()
    settings = get_settings()
    logging.getLogger(__name__).info(
        "HookPing started (telegram=%s)", "enabled" if settings.telegram_enabled else "not configured"
    )
    poller: TelegramPoller | None = None
    if settings.telegram_enabled and settings.telegram_polling:
        poller = TelegramPoller(get_telegram_client(), poll_timeout=settings.telegram_poll_timeout_seconds)
        await poller.start()
    app.state.telegram_poller = poller
    try:
        yield
    finally:
        if poller is not None:
            await poller.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="HookPing",
        description="Turn any webhook into a human-readable Telegram notification.",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    app.include_router(health.router)
    app.include_router(webhook.router)
    app.include_router(api.router)
    app.include_router(ui.router)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if request.url.path.startswith("/h/") or not ui.wants_html(request):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return ui.render_error(request, exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": "Invalid request", "errors": jsonable_encoder(exc.errors())}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logging.getLogger(__name__).exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "Internal server error"}, status_code=500)

    return app


app = create_app()
