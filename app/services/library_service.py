from __future__ import annotations

from typing import Any

from app.errors import BridgeError
from app.models import (
    AdvancedSearchResponse,
    BatchItemResponse,
    CitationResponse,
    ItemListResponse,
    NoteRecord,
    ResolveItemsResponse,
    ReviewPackItem,
    ReviewPackRequest,
    ReviewPackResponse,
    SearchHint,
    SearchItem,
    SearchResponse,
    SearchResultItem,
)
from app.services.note_search_cache import CachedNoteSearchRecord

DEFAULT_BATCH_CONCURRENCY = 6


class LibraryService:
    def __init__(self, bridge: Any) -> None:
        self._bridge = bridge

    async def search_items(
        self,
        *,
        q: str,
        start: int,
        limit: int,
        include_attachments: bool,
        include_notes: bool,
        item_type: str | None,
        collection_key: str | None,
        tag: str | None,
        sort: str | None,
        direction: str | None,
    ) -> SearchResponse:
        bridge = self._bridge
        upstream_refs, refs_by_key, seen = await bridge._collect_upstream_search_refs(
            q=q,
            item_type=item_type,
            collection_key=collection_key,
            tag=tag,
            sort=sort,
            direction=direction,
        )
        local_index_refs: list[dict[str, Any]] = []
        if include_notes:
            local_index_refs = await bridge._collect_local_index_search_refs(
                q=q,
                seen=seen,
                item_type=item_type,
                collection_key=collection_key,
                tag=tag,
                starting_ordinal=len(upstream_refs),
                existing_refs_by_key=refs_by_key,
            )
        combined_refs = [*upstream_refs, *local_index_refs]
        if sort in {"title", "dateAdded", "dateModified"}:
            await bridge._hydrate_search_result_refs_for_sort(combined_refs)
        ordered_refs = bridge._order_search_result_refs(
            upstream_refs=combined_refs,
            local_cache_refs=[],
            sort=sort,
            direction=direction,
        )
        page_items, next_start = await bridge._resolve_search_result_page(
            refs=ordered_refs,
            query=q,
            include_attachments=include_attachments,
            include_notes=include_notes,
            start=start,
            limit=limit,
        )
        total = len(ordered_refs)
        return SearchResponse(
            items=page_items,
            count=len(page_items),
            total=total,
            start=start,
            limit=limit,
            nextStart=next_start,
        )

    async def search_items_advanced(
        self,
        *,
        q: str | None,
        fields: str,
        title: str | None,
        author: str | None,
        abstract: str | None,
        venue: str | None,
        doi: str | None,
        year_from: int | None,
        year_to: int | None,
        start: int,
        limit: int,
        item_type: str | None,
        collection_key: str | None,
        tag: str | None,
        has_ai_notes: bool | None,
        include_attachments: bool,
        include_notes: bool,
        sort: str,
        direction: str | None,
    ) -> AdvancedSearchResponse:
        bridge = self._bridge
        bridge._validate_advanced_search_request(
            q=q,
            title=title,
            author=author,
            abstract=abstract,
            venue=venue,
            doi=doi,
            year_from=year_from,
            year_to=year_to,
            has_ai_notes=has_ai_notes,
        )
        normalized_fields = bridge._advanced_search_fields(fields)
        query = (q or "").strip()
        query_casefold = query.casefold()
        needs_notes = include_notes or has_ai_notes is not None or "note" in normalized_fields
        needs_attachments = include_attachments

        hint_map: dict[str, list[SearchHint]] = {}
        score_map: dict[str, float] = {}
        local_search_fields = {
            field
            for field in normalized_fields
            if field in {"title", "creator", "abstract", "venue", "doi", "tag", "note"}
        }
        local_hits = await bridge._search_local_index_hits(
            query=query,
            fields=local_search_fields,
            item_type=item_type,
            collection_key=collection_key,
            tag=tag,
        )
        candidate_keys: set[str] = set()
        for hit in local_hits:
            record = hit.get("record", {})
            item_key_value = str(record.get("itemKey") or "").strip()
            if not item_key_value:
                continue
            hints = [hint for hint in hit.get("hints", []) if isinstance(hint, SearchHint)]
            if hints:
                hint_map[item_key_value] = hints
            score_map[item_key_value] = bridge._coerce_optional_float(hit.get("score")) or 0.0
            candidate_keys.add(item_key_value)

        if query and bridge._local_search_index is None:
            upstream_candidate_keys, upstream_hints, upstream_scores = (
                await self._collect_upstream_query_candidates(
                    query=query,
                    item_type=item_type,
                    collection_key=collection_key,
                    tag=tag,
                )
            )
            candidate_keys.update(upstream_candidate_keys)
            for item_key_value, hints in upstream_hints.items():
                hint_map[item_key_value] = bridge._dedupe_search_hints(
                    [*hint_map.get(item_key_value, []), *hints]
                )
            for item_key_value, score in upstream_scores.items():
                score_map[item_key_value] = max(score_map.get(item_key_value, 0.0), score)

        if query:
            if not candidate_keys:
                if bridge._local_search_index is None and "note" in normalized_fields:
                    scanned_keys, scanned_hints, scanned_scores = (
                        await self._collect_request_scoped_note_scan_candidates(
                            query=query_casefold,
                            item_type=item_type,
                            collection_key=collection_key,
                            tag=tag,
                        )
                    )
                    candidate_keys.update(scanned_keys)
                    for item_key_value, hints in scanned_hints.items():
                        hint_map[item_key_value] = bridge._dedupe_search_hints(
                            [*hint_map.get(item_key_value, []), *hints]
                        )
                    for item_key_value, score in scanned_scores.items():
                        score_map[item_key_value] = max(score_map.get(item_key_value, 0.0), score)
                    if not candidate_keys:
                        return AdvancedSearchResponse(
                            items=[],
                            count=0,
                            total=0,
                            start=start,
                            limit=limit,
                            nextStart=None,
                        )
                else:
                    return AdvancedSearchResponse(
                        items=[],
                        count=0,
                        total=0,
                        start=start,
                        limit=limit,
                        nextStart=None,
                    )
            raw_items = await bridge._zotero.get_items_by_keys_raw(sorted(candidate_keys))
        else:
            raw_items = await bridge._list_all_top_level_items_raw(
                item_type=item_type,
                collection_key=collection_key,
                tag=tag,
                sort="dateModified",
                direction="desc",
        )
        matches: list[SearchResultItem] = []
        for raw_item in raw_items:
            item_key = str(raw_item.get("key") or "")
            children: list[dict[str, Any]] = []
            if needs_attachments or needs_notes:
                children = await bridge._zotero.get_children(item_key)
            item = await bridge._normalize_parent_item(
                raw_item,
                children=children,
                include_attachments=needs_attachments,
                include_notes=needs_notes,
            )
            if not bridge._item_matches_filters(
                item=item,
                item_type=item_type,
                collection_key=collection_key,
                tag=tag,
            ):
                continue
            local_note_hints = (
                bridge._note_search_hints_from_children(children=children, query=query_casefold)
                if query and "note" in normalized_fields
                else []
            )
            if not bridge._advanced_item_matches(
                item=item,
                raw_item=raw_item,
                query=query_casefold,
                fields=normalized_fields,
                title=title,
                author=author,
                abstract=abstract,
                venue=venue,
                doi=doi,
                year_from=year_from,
                year_to=year_to,
                has_ai_notes=has_ai_notes,
                note_match=bool(local_note_hints)
                or any(
                    bridge._search_hint_field_key(hint.field) in {"note", "aiNote"}
                    for hint in hint_map.get(item.itemKey, [])
                ),
            ):
                continue
            extra_hints = bridge._dedupe_search_hints(
                [*hint_map.get(item.itemKey, []), *local_note_hints]
            )
            matches.append(
                bridge._build_advanced_search_result_item(
                    item=item,
                    query=query_casefold,
                    fields=normalized_fields,
                    extra_hints=extra_hints,
                    score=max(
                        score_map.get(item.itemKey, 0.0),
                        bridge._score_search_hints(extra_hints),
                    ),
                )
            )

        matches = bridge._sort_advanced_search_results(
            matches=matches,
            sort=sort,
            direction=direction,
            query=query_casefold,
        )
        total = len(matches)
        page_items = matches[start : start + limit]
        return AdvancedSearchResponse(
            items=page_items,
            count=len(page_items),
            total=total,
            start=start,
            limit=limit,
            nextStart=bridge._next_start(
                start=start,
                returned_count=len(page_items),
                total=total,
            ),
        )

    async def _collect_upstream_query_candidates(
        self,
        *,
        query: str,
        item_type: str | None,
        collection_key: str | None,
        tag: str | None,
    ) -> tuple[set[str], dict[str, list[SearchHint]], dict[str, float]]:
        bridge = self._bridge
        candidate_keys: set[str] = set()
        hint_map: dict[str, list[SearchHint]] = {}
        score_map: dict[str, float] = {}
        start = 0
        page_size = 100

        while True:
            raw_hits, _ = await bridge._zotero.search_items_page_raw(
                q=query,
                start=start,
                limit=page_size,
                item_type=item_type,
                tag=tag,
                collection_key=collection_key,
                sort=None,
                direction=None,
            )
            if not raw_hits:
                break

            for raw_hit in raw_hits:
                item_key = bridge._parent_item_key_from_raw(raw_hit)
                if item_key is None:
                    continue
                candidate_keys.add(item_key)
                hints = bridge._raw_hit_search_hints(
                    item_key=item_key,
                    raw_hit=raw_hit,
                    query=query,
                )
                if not hints:
                    continue
                hint_map[item_key] = bridge._dedupe_search_hints(
                    [*hint_map.get(item_key, []), *hints]
                )
                score_map[item_key] = max(
                    score_map.get(item_key, 0.0),
                    bridge._score_search_hints(hint_map[item_key]),
                )

            start += len(raw_hits)
            if len(raw_hits) < page_size:
                break

        return candidate_keys, hint_map, score_map

    async def _collect_request_scoped_note_scan_candidates(
        self,
        *,
        query: str,
        item_type: str | None,
        collection_key: str | None,
        tag: str | None,
    ) -> tuple[set[str], dict[str, list[SearchHint]], dict[str, float]]:
        bridge = self._bridge
        note_records = await self._request_scoped_note_search_records()
        candidate_keys: set[str] = set()
        hint_map: dict[str, list[SearchHint]] = {}
        score_map: dict[str, float] = {}

        for note_record in note_records:
            snippet = bridge._query_snippet(
                note_record.visible_text,
                query,
            ) or bridge._query_snippet(
                note_record.structured_text,
                query,
            )
            if snippet is None:
                continue
            candidate_keys.add(note_record.item_key)
            hint_map[note_record.item_key] = bridge._dedupe_search_hints(
                [
                    *hint_map.get(note_record.item_key, []),
                    SearchHint(field=note_record.hint_field, snippet=snippet),
                ]
            )
            score_map[note_record.item_key] = bridge._score_search_hints(
                hint_map[note_record.item_key]
            )

        return candidate_keys, hint_map, score_map

    async def _list_all_library_notes_raw(self) -> list[dict[str, Any]]:
        bridge = self._bridge
        raw_notes: list[dict[str, Any]] = []
        start = 0
        page_size = 100

        while True:
            page, _ = await bridge._zotero.list_items_page_raw(
                start=start,
                limit=page_size,
                item_type="note",
                collection_key=None,
                tag=None,
                sort="dateModified",
                direction="desc",
                top_level=False,
            )
            if not page:
                break
            raw_notes.extend(page)
            start += len(page)
            if len(page) < page_size:
                break

        return raw_notes

    async def _request_scoped_note_search_records(self) -> list[CachedNoteSearchRecord]:
        bridge = self._bridge
        cache = bridge._note_search_cache
        if cache is None:
            return await self._build_request_scoped_note_search_records()
        return await cache.get_or_build(
            scope=bridge._note_search_scope(),
            builder=self._build_request_scoped_note_search_records,
        )

    async def _build_request_scoped_note_search_records(self) -> list[CachedNoteSearchRecord]:
        bridge = self._bridge
        raw_notes = await self._list_all_library_notes_raw()
        records: list[CachedNoteSearchRecord] = []
        for raw_note in raw_notes:
            item_key = bridge._parent_item_key_from_raw(raw_note)
            if item_key is None:
                continue
            note_html = str(raw_note.get("data", {}).get("note") or "")
            parsed_note = bridge._note_renderer.parse(note_html)
            visible_text = bridge._normalize_search_text(
                bridge._note_renderer.to_plain_text(parsed_note.human_html)
            ) or ""
            structured_text = bridge._normalize_search_text(
                bridge._note_renderer.structured_payload_text(parsed_note.payload)
            ) or ""
            if not visible_text and not structured_text:
                continue
            records.append(
                CachedNoteSearchRecord(
                    note_key=str(raw_note.get("key") or ""),
                    item_key=item_key,
                    hint_field=bridge._note_search_hint_field(raw_note),
                    visible_text=visible_text,
                    structured_text=structured_text,
                )
            )
        return records

    async def build_review_pack(self, payload: ReviewPackRequest) -> ReviewPackResponse:
        bridge = self._bridge
        citation_style = payload.citationStyle or bridge._settings.default_citation_style
        citation_locale = payload.citationLocale or bridge._settings.default_citation_locale

        async def build_one(item_key: str) -> tuple[str, ReviewPackItem | None]:
            try:
                item = await bridge.get_parent_item(
                    item_key=item_key,
                    include_attachments=True,
                    include_notes=payload.includeNotes,
                )
            except BridgeError as exc:
                if exc.code == "ITEM_NOT_FOUND":
                    return item_key, None
                raise
            citation = await self.get_item_citation(
                item_key=item_key,
                style=citation_style,
                locale=citation_locale,
                linkwrap=False,
            )
            notes: list[NoteRecord] = []
            if payload.includeNotes:
                notes = (await bridge.list_item_notes(item_key)).notes
            related_items: list[SearchItem] = []
            if payload.includeRelated:
                related_items = (
                    await bridge.get_related_items(
                        item_key=item_key,
                        include_attachments=False,
                        include_notes=False,
                    )
                ).items
            return (
                item_key,
                ReviewPackItem(
                    item=item,
                    citation=citation,
                    notes=notes,
                    relatedItems=related_items,
                ),
            )

        results = await bridge._map_with_concurrency(
            payload.itemKeys,
            build_one,
            limit=min(DEFAULT_BATCH_CONCURRENCY, len(payload.itemKeys)),
        )
        items: list[ReviewPackItem] = []
        not_found_keys: list[str] = []
        for item_key, review_item in results:
            if review_item is None:
                not_found_keys.append(item_key)
                continue
            items.append(review_item)
        return ReviewPackResponse(
            items=items,
            count=len(items),
            notFoundKeys=not_found_keys,
            warnings=[],
        )

    async def list_items(
        self,
        *,
        start: int,
        limit: int,
        item_type: str | None,
        collection_key: str | None,
        tag: str | None,
        include_attachments: bool,
        include_notes: bool,
        sort: str,
        direction: str,
    ) -> ItemListResponse:
        bridge = self._bridge
        raw_items, total = await bridge._zotero.list_top_level_items_raw(
            start=start,
            limit=limit,
            item_type=item_type,
            collection_key=collection_key,
            tag=tag,
            sort=sort,
            direction=direction,
        )
        items = await bridge._map_with_concurrency(
            raw_items,
            lambda raw_item: bridge._normalize_parent_item(
                raw_item,
                include_attachments=include_attachments,
                include_notes=include_notes,
            ),
            limit=min(DEFAULT_BATCH_CONCURRENCY, len(raw_items)),
        )
        return ItemListResponse(
            items=items,
            count=len(items),
            total=total,
            start=start,
            limit=limit,
            nextStart=bridge._next_start(
                start=start,
                returned_count=len(items),
                total=total,
            ),
        )

    async def batch_get_items(
        self,
        *,
        item_keys: list[str],
        include_attachments: bool,
        include_notes: bool,
    ) -> BatchItemResponse:
        bridge = self._bridge
        valid_item_keys = [
            item_key for item_key in item_keys if bridge._is_probably_zotero_key(item_key)
        ]
        raw_items = await bridge._zotero.get_items_by_keys_raw(valid_item_keys)
        raw_by_key = {
            str(raw_item.get("key") or ""): raw_item
            for raw_item in raw_items
            if str(raw_item.get("key") or "")
        }
        items: list[SearchItem] = []
        not_found_keys: list[str] = []
        for item_key in item_keys:
            if not bridge._is_probably_zotero_key(item_key):
                not_found_keys.append(item_key)
                continue
            raw_item = raw_by_key.get(item_key)
            if raw_item is None:
                not_found_keys.append(item_key)
                continue
            data = raw_item.get("data", {})
            if data.get("itemType") in {"attachment", "note"} and data.get("parentItem"):
                item = await bridge.get_parent_item(
                    item_key=str(data["parentItem"]),
                    include_attachments=include_attachments,
                    include_notes=include_notes,
                )
            else:
                item = await bridge._normalize_parent_item(
                    raw_item,
                    include_attachments=include_attachments,
                    include_notes=include_notes,
                )
            items.append(item)
        return BatchItemResponse(items=items, count=len(items), notFoundKeys=not_found_keys)

    async def resolve_items(
        self,
        *,
        doi: str | None,
        title: str | None,
        item_type: str | None,
        collection_key: str | None,
        tag: str | None,
        include_attachments: bool,
        include_notes: bool,
        limit: int,
    ) -> ResolveItemsResponse:
        bridge = self._bridge
        if doi:
            items = await bridge._resolve_items_by_doi(
                doi=doi,
                item_type=item_type,
                collection_key=collection_key,
                tag=tag,
                include_attachments=include_attachments,
                include_notes=include_notes,
                limit=limit,
            )
            return ResolveItemsResponse(strategy="doi", items=items, count=len(items))
        if title:
            items = await bridge._resolve_items_by_title(
                title=title,
                item_type=item_type,
                collection_key=collection_key,
                tag=tag,
                include_attachments=include_attachments,
                include_notes=include_notes,
                limit=limit,
            )
            return ResolveItemsResponse(strategy="title", items=items, count=len(items))
        raise BridgeError(
            code="BAD_REQUEST",
            message="One of doi or title is required",
            status_code=400,
        )

    async def get_item_citation(
        self,
        *,
        item_key: str,
        style: str,
        locale: str,
        linkwrap: bool,
    ) -> CitationResponse:
        bridge = self._bridge
        payload = await bridge._zotero.get_citation(
            item_key=item_key,
            style=style,
            locale=locale,
            linkwrap=linkwrap,
        )
        citation_html = str(payload.get("citation") or "")
        bibliography_html = str(payload.get("bib") or "")
        return CitationResponse(
            itemKey=item_key,
            style=style,
            locale=locale,
            citationHtml=citation_html,
            bibliographyHtml=bibliography_html,
        )
