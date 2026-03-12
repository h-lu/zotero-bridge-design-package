from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import Header, Request

from app.config import Settings, get_settings
from app.errors import BridgeError
from app.services.bridge_service import BridgeService
from app.services.doi_resolver import DOIResolver
from app.services.note_renderer import NoteRenderer
from app.services.note_search_cache import NoteSearchCache
from app.services.zotero_client import ZoteroClient
from app.services.zotero_scope_resolver import ZoteroScopeResolver


def get_default_bridge_service(request: Request) -> BridgeService:
    return cast(BridgeService, request.app.state.bridge_service)


async def get_bridge_service(
    request: Request,
    zotero_api_key: Annotated[str, Header(alias="X-Zotero-API-Key")],
) -> BridgeService:
    settings = get_settings()
    default_bridge = cast(BridgeService, request.app.state.bridge_service)
    normalized_key = zotero_api_key.strip()
    if not normalized_key:
        raise BridgeError(
            code="MISSING_ZOTERO_API_KEY",
            message="Missing X-Zotero-API-Key",
            status_code=401,
        )
    if normalized_key == settings.zotero_api_key:
        request.state.using_zotero_key_override = False
        request.state.request_zotero_library_id = settings.zotero_library_id
        return default_bridge
    if not settings.enable_request_scoped_zotero_key:
        raise BridgeError(
            code="REQUEST_SCOPED_ZOTERO_KEY_DISABLED",
            message="X-Zotero-API-Key is disabled on this bridge",
            status_code=403,
        )

    resolver = cast(ZoteroScopeResolver, request.app.state.zotero_scope_resolver)
    scope = await resolver.resolve_user_scope(normalized_key)
    request.state.using_zotero_key_override = True
    request.state.request_zotero_library_id = scope.library_id

    scoped_settings: Settings = settings.model_copy(
        update={
            "zotero_api_key": scope.api_key,
            "zotero_library_type": scope.library_type,
            "zotero_library_id": scope.library_id,
        }
    )
    http_client = request.app.state.http_client
    return BridgeService(
        settings=scoped_settings,
        http_client=http_client,
        zotero_client=ZoteroClient(settings=scoped_settings, client=http_client),
        doi_resolver=cast(DOIResolver, request.app.state.doi_resolver),
        note_renderer=cast(NoteRenderer, request.app.state.note_renderer),
        local_search_index=None,
        attachment_tokens=cast(dict[str, Any], request.app.state.attachment_tokens),
        note_search_cache=cast(NoteSearchCache, request.app.state.note_search_cache),
    )
