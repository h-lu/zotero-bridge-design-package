from __future__ import annotations

from pathlib import Path

from app.services.fulltext import FulltextService
from app.services.local_fulltext_store import LocalFulltextStore


def test_selects_newest_pdf_attachment() -> None:
    service = FulltextService(default_max_chars=8000, hard_max_chars=12000)
    children = [
        {
            "key": "ATTACH01",
            "data": {
                "itemType": "attachment",
                "contentType": "application/pdf",
                "dateModified": "2025-01-01T00:00:00Z",
            },
        },
        {
            "key": "ATTACH02",
            "data": {
                "itemType": "attachment",
                "contentType": "application/pdf",
                "dateModified": "2025-02-01T00:00:00Z",
            },
        },
    ]

    selection = service.select_attachment(children)
    assert selection.attachment["key"] == "ATTACH02"
    assert selection.candidate_keys == ["ATTACH02", "ATTACH01"]


def test_chunk_response_preserves_cursor_semantics() -> None:
    service = FulltextService(default_max_chars=8000, hard_max_chars=12000)
    content = ("Para one.\n\nPara two is a bit longer.\n\nPara three.\n\n" * 40).strip()

    response = service.build_chunk_response(
        item_key="ITEM0001",
        attachment_key="ATTACH01",
        fulltext_payload={"content": content, "indexedPages": 1, "totalPages": 1},
        cursor=0,
        max_chars=1000,
        candidate_keys=["ATTACH01"],
    )

    assert response.cursor == 0
    assert response.nextCursor is not None
    assert response.done is False
    assert response.content.startswith("Para one.")


def test_local_fulltext_store_roundtrips_payload(tmp_path: Path) -> None:
    store = LocalFulltextStore(tmp_path)
    store.write_payload(
        attachment_key="ATTACH01",
        item_key="ITEM0001",
        filename="paper.pdf",
        fulltext_payload={
            "content": "Local payload text",
            "indexedPages": 1,
            "totalPages": 2,
        },
    )

    payload = store.get_payload("ATTACH01")

    assert payload == {
        "content": "Local payload text",
        "indexedPages": 1,
        "totalPages": 2,
    }


def test_local_fulltext_store_can_search_item_keys(tmp_path: Path) -> None:
    store = LocalFulltextStore(tmp_path)
    store.write_payload(
        attachment_key="ATTACH01",
        item_key="ITEM0001",
        filename="paper.pdf",
        fulltext_payload={
            "content": "Transformer encoder search token",
            "indexedPages": 1,
            "totalPages": 1,
        },
    )
    store.write_payload(
        attachment_key="ATTACH02",
        item_key="ITEM0002",
        filename="other.pdf",
        fulltext_payload={
            "content": "Different content",
            "indexedPages": 1,
            "totalPages": 1,
        },
    )

    payload = store.search_item_keys("search token", limit=5)

    assert payload == ["ITEM0001"]


def test_local_fulltext_store_uses_attachment_key_for_legacy_top_level_records(
    tmp_path: Path,
) -> None:
    store = LocalFulltextStore(tmp_path)
    store.write_payload(
        attachment_key="ATTACH01",
        item_key=None,
        filename="standalone.pdf",
        fulltext_payload={
            "content": "Legacy standalone cache token",
            "indexedPages": 1,
            "totalPages": 1,
        },
    )

    assert store.search_item_keys("standalone cache token", limit=5) == ["ATTACH01"]
    assert (
        store.first_match_snippet(item_key="ATTACH01", query="standalone cache token")
        == "Legacy standalone cache token"
    )
    assert store.item_search_text("ATTACH01") == "Legacy standalone cache token"
    assert store.delete_item_records("ATTACH01") == 1
    assert store.get_payload("ATTACH01") is None


def test_local_fulltext_store_can_delete_item_records(tmp_path: Path) -> None:
    store = LocalFulltextStore(tmp_path)
    store.write_payload(
        attachment_key="ATTACH01",
        item_key="ITEM0001",
        filename="paper.pdf",
        fulltext_payload={
            "content": "First record",
            "indexedPages": 1,
            "totalPages": 1,
        },
    )
    store.write_payload(
        attachment_key="ATTACH02",
        item_key="ITEM0001",
        filename="appendix.pdf",
        fulltext_payload={
            "content": "Second record",
            "indexedPages": 1,
            "totalPages": 1,
        },
    )
    store.write_payload(
        attachment_key="ATTACH03",
        item_key="ITEM0002",
        filename="other.pdf",
        fulltext_payload={
            "content": "Keep me",
            "indexedPages": 1,
            "totalPages": 1,
        },
    )

    deleted = store.delete_item_records("ITEM0001")

    assert deleted == 2
    assert store.get_payload("ATTACH01") is None
    assert store.get_payload("ATTACH02") is None
    assert store.get_payload("ATTACH03") == {
        "content": "Keep me",
        "indexedPages": 1,
        "totalPages": 1,
    }
