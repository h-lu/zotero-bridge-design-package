from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.auth import require_bearer_auth
from app.dependencies import get_bridge_service
from app.models import CollectionListResponse, LibraryStatsResponse, TagListResponse
from app.services.bridge_service import BridgeService

router = APIRouter(
    prefix="/v1",
    tags=["Library"],
    dependencies=[Depends(require_bearer_auth)],
)
BridgeDep = Annotated[BridgeService, Depends(get_bridge_service)]


@router.get(
    "/library/stats",
    response_model=LibraryStatsResponse,
    operation_id="getLibraryStats",
)
async def get_library_stats(bridge: BridgeDep) -> LibraryStatsResponse:
    return await bridge.get_library_stats()


@router.get("/collections", response_model=CollectionListResponse, operation_id="listCollections")
async def list_collections(
    bridge: BridgeDep,
    start: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    topLevelOnly: bool = Query(default=False),
) -> CollectionListResponse:
    return await bridge.list_collections(
        start=start,
        limit=limit,
        top_level_only=topLevelOnly,
    )


@router.get("/tags", response_model=TagListResponse, operation_id="listTags")
async def list_tags(
    bridge: BridgeDep,
    start: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    q: str | None = Query(default=None),
    topLevelOnly: bool = Query(default=True),
    collectionKey: str | None = Query(default=None),
) -> TagListResponse:
    return await bridge.list_tags(
        start=start,
        limit=limit,
        q=q,
        top_level_only=topLevelOnly,
        collection_key=collectionKey,
    )
