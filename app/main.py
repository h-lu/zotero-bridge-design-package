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
from app.routes.attachments import router as attachments_router
from app.routes.discovery import router as discovery_router
from app.routes.health import router as health_router
from app.routes.items import router as items_router
from app.routes.library import router as library_router
from app.routes.notes import router as notes_router
from app.routes.papers import router as papers_router
from app.services.bridge_service import BridgeService
from app.services.doi_resolver import DOIResolver
from app.services.local_search_index import LocalSearchIndex
from app.services.note_renderer import NoteRenderer
from app.services.note_search_cache import NoteSearchCache
from app.services.zotero_client import ZoteroClient
from app.services.zotero_scope_resolver import ZoteroScopeResolver

app_settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    http_client = httpx.AsyncClient()
    zotero_client = ZoteroClient(settings=settings, client=http_client)
    doi_resolver = DOIResolver(http_client)
    note_renderer = NoteRenderer(settings.default_note_tag_prefix)
    attachment_tokens: dict[str, Any] = {}
    note_search_cache = NoteSearchCache(
        ttl_seconds=settings.note_search_cache_ttl_seconds,
    )
    local_search_index = (
        LocalSearchIndex(settings.local_search_index_path)
        if settings.enable_local_search_index
        else None
    )
    bridge_service = BridgeService(
        settings=settings,
        http_client=http_client,
        zotero_client=zotero_client,
        doi_resolver=doi_resolver,
        note_renderer=note_renderer,
        local_search_index=local_search_index,
        attachment_tokens=attachment_tokens,
        note_search_cache=note_search_cache,
    )
    app.state.bridge_service = bridge_service
    app.state.http_client = http_client
    app.state.doi_resolver = doi_resolver
    app.state.note_renderer = note_renderer
    app.state.attachment_tokens = attachment_tokens
    app.state.note_search_cache = note_search_cache
    app.state.zotero_scope_resolver = ZoteroScopeResolver(
        settings=settings,
        http_client=http_client,
    )
    app.state.zotero_key_valid = None

    if settings.startup_validate_zotero_key:
        try:
            app.state.zotero_key_valid = await bridge_service.validate_upstream_key()
        except BridgeError:
            app.state.zotero_key_valid = False
    await bridge_service.startup()

    try:
        yield
    finally:
        await bridge_service.shutdown()
        await http_client.aclose()


app_kwargs: dict[str, Any] = {
    "title": "Zotero Bridge API",
    "version": "2.0.0",
    "description": "Zotero I/O and workflow layer for ChatGPT/Codex.",
    "lifespan": lifespan,
}
if app_settings.public_base_url:
    app_kwargs["servers"] = [
        {
            "url": app_settings.public_base_url.rstrip("/"),
            "description": "Public Zotero Bridge endpoint",
        }
    ]

app = FastAPI(**app_kwargs)
app.include_router(health_router)
app.include_router(papers_router)
app.include_router(items_router)
app.include_router(attachments_router)
app.include_router(library_router)
app.include_router(discovery_router)
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
