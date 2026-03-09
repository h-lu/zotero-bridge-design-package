from __future__ import annotations

from typing import Any, cast

import httpx
import pytest

from app.errors import BridgeError
from app.services.doi_resolver import DOIResolver


def build_resolver() -> DOIResolver:
    resolver = cast(Any, DOIResolver.__new__(DOIResolver))
    resolver._client = None
    return cast(DOIResolver, resolver)


def test_normalize_doi_variants() -> None:
    resolver = build_resolver()
    assert resolver.normalize_doi("https://doi.org/10.1038/NRD842.") == "10.1038/nrd842"
    assert (
        resolver.normalize_doi("doi:10.1126/SCIENCE.169.3946.635")
        == "10.1126/science.169.3946.635"
    )


def test_build_zotero_item_from_metadata() -> None:
    resolver = build_resolver()
    template = {
        "itemType": "journalArticle",
        "title": "",
        "creators": [],
        "DOI": "",
        "url": "",
        "date": "",
        "publicationTitle": "",
        "volume": "",
        "issue": "",
        "pages": "",
        "abstractNote": "",
        "tags": [],
        "collections": [],
    }
    metadata = {
        "type": "article-journal",
        "title": "Bridge Design",
        "author": [{"given": "Ada", "family": "Lovelace"}],
        "container-title": "Journal of Bridges",
        "volume": "7",
        "issue": "2",
        "page": "10-20",
        "abstract": "Study abstract",
        "issued": {"date-parts": [[2024, 1, 15]]},
        "URL": "https://example.com/paper",
    }

    item = resolver.build_zotero_item(
        metadata=metadata,
        template=template,
        doi="10.1000/bridge",
        collection_key="COLL1",
        default_collection_key=None,
        tags=["ai", "reading"],
    )

    assert item["itemType"] == "journalArticle"
    assert item["title"] == "Bridge Design"
    assert item["DOI"] == "10.1000/bridge"
    assert item["creators"][0]["lastName"] == "Lovelace"
    assert item["publicationTitle"] == "Journal of Bridges"
    assert item["date"] == "2024-1-15"
    assert item["collections"] == ["COLL1"]
    assert item["tags"] == [{"tag": "ai"}, {"tag": "reading"}]


def test_extract_date_returns_empty_for_undated_metadata() -> None:
    resolver = build_resolver()
    assert resolver._extract_date({}) == ""


@pytest.mark.asyncio
async def test_resolve_wraps_transport_failures_as_bridge_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = DOIResolver(client)
        with pytest.raises(BridgeError) as exc_info:
            await resolver.resolve("10.1000/bridge")

    assert exc_info.value.code == "DOI_RESOLUTION_FAILED"
    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_resolve_wraps_crossref_parse_failures_as_bridge_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "doi.org":
            return httpx.Response(404)
        if request.url.host == "api.crossref.org":
            return httpx.Response(200, content=b"<html>not-json</html>")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = DOIResolver(client)
        with pytest.raises(BridgeError) as exc_info:
            await resolver.resolve("10.1000/bridge")

    assert exc_info.value.code == "DOI_RESOLUTION_FAILED"
    assert exc_info.value.status_code == 502
    assert exc_info.value.upstream_status == 200
