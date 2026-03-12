from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NoteSearchScope:
    api_base: str
    library_type: str
    library_id: str


@dataclass(frozen=True, slots=True)
class CachedNoteSearchRecord:
    note_key: str
    item_key: str
    hint_field: str
    visible_text: str
    structured_text: str


@dataclass(slots=True)
class _CacheEntry:
    records: list[CachedNoteSearchRecord]
    expires_at: float


class NoteSearchCache:
    def __init__(self, *, ttl_seconds: int) -> None:
        self._ttl_seconds = max(ttl_seconds, 1)
        self._entries: dict[NoteSearchScope, _CacheEntry] = {}
        self._locks: dict[NoteSearchScope, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def get_or_build(
        self,
        *,
        scope: NoteSearchScope,
        builder: Callable[[], Awaitable[list[CachedNoteSearchRecord]]],
    ) -> list[CachedNoteSearchRecord]:
        cached = self._entries.get(scope)
        now = time.monotonic()
        if cached is not None and cached.expires_at > now:
            return cached.records

        lock = await self._scope_lock(scope)
        async with lock:
            cached = self._entries.get(scope)
            now = time.monotonic()
            if cached is not None and cached.expires_at > now:
                return cached.records
            records = await builder()
            self._entries[scope] = _CacheEntry(
                records=records,
                expires_at=now + self._ttl_seconds,
            )
            return records

    def invalidate(self, *, scope: NoteSearchScope) -> None:
        self._entries.pop(scope, None)

    async def _scope_lock(self, scope: NoteSearchScope) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(scope)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[scope] = lock
            return lock
