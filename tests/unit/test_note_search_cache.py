from __future__ import annotations

import asyncio

import pytest

from app.services.note_search_cache import CachedNoteSearchRecord, NoteSearchCache, NoteSearchScope


@pytest.mark.asyncio
async def test_note_search_cache_reuses_builder_until_invalidated() -> None:
    cache = NoteSearchCache(ttl_seconds=60)
    scope = NoteSearchScope(
        api_base="https://api.zotero.org",
        library_type="user",
        library_id="123456",
    )
    calls = 0

    async def builder() -> list[CachedNoteSearchRecord]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return [
            CachedNoteSearchRecord(
                note_key="NOTE0001",
                item_key="ITEM0001",
                hint_field="aiNote:paper.summary",
                visible_text="visible",
                structured_text="structured",
            )
        ]

    first = await cache.get_or_build(scope=scope, builder=builder)
    second = await cache.get_or_build(scope=scope, builder=builder)

    assert calls == 1
    assert first == second

    cache.invalidate(scope=scope)
    third = await cache.get_or_build(scope=scope, builder=builder)

    assert calls == 2
    assert third == first
