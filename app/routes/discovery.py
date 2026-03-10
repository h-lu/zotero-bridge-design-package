from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.auth import require_bearer_auth
from app.dependencies import get_bridge_service
from app.models import DiscoverySearchResponse
from app.services.bridge_service import BridgeService

router = APIRouter(
    prefix="/v1/discovery",
    tags=["Discovery"],
    dependencies=[Depends(require_bearer_auth)],
)
BridgeDep = Annotated[BridgeService, Depends(get_bridge_service)]


@router.get("/search", response_model=DiscoverySearchResponse, operation_id="searchDiscovery")
async def search_discovery(
    bridge: BridgeDep,
    q: str,
    start: int = Query(default=0, ge=0),
    limit: int = Query(default=10, ge=1, le=25),
    yearFrom: int | None = Query(default=None),
    yearTo: int | None = Query(default=None),
    oaOnly: bool = Query(default=False),
    resolveInLibrary: bool = Query(default=True),
    excludeExisting: bool = Query(default=False),
    sort: str = Query(default="relevance", pattern="^(relevance|cited_by|recent)$"),
) -> DiscoverySearchResponse:
    return await bridge.search_discovery(
        q=q,
        start=start,
        limit=limit,
        year_from=yearFrom,
        year_to=yearTo,
        oa_only=oaOnly,
        resolve_in_library=resolveInLibrary,
        exclude_existing=excludeExisting,
        sort=sort,
    )
