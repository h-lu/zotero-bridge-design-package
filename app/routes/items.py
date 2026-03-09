from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.auth import require_bearer_auth
from app.config import get_settings
from app.dependencies import get_bridge_service
from app.models import (
    CitationResponse,
    FulltextResponse,
    ItemDetailResponse,
    ItemNotesResponse,
    NoteWriteRequest,
    NoteWriteResponse,
    SearchResponse,
    UpsertAINoteRequest,
    UpsertAINoteResponse,
)
from app.services.bridge_service import BridgeService

router = APIRouter(
    prefix="/v1/items",
    tags=["Items"],
    dependencies=[Depends(require_bearer_auth)],
)
BridgeDep = Annotated[BridgeService, Depends(get_bridge_service)]
_settings = get_settings()


@router.get("/search", response_model=SearchResponse, operation_id="searchItems")
async def search_items(
    bridge: BridgeDep,
    q: str,
    limit: int = Query(default=10, ge=1, le=25),
    includeFulltext: bool = Query(default=True),
    includeAttachments: bool = Query(default=True),
    includeNotes: bool = Query(default=True),
) -> SearchResponse:
    return await bridge.search_items(
        q=q,
        limit=limit,
        include_fulltext=includeFulltext,
        include_attachments=includeAttachments,
        include_notes=includeNotes,
    )


@router.get("/{itemKey}", response_model=ItemDetailResponse, operation_id="getItemDetail")
async def get_item_detail(
    itemKey: str,
    bridge: BridgeDep,
) -> ItemDetailResponse:
    return await bridge.get_item_detail(itemKey)


@router.get("/{itemKey}/notes", response_model=ItemNotesResponse, operation_id="listItemNotes")
async def list_item_notes(
    itemKey: str,
    bridge: BridgeDep,
) -> ItemNotesResponse:
    return await bridge.list_item_notes(itemKey)


@router.post("/{itemKey}/notes", response_model=NoteWriteResponse, operation_id="createItemNote")
async def create_item_note(
    itemKey: str,
    payload: NoteWriteRequest,
    bridge: BridgeDep,
) -> NoteWriteResponse:
    return await bridge.create_item_note(item_key=itemKey, payload=payload)


@router.get(
    "/{itemKey}/fulltext",
    response_model=FulltextResponse,
    operation_id="getItemFulltext",
)
async def get_item_fulltext(
    bridge: BridgeDep,
    itemKey: str,
    attachmentKey: str | None = None,
    cursor: int = Query(default=0, ge=0),
    maxChars: int = Query(
        default=_settings.fulltext_default_max_chars,
        ge=1000,
        le=_settings.fulltext_max_chars_hard_limit,
    ),
    preferSource: str = Query(default="auto", pattern="^(auto|web|cache)$"),
) -> FulltextResponse:
    return await bridge.get_item_fulltext(
        item_key=itemKey,
        attachment_key=attachmentKey,
        cursor=cursor,
        max_chars=maxChars,
        prefer_source=preferSource,
    )


@router.post(
    "/{itemKey}/notes/upsert-ai-note",
    response_model=UpsertAINoteResponse,
    operation_id="upsertAiNote",
)
async def upsert_ai_note(
    itemKey: str,
    payload: UpsertAINoteRequest,
    bridge: BridgeDep,
) -> UpsertAINoteResponse:
    return await bridge.upsert_ai_note(item_key=itemKey, payload=payload)


@router.get(
    "/{itemKey}/citation",
    response_model=CitationResponse,
    operation_id="getItemCitation",
)
async def get_item_citation(
    bridge: BridgeDep,
    itemKey: str,
    style: str | None = Query(default=None),
    locale: str | None = Query(default=None),
    linkwrap: bool = False,
) -> CitationResponse:
    settings = get_settings()
    return await bridge.get_item_citation(
        item_key=itemKey,
        style=style or settings.default_citation_style,
        locale=locale or settings.default_citation_locale,
        linkwrap=linkwrap,
    )
