from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.dependencies import get_bridge_service
from app.models import HealthResponse
from app.services.bridge_service import BridgeService

router = APIRouter(tags=["Health"])
BridgeDep = Annotated[BridgeService, Depends(get_bridge_service)]


@router.get("/healthz", response_model=HealthResponse, operation_id="getHealth")
async def get_health(
    request: Request,
    bridge: BridgeDep,
) -> HealthResponse:
    key_valid = None
    if not getattr(request.state, "using_zotero_key_override", False):
        key_valid = getattr(request.app.state, "zotero_key_valid", None)
    return bridge.build_health(key_valid=key_valid)
