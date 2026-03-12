from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.config import get_settings
from app.dependencies import get_bridge_service
from app.models import (
    AdvancedSearchResponse,
    AttachmentListResponse,
    BatchItemRequest,
    BatchItemResponse,
    CitationResponse,
    DuplicateGroupsResponse,
    ItemChangesResponse,
    ItemCollectionsResponse,
    ItemCollectionsWriteRequest,
    ItemDetailResponse,
    ItemListResponse,
    ItemNotesResponse,
    ItemTagsResponse,
    ItemTagsWriteRequest,
    MergeDuplicateItemsRequest,
    MergeDuplicateItemsResponse,
    NoteWriteRequest,
    NoteWriteResponse,
    RelatedItemsResponse,
    ResolveItemsResponse,
    ReviewPackRequest,
    ReviewPackResponse,
    SearchResponse,
    UpsertAINoteRequest,
    UpsertAINoteResponse,
)
from app.services.bridge_service import BridgeService

router = APIRouter(
    prefix="/v1/items",
    tags=["Items"],
)
BridgeDep = Annotated[BridgeService, Depends(get_bridge_service)]


@router.get("", response_model=ItemListResponse, operation_id="listItems")
async def list_items(
    bridge: BridgeDep,
    start: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
    itemType: str | None = Query(default=None),
    collectionKey: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    includeAttachments: bool = Query(default=False),
    includeNotes: bool = Query(default=False),
    sort: str = Query(default="dateAdded", pattern="^(dateAdded|dateModified|title)$"),
    direction: str = Query(default="desc", pattern="^(asc|desc)$"),
) -> ItemListResponse:
    return await bridge.list_items(
        start=start,
        limit=limit,
        item_type=itemType,
        collection_key=collectionKey,
        tag=tag,
        include_attachments=includeAttachments,
        include_notes=includeNotes,
        sort=sort,
        direction=direction,
    )


@router.get("/search", response_model=SearchResponse, operation_id="searchItems")
async def search_items(
    bridge: BridgeDep,
    q: str,
    start: int = Query(default=0, ge=0),
    limit: int = Query(default=10, ge=1, le=25),
    includeAttachments: bool = Query(default=True),
    includeNotes: bool = Query(default=True),
    itemType: str | None = Query(default=None),
    collectionKey: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    sort: str | None = Query(default=None, pattern="^(dateAdded|dateModified|title)$"),
    direction: str | None = Query(default=None, pattern="^(asc|desc)$"),
) -> SearchResponse:
    return await bridge.search_items(
        q=q,
        start=start,
        limit=limit,
        include_attachments=includeAttachments,
        include_notes=includeNotes,
        item_type=itemType,
        collection_key=collectionKey,
        tag=tag,
        sort=sort,
        direction=direction,
    )


@router.get(
    "/search-advanced",
    response_model=AdvancedSearchResponse,
    operation_id="searchItemsAdvanced",
)
async def search_items_advanced(
    bridge: BridgeDep,
    q: str | None = Query(default=None),
    fields: str = Query(
        default="title,creator,abstract,venue,doi,tag",
        pattern="^(title|creator|abstract|venue|doi|tag|note)(,(title|creator|abstract|venue|doi|tag|note))*$",
    ),
    title: str | None = Query(default=None),
    author: str | None = Query(default=None),
    abstract: str | None = Query(default=None),
    venue: str | None = Query(default=None),
    doi: str | None = Query(default=None),
    yearFrom: int | None = Query(default=None),
    yearTo: int | None = Query(default=None),
    start: int = Query(default=0, ge=0),
    limit: int = Query(default=10, ge=1, le=25),
    itemType: str | None = Query(default=None),
    collectionKey: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    hasAiNotes: bool | None = Query(default=None),
    includeAttachments: bool = Query(default=False),
    includeNotes: bool = Query(default=False),
    sort: str = Query(default="relevance", pattern="^(relevance|dateAdded|dateModified|title)$"),
    direction: str | None = Query(default=None, pattern="^(asc|desc)$"),
) -> AdvancedSearchResponse:
    return await bridge.search_items_advanced(
        q=q,
        fields=fields,
        title=title,
        author=author,
        abstract=abstract,
        venue=venue,
        doi=doi,
        year_from=yearFrom,
        year_to=yearTo,
        start=start,
        limit=limit,
        item_type=itemType,
        collection_key=collectionKey,
        tag=tag,
        has_ai_notes=hasAiNotes,
        include_attachments=includeAttachments,
        include_notes=includeNotes,
        sort=sort,
        direction=direction,
    )


@router.post("/batch", response_model=BatchItemResponse, operation_id="batchGetItems")
async def batch_get_items(
    bridge: BridgeDep,
    payload: BatchItemRequest,
) -> BatchItemResponse:
    return await bridge.batch_get_items(
        item_keys=payload.itemKeys,
        include_attachments=payload.includeAttachments,
        include_notes=payload.includeNotes,
    )


