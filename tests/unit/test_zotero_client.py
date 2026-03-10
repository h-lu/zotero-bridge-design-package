from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from app.errors import BridgeError
from app.services.zotero_client import ZoteroClient


@pytest.mark.asyncio
async def test_validate_key_wraps_transport_errors(test_env: None) -> None:
    settings = get_settings()
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        raise httpx.ConnectError("dns failure", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        zotero_client = ZoteroClient(settings=settings, client=client)
        with pytest.raises(BridgeError) as exc_info:
            await zotero_client.validate_key()

    assert exc_info.value.code == "UPSTREAM_ERROR"
    assert exc_info.value.status_code == 502
    assert exc_info.value.upstream_status is None
    assert attempts["count"] == 4


@pytest.mark.asyncio
async def test_get_children_fetches_all_pages(test_env: None) -> None:
    settings = get_settings()
    starts: list[int] = []
    all_children = [
        {"key": f"CHILD{i:04d}", "version": 1, "data": {"itemType": "note", "note": f"<p>{i}</p>"}}
        for i in range(125)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/users/123456/items/ITEM0001/children"
        start = int(request.url.params.get("start", "0"))
        limit = int(request.url.params.get("limit", "100"))
        starts.append(start)
        return httpx.Response(200, json=all_children[start : start + limit])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        zotero_client = ZoteroClient(settings=settings, client=client)
        children = await zotero_client.get_children("ITEM0001")

    assert starts == [0, 100]
    assert len(children) == 125
    assert children[0]["key"] == "CHILD0000"
    assert children[-1]["key"] == "CHILD0124"


@pytest.mark.asyncio
async def test_upload_to_authorized_url_wraps_transport_errors(test_env: None) -> None:
    settings = get_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failure", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        zotero_client = ZoteroClient(settings=settings, client=client)
        with pytest.raises(BridgeError) as exc_info:
            await zotero_client.upload_to_authorized_url(
                {
                    "url": "https://upload.example.com/file",
                    "prefix": "",
                    "suffix": "",
                    "contentType": "application/octet-stream",
                },
                b"%PDF-1.4",
            )

    assert exc_info.value.code == "UPLOAD_FAILED"
    assert exc_info.value.status_code == 502
    assert exc_info.value.upstream_status is None
