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
    assert response.json() == {"items": [], "count": 0}
    assert store.search_item_keys("stale semantic cache token", limit=5) == []


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