@router.get("/resolve", response_model=ResolveItemsResponse, operation_id="resolveItems")
async def resolve_items(
    bridge: BridgeDep,
    doi: str | None = Query(default=None),
    title: str | None = Query(default=None),
    itemType: str | None = Query(default=None),
    collectionKey: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    includeAttachments: bool = Query(default=False),
    includeNotes: bool = Query(default=False),
    limit: int = Query(default=10, ge=1, le=25),
) -> ResolveItemsResponse:
    return await bridge.resolve_items(
        doi=doi,
        title=title,
        item_type=itemType,
        collection_key=collectionKey,
        tag=tag,
        include_attachments=includeAttachments,
        include_notes=includeNotes,
        limit=limit,
    )


@router.get(
    "/duplicates",
    response_model=DuplicateGroupsResponse,
    operation_id="findDuplicateItems",
)
async def find_duplicate_items(
    bridge: BridgeDep,
    start: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
    by: str = Query(default="title,doi", pattern="^(title|doi)(,(title|doi))*$"),
    itemType: str | None = Query(default="journalArticle"),
    collectionKey: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    includeAttachments: bool = Query(default=False),
    includeNotes: bool = Query(default=False),
) -> DuplicateGroupsResponse:
    return await bridge.find_duplicate_items(
        start=start,
        limit=limit,
        by=by,
        item_type=itemType,
        collection_key=collectionKey,
        tag=tag,
        include_attachments=includeAttachments,
        include_notes=includeNotes,
    )


@router.get("/changes", response_model=ItemChangesResponse, operation_id="listItemChanges")
async def list_item_changes(
    bridge: BridgeDep,
    start: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
    sinceVersion: int | None = Query(default=None, ge=0),
    sinceTimestamp: str | None = Query(default=None),
    includeAttachments: bool = Query(default=False),
    includeNotes: bool = Query(default=False),
) -> ItemChangesResponse:
    return await bridge.list_item_changes(
        start=start,
        limit=limit,
        since_version=sinceVersion,
        since_timestamp=sinceTimestamp,
        include_attachments=includeAttachments,
        include_notes=includeNotes,
    )


@router.post("/review-pack", response_model=ReviewPackResponse, operation_id="buildReviewPack")
async def build_review_pack(
    bridge: BridgeDep,
    payload: ReviewPackRequest,
) -> ReviewPackResponse:
    return await bridge.build_review_pack(payload)


@router.post(
    "/duplicates/merge",
    response_model=MergeDuplicateItemsResponse,
    operation_id="mergeDuplicateItems",
)
async def merge_duplicate_items(
    bridge: BridgeDep,
    payload: MergeDuplicateItemsRequest,
) -> MergeDuplicateItemsResponse:
    return await bridge.merge_duplicate_items(
        primary_item_key=payload.primaryItemKey,
        duplicate_item_keys=payload.duplicateItemKeys,
        dry_run=payload.dryRun,
        move_attachments=payload.moveAttachments,
        move_notes=payload.moveNotes,
        merge_tags=payload.mergeTags,
        merge_collections=payload.mergeCollections,
    )


@router.get("/{itemKey}", response_model=ItemDetailResponse, operation_id="getItemDetail")
async def get_item_detail(
    itemKey: str,
    bridge: BridgeDep,
) -> ItemDetailResponse:
    return await bridge.get_item_detail(itemKey)


@router.get(
    "/{itemKey}/attachments",
    response_model=AttachmentListResponse,
    operation_id="listItemAttachments",
)
async def list_item_attachments(
    itemKey: str,
    bridge: BridgeDep,
) -> AttachmentListResponse:
    return await bridge.list_item_attachments(itemKey)


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


@router.post("/{itemKey}/tags", response_model=ItemTagsResponse, operation_id="addItemTags")
async def add_item_tags(
    itemKey: str,
    payload: ItemTagsWriteRequest,
    bridge: BridgeDep,
) -> ItemTagsResponse:
    return await bridge.add_item_tags(item_key=itemKey, tags=payload.tags)


@router.delete(
    "/{itemKey}/tags/{tag}",
    response_model=ItemTagsResponse,
    operation_id="removeItemTag",
)
async def remove_item_tag(
    itemKey: str,
    tag: str,
    bridge: BridgeDep,
) -> ItemTagsResponse:
    return await bridge.remove_item_tag(item_key=itemKey, tag=tag)


@router.post(
    "/{itemKey}/collections",
    response_model=ItemCollectionsResponse,
    operation_id="addItemToCollections",
)
async def add_item_to_collections(
    itemKey: str,
    payload: ItemCollectionsWriteRequest,
    bridge: BridgeDep,
) -> ItemCollectionsResponse:
    return await bridge.add_item_to_collections(
        item_key=itemKey,
        collection_keys=payload.collectionKeys,
    )


@router.get(
    "/{itemKey}/related",
    response_model=RelatedItemsResponse,
    operation_id="getRelatedItems",
)
async def get_related_items(
    itemKey: str,
    bridge: BridgeDep,
    includeAttachments: bool = Query(default=False),
    includeNotes: bool = Query(default=False),
) -> RelatedItemsResponse:
    return await bridge.get_related_items(
        item_key=itemKey,
        include_attachments=includeAttachments,
        include_notes=includeNotes,
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
