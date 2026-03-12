from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from app.services.note_renderer import STRUCTURED_BLOCK_MARKER, NoteRenderer

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF"


def zotero_item(key: str, data: dict[str, Any], version: int = 1) -> dict[str, Any]:
    return {"key": key, "version": version, "data": data}


def parent_item(
    *,
    key: str = "ITEM0001",
    title: str = "Bridge Paper",
    doi: str | None = "10.1000/bridge",
    date: str = "2024",
    creators: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return zotero_item(
        key,
        {
            "itemType": "journalArticle",
            "title": title,
            "DOI": doi,
            "date": date,
            "creators": creators or [{"name": "Ada Lovelace", "creatorType": "author"}],
            "collections": ["COLL1"],
            "tags": [{"tag": "llm"}],
        },
    )


def attachment_item(
    *,
    key: str = "ATTACH01",
    parent: str = "ITEM0001",
    filename: str = "paper.pdf",
) -> dict[str, Any]:
    return zotero_item(
        key,
        {
            "itemType": "attachment",
            "parentItem": parent,
            "title": filename,
            "filename": filename,
            "contentType": "application/pdf",
            "linkMode": "imported_file",
        },
    )


def note_item(
    *,
    key: str = "NOTE0001",
    parent: str = "ITEM0001",
    note_html: str,
    tags: list[str],
) -> dict[str, Any]:
    return zotero_item(
        key,
        {
            "itemType": "note",
            "parentItem": parent,
            "note": note_html,
            "tags": [{"tag": tag} for tag in tags],
            "dateModified": "2026-03-11T10:00:00Z",
        },
    )


@pytest.mark.asyncio
async def test_healthz(async_client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await async_client.get("/healthz", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["version"] == "2.0.0"


@pytest.mark.asyncio
async def test_healthz_rejects_missing_zotero_api_key(
    async_client: httpx.AsyncClient,
) -> None:
    response = await async_client.get("/healthz")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "MISSING_ZOTERO_API_KEY"


@pytest.mark.asyncio
async def test_healthz_supports_request_scoped_zotero_key(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/keys/scoped-key":
            return httpx.Response(200, json={"userID": 999999})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/healthz",
        headers={**auth_headers, "X-Zotero-API-Key": "scoped-key"},
    )

    assert response.status_code == 200
    assert response.json()["config"]["libraryType"] == "user"
    assert response.json()["config"]["libraryId"] == "999999"


@pytest.mark.asyncio
async def test_removed_fulltext_endpoints_return_404(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    fulltext_response = await async_client.get(
        "/v1/items/ITEM0001/fulltext",
        headers=auth_headers,
    )
    preview_response = await async_client.post(
        "/v1/items/fulltext/batch-preview",
        headers=auth_headers,
        json={"itemKeys": ["ITEM0001"]},
    )

    assert fulltext_response.status_code == 404
    assert preview_response.status_code == 404


@pytest.mark.asyncio
async def test_search_items_applies_explicit_title_sort(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    zebra = parent_item(key="ITEM0002", title="Zebra Paper")
    alpha = parent_item(key="ITEM0001", title="Alpha Paper")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/users/123456/items":
            item_key = request.url.params.get("itemKey")
            if item_key == "ITEM0001,ITEM0002":
                return httpx.Response(200, json=[zebra, alpha])
            assert request.url.params["q"] == "paper"
            return httpx.Response(200, json=[zebra, alpha])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/search",
        headers=auth_headers,
        params={
            "q": "paper",
            "sort": "title",
            "direction": "asc",
            "includeAttachments": "false",
            "includeNotes": "false",
        },
    )

    assert response.status_code == 200
    assert [item["title"] for item in response.json()["items"]] == [
        "Alpha Paper",
        "Zebra Paper",
    ]


@pytest.mark.asyncio
async def test_search_items_sorts_by_parent_title_for_child_hits(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    zebra_parent = parent_item(key="ITEM0002", title="Zebra Paper")
    alpha_parent = parent_item(key="ITEM0001", title="Alpha Paper")
    zebra_attachment = attachment_item(key="ATTACH02", parent="ITEM0002", filename="zebra.pdf")
    alpha_attachment = attachment_item(key="ATTACH01", parent="ITEM0001", filename="alpha.pdf")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/users/123456/items":
            item_key = request.url.params.get("itemKey")
            if item_key == "ITEM0001,ITEM0002":
                return httpx.Response(200, json=[zebra_parent, alpha_parent])
            if request.url.params.get("q") == "paper":
                return httpx.Response(200, json=[zebra_attachment, alpha_attachment])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/search",
        headers=auth_headers,
        params={
            "q": "paper",
            "sort": "title",
            "direction": "asc",
            "includeAttachments": "false",
            "includeNotes": "false",
        },
    )

    assert response.status_code == 200
    assert [item["title"] for item in response.json()["items"]] == [
        "Alpha Paper",
        "Zebra Paper",
    ]


@pytest.mark.asyncio
async def test_attachment_list_detail_handoff_and_download(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    parent = parent_item()
    attachment = attachment_item()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            return httpx.Response(200, json=[attachment])
        if request.method == "GET" and request.url.path == "/users/123456/items/ATTACH01":
            return httpx.Response(200, json=attachment)
        if request.method == "GET" and request.url.path == "/users/123456/items/ATTACH01/file":
            return httpx.Response(
                200,
                content=PDF_BYTES,
                headers={"Content-Type": "application/pdf"},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    list_response = await async_client.get(
        "/v1/items/ITEM0001/attachments",
        headers=auth_headers,
    )
    detail_response = await async_client.get(
        "/v1/attachments/ATTACH01",
        headers=auth_headers,
    )
    handoff_response = await async_client.post(
        "/v1/attachments/ATTACH01/handoff",
        headers=auth_headers,
        json={"mode": "proxy_download"},
    )

    assert list_response.status_code == 200
    assert list_response.json()["attachments"][0]["downloadable"] is True
    assert detail_response.status_code == 200
    assert detail_response.json()["attachment"]["filename"] == "paper.pdf"
    assert handoff_response.status_code == 200

    download_url = handoff_response.json()["downloadUrl"]
    download_response = await async_client.get(download_url.replace("http://testserver", ""))

    assert download_response.status_code == 200
    assert download_response.headers["content-type"].startswith("application/pdf")
    assert "filename*=UTF-8''paper.pdf" in download_response.headers["content-disposition"]
    assert download_response.content == PDF_BYTES


@pytest.mark.asyncio
async def test_attachment_handoff_uses_public_base_url_when_configured(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    from app.main import app

    original_public_base_url = app.state.bridge_service._settings.public_base_url
    app.state.bridge_service._settings.public_base_url = "https://public.example.com/bridge"
    attachment = attachment_item()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/users/123456/items/ATTACH01":
            return httpx.Response(200, json=attachment)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    try:
        response = await async_client.post(
            "/v1/attachments/ATTACH01/handoff",
            headers=auth_headers,
            json={"mode": "proxy_download"},
        )
    finally:
        app.state.bridge_service._settings.public_base_url = original_public_base_url

    assert response.status_code == 200
    assert response.json()["downloadUrl"].startswith(
        "https://public.example.com/bridge/v1/attachments/download/"
    )


@pytest.mark.asyncio
async def test_attachment_handoff_download_uses_request_scoped_zotero_key(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    attachment = attachment_item(key="ATTACH09", parent="ITEM9000", filename="scoped.pdf")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/keys/scoped-key":
            return httpx.Response(200, json={"userID": 999999})
        if request.method == "GET" and request.url.path == "/users/999999/items/ATTACH09":
            return httpx.Response(200, json=attachment)
        if request.method == "GET" and request.url.path == "/users/999999/items/ATTACH09/file":
            return httpx.Response(
                200,
                content=PDF_BYTES,
                headers={"Content-Type": "application/pdf"},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    handoff_response = await async_client.post(
        "/v1/attachments/ATTACH09/handoff",
        headers={**auth_headers, "X-Zotero-API-Key": "scoped-key"},
        json={"mode": "proxy_download"},
    )

    assert handoff_response.status_code == 200
    token = handoff_response.json()["downloadUrl"].rsplit("/", 1)[-1]

    download_response = await async_client.get(f"/v1/attachments/download/{token}")

    assert download_response.status_code == 200
    assert download_response.headers["content-type"] == "application/pdf"
    assert "filename*=UTF-8''scoped.pdf" in download_response.headers["content-disposition"]
    assert download_response.content == PDF_BYTES


@pytest.mark.asyncio
async def test_attachment_download_rejects_invalid_and_expired_tokens(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    from app.main import app

    attachment = attachment_item()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/users/123456/items/ATTACH01":
            return httpx.Response(200, json=attachment)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    handoff_response = await async_client.post(
        "/v1/attachments/ATTACH01/handoff",
        headers=auth_headers,
        json={"mode": "proxy_download"},
    )
    assert handoff_response.status_code == 200

    token = handoff_response.json()["downloadUrl"].rsplit("/", 1)[-1]
    app.state.bridge_service._attachment_service._tokens[token] = (
        app.state.bridge_service._attachment_service._tokens[token].__class__(
            attachment_key="ATTACH01",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
            zotero_api_key="zotero-test-key",
            zotero_library_type="user",
            zotero_library_id="123456",
        )
    )

    expired_response = await async_client.get(f"/v1/attachments/download/{token}")
    invalid_response = await async_client.get("/v1/attachments/download/not-a-real-token")

    assert expired_response.status_code == 410
    assert invalid_response.status_code == 404


@pytest.mark.asyncio
async def test_advanced_search_falls_back_without_local_index_for_request_scoped_key(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    scoped_item = parent_item(key="ITEM9000", title="Scoped Library Paper")
    seen_search = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_search
        if request.method == "GET" and request.url.path == "/keys/scoped-key":
            return httpx.Response(200, json={"userID": 999999})
        if request.method == "GET" and request.url.path == "/users/999999/items":
            if request.url.params.get("q") == "scoped":
                seen_search = True
                return httpx.Response(
                    200,
                    json=[scoped_item],
                    headers={"Total-Results": "1"},
                )
            if request.url.params.get("itemKey") == "ITEM9000":
                return httpx.Response(200, json=[scoped_item])
        if request.method == "GET" and request.url.path == "/users/999999/items/top":
            raise AssertionError("request-scoped query search should not scan /items/top")
        if request.method == "GET" and request.url.path == "/users/999999/items/ITEM9000/children":
            return httpx.Response(
                200,
                json=[],
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/search-advanced",
        headers={**auth_headers, "X-Zotero-API-Key": "scoped-key"},
        params={
            "q": "scoped",
            "fields": "title",
            "includeAttachments": "false",
            "includeNotes": "false",
        },
    )

    assert response.status_code == 200
    assert seen_search is True
    assert response.json()["count"] == 1
    assert response.json()["items"][0]["itemKey"] == "ITEM9000"


@pytest.mark.asyncio
async def test_request_scoped_discovery_search_marks_existing_and_filters_duplicates(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    scoped_item = parent_item(
        key="ITEM9010",
        title="Scoped Existing Paper",
        doi="10.1000/existing",
    )
    openalex_payload = {
        "meta": {"count": 1},
        "results": [
            {
                "id": "https://openalex.org/W123",
                "doi": "https://doi.org/10.1000/existing",
                "display_name": "Scoped Existing Paper",
                "publication_year": 2025,
                "publication_date": "2025-01-01",
                "type": "article",
                "cited_by_count": 7,
                "authorships": [],
                "primary_location": {"source": {"display_name": "Test Venue"}},
                "open_access": {"is_oa": True},
                "abstract_inverted_index": None,
                "primary_topic": {},
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/keys/scoped-key":
            return httpx.Response(200, json={"userID": 999999})
        if request.method == "GET" and request.url.path == "/users/999999/items/top":
            return httpx.Response(200, json=[scoped_item], headers={"Total-Results": "1"})
        if request.method == "GET" and request.url.path == "/works":
            return httpx.Response(200, json=openalex_payload)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    marked_response = await async_client.get(
        "/v1/discovery/search",
        headers={**auth_headers, "X-Zotero-API-Key": "scoped-key"},
        params={"q": "existing", "resolveInLibrary": "true", "excludeExisting": "false"},
    )
    filtered_response = await async_client.get(
        "/v1/discovery/search",
        headers={**auth_headers, "X-Zotero-API-Key": "scoped-key"},
        params={"q": "existing", "resolveInLibrary": "true", "excludeExisting": "true"},
    )

    assert marked_response.status_code == 200
    assert marked_response.json()["count"] == 1
    assert marked_response.json()["items"][0]["alreadyInLibrary"] is True
    assert marked_response.json()["items"][0]["libraryItemKey"] == "ITEM9010"
    assert marked_response.json()["items"][0]["libraryMatchStrategy"] == "doi"

    assert filtered_response.status_code == 200
    assert filtered_response.json()["count"] == 0


@pytest.mark.asyncio
async def test_request_scoped_note_search_uses_upstream_query_candidates(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    scoped_item = parent_item(key="ITEM9000", title="Scoped Library Paper")
    scoped_note = note_item(
        key="NOTE9000",
        parent="ITEM9000",
        note_html="<p>quick_local_pdf_spot_check note body</p>",
        tags=[
            "zbridge",
            "zbridge:agent:codex",
            "zbridge:type:paper.summary",
            "zbridge:slot:default",
        ],
    )
    calls = {"search": 0, "children": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/keys/scoped-key":
            return httpx.Response(200, json={"userID": 999999})
        if request.method == "GET" and request.url.path == "/users/999999/items":
            if request.url.params.get("q") == "quick_local_pdf_spot_check":
                calls["search"] += 1
                return httpx.Response(
                    200,
                    json=[scoped_note],
                    headers={"Total-Results": "1"},
                )
            if request.url.params.get("itemKey") == "ITEM9000":
                return httpx.Response(200, json=[scoped_item])
        if request.method == "GET" and request.url.path == "/users/999999/items/ITEM9000/children":
            calls["children"] += 1
            return httpx.Response(200, json=[scoped_note])
        if request.method == "GET" and request.url.path == "/users/999999/items/top":
            raise AssertionError("request-scoped note search should not scan /items/top")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/search-advanced",
        headers={**auth_headers, "X-Zotero-API-Key": "scoped-key"},
        params={
            "q": "quick_local_pdf_spot_check",
            "fields": "note",
            "sort": "relevance",
            "includeAttachments": "false",
            "includeNotes": "false",
        },
    )

    assert response.status_code == 200
    assert calls == {"search": 1, "children": 1}
    assert response.json()["count"] == 1
    assert response.json()["items"][0]["itemKey"] == "ITEM9000"
    assert response.json()["items"][0]["searchHints"][0]["field"] == "aiNote:paper.summary"


@pytest.mark.asyncio
async def test_request_scoped_note_search_falls_back_to_structured_payload_scan(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    scoped_item = parent_item(key="ITEM9001", title="Scoped Payload Paper")
    note_html = NoteRenderer("zbridge").render(
        title="Quick Read",
        body_markdown="Human-readable body without the lookup token.",
        agent="codex",
        note_type="paper.summary",
        model=None,
        source_attachment_key=None,
        source_cursor_start=None,
        source_cursor_end=None,
        schema_version="1.0",
        payload={"readingMode": "quick_local_pdf_spot_check"},
        provenance=[],
        mode="replace",
    )
    scoped_note = note_item(
        key="NOTE9001",
        parent="ITEM9001",
        note_html=note_html,
        tags=[
            "zbridge",
            "zbridge:agent:codex",
            "zbridge:type:paper.summary",
            "zbridge:slot:default",
        ],
    )
    calls = {"search": 0, "notes": 0, "children": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/keys/scoped-key":
            return httpx.Response(200, json={"userID": 999999})
        if request.method == "GET" and request.url.path == "/users/999999/items":
            if request.url.params.get("q") == "quick_local_pdf_spot_check":
                calls["search"] += 1
                return httpx.Response(200, json=[], headers={"Total-Results": "0"})
            if request.url.params.get("itemType") == "note":
                calls["notes"] += 1
                return httpx.Response(200, json=[scoped_note], headers={"Total-Results": "1"})
            if request.url.params.get("itemKey") == "ITEM9001":
                return httpx.Response(200, json=[scoped_item])
        if request.method == "GET" and request.url.path == "/users/999999/items/top":
            raise AssertionError("payload fallback should scan notes, not /items/top")
        if request.method == "GET" and request.url.path == "/users/999999/items/ITEM9001/children":
            calls["children"] += 1
            return httpx.Response(200, json=[scoped_note])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.get(
        "/v1/items/search-advanced",
        headers={**auth_headers, "X-Zotero-API-Key": "scoped-key"},
        params={
            "q": "quick_local_pdf_spot_check",
            "fields": "note",
            "sort": "relevance",
            "includeAttachments": "false",
            "includeNotes": "false",
        },
    )

    assert response.status_code == 200
    assert calls == {"search": 1, "notes": 1, "children": 1}
    assert response.json()["count"] == 1
    assert response.json()["items"][0]["itemKey"] == "ITEM9001"
    assert response.json()["items"][0]["searchHints"][0]["field"] == "aiNote:paper.summary"


@pytest.mark.asyncio
async def test_request_scoped_note_search_reuses_note_cache_across_requests(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    scoped_item = parent_item(key="ITEM9002", title="Scoped Cached Payload Paper")
    note_html = NoteRenderer("zbridge").render(
        title="Cached Quick Read",
        body_markdown="Visible body without the token.",
        agent="codex",
        note_type="paper.summary",
        model=None,
        source_attachment_key=None,
        source_cursor_start=None,
        source_cursor_end=None,
        schema_version="1.0",
        payload={"readingMode": "cache_me_once"},
        provenance=[],
        mode="replace",
    )
    scoped_note = note_item(
        key="NOTE9002",
        parent="ITEM9002",
        note_html=note_html,
        tags=[
            "zbridge",
            "zbridge:agent:codex",
            "zbridge:type:paper.summary",
            "zbridge:slot:default",
        ],
    )
    calls = {"search": 0, "notes": 0, "children": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/keys/scoped-key":
            return httpx.Response(200, json={"userID": 999999})
        if request.method == "GET" and request.url.path == "/users/999999/items":
            if request.url.params.get("q") == "cache_me_once":
                calls["search"] += 1
                return httpx.Response(200, json=[], headers={"Total-Results": "0"})
            if request.url.params.get("itemType") == "note":
                calls["notes"] += 1
                return httpx.Response(200, json=[scoped_note], headers={"Total-Results": "1"})
            if request.url.params.get("itemKey") == "ITEM9002":
                return httpx.Response(200, json=[scoped_item])
        if request.method == "GET" and request.url.path == "/users/999999/items/ITEM9002/children":
            calls["children"] += 1
            return httpx.Response(200, json=[scoped_note])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    for _ in range(2):
        response = await async_client.get(
            "/v1/items/search-advanced",
            headers={**auth_headers, "X-Zotero-API-Key": "scoped-key"},
            params={
                "q": "cache_me_once",
                "fields": "note",
                "sort": "relevance",
                "includeAttachments": "false",
                "includeNotes": "false",
            },
        )
        assert response.status_code == 200
        assert response.json()["count"] == 1
        assert response.json()["items"][0]["itemKey"] == "ITEM9002"

    assert calls == {"search": 2, "notes": 1, "children": 2}


@pytest.mark.asyncio
async def test_import_metadata_creates_item(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    created = parent_item(key="ITEMNEW01", title="New Paper", doi="10.1000/new")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/users/123456/items":
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path == "/users/123456/items/top":
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path == "/items/new":
            return httpx.Response(
                200,
                json={
                    "itemType": "journalArticle",
                    "title": "",
                    "creators": [],
                    "DOI": "",
                    "publicationTitle": "",
                    "abstractNote": "",
                    "date": "",
                    "url": "",
                    "extra": "",
                    "tags": [],
                    "collections": [],
                },
            )
        if request.method == "POST" and request.url.path == "/users/123456/items":
            return httpx.Response(200, json={"successful": {"0": {"key": "ITEMNEW01"}}})
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEMNEW01":
            return httpx.Response(200, json=created)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEMNEW01/children":
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.post(
        "/v1/papers/import-metadata",
        headers=auth_headers,
        json={
            "itemType": "journalArticle",
            "title": "New Paper",
            "creators": [{"firstName": "Ada", "lastName": "Lovelace", "creatorType": "author"}],
            "doi": "10.1000/new",
            "collectionKey": "COLL1",
            "tags": ["review"],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "created",
        "itemKey": "ITEMNEW01",
        "title": "New Paper",
        "dedupeStrategy": "none",
    }


@pytest.mark.asyncio
async def test_merge_duplicates_preserves_non_duplicate_relations(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    related_uri = "https://api.zotero.org/users/123456/items/REL00001"
    duplicate_uri = "https://api.zotero.org/users/123456/items/ITEM0002"
    state = {
        "primary": zotero_item(
            "ITEM0001",
            {
                "itemType": "journalArticle",
                "title": "Primary",
                "date": "2024",
                "creators": [{"name": "Ada Lovelace", "creatorType": "author"}],
                "collections": ["COLL1"],
                "tags": [{"tag": "llm"}],
                "relations": {"dc:relation": [duplicate_uri]},
            },
            version=1,
        ),
        "duplicate": zotero_item(
            "ITEM0002",
            {
                "itemType": "journalArticle",
                "title": "Duplicate",
                "date": "2024",
                "creators": [{"name": "Ada Lovelace", "creatorType": "author"}],
                "collections": ["COLL1"],
                "tags": [{"tag": "review"}],
                "relations": {"dc:relation": [related_uri]},
            },
            version=1,
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=state["primary"])
        if request.method == "GET" and request.url.path == "/users/123456/items":
            if request.url.params.get("itemKey") == "ITEM0002":
                return httpx.Response(200, json=[state["duplicate"]])
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0002/children":
            return httpx.Response(200, json=[])
        if request.method == "PATCH" and request.url.path == "/users/123456/items/ITEM0001":
            payload = json.loads(request.content.decode("utf-8"))
            state["primary"] = zotero_item(
                "ITEM0001",
                {
                    **state["primary"]["data"],
                    **payload,
                },
                version=2,
            )
            return httpx.Response(204)
        if request.method == "DELETE" and request.url.path == "/users/123456/items/ITEM0002":
            return httpx.Response(204)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.post(
        "/v1/items/duplicates/merge",
        headers=auth_headers,
        json={
            "primaryItemKey": "ITEM0001",
            "duplicateItemKeys": ["ITEM0002"],
            "dryRun": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "merged"
    assert related_uri in payload["primaryItem"]["relations"]
    assert duplicate_uri not in payload["primaryItem"]["relations"]


@pytest.mark.asyncio
async def test_import_metadata_detects_existing_by_doi(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    existing = parent_item()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/users/123456/items":
            return httpx.Response(200, json=[existing])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.post(
        "/v1/papers/import-metadata",
        headers=auth_headers,
        json={
            "itemType": "journalArticle",
            "title": "Bridge Paper",
            "doi": "10.1000/bridge",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "exists"
    assert response.json()["dedupeStrategy"] == "doi"


@pytest.mark.asyncio
async def test_import_metadata_update_if_exists_updates_item(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    existing = parent_item()
    updated = parent_item(title="Bridge Paper Updated")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/users/123456/items":
            return httpx.Response(200, json=[existing])
        if request.method == "PATCH" and request.url.path == "/users/123456/items/ITEM0001":
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["title"] == "Bridge Paper Updated"
            return httpx.Response(204)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=updated)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.post(
        "/v1/papers/import-metadata",
        headers=auth_headers,
        json={
            "itemType": "journalArticle",
            "title": "Bridge Paper Updated",
            "doi": "10.1000/bridge",
            "updateIfExists": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "updated"
    assert response.json()["title"] == "Bridge Paper Updated"


@pytest.mark.asyncio
async def test_import_discovery_hit_uses_weak_dedupe(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    existing = parent_item(
        title="Discovery Match",
        doi=None,
        date="2024",
        creators=[{"name": "Grace Hopper", "creatorType": "author"}],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/users/123456/items/top":
            return httpx.Response(200, json=[existing])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.post(
        "/v1/papers/import-discovery-hit",
        headers=auth_headers,
        json={
            "title": "Discovery Match",
            "publicationYear": 2024,
            "authors": [{"name": "Grace Hopper"}],
            "venue": "Journal",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "exists"
    assert response.json()["dedupeStrategy"] == "title_author_year"


@pytest.mark.asyncio
async def test_structured_ai_note_round_trip_and_note_search(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    parent = parent_item()
    state: dict[str, Any] = {"note": None, "version": 1}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/users/123456/items":
            if request.url.params.get("itemKey") == "ITEM0001":
                return httpx.Response(200, json=[parent])
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            if state["note"] is None:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[state["note"]])
        if request.method == "POST" and request.url.path == "/users/123456/items":
            payload = json.loads(request.content.decode("utf-8"))
            note_html = payload[0]["note"]
            state["note"] = note_item(
                note_html=note_html,
                tags=[tag["tag"] for tag in payload[0]["tags"]],
            )
            return httpx.Response(200, json={"successful": {"0": {"key": "NOTE0001"}}})
        if request.method == "PATCH" and request.url.path == "/users/123456/items/NOTE0001":
            payload = json.loads(request.content.decode("utf-8"))
            state["version"] += 1
            state["note"] = zotero_item(
                "NOTE0001",
                {
                    **state["note"]["data"],
                    "note": payload["note"],
                    "tags": payload["tags"],
                },
                version=state["version"],
            )
            return httpx.Response(204)
        if request.method == "GET" and request.url.path == "/users/123456/items/NOTE0001":
            assert state["note"] is not None
            return httpx.Response(200, json=state["note"])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    create_response = await async_client.post(
        "/v1/items/ITEM0001/notes/upsert-ai-note",
        headers=auth_headers,
        json={
            "agent": "codex",
            "noteType": "paper.findings",
            "slot": "default",
            "title": "Key Findings",
            "bodyMarkdown": "Primary finding",
            "schemaVersion": "1.0",
            "payload": {"dataset": "benchmark-42", "findings": ["Primary finding"]},
            "provenance": [{"attachmentKey": "ATTACH01", "page": 5, "locator": "p.5"}],
        },
    )

    append_response = await async_client.post(
        "/v1/items/ITEM0001/notes/upsert-ai-note",
        headers=auth_headers,
        json={
            "agent": "codex",
            "noteType": "paper.findings",
            "slot": "default",
            "mode": "append",
            "title": "Key Findings",
            "bodyMarkdown": "Secondary finding",
            "schemaVersion": "1.1",
            "payload": {"dataset": "benchmark-42", "findings": ["Secondary finding"]},
        },
    )
    detail_response = await async_client.get("/v1/notes/NOTE0001", headers=auth_headers)
    search_response = await async_client.get(
        "/v1/items/search-advanced",
        headers=auth_headers,
        params={"q": "benchmark-42", "fields": "note", "sort": "relevance"},
    )

    assert create_response.status_code == 200
    assert append_response.status_code == 200
    assert detail_response.status_code == 200
    detail_payload = detail_response.json()["note"]
    assert detail_payload["schemaVersion"] == "1.1"
    assert detail_payload["structuredPayload"]["dataset"] == "benchmark-42"
    assert detail_payload["provenance"] == [
        {
            "attachmentKey": "ATTACH01",
            "page": 5,
            "locator": "p.5",
            "cursorStart": None,
            "cursorEnd": None,
            "quote": None,
        }
    ]
    assert "Primary finding" in detail_payload["bodyText"]
    assert "Secondary finding" in detail_payload["bodyText"]
    assert state["note"]["data"]["note"].count(STRUCTURED_BLOCK_MARKER) == 1
    assert search_response.status_code == 200
    assert search_response.json()["items"][0]["itemKey"] == "ITEM0001"
    assert search_response.json()["items"][0]["searchHints"][0]["field"] == "aiNote:paper.findings"


@pytest.mark.asyncio
async def test_ai_note_append_preserves_schema_version_in_response(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    from app.services.note_renderer import NoteRenderer

    parent = parent_item()
    renderer = NoteRenderer("zbridge")
    existing_note = note_item(
        key="NOTE0002",
        note_html=renderer.render(
            title="Key Findings",
            body_markdown="Primary finding",
            agent="codex",
            note_type="paper.findings",
            model=None,
            source_attachment_key=None,
            source_cursor_start=None,
            source_cursor_end=None,
            schema_version="1.0",
            payload={"dataset": "benchmark-42"},
            provenance=[],
            mode="replace",
        ),
        tags=[
            "zbridge",
            "zbridge:agent:codex",
            "zbridge:type:paper.findings",
            "zbridge:slot:default",
        ],
    )
    state: dict[str, Any] = {"note": existing_note, "version": 1}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            return httpx.Response(200, json=[state["note"]])
        if request.method == "PATCH" and request.url.path == "/users/123456/items/NOTE0002":
            payload = json.loads(request.content.decode("utf-8"))
            state["version"] += 1
            state["note"] = zotero_item(
                "NOTE0002",
                {
                    **state["note"]["data"],
                    "note": payload["note"],
                    "tags": payload["tags"],
                },
                version=state["version"],
            )
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.post(
        "/v1/items/ITEM0001/notes/upsert-ai-note",
        headers=auth_headers,
        json={
            "agent": "codex",
            "noteType": "paper.findings",
            "slot": "default",
            "mode": "append",
            "title": "Key Findings",
            "bodyMarkdown": "Secondary finding",
        },
    )

    assert response.status_code == 200
    assert response.json()["schemaVersion"] == "1.0"


@pytest.mark.asyncio
async def test_review_pack_rejects_removed_fulltext_compat_flags(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await async_client.post(
        "/v1/items/review-pack",
        headers=auth_headers,
        json={
            "itemKeys": ["ITEM0001"],
            "includeRelated": False,
            "includeNotes": False,
            "includeFulltextPreview": True,
            "maxFulltextChars": 3000,
        },
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_review_pack_excludes_note_summaries_when_include_notes_is_false(
    async_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    respx_mock: respx.MockRouter,
) -> None:
    parent = parent_item()
    attachment = attachment_item()
    note = note_item(
        key="NOTE0001",
        note_html="<p>Structured summary</p>",
        tags=[
            "zbridge",
            "zbridge:agent:codex-cli",
            "zbridge:type:paper.summary",
            "zbridge:slot:default",
        ],
    )
    child_request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal child_request_count
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001":
            if request.url.params.get("include") == "citation,bib":
                return httpx.Response(
                    200,
                    json={"citation": "<span>Bridge Paper</span>", "bib": "<div>Bridge Bib</div>"},
                )
            return httpx.Response(200, json=parent)
        if request.method == "GET" and request.url.path == "/users/123456/items/ITEM0001/children":
            child_request_count += 1
            return httpx.Response(200, json=[attachment, note])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    respx_mock.route().mock(side_effect=handler)

    response = await async_client.post(
        "/v1/items/review-pack",
        headers=auth_headers,
        json={
            "itemKeys": ["ITEM0001"],
            "includeRelated": False,
            "includeNotes": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert child_request_count == 1
    assert payload["items"][0]["item"]["attachments"][0]["attachmentKey"] == "ATTACH01"
    assert payload["items"][0]["item"]["aiNotes"] == []
    assert payload["items"][0]["notes"] == []
