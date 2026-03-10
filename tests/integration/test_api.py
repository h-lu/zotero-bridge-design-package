from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import httpx
import pytest
import respx

ZOTERO_ITEMS_URL = "https://api.zotero.org/users/123456/items"
ZOTERO_ITEM_NEW_URL = "https://api.zotero.org/items/new"
PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF"


def zotero_item(key: str, data: dict[str, Any], version: int = 1) -> dict[str, Any]:
    return {"key": key, "version": version, "data": data}


@pytest.mark.asyncio
async def test_healthz(async_client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await async_client.get("/healthz", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["service"] == "zotero-bridge"
    assert payload["config"]["libraryId"] == "123456"


async def test_list_items_endpoint_supports_pagination_and_item_type_filter(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    paper_one = zotero_item(
        "ITEM0002",
        {
            "itemType": "journalArticle",
            "title": "Alpha Paper",
            "date": "2024-01-01",
            "DOI": "10.1000/alpha",
            "creators": [{"name": "Ada Lovelace", "creatorType": "author"}],
            "tags": [{"tag": "ml"}],
            "collections": ["COLL1"],
        },
    )
    paper_two = zotero_item(
        "ITEM0003",
        {
            "itemType": "journalArticle",
            "title": "Beta Paper",
            "date": "2025-02-02",
            "DOI": "10.1000/beta",
            "creators": [{"name": "Grace Hopper", "creatorType": "author"}],
            "tags": [],
            "collections": [],
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/top":
            assert request.url.params.get("format") == "json"
            assert request.url.params.get("start") == "0"
            assert request.url.params.get("limit") == "2"
            assert request.url.params.get("itemType") == "journalArticle"
            assert request.url.params.get("sort") == "title"
            assert request.url.params.get("direction") == "asc"
            return httpx.Response(
                200,
                json=[paper_one, paper_two],
                headers={"Total-Results": "3"},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items",
        headers=auth_headers,
        params={
            "start": 0,
            "limit": 2,
            "itemType": "journalArticle",
            "sort": "title",
            "direction": "asc",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    assert payload["total"] == 3
    assert payload["start"] == 0
    assert payload["limit"] == 2
    assert [item["itemKey"] for item in payload["items"]] == ["ITEM0002", "ITEM0003"]
    assert payload["items"][0]["title"] == "Alpha Paper"
    assert payload["items"][0]["attachments"] == []
    assert payload["items"][0]["aiNotes"] == []


async def test_search_endpoint_returns_pagination_and_search_hints(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    parent = zotero_item(
        "ITEM0001",
        {
            "itemType": "journalArticle",
            "title": "Alpha Search Paper",
            "date": "2024-01-01",
            "DOI": "10.1000/alpha-search",
            "creators": [{"name": "Ada Lovelace", "creatorType": "author"}],
            "tags": [{"tag": "alpha"}],
            "collections": [],
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items":
            assert request.url.params.get("q") == "alpha"
            assert request.url.params.get("start") == "0"
            assert request.url.params.get("limit") == "100"
            assert request.url.params.get("sort") == "title"
            assert request.url.params.get("direction") == "asc"
            return httpx.Response(200, json=[parent], headers={"Total-Results": "1"})
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=parent)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/search",
        headers=auth_headers,
        params={
            "q": "alpha",
            "start": 0,
            "limit": 5,
            "includeFulltext": "false",
            "includeAttachments": "false",
            "includeNotes": "false",
            "sort": "title",
            "direction": "asc",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["total"] == 1
    assert payload["start"] == 0
    assert payload["limit"] == 5
    assert payload["nextStart"] is None
    assert payload["items"][0]["itemKey"] == "ITEM0001"
    assert payload["items"][0]["searchHints"][0]["field"] == "title"


async def test_search_endpoint_only_resolves_items_needed_for_current_page(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    first = zotero_item(
        "ITEM0001",
        {
            "itemType": "journalArticle",
            "title": "Alpha Paper",
            "date": "2024-01-01",
            "creators": [],
            "tags": [],
            "collections": [],
        },
    )
    second = zotero_item(
        "ITEM0002",
        {
            "itemType": "journalArticle",
            "title": "Beta Paper",
            "date": "2024-01-02",
            "creators": [],
            "tags": [],
            "collections": [],
        },
    )
    third = zotero_item(
        "ITEM0003",
        {
            "itemType": "journalArticle",
            "title": "Gamma Paper",
            "date": "2024-01-03",
            "creators": [],
            "tags": [],
            "collections": [],
        },
    )
    state: dict[str, list[str]] = {"item_gets": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items":
            return httpx.Response(
                200,
                json=[first, second, third],
                headers={"Total-Results": "3"},
            )
        if request.method == "GET" and request.url.path.startswith("/users/123456/items/"):
            state["item_gets"].append(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=second)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/search",
        headers=auth_headers,
        params={
            "q": "paper",
            "start": 1,
            "limit": 1,
            "includeFulltext": "false",
            "includeAttachments": "false",
            "includeNotes": "false",
            "sort": "title",
            "direction": "asc",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["total"] == 3
    assert payload["nextStart"] == 2
    assert payload["items"][0]["itemKey"] == "ITEM0002"
    assert set(state["item_gets"]).issubset({"ITEM0002"})
    assert len(state["item_gets"]) <= 1


async def test_search_endpoint_re_sorts_local_cache_only_hits_before_pagination(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    from app.main import app

    alpha = zotero_item(
        "ITEM0001",
        {
            "itemType": "journalArticle",
            "title": "Alpha Cache Paper",
            "date": "2024-01-01",
            "dateAdded": "2024-01-01T00:00:00Z",
            "dateModified": "2024-01-01T00:00:00Z",
            "creators": [],
            "tags": [],
            "collections": [],
        },
    )
    beta = zotero_item(
        "ITEM0002",
        {
            "itemType": "journalArticle",
            "title": "Beta Upstream Paper",
            "date": "2024-01-02",
            "dateAdded": "2024-01-02T00:00:00Z",
            "dateModified": "2024-01-02T00:00:00Z",
            "creators": [],
            "tags": [],
            "collections": [],
        },
    )
    store = app.state.bridge_service._local_fulltext_store
    assert store is not None
    store.write_payload(
        attachment_key="ATTACHCACHE1",
        item_key="ITEM0001",
        filename="alpha.pdf",
        fulltext_payload={
            "content": "cache ordering token",
            "indexedPages": 1,
            "totalPages": 1,
        },
    )
    state: dict[str, list[str]] = {"item_gets": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items":
            if request.url.params.get("itemKey"):
                return httpx.Response(200, json=[alpha], headers={"Total-Results": "1"})
            return httpx.Response(200, json=[beta], headers={"Total-Results": "1"})
        if request.method == "GET" and request.url.path == "/users/123456/items/top":
            return httpx.Response(200, json=[alpha, beta], headers={"Total-Results": "2"})
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            state["item_gets"].append("ITEM0001")
            return httpx.Response(200, json=alpha)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0002":
            state["item_gets"].append("ITEM0002")
            return httpx.Response(200, json=beta)
        if request.method == "GET" and request.url.path in {
            "/users/123456/items/ITEM0001/children",
            "/users/123456/items/ITEM0002/children",
        }:
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/search",
        headers=auth_headers,
        params={
            "q": "cache ordering token",
            "start": 0,
            "limit": 1,
            "includeFulltext": "true",
            "includeAttachments": "false",
            "includeNotes": "false",
            "sort": "title",
            "direction": "asc",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["total"] == 2
    assert payload["nextStart"] == 1
    assert payload["items"][0]["itemKey"] == "ITEM0001"
    assert payload["items"][0]["searchHints"][0]["field"] == "local_cache_fulltext"
    assert state["item_gets"] == ["ITEM0001"]


async def test_search_endpoint_returns_local_note_only_match(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    parent = zotero_item(
        "ITEM0001",
        {
            "itemType": "journalArticle",
            "title": "Generative AI at Work",
            "creators": [{"name": "Erik Brynjolfsson", "creatorType": "author"}],
            "tags": [],
            "collections": [],
        },
    )
    ai_note = zotero_item(
        "NOTE0001",
        {
            "itemType": "note",
            "parentItem": "ITEM0001",
            "note": "<p>Tacit knowledge transfer helps novice workers.</p>",
            "tags": [{"tag": "generated-note"}],
            "collections": [],
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items":
            return httpx.Response(200, json=[], headers={"Total-Results": "0"})
        if request.method == "GET" and request.url.path == "/users/123456/items/top":
            return httpx.Response(200, json=[parent], headers={"Total-Results": "1"})
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            return httpx.Response(200, json=[ai_note])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/search",
        headers=auth_headers,
        params={
            "q": "tacit knowledge",
            "includeFulltext": "false",
            "includeNotes": "true",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["itemKey"] == "ITEM0001"
    assert payload["items"][0]["searchHints"][0]["field"] == "note"
    assert payload["items"][0]["score"] and payload["items"][0]["score"] > 0


async def test_search_advanced_endpoint_filters_on_abstract_venue_and_year(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    matching = zotero_item(
        "ITEM0001",
        {
            "itemType": "journalArticle",
            "title": "Knowledge Spillovers in Trade",
            "date": "2023-01-01",
            "abstractNote": "This paper studies spillovers across exporting firms.",
            "publicationTitle": "Journal of Trade",
            "creators": [{"name": "Ada Lovelace", "creatorType": "author"}],
            "tags": [{"tag": "trade"}],
            "collections": [],
        },
    )
    non_matching = zotero_item(
        "ITEM0002",
        {
            "itemType": "journalArticle",
            "title": "Unrelated Paper",
            "date": "2018-01-01",
            "abstractNote": "This paper studies something else.",
            "publicationTitle": "Economic History Review",
            "creators": [{"name": "Grace Hopper", "creatorType": "author"}],
            "tags": [],
            "collections": [],
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items":
            return httpx.Response(
                200,
                json=[matching],
                headers={"Total-Results": "1"},
            )
        if request.method == "GET" and request.url.path == "/users/123456/items/top":
            return httpx.Response(
                200,
                json=[matching, non_matching],
                headers={"Total-Results": "2"},
            )
        if request.method == "GET" and request.url.path in {
            "/users/123456/items/ITEM0001/children",
            "/users/123456/items/ITEM0002/children",
        }:
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/search-advanced",
        headers=auth_headers,
        params={
            "q": "spillovers",
            "fields": "abstract,venue",
            "yearFrom": 2020,
            "itemType": "journalArticle",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["itemKey"] == "ITEM0001"
    assert payload["items"][0]["abstractNote"] == (
        "This paper studies spillovers across exporting firms."
    )
    assert payload["items"][0]["venue"] == "Journal of Trade"
    assert payload["items"][0]["searchHints"][0]["field"] == "abstract"


async def test_search_advanced_sorts_by_relevance_and_normalizes_abstract_text(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    title_match = zotero_item(
        "ITEM0001",
        {
            "itemType": "journalArticle",
            "title": "Knowledge Spillovers in Trade",
            "abstractNote": "<jats:p>Background only.</jats:p>",
            "creators": [{"name": "Ada Lovelace", "creatorType": "author"}],
            "tags": [],
            "collections": [],
            "dateModified": "2026-03-10T12:00:00Z",
        },
    )
    abstract_match = zotero_item(
        "ITEM0002",
        {
            "itemType": "journalArticle",
            "title": "Trade and Firms",
            "abstractNote": (
                "<jats:p>This paper studies knowledge spillovers across firms.</jats:p>"
            ),
            "creators": [{"name": "Grace Hopper", "creatorType": "author"}],
            "tags": [],
            "collections": [],
            "dateModified": "2026-03-10T12:05:00Z",
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items":
            return httpx.Response(
                200,
                json=[title_match, abstract_match],
                headers={"Total-Results": "2"},
            )
        if request.method == "GET" and request.url.path == "/users/123456/items/top":
            return httpx.Response(
                200,
                json=[title_match, abstract_match],
                headers={"Total-Results": "2"},
            )
        if request.method == "GET" and request.url.path in {
            "/users/123456/items/ITEM0001/children",
            "/users/123456/items/ITEM0002/children",
        }:
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/search-advanced",
        headers=auth_headers,
        params={
            "q": "knowledge spillovers",
            "fields": "title,abstract",
            "sort": "relevance",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    assert payload["items"][0]["itemKey"] == "ITEM0001"
    assert payload["items"][0]["score"] > payload["items"][1]["score"]
    assert payload["items"][1]["abstractNote"] == (
        "This paper studies knowledge spillovers across firms."
    )
    assert "<jats:" not in payload["items"][1]["searchHints"][0]["snippet"]


async def test_search_advanced_note_field_matches_local_note_text(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    parent = zotero_item(
        "ITEM0001",
        {
            "itemType": "journalArticle",
            "title": "Generative AI at Work",
            "creators": [{"name": "Erik Brynjolfsson", "creatorType": "author"}],
            "tags": [],
            "collections": [],
        },
    )
    ai_note = zotero_item(
        "NOTE0001",
        {
            "itemType": "note",
            "parentItem": "ITEM0001",
            "note": (
                "<p>The mechanism is consistent with tacit knowledge transfer "
                "from stronger workers to weaker workers.</p>"
            ),
            "tags": [{"tag": "generated-note"}, {"tag": "zbridge"}],
            "collections": [],
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items":
            return httpx.Response(200, json=[parent], headers={"Total-Results": "1"})
        if request.method == "GET" and request.url.path == "/users/123456/items/top":
            return httpx.Response(
                200,
                json=[parent],
                headers={"Total-Results": "1"},
            )
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            return httpx.Response(200, json=[ai_note])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/search-advanced",
        headers=auth_headers,
        params={
            "q": "tacit knowledge",
            "fields": "note",
            "includeNotes": "true",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["itemKey"] == "ITEM0001"
    assert payload["items"][0]["searchHints"][0]["field"] == "note"
    assert "tacit knowledge" in payload["items"][0]["searchHints"][0]["snippet"].lower()


async def test_search_advanced_has_fulltext_filter_distinguishes_attachment_types(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    article = zotero_item(
        "ITEM0001",
        {
            "itemType": "journalArticle",
            "title": "Parent Article",
            "creators": [],
            "tags": [],
            "collections": [],
        },
    )
    pdf_attachment = zotero_item(
        "ATTPDF01",
        {
            "itemType": "attachment",
            "title": "fulltext.pdf",
            "filename": "fulltext.pdf",
            "contentType": "application/pdf",
            "linkMode": "imported_file",
            "creators": [],
            "tags": [],
            "collections": [],
        },
    )
    snapshot_attachment = zotero_item(
        "ATTHMT01",
        {
            "itemType": "attachment",
            "title": "snapshot.html",
            "filename": "snapshot.html",
            "contentType": "text/html",
            "linkMode": "imported_url",
            "creators": [],
            "tags": [],
            "collections": [],
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/top":
            return httpx.Response(
                200,
                json=[article, pdf_attachment, snapshot_attachment],
                headers={"Total-Results": "3"},
            )
        if request.method == "GET" and request.url.path in {
            "/users/123456/items/ITEM0001/children",
            "/users/123456/items/ATTPDF01/children",
            "/users/123456/items/ATTHMT01/children",
        }:
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    with_fulltext = await async_client.get(
        "/v1/items/search-advanced",
        headers=auth_headers,
        params={"hasFulltext": "true", "itemType": "attachment"},
    )
    without_fulltext = await async_client.get(
        "/v1/items/search-advanced",
        headers=auth_headers,
        params={"hasFulltext": "false", "itemType": "attachment"},
    )

    assert with_fulltext.status_code == 200
    assert with_fulltext.json()["items"][0]["itemKey"] == "ATTPDF01"
    assert with_fulltext.json()["count"] == 1
    assert without_fulltext.status_code == 200
    assert without_fulltext.json()["items"][0]["itemKey"] == "ATTHMT01"
    assert without_fulltext.json()["count"] == 1


async def test_batch_get_items_endpoint_returns_items_and_not_found_keys(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    parent = zotero_item(
        "ITEM0001",
        {
            "itemType": "journalArticle",
            "title": "Batch Paper",
            "date": "2024-01-01",
            "DOI": "10.1000/batch",
            "creators": [{"name": "Ada Lovelace", "creatorType": "author"}],
            "tags": [],
            "collections": [],
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items":
            assert request.url.params.get("itemKey") == "ITEM0001"
            return httpx.Response(200, json=[parent], headers={"Total-Results": "1"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.post(
        "/v1/items/batch",
        headers=auth_headers,
        json={
            "itemKeys": ["ITEM0001", "MISSING99"],
            "includeAttachments": False,
            "includeNotes": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["itemKey"] == "ITEM0001"
    assert payload["notFoundKeys"] == ["MISSING99"]


async def test_resolve_items_endpoint_finds_exact_doi_matches(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    parent = zotero_item(
        "ITEM0001",
        {
            "itemType": "journalArticle",
            "title": "Resolved Paper",
            "date": "2024-01-01",
            "DOI": "10.1000/resolve",
            "creators": [{"name": "Ada Lovelace", "creatorType": "author"}],
            "tags": [],
            "collections": [],
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items":
            assert request.url.params.get("q") == "10.1000/resolve"
            return httpx.Response(200, json=[parent], headers={"Total-Results": "1"})
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=parent)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/resolve",
        headers=auth_headers,
        params={"doi": "10.1000/resolve"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["strategy"] == "doi"
    assert payload["count"] == 1
    assert payload["items"][0]["itemKey"] == "ITEM0001"


async def test_resolve_items_endpoint_falls_back_to_full_library_scan_for_doi(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    parent = zotero_item(
        "ITEM0001",
        {
            "itemType": "journalArticle",
            "title": "Resolved By Fallback",
            "date": "2024-01-01",
            "DOI": "10.1000/fallback",
            "creators": [{"name": "Ada Lovelace", "creatorType": "author"}],
            "tags": [],
            "collections": [],
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items":
            return httpx.Response(200, json=[], headers={"Total-Results": "0"})
        if request.method == "GET" and request.url.path == "/users/123456/items/top":
            assert request.url.params.get("sort") == "dateModified"
            return httpx.Response(200, json=[parent], headers={"Total-Results": "1"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/resolve",
        headers=auth_headers,
        params={"doi": "10.1000/fallback"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["strategy"] == "doi"
    assert payload["count"] == 1
    assert payload["items"][0]["itemKey"] == "ITEM0001"


async def test_duplicates_endpoint_groups_title_and_doi_matches(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    first = zotero_item(
        "ITEM0001",
        {
            "itemType": "journalArticle",
            "title": "Duplicate Paper",
            "date": "2024-01-01",
            "DOI": "10.1000/dup",
            "creators": [{"name": "Ada Lovelace", "creatorType": "author"}],
            "tags": [],
            "collections": [],
        },
    )
    second = zotero_item(
        "ITEM0002",
        {
            "itemType": "journalArticle",
            "title": "Duplicate Paper",
            "date": "2024-01-02",
            "DOI": "10.1000/dup",
            "creators": [{"name": "Grace Hopper", "creatorType": "author"}],
            "tags": [],
            "collections": [],
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/top":
            return httpx.Response(
                200,
                json=[first, second],
                headers={"Total-Results": "2"},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/duplicates",
        headers=auth_headers,
        params={"by": "title,doi", "itemType": "journalArticle"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    assert payload["total"] == 2
    assert {group["field"] for group in payload["groups"]} == {"title", "doi"}


async def test_collections_and_tags_endpoints_return_library_metadata(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    collection = {
        "key": "COLL0001",
        "version": 1,
        "data": {"name": "Machine Learning", "parentCollection": False},
        "meta": {"numCollections": 2, "numItems": 7},
    }
    tag = {"tag": "ml", "type": 0, "meta": {"numItems": 4}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/collections":
            return httpx.Response(200, json=[collection], headers={"Total-Results": "1"})
        if request.method == "GET" and request.url.path == "/users/123456/items/top/tags":
            assert request.url.params.get("q") == "ml"
            return httpx.Response(200, json=[tag], headers={"Total-Results": "1"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    collections_response = await async_client.get("/v1/collections", headers=auth_headers)
    tags_response = await async_client.get(
        "/v1/tags",
        headers=auth_headers,
        params={"q": "ml"},
    )

    assert collections_response.status_code == 200
    assert collections_response.json()["collections"][0]["collectionKey"] == "COLL0001"
    assert collections_response.json()["collections"][0]["path"] == "Machine Learning"
    assert collections_response.json()["collections"][0]["depth"] == 0
    assert collections_response.json()["collections"][0]["numItems"] == 7
    assert collections_response.json()["total"] == 1
    assert tags_response.status_code == 200
    assert tags_response.json()["tags"][0]["tag"] == "ml"
    assert tags_response.json()["tags"][0]["numItems"] == 4


async def test_library_stats_endpoint_returns_counts(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    first = zotero_item(
        "ITEM0001",
        {
            "itemType": "journalArticle",
            "title": "Duplicate Paper",
            "date": "2024-01-01",
            "DOI": "10.1000/dup",
            "creators": [],
            "tags": [],
            "collections": [],
        },
    )
    second = zotero_item(
        "ITEM0002",
        {
            "itemType": "journalArticle",
            "title": "Duplicate Paper",
            "date": "2024-01-02",
            "DOI": "10.1000/dup",
            "creators": [],
            "tags": [],
            "collections": [],
        },
    )
    third = zotero_item(
        "ITEM0003",
        {
            "itemType": "book",
            "title": "Unique Book",
            "date": "2024-01-03",
            "creators": [],
            "tags": [],
            "collections": [],
        },
    )
    collection = {
        "key": "COLL0001",
        "version": 1,
        "data": {"name": "ML", "parentCollection": False},
        "meta": {"numCollections": 0, "numItems": 3},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/top":
            if request.url.params.get("format") == "versions":
                return httpx.Response(200, json={}, headers={"Last-Modified-Version": "42"})
            return httpx.Response(
                200,
                json=[first, second, third],
                headers={"Total-Results": "3"},
            )
        if request.method == "GET" and request.url.path == "/users/123456/collections":
            return httpx.Response(200, json=[collection], headers={"Total-Results": "1"})
        if request.method == "GET" and request.url.path == "/users/123456/items/top/tags":
            return httpx.Response(200, json=[], headers={"Total-Results": "4"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get("/v1/library/stats", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["totalItems"] == 3
    assert payload["itemTypeCounts"] == [
        {"itemType": "journalArticle", "count": 2},
        {"itemType": "book", "count": 1},
    ]
    assert payload["collectionCount"] == 1
    assert payload["tagCount"] == 4
    assert payload["duplicateGroups"] == {"titleGroups": 1, "doiGroups": 1}
    assert payload["lastModifiedVersion"] == 42
    assert payload["searchIndex"]["enabled"] is True
    assert payload["searchIndex"]["ready"] is False


async def test_library_stats_endpoint_reports_search_index_health(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    from app.main import app

    search_index = app.state.bridge_service._local_search_index
    assert search_index is not None
    search_index.replace_records(
        [
            {
                "itemKey": "ITEM0001",
                "itemType": "journalArticle",
                "title": "Indexed Paper",
                "dateAdded": "2024-01-01T00:00:00Z",
                "dateModified": "2024-01-02T00:00:00Z",
                "year": "2024",
                "DOI": "10.1000/indexed",
                "abstractNote": "Indexed abstract",
                "venue": "Journal",
                "creators": [],
                "tags": [],
                "collectionKeys": [],
                "noteText": "",
                "fulltextText": "cached fulltext",
            }
        ],
        last_modified_version=77,
        sync_method="rebuild",
    )

    item = zotero_item(
        "ITEM0001",
        {
            "itemType": "journalArticle",
            "title": "Indexed Paper",
            "creators": [],
            "tags": [],
            "collections": [],
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/top":
            if request.url.params.get("format") == "versions":
                return httpx.Response(200, json={}, headers={"Last-Modified-Version": "77"})
            return httpx.Response(200, json=[item], headers={"Total-Results": "1"})
        if request.method == "GET" and request.url.path == "/users/123456/collections":
            return httpx.Response(200, json=[], headers={"Total-Results": "0"})
        if request.method == "GET" and request.url.path == "/users/123456/items/top/tags":
            return httpx.Response(200, json=[], headers={"Total-Results": "0"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get("/v1/library/stats", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["searchIndex"]["enabled"] is True
    assert payload["searchIndex"]["ready"] is True
    assert payload["searchIndex"]["recordCount"] == 1
    assert payload["searchIndex"]["lastModifiedVersion"] == 77
    assert payload["searchIndex"]["lastSyncMethod"] == "rebuild"
    assert payload["searchIndex"]["refreshedAt"] is not None


async def test_item_changes_endpoint_supports_since_version_and_deleted_keys(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    changed_item = zotero_item(
        "ITEM0002",
        {
            "itemType": "journalArticle",
            "title": "Changed Paper",
            "date": "2024-01-01",
            "dateModified": "2025-01-01T00:00:00Z",
            "creators": [],
            "tags": [],
            "collections": [],
        },
        version=5,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/top":
            if (
                request.url.params.get("format") == "versions"
                and request.url.params.get("since") == "4"
            ):
                return httpx.Response(
                    200,
                    json={"ITEM0002": 5},
                    headers={"Last-Modified-Version": "6"},
                )
            if request.url.params.get("format") == "versions":
                return httpx.Response(200, json={}, headers={"Last-Modified-Version": "6"})
        if request.method == "GET" and request.url.path == "/users/123456/deleted":
            assert request.url.params.get("since") == "4"
            return httpx.Response(200, json={"items": ["OLD00001"]})
        if request.method == "GET" and request.url.path == "/users/123456/items":
            assert request.url.params.get("itemKey") == "ITEM0002"
            return httpx.Response(200, json=[changed_item], headers={"Total-Results": "1"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/changes",
        headers=auth_headers,
        params={"sinceVersion": 4},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["itemKey"] == "ITEM0002"
    assert payload["deletedItemKeys"] == ["OLD00001"]
    assert payload["deletedCount"] == 1
    assert payload["sinceVersion"] == 4
    assert payload["latestVersion"] == 6


async def test_item_changes_endpoint_supports_since_timestamp(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    fresh_item = zotero_item(
        "ITEM0002",
        {
            "itemType": "journalArticle",
            "title": "Fresh Paper",
            "dateModified": "2025-01-02T00:00:00Z",
            "creators": [],
            "tags": [],
            "collections": [],
        },
    )
    stale_item = zotero_item(
        "ITEM0001",
        {
            "itemType": "journalArticle",
            "title": "Old Paper",
            "dateModified": "2024-12-31T23:59:59Z",
            "creators": [],
            "tags": [],
            "collections": [],
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/top":
            if request.url.params.get("format") == "versions":
                return httpx.Response(200, json={}, headers={"Last-Modified-Version": "9"})
            return httpx.Response(
                200,
                json=[fresh_item, stale_item],
                headers={"Total-Results": "2"},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/changes",
        headers=auth_headers,
        params={"sinceTimestamp": "2025-01-01T00:00:00Z"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["itemKey"] == "ITEM0002"
    assert payload["sinceTimestamp"] == "2025-01-01T00:00:00Z"
    assert payload["latestVersion"] == 9


async def test_add_by_doi_created_then_existing(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    created_item = zotero_item(
        "ABCD1234",
        {
            "itemType": "journalArticle",
            "title": "Bridge Paper",
            "DOI": "10.1038/nrd842",
            "creators": [{"firstName": "Ada", "lastName": "Lovelace", "creatorType": "author"}],
            "tags": [{"tag": "inbox"}],
            "collections": [],
        },
    )
    state: dict[str, Any] = {"search_calls": 0, "create_requests": [], "created": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.zotero.org":
            if request.method == "GET" and request.url.path == "/users/123456/items":
                state["search_calls"] += 1
                payload = [created_item] if state["created"] else []
                return httpx.Response(200, json=payload)
            if request.method == "GET" and request.url.path == "/items/new":
                return httpx.Response(
                    200,
                    json={
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
                    },
                )
            if request.method == "POST" and request.url.path == "/users/123456/items":
                state["create_requests"].append(request)
                state["created"] = True
                return httpx.Response(
                    200,
                    json={"successful": {"0": {"key": "ABCD1234", "version": 1}}},
                )
            if request.method == "GET" and request.url.path == "/users/123456/items/ABCD1234":
                return httpx.Response(200, json=created_item)
        if request.url.host == "doi.org":
            assert request.url.path == "/10.1038/nrd842"
            return httpx.Response(
                200,
                json={
                    "type": "article-journal",
                    "title": "Bridge Paper",
                    "author": [{"given": "Ada", "family": "Lovelace"}],
                    "issued": {"date-parts": [[2025, 1, 1]]},
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    first = await async_client.post(
        "/v1/papers/add-by-doi",
        headers=auth_headers,
        json={"doi": "10.1038/nrd842", "tags": ["inbox"], "requestId": "req-1"},
    )
    second = await async_client.post(
        "/v1/papers/add-by-doi",
        headers=auth_headers,
        json={"doi": "10.1038/nrd842", "tags": ["inbox"], "requestId": "req-1"},
    )

    assert first.status_code == 200
    assert first.json()["status"] == "created"
    assert second.status_code == 200
    assert second.json()["status"] == "existing"

    posted = json.loads(state["create_requests"][0].content.decode("utf-8"))[0]
    assert posted["DOI"] == "10.1038/nrd842"
    assert state["create_requests"][0].headers["Zotero-Write-Token"]
    assert state["search_calls"] == 4


async def test_search_detail_and_citation(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    parent = zotero_item(
        "ITEM0001",
        {
            "itemType": "journalArticle",
            "title": "Neural Bridge",
            "date": "2024-09-01",
            "DOI": "10.1000/neural",
            "creators": [{"name": "Ada Lovelace", "creatorType": "author"}],
            "tags": [{"tag": "ml"}],
            "collections": ["COLL1"],
        },
    )
    children = [
        zotero_item(
            "ATTACH01",
            {
                "itemType": "attachment",
                "title": "paper.pdf",
                "filename": "paper.pdf",
                "contentType": "application/pdf",
                "linkMode": "imported_file",
                "md5": "abc",
                "mtime": "123",
            },
        ),
        zotero_item(
            "NOTE0001",
            {
                "itemType": "note",
                "note": "<p>hidden</p>",
                "dateModified": "2025-01-01T00:00:00Z",
                "tags": [
                    {"tag": "zbridge"},
                    {"tag": "zbridge:agent:codex"},
                    {"tag": "zbridge:type:summary"},
                    {"tag": "zbridge:slot:default"},
                ],
            },
        ),
    ]
    state: dict[str, Any] = {"item_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items":
            return httpx.Response(200, json=[parent])
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            state["item_calls"] += 1
            if state["item_calls"] < 3:
                return httpx.Response(200, json=parent)
            return httpx.Response(
                200,
                json={
                    **parent,
                    "citation": "<span>Ada (2024)</span>",
                    "bib": "<div>Bibliography</div>",
                },
            )
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            return httpx.Response(200, json=children)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    search_response = await async_client.get(
        "/v1/items/search",
        headers=auth_headers,
        params={"q": "neural", "limit": 1, "includeFulltext": "true"},
    )
    detail_response = await async_client.get("/v1/items/ITEM0001", headers=auth_headers)

    citation_response = await async_client.get(
        "/v1/items/ITEM0001/citation",
        headers=auth_headers,
    )

    assert search_response.status_code == 200
    assert search_response.json()["count"] == 1
    assert search_response.json()["items"][0]["attachments"][0]["attachmentKey"] == "ATTACH01"
    assert "body" not in search_response.text

    assert detail_response.status_code == 200
    assert detail_response.json()["item"]["aiNotes"][0]["noteKey"] == "NOTE0001"

    assert citation_response.status_code == 200
    assert citation_response.json()["citationHtml"] == "<span>Ada (2024)</span>"


async def test_get_item_citation_uses_configured_defaults(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: respx.MockRouter,
) -> None:
    from app.config import get_settings

    monkeypatch.setenv("DEFAULT_CITATION_STYLE", "chicago-author-date")
    monkeypatch.setenv("DEFAULT_CITATION_LOCALE", "fr-FR")
    get_settings.cache_clear()

    parent = zotero_item(
        "ITEM0001",
        {
            "itemType": "journalArticle",
            "title": "Neural Bridge",
            "date": "2024-09-01",
            "DOI": "10.1000/neural",
            "creators": [{"name": "Ada Lovelace", "creatorType": "author"}],
            "tags": [{"tag": "ml"}],
            "collections": ["COLL1"],
        },
    )
    state: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            state["style"] = request.url.params.get("style")
            state["locale"] = request.url.params.get("locale")
            return httpx.Response(
                200,
                json={
                    **parent,
                    "citation": "<span>Ada (2024)</span>",
                    "bib": "<div>Bibliography</div>",
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/ITEM0001/citation",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert state == {"style": "chicago-author-date", "locale": "fr-FR"}
    assert response.json()["style"] == "chicago-author-date"
    assert response.json()["locale"] == "fr-FR"
    get_settings.cache_clear()


async def test_item_tag_and_collection_write_endpoints_update_metadata(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    state: dict[str, Any] = {
        "item": zotero_item(
            "ITEM0001",
            {
                "itemType": "journalArticle",
                "title": "Managed Paper",
                "tags": [{"tag": "keep"}],
                "collections": ["COLL0001"],
                "creators": [],
            },
            version=3,
        ),
        "patches": [],
    }
    collections = [
        {
            "key": "COLL0001",
            "version": 1,
            "data": {"name": "Existing", "parentCollection": False},
            "meta": {"numCollections": 0, "numItems": 1},
        },
        {
            "key": "COLL0002",
            "version": 1,
            "data": {"name": "Added", "parentCollection": False},
            "meta": {"numCollections": 0, "numItems": 1},
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=state["item"])
        if request.method == "PATCH" and request.url.path == "/users/123456/items/ITEM0001":
            payload = json.loads(request.content.decode("utf-8"))
            state["patches"].append(payload)
            item_data = state["item"]["data"]
            if "tags" in payload:
                item_data["tags"] = payload["tags"]
            if "collections" in payload:
                item_data["collections"] = payload["collections"]
            state["item"]["version"] += 1
            return httpx.Response(204)
        if request.method == "GET" and request.url.path == "/users/123456/collections":
            return httpx.Response(200, json=collections, headers={"Total-Results": "2"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    add_tags_response = await async_client.post(
        "/v1/items/ITEM0001/tags",
        headers=auth_headers,
        json={"tags": ["new-tag", "keep"]},
    )
    remove_tag_response = await async_client.delete(
        "/v1/items/ITEM0001/tags/new-tag",
        headers=auth_headers,
    )
    add_collection_response = await async_client.post(
        "/v1/items/ITEM0001/collections",
        headers=auth_headers,
        json={"collectionKeys": ["COLL0002"]},
    )

    assert add_tags_response.status_code == 200
    assert add_tags_response.json()["addedTags"] == ["new-tag"]
    assert add_tags_response.json()["tags"] == ["keep", "new-tag"]
    assert remove_tag_response.status_code == 200
    assert remove_tag_response.json()["removedTag"] == "new-tag"
    assert remove_tag_response.json()["tags"] == ["keep"]
    assert add_collection_response.status_code == 200
    assert add_collection_response.json()["addedCollectionKeys"] == ["COLL0002"]
    assert add_collection_response.json()["collectionKeys"] == ["COLL0001", "COLL0002"]
    assert len(state["patches"]) == 3


async def test_batch_fulltext_preview_endpoint_returns_successes_and_errors(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    item = zotero_item(
        "ITEM0001",
        {"itemType": "journalArticle", "title": "Preview Paper", "creators": []},
    )
    children = [
        zotero_item(
            "ATTACH01",
            {
                "itemType": "attachment",
                "title": "paper.pdf",
                "filename": "paper.pdf",
                "contentType": "application/pdf",
                "linkMode": "imported_file",
            },
        )
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=item)
        if request.method == "GET" and request.url.path == "/users/123456/items/MISSING99":
            return httpx.Response(404)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            return httpx.Response(200, json=children)
        if request.method == "GET" and request.url.path == "/users/123456/items/ATTACH01/fulltext":
            return httpx.Response(
                200,
                json={"content": "Preview text content.", "indexedPages": 1, "totalPages": 1},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.post(
        "/v1/items/fulltext/batch-preview",
        headers=auth_headers,
        json={"itemKeys": ["ITEM0001", "MISSING99"], "maxChars": 1000, "preferSource": "auto"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    assert payload["items"][0]["attachmentKey"] == "ATTACH01"
    assert payload["items"][0]["content"] == "Preview text content."
    assert payload["items"][1]["errorCode"] == "ITEM_NOT_FOUND"


async def test_batch_fulltext_preview_endpoint_rejects_small_max_chars(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await async_client.post(
        "/v1/items/fulltext/batch-preview",
        headers=auth_headers,
        json={"itemKeys": ["ITEM0001"], "maxChars": 800, "preferSource": "auto"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"
    assert (
        response.json()["error"]["message"]
        == "maxChars: Input should be greater than or equal to 1000"
    )


async def test_related_items_endpoint_returns_resolved_related_items(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    item = zotero_item(
        "ITEM0001",
        {
            "itemType": "journalArticle",
            "title": "Seed Paper",
            "creators": [],
            "relations": {"dc:relation": ["https://api.zotero.org/users/123456/items/ITEM0002"]},
        },
    )
    related = zotero_item(
        "ITEM0002",
        {
            "itemType": "journalArticle",
            "title": "Related Paper",
            "creators": [],
            "tags": [],
            "collections": [],
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=item)
        if request.method == "GET" and request.url.path == "/users/123456/items":
            assert request.url.params.get("itemKey") == "ITEM0002"
            return httpx.Response(200, json=[related], headers={"Total-Results": "1"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get("/v1/items/ITEM0001/related", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["itemKey"] == "ITEM0001"
    assert payload["count"] == 1
    assert payload["items"][0]["itemKey"] == "ITEM0002"


async def test_review_pack_endpoint_returns_rich_bundle(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    item = zotero_item(
        "ITEM0001",
        {
            "itemType": "journalArticle",
            "title": "Review Pack Paper",
            "date": "2024-02-01",
            "abstractNote": "A concise abstract.",
            "publicationTitle": "Journal of Tests",
            "creators": [{"name": "Ada Lovelace", "creatorType": "author"}],
            "tags": [],
            "collections": [],
            "relations": {"dc:relation": ["https://api.zotero.org/users/123456/items/ITEM0002"]},
        },
    )
    related = zotero_item(
        "ITEM0002",
        {
            "itemType": "journalArticle",
            "title": "Related Paper",
            "creators": [],
            "tags": [],
            "collections": [],
        },
    )
    children = [
        zotero_item(
            "ATTACH01",
            {
                "itemType": "attachment",
                "title": "paper.pdf",
                "filename": "paper.pdf",
                "contentType": "application/pdf",
                "linkMode": "imported_file",
            },
        ),
        zotero_item(
            "NOTE0001",
            {
                "itemType": "note",
                "parentItem": "ITEM0001",
                "note": "<p>Important note.</p>",
                "dateModified": "2025-01-01T00:00:00Z",
                "tags": [{"tag": "manual"}],
            },
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            if request.url.params.get("include") == "citation,bib":
                return httpx.Response(
                    200,
                    json={**item, "citation": "<span>Citation</span>", "bib": "<div>Bib</div>"},
                )
            return httpx.Response(200, json=item)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            return httpx.Response(200, json=children)
        if request.method == "GET" and request.url.path == "/users/123456/items":
            assert request.url.params.get("itemKey") == "ITEM0002"
            return httpx.Response(200, json=[related], headers={"Total-Results": "1"})
        if request.method == "GET" and request.url.path == "/users/123456/items/ATTACH01/fulltext":
            return httpx.Response(
                200,
                json={"content": "Full text preview.", "indexedPages": 1, "totalPages": 1},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.post(
        "/v1/items/review-pack",
        headers=auth_headers,
        json={"itemKeys": ["ITEM0001"], "maxFulltextChars": 1000},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["item"]["abstractNote"] == "A concise abstract."
    assert payload["items"][0]["citation"]["citationHtml"] == "<span>Citation</span>"
    assert payload["items"][0]["fulltextPreview"]["content"] == "Full text preview."
    assert payload["items"][0]["notes"][0]["noteKey"] == "NOTE0001"
    assert payload["items"][0]["relatedItems"][0]["itemKey"] == "ITEM0002"


async def test_search_advanced_fulltext_field_uses_local_indexed_cache(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    from app.main import app

    parent = zotero_item(
        "ITEM0001",
        {
            "itemType": "journalArticle",
            "title": "Indexed Local Fulltext Paper",
            "creators": [{"name": "Ada Lovelace", "creatorType": "author"}],
            "tags": [],
            "collections": [],
        },
    )
    children = [
        zotero_item(
            "ATTACH01",
            {
                "itemType": "attachment",
                "title": "paper.pdf",
                "filename": "paper.pdf",
                "contentType": "application/pdf",
                "linkMode": "imported_file",
            },
        )
    ]

    store = app.state.bridge_service._local_fulltext_store
    assert store is not None
    store.write_payload(
        attachment_key="ATTACH01",
        item_key="ITEM0001",
        filename="paper.pdf",
        fulltext_payload={
            "content": "semantic cache token from indexed local fulltext",
            "indexedPages": 1,
            "totalPages": 1,
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/top":
            if request.url.params.get("format") == "versions":
                return httpx.Response(
                    200,
                    json={"ITEM0001": 3},
                    headers={"Last-Modified-Version": "3"},
                )
            return httpx.Response(200, json=[parent], headers={"Total-Results": "1"})
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            return httpx.Response(200, json=children)
        if request.method == "GET" and request.url.path == "/users/123456/items":
            if request.url.params.get("itemKey") == "ITEM0001":
                return httpx.Response(200, json=[parent], headers={"Total-Results": "1"})
            return httpx.Response(200, json=[], headers={"Total-Results": "0"})
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=parent)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/search-advanced",
        headers=auth_headers,
        params={"q": "semantic cache token", "fields": "fulltext", "sort": "relevance"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["itemKey"] == "ITEM0001"
    assert payload["items"][0]["searchHints"][0]["field"] == "local_cache_fulltext"


async def test_review_pack_endpoint_rejects_small_max_fulltext_chars(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await async_client.post(
        "/v1/items/review-pack",
        headers=auth_headers,
        json={"itemKeys": ["ITEM0001"], "maxFulltextChars": 800},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"
    assert (
        response.json()["error"]["message"]
        == "maxFulltextChars: Input should be greater than or equal to 1000"
    )


async def test_merge_duplicate_items_endpoint_moves_children_and_deletes_duplicates(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    state: dict[str, Any] = {
        "primary": zotero_item(
            "PRIM0001",
            {
                "itemType": "journalArticle",
                "title": "Primary Paper",
                "tags": [{"tag": "keep"}],
                "collections": ["COLL0001"],
                "creators": [],
            },
            version=3,
        ),
        "duplicates": {
            "DUPL0001": zotero_item(
                "DUPL0001",
                {
                    "itemType": "journalArticle",
                    "title": "Duplicate A",
                    "tags": [{"tag": "extra"}],
                    "collections": ["COLL0002"],
                    "creators": [],
                },
                version=2,
            ),
            "DUPL0002": zotero_item(
                "DUPL0002",
                {
                    "itemType": "journalArticle",
                    "title": "Duplicate B",
                    "tags": [],
                    "collections": [],
                    "creators": [],
                },
                version=4,
            ),
        },
        "patches": [],
        "deletes": [],
    }
    duplicate_children = {
        "DUPL0001": [
            zotero_item(
                "ATTACH01",
                {"itemType": "attachment", "parentItem": "DUPL0001", "title": "paper.pdf"},
                version=1,
            ),
            zotero_item(
                "NOTE0001",
                {"itemType": "note", "parentItem": "DUPL0001", "note": "<p>note</p>"},
                version=1,
            ),
        ],
        "DUPL0002": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/PRIM0001":
            return httpx.Response(200, json=state["primary"])
        if request.method == "GET" and request.url.path == "/users/123456/items":
            assert request.url.params.get("itemKey") == "DUPL0001,DUPL0002"
            return httpx.Response(
                200,
                json=[state["duplicates"]["DUPL0001"], state["duplicates"]["DUPL0002"]],
                headers={"Total-Results": "2"},
            )
        if request.method == "GET" and request.url.path == "/users/123456/items/DUPL0001/children":
            return httpx.Response(200, json=duplicate_children["DUPL0001"])
        if request.method == "GET" and request.url.path == "/users/123456/items/DUPL0002/children":
            return httpx.Response(200, json=duplicate_children["DUPL0002"])
        if request.method == "PATCH" and request.url.path == "/users/123456/items/PRIM0001":
            payload = json.loads(request.content.decode("utf-8"))
            state["patches"].append(("primary", payload))
            state["primary"]["data"]["tags"] = payload["tags"]
            state["primary"]["data"]["collections"] = payload["collections"]
            state["primary"]["version"] += 1
            return httpx.Response(204)
        if request.method == "PATCH" and request.url.path in {
            "/users/123456/items/ATTACH01",
            "/users/123456/items/NOTE0001",
        }:
            payload = json.loads(request.content.decode("utf-8"))
            state["patches"].append((request.url.path, payload))
            return httpx.Response(204)
        if request.method == "DELETE" and request.url.path in {
            "/users/123456/items/DUPL0001",
            "/users/123456/items/DUPL0002",
        }:
            state["deletes"].append(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.post(
        "/v1/items/duplicates/merge",
        headers=auth_headers,
        json={
            "primaryItemKey": "PRIM0001",
            "duplicateItemKeys": ["DUPL0001", "DUPL0002"],
            "dryRun": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "merged"
    assert payload["primaryItem"]["itemKey"] == "PRIM0001"
    assert payload["addedTags"] == ["extra"]
    assert payload["addedCollectionKeys"] == ["COLL0002"]
    assert payload["movedAttachmentKeys"] == ["ATTACH01"]
    assert payload["movedNoteKeys"] == ["NOTE0001"]
    assert payload["deletedItemKeys"] == ["DUPL0001", "DUPL0002"]
    assert state["deletes"] == ["DUPL0001", "DUPL0002"]


async def test_discovery_search_endpoint_returns_openalex_results(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.openalex.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/works":
            assert request.url.params.get("search") == "knowledge spillovers"
            assert (
                request.url.params.get("filter")
                == "from_publication_date:2020-01-01,is_oa:true"
            )
            assert request.url.params.get("sort") == "cited_by_count:desc"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "https://openalex.org/W123",
                            "doi": "https://doi.org/10.1000/discovery",
                            "display_name": "Knowledge Spillovers and Trade",
                            "publication_year": 2024,
                            "publication_date": "2024-01-01",
                            "type": "article",
                            "cited_by_count": 12,
                            "authorships": [
                                {
                                    "author": {
                                        "id": "https://openalex.org/A1",
                                        "display_name": "Ada Lovelace",
                                    }
                                }
                            ],
                            "primary_location": {
                                "landing_page_url": "https://example.org/paper",
                                "pdf_url": "https://example.org/paper.pdf",
                                "source": {"display_name": "Journal of Trade"},
                            },
                            "open_access": {"is_oa": True},
                            "abstract_inverted_index": {
                                "Knowledge": [0],
                                "spillovers": [1],
                            },
                            "primary_topic": {
                                "id": "https://openalex.org/T1",
                                "display_name": "International Trade",
                                "score": 0.9,
                            },
                        }
                    ],
                    "meta": {"count": 1},
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/discovery/search",
        headers=auth_headers,
        params={
            "q": "knowledge spillovers",
            "yearFrom": 2020,
            "oaOnly": "true",
            "resolveInLibrary": "false",
            "sort": "cited_by",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["openAlexId"] == "https://openalex.org/W123"
    assert payload["items"][0]["title"] == "Knowledge Spillovers and Trade"
    assert payload["items"][0]["venue"] == "Journal of Trade"
    assert payload["items"][0]["abstract"] == "Knowledge spillovers"
    assert payload["items"][0]["authors"][0]["name"] == "Ada Lovelace"
    assert payload["items"][0]["alreadyInLibrary"] is False
    assert payload["items"][0]["topics"][0]["name"] == "International Trade"


async def test_discovery_search_endpoint_resolves_and_excludes_existing_items(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    from app.main import app

    search_index = app.state.bridge_service._local_search_index
    assert search_index is not None
    search_index.replace_records(
        [
            {
                "itemKey": "ITEM0001",
                "itemType": "journalArticle",
                "title": "Knowledge Spillovers and Trade",
                "dateAdded": "2024-01-01T00:00:00Z",
                "dateModified": "2024-01-02T00:00:00Z",
                "year": "2024",
                "DOI": "10.1000/discovery",
                "abstractNote": "Indexed abstract",
                "venue": "Journal of Trade",
                "creators": [],
                "tags": [],
                "collectionKeys": [],
                "noteText": "",
                "fulltextText": "",
            }
        ],
        last_modified_version=15,
        sync_method="rebuild",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.openalex.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/works":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "https://openalex.org/W123",
                            "doi": "https://doi.org/10.1000/discovery",
                            "display_name": "Knowledge Spillovers and Trade",
                            "publication_year": 2024,
                            "publication_date": "2024-01-01",
                            "type": "article",
                            "cited_by_count": 12,
                            "authorships": [],
                            "primary_location": {"source": {"display_name": "Journal of Trade"}},
                            "open_access": {"is_oa": True},
                            "abstract_inverted_index": {"Knowledge": [0], "spillovers": [1]},
                            "primary_topic": {},
                        },
                        {
                            "id": "https://openalex.org/W999",
                            "doi": "https://doi.org/10.1000/new-paper",
                            "display_name": "New Discovery Paper",
                            "publication_year": 2025,
                            "publication_date": "2025-02-01",
                            "type": "article",
                            "cited_by_count": 3,
                            "authorships": [],
                            "primary_location": {"source": {"display_name": "New Journal"}},
                            "open_access": {"is_oa": False},
                            "abstract_inverted_index": {"New": [0], "paper": [1]},
                            "primary_topic": {},
                        },
                    ],
                    "meta": {"count": 2},
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    resolve_response = await async_client.get(
        "/v1/discovery/search",
        headers=auth_headers,
        params={"q": "knowledge spillovers", "resolveInLibrary": "true"},
    )
    exclude_response = await async_client.get(
        "/v1/discovery/search",
        headers=auth_headers,
        params={
            "q": "knowledge spillovers",
            "resolveInLibrary": "true",
            "excludeExisting": "true",
        },
    )

    assert resolve_response.status_code == 200
    resolved_payload = resolve_response.json()
    assert resolved_payload["items"][0]["alreadyInLibrary"] is True
    assert resolved_payload["items"][0]["libraryItemKey"] == "ITEM0001"
    assert resolved_payload["items"][0]["libraryMatchStrategy"] == "doi"

    assert exclude_response.status_code == 200
    excluded_payload = exclude_response.json()
    assert excluded_payload["count"] == 1
    assert excluded_payload["items"][0]["title"] == "New Discovery Paper"
    assert excluded_payload["items"][0]["alreadyInLibrary"] is False


async def test_discovery_search_exclude_existing_paginates_by_raw_openalex_offsets(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    from app.main import app

    search_index = app.state.bridge_service._local_search_index
    assert search_index is not None
    search_index.replace_records(
        [
            {
                "itemKey": "ITEM0001",
                "itemType": "journalArticle",
                "title": "Knowledge Spillovers and Trade",
                "dateAdded": "2024-01-01T00:00:00Z",
                "dateModified": "2024-01-02T00:00:00Z",
                "year": "2024",
                "DOI": "10.1000/discovery",
                "abstractNote": "Indexed abstract",
                "venue": "Journal of Trade",
                "creators": [],
                "tags": [],
                "collectionKeys": [],
                "noteText": "",
                "fulltextText": "",
            }
        ],
        last_modified_version=15,
        sync_method="rebuild",
    )

    existing_result = {
        "id": "https://openalex.org/W123",
        "doi": "https://doi.org/10.1000/discovery",
        "display_name": "Knowledge Spillovers and Trade",
        "publication_year": 2024,
        "publication_date": "2024-01-01",
        "type": "article",
        "cited_by_count": 12,
        "authorships": [],
        "primary_location": {"source": {"display_name": "Journal of Trade"}},
        "open_access": {"is_oa": True},
        "abstract_inverted_index": {"Knowledge": [0], "spillovers": [1]},
        "primary_topic": {},
    }
    new_result = {
        "id": "https://openalex.org/W999",
        "doi": "https://doi.org/10.1000/new-paper",
        "display_name": "New Discovery Paper",
        "publication_year": 2025,
        "publication_date": "2025-02-01",
        "type": "article",
        "cited_by_count": 3,
        "authorships": [],
        "primary_location": {"source": {"display_name": "New Journal"}},
        "open_access": {"is_oa": False},
        "abstract_inverted_index": {"New": [0], "paper": [1]},
        "primary_topic": {},
    }
    second_new_result = {
        **new_result,
        "id": "https://openalex.org/W1000",
        "doi": "https://doi.org/10.1000/new-paper-2",
        "display_name": "Another Discovery Paper",
    }
    seen_pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.openalex.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/works":
            page = str(request.url.params.get("page") or "")
            seen_pages.append(page)
            if page == "1":
                return httpx.Response(
                    200,
                    json={"results": [existing_result] * 200, "meta": {"count": 202}},
                )
            if page == "2":
                return httpx.Response(
                    200,
                    json={"results": [new_result, second_new_result], "meta": {"count": 202}},
                )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/discovery/search",
        headers=auth_headers,
        params={
            "q": "knowledge spillovers",
            "limit": 1,
            "resolveInLibrary": "true",
            "excludeExisting": "true",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["title"] == "New Discovery Paper"
    assert payload["nextStart"] == 201
    assert seen_pages == ["1", "2"]


async def test_discovery_search_waits_for_local_index_when_resolving_matches(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: respx.MockRouter,
) -> None:
    from app.main import app

    service = app.state.bridge_service
    search_index = service._local_search_index
    assert search_index is not None
    assert search_index.is_ready() is False
    state = {"ensure_calls": 0}

    async def fake_ensure_ready() -> None:
        state["ensure_calls"] += 1
        search_index.replace_records(
            [
                {
                    "itemKey": "ITEM0001",
                    "itemType": "journalArticle",
                    "title": "Knowledge Spillovers and Trade",
                    "dateAdded": "2024-01-01T00:00:00Z",
                    "dateModified": "2024-01-02T00:00:00Z",
                    "year": "2024",
                    "DOI": "10.1000/discovery",
                    "abstractNote": "Indexed abstract",
                    "venue": "Journal of Trade",
                    "creators": [],
                    "tags": [],
                    "collectionKeys": [],
                    "noteText": "",
                    "fulltextText": "",
                }
            ],
            last_modified_version=15,
            sync_method="rebuild",
        )

    monkeypatch.setattr(service, "_ensure_local_search_index_ready", fake_ensure_ready)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.openalex.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/works":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "https://openalex.org/W123",
                            "doi": "https://doi.org/10.1000/discovery",
                            "display_name": "Knowledge Spillovers and Trade",
                            "publication_year": 2024,
                            "publication_date": "2024-01-01",
                            "type": "article",
                            "cited_by_count": 12,
                            "authorships": [],
                            "primary_location": {"source": {"display_name": "Journal of Trade"}},
                            "open_access": {"is_oa": True},
                            "abstract_inverted_index": {"Knowledge": [0], "spillovers": [1]},
                            "primary_topic": {},
                        }
                    ],
                    "meta": {"count": 1},
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/discovery/search",
        headers=auth_headers,
        params={"q": "knowledge spillovers", "resolveInLibrary": "true"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"][0]["alreadyInLibrary"] is True
    assert payload["items"][0]["libraryItemKey"] == "ITEM0001"
    assert payload["items"][0]["libraryMatchStrategy"] == "doi"
    assert state["ensure_calls"] == 1


async def test_search_uses_local_fulltext_cache_when_upstream_misses(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    from app.main import app

    parent = zotero_item(
        "ITEM0001",
        {
            "itemType": "journalArticle",
            "title": "Local Cache Paper",
            "date": "2025-01-01",
            "DOI": "10.1000/local-cache",
            "creators": [{"name": "Ada Lovelace", "creatorType": "author"}],
            "tags": [],
            "collections": [],
        },
    )
    children = [
        zotero_item(
            "ATTACH01",
            {
                "itemType": "attachment",
                "title": "paper.pdf",
                "filename": "paper.pdf",
                "contentType": "application/pdf",
                "linkMode": "imported_file",
                "md5": "abc",
                "mtime": "123",
            },
        )
    ]

    store = app.state.bridge_service._local_fulltext_store
    assert store is not None
    store.write_payload(
        attachment_key="ATTACH01",
        item_key="ITEM0001",
        filename="paper.pdf",
        fulltext_payload={
            "content": "semantic cache token from local extraction",
            "indexedPages": 1,
            "totalPages": 1,
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items":
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            return httpx.Response(200, json=children)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/search",
        headers=auth_headers,
        params={"q": "semantic cache token", "limit": 5, "includeFulltext": "true"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["itemKey"] == "ITEM0001"
    assert payload["items"][0]["attachments"][0]["attachmentKey"] == "ATTACH01"


async def test_search_skips_stale_local_fulltext_cache_entries(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    from app.main import app

    store = app.state.bridge_service._local_fulltext_store
    assert store is not None
    store.write_payload(
        attachment_key="ATTACHSTALE",
        item_key="STALE001",
        filename="stale.pdf",
        fulltext_payload={
            "content": "stale semantic cache token",
            "indexedPages": 1,
            "totalPages": 1,
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items":
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path == "/users/123456/items/STALE001":
            return httpx.Response(404)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/search",
        headers=auth_headers,
        params={"q": "stale semantic cache token", "limit": 5, "includeFulltext": "true"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "count": 0,
        "total": 0,
        "start": 0,
        "limit": 5,
        "nextStart": None,
    }
    assert store.search_item_keys("stale semantic cache token", limit=5) == []


async def test_search_returns_top_level_attachment_hits(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    attachment = zotero_item(
        "ATTACHTOP",
        {
            "itemType": "attachment",
            "title": "top-level-attachment.pdf",
            "filename": "top-level-attachment.pdf",
            "contentType": "application/pdf",
            "linkMode": "imported_file",
            "md5": "abc",
            "mtime": "123",
            "collections": [],
            "tags": [{"tag": "pdf"}],
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items":
            return httpx.Response(200, json=[attachment], headers={"Total-Results": "1"})
        if request.method == "GET" and request.url.path == "/users/123456/items/ATTACHTOP":
            return httpx.Response(200, json=attachment)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/search",
        headers=auth_headers,
        params={
            "q": "top-level-attachment",
            "includeFulltext": "false",
            "includeAttachments": "false",
            "includeNotes": "false",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["itemKey"] == "ATTACHTOP"
    assert payload["items"][0]["itemType"] == "attachment"


async def test_search_uses_legacy_top_level_attachment_fulltext_cache(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    from app.main import app

    store = app.state.bridge_service._local_fulltext_store
    assert store is not None
    store.write_payload(
        attachment_key="ATTLEG01",
        item_key=None,
        filename="legacy-top-level.pdf",
        fulltext_payload={
            "content": "legacy standalone cache token",
            "indexedPages": 1,
            "totalPages": 1,
        },
    )

    attachment = zotero_item(
        "ATTLEG01",
        {
            "itemType": "attachment",
            "title": "legacy-top-level.pdf",
            "filename": "legacy-top-level.pdf",
            "contentType": "application/pdf",
            "linkMode": "imported_file",
            "collections": [],
            "tags": [],
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items":
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path == "/users/123456/items/ATTLEG01":
            return httpx.Response(200, json=attachment)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/search",
        headers=auth_headers,
        params={
            "q": "legacy standalone cache token",
            "limit": 5,
            "includeFulltext": "true",
            "includeAttachments": "false",
            "includeNotes": "false",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["itemKey"] == "ATTLEG01"
    assert payload["items"][0]["itemType"] == "attachment"
    assert payload["items"][0]["searchHints"][0]["field"] == "local_cache_fulltext"


async def test_search_backfills_valid_local_fulltext_after_stale_hit(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    from app.main import app

    parent = zotero_item(
        "ITEM0001",
        {
            "itemType": "journalArticle",
            "title": "Recovered Local Cache Paper",
            "creators": [{"name": "Ada Lovelace", "creatorType": "author"}],
            "tags": [],
            "collections": [],
        },
    )
    children = [
        zotero_item(
            "ATTACH01",
            {
                "itemType": "attachment",
                "title": "paper.pdf",
                "filename": "paper.pdf",
                "contentType": "application/pdf",
                "linkMode": "imported_file",
                "md5": "abc",
                "mtime": "123",
            },
        )
    ]

    store = app.state.bridge_service._local_fulltext_store
    assert store is not None
    store.write_payload(
        attachment_key="ATTACHVALID",
        item_key="ITEM0001",
        filename="valid.pdf",
        fulltext_payload={
            "content": "backfill token from valid cache entry",
            "indexedPages": 1,
            "totalPages": 1,
        },
    )
    store.write_payload(
        attachment_key="ATTACHSTALE2",
        item_key="STALE002",
        filename="stale.pdf",
        fulltext_payload={
            "content": "backfill token from stale cache entry",
            "indexedPages": 1,
            "totalPages": 1,
        },
    )
    stale_path = store._record_path("ATTACHSTALE2")
    stat = stale_path.stat()
    os.utime(stale_path, (stat.st_atime + 10, stat.st_mtime + 10))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items":
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path == "/users/123456/items/STALE002":
            return httpx.Response(404)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            return httpx.Response(200, json=children)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/search",
        headers=auth_headers,
        params={"q": "backfill token", "limit": 1, "includeFulltext": "true"},
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["itemKey"] == "ITEM0001"
    assert store.search_item_keys("stale cache entry", limit=5) == []


async def test_fulltext_endpoint(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    parent = zotero_item(
        "ITEM0001",
        {"itemType": "journalArticle", "title": "Neural Bridge", "creators": []},
    )
    children = [
        zotero_item(
            "ATTACH01",
            {
                "itemType": "attachment",
                "title": "paper.pdf",
                "filename": "paper.pdf",
                "contentType": "application/pdf",
                "linkMode": "imported_file",
                "dateModified": "2025-01-01T00:00:00Z",
            },
        )
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            return httpx.Response(200, json=children)
        if request.method == "GET" and request.url.path == "/users/123456/items/ATTACH01/fulltext":
            return httpx.Response(
                200,
                json={
                    "content": (
                        "Paragraph one.\n\nParagraph two is longer than the first paragraph."
                    ),
                    "indexedPages": 2,
                    "totalPages": 2,
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/ITEM0001/fulltext",
        headers=auth_headers,
        params={"maxChars": 1000},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["attachmentKey"] == "ATTACH01"
    assert payload["source"] == "zotero_web_api"
    assert payload["done"] is True


async def test_fulltext_endpoint_falls_back_to_local_cache(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    from app.main import app

    parent = zotero_item(
        "ITEM0001",
        {"itemType": "journalArticle", "title": "Neural Bridge", "creators": []},
    )
    children = [
        zotero_item(
            "ATTACH01",
            {
                "itemType": "attachment",
                "title": "paper.pdf",
                "filename": "paper.pdf",
                "contentType": "application/pdf",
                "linkMode": "imported_file",
                "dateModified": "2025-01-01T00:00:00Z",
            },
        )
    ]

    store = app.state.bridge_service._local_fulltext_store
    assert store is not None
    store.write_payload(
        attachment_key="ATTACH01",
        item_key="ITEM0001",
        filename="paper.pdf",
        fulltext_payload={
            "content": "Local fallback content for pdf extraction.",
            "indexedPages": 1,
            "totalPages": 1,
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            return httpx.Response(200, json=children)
        if request.method == "GET" and request.url.path == "/users/123456/items/ATTACH01/fulltext":
            return httpx.Response(404)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/ITEM0001/fulltext",
        headers=auth_headers,
        params={"maxChars": 1000},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "local_cache"
    assert payload["content"] == "Local fallback content for pdf extraction."


async def test_fulltext_endpoint_accepts_top_level_attachment_items(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    from app.main import app

    attachment = zotero_item(
        "ATTTOP01",
        {
            "itemType": "attachment",
            "title": "top-level.pdf",
            "filename": "top-level.pdf",
            "contentType": "application/pdf",
            "linkMode": "imported_file",
            "dateModified": "2025-01-01T00:00:00Z",
        },
    )

    store = app.state.bridge_service._local_fulltext_store
    assert store is not None
    store.write_payload(
        attachment_key="ATTTOP01",
        item_key=None,
        filename="top-level.pdf",
        fulltext_payload={
            "content": "Top-level attachment local cache content.",
            "indexedPages": 1,
            "totalPages": 1,
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/ATTTOP01":
            return httpx.Response(200, json=attachment)
        if request.method == "GET" and request.url.path == "/users/123456/items/ATTTOP01/fulltext":
            return httpx.Response(404)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/ATTTOP01/fulltext",
        headers=auth_headers,
        params={"maxChars": 1000},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["itemKey"] == "ATTTOP01"
    assert payload["attachmentKey"] == "ATTTOP01"
    assert payload["source"] == "local_cache"
    assert payload["content"] == "Top-level attachment local cache content."


async def test_upsert_ai_note_create_then_update(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    parent = zotero_item(
        "ITEM0001",
        {"itemType": "journalArticle", "title": "Paper", "creators": []},
    )
    existing_note = zotero_item(
        "NOTE0001",
        {
            "itemType": "note",
            "note": "<p>old</p>",
            "dateModified": "2025-01-01T00:00:00Z",
            "tags": [
                {"tag": "zbridge"},
                {"tag": "zbridge:agent:codex"},
                {"tag": "zbridge:type:summary"},
                {"tag": "zbridge:slot:default"},
            ],
        },
        version=5,
    )
    state: dict[str, Any] = {"child_calls": 0, "created": False, "patched": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            state["child_calls"] += 1
            payload = [] if state["child_calls"] == 1 else [existing_note]
            return httpx.Response(200, json=payload)
        if request.method == "POST" and request.url.path == "/users/123456/items":
            state["created"] = True
            return httpx.Response(
                200,
                json={"successful": {"0": {"key": "NOTE0001", "version": 6}}},
            )
        if request.method == "PATCH" and request.url.path == "/users/123456/items/NOTE0001":
            state["patched"] = True
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    create_response = await async_client.post(
        "/v1/items/ITEM0001/notes/upsert-ai-note",
        headers=auth_headers,
        json={
            "agent": "codex",
            "noteType": "summary",
            "bodyMarkdown": "First pass",
            "requestId": "note-1",
        },
    )
    update_response = await async_client.post(
        "/v1/items/ITEM0001/notes/upsert-ai-note",
        headers=auth_headers,
        json={
            "agent": "codex",
            "noteType": "summary",
            "bodyMarkdown": "Second pass",
            "mode": "append",
            "requestId": "note-2",
        },
    )

    assert create_response.status_code == 200
    assert create_response.json()["status"] == "created"
    assert update_response.status_code == 200
    assert update_response.json()["status"] == "updated"
    assert state["created"] is True
    assert state["patched"] is True


async def test_upsert_ai_note_replays_append_request_without_duplicate_write(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    parent = zotero_item(
        "ITEM0001",
        {"itemType": "journalArticle", "title": "Paper", "creators": []},
    )
    request_id = "ai-note-append-replay-1"
    request_token = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
    request_sig = hashlib.sha256(
        json.dumps(
            {
                "agent": "codex",
                "noteType": "summary",
                "slot": "default",
                "mode": "append",
                "title": None,
                "bodyMarkdown": "Second pass",
                "tags": [],
                "model": None,
                "sourceAttachmentKey": None,
                "sourceCursorStart": None,
                "sourceCursorEnd": None,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:32]
    existing_note = zotero_item(
        "NOTE0001",
        {
            "itemType": "note",
            "parentItem": "ITEM0001",
            "note": "<p>old</p><hr><p>Second pass</p>",
            "dateModified": "2025-01-01T00:00:00Z",
            "tags": [
                {"tag": "zbridge"},
                {"tag": "zbridge:agent:codex"},
                {"tag": "zbridge:type:summary"},
                {"tag": "zbridge:slot:default"},
                {"tag": f"zbridge:req:{request_token}"},
                {"tag": f"zbridge:reqsig:{request_token}:{request_sig}"},
                {"tag": f"zbridge:reqstatus:{request_token}:updated"},
            ],
        },
        version=5,
    )
    state: dict[str, Any] = {"patched": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            return httpx.Response(200, json=[existing_note])
        if request.method == "PATCH" and request.url.path == "/users/123456/items/NOTE0001":
            state["patched"] = True
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.post(
        "/v1/items/ITEM0001/notes/upsert-ai-note",
        headers=auth_headers,
        json={
            "agent": "codex",
            "noteType": "summary",
            "bodyMarkdown": "Second pass",
            "mode": "append",
            "requestId": request_id,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "updated",
        "noteKey": "NOTE0001",
        "itemKey": "ITEM0001",
        "agent": "codex",
        "noteType": "summary",
        "slot": "default",
    }
    assert state["patched"] is False


async def test_upsert_ai_note_recomputes_append_after_write_conflict(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    parent = zotero_item(
        "ITEM0001",
        {"itemType": "journalArticle", "title": "Paper", "creators": []},
    )
    stale_note = zotero_item(
        "NOTE0001",
        {
            "itemType": "note",
            "parentItem": "ITEM0001",
            "note": "<p>Original summary</p>",
            "dateModified": "2025-01-01T00:00:00Z",
            "tags": [
                {"tag": "zbridge"},
                {"tag": "zbridge:agent:codex"},
                {"tag": "zbridge:type:summary"},
                {"tag": "zbridge:slot:default"},
            ],
        },
        version=5,
    )
    latest_note = zotero_item(
        "NOTE0001",
        {
            "itemType": "note",
            "parentItem": "ITEM0001",
            "note": "<p>Original summary</p><hr><p>Concurrent insight</p>",
            "dateModified": "2025-01-02T00:00:00Z",
            "tags": [
                {"tag": "zbridge"},
                {"tag": "zbridge:agent:codex"},
                {"tag": "zbridge:type:summary"},
                {"tag": "zbridge:slot:default"},
            ],
        },
        version=6,
    )
    state: dict[str, Any] = {"child_calls": 0, "patch_requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            state["child_calls"] += 1
            payload = [stale_note] if state["child_calls"] == 1 else [latest_note]
            return httpx.Response(200, json=payload)
        if request.method == "PATCH" and request.url.path == "/users/123456/items/NOTE0001":
            state["patch_requests"].append(request)
            if len(state["patch_requests"]) == 1:
                return httpx.Response(412)
            assert request.headers["If-Unmodified-Since-Version"] == "6"
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.post(
        "/v1/items/ITEM0001/notes/upsert-ai-note",
        headers=auth_headers,
        json={
            "agent": "codex",
            "noteType": "summary",
            "bodyMarkdown": "Second pass",
            "mode": "append",
            "requestId": "ai-note-conflict-recompute-1",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "updated"
    assert len(state["patch_requests"]) == 2
    patched_payload = json.loads(state["patch_requests"][-1].content.decode("utf-8"))
    assert "Concurrent insight" in patched_payload["note"]
    assert "Second pass" in patched_payload["note"]


async def test_upsert_ai_note_reused_request_id_with_different_payload_conflicts(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    parent = zotero_item(
        "ITEM0001",
        {"itemType": "journalArticle", "title": "Paper", "creators": []},
    )
    request_id = "ai-note-conflict-1"
    request_token = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
    request_sig = hashlib.sha256(
        json.dumps(
            {
                "agent": "codex",
                "noteType": "summary",
                "slot": "default",
                "mode": "replace",
                "title": None,
                "bodyMarkdown": "Original summary",
                "tags": [],
                "model": None,
                "sourceAttachmentKey": None,
                "sourceCursorStart": None,
                "sourceCursorEnd": None,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:32]
    existing_note = zotero_item(
        "NOTE0001",
        {
            "itemType": "note",
            "parentItem": "ITEM0001",
            "note": "<p>Original summary</p>",
            "dateModified": "2025-01-01T00:00:00Z",
            "tags": [
                {"tag": "zbridge"},
                {"tag": "zbridge:agent:codex"},
                {"tag": "zbridge:type:summary"},
                {"tag": "zbridge:slot:default"},
                {"tag": f"zbridge:req:{request_token}"},
                {"tag": f"zbridge:reqsig:{request_token}:{request_sig}"},
                {"tag": f"zbridge:reqstatus:{request_token}:created"},
            ],
        },
        version=5,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            return httpx.Response(200, json=[existing_note])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.post(
        "/v1/items/ITEM0001/notes/upsert-ai-note",
        headers=auth_headers,
        json={
            "agent": "codex",
            "noteType": "summary",
            "bodyMarkdown": "Changed summary",
            "requestId": request_id,
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REQUEST_ID_REUSED"


async def test_generic_note_list_get_create_and_update(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    parent = zotero_item(
        "ITEM0001",
        {"itemType": "journalArticle", "title": "Paper", "creators": []},
    )
    existing_note = zotero_item(
        "NOTE0001",
        {
            "itemType": "note",
            "parentItem": "ITEM0001",
            "note": "<p>Paragraph <strong>one</strong></p><p>Second line</p>",
            "dateAdded": "2025-01-01T00:00:00Z",
            "dateModified": "2025-01-02T00:00:00Z",
            "tags": [{"tag": "manual"}],
        },
        version=5,
    )
    state: dict[str, Any] = {"create_requests": [], "patch_requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            return httpx.Response(200, json=[existing_note])
        if request.method == "GET" and request.url.path == "/users/123456/items/NOTE0001":
            return httpx.Response(200, json=existing_note)
        if request.method == "POST" and request.url.path == "/users/123456/items":
            state["create_requests"].append(request)
            return httpx.Response(
                200,
                json={"successful": {"0": {"key": "NOTE0002", "version": 1}}},
            )
        if request.method == "PATCH" and request.url.path == "/users/123456/items/NOTE0001":
            state["patch_requests"].append(request)
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    list_response = await async_client.get("/v1/items/ITEM0001/notes", headers=auth_headers)
    detail_response = await async_client.get("/v1/notes/NOTE0001", headers=auth_headers)
    create_response = await async_client.post(
        "/v1/items/ITEM0001/notes",
        headers=auth_headers,
        json={
            "title": "Research Log",
            "bodyMarkdown": "Created from bridge",
            "tags": ["manual", "reading"],
            "requestId": "generic-note-create-1",
        },
    )
    update_response = await async_client.patch(
        "/v1/notes/NOTE0001",
        headers=auth_headers,
        json={
            "bodyMarkdown": "Appended section",
            "mode": "append",
            "tags": ["manual", "updated"],
            "requestId": "generic-note-update-1",
        },
    )

    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert list_payload["count"] == 1
    assert list_payload["notes"][0]["noteKey"] == "NOTE0001"
    assert list_payload["notes"][0]["bodyText"] == "Paragraph one\n\nSecond line"
    assert list_payload["notes"][0]["isAiNote"] is False

    assert detail_response.status_code == 200
    assert detail_response.json()["note"]["bodyHtml"].startswith("<p>Paragraph")

    assert create_response.status_code == 200
    assert create_response.json()["status"] == "created"
    created_payload = json.loads(state["create_requests"][0].content.decode("utf-8"))[0]
    assert created_payload["itemType"] == "note"
    assert created_payload["parentItem"] == "ITEM0001"
    created_tags = [tag["tag"] for tag in created_payload["tags"]]
    assert "manual" in created_tags
    assert "reading" in created_tags
    assert any(tag.startswith("zbridge:req:") for tag in created_tags)
    assert any(tag.startswith("zbridge:reqsig:") for tag in created_tags)
    assert "<h2>Research Log</h2>" in created_payload["note"]

    assert update_response.status_code == 200
    assert update_response.json()["status"] == "updated"
    patched_payload = json.loads(state["patch_requests"][0].content.decode("utf-8"))
    patched_tags = [tag["tag"] for tag in patched_payload["tags"]]
    assert "manual" in patched_tags
    assert "updated" in patched_tags
    assert any(tag.startswith("zbridge:req:") for tag in patched_tags)
    assert any(tag.startswith("zbridge:reqsig:") for tag in patched_tags)
    assert "Paragraph <strong>one</strong>" in patched_payload["note"]
    assert "Appended section" in patched_payload["note"]


async def test_generic_note_create_replays_request_id_without_duplicate_write(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    parent = zotero_item(
        "ITEM0001",
        {"itemType": "journalArticle", "title": "Paper", "creators": []},
    )
    request_id = "generic-note-create-replay-1"
    request_token = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
    request_sig = hashlib.sha256(
        json.dumps(
            {
                "operation": "create",
                "title": "Research Log",
                "bodyMarkdown": "Created from bridge",
                "mode": "replace",
                "tags": ["manual"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:32]
    replayed_note = zotero_item(
        "NOTE0002",
        {
            "itemType": "note",
            "parentItem": "ITEM0001",
            "note": "<h2>Research Log</h2>\n<p>Created from bridge</p>\n",
            "tags": [
                {"tag": f"zbridge:req:{request_token}"},
                {"tag": f"zbridge:reqsig:{request_token}:{request_sig}"},
                {"tag": "manual"},
            ],
        },
        version=1,
    )
    state: dict[str, Any] = {"create_requests": [], "child_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            state["child_calls"] += 1
            payload = [] if state["child_calls"] == 1 else [replayed_note]
            return httpx.Response(200, json=payload)
        if request.method == "POST" and request.url.path == "/users/123456/items":
            state["create_requests"].append(request)
            return httpx.Response(
                200,
                json={"successful": {"0": {"key": "NOTE0002", "version": 1}}},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    first_response = await async_client.post(
        "/v1/items/ITEM0001/notes",
        headers=auth_headers,
        json={
            "title": "Research Log",
            "bodyMarkdown": "Created from bridge",
            "tags": ["manual"],
            "requestId": request_id,
        },
    )
    second_response = await async_client.post(
        "/v1/items/ITEM0001/notes",
        headers=auth_headers,
        json={
            "title": "Research Log",
            "bodyMarkdown": "Created from bridge",
            "tags": ["manual"],
            "requestId": request_id,
        },
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json() == {
        "status": "created",
        "noteKey": "NOTE0002",
        "itemKey": "ITEM0001",
    }
    assert second_response.json() == {
        "status": "created",
        "noteKey": "NOTE0002",
        "itemKey": "ITEM0001",
    }
    assert len(state["create_requests"]) == 1


async def test_generic_note_update_recomputes_append_after_write_conflict(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    stale_note = zotero_item(
        "NOTE0001",
        {
            "itemType": "note",
            "parentItem": "ITEM0001",
            "note": "<p>Original</p>",
            "tags": [{"tag": "manual"}],
        },
        version=5,
    )
    latest_note = zotero_item(
        "NOTE0001",
        {
            "itemType": "note",
            "parentItem": "ITEM0001",
            "note": "<p>Original</p><p>Concurrent edit</p>",
            "tags": [{"tag": "manual"}, {"tag": "reviewed"}],
        },
        version=6,
    )
    state: dict[str, Any] = {"note_calls": 0, "patch_requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/NOTE0001":
            state["note_calls"] += 1
            payload = stale_note if state["note_calls"] == 1 else latest_note
            return httpx.Response(200, json=payload)
        if request.method == "PATCH" and request.url.path == "/users/123456/items/NOTE0001":
            state["patch_requests"].append(request)
            if len(state["patch_requests"]) == 1:
                return httpx.Response(412)
            assert request.headers["If-Unmodified-Since-Version"] == "6"
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.patch(
        "/v1/notes/NOTE0001",
        headers=auth_headers,
        json={
            "bodyMarkdown": "Appended section",
            "mode": "append",
            "requestId": "generic-note-conflict-recompute-1",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "updated"
    assert len(state["patch_requests"]) == 2
    patched_payload = json.loads(state["patch_requests"][-1].content.decode("utf-8"))
    patched_tags = [tag["tag"] for tag in patched_payload["tags"]]
    assert "Concurrent edit" in patched_payload["note"]
    assert "Appended section" in patched_payload["note"]
    assert "reviewed" in patched_tags


async def test_generic_note_create_recovers_existing_note_after_write_conflict(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    parent = zotero_item(
        "ITEM0001",
        {"itemType": "journalArticle", "title": "Paper", "creators": []},
    )
    request_id = "generic-note-conflict-1"
    request_token = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
    request_sig = hashlib.sha256(
        json.dumps(
            {
                "operation": "create",
                "title": None,
                "bodyMarkdown": "Recovered from conflict",
                "mode": "replace",
                "tags": ["manual"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:32]
    replayed_note = zotero_item(
        "NOTE0003",
        {
            "itemType": "note",
            "parentItem": "ITEM0001",
            "note": "<p>Recovered from conflict</p>",
            "tags": [
                {"tag": f"zbridge:req:{request_token}"},
                {"tag": f"zbridge:reqsig:{request_token}:{request_sig}"},
                {"tag": "manual"},
            ],
        },
        version=1,
    )
    state: dict[str, Any] = {"child_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            state["child_calls"] += 1
            payload = [] if state["child_calls"] == 1 else [replayed_note]
            return httpx.Response(200, json=payload)
        if request.method == "POST" and request.url.path == "/users/123456/items":
            return httpx.Response(412)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.post(
        "/v1/items/ITEM0001/notes",
        headers=auth_headers,
        json={
            "bodyMarkdown": "Recovered from conflict",
            "tags": ["manual"],
            "requestId": request_id,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "created",
        "noteKey": "NOTE0003",
        "itemKey": "ITEM0001",
    }


async def test_generic_note_create_reused_request_id_with_different_payload_conflicts(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    parent = zotero_item(
        "ITEM0001",
        {"itemType": "journalArticle", "title": "Paper", "creators": []},
    )
    request_id = "generic-note-create-conflict-2"
    request_token = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
    original_payload = {
        "operation": "create",
        "title": "Research Log",
        "bodyMarkdown": "Original content",
        "mode": "replace",
        "tags": ["manual"],
    }
    original_sig = hashlib.sha256(
        json.dumps(
            original_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()[:32]
    existing_note = zotero_item(
        "NOTE0004",
        {
            "itemType": "note",
            "parentItem": "ITEM0001",
            "note": "<h2>Research Log</h2>\n<p>Original content</p>",
            "tags": [
                {"tag": f"zbridge:req:{request_token}"},
                {"tag": f"zbridge:reqsig:{request_token}:{original_sig}"},
                {"tag": "manual"},
            ],
        },
        version=1,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            return httpx.Response(200, json=[existing_note])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.post(
        "/v1/items/ITEM0001/notes",
        headers=auth_headers,
        json={
            "title": "Research Log",
            "bodyMarkdown": "Changed content",
            "tags": ["manual"],
            "requestId": request_id,
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REQUEST_ID_REUSED"


async def test_generic_note_update_preserves_ai_identity_tags(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    ai_note = zotero_item(
        "NOTE0001",
        {
            "itemType": "note",
            "parentItem": "ITEM0001",
            "note": "<p>Old AI note</p>",
            "tags": [
                {"tag": "zbridge"},
                {"tag": "zbridge:agent:codex"},
                {"tag": "zbridge:type:summary"},
                {"tag": "zbridge:slot:default"},
                {"tag": "manual"},
            ],
        },
        version=5,
    )
    state: dict[str, Any] = {"patch_requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/NOTE0001":
            return httpx.Response(200, json=ai_note)
        if request.method == "PATCH" and request.url.path == "/users/123456/items/NOTE0001":
            state["patch_requests"].append(request)
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.patch(
        "/v1/notes/NOTE0001",
        headers=auth_headers,
        json={
            "bodyMarkdown": "Patched AI note",
            "tags": ["manual", "updated"],
        },
    )

    assert response.status_code == 200
    patched_payload = json.loads(state["patch_requests"][0].content.decode("utf-8"))
    patched_tags = {tag["tag"] for tag in patched_payload["tags"]}
    assert patched_tags == {
        "zbridge",
        "zbridge:agent:codex",
        "zbridge:type:summary",
        "zbridge:slot:default",
        "manual",
        "updated",
    }


async def test_generic_note_update_replays_request_id_without_duplicate_append(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    request_id = "generic-note-update-replay-1"
    request_token = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
    replay_payload = {
        "operation": "update",
        "title": None,
        "bodyMarkdown": "Appended section",
        "mode": "append",
        "tags": ["manual", "updated"],
    }
    replay_sig = hashlib.sha256(
        json.dumps(
            replay_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()[:32]
    existing_note = zotero_item(
        "NOTE0005",
        {
            "itemType": "note",
            "parentItem": "ITEM0001",
            "note": "<p>Original section</p><hr><p>Appended section</p>",
            "tags": [
                {"tag": f"zbridge:req:{request_token}"},
                {"tag": f"zbridge:reqsig:{request_token}:{replay_sig}"},
                {"tag": "manual"},
                {"tag": "updated"},
            ],
        },
        version=7,
    )
    state: dict[str, Any] = {"patch_requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/NOTE0005":
            return httpx.Response(200, json=existing_note)
        if request.method == "PATCH" and request.url.path == "/users/123456/items/NOTE0005":
            state["patch_requests"].append(request)
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.patch(
        "/v1/notes/NOTE0005",
        headers=auth_headers,
        json={
            "bodyMarkdown": "Appended section",
            "mode": "append",
            "tags": ["manual", "updated"],
            "requestId": request_id,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "updated",
        "noteKey": "NOTE0005",
        "itemKey": "ITEM0001",
    }
    assert state["patch_requests"] == []


async def test_generic_note_update_reused_request_id_with_different_payload_conflicts(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    request_id = "generic-note-update-conflict-2"
    request_token = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
    prior_payload = {
        "operation": "update",
        "title": None,
        "bodyMarkdown": "Older append",
        "mode": "append",
        "tags": ["manual"],
    }
    prior_sig = hashlib.sha256(
        json.dumps(prior_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    ).hexdigest()[:32]
    existing_note = zotero_item(
        "NOTE0006",
        {
            "itemType": "note",
            "parentItem": "ITEM0001",
            "note": "<p>Original section</p><hr><p>Older append</p>",
            "tags": [
                {"tag": f"zbridge:req:{request_token}"},
                {"tag": f"zbridge:reqsig:{request_token}:{prior_sig}"},
                {"tag": "manual"},
            ],
        },
        version=8,
    )
    state: dict[str, Any] = {"patch_requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/NOTE0006":
            return httpx.Response(200, json=existing_note)
        if request.method == "PATCH" and request.url.path == "/users/123456/items/NOTE0006":
            state["patch_requests"].append(request)
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.patch(
        "/v1/notes/NOTE0006",
        headers=auth_headers,
        json={
            "bodyMarkdown": "New append",
            "mode": "append",
            "tags": ["manual"],
            "requestId": request_id,
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REQUEST_ID_REUSED"
    assert state["patch_requests"] == []


async def test_generic_note_delete(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    existing_note = zotero_item(
        "NOTE0001",
        {
            "itemType": "note",
            "parentItem": "ITEM0001",
            "note": "<p>Delete me</p>",
            "tags": [{"tag": "manual"}],
        },
        version=7,
    )
    state: dict[str, Any] = {"deleted": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/NOTE0001":
            return httpx.Response(200, json=existing_note)
        if request.method == "DELETE" and request.url.path == "/users/123456/items/NOTE0001":
            state["deleted"] = True
            assert request.headers["If-Unmodified-Since-Version"] == "7"
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.delete("/v1/notes/NOTE0001", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {
        "status": "deleted",
        "noteKey": "NOTE0001",
        "itemKey": "ITEM0001",
    }
    assert state["deleted"] is True


async def test_upload_pdf_action_and_multipart(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    parent = zotero_item(
        "ITEM0001",
        {"itemType": "journalArticle", "title": "Paper", "creators": []},
    )
    attachment = zotero_item(
        "ATTACH01",
        {
            "itemType": "attachment",
            "title": "paper.pdf",
            "filename": "paper.pdf",
            "contentType": "application/pdf",
            "linkMode": "imported_file",
        },
    )
    template = {
        "itemType": "attachment",
        "linkMode": "imported_file",
        "title": "",
        "filename": "",
        "contentType": "",
        "tags": [],
        "collections": [],
    }

    state: dict[str, Any] = {
        "create_requests": [],
        "upload_calls": 0,
        "auth_calls": {"ATTACH01": 0, "ATTACH02": 0},
        "created_keys": ["ATTACH01", "ATTACH02"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "files.example.com":
            return httpx.Response(
                200,
                content=PDF_BYTES,
                headers={"Content-Type": "application/pdf"},
            )
        if request.url.host == "upload.example.com":
            state["upload_calls"] += 1
            return httpx.Response(204)
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path == "/items/new":
            return httpx.Response(200, json=template)
        if request.method == "POST" and request.url.path == "/users/123456/items":
            key = state["created_keys"].pop(0)
            state["create_requests"].append(request)
            return httpx.Response(
                200,
                json={"successful": {"0": {"key": key, "version": 1}}},
            )
        if request.method == "POST" and request.url.path in {
            "/users/123456/items/ATTACH01/file",
            "/users/123456/items/ATTACH02/file",
        }:
            attachment_key = request.url.path.split("/")[-2]
            state["auth_calls"][attachment_key] += 1
            if state["auth_calls"][attachment_key] == 1:
                assert request.headers["If-None-Match"] == "*"
                return httpx.Response(
                    200,
                    json={
                        "url": "https://upload.example.com/file",
                        "uploadKey": f"UP-{attachment_key}",
                        "prefix": "",
                        "suffix": "",
                        "contentType": "application/octet-stream",
                    },
                )
            assert request.headers["If-None-Match"] == "*"
            return httpx.Response(204)
        if request.method == "GET" and request.url.path == "/users/123456/items/ATTACH01":
            return httpx.Response(200, json=attachment)
        if request.method == "GET" and request.url.path == "/users/123456/items/ATTACH02":
            return httpx.Response(
                200,
                json={
                    **attachment,
                    "key": "ATTACH02",
                    "data": {**attachment["data"], "filename": "local.pdf"},
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    action_response = await async_client.post(
        "/v1/papers/upload-pdf-action",
        headers=auth_headers,
        json={
            "itemKey": "ITEM0001",
            "fileUrl": "https://files.example.com/paper.pdf",
            "requestId": "upload-action-1",
        },
    )
    multipart_response = await async_client.post(
        "/v1/papers/upload-pdf-multipart",
        headers=auth_headers,
        data={"itemKey": "ITEM0001", "requestId": "upload-multipart-1"},
        files={"file": ("local.pdf", PDF_BYTES, "application/pdf")},
    )

    assert action_response.status_code == 200
    assert action_response.json()["attachmentKey"] == "ATTACH01"
    assert multipart_response.status_code == 200
    assert multipart_response.json()["attachmentKey"] == "ATTACH02"
    created_payload = json.loads(state["create_requests"][0].content.decode("utf-8"))[0]
    assert created_payload["linkMode"] == "imported_file"
    assert state["upload_calls"] == 2


async def test_upload_pdf_action_with_doi_uses_distinct_write_tokens(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    parent = zotero_item(
        "ITEM0001",
        {
            "itemType": "journalArticle",
            "title": "Bridge Paper",
            "DOI": "10.1038/nrd842",
            "creators": [{"name": "Ada Lovelace", "creatorType": "author"}],
            "tags": [{"tag": "inbox"}],
            "collections": [],
        },
    )
    attachment = zotero_item(
        "ATTACH01",
        {
            "itemType": "attachment",
            "title": "paper.pdf",
            "filename": "paper.pdf",
            "contentType": "application/pdf",
            "linkMode": "imported_file",
        },
    )
    parent_template = {
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
    attachment_template = {
        "itemType": "attachment",
        "linkMode": "imported_file",
        "title": "",
        "filename": "",
        "contentType": "",
        "tags": [],
        "collections": [],
    }
    state: dict[str, Any] = {
        "create_tokens": [],
        "parent_created": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "files.example.com":
            return httpx.Response(
                200,
                content=PDF_BYTES,
                headers={"Content-Type": "application/pdf"},
            )
        if request.url.host == "doi.org":
            return httpx.Response(
                200,
                json={
                    "type": "article-journal",
                    "title": "Bridge Paper",
                    "author": [{"given": "Ada", "family": "Lovelace"}],
                    "issued": {"date-parts": [[2025, 1, 1]]},
                },
            )
        if request.url.host == "upload.example.com":
            return httpx.Response(204)
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items":
            payload = [parent] if state["parent_created"] else []
            return httpx.Response(200, json=payload)
        if request.method == "GET" and request.url.path == "/items/new":
            item_type = request.url.params.get("itemType")
            if item_type == "journalArticle":
                return httpx.Response(200, json=parent_template)
            if item_type == "attachment":
                return httpx.Response(200, json=attachment_template)
        if request.method == "POST" and request.url.path == "/users/123456/items":
            token = request.headers["Zotero-Write-Token"]
            state["create_tokens"].append(token)
            if len(state["create_tokens"]) == 2 and token == state["create_tokens"][0]:
                return httpx.Response(412)

            posted = json.loads(request.content.decode("utf-8"))[0]
            if posted["itemType"] == "journalArticle":
                state["parent_created"] = True
                return httpx.Response(
                    200,
                    json={"successful": {"0": {"key": "ITEM0001", "version": 1}}},
                )
            if posted["itemType"] == "attachment":
                return httpx.Response(
                    200,
                    json={"successful": {"0": {"key": "ATTACH01", "version": 1}}},
                )
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path == "/users/123456/items/ATTACH01/file":
            if request.content.decode("utf-8") == "upload=UP-ATTACH01":
                return httpx.Response(204)
            return httpx.Response(
                200,
                json={
                    "url": "https://upload.example.com/file",
                    "uploadKey": "UP-ATTACH01",
                    "prefix": "",
                    "suffix": "",
                    "contentType": "application/octet-stream",
                },
            )
        if request.method == "GET" and request.url.path == "/users/123456/items/ATTACH01":
            return httpx.Response(200, json=attachment)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.post(
        "/v1/papers/upload-pdf-action",
        headers=auth_headers,
        json={
            "doi": "10.1038/nrd842",
            "fileUrl": "https://files.example.com/paper.pdf",
            "requestId": "upload-by-doi-1",
        },
    )

    assert response.status_code == 200
    assert response.json()["attachmentKey"] == "ATTACH01"
    assert len(state["create_tokens"]) == 2
    assert state["create_tokens"][0] != state["create_tokens"][1]


async def test_upload_pdf_action_populates_local_fulltext_cache(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: respx.MockRouter,
) -> None:
    from app.main import app

    parent = zotero_item(
        "ITEM0001",
        {"itemType": "journalArticle", "title": "Paper", "creators": []},
    )
    attachment = zotero_item(
        "ATTACH01",
        {
            "itemType": "attachment",
            "title": "paper.pdf",
            "filename": "paper.pdf",
            "contentType": "application/pdf",
            "linkMode": "imported_file",
            "md5": "abc123",
        },
    )
    template = {
        "itemType": "attachment",
        "linkMode": "imported_file",
        "title": "",
        "filename": "",
        "contentType": "",
        "tags": [],
        "collections": [],
    }

    store = app.state.bridge_service._local_fulltext_store
    assert store is not None

    def fake_cache_pdf(
        *,
        attachment_key: str,
        item_key: str | None,
        filename: str | None,
        content: bytes,
    ) -> bool:
        del content
        store.write_payload(
            attachment_key=attachment_key,
            item_key=item_key,
            filename=filename,
            fulltext_payload={
                "content": "Cached from upload flow.",
                "indexedPages": 1,
                "totalPages": 1,
            },
        )
        return True

    monkeypatch.setattr(store, "cache_pdf", fake_cache_pdf)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "files.example.com":
            return httpx.Response(
                200,
                content=PDF_BYTES,
                headers={"Content-Type": "application/pdf"},
            )
        if request.url.host == "upload.example.com":
            return httpx.Response(204)
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            return httpx.Response(200, json=[attachment])
        if request.method == "GET" and request.url.path == "/items/new":
            return httpx.Response(200, json=template)
        if request.method == "POST" and request.url.path == "/users/123456/items":
            return httpx.Response(
                200,
                json={"successful": {"0": {"key": "ATTACH01", "version": 1}}},
            )
        if request.method == "POST" and request.url.path == "/users/123456/items/ATTACH01/file":
            if request.content.decode("utf-8") == "upload=UP-ATTACH01":
                assert request.headers["If-None-Match"] == "*"
                return httpx.Response(204)
            assert request.headers["If-None-Match"] == "*"
            return httpx.Response(
                200,
                json={
                    "url": "https://upload.example.com/file",
                    "uploadKey": "UP-ATTACH01",
                    "prefix": "",
                    "suffix": "",
                    "contentType": "application/octet-stream",
                },
            )
        if request.method == "GET" and request.url.path == "/users/123456/items/ATTACH01":
            return httpx.Response(200, json=attachment)
        if request.method == "GET" and request.url.path == "/users/123456/items/ATTACH01/fulltext":
            return httpx.Response(404)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    upload_response = await async_client.post(
        "/v1/papers/upload-pdf-action",
        headers=auth_headers,
        json={
            "itemKey": "ITEM0001",
            "fileUrl": "https://files.example.com/paper.pdf",
            "requestId": "upload-local-cache-1",
        },
    )
    fulltext_response = await async_client.get(
        "/v1/items/ITEM0001/fulltext",
        headers=auth_headers,
        params={"maxChars": 1000},
    )

    assert upload_response.status_code == 200
    assert fulltext_response.status_code == 200
    assert fulltext_response.json()["source"] == "local_cache"
    assert fulltext_response.json()["content"] == "Cached from upload flow."


@pytest.mark.asyncio
async def test_upload_pdf_action_returns_download_failed_on_transport_error(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "files.example.com":
            raise httpx.ConnectError("dns failure", request=request)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.post(
        "/v1/papers/upload-pdf-action",
        headers=auth_headers,
        json={
            "fileUrl": "https://files.example.com/paper.pdf",
            "createTopLevelAttachmentIfNeeded": True,
        },
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "DOWNLOAD_FAILED"
    assert response.json()["error"]["message"] == "Unable to download remote PDF"


@pytest.mark.asyncio
async def test_upload_pdf_action_rejects_loopback_file_url(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await async_client.post(
        "/v1/papers/upload-pdf-action",
        headers=auth_headers,
        json={
            "fileUrl": "http://127.0.0.1:9/paper.pdf",
            "createTopLevelAttachmentIfNeeded": True,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"
    assert response.json()["error"]["message"] == "Remote file URL must resolve to a public host"


@pytest.mark.asyncio
async def test_upload_pdf_action_rejects_redirect_to_loopback(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    state = {"download_requests": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "files.example.com":
            state["download_requests"] += 1
            return httpx.Response(
                302,
                headers={"Location": "http://127.0.0.1:9/secret.pdf"},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.post(
        "/v1/papers/upload-pdf-action",
        headers=auth_headers,
        json={
            "fileUrl": "https://files.example.com/paper.pdf",
            "createTopLevelAttachmentIfNeeded": True,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"
    assert response.json()["error"]["message"] == "Remote file URL must resolve to a public host"
    assert state["download_requests"] == 1


@pytest.mark.asyncio
async def test_upload_pdf_action_rejects_multiple_files(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await async_client.post(
        "/v1/papers/upload-pdf-action",
        headers=auth_headers,
        json={
            "itemKey": "ITEM0001",
            "openaiFileIdRefs": [
                {
                    "name": "one.pdf",
                    "id": "1",
                    "mime_type": "application/pdf",
                    "download_link": "https://files.example.com/one.pdf",
                },
                {
                    "name": "two.pdf",
                    "id": "2",
                    "mime_type": "application/pdf",
                    "download_link": "https://files.example.com/two.pdf",
                },
            ],
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


async def test_upload_pdf_multipart_returns_upload_failed_on_transport_error(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    parent = zotero_item(
        "ITEM0001",
        {"itemType": "journalArticle", "title": "Paper", "creators": []},
    )
    template = {
        "itemType": "attachment",
        "linkMode": "imported_file",
        "title": "",
        "filename": "",
        "contentType": "",
        "tags": [],
        "collections": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "upload.example.com":
            raise httpx.ConnectError("tls failure", request=request)
        if request.url.host != "api.zotero.org":
            raise AssertionError(f"unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path == "/items/new":
            return httpx.Response(200, json=template)
        if request.method == "POST" and request.url.path == "/users/123456/items":
            return httpx.Response(
                200,
                json={"successful": {"0": {"key": "ATTACH01", "version": 1}}},
            )
        if request.method == "POST" and request.url.path == "/users/123456/items/ATTACH01/file":
            return httpx.Response(
                200,
                json={
                    "url": "https://upload.example.com/file",
                    "uploadKey": "UP-ATTACH01",
                    "prefix": "",
                    "suffix": "",
                    "contentType": "application/octet-stream",
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.post(
        "/v1/papers/upload-pdf-multipart",
        headers=auth_headers,
        data={"itemKey": "ITEM0001", "requestId": "upload-multipart-transport-1"},
        files={"file": ("paper.pdf", PDF_BYTES, "application/pdf")},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "UPLOAD_FAILED"
    assert response.json()["error"]["message"] == "Authorized file upload failed"


async def test_upload_pdf_multipart_rejects_non_pdf_bytes(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await async_client.post(
        "/v1/papers/upload-pdf-multipart",
        headers=auth_headers,
        data={"itemKey": "ITEM0001", "requestId": "upload-html-1"},
        files={"file": ("paper.pdf", b"<html>login</html>", "application/pdf")},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"
    assert response.json()["error"]["message"] == "Only PDF uploads are supported"
