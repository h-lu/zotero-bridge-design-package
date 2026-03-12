from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import Settings
from app.errors import BridgeError


@dataclass(frozen=True, slots=True)
class ZoteroUserScope:
    api_key: str
    library_type: str
    library_id: str


class ZoteroScopeResolver:
    def __init__(self, *, settings: Settings, http_client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http_client = http_client
        self._cache: dict[str, ZoteroUserScope] = {}

    async def resolve_user_scope(self, api_key: str) -> ZoteroUserScope:
        normalized_key = api_key.strip()
        if not normalized_key:
            raise BridgeError(
                code="BAD_REQUEST",
                message="X-Zotero-API-Key cannot be empty",
                status_code=400,
            )
        cached_scope = self._cache.get(normalized_key)
        if cached_scope is not None:
            return cached_scope

        try:
            response = await self._http_client.get(
                f"{self._settings.zotero_api_base.rstrip('/')}/keys/{normalized_key}",
                headers={
                    "Zotero-API-Version": str(self._settings.zotero_api_version),
                    "Accept": "application/json",
                },
                timeout=30.0,
                follow_redirects=True,
            )
        except httpx.RequestError as exc:
            raise BridgeError(
                code="UPSTREAM_ERROR",
                message="Zotero key resolution failed",
                status_code=502,
            ) from exc

        if response.status_code in {401, 403, 404}:
            raise BridgeError(
                code="INVALID_ZOTERO_API_KEY",
                message="Provided Zotero API key is invalid or cannot access a user library",
                status_code=400,
                upstream_status=response.status_code,
            )
        if response.status_code != 200:
            raise BridgeError(
                code="UPSTREAM_ERROR",
                message="Zotero key resolution failed",
                status_code=502,
                upstream_status=response.status_code,
            )

        payload = response.json()
        if not isinstance(payload, dict):
            raise BridgeError(
                code="UPSTREAM_ERROR",
                message="Unexpected Zotero key metadata payload",
                status_code=502,
            )
        user_id = payload.get("userID")
        if user_id is None:
            raise BridgeError(
                code="UPSTREAM_ERROR",
                message="Zotero key metadata did not include a user ID",
                status_code=502,
            )
        scope = ZoteroUserScope(
            api_key=normalized_key,
            library_type="user",
            library_id=str(user_id),
        )
        self._cache[normalized_key] = scope
        return scope
