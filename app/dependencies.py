from __future__ import annotations

from typing import cast

from fastapi import Request

from app.services.bridge_service import BridgeService


def get_bridge_service(request: Request) -> BridgeService:
    return cast(BridgeService, request.app.state.bridge_service)
