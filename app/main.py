from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError

from app.config import get_settings
from app.errors import (
    BridgeError,
    bridge_error_handler,
    request_validation_error_handler,
    unexpected_error_handler,
)
from app.routes.health import router as health_router
from app.routes.items import router as items_router
from app.routes.notes import router as notes_router
from app.routes.papers import router as papers_router
from app.services.bridge_service import BridgeService
from app.services.doi_resolver import DOIResolver
from app.services.fulltext import FulltextService
from app.services.local_fulltext_store import LocalFulltextStore
from app.services.note_renderer import NoteRenderer
from app.services.zotero_client import ZoteroClient


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    http_client = httpx.AsyncClient()
    zotero_client = ZoteroClient(settings=settings, client=http_client)
    local_fulltext_store = (
        LocalFulltextStore(settings.local_fulltext_cache_path)
        if settings.enable_local_fulltext_cache
        else None
    )
    bridge_service = BridgeService(
        settings=settings,
        http_client=http_client,
        zotero_client=zotero_client,
        doi_resolver=DOIResolver(http_client),
        note_renderer=NoteRenderer(settings.default_note_tag_prefix),
        fulltext_service=FulltextService(
            default_max_chars=settings.fulltext_default_max_chars,
            hard_max_chars=settings.fulltext_max_chars_hard_limit,
        ),
        local_fulltext_store=local_fulltext_store,
    )
    app.state.bridge_service = bridge_service
    app.state.zotero_key_valid = None

    if settings.startup_validate_zotero_key:
        try:
            app.state.zotero_key_valid = await bridge_service.validate_upstream_key()
        except BridgeError:
            app.state.zotero_key_valid = False

    try:
        yield
    finally:
        await http_client.aclose()


app = FastAPI(
    title="Zotero Bridge API",
    version="1.0.0",
    description="REST bridge between ChatGPT/Codex and a Zotero library.",
    lifespan=lifespan,
)
app.include_router(health_router)
app.include_router(papers_router)
app.include_router(items_router)
app.include_router(notes_router)

app.add_exception_handler(BridgeError, cast(Any, bridge_error_handler))
app.add_exception_handler(
    RequestValidationError,
    cast(Any, request_validation_error_handler),
)
app.add_exception_handler(Exception, unexpected_error_handler)


@app.middleware("http")
async def request_context_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    request_id = request.headers.get("X-Request-ID") or uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response
