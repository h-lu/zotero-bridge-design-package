from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from app.services.note_renderer import STRUCTURED_BLOCK_MARKER

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
        )
    )

    expired_response = await async_client.get(f"/v1/attachments/download/{token}")
    invalid_response = await async_client.get("/v1/attachments/download/not-a-real-token")

    assert expired_response.status_code == 410
    assert invalid_response.status_code == 404


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
