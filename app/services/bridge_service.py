from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import secrets
import socket
import time
from datetime import UTC, datetime
from functools import cmp_to_key
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urljoin

import httpx

from app import __version__
from app.config import Settings
from app.errors import BridgeError
from app.models import (
    AddByDOIRequest,
    AddByDOIResponse,
    AddByDOIStatus,
    AdvancedSearchResponse,
    AINoteSummary,
    AttachmentSummary,
    BatchFulltextPreviewResponse,
    BatchItemResponse,
    CitationResponse,
    CollectionListResponse,
    CollectionSummary,
    Creator,
    DiscoveryAuthor,
    DiscoverySearchResponse,
    DiscoveryTopic,
    DiscoveryWork,
    DuplicateGroup,
    DuplicateGroupsResponse,
    DuplicateStats,
    FulltextPreviewItem,
    FulltextResponse,
    HealthConfig,
    HealthResponse,
    ItemChangesResponse,
    ItemCollectionsResponse,
    ItemDetailResponse,
    ItemListResponse,
    ItemNotesResponse,
    ItemTagsResponse,
    ItemTypeCount,
    LibraryStatsResponse,
    MergeDuplicateItemsResponse,
    MergeDuplicateItemsStatus,
    NoteDeleteResponse,
    NoteDeleteStatus,
    NoteDetailResponse,
    NoteRecord,
    NoteWriteRequest,
    NoteWriteResponse,
    NoteWriteStatus,
    RelatedItemsResponse,
    ResolveItemsResponse,
    ReviewPackItem,
    ReviewPackRequest,
    ReviewPackResponse,
    SearchHint,
    SearchIndexStats,
    SearchItem,
    SearchResponse,
    SearchResultItem,
    TagListResponse,
    TagSummary,
    UploadPdfActionRequest,
    UploadPdfResponse,
    UploadPdfStatus,
    UpsertAINoteRequest,
    UpsertAINoteResponse,
    UpsertAINoteStatus,
)
from app.services.doi_resolver import DOIResolver
from app.services.fulltext import FulltextService
from app.services.local_fulltext_store import LocalFulltextStore
from app.services.local_search_index import LocalSearchIndex
from app.services.note_renderer import NoteRenderer
from app.services.zotero_client import ZoteroClient

REMOTE_DOWNLOAD_MAX_REDIRECTS = 5
NOTE_UPDATE_MAX_ATTEMPTS = 3
DEFAULT_BATCH_CONCURRENCY = 6
ZOTERO_KEY_PATTERN = re.compile(r"^[A-Za-z0-9]{8}$")
ZOTERO_RELATION_ITEM_PATTERN = re.compile(r"/items/([A-Za-z0-9]{8})$")


class BridgeService:
    def __init__(
        self,
        *,
        settings: Settings,
        http_client: httpx.AsyncClient,
        zotero_client: ZoteroClient,
        doi_resolver: DOIResolver,
        note_renderer: NoteRenderer,
        fulltext_service: FulltextService,
        local_fulltext_store: LocalFulltextStore | None,
        local_search_index: LocalSearchIndex | None,
    ) -> None:
        self._settings = settings
        self._http_client = http_client
        self._zotero = zotero_client
        self._doi_resolver = doi_resolver
        self._note_renderer = note_renderer
        self._fulltext = fulltext_service
        self._local_fulltext_store = local_fulltext_store
        self._local_search_index = local_search_index
        self._local_search_index_lock = asyncio.Lock()
        self._local_search_index_sync_task: asyncio.Task[None] | None = None

    async def validate_upstream_key(self) -> bool:
        return await self._zotero.validate_key()

    def build_health(self, *, key_valid: bool | None = None) -> HealthResponse:
        ok = self._settings.zotero_configured
        if key_valid is False:
            ok = False
        return HealthResponse(
            ok=ok,
            service="zotero-bridge",
            version=__version__,
            config=HealthConfig(
                zoteroConfigured=self._settings.zotero_configured,
                libraryType=self._settings.zotero_library_type,
                libraryId=self._settings.zotero_library_id,
            ),
        )

    def _build_search_index_stats(self) -> SearchIndexStats:
        if self._local_search_index is None:
            return SearchIndexStats(enabled=False, ready=False)
        state = self._local_search_index.state()
        return SearchIndexStats(
            enabled=True,
            ready=bool(state.get("ready")),
            recordCount=self._coerce_optional_int(state.get("count")) or 0,
            refreshedAt=self._format_unix_timestamp(state.get("refreshedAt")),
            lastModifiedVersion=self._coerce_optional_int(state.get("lastModifiedVersion")),
            lastSyncMethod=self._clean_optional_str(state.get("lastSyncMethod")),
            lastError=self._clean_optional_str(state.get("lastError")),
            lastErrorAt=self._format_unix_timestamp(state.get("lastErrorAt")),
        )

    async def startup(self) -> None:
        if self._local_search_index is None or self._settings.app_env == "test":
            return
        if (
            self._local_search_index_sync_task is not None
            and not self._local_search_index_sync_task.done()
        ):
            return
        self._local_search_index_sync_task = asyncio.create_task(
            self._local_search_index_sync_loop(),
            name="local-search-index-sync",
        )

    async def shutdown(self) -> None:
        task = self._local_search_index_sync_task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._local_search_index_sync_task = None

    async def add_by_doi(self, payload: AddByDOIRequest) -> AddByDOIResponse:
        normalized_doi = self._doi_resolver.normalize_doi(payload.doi)
        existing = await self._find_item_by_doi(normalized_doi)
        if existing is not None:
            normalized = await self._normalize_parent_item(
                existing,
                include_attachments=False,
                include_notes=False,
            )
            return AddByDOIResponse(
                status=AddByDOIStatus.EXISTING,
                itemKey=normalized.itemKey,
                title=normalized.title,
                DOI=normalized.DOI,
            )

        metadata = await self._doi_resolver.resolve(normalized_doi)
        existing = await self._find_item_by_doi(
            normalized_doi,
            title_hint=self._doi_resolver._first_text(metadata.get("title")),
        )
        if existing is not None:
            normalized = await self._normalize_parent_item(
                existing,
                include_attachments=False,
                include_notes=False,
            )
            return AddByDOIResponse(
                status=AddByDOIStatus.EXISTING,
                itemKey=normalized.itemKey,
                title=normalized.title,
                DOI=normalized.DOI,
            )
        item_type = self._doi_resolver.guess_zotero_item_type(metadata)
        template = await self._zotero.get_item_template(item_type)
        item_payload = self._doi_resolver.build_zotero_item(
            metadata=metadata,
            template=template,
            doi=normalized_doi,
            collection_key=payload.collectionKey,
            default_collection_key=self._settings.default_collection_key,
            tags=payload.tags,
        )
        write_token = self._build_write_token(payload.requestId)
        try:
            created_key = (
                await self._zotero.create_items([item_payload], write_token=write_token)
            )[0]
        except BridgeError as exc:
            if exc.code == "WRITE_CONFLICT":
                existing = await self._find_item_by_doi(normalized_doi)
                if existing is not None:
                    normalized = await self._normalize_parent_item(
                        existing,
                        include_attachments=False,
                        include_notes=False,
                    )
                    return AddByDOIResponse(
                        status=AddByDOIStatus.EXISTING,
                        itemKey=normalized.itemKey,
                        title=normalized.title,
                        DOI=normalized.DOI,
                    )
            raise

        created_item = await self._zotero.get_item(created_key)
        normalized = await self._normalize_parent_item(
            created_item,
            include_attachments=False,
            include_notes=False,
        )
        await self._refresh_local_search_index_item(normalized.itemKey)
        return AddByDOIResponse(
            status=AddByDOIStatus.CREATED,
            itemKey=normalized.itemKey,
            title=normalized.title,
            DOI=normalized.DOI,
        )

    async def search_items(
        self,
        *,
        q: str,
        start: int,
        limit: int,
        include_fulltext: bool,
        include_attachments: bool,
        include_notes: bool,
        item_type: str | None,
        collection_key: str | None,
        tag: str | None,
        sort: str | None,
        direction: str | None,
    ) -> SearchResponse:
        upstream_refs, refs_by_key, seen = await self._collect_upstream_search_refs(
            q=q,
            include_fulltext=include_fulltext,
            item_type=item_type,
            collection_key=collection_key,
            tag=tag,
            sort=sort,
            direction=direction,
        )

        local_cache_refs = await self._collect_local_cache_search_refs(
            q=q,
            include_fulltext=include_fulltext,
            seen=seen,
            item_type=item_type,
            collection_key=collection_key,
            tag=tag,
            starting_ordinal=len(upstream_refs),
        )
        local_index_refs: list[dict[str, Any]] = []
        if include_notes and not upstream_refs and not local_cache_refs:
            local_index_refs = await self._collect_local_index_search_refs(
                q=q,
                seen=seen,
                item_type=item_type,
                collection_key=collection_key,
                tag=tag,
                starting_ordinal=len(upstream_refs) + len(local_cache_refs),
                existing_refs_by_key=refs_by_key,
            )
        ordered_refs = self._order_search_result_refs(
            upstream_refs=[*upstream_refs, *local_index_refs],
            local_cache_refs=local_cache_refs,
            sort=sort,
            direction=direction,
        )
        page_items, next_start = await self._resolve_search_result_page(
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

    async def _collect_upstream_search_refs(
        self,
        *,
        q: str,
        include_fulltext: bool,
        item_type: str | None,
        collection_key: str | None,
        tag: str | None,
        sort: str | None,
        direction: str | None,
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], set[str]]:
        page_size = 100
        raw_start = 0
        upstream_refs: list[dict[str, Any]] = []
        refs_by_key: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()

        while True:
            raw_items, _ = await self._zotero.search_items_page_raw(
                q=q,
                start=raw_start,
                limit=page_size,
                include_fulltext=include_fulltext,
                item_type=item_type,
                tag=tag,
                collection_key=collection_key,
                sort=sort,
                direction=direction,
            )
            if not raw_items:
                break
            for raw_item in raw_items:
                parent_key = self._parent_item_key_from_raw(raw_item)
                if parent_key is None or parent_key in seen:
                    continue
                seen.add(parent_key)
                ref = self._build_search_result_ref(
                    item_key=parent_key,
                    raw_hit=raw_item,
                    local_cache_only=False,
                    item=None,
                    ordinal=len(upstream_refs),
                )
                upstream_refs.append(ref)
                refs_by_key[parent_key] = ref
            raw_start += len(raw_items)
            if len(raw_items) < page_size:
                break
        return upstream_refs, refs_by_key, seen

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
        has_fulltext: bool | None,
        has_ai_notes: bool | None,
        include_attachments: bool,
        include_notes: bool,
        sort: str,
        direction: str | None,
    ) -> AdvancedSearchResponse:
        self._validate_advanced_search_request(
            q=q,
            title=title,
            author=author,
            abstract=abstract,
            venue=venue,
            doi=doi,
            year_from=year_from,
            year_to=year_to,
            has_fulltext=has_fulltext,
            has_ai_notes=has_ai_notes,
        )
        normalized_fields = self._advanced_search_fields(fields)
        query = (q or "").strip()
        query_casefold = query.casefold()
        needs_notes = include_notes or has_ai_notes is not None or "note" in normalized_fields
        needs_attachments = (
            include_attachments
            or has_fulltext is not None
            or "fulltext" in normalized_fields
        )

        hint_map: dict[str, list[SearchHint]] = {}
        score_map: dict[str, float] = {}
        fulltext_match_keys: set[str] = set()
        local_search_fields = {
            field
            for field in normalized_fields
            if field in {"title", "creator", "abstract", "venue", "doi", "tag", "note", "fulltext"}
        }
        local_hits = await self._search_local_index_hits(
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
                if any(hint.field in {"fulltext", "local_cache_fulltext"} for hint in hints):
                    fulltext_match_keys.add(item_key_value)
            score_map[item_key_value] = self._coerce_optional_float(hit.get("score")) or 0.0
            candidate_keys.add(item_key_value)

        if query and "fulltext" in normalized_fields:
            fulltext_refs, _, _ = await self._collect_upstream_search_refs(
                q=query,
                include_fulltext=True,
                item_type=item_type,
                collection_key=collection_key,
                tag=tag,
                sort=None,
                direction=None,
            )
            for ref in fulltext_refs:
                item_key_value = str(ref.get("itemKey") or "").strip()
                raw_hit = ref.get("rawHit")
                if not item_key_value or not isinstance(raw_hit, dict):
                    continue
                raw_hints = [
                    hint
                    for hint in self._raw_hit_search_hints(
                        item_key=item_key_value,
                        raw_hit=raw_hit,
                        query=query,
                    )
                    if hint.field in {"fulltext", "local_cache_fulltext"}
                ]
                if not raw_hints:
                    continue
                hint_map[item_key_value] = self._dedupe_search_hints(
                    [*hint_map.get(item_key_value, []), *raw_hints]
                )
                score_map[item_key_value] = max(
                    score_map.get(item_key_value, 0.0),
                    self._score_search_hints(raw_hints),
                )
                candidate_keys.add(item_key_value)
                fulltext_match_keys.add(item_key_value)

        if query:
            if not candidate_keys:
                return AdvancedSearchResponse(
                    items=[],
                    count=0,
                    total=0,
                    start=start,
                    limit=limit,
                    nextStart=None,
                )
            raw_items = await self._zotero.get_items_by_keys_raw(sorted(candidate_keys))
        else:
            raw_items = await self._list_all_top_level_items_raw(
                item_type=item_type,
                collection_key=collection_key,
                tag=tag,
                sort="dateModified",
                direction="desc",
            )
        matches: list[SearchResultItem] = []
        for raw_item in raw_items:
            children: list[dict[str, Any]] = []
            if needs_attachments or needs_notes:
                children = await self._zotero.get_children(str(raw_item.get("key")))
            item = await self._normalize_parent_item(
                raw_item,
                children=children,
                include_attachments=needs_attachments,
                include_notes=needs_notes,
            )
            if not self._item_matches_filters(
                item=item,
                item_type=item_type,
                collection_key=collection_key,
                tag=tag,
            ):
                continue
            local_note_hints = (
                self._note_search_hints_from_children(children=children, query=query_casefold)
                if query and "note" in normalized_fields
                else []
            )
            if not self._advanced_item_matches(
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
                has_fulltext=has_fulltext,
                has_ai_notes=has_ai_notes,
                note_match=bool(local_note_hints),
                fulltext_match=item.itemKey in fulltext_match_keys,
            ):
                continue
            extra_hints = self._dedupe_search_hints(
                [*hint_map.get(item.itemKey, []), *local_note_hints]
            )
            matches.append(
                self._build_advanced_search_result_item(
                    item=item,
                    query=query_casefold,
                    fields=normalized_fields,
                    extra_hints=extra_hints,
                    score=max(
                        score_map.get(item.itemKey, 0.0),
                        self._score_search_hints(extra_hints),
                    ),
                )
            )

        matches = self._sort_advanced_search_results(
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
            nextStart=self._next_start(start=start, returned_count=len(page_items), total=total),
        )

    async def build_review_pack(self, payload: ReviewPackRequest) -> ReviewPackResponse:
        citation_style = payload.citationStyle or self._settings.default_citation_style
        citation_locale = payload.citationLocale or self._settings.default_citation_locale

        async def build_one(item_key: str) -> tuple[str, ReviewPackItem | None]:
            try:
                item = await self.get_parent_item(
                    item_key=item_key,
                    include_attachments=True,
                    include_notes=True,
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
                notes = (await self.list_item_notes(item_key)).notes
            related_items: list[SearchItem] = []
            if payload.includeRelated:
                related_items = (
                    await self.get_related_items(
                        item_key=item_key,
                        include_attachments=False,
                        include_notes=False,
                    )
                ).items
            fulltext_preview: FulltextPreviewItem | None = None
            if payload.includeFulltextPreview:
                preview_response = await self.batch_fulltext_preview(
                    item_keys=[item_key],
                    max_chars=payload.maxFulltextChars,
                    prefer_source="auto",
                )
                fulltext_preview = preview_response.items[0] if preview_response.items else None
            return (
                item_key,
                ReviewPackItem(
                    item=item,
                    citation=citation,
                    fulltextPreview=fulltext_preview,
                    notes=notes,
                    relatedItems=related_items,
                ),
            )

        results = await self._map_with_concurrency(
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
        )

    async def search_discovery(
        self,
        *,
        q: str,
        start: int,
        limit: int,
        year_from: int | None,
        year_to: int | None,
        oa_only: bool,
        resolve_in_library: bool,
        exclude_existing: bool,
        sort: str,
    ) -> DiscoverySearchResponse:
        page_size = 200
        page_number = (start // page_size) + 1
        local_start = start % page_size
        filters: list[str] = []
        if year_from is not None:
            filters.append(f"from_publication_date:{year_from}-01-01")
        if year_to is not None:
            filters.append(f"to_publication_date:{year_to}-12-31")
        if oa_only:
            filters.append("is_oa:true")
        params: dict[str, Any] = {
            "search": q,
            "per-page": page_size,
            "page": page_number,
            "select": ",".join(
                [
                    "id",
                    "doi",
                    "display_name",
                    "publication_year",
                    "publication_date",
                    "type",
                    "cited_by_count",
                    "authorships",
                    "primary_location",
                    "open_access",
                    "abstract_inverted_index",
                    "primary_topic",
                ]
            ),
        }
        if filters:
            params["filter"] = ",".join(filters)
        if sort == "cited_by":
            params["sort"] = "cited_by_count:desc"
        elif sort == "recent":
            params["sort"] = "publication_date:desc"
        if self._settings.openalex_api_key:
            params["api_key"] = self._settings.openalex_api_key
        doi_matches: dict[str, str] = {}
        title_matches: dict[str, str] = {}
        if resolve_in_library or exclude_existing:
            doi_matches, title_matches = await self._discovery_library_match_maps(
                require_ready=True
            )

        items: list[DiscoveryWork] = []
        total: int | None = None
        consumed_results = 0
        current_page = page_number
        current_local_start = local_start

        while len(items) < limit:
            payload = await self._openalex_get(
                "/works",
                params={**params, "page": current_page},
            )
            if total is None:
                meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
                total = self._coerce_optional_int(meta.get("count"))
            results = payload.get("results", []) if isinstance(payload, dict) else []
            if not isinstance(results, list):
                results = []
            if current_local_start >= len(results):
                if len(results) < page_size:
                    break
                current_page += 1
                current_local_start = 0
                continue

            for result in results[current_local_start:]:
                if not isinstance(result, dict):
                    continue
                consumed_results += 1
                item = self._normalize_openalex_work(result)
                matched_item_key: str | None = None
                match_strategy: str | None = None
                if resolve_in_library or exclude_existing:
                    matched_item_key, match_strategy = self._match_discovery_work_in_library(
                        item=item,
                        doi_matches=doi_matches,
                        title_matches=title_matches,
                    )
                    item = item.model_copy(
                        update={
                            "alreadyInLibrary": matched_item_key is not None,
                            "libraryItemKey": matched_item_key,
                            "libraryMatchStrategy": match_strategy,
                        }
                    )
                if exclude_existing and matched_item_key is not None:
                    continue
                items.append(item)
                if len(items) >= limit:
                    break

            if len(items) >= limit or len(results) < page_size:
                break
            current_page += 1
            current_local_start = 0

        resolved_total = total if total is not None else start + consumed_results
        return DiscoverySearchResponse(
            items=items,
            count=len(items),
            total=resolved_total,
            start=start,
            limit=limit,
            nextStart=self._next_start(
                start=start,
                returned_count=consumed_results,
                total=resolved_total,
            ),
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
        raw_items, total = await self._zotero.list_top_level_items_raw(
            start=start,
            limit=limit,
            item_type=item_type,
            collection_key=collection_key,
            tag=tag,
            sort=sort,
            direction=direction,
        )
        items = await self._map_with_concurrency(
            raw_items,
            lambda raw_item: self._normalize_parent_item(
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
            nextStart=self._next_start(start=start, returned_count=len(items), total=total),
        )

    async def batch_get_items(
        self,
        *,
        item_keys: list[str],
        include_attachments: bool,
        include_notes: bool,
    ) -> BatchItemResponse:
        valid_item_keys = [
            item_key for item_key in item_keys if self._is_probably_zotero_key(item_key)
        ]
        raw_items = await self._zotero.get_items_by_keys_raw(valid_item_keys)
        raw_by_key = {
            str(raw_item.get("key") or ""): raw_item
            for raw_item in raw_items
            if str(raw_item.get("key") or "")
        }
        items: list[SearchItem] = []
        not_found_keys: list[str] = []
        for item_key in item_keys:
            if not self._is_probably_zotero_key(item_key):
                not_found_keys.append(item_key)
                continue
            raw_item = raw_by_key.get(item_key)
            if raw_item is None:
                not_found_keys.append(item_key)
                continue
            data = raw_item.get("data", {})
            if data.get("itemType") in {"attachment", "note"} and data.get("parentItem"):
                item = await self.get_parent_item(
                    item_key=str(data["parentItem"]),
                    include_attachments=include_attachments,
                    include_notes=include_notes,
                )
            else:
                item = await self._normalize_parent_item(
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
        if doi:
            items = await self._resolve_items_by_doi(
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
            items = await self._resolve_items_by_title(
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

    async def list_collections(
        self,
        *,
        start: int,
        limit: int,
        top_level_only: bool,
    ) -> CollectionListResponse:
        all_collections = await self._list_all_collections_raw(
            top_level_only=top_level_only,
        )
        collections_by_key = {
            str(raw_collection.get("key") or ""): raw_collection
            for raw_collection in all_collections
            if str(raw_collection.get("key") or "")
        }
        total = len(all_collections)
        page_collections = all_collections[start : start + limit]
        collections = [
            self._normalize_collection(
                raw_collection,
                collections_by_key=collections_by_key,
            )
            for raw_collection in page_collections
        ]
        return CollectionListResponse(
            collections=collections,
            count=len(collections),
            total=total,
            start=start,
            limit=limit,
            nextStart=self._next_start(start=start, returned_count=len(collections), total=total),
        )

    async def list_tags(
        self,
        *,
        start: int,
        limit: int,
        q: str | None,
        top_level_only: bool,
        collection_key: str | None,
    ) -> TagListResponse:
        raw_tags, total = await self._zotero.list_tags_raw(
            start=start,
            limit=limit,
            q=q,
            top_level_only=top_level_only,
            collection_key=collection_key,
        )
        tags = [self._normalize_tag(raw_tag) for raw_tag in raw_tags]
        return TagListResponse(
            tags=tags,
            count=len(tags),
            total=total,
            start=start,
            limit=limit,
            nextStart=self._next_start(start=start, returned_count=len(tags), total=total),
        )

    async def get_library_stats(self) -> LibraryStatsResponse:
        raw_items = await self._list_all_top_level_items_raw(
            item_type=None,
            collection_key=None,
            tag=None,
            sort="dateModified",
            direction="desc",
        )
        items = await self._map_with_concurrency(
            raw_items,
            lambda raw_item: self._normalize_parent_item(
                raw_item,
                include_attachments=False,
                include_notes=False,
            ),
            limit=min(DEFAULT_BATCH_CONCURRENCY, len(raw_items)),
        )
        all_collections = await self._list_all_collections_raw(top_level_only=False)
        _, tag_total = await self._zotero.list_tags_raw(
            start=0,
            limit=1,
            q=None,
            top_level_only=True,
            collection_key=None,
        )
        _, last_modified_version = await self._zotero.list_top_level_item_versions(
            since_version=None
        )
        return LibraryStatsResponse(
            totalItems=len(items),
            itemTypeCounts=self._item_type_counts(items),
            collectionCount=len(all_collections),
            tagCount=tag_total,
            duplicateGroups=DuplicateStats(
                titleGroups=len(self._group_duplicate_items(items, field="title")),
                doiGroups=len(self._group_duplicate_items(items, field="doi")),
            ),
            lastModifiedVersion=last_modified_version,
            searchIndex=self._build_search_index_stats(),
        )

    async def list_item_changes(
        self,
        *,
        start: int,
        limit: int,
        since_version: int | None,
        since_timestamp: str | None,
        include_attachments: bool,
        include_notes: bool,
    ) -> ItemChangesResponse:
        if (since_version is None) == (since_timestamp is None):
            raise BridgeError(
                code="BAD_REQUEST",
                message="Provide exactly one of sinceVersion or sinceTimestamp",
                status_code=400,
            )

        _, latest_version = await self._zotero.list_top_level_item_versions(since_version=None)
        if since_version is not None:
            changed_versions, _ = await self._zotero.list_top_level_item_versions(
                since_version=since_version
            )
            ordered_keys = [
                key
                for key, _ in sorted(
                    changed_versions.items(),
                    key=lambda entry: (-entry[1], entry[0]),
                )
            ]
            page_keys = ordered_keys[start : start + limit]
            raw_items = await self._zotero.get_items_by_keys_raw(page_keys)
            raw_by_key = {
                str(raw_item.get("key") or ""): raw_item
                for raw_item in raw_items
                if str(raw_item.get("key") or "")
            }
            items = [
                await self._normalize_parent_item(
                    raw_by_key[item_key],
                    include_attachments=include_attachments,
                    include_notes=include_notes,
                )
                for item_key in page_keys
                if item_key in raw_by_key
            ]
            deleted_item_keys = await self._zotero.get_deleted_item_keys(
                since_version=since_version
            )
            total = len(ordered_keys)
            return ItemChangesResponse(
                items=items,
                count=len(items),
                total=total,
                start=start,
                limit=limit,
                nextStart=self._next_start(
                    start=start,
                    returned_count=len(items),
                    total=total,
                ),
                deletedItemKeys=deleted_item_keys,
                deletedCount=len(deleted_item_keys),
                sinceVersion=since_version,
                latestVersion=latest_version,
            )

        assert since_timestamp is not None
        cutoff = self._parse_since_timestamp(since_timestamp)
        raw_items = await self._list_all_top_level_items_raw(
            item_type=None,
            collection_key=None,
            tag=None,
            sort="dateModified",
            direction="desc",
        )
        filtered_raw_items = [
            raw_item
            for raw_item in raw_items
            if self._item_modified_since(raw_item, cutoff=cutoff)
        ]
        page_items = filtered_raw_items[start : start + limit]
        items = [
            await self._normalize_parent_item(
                raw_item,
                include_attachments=include_attachments,
                include_notes=include_notes,
            )
            for raw_item in page_items
        ]
        total = len(filtered_raw_items)
        return ItemChangesResponse(
            items=items,
            count=len(items),
            total=total,
            start=start,
            limit=limit,
            nextStart=self._next_start(start=start, returned_count=len(items), total=total),
            sinceTimestamp=since_timestamp,
            latestVersion=latest_version,
        )

    async def add_item_tags(
        self,
        *,
        item_key: str,
        tags: list[str],
    ) -> ItemTagsResponse:
        normalized_tags = self._normalize_input_strings(tags)
        if not normalized_tags:
            raise BridgeError(
                code="BAD_REQUEST",
                message="At least one tag is required",
                status_code=400,
            )
        raw_item = await self._zotero.get_item(item_key)
        existing_tags = self._normalize_tags(raw_item.get("data", {}).get("tags"))
        merged_tags = self._merge_tags(existing_tags, normalized_tags)
        added_tags = [tag for tag in merged_tags if tag not in existing_tags]
        if added_tags:
            await self._zotero.update_item(
                item_key=item_key,
                version=int(raw_item.get("version", 0)),
                data={"tags": [{"tag": tag} for tag in merged_tags]},
            )
            raw_item = await self._zotero.get_item(item_key)
            existing_tags = self._normalize_tags(raw_item.get("data", {}).get("tags"))
            await self._refresh_local_search_index_item(item_key)
        return ItemTagsResponse(itemKey=item_key, tags=existing_tags, addedTags=added_tags)

    async def remove_item_tag(
        self,
        *,
        item_key: str,
        tag: str,
    ) -> ItemTagsResponse:
        normalized_tag = tag.strip()
        if not normalized_tag:
            raise BridgeError(
                code="BAD_REQUEST",
                message="tag is required",
                status_code=400,
            )
        raw_item = await self._zotero.get_item(item_key)
        existing_tags = self._normalize_tags(raw_item.get("data", {}).get("tags"))
        remaining_tags = [value for value in existing_tags if value != normalized_tag]
        removed_tag = normalized_tag if len(remaining_tags) != len(existing_tags) else None
        if removed_tag is not None:
            await self._zotero.update_item(
                item_key=item_key,
                version=int(raw_item.get("version", 0)),
                data={"tags": [{"tag": value} for value in remaining_tags]},
            )
            raw_item = await self._zotero.get_item(item_key)
            remaining_tags = self._normalize_tags(raw_item.get("data", {}).get("tags"))
            await self._refresh_local_search_index_item(item_key)
        return ItemTagsResponse(
            itemKey=item_key,
            tags=remaining_tags,
            removedTag=removed_tag,
        )

    async def add_item_to_collections(
        self,
        *,
        item_key: str,
        collection_keys: list[str],
    ) -> ItemCollectionsResponse:
        normalized_collection_keys = self._normalize_input_strings(collection_keys)
        if not normalized_collection_keys:
            raise BridgeError(
                code="BAD_REQUEST",
                message="At least one collectionKey is required",
                status_code=400,
            )
        available_collections = {
            str(raw_collection.get("key") or "")
            for raw_collection in await self._list_all_collections_raw(top_level_only=False)
            if str(raw_collection.get("key") or "")
        }
        missing_collections = [
            collection_key
            for collection_key in normalized_collection_keys
            if collection_key not in available_collections
        ]
        if missing_collections:
            raise BridgeError(
                code="BAD_REQUEST",
                message=f"Unknown collection keys: {', '.join(missing_collections)}",
                status_code=400,
            )
        raw_item = await self._zotero.get_item(item_key)
        data = raw_item.get("data", {})
        if data.get("parentItem"):
            raise BridgeError(
                code="BAD_REQUEST",
                message="Only top-level items can be added to collections",
                status_code=400,
            )
        existing_collections = [
            str(value)
            for value in data.get("collections", [])
            if isinstance(value, str) and value
        ]
        merged_collections = self._merge_strings(
            existing_collections,
            normalized_collection_keys,
        )
        added_collection_keys = [
            value for value in merged_collections if value not in existing_collections
        ]
        if added_collection_keys:
            await self._zotero.update_item(
                item_key=item_key,
                version=int(raw_item.get("version", 0)),
                data={"collections": merged_collections},
            )
            raw_item = await self._zotero.get_item(item_key)
            existing_collections = [
                str(value)
                for value in raw_item.get("data", {}).get("collections", [])
                if isinstance(value, str) and value
            ]
            await self._refresh_local_search_index_item(item_key)
        return ItemCollectionsResponse(
            itemKey=item_key,
            collectionKeys=existing_collections,
            addedCollectionKeys=added_collection_keys,
        )

    async def batch_fulltext_preview(
        self,
        *,
        item_keys: list[str],
        max_chars: int,
        prefer_source: str,
    ) -> BatchFulltextPreviewResponse:
        if max_chars > self._settings.fulltext_max_chars_hard_limit:
            raise BridgeError(
                code="BAD_REQUEST",
                message="maxChars exceeds configured hard limit",
                status_code=400,
            )

        async def preview_one(item_key: str) -> FulltextPreviewItem:
            try:
                fulltext = await self.get_item_fulltext(
                    item_key=item_key,
                    attachment_key=None,
                    cursor=0,
                    max_chars=max_chars,
                    prefer_source=prefer_source,
                )
            except BridgeError as exc:
                return FulltextPreviewItem(
                    itemKey=item_key,
                    errorCode=exc.code,
                    errorMessage=exc.message,
                )
            return FulltextPreviewItem(
                itemKey=item_key,
                attachmentKey=fulltext.attachmentKey,
                content=fulltext.content,
                source=fulltext.source,
                nextCursor=fulltext.nextCursor,
                attachmentCandidates=fulltext.attachmentCandidates,
            )
        previews = await self._map_with_concurrency(
            item_keys,
            preview_one,
            limit=min(DEFAULT_BATCH_CONCURRENCY, len(item_keys)),
        )
        return BatchFulltextPreviewResponse(items=previews, count=len(previews))

    async def get_related_items(
        self,
        *,
        item_key: str,
        include_attachments: bool,
        include_notes: bool,
    ) -> RelatedItemsResponse:
        raw_item = await self._zotero.get_item(item_key)
        related_item_keys = self._related_item_keys(raw_item)
        if not related_item_keys:
            return RelatedItemsResponse(itemKey=item_key, items=[], count=0)
        raw_related_items = await self._zotero.get_items_by_keys_raw(related_item_keys)
        raw_by_key = {
            str(raw_related_item.get("key") or ""): raw_related_item
            for raw_related_item in raw_related_items
            if str(raw_related_item.get("key") or "")
        }
        items = [
            await self._normalize_parent_item(
                raw_by_key[related_item_key],
                include_attachments=include_attachments,
                include_notes=include_notes,
            )
            for related_item_key in related_item_keys
            if related_item_key in raw_by_key
        ]
        return RelatedItemsResponse(itemKey=item_key, items=items, count=len(items))

    async def merge_duplicate_items(
        self,
        *,
        primary_item_key: str,
        duplicate_item_keys: list[str],
        dry_run: bool,
        move_attachments: bool,
        move_notes: bool,
        merge_tags: bool,
        merge_collections: bool,
    ) -> MergeDuplicateItemsResponse:
        duplicate_keys = [
            item_key
            for item_key in self._normalize_input_strings(duplicate_item_keys)
            if item_key != primary_item_key
        ]
        if not duplicate_keys:
            raise BridgeError(
                code="BAD_REQUEST",
                message="At least one duplicateItemKey distinct from primaryItemKey is required",
                status_code=400,
            )

        primary_raw = await self._zotero.get_item(primary_item_key)
        self._assert_duplicate_merge_candidate(primary_raw, item_key=primary_item_key)
        duplicate_raw_items = await self._zotero.get_items_by_keys_raw(duplicate_keys)
        duplicate_raw_by_key = {
            str(raw_item.get("key") or ""): raw_item
            for raw_item in duplicate_raw_items
            if str(raw_item.get("key") or "")
        }
        missing_keys = [
            item_key for item_key in duplicate_keys if item_key not in duplicate_raw_by_key
        ]
        if missing_keys:
            raise BridgeError(
                code="BAD_REQUEST",
                message=f"Unknown duplicate item keys: {', '.join(missing_keys)}",
                status_code=400,
            )

        primary_item_type = str(primary_raw.get("data", {}).get("itemType") or "")
        ordered_duplicates = [duplicate_raw_by_key[item_key] for item_key in duplicate_keys]
        for duplicate_raw in ordered_duplicates:
            self._assert_duplicate_merge_candidate(
                duplicate_raw,
                item_key=str(duplicate_raw.get("key") or ""),
            )
            duplicate_item_type = str(duplicate_raw.get("data", {}).get("itemType") or "")
            if duplicate_item_type != primary_item_type:
                raise BridgeError(
                    code="BAD_REQUEST",
                    message="All duplicate items must share the same itemType as the primary item",
                    status_code=400,
                )

        primary_data = primary_raw.get("data", {})
        primary_tags = self._normalize_tags(primary_data.get("tags"))
        primary_collections = [
            str(value)
            for value in primary_data.get("collections", [])
            if isinstance(value, str) and value
        ]
        merged_tags = primary_tags[:]
        merged_collections = primary_collections[:]
        for duplicate_raw in ordered_duplicates:
            duplicate_data = duplicate_raw.get("data", {})
            if merge_tags:
                merged_tags = self._merge_tags(
                    merged_tags,
                    self._normalize_tags(duplicate_data.get("tags")),
                )
            if merge_collections:
                merged_collections = self._merge_strings(
                    merged_collections,
                    [
                        str(value)
                        for value in duplicate_data.get("collections", [])
                        if isinstance(value, str) and value
                    ],
                )

        added_tags = [tag for tag in merged_tags if tag not in primary_tags]
        added_collection_keys = [
            value for value in merged_collections if value not in primary_collections
        ]

        child_updates: list[dict[str, Any]] = []
        for duplicate_raw in ordered_duplicates:
            duplicate_key = str(duplicate_raw.get("key") or "")
            children = await self._zotero.get_children(duplicate_key)
            for child in children:
                child_type = str(child.get("data", {}).get("itemType") or "")
                if child_type == "attachment" and move_attachments:
                    child_updates.append(child)
                if child_type == "note" and move_notes:
                    child_updates.append(child)

        moved_attachment_keys = [
            str(child.get("key") or "")
            for child in child_updates
            if child.get("data", {}).get("itemType") == "attachment"
        ]
        moved_note_keys = [
            str(child.get("key") or "")
            for child in child_updates
            if child.get("data", {}).get("itemType") == "note"
        ]

        primary_item = await self._normalize_parent_item(
            primary_raw,
            include_attachments=False,
            include_notes=False,
        )
        if dry_run:
            primary_item = primary_item.model_copy(
                update={"tags": merged_tags, "collectionKeys": merged_collections}
            )
            return MergeDuplicateItemsResponse(
                status=MergeDuplicateItemsStatus.DRY_RUN,
                primaryItem=primary_item,
                duplicateItemKeys=duplicate_keys,
                movedAttachmentKeys=moved_attachment_keys,
                movedNoteKeys=moved_note_keys,
                addedTags=added_tags,
                addedCollectionKeys=added_collection_keys,
            )

        update_payload: dict[str, Any] = {}
        if added_tags:
            update_payload["tags"] = [{"tag": tag} for tag in merged_tags]
        if added_collection_keys:
            update_payload["collections"] = merged_collections
        if update_payload:
            await self._zotero.update_item(
                item_key=primary_item_key,
                version=int(primary_raw.get("version", 0)),
                data=update_payload,
            )

        for child in child_updates:
            await self._zotero.update_item(
                item_key=str(child.get("key") or ""),
                version=int(child.get("version", 0)),
                data={"parentItem": primary_item_key},
            )

        deleted_item_keys: list[str] = []
        for duplicate_raw in ordered_duplicates:
            duplicate_key = str(duplicate_raw.get("key") or "")
            await self._zotero.delete_item(
                item_key=duplicate_key,
                version=int(duplicate_raw.get("version", 0)),
            )
            deleted_item_keys.append(duplicate_key)
            self._prune_cached_fulltext_records_for_item(duplicate_key)
            if self._local_search_index is not None:
                self._local_search_index.delete_record(duplicate_key)

        await self._refresh_local_search_index_item(primary_item_key)

        primary_item = await self.get_parent_item(
            item_key=primary_item_key,
            include_attachments=False,
            include_notes=False,
        )
        return MergeDuplicateItemsResponse(
            status=MergeDuplicateItemsStatus.MERGED,
            primaryItem=primary_item,
            duplicateItemKeys=duplicate_keys,
            movedAttachmentKeys=moved_attachment_keys,
            movedNoteKeys=moved_note_keys,
            addedTags=added_tags,
            addedCollectionKeys=added_collection_keys,
            deletedItemKeys=deleted_item_keys,
        )

    async def find_duplicate_items(
        self,
        *,
        start: int,
        limit: int,
        by: str,
        item_type: str | None,
        collection_key: str | None,
        tag: str | None,
        include_attachments: bool,
        include_notes: bool,
    ) -> DuplicateGroupsResponse:
        fields = self._duplicate_fields(by)
        raw_items = await self._list_all_top_level_items_raw(
            item_type=item_type,
            collection_key=collection_key,
            tag=tag,
            sort="title",
            direction="asc",
        )
        items = await self._map_with_concurrency(
            raw_items,
            lambda raw_item: self._normalize_parent_item(
                raw_item,
                include_attachments=include_attachments,
                include_notes=include_notes,
            ),
            limit=min(DEFAULT_BATCH_CONCURRENCY, len(raw_items)),
        )
        groups: list[DuplicateGroup] = []
        for field in fields:
            grouped = self._group_duplicate_items(items, field=field)
            groups.extend(grouped)
        groups.sort(key=lambda group: (-group.count, group.field, group.value.casefold()))
        page_groups = groups[start : start + limit]
        total = len(groups)
        return DuplicateGroupsResponse(
            groups=page_groups,
            count=len(page_groups),
            total=total,
            start=start,
            limit=limit,
            nextStart=self._next_start(start=start, returned_count=len(page_groups), total=total),
        )

    async def get_parent_item(
        self,
        *,
        item_key: str,
        include_attachments: bool,
        include_notes: bool,
    ) -> SearchItem:
        raw_item = await self._zotero.get_item(item_key)
        return await self._normalize_parent_item(
            raw_item,
            include_attachments=include_attachments,
            include_notes=include_notes,
        )

    async def get_item_detail(self, item_key: str) -> ItemDetailResponse:
        item = await self.get_parent_item(
            item_key=item_key,
            include_attachments=True,
            include_notes=True,
        )
        return ItemDetailResponse(item=item)

    async def list_item_notes(self, item_key: str) -> ItemNotesResponse:
        await self._zotero.get_item(item_key)
        children = await self._zotero.get_children(item_key)
        notes = self._normalize_note_records(children)
        return ItemNotesResponse(itemKey=item_key, notes=notes, count=len(notes))

    async def create_item_note(
        self,
        *,
        item_key: str,
        payload: NoteWriteRequest,
    ) -> NoteWriteResponse:
        self._validate_note_body(payload.bodyMarkdown)
        await self._zotero.get_item(item_key)
        rendered_html = self._note_renderer.render_user_note(
            title=payload.title,
            body_markdown=payload.bodyMarkdown,
            mode=payload.mode.value,
        )
        payload_fingerprint = self._note_write_payload_fingerprint(
            operation="create",
            payload=payload,
        )
        if payload.requestId:
            existing_note = await self._find_note_by_request_id(
                item_key=item_key,
                request_id=payload.requestId,
            )
            if existing_note is not None:
                self._assert_create_replay_matches(
                    existing_note=existing_note,
                    request_id=payload.requestId,
                    payload_fingerprint=payload_fingerprint,
                    rendered_html=rendered_html,
                    user_tags=payload.tags or [],
                )
                return NoteWriteResponse(
                    status=NoteWriteStatus.CREATED,
                    noteKey=str(existing_note.get("key") or ""),
                    itemKey=item_key,
                )
        note_tags = self._merge_tags(
            self._request_metadata_tags(
                request_id=payload.requestId,
                payload_fingerprint=payload_fingerprint,
            ),
            payload.tags or [],
        )
        note_payload = {
            "itemType": "note",
            "parentItem": item_key,
            "note": rendered_html,
            "tags": [{"tag": tag} for tag in note_tags],
        }
        try:
            note_key = (
                await self._zotero.create_items(
                    [note_payload],
                    write_token=self._build_write_token(payload.requestId),
                )
            )[0]
        except BridgeError as exc:
            if exc.code == "WRITE_CONFLICT" and payload.requestId:
                existing_note = await self._find_note_by_request_id(
                    item_key=item_key,
                    request_id=payload.requestId,
                )
                if existing_note is not None:
                    self._assert_create_replay_matches(
                        existing_note=existing_note,
                        request_id=payload.requestId,
                        payload_fingerprint=payload_fingerprint,
                        rendered_html=rendered_html,
                        user_tags=payload.tags or [],
                    )
                    return NoteWriteResponse(
                        status=NoteWriteStatus.CREATED,
                        noteKey=str(existing_note.get("key") or ""),
                        itemKey=item_key,
                    )
            raise
        await self._refresh_local_search_index_item(item_key)
        return NoteWriteResponse(
            status=NoteWriteStatus.CREATED,
            noteKey=note_key,
            itemKey=item_key,
        )

    async def get_note_detail(self, note_key: str) -> NoteDetailResponse:
        raw_note = await self._get_note_item(note_key)
        return NoteDetailResponse(note=self._normalize_note_record(raw_note))

    async def update_note(
        self,
        *,
        note_key: str,
        payload: NoteWriteRequest,
    ) -> NoteWriteResponse:
        self._validate_note_body(payload.bodyMarkdown)
        payload_fingerprint = self._note_write_payload_fingerprint(
            operation="update",
            payload=payload,
        )
        for _ in range(NOTE_UPDATE_MAX_ATTEMPTS):
            raw_note = await self._get_note_item(note_key)
            data = raw_note.get("data", {})
            existing_tags = self._normalize_tags(data.get("tags"))
            if payload.requestId:
                replay_state = self._request_replay_state(
                    tags=existing_tags,
                    request_id=payload.requestId,
                    payload_fingerprint=payload_fingerprint,
                )
                if replay_state == "matched":
                    return NoteWriteResponse(
                        status=NoteWriteStatus.UPDATED,
                        noteKey=note_key,
                        itemKey=self._clean_optional_str(data.get("parentItem")),
                    )
                if replay_state == "conflict":
                    self._raise_request_id_conflict()
            rendered_html = self._note_renderer.render_user_note(
                title=payload.title,
                body_markdown=payload.bodyMarkdown,
                mode=payload.mode.value,
                existing_html=str(data.get("note") or ""),
            )
            user_tags = payload.tags
            if user_tags is None:
                user_tags = self._mutable_note_tags(existing_tags)
            tags = self._merge_tags(
                self._identity_note_tags(existing_tags),
                self._request_metadata_tags(
                    request_id=payload.requestId,
                    payload_fingerprint=payload_fingerprint,
                ),
            )
            tags = self._merge_tags(tags, user_tags)
            try:
                await self._zotero.update_item(
                    item_key=note_key,
                    version=int(raw_note.get("version", 0)),
                    data={
                        "note": rendered_html,
                        "tags": [{"tag": tag} for tag in tags],
                    },
                )
            except BridgeError as exc:
                if exc.code == "WRITE_CONFLICT":
                    continue
                raise
            parent_item_key = self._clean_optional_str(data.get("parentItem"))
            if parent_item_key:
                await self._refresh_local_search_index_item(parent_item_key)
            return NoteWriteResponse(
                status=NoteWriteStatus.UPDATED,
                noteKey=note_key,
                itemKey=parent_item_key,
            )
        self._raise_item_update_conflict()

    async def delete_note(self, *, note_key: str) -> NoteDeleteResponse:
        raw_note = await self._get_note_item(note_key)
        data = raw_note.get("data", {})
        await self._zotero.delete_item(
            item_key=note_key,
            version=int(raw_note.get("version", 0)),
        )
        parent_item_key = self._clean_optional_str(data.get("parentItem"))
        if parent_item_key:
            await self._refresh_local_search_index_item(parent_item_key)
        return NoteDeleteResponse(
            status=NoteDeleteStatus.DELETED,
            noteKey=note_key,
            itemKey=parent_item_key,
        )

    async def get_item_fulltext(
        self,
        *,
        item_key: str,
        attachment_key: str | None,
        cursor: int,
        max_chars: int,
        prefer_source: str,
    ) -> FulltextResponse:
        if prefer_source == "cache" and self._local_fulltext_store is None:
            raise BridgeError(
                code="FULLTEXT_NOT_AVAILABLE",
                message="Local cache source is not enabled",
                status_code=404,
            )
        item = await self._zotero.get_item(item_key)
        item_data = item.get("data", {})
        fulltext_candidates = (
            [item]
            if item_data.get("itemType") == "attachment"
            else await self._zotero.get_children(item_key)
        )
        selection = self._fulltext.select_attachment(fulltext_candidates, attachment_key)
        attachment_key_resolved = str(selection.attachment.get("key"))
        if prefer_source == "cache":
            return self._build_cached_fulltext_response(
                item_key=item_key,
                attachment_key=attachment_key_resolved,
                cursor=cursor,
                max_chars=max_chars,
                candidate_keys=selection.candidate_keys,
            )

        web_error: BridgeError | None = None
        try:
            payload = await self._zotero.get_fulltext(attachment_key_resolved)
        except BridgeError as exc:
            if exc.code != "FULLTEXT_NOT_AVAILABLE" or prefer_source == "web":
                raise
            web_error = exc
        else:
            return self._fulltext.build_chunk_response(
                item_key=item_key,
                attachment_key=attachment_key_resolved,
                fulltext_payload=payload,
                cursor=cursor,
                max_chars=max_chars,
                candidate_keys=selection.candidate_keys,
            )

        cached_payload = self._get_cached_fulltext_payload(attachment_key_resolved)
        if cached_payload is not None:
            return self._fulltext.build_chunk_response(
                item_key=item_key,
                attachment_key=attachment_key_resolved,
                fulltext_payload=cached_payload,
                cursor=cursor,
                max_chars=max_chars,
                candidate_keys=selection.candidate_keys,
                source=self._fulltext.local_cache_source,
            )
        if web_error is not None:
            raise web_error
        raise BridgeError(
            code="FULLTEXT_NOT_AVAILABLE",
            message="Full text is not available for this attachment",
            status_code=404,
        )

    async def upsert_ai_note(
        self,
        *,
        item_key: str,
        payload: UpsertAINoteRequest,
    ) -> UpsertAINoteResponse:
        self._validate_note_body(payload.bodyMarkdown)

        await self._zotero.get_item(item_key)
        identity_tags = self._note_renderer.identity_tags(
            agent=payload.agent,
            note_type=payload.noteType,
            slot=payload.slot,
        )
        payload_fingerprint = self._ai_note_payload_fingerprint(payload)
        for _ in range(NOTE_UPDATE_MAX_ATTEMPTS):
            children = await self._zotero.get_children(item_key)
            existing_note = self._find_matching_note(children, identity_tags)
            replayed_note = self._find_note_in_children_by_request_id(
                children=children,
                request_id=payload.requestId,
            )
            if existing_note is None and replayed_note is not None and payload.requestId:
                replay_tags = self._assert_ai_note_replay_matches(
                    existing_note=replayed_note,
                    request_id=payload.requestId,
                    payload_fingerprint=payload_fingerprint,
                    identity_tags=identity_tags,
                )
                return UpsertAINoteResponse(
                    status=self._upsert_ai_note_replay_status(
                        tags=replay_tags,
                        request_id=payload.requestId,
                    ),
                    noteKey=str(replayed_note.get("key") or ""),
                    itemKey=item_key,
                    agent=payload.agent,
                    noteType=payload.noteType,
                    slot=payload.slot,
                )

            existing_html = None
            if existing_note is not None:
                existing_tags = self._normalize_tags(existing_note.get("data", {}).get("tags"))
                if payload.requestId:
                    if (
                        replayed_note is not None
                        and replayed_note.get("key") != existing_note.get("key")
                    ):
                        self._raise_request_id_conflict()
                    replay_state = self._request_replay_state(
                        tags=existing_tags,
                        request_id=payload.requestId,
                        payload_fingerprint=payload_fingerprint,
                    )
                    if replay_state == "matched":
                        return UpsertAINoteResponse(
                            status=self._upsert_ai_note_replay_status(
                                tags=existing_tags,
                                request_id=payload.requestId,
                            ),
                            noteKey=str(existing_note.get("key") or ""),
                            itemKey=item_key,
                            agent=payload.agent,
                            noteType=payload.noteType,
                            slot=payload.slot,
                        )
                    if replay_state == "conflict":
                        self._raise_request_id_conflict()
                existing_html = str(existing_note.get("data", {}).get("note") or "")

            rendered_html = self._note_renderer.render(
                title=payload.title,
                body_markdown=payload.bodyMarkdown,
                agent=payload.agent,
                note_type=payload.noteType,
                model=payload.model,
                source_attachment_key=payload.sourceAttachmentKey,
                source_cursor_start=payload.sourceCursorStart,
                source_cursor_end=payload.sourceCursorEnd,
                mode=payload.mode.value,
                existing_html=existing_html,
            )
            request_tags = self._request_metadata_tags(
                request_id=payload.requestId,
                payload_fingerprint=payload_fingerprint,
                outcome=(
                    UpsertAINoteStatus.CREATED.value
                    if existing_note is None
                    else UpsertAINoteStatus.UPDATED.value
                ),
            )
            all_tags = self._merge_tags(identity_tags, request_tags)
            all_tags = self._merge_tags(all_tags, payload.tags)

            if existing_note is None:
                note_payload = {
                    "itemType": "note",
                    "parentItem": item_key,
                    "note": rendered_html,
                    "tags": [{"tag": tag} for tag in all_tags],
                }
                try:
                    note_key = (
                        await self._zotero.create_items(
                            [note_payload],
                            write_token=self._build_write_token(payload.requestId),
                        )
                    )[0]
                except BridgeError as exc:
                    if exc.code == "WRITE_CONFLICT" and payload.requestId:
                        replayed_note = await self._find_note_by_request_id(
                            item_key=item_key,
                            request_id=payload.requestId,
                        )
                        if replayed_note is not None:
                            replay_tags = self._assert_ai_note_replay_matches(
                                existing_note=replayed_note,
                                request_id=payload.requestId,
                                payload_fingerprint=payload_fingerprint,
                                identity_tags=identity_tags,
                            )
                            return UpsertAINoteResponse(
                                status=self._upsert_ai_note_replay_status(
                                    tags=replay_tags,
                                    request_id=payload.requestId,
                                ),
                                noteKey=str(replayed_note.get("key") or ""),
                                itemKey=item_key,
                                agent=payload.agent,
                                noteType=payload.noteType,
                                slot=payload.slot,
                            )
                    raise
                await self._refresh_local_search_index_item(item_key)
                return UpsertAINoteResponse(
                    status=UpsertAINoteStatus.CREATED,
                    noteKey=note_key,
                    itemKey=item_key,
                    agent=payload.agent,
                    noteType=payload.noteType,
                    slot=payload.slot,
                )

            note_key = str(existing_note.get("key"))
            try:
                await self._zotero.update_item(
                    item_key=note_key,
                    version=int(existing_note.get("version", 0)),
                    data={
                        "note": rendered_html,
                        "tags": [{"tag": tag} for tag in all_tags],
                    },
                )
            except BridgeError as exc:
                if exc.code == "WRITE_CONFLICT":
                    continue
                raise
            await self._refresh_local_search_index_item(item_key)
            return UpsertAINoteResponse(
                status=UpsertAINoteStatus.UPDATED,
                noteKey=note_key,
                itemKey=item_key,
                agent=payload.agent,
                noteType=payload.noteType,
                slot=payload.slot,
            )
        self._raise_item_update_conflict()


    async def get_item_citation(
        self,
        *,
        item_key: str,
        style: str,
        locale: str,
        linkwrap: bool,
    ) -> CitationResponse:
        payload = await self._zotero.get_citation(
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

    async def _resolve_items_by_doi(
        self,
        *,
        doi: str,
        item_type: str | None,
        collection_key: str | None,
        tag: str | None,
        include_attachments: bool,
        include_notes: bool,
        limit: int,
    ) -> list[SearchItem]:
        normalized_doi = self._doi_resolver.normalize_doi(doi)
        page_size = 100
        raw_start = 0
        resolved: list[SearchItem] = []
        seen: set[str] = set()
        while True:
            raw_items, _ = await self._zotero.search_items_page_raw(
                q=normalized_doi,
                start=raw_start,
                limit=page_size,
                include_fulltext=True,
                item_type=item_type,
                tag=tag,
                collection_key=collection_key,
                sort=None,
                direction=None,
            )
            if not raw_items:
                break
            for raw_item in raw_items:
                parent_key = self._parent_item_key_from_raw(raw_item)
                if parent_key is None or parent_key in seen:
                    continue
                seen.add(parent_key)
                item = await self.get_parent_item(
                    item_key=parent_key,
                    include_attachments=include_attachments,
                    include_notes=include_notes,
                )
                if not self._item_matches_filters(
                    item=item,
                    item_type=item_type,
                    collection_key=collection_key,
                    tag=tag,
                ):
                    continue
                item_doi = item.DOI
                if not self._doi_matches(item_doi, normalized_doi):
                    continue
                resolved.append(item)
                if len(resolved) >= limit:
                    return resolved
            raw_start += len(raw_items)
            if len(raw_items) < page_size:
                break

        if resolved:
            return resolved

        fallback_items = await self._list_all_top_level_items_raw(
            item_type=item_type,
            collection_key=collection_key,
            tag=tag,
            sort="dateModified",
            direction="desc",
        )
        for raw_item in fallback_items:
            item_key = str(raw_item.get("key") or "")
            if not item_key or item_key in seen:
                continue
            data = raw_item.get("data", {})
            if not self._doi_matches(data.get("DOI"), normalized_doi):
                continue
            resolved.append(
                await self._normalize_parent_item(
                    raw_item,
                    include_attachments=include_attachments,
                    include_notes=include_notes,
                )
            )
            if len(resolved) >= limit:
                break
        return resolved

    async def _resolve_items_by_title(
        self,
        *,
        title: str,
        item_type: str | None,
        collection_key: str | None,
        tag: str | None,
        include_attachments: bool,
        include_notes: bool,
        limit: int,
    ) -> list[SearchItem]:
        normalized_title = self._normalize_title_key(title)
        resolved: list[SearchItem] = []
        candidates = await self._list_all_top_level_items_raw(
            item_type=item_type,
            collection_key=collection_key,
            tag=tag,
            sort="title",
            direction="asc",
        )
        for raw_item in candidates:
            data = raw_item.get("data", {})
            if self._normalize_title_key(str(data.get("title") or "")) != normalized_title:
                continue
            item = await self._normalize_parent_item(
                raw_item,
                include_attachments=include_attachments,
                include_notes=include_notes,
            )
            resolved.append(item)
            if len(resolved) >= limit:
                break
        return resolved

    async def _list_all_top_level_items_raw(
        self,
        *,
        item_type: str | None,
        collection_key: str | None,
        tag: str | None,
        sort: str,
        direction: str,
    ) -> list[dict[str, Any]]:
        raw_items: list[dict[str, Any]] = []
        start = 0
        page_size = 100
        while True:
            page, _ = await self._zotero.list_top_level_items_raw(
                start=start,
                limit=page_size,
                item_type=item_type,
                collection_key=collection_key,
                tag=tag,
                sort=sort,
                direction=direction,
            )
            if not page:
                break
            raw_items.extend(page)
            start += len(page)
            if len(page) < page_size:
                break
        return raw_items

    async def _list_all_collections_raw(
        self,
        *,
        top_level_only: bool,
    ) -> list[dict[str, Any]]:
        raw_collections: list[dict[str, Any]] = []
        start = 0
        page_size = 100
        while True:
            page, _ = await self._zotero.list_collections_raw(
                start=start,
                limit=page_size,
                top_level_only=top_level_only,
            )
            if not page:
                break
            raw_collections.extend(page)
            start += len(page)
            if len(page) < page_size:
                break
        return raw_collections

    def _build_search_result_item(
        self,
        *,
        item: SearchItem,
        query: str,
        raw_hit: dict[str, Any] | None,
        local_cache_only: bool,
        extra_hints: list[SearchHint] | None = None,
        score: float | None = None,
    ) -> SearchResultItem:
        hints = self._build_search_hints(
            item=item,
            query=query,
            raw_hit=raw_hit,
            local_cache_only=local_cache_only,
            extra_hints=extra_hints or [],
        )
        return SearchResultItem(
            **item.model_dump(),
            searchHints=hints,
            score=round(score if score is not None else self._score_search_hints(hints), 3),
        )

    @staticmethod
    def _build_search_result_ref(
        *,
        item_key: str,
        raw_hit: dict[str, Any] | None,
        local_cache_only: bool,
        item: SearchItem | None,
        ordinal: int,
        local_hints: list[SearchHint] | None = None,
        score: float | None = None,
    ) -> dict[str, Any]:
        return {
            "itemKey": item_key,
            "rawHit": raw_hit,
            "localCacheOnly": local_cache_only,
            "item": item,
            "ordinal": ordinal,
            "localHints": list(local_hints or []),
            "score": score,
        }

    async def _collect_local_cache_search_refs(
        self,
        *,
        q: str,
        include_fulltext: bool,
        seen: set[str],
        item_type: str | None,
        collection_key: str | None,
        tag: str | None,
        starting_ordinal: int,
    ) -> list[dict[str, Any]]:
        if not include_fulltext:
            return []

        refs: list[dict[str, Any]] = []
        ordinal = starting_ordinal
        for item_key in self._search_local_fulltext_item_keys(q, limit=None):
            if item_key in seen:
                continue
            try:
                item = await self.get_parent_item(
                    item_key=item_key,
                    include_attachments=False,
                    include_notes=False,
                )
            except BridgeError as exc:
                if exc.code != "ITEM_NOT_FOUND":
                    raise
                self._prune_cached_fulltext_records_for_item(item_key)
                continue
            if not self._item_matches_filters(
                item=item,
                item_type=item_type,
                collection_key=collection_key,
                tag=tag,
            ):
                continue
            seen.add(item_key)
            refs.append(
                self._build_search_result_ref(
                    item_key=item_key,
                    raw_hit=None,
                    local_cache_only=True,
                    item=item,
                    ordinal=ordinal,
                )
            )
            ordinal += 1
        return refs

    async def _collect_local_index_search_refs(
        self,
        *,
        q: str,
        seen: set[str],
        item_type: str | None,
        collection_key: str | None,
        tag: str | None,
        starting_ordinal: int,
        existing_refs_by_key: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        hits = await self._search_local_index_hits(
            query=q,
            fields={"title", "creator", "abstract", "venue", "doi", "tag", "note"},
            item_type=item_type,
            collection_key=collection_key,
            tag=tag,
        )
        refs: list[dict[str, Any]] = []
        ordinal = starting_ordinal
        for hit in hits:
            record = hit.get("record", {})
            item_key = str(record.get("itemKey") or "").strip()
            hints = [
                hint for hint in hit.get("hints", []) if isinstance(hint, SearchHint)
            ]
            score = self._coerce_optional_float(hit.get("score"))
            if not item_key:
                continue
            existing = existing_refs_by_key.get(item_key)
            if existing is not None:
                existing_hints = [
                    hint for hint in existing.get("localHints", []) if isinstance(hint, SearchHint)
                ]
                merged_hints = [*existing_hints, *hints]
                existing["localHints"] = self._dedupe_search_hints(merged_hints)
                existing["score"] = max(
                    self._coerce_optional_float(existing.get("score")) or 0.0,
                    score or 0.0,
                )
                continue
            seen.add(item_key)
            ref = self._build_search_result_ref(
                item_key=item_key,
                raw_hit=None,
                local_cache_only=False,
                item=self._search_item_from_local_record(record),
                ordinal=ordinal,
                local_hints=hints,
                score=score,
            )
            refs.append(ref)
            existing_refs_by_key[item_key] = ref
            ordinal += 1
        return refs

    def _order_search_result_refs(
        self,
        *,
        upstream_refs: list[dict[str, Any]],
        local_cache_refs: list[dict[str, Any]],
        sort: str | None,
        direction: str | None,
    ) -> list[dict[str, Any]]:
        ordered = [*upstream_refs, *local_cache_refs]
        if not sort or not local_cache_refs:
            return ordered

        normalized_direction = self._effective_search_sort_direction(
            sort=sort,
            direction=direction,
        )
        return sorted(
            ordered,
            key=cmp_to_key(
                lambda left, right: self._compare_search_result_refs(
                    left,
                    right,
                    sort=sort,
                    direction=normalized_direction,
                )
            ),
        )

    async def _resolve_search_result_page(
        self,
        *,
        refs: list[dict[str, Any]],
        query: str,
        include_attachments: bool,
        include_notes: bool,
        start: int,
        limit: int,
    ) -> tuple[list[SearchResultItem], int | None]:
        if start >= len(refs) or limit <= 0:
            return [], None

        items: list[SearchResultItem] = []
        index = start
        while index < len(refs) and len(items) < limit:
            resolved = await self._resolve_search_result_ref(
                ref=refs[index],
                query=query,
                include_attachments=include_attachments,
                include_notes=include_notes,
            )
            index += 1
            if resolved is None:
                continue
            items.append(resolved)

        next_start = index if index < len(refs) else None
        return items, next_start

    async def _resolve_search_result_ref(
        self,
        *,
        ref: dict[str, Any],
        query: str,
        include_attachments: bool,
        include_notes: bool,
    ) -> SearchResultItem | None:
        item = ref.get("item")
        raw_hit = ref.get("rawHit")
        local_cache_only = bool(ref.get("localCacheOnly"))
        item_key = str(ref.get("itemKey") or "")

        try:
            if (
                item is None
                and not include_attachments
                and not include_notes
                and isinstance(raw_hit, dict)
                and self._raw_hit_is_parent_item(raw_hit, item_key=item_key)
            ):
                item = await self._normalize_parent_item(
                    raw_hit,
                    include_attachments=False,
                    include_notes=False,
                )
            elif item is None or include_attachments or include_notes:
                item = await self.get_parent_item(
                    item_key=item_key,
                    include_attachments=include_attachments,
                    include_notes=include_notes,
                )
        except BridgeError as exc:
            if exc.code != "ITEM_NOT_FOUND":
                raise
            if local_cache_only:
                self._prune_cached_fulltext_records_for_item(item_key)
            return None

        assert isinstance(item, SearchItem)
        return self._build_search_result_item(
            item=item,
            query=query,
            raw_hit=raw_hit if isinstance(raw_hit, dict) else None,
            local_cache_only=local_cache_only,
            extra_hints=[
                hint
                for hint in ref.get("localHints", [])
                if isinstance(hint, SearchHint)
            ],
            score=self._coerce_optional_float(ref.get("score")),
        )

    @staticmethod
    def _raw_hit_is_parent_item(raw_hit: dict[str, Any], *, item_key: str) -> bool:
        data = raw_hit.get("data", {})
        return (
            str(raw_hit.get("key") or "") == item_key
            and not BridgeService._clean_optional_str(data.get("parentItem"))
        )

    def _build_search_hints(
        self,
        *,
        item: SearchItem,
        query: str,
        raw_hit: dict[str, Any] | None,
        local_cache_only: bool,
        extra_hints: list[SearchHint],
    ) -> list[SearchHint]:
        normalized_query = query.strip().casefold()
        if not normalized_query:
            return list(extra_hints)

        hints: list[SearchHint] = []
        self._append_search_hint(
            hints,
            field="title",
            snippet=item.title if normalized_query in item.title.casefold() else None,
        )
        self._append_search_hint(
            hints,
            field="doi",
            snippet=item.DOI if item.DOI and normalized_query in item.DOI.casefold() else None,
        )
        self._append_search_hint(
            hints,
            field="abstract",
            snippet=self._query_snippet(item.abstractNote or "", normalized_query),
        )
        self._append_search_hint(
            hints,
            field="venue",
            snippet=(
                item.venue
                if item.venue and normalized_query in item.venue.casefold()
                else None
            ),
        )
        for creator in item.creators:
            if normalized_query in creator.displayName.casefold():
                self._append_search_hint(hints, field="creator", snippet=creator.displayName)
        for tag in item.tags:
            if normalized_query in tag.casefold():
                self._append_search_hint(hints, field="tag", snippet=tag)

        if raw_hit is not None:
            for hint in self._raw_hit_search_hints(
                item_key=item.itemKey,
                raw_hit=raw_hit,
                query=query,
            ):
                self._append_search_hint(hints, field=hint.field, snippet=hint.snippet)

        if local_cache_only:
            self._append_search_hint(
                hints,
                field="local_cache_fulltext",
                snippet=self._cached_fulltext_search_snippet(item.itemKey, query),
            )

        for hint in extra_hints:
            self._append_search_hint(hints, field=hint.field, snippet=hint.snippet)

        if not hints:
            self._append_search_hint(
                hints,
                field="search",
                snippet=self._cached_fulltext_search_snippet(item.itemKey, query),
            )
        return hints

    def _cached_fulltext_search_snippet(self, item_key: str, query: str) -> str | None:
        if self._local_fulltext_store is None:
            return None
        return self._local_fulltext_store.first_match_snippet(item_key=item_key, query=query)

    def _raw_hit_search_hints(
        self,
        *,
        item_key: str,
        raw_hit: dict[str, Any],
        query: str,
    ) -> list[SearchHint]:
        normalized_query = query.strip().casefold()
        if not normalized_query:
            return []

        data = raw_hit.get("data", {})
        raw_item_type = str(data.get("itemType") or "")
        hints: list[SearchHint] = []
        if raw_item_type == "attachment":
            attachment_values = [
                str(data.get("title") or ""),
                str(data.get("filename") or ""),
            ]
            attachment_text = " ".join(value for value in attachment_values if value)
            attachment_snippet = self._query_snippet(attachment_text, normalized_query)
            if attachment_snippet is not None:
                hints.append(SearchHint(field="attachment", snippet=attachment_snippet))
                return hints
            cached_snippet = self._cached_fulltext_search_snippet(item_key, query)
            hints.append(
                SearchHint(
                    field="local_cache_fulltext" if cached_snippet else "fulltext",
                    snippet=cached_snippet,
                )
            )
            return hints
        if raw_item_type == "note":
            note_text = self._note_renderer.to_plain_text(str(data.get("note") or ""))
            note_snippet = self._query_snippet(note_text, normalized_query)
            if note_snippet is not None:
                hints.append(SearchHint(field="note", snippet=note_snippet))
        return hints

    async def _ensure_local_search_index_ready(self) -> None:
        if self._local_search_index is None:
            return
        if self._local_search_index.is_ready():
            return
        await self._sync_local_search_index()

    async def _local_search_index_sync_loop(self) -> None:
        interval = max(self._settings.local_search_index_refresh_seconds, 60)
        try:
            while True:
                try:
                    await self._sync_local_search_index()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise

    async def _sync_local_search_index(self, *, force_rebuild: bool = False) -> None:
        if self._local_search_index is None:
            return
        async with self._local_search_index_lock:
            try:
                if force_rebuild or not self._local_search_index.is_ready():
                    await self._rebuild_local_search_index()
                    return
                await self._incremental_sync_local_search_index()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._local_search_index.mark_error(f"{type(exc).__name__}: {exc}")
                raise

    async def _rebuild_local_search_index(self) -> None:
        if self._local_search_index is None:
            return
        _, latest_version = await self._zotero.list_top_level_item_versions(since_version=None)
        raw_items = await self._list_all_top_level_items_raw(
            item_type=None,
            collection_key=None,
            tag=None,
            sort="dateModified",
            direction="desc",
        )
        records: list[dict[str, Any]] = []
        for raw_item in raw_items:
            children = await self._zotero.get_children(str(raw_item.get("key")))
            records.append(
                self._build_local_search_index_record(
                    raw_item=raw_item,
                    children=children,
                )
            )
        self._local_search_index.replace_records(
            records,
            last_modified_version=latest_version,
            sync_method="rebuild",
        )

    async def _incremental_sync_local_search_index(self) -> None:
        if self._local_search_index is None:
            return
        since_version = self._local_search_index.last_modified_version()
        if since_version is None:
            await self._rebuild_local_search_index()
            return
        changed_versions, latest_version = await self._zotero.list_top_level_item_versions(
            since_version=since_version
        )
        deleted_item_keys = await self._zotero.get_deleted_item_keys(since_version=since_version)
        changed_keys = sorted(changed_versions.keys())
        raw_items = await self._zotero.get_items_by_keys_raw(changed_keys) if changed_keys else []
        raw_by_key = {
            str(raw_item.get("key") or ""): raw_item
            for raw_item in raw_items
            if str(raw_item.get("key") or "")
        }
        for item_key in changed_keys:
            raw_item = raw_by_key.get(item_key)
            if raw_item is None:
                self._local_search_index.delete_record(item_key)
                continue
            children = await self._zotero.get_children(item_key)
            self._local_search_index.upsert_record(
                self._build_local_search_index_record(
                    raw_item=raw_item,
                    children=children,
                )
            )
        for deleted_item_key in deleted_item_keys:
            self._local_search_index.delete_record(deleted_item_key)
        self._local_search_index.mark_synced(
            last_modified_version=latest_version if latest_version is not None else since_version,
            sync_method="incremental",
        )

    async def _refresh_local_search_index_item(self, item_key: str) -> None:
        if self._local_search_index is None or not item_key.strip():
            return
        try:
            raw_item = await self._zotero.get_item(item_key)
            children = await self._zotero.get_children(item_key)
        except BridgeError as exc:
            if exc.code == "ITEM_NOT_FOUND":
                self._local_search_index.delete_record(item_key)
                self._local_search_index.mark_synced(
                    last_modified_version=self._local_search_index.last_modified_version(),
                    sync_method="write_through",
                )
            return
        except Exception:
            return
        self._local_search_index.upsert_record(
            self._build_local_search_index_record(raw_item=raw_item, children=children)
        )
        self._local_search_index.mark_synced(
            last_modified_version=self._local_search_index.last_modified_version(),
            sync_method="write_through",
        )

    def _build_local_search_index_record(
        self,
        *,
        raw_item: dict[str, Any],
        children: list[dict[str, Any]],
    ) -> dict[str, Any]:
        data = raw_item.get("data", {})
        note_texts = [
            self._normalize_search_text(
                self._note_renderer.to_plain_text(str(child.get("data", {}).get("note") or ""))
            )
            for child in children
            if child.get("data", {}).get("itemType") == "note"
        ]
        item_key = str(raw_item.get("key") or "")
        fulltext_text = None
        if self._local_fulltext_store is not None and item_key:
            fulltext_text = self._normalize_search_text(
                self._local_fulltext_store.item_search_text(item_key)
            )
        return {
            "itemKey": item_key,
            "itemType": str(data.get("itemType") or ""),
            "title": str(data.get("title") or "(untitled)"),
            "dateAdded": self._clean_optional_str(data.get("dateAdded")),
            "dateModified": self._clean_optional_str(data.get("dateModified")),
            "year": self._extract_year(data.get("date")),
            "DOI": self._clean_optional_str(data.get("DOI")),
            "abstractNote": self._normalize_search_text(data.get("abstractNote")),
            "venue": self._normalize_search_text(self._normalize_venue(data)),
            "creators": [
                creator.displayName
                for creator in self._normalize_creators(data.get("creators"))
            ],
            "tags": self._normalize_tags(data.get("tags")),
            "collectionKeys": [
                str(value) for value in data.get("collections", []) if isinstance(value, str)
            ],
            "noteText": "\n\n".join(text for text in note_texts if text),
            "fulltextText": fulltext_text,
        }

    async def _search_local_index_hits(
        self,
        *,
        query: str,
        fields: set[str],
        item_type: str | None,
        collection_key: str | None,
        tag: str | None,
    ) -> list[dict[str, Any]]:
        if self._local_search_index is None or not query.strip() or not fields:
            return []
        try:
            await self._ensure_local_search_index_ready()
            hits = self._local_search_index.search(
                query=query,
                fields=fields,
                item_type=item_type,
                collection_key=collection_key,
                tag=tag,
            )
        except Exception:
            return []
        for hit in hits:
            hints = [
                SearchHint(
                    field=(
                        "local_cache_fulltext"
                        if str(value.get("field") or "") == "fulltext"
                        else str(value.get("field") or "")
                    ),
                    snippet=value.get("snippet"),
                )
                for value in hit.get("hints", [])
                if isinstance(value, dict) and value.get("field")
            ]
            hit["hints"] = hints
            hit["score"] = self._score_search_hints(hints)
        return sorted(
            hits,
            key=lambda hit: (
                -float(hit.get("score") or 0.0),
                -(
                    self._coerce_optional_timestamp(hit.get("record", {}).get("dateModified"))
                    or 0.0
                ),
                str(hit.get("record", {}).get("title") or "").casefold(),
            ),
        )

    async def _all_local_search_index_records(
        self,
        *,
        require_ready: bool = False,
    ) -> list[dict[str, Any]]:
        if self._local_search_index is None:
            return []
        try:
            if not self._local_search_index.is_ready():
                if require_ready:
                    await self._ensure_local_search_index_ready()
                else:
                    return []
            return self._local_search_index.all_records()
        except Exception:
            return []

    @staticmethod
    def _search_hint_weight(field: str) -> float:
        return {
            "doi": 12.0,
            "title": 10.0,
            "creator": 7.0,
            "note": 6.0,
            "venue": 5.0,
            "abstract": 4.5,
            "tag": 3.5,
            "attachment": 3.0,
            "fulltext": 3.0,
            "local_cache_fulltext": 3.0,
            "search": 1.0,
        }.get(field, 1.0)

    def _score_search_hints(self, hints: list[SearchHint]) -> float:
        if not hints:
            return 0.0
        total = 0.0
        seen_fields: set[tuple[str, str | None]] = set()
        for hint in hints:
            signature = (hint.field, hint.snippet)
            if signature in seen_fields:
                continue
            seen_fields.add(signature)
            weight = self._search_hint_weight(hint.field)
            if hint.snippet:
                total += weight
            else:
                total += weight * 0.6
        return round(total, 3)

    @staticmethod
    def _append_search_hint(
        hints: list[SearchHint],
        *,
        field: str,
        snippet: str | None,
    ) -> None:
        if snippet is None and field not in {"fulltext", "local_cache_fulltext", "search"}:
            return
        for existing in hints:
            if existing.field == field and existing.snippet == snippet:
                return
        hints.append(SearchHint(field=field, snippet=snippet))

    @staticmethod
    def _parent_item_key_from_raw(raw_item: dict[str, Any]) -> str | None:
        data = raw_item.get("data", {})
        item_type = data.get("itemType")
        if item_type in {"attachment", "note"}:
            key = data.get("parentItem") or raw_item.get("key")
        else:
            key = raw_item.get("key")
        if not isinstance(key, str) or not key:
            return None
        return key

    @staticmethod
    def _query_snippet(text: str, normalized_query: str, radius: int = 80) -> str | None:
        normalized_text = text.replace("\u00ad", "")
        normalized_text = normalized_text.replace("\r\n", "\n").replace("\r", "\n")
        normalized_text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", normalized_text)
        lowered = normalized_text.casefold()
        index = lowered.find(normalized_query)
        if index == -1:
            return None
        start = max(index - radius, 0)
        end = min(index + len(normalized_query) + radius, len(normalized_text))
        snippet = normalized_text[start:end].strip().replace("\r", " ").replace("\n", " ")
        if start > 0:
            snippet = f"...{snippet}"
        if end < len(normalized_text):
            snippet = f"{snippet}..."
        return snippet

    @staticmethod
    def _dedupe_search_hints(hints: list[SearchHint]) -> list[SearchHint]:
        deduped: list[SearchHint] = []
        seen: set[tuple[str, str | None]] = set()
        for hint in hints:
            signature = (hint.field, hint.snippet)
            if signature in seen:
                continue
            seen.add(signature)
            deduped.append(hint)
        return deduped

    def _search_item_from_local_record(self, record: dict[str, Any]) -> SearchItem:
        return SearchItem(
            itemKey=str(record.get("itemKey") or ""),
            itemType=str(record.get("itemType") or ""),
            title=str(record.get("title") or "(untitled)"),
            date=None,
            dateAdded=self._clean_optional_str(record.get("dateAdded")),
            dateModified=self._clean_optional_str(record.get("dateModified")),
            year=self._clean_optional_str(record.get("year")),
            DOI=self._clean_optional_str(record.get("DOI")),
            abstractNote=self._clean_optional_str(record.get("abstractNote")),
            publicationTitle=self._clean_optional_str(record.get("venue")),
            venue=self._clean_optional_str(record.get("venue")),
            url=None,
            publisher=None,
            bookTitle=None,
            proceedingsTitle=None,
            conferenceName=None,
            language=None,
            extra=None,
            relations=[],
            creators=[
                Creator(displayName=str(name))
                for name in record.get("creators", [])
                if str(name).strip()
            ],
            tags=[str(tag) for tag in record.get("tags", []) if str(tag).strip()],
            collectionKeys=[
                str(key) for key in record.get("collectionKeys", []) if str(key).strip()
            ],
            attachments=[],
            aiNotes=[],
        )

    async def _map_with_concurrency(
        self,
        values: list[Any],
        worker: Any,
        *,
        limit: int,
    ) -> list[Any]:
        if not values:
            return []
        semaphore = asyncio.Semaphore(max(limit, 1))
        results: list[Any] = [None] * len(values)

        async def run(index: int, value: Any) -> None:
            async with semaphore:
                results[index] = await worker(value)

        await asyncio.gather(*(run(index, value) for index, value in enumerate(values)))
        return results

    @staticmethod
    def _next_start(*, start: int, returned_count: int, total: int) -> int | None:
        next_start = start + returned_count
        if returned_count <= 0 or next_start >= total:
            return None
        return next_start

    def _compare_search_result_refs(
        self,
        left: dict[str, Any],
        right: dict[str, Any],
        *,
        sort: str,
        direction: str,
    ) -> int:
        left_value = self._search_result_ref_sort_value(left, field=sort)
        right_value = self._search_result_ref_sort_value(right, field=sort)
        if left_value is None and right_value is None:
            return int(left["ordinal"]) - int(right["ordinal"])
        if left_value is None:
            return 1
        if right_value is None:
            return -1
        if left_value < right_value:
            comparison = -1
        elif left_value > right_value:
            comparison = 1
        else:
            comparison = 0
        if comparison == 0:
            return int(left["ordinal"]) - int(right["ordinal"])
        if direction == "desc":
            return -comparison
        return comparison

    def _search_result_ref_sort_value(
        self,
        ref: dict[str, Any],
        *,
        field: str,
    ) -> str | None:
        item = ref.get("item")
        if isinstance(item, SearchItem):
            if field == "title":
                return item.title.casefold()
            if field == "dateAdded":
                return item.dateAdded
            if field == "dateModified":
                return item.dateModified
            return None

        raw_hit = ref.get("rawHit")
        if not isinstance(raw_hit, dict):
            return None
        data = raw_hit.get("data", {})
        if field == "title":
            title = self._clean_optional_str(data.get("title"))
            return None if title is None else title.casefold()
        if field == "dateAdded":
            return self._clean_optional_str(data.get("dateAdded"))
        if field == "dateModified":
            return self._clean_optional_str(data.get("dateModified"))
        return None

    @staticmethod
    def _effective_search_sort_direction(
        *,
        sort: str,
        direction: str | None,
    ) -> str:
        if direction in {"asc", "desc"}:
            return direction
        if sort == "title":
            return "asc"
        return "desc"

    @staticmethod
    def _normalize_title_key(title: str) -> str:
        return " ".join(title.casefold().split())

    @staticmethod
    def _advanced_search_fields(fields: str) -> set[str]:
        return {field.strip() for field in fields.split(",") if field.strip()}

    def _validate_advanced_search_request(
        self,
        *,
        q: str | None,
        title: str | None,
        author: str | None,
        abstract: str | None,
        venue: str | None,
        doi: str | None,
        year_from: int | None,
        year_to: int | None,
        has_fulltext: bool | None,
        has_ai_notes: bool | None,
    ) -> None:
        if year_from is not None and year_to is not None and year_from > year_to:
            raise BridgeError(
                code="BAD_REQUEST",
                message="yearFrom must be less than or equal to yearTo",
                status_code=400,
            )
        if not any(
            [
                (q or "").strip(),
                (title or "").strip(),
                (author or "").strip(),
                (abstract or "").strip(),
                (venue or "").strip(),
                (doi or "").strip(),
                year_from is not None,
                year_to is not None,
                has_fulltext is not None,
                has_ai_notes is not None,
            ]
        ):
            raise BridgeError(
                code="BAD_REQUEST",
                message="At least one advanced search filter is required",
                status_code=400,
            )

    def _advanced_item_matches(
        self,
        *,
        item: SearchItem,
        raw_item: dict[str, Any],
        query: str,
        fields: set[str],
        title: str | None,
        author: str | None,
        abstract: str | None,
        venue: str | None,
        doi: str | None,
        year_from: int | None,
        year_to: int | None,
        has_fulltext: bool | None,
        has_ai_notes: bool | None,
        note_match: bool,
        fulltext_match: bool,
    ) -> bool:
        if title and title.casefold() not in item.title.casefold():
            return False
        if author and not any(
            author.casefold() in creator.displayName.casefold()
            for creator in item.creators
        ):
            return False
        if abstract and abstract.casefold() not in (item.abstractNote or "").casefold():
            return False
        if venue and venue.casefold() not in (item.venue or "").casefold():
            return False
        if doi:
            normalized_expected = self._normalize_doi_safe(doi)
            if normalized_expected is None:
                if doi.casefold() not in (item.DOI or "").casefold():
                    return False
            elif self._normalize_doi_safe(item.DOI) != normalized_expected:
                return False
        item_year = self._coerce_optional_int(item.year)
        if year_from is not None and (item_year is None or item_year < year_from):
            return False
        if year_to is not None and (item_year is None or item_year > year_to):
            return False
        item_has_fulltext = self._item_has_fulltext_candidate(item, raw_item=raw_item)
        if has_fulltext is not None and item_has_fulltext != has_fulltext:
            return False
        item_has_ai_notes = bool(item.aiNotes)
        if has_ai_notes is not None and item_has_ai_notes != has_ai_notes:
            return False
        if not query:
            return True
        metadata_matchers = {
            "title": query in item.title.casefold(),
            "creator": any(query in creator.displayName.casefold() for creator in item.creators),
            "abstract": query in (item.abstractNote or "").casefold(),
            "venue": query in (item.venue or "").casefold(),
            "doi": query in (item.DOI or "").casefold(),
            "tag": any(query in tag.casefold() for tag in item.tags),
            "note": note_match,
            "fulltext": fulltext_match,
        }
        return any(metadata_matchers[field] for field in fields)

    def _build_advanced_search_result_item(
        self,
        *,
        item: SearchItem,
        query: str,
        fields: set[str],
        extra_hints: list[SearchHint],
        score: float,
    ) -> SearchResultItem:
        hints: list[SearchHint] = []
        if query:
            if "title" in fields:
                self._append_search_hint(
                    hints,
                    field="title",
                    snippet=item.title if query in item.title.casefold() else None,
                )
            if "creator" in fields:
                for creator in item.creators:
                    if query in creator.displayName.casefold():
                        self._append_search_hint(
                            hints,
                            field="creator",
                            snippet=creator.displayName,
                        )
            if "abstract" in fields:
                self._append_search_hint(
                    hints,
                    field="abstract",
                    snippet=self._query_snippet(item.abstractNote or "", query),
                )
            if "venue" in fields:
                self._append_search_hint(
                    hints,
                    field="venue",
                    snippet=item.venue if item.venue and query in item.venue.casefold() else None,
                )
            if "doi" in fields:
                self._append_search_hint(
                    hints,
                    field="doi",
                    snippet=item.DOI if item.DOI and query in item.DOI.casefold() else None,
                )
            if "tag" in fields:
                for tag in item.tags:
                    if query in tag.casefold():
                        self._append_search_hint(hints, field="tag", snippet=tag)
        for hint in extra_hints:
            if hint.field in fields or hint.field in {"note", "fulltext", "local_cache_fulltext"}:
                self._append_search_hint(
                    hints,
                    field=hint.field,
                    snippet=hint.snippet,
                )
        return SearchResultItem(
            **item.model_dump(),
            searchHints=self._dedupe_search_hints(hints),
            score=round(score, 3),
        )

    def _sort_advanced_search_results(
        self,
        *,
        matches: list[SearchResultItem],
        sort: str,
        direction: str | None,
        query: str,
    ) -> list[SearchResultItem]:
        if not matches:
            return matches
        if sort == "relevance" and query:
            return sorted(
                matches,
                key=lambda item: (
                    -(item.score or 0.0),
                    -(self._coerce_optional_timestamp(item.dateModified) or 0.0),
                    item.title.casefold(),
                    item.itemKey,
                ),
            )

        normalized_direction = self._effective_search_sort_direction(
            sort="dateModified" if sort == "relevance" else sort,
            direction=direction,
        )
        field = "dateModified" if sort == "relevance" else sort
        reverse = normalized_direction == "desc"
        return sorted(
            matches,
            key=lambda item: self._advanced_sort_value(item=item, field=field),
            reverse=reverse,
        )

    def _advanced_sort_value(
        self,
        *,
        item: SearchResultItem,
        field: str,
    ) -> tuple[float, str]:
        if field == "title":
            return (0.0, item.title.casefold())
        if field == "dateAdded":
            return (
                self._coerce_optional_timestamp(item.dateAdded) or 0.0,
                item.itemKey,
            )
        return (
            self._coerce_optional_timestamp(item.dateModified) or 0.0,
            item.itemKey,
        )

    def _item_has_fulltext_candidate(
        self,
        item: SearchItem,
        *,
        raw_item: dict[str, Any] | None = None,
    ) -> bool:
        if item.itemType == "attachment":
            raw_data = raw_item.get("data", {}) if isinstance(raw_item, dict) else {}
            content_type = str(raw_data.get("contentType") or "").lower()
            filename = str(raw_data.get("filename") or "").lower()
            title = item.title.casefold()
            return content_type == "application/pdf" or filename.endswith(".pdf") or title.endswith(
                ".pdf"
            )
        return any(attachment.hasFulltext for attachment in item.attachments)

    def _normalize_doi_safe(self, value: str | None) -> str | None:
        if not value:
            return None
        try:
            return self._doi_resolver.normalize_doi(value)
        except BridgeError:
            return None

    @staticmethod
    def _item_type_counts(items: list[SearchItem]) -> list[ItemTypeCount]:
        counts: dict[str, int] = {}
        for item in items:
            counts[item.itemType] = counts.get(item.itemType, 0) + 1
        return [
            ItemTypeCount(itemType=item_type, count=count)
            for item_type, count in sorted(
                counts.items(),
                key=lambda entry: (-entry[1], entry[0]),
            )
        ]

    @staticmethod
    def _item_matches_filters(
        *,
        item: SearchItem,
        item_type: str | None,
        collection_key: str | None,
        tag: str | None,
    ) -> bool:
        if item_type and item.itemType != item_type:
            return False
        if collection_key and collection_key not in item.collectionKeys:
            return False
        if tag and tag not in item.tags:
            return False
        return True

    def _normalize_collection(
        self,
        raw_collection: dict[str, Any],
        *,
        collections_by_key: dict[str, dict[str, Any]],
    ) -> CollectionSummary:
        data = raw_collection.get("data", {})
        name = str(data.get("name") or "(untitled collection)")
        parent_collection_key = self._clean_optional_str(data.get("parentCollection"))
        path_names = [name]
        seen = {str(raw_collection.get("key") or "")}
        current_key = parent_collection_key
        while current_key:
            parent_collection = collections_by_key.get(current_key)
            if parent_collection is None or current_key in seen:
                break
            seen.add(current_key)
            parent_data = parent_collection.get("data", {})
            path_names.append(str(parent_data.get("name") or "(untitled collection)"))
            current_key = self._clean_optional_str(parent_data.get("parentCollection"))
        path_names.reverse()
        meta = raw_collection.get("meta", {})
        return CollectionSummary(
            collectionKey=str(raw_collection.get("key") or ""),
            name=name,
            parentCollectionKey=parent_collection_key,
            path=" / ".join(path_names),
            depth=max(len(path_names) - 1, 0),
            numCollections=self._coerce_optional_int(meta.get("numCollections"))
            if isinstance(meta, dict)
            else None,
            numItems=self._coerce_optional_int(meta.get("numItems"))
            if isinstance(meta, dict)
            else None,
        )

    def _normalize_tag(self, raw_tag: dict[str, Any]) -> TagSummary:
        return TagSummary(
            tag=str(raw_tag.get("tag") or ""),
            type=self._coerce_optional_int(raw_tag.get("type")),
            numItems=self._coerce_optional_int(raw_tag.get("meta", {}).get("numItems"))
            if isinstance(raw_tag.get("meta"), dict)
            else None,
        )

    def _duplicate_fields(self, by: str) -> list[str]:
        requested = [part.strip() for part in by.split(",") if part.strip()]
        valid_fields = [field for field in requested if field in {"title", "doi"}]
        if not valid_fields:
            raise BridgeError(
                code="BAD_REQUEST",
                message="by must contain title and/or doi",
                status_code=400,
            )
        return valid_fields

    def _group_duplicate_items(
        self,
        items: list[SearchItem],
        *,
        field: str,
    ) -> list[DuplicateGroup]:
        grouped: dict[str, list[SearchItem]] = {}
        for item in items:
            duplicate_value = self._duplicate_value(item, field=field)
            if duplicate_value is None:
                continue
            grouped.setdefault(duplicate_value, []).append(item)
        return [
            DuplicateGroup(
                field=field,
                value=value,
                items=group_items,
                count=len(group_items),
            )
            for value, group_items in grouped.items()
            if len(group_items) > 1
        ]

    def _duplicate_value(self, item: SearchItem, *, field: str) -> str | None:
        if field == "doi":
            if item.DOI is None:
                return None
            try:
                return self._doi_resolver.normalize_doi(item.DOI)
            except BridgeError:
                return self._clean_optional_str(item.DOI.casefold())
        if field == "title":
            normalized_title = self._normalize_title_key(item.title)
            return normalized_title or None
        return None

    @staticmethod
    def _parse_since_timestamp(value: str) -> datetime:
        normalized_value = value.strip()
        if not normalized_value:
            raise BridgeError(
                code="BAD_REQUEST",
                message="sinceTimestamp is required",
                status_code=400,
            )
        if normalized_value.endswith("Z"):
            normalized_value = normalized_value[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(normalized_value)
        except ValueError as exc:
            raise BridgeError(
                code="BAD_REQUEST",
                message="sinceTimestamp must be a valid ISO 8601 timestamp",
                status_code=400,
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _item_modified_since(self, raw_item: dict[str, Any], *, cutoff: datetime) -> bool:
        date_modified = self._clean_optional_str(raw_item.get("data", {}).get("dateModified"))
        if date_modified is None:
            return False
        normalized_value = (
            date_modified[:-1] + "+00:00"
            if date_modified.endswith("Z")
            else date_modified
        )
        try:
            parsed = datetime.fromisoformat(normalized_value)
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC) >= cutoff

    def _related_item_keys(self, raw_item: dict[str, Any]) -> list[str]:
        relations = raw_item.get("data", {}).get("relations", {})
        if not isinstance(relations, dict):
            return []
        current_key = str(raw_item.get("key") or "")
        related_keys: list[str] = []
        for relation_value in relations.values():
            values = relation_value if isinstance(relation_value, list) else [relation_value]
            for value in values:
                if not isinstance(value, str):
                    continue
                match = ZOTERO_RELATION_ITEM_PATTERN.search(value)
                if match is None:
                    continue
                related_key = match.group(1)
                if related_key == current_key or related_key in related_keys:
                    continue
                related_keys.append(related_key)
        return related_keys

    @staticmethod
    def _normalize_input_strings(values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            cleaned = value.strip()
            if not cleaned or cleaned in normalized:
                continue
            normalized.append(cleaned)
        return normalized

    @staticmethod
    def _merge_strings(existing: list[str], extra: list[str]) -> list[str]:
        merged = existing[:]
        for value in extra:
            if value not in merged:
                merged.append(value)
        return merged

    def _assert_duplicate_merge_candidate(
        self,
        raw_item: dict[str, Any],
        *,
        item_key: str,
    ) -> None:
        data = raw_item.get("data", {})
        item_type = str(data.get("itemType") or "")
        if item_type in {"attachment", "note"} or data.get("parentItem"):
            raise BridgeError(
                code="BAD_REQUEST",
                message=f"Item {item_key} is not a top-level bibliographic item",
                status_code=400,
            )

    async def upload_pdf_from_action(
        self,
        payload: UploadPdfActionRequest,
    ) -> UploadPdfResponse:
        file_url: str | None = None
        filename: str | None = None
        content_type: str | None = None

        if payload.fileUrl and payload.openaiFileIdRefs:
            raise BridgeError(
                code="BAD_REQUEST",
                message="Provide either fileUrl or openaiFileIdRefs, not both",
                status_code=400,
            )
        if payload.openaiFileIdRefs:
            if len(payload.openaiFileIdRefs) != 1:
                raise BridgeError(
                    code="BAD_REQUEST",
                    message="MVP accepts exactly one openaiFileIdRef",
                    status_code=400,
                )
            file_ref = payload.openaiFileIdRefs[0]
            file_url = str(file_ref.download_link)
            filename = file_ref.name
            content_type = file_ref.mime_type
        elif payload.fileUrl:
            assert payload.fileUrl is not None
            file_url = str(payload.fileUrl)
            filename = PurePosixPath(payload.fileUrl.path or "/upload.pdf").name or "upload.pdf"

        if not file_url:
            raise BridgeError(
                code="BAD_REQUEST",
                message="A PDF source is required",
                status_code=400,
            )

        file_bytes, detected_content_type = await self._download_file(file_url)
        return await self.upload_pdf_bytes(
            content=file_bytes,
            filename=filename or "upload.pdf",
            content_type=content_type or detected_content_type or "application/pdf",
            item_key=payload.itemKey,
            doi=payload.doi,
            collection_key=payload.collectionKey,
            tags=payload.tags,
            create_top_level=payload.createTopLevelAttachmentIfNeeded,
            request_id=payload.requestId,
        )

    async def upload_pdf_bytes(
        self,
        *,
        content: bytes,
        filename: str,
        content_type: str,
        item_key: str | None,
        doi: str | None,
        collection_key: str | None,
        tags: list[str],
        create_top_level: bool,
        request_id: str | None,
    ) -> UploadPdfResponse:
        if len(content) > self._settings.max_upload_file_bytes:
            raise BridgeError(
                code="FILE_TOO_LARGE",
                message="Uploaded file exceeds configured size limit",
                status_code=413,
            )
        if not self._looks_like_pdf(content):
            raise BridgeError(
                code="BAD_REQUEST",
                message="Only PDF uploads are supported",
                status_code=400,
            )

        parent_item_key = await self._resolve_upload_parent(
            item_key=item_key,
            doi=doi,
            collection_key=collection_key,
            tags=tags,
            request_id=request_id,
            create_top_level=create_top_level,
        )

        md5 = hashlib.md5(content, usedforsecurity=False).hexdigest()
        attachment_status = UploadPdfStatus.CREATED
        attachment_key: str | None = None
        if request_id:
            existing = await self._find_existing_attachment(
                parent_item_key=parent_item_key,
                filename=filename,
                md5=md5,
            )
            if existing is not None:
                existing_data = existing.get("data", {})
                existing_md5 = str(existing_data.get("md5") or "")
                if existing_md5 == md5:
                    self._cache_uploaded_fulltext(
                        attachment_key=str(existing.get("key") or ""),
                        item_key=parent_item_key,
                        filename=filename,
                        content=content,
                    )
                    await self._refresh_local_search_index_item(
                        parent_item_key or str(existing.get("key") or "")
                    )
                    return self._attachment_upload_response(
                        status=UploadPdfStatus.UPDATED,
                        attachment=existing,
                        parent_item_key=parent_item_key,
                    )
                attachment_key = str(existing.get("key") or "")
                attachment_status = UploadPdfStatus.UPDATED

        if not attachment_key:
            template = await self._zotero.get_item_template(
                "attachment",
                link_mode="imported_file",
            )
            attachment_payload = dict(template)
            attachment_payload["itemType"] = "attachment"
            attachment_payload["linkMode"] = "imported_file"
            attachment_payload["title"] = filename
            attachment_payload["filename"] = filename
            attachment_payload["contentType"] = "application/pdf"
            attachment_payload["tags"] = [{"tag": tag} for tag in tags]
            if parent_item_key:
                attachment_payload["parentItem"] = parent_item_key
            else:
                attachment_payload["collections"] = [
                    key for key in [collection_key or self._settings.default_collection_key] if key
                ]

            try:
                attachment_key = (
                    await self._zotero.create_items(
                        [attachment_payload],
                        write_token=self._build_write_token(request_id),
                    )
                )[0]
            except BridgeError as exc:
                if exc.code == "WRITE_CONFLICT" and request_id:
                    existing = await self._find_existing_attachment(
                        parent_item_key=parent_item_key,
                        filename=filename,
                        md5=md5,
                    )
                    if existing is not None:
                        existing_data = existing.get("data", {})
                        existing_md5 = str(existing_data.get("md5") or "")
                        if existing_md5 == md5:
                            return self._attachment_upload_response(
                                status=UploadPdfStatus.UPDATED,
                                attachment=existing,
                                parent_item_key=parent_item_key,
                            )
                        attachment_key = str(existing.get("key") or "")
                        attachment_status = UploadPdfStatus.UPDATED
                if not attachment_key:
                    raise

        authorization = await self._zotero.authorize_upload(
            attachment_key=attachment_key,
            filename=filename,
            md5=md5,
            filesize=len(content),
            mtime_ms=int(time.time() * 1000),
        )

        if authorization.get("exists") != 1:
            upload_key = authorization.get("uploadKey")
            if not isinstance(upload_key, str) or not upload_key:
                raise BridgeError(
                    code="UPSTREAM_ERROR",
                    message="Upload authorization did not include uploadKey",
                    status_code=502,
                )
            await self._zotero.upload_to_authorized_url(authorization, content)
            await self._zotero.register_upload(
                attachment_key=attachment_key,
                upload_key=upload_key,
            )

        attachment = await self._zotero.get_item(attachment_key)
        self._cache_uploaded_fulltext(
            attachment_key=attachment_key,
            item_key=parent_item_key,
            filename=filename,
            content=content,
        )
        await self._refresh_local_search_index_item(parent_item_key or attachment_key)
        return self._attachment_upload_response(
            status=attachment_status,
            attachment=attachment,
            parent_item_key=parent_item_key,
        )

    async def _find_item_by_doi(
        self,
        normalized_doi: str,
        title_hint: str | None = None,
    ) -> dict[str, Any] | None:
        raw_items = await self._search_candidate_items(
            normalized_doi,
            include_fulltext=True,
        )
        if title_hint:
            raw_items.extend(
                await self._search_candidate_items(
                    title_hint,
                    include_fulltext=False,
                )
            )

        seen_keys: set[str] = set()
        for item in raw_items:
            item_key = str(item.get("key") or "")
            if not item_key or item_key in seen_keys:
                continue
            seen_keys.add(item_key)
            data = item.get("data", {})
            item_doi = data.get("DOI")
            if self._doi_matches(item_doi, normalized_doi):
                if data.get("itemType") in {"attachment", "note"} and data.get("parentItem"):
                    return await self._zotero.get_item(str(data["parentItem"]))
                return item
        return None

    def _doi_matches(self, doi: Any, normalized_doi: str) -> bool:
        if not isinstance(doi, str) or not doi:
            return False
        try:
            return self._doi_resolver.normalize_doi(doi) == normalized_doi
        except BridgeError:
            return False

    @staticmethod
    def _is_probably_zotero_key(value: str) -> bool:
        return bool(ZOTERO_KEY_PATTERN.fullmatch(value.strip()))

    async def _search_candidate_items(
        self,
        query: str,
        *,
        include_fulltext: bool,
    ) -> list[dict[str, Any]]:
        raw_items = await self._zotero.search_items_raw(
            q=query,
            limit=10,
            include_fulltext=include_fulltext,
        )
        return raw_items

    async def _normalize_parent_item(
        self,
        raw_item: dict[str, Any],
        *,
        children: list[dict[str, Any]] | None = None,
        include_attachments: bool,
        include_notes: bool,
    ) -> SearchItem:
        data = raw_item.get("data", {})
        resolved_children = children or []
        if not resolved_children and (include_attachments or include_notes):
            resolved_children = await self._zotero.get_children(str(raw_item.get("key")))
        attachments = (
            self._normalize_attachments(resolved_children) if include_attachments else []
        )
        ai_notes = self._normalize_ai_notes(resolved_children) if include_notes else []
        return SearchItem(
            itemKey=str(raw_item.get("key")),
            itemType=str(data.get("itemType") or ""),
            title=str(data.get("title") or "(untitled)"),
            date=self._clean_optional_str(data.get("date")),
            dateAdded=self._clean_optional_str(data.get("dateAdded")),
            dateModified=self._clean_optional_str(data.get("dateModified")),
            year=self._extract_year(data.get("date")),
            DOI=self._clean_optional_str(data.get("DOI")),
            abstractNote=self._normalize_search_text(data.get("abstractNote")),
            publicationTitle=self._clean_optional_str(data.get("publicationTitle")),
            venue=self._normalize_search_text(self._normalize_venue(data)),
            url=self._clean_optional_str(data.get("url")),
            publisher=self._clean_optional_str(data.get("publisher")),
            bookTitle=self._clean_optional_str(data.get("bookTitle")),
            proceedingsTitle=self._clean_optional_str(data.get("proceedingsTitle")),
            conferenceName=self._clean_optional_str(data.get("conferenceName")),
            language=self._clean_optional_str(data.get("language")),
            extra=self._clean_optional_str(data.get("extra")),
            relations=self._normalize_relations(data.get("relations")),
            creators=self._normalize_creators(data.get("creators")),
            tags=self._normalize_tags(data.get("tags")),
            collectionKeys=[
                str(value) for value in data.get("collections", []) if isinstance(value, str)
            ],
            attachments=attachments,
            aiNotes=ai_notes,
        )

    def _note_search_hints_from_children(
        self,
        *,
        children: list[dict[str, Any]],
        query: str,
    ) -> list[SearchHint]:
        if not query:
            return []
        hints: list[SearchHint] = []
        for child in children:
            data = child.get("data", {})
            if data.get("itemType") != "note":
                continue
            note_text = self._normalize_search_text(
                self._note_renderer.to_plain_text(str(data.get("note") or ""))
            ) or ""
            self._append_search_hint(
                hints,
                field="note",
                snippet=self._query_snippet(note_text, query),
            )
        return hints

    def _normalize_attachments(self, children: list[dict[str, Any]]) -> list[AttachmentSummary]:
        attachments: list[AttachmentSummary] = []
        for child in children:
            data = child.get("data", {})
            if data.get("itemType") != "attachment":
                continue
            content_type = str(data.get("contentType") or "")
            filename = self._clean_optional_str(data.get("filename"))
            is_pdf = content_type.lower() == "application/pdf" or (filename or "").lower().endswith(
                ".pdf"
            )
            attachments.append(
                AttachmentSummary(
                    attachmentKey=str(child.get("key")),
                    title=str(data.get("title") or filename or "Attachment"),
                    contentType=content_type,
                    filename=filename,
                    linkMode=str(data.get("linkMode") or ""),
                    md5=self._clean_optional_str(data.get("md5")),
                    mtime=self._clean_optional_str(data.get("mtime")),
                    isPdf=is_pdf,
                    hasFulltext=is_pdf,
                )
            )
        return attachments

    def _normalize_ai_notes(self, children: list[dict[str, Any]]) -> list[AINoteSummary]:
        notes: list[AINoteSummary] = []
        for child in children:
            data = child.get("data", {})
            if data.get("itemType") != "note":
                continue
            tags = self._normalize_tags(data.get("tags"))
            identity = self._note_renderer.extract_identity(tags)
            if identity is None:
                continue
            agent, note_type, slot = identity
            notes.append(
                AINoteSummary(
                    noteKey=str(child.get("key")),
                    agent=agent,
                    noteType=note_type,
                    slot=slot,
                    dateModified=str(data.get("dateModified") or ""),
                    tags=self._public_note_tags(tags),
                )
            )
        return notes

    def _normalize_openalex_work(self, raw_work: dict[str, Any]) -> DiscoveryWork:
        primary_location = raw_work.get("primary_location", {})
        source = primary_location.get("source", {}) if isinstance(primary_location, dict) else {}
        open_access = raw_work.get("open_access", {})
        authorships = raw_work.get("authorships", [])
        primary_topic = raw_work.get("primary_topic", {})
        topics: list[DiscoveryTopic] = []
        if isinstance(primary_topic, dict) and primary_topic:
            topics.append(
                DiscoveryTopic(
                    name=str(primary_topic.get("display_name") or ""),
                    openAlexId=self._clean_optional_str(primary_topic.get("id")),
                    score=self._coerce_optional_float(primary_topic.get("score")),
                )
            )
        authors = []
        if isinstance(authorships, list):
            for authorship in authorships[:10]:
                if not isinstance(authorship, dict):
                    continue
                author = authorship.get("author", {})
                if not isinstance(author, dict):
                    continue
                name = str(author.get("display_name") or "").strip()
                if not name:
                    continue
                authors.append(
                    DiscoveryAuthor(
                        name=name,
                        openAlexId=self._clean_optional_str(author.get("id")),
                    )
                )
        return DiscoveryWork(
            openAlexId=str(raw_work.get("id") or ""),
            title=str(raw_work.get("display_name") or "(untitled)"),
            doi=self._clean_optional_str(raw_work.get("doi")),
            publicationYear=self._coerce_optional_int(raw_work.get("publication_year")),
            publicationDate=self._clean_optional_str(raw_work.get("publication_date")),
            workType=self._clean_optional_str(raw_work.get("type")),
            citedByCount=self._coerce_optional_int(raw_work.get("cited_by_count")),
            venue=self._clean_optional_str(source.get("display_name"))
            if isinstance(source, dict)
            else None,
            landingPageUrl=self._clean_optional_str(primary_location.get("landing_page_url"))
            if isinstance(primary_location, dict)
            else None,
            pdfUrl=self._clean_optional_str(primary_location.get("pdf_url"))
            if isinstance(primary_location, dict)
            else None,
            isOpenAccess=bool(open_access.get("is_oa")) if isinstance(open_access, dict) else None,
            abstract=self._decode_openalex_abstract(raw_work.get("abstract_inverted_index")),
            authors=authors,
            topics=topics,
        )

    async def _discovery_library_match_maps(
        self,
        *,
        require_ready: bool = False,
    ) -> tuple[dict[str, str], dict[str, str]]:
        records = await self._all_local_search_index_records(require_ready=require_ready)
        doi_matches: dict[str, str] = {}
        title_matches: dict[str, str] = {}
        for record in records:
            item_key = str(record.get("itemKey") or "").strip()
            if not item_key:
                continue
            normalized_doi = self._normalize_doi_safe(record.get("DOI"))
            if normalized_doi and normalized_doi not in doi_matches:
                doi_matches[normalized_doi] = item_key
            normalized_title = self._normalize_title_key(str(record.get("title") or ""))
            if normalized_title and normalized_title not in title_matches:
                title_matches[normalized_title] = item_key
        return doi_matches, title_matches

    def _match_discovery_work_in_library(
        self,
        *,
        item: DiscoveryWork,
        doi_matches: dict[str, str],
        title_matches: dict[str, str],
    ) -> tuple[str | None, str | None]:
        normalized_doi = self._normalize_doi_safe(item.doi)
        if normalized_doi:
            matched_item_key = doi_matches.get(normalized_doi)
            if matched_item_key is not None:
                return matched_item_key, "doi"
        normalized_title = self._normalize_title_key(item.title)
        matched_item_key = title_matches.get(normalized_title)
        if matched_item_key is not None:
            return matched_item_key, "title"
        return None, None

    def _normalize_note_records(self, children: list[dict[str, Any]]) -> list[NoteRecord]:
        notes = [
            self._normalize_note_record(child)
            for child in children
            if child.get("data", {}).get("itemType") == "note"
        ]
        notes.sort(
            key=lambda note: note.dateModified or "",
            reverse=True,
        )
        return notes

    def _normalize_note_record(self, raw_note: dict[str, Any]) -> NoteRecord:
        data = raw_note.get("data", {})
        raw_tags = self._normalize_tags(data.get("tags"))
        identity = self._note_renderer.extract_identity(raw_tags)
        agent: str | None = None
        note_type: str | None = None
        slot: str | None = None
        if identity is not None:
            agent, note_type, slot = identity
        body_html = str(data.get("note") or "")
        return NoteRecord(
            noteKey=str(raw_note.get("key") or ""),
            itemKey=self._clean_optional_str(data.get("parentItem")),
            bodyHtml=body_html,
            bodyText=self._note_renderer.to_plain_text(body_html),
            tags=self._public_note_tags(raw_tags),
            dateAdded=self._clean_optional_str(data.get("dateAdded")),
            dateModified=self._clean_optional_str(data.get("dateModified")),
            isAiNote=identity is not None,
            agent=agent,
            noteType=note_type,
            slot=slot,
        )

    @staticmethod
    def _normalize_creators(raw_creators: Any) -> list[Creator]:
        creators: list[Creator] = []
        if not isinstance(raw_creators, list):
            return creators
        for creator in raw_creators:
            if not isinstance(creator, dict):
                continue
            if creator.get("name"):
                display_name = str(creator["name"])
            else:
                first = str(creator.get("firstName") or "").strip()
                last = str(creator.get("lastName") or "").strip()
                display_name = " ".join(part for part in [first, last] if part).strip()
            if not display_name:
                continue
            creators.append(
                Creator(
                    displayName=display_name,
                    creatorType=BridgeService._clean_optional_str(creator.get("creatorType")),
                )
            )
        return creators

    @staticmethod
    def _normalize_tags(raw_tags: Any) -> list[str]:
        tags: list[str] = []
        if not isinstance(raw_tags, list):
            return tags
        for raw_tag in raw_tags:
            if isinstance(raw_tag, dict) and raw_tag.get("tag"):
                tags.append(str(raw_tag["tag"]))
            elif isinstance(raw_tag, str):
                tags.append(raw_tag)
        return tags

    @staticmethod
    def _normalize_relations(raw_relations: Any) -> list[str]:
        relations: list[str] = []
        if not isinstance(raw_relations, dict):
            return relations
        for relation_value in raw_relations.values():
            values = relation_value if isinstance(relation_value, list) else [relation_value]
            for value in values:
                if not isinstance(value, str) or not value or value in relations:
                    continue
                relations.append(value)
        return relations

    @staticmethod
    def _normalize_venue(data: dict[str, Any]) -> str | None:
        for field_name in (
            "publicationTitle",
            "proceedingsTitle",
            "bookTitle",
            "conferenceName",
            "publisher",
        ):
            value = BridgeService._clean_optional_str(data.get(field_name))
            if value:
                return value
        return None

    def _normalize_search_text(self, value: Any) -> str | None:
        text = self._clean_optional_str(value)
        if text is None:
            return None
        normalized = text.replace("\u00ad", "")
        normalized = normalized.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            return None
        if "<" in normalized and ">" in normalized:
            plain_text = self._note_renderer.to_plain_text(normalized)
            if plain_text:
                normalized = plain_text
        normalized = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", normalized)
        normalized = re.sub(r"[ \t]+\n", "\n", normalized)
        normalized = re.sub(r"^(abstract|summary)\s*\n+", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(
            r"^(abstract|summary)\s*[:\-–—]+\s*",
            "",
            normalized,
            flags=re.IGNORECASE,
        )
        normalized = re.sub(r"^(abstract|summary)(?=[A-Z])", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        return normalized.strip() or None

    @staticmethod
    def _extract_year(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        for token in value.replace("/", " ").replace("-", " ").split():
            if len(token) == 4 and token.isdigit():
                return token
        return None

    @staticmethod
    def _clean_optional_str(value: Any) -> str | None:
        if value is None or value is False:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _coerce_optional_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _format_unix_timestamp(value: Any) -> str | None:
        if value is None:
            return None
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            return None
        return datetime.fromtimestamp(timestamp, tz=UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _coerce_optional_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_optional_timestamp(value: str | None) -> float | None:
        if not value:
            return None
        try:
            normalized = value.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).timestamp()
        except ValueError:
            return None

    @staticmethod
    def _decode_openalex_abstract(value: Any) -> str | None:
        if not isinstance(value, dict) or not value:
            return None
        positions: list[tuple[int, str]] = []
        for token, token_positions in value.items():
            if not isinstance(token, str) or not isinstance(token_positions, list):
                continue
            for position in token_positions:
                if isinstance(position, int):
                    positions.append((position, token))
        if not positions:
            return None
        positions.sort(key=lambda item: item[0])
        return " ".join(token for _, token in positions)

    def _validate_note_body(self, body_markdown: str) -> None:
        if len(body_markdown) > self._settings.max_action_request_chars:
            raise BridgeError(
                code="BAD_REQUEST",
                message="bodyMarkdown exceeds configured request size limit",
                status_code=400,
            )

    def _find_matching_note(
        self,
        children: list[dict[str, Any]],
        identity_tags: list[str],
    ) -> dict[str, Any] | None:
        identity_set = set(identity_tags)
        for child in children:
            data = child.get("data", {})
            if data.get("itemType") != "note":
                continue
            tags = set(self._normalize_tags(data.get("tags")))
            if identity_set.issubset(tags):
                return child
        return None

    async def _find_note_by_request_id(
        self,
        *,
        item_key: str,
        request_id: str,
    ) -> dict[str, Any] | None:
        children = await self._zotero.get_children(item_key)
        return self._find_note_in_children_by_request_id(
            children=children,
            request_id=request_id,
        )

    def _find_note_in_children_by_request_id(
        self,
        *,
        children: list[dict[str, Any]],
        request_id: str | None,
    ) -> dict[str, Any] | None:
        request_tag = self._request_id_tag(request_id)
        if request_tag is None:
            return None
        for child in children:
            data = child.get("data", {})
            if data.get("itemType") != "note":
                continue
            tags = set(self._normalize_tags(data.get("tags")))
            if request_tag in tags:
                return child
        return None

    async def _get_note_item(self, note_key: str) -> dict[str, Any]:
        raw_note = await self._zotero.get_item(note_key)
        if raw_note.get("data", {}).get("itemType") != "note":
            raise BridgeError(
                code="NOTE_NOT_FOUND",
                message="Note not found",
                status_code=404,
            )
        return raw_note

    async def _resolve_upload_parent(
        self,
        *,
        item_key: str | None,
        doi: str | None,
        collection_key: str | None,
        tags: list[str],
        request_id: str | None,
        create_top_level: bool,
    ) -> str | None:
        if item_key:
            await self._zotero.get_item(item_key)
            return item_key
        if doi:
            added = await self.add_by_doi(
                AddByDOIRequest(
                    doi=doi,
                    collectionKey=collection_key,
                    tags=tags,
                    requestId=self._scoped_request_id(request_id, scope="upload-parent"),
                )
            )
            return added.itemKey
        if create_top_level:
            return None
        raise BridgeError(
            code="BAD_REQUEST",
            message="itemKey, doi, or createTopLevelAttachmentIfNeeded=true is required",
            status_code=400,
        )

    async def _find_existing_attachment(
        self,
        *,
        parent_item_key: str | None,
        filename: str,
        md5: str,
    ) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]]
        if parent_item_key:
            candidates = await self._zotero.get_children(parent_item_key)
        else:
            candidates = await self._zotero.find_top_level_attachments(filename=filename)

        for candidate in candidates:
            data = candidate.get("data", {})
            if data.get("itemType") != "attachment":
                continue
            if str(data.get("md5") or "") == md5:
                return candidate
            if (
                str(data.get("filename") or "") == filename
                and str(data.get("contentType") or "").lower() == "application/pdf"
                and not str(data.get("md5") or "")
            ):
                return candidate
        return None

    def _attachment_upload_response(
        self,
        *,
        status: UploadPdfStatus,
        attachment: dict[str, Any],
        parent_item_key: str | None,
    ) -> UploadPdfResponse:
        data = attachment.get("data", {})
        return UploadPdfResponse(
            status=status,
            itemKey=parent_item_key,
            attachmentKey=str(attachment.get("key")),
            filename=self._clean_optional_str(data.get("filename")),
            contentType=str(data.get("contentType") or "application/pdf"),
            title=self._clean_optional_str(data.get("title")),
        )

    def _get_cached_fulltext_payload(self, attachment_key: str) -> dict[str, Any] | None:
        if self._local_fulltext_store is None:
            return None
        return self._local_fulltext_store.get_payload(attachment_key)

    def _search_local_fulltext_item_keys(self, query: str, *, limit: int | None) -> list[str]:
        if self._local_fulltext_store is None:
            return []
        if limit is not None and limit <= 0:
            return []
        return self._local_fulltext_store.search_item_keys(query, limit=limit)

    def _build_cached_fulltext_response(
        self,
        *,
        item_key: str,
        attachment_key: str,
        cursor: int,
        max_chars: int,
        candidate_keys: list[str],
    ) -> FulltextResponse:
        cached_payload = self._get_cached_fulltext_payload(attachment_key)
        if cached_payload is None:
            raise BridgeError(
                code="FULLTEXT_NOT_AVAILABLE",
                message="Local full text cache is not available for this attachment",
                status_code=404,
            )
        return self._fulltext.build_chunk_response(
            item_key=item_key,
            attachment_key=attachment_key,
            fulltext_payload=cached_payload,
            cursor=cursor,
            max_chars=max_chars,
            candidate_keys=candidate_keys,
            source=self._fulltext.local_cache_source,
        )

    def _cache_uploaded_fulltext(
        self,
        *,
        attachment_key: str,
        item_key: str | None,
        filename: str,
        content: bytes,
    ) -> None:
        if self._local_fulltext_store is None or not attachment_key:
            return
        try:
            self._local_fulltext_store.cache_pdf(
                attachment_key=attachment_key,
                item_key=item_key or attachment_key,
                filename=filename,
                content=content,
            )
        except Exception:
            return

    def _prune_cached_fulltext_records_for_item(self, item_key: str) -> None:
        if self._local_fulltext_store is None:
            deleted = 0
        else:
            deleted = self._local_fulltext_store.delete_item_records(item_key)
        if deleted and self._local_search_index is not None:
            self._local_search_index.delete_record(item_key)

    async def _openalex_get(
        self,
        path: str,
        *,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            response = await self._http_client.get(
                f"{self._settings.openalex_api_base.rstrip('/')}{path}",
                params=params,
                timeout=60.0,
                follow_redirects=True,
            )
        except httpx.RequestError as exc:
            raise BridgeError(
                code="UPSTREAM_ERROR",
                message="OpenAlex request failed",
                status_code=502,
            ) from exc
        if response.status_code != 200:
            raise BridgeError(
                code="UPSTREAM_ERROR",
                message="OpenAlex request failed",
                status_code=502,
                upstream_status=response.status_code,
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise BridgeError(
                code="UPSTREAM_ERROR",
                message="Unexpected OpenAlex payload",
                status_code=502,
            )
        return payload

    async def _download_file(self, url: str) -> tuple[bytes, str | None]:
        current_url = self._normalize_remote_download_url(url)
        for _ in range(REMOTE_DOWNLOAD_MAX_REDIRECTS + 1):
            await self._assert_safe_remote_download_url(current_url)
            try:
                async with self._http_client.stream(
                    "GET",
                    str(current_url),
                    timeout=120.0,
                    follow_redirects=False,
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise BridgeError(
                                code="DOWNLOAD_FAILED",
                                message="Remote download redirect was missing a location header",
                                status_code=502,
                                upstream_status=response.status_code,
                            )
                        current_url = self._normalize_remote_download_url(
                            urljoin(str(current_url), location)
                        )
                        continue
                    if response.status_code != 200:
                        raise BridgeError(
                            code="DOWNLOAD_FAILED",
                            message="Unable to download remote PDF",
                            status_code=502,
                            upstream_status=response.status_code,
                        )
                    content = bytearray()
                    content_type = response.headers.get("Content-Type")
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > self._settings.max_upload_file_bytes:
                            raise BridgeError(
                                code="FILE_TOO_LARGE",
                                message="Downloaded file exceeds configured size limit",
                                status_code=413,
                            )
                    return bytes(content), content_type
            except httpx.RequestError as exc:
                raise BridgeError(
                    code="DOWNLOAD_FAILED",
                    message="Unable to download remote PDF",
                    status_code=502,
                ) from exc
        raise BridgeError(
            code="BAD_REQUEST",
            message="Remote file URL redirected too many times",
            status_code=400,
        )

    def _normalize_remote_download_url(self, url: str) -> httpx.URL:
        try:
            parsed = httpx.URL(url)
        except httpx.InvalidURL as exc:
            raise BridgeError(
                code="BAD_REQUEST",
                message="Remote file URL is invalid",
                status_code=400,
            ) from exc
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            raise BridgeError(
                code="BAD_REQUEST",
                message="Remote file URL must use http or https",
                status_code=400,
            )
        return parsed

    async def _assert_safe_remote_download_url(self, url: httpx.URL) -> None:
        host = (url.host or "").rstrip(".").lower()
        if not host:
            self._raise_unsafe_remote_download_url()
        if host == "localhost" or host.endswith(".localhost"):
            self._raise_unsafe_remote_download_url()

        resolved_addresses = await self._resolve_download_host_ips(
            host,
            port=url.port or self._default_port_for_scheme(url.scheme),
        )
        for address in resolved_addresses:
            if not address.is_global:
                self._raise_unsafe_remote_download_url()

    async def _resolve_download_host_ips(
        self,
        host: str,
        *,
        port: int,
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        try:
            return [ipaddress.ip_address(host)]
        except ValueError:
            pass

        try:
            addrinfo = await asyncio.get_running_loop().getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror:
            return []

        addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        seen: set[str] = set()
        for _, _, _, _, sockaddr in addrinfo:
            raw_address = sockaddr[0]
            try:
                address = ipaddress.ip_address(raw_address)
            except ValueError:
                continue
            key = str(address)
            if key in seen:
                continue
            seen.add(key)
            addresses.append(address)
        return addresses

    @staticmethod
    def _default_port_for_scheme(scheme: str) -> int:
        return 443 if scheme == "https" else 80

    def _raise_unsafe_remote_download_url(self) -> None:
        raise BridgeError(
            code="BAD_REQUEST",
            message="Remote file URL must resolve to a public host",
            status_code=400,
        )

    @staticmethod
    def _merge_tags(identity_tags: list[str], extra_tags: list[str]) -> list[str]:
        merged: list[str] = []
        for tag in [*identity_tags, *extra_tags]:
            if tag not in merged:
                merged.append(tag)
        return merged

    def _request_id_token(self, request_id: str | None) -> str | None:
        normalized_request_id = (request_id or "").strip()
        if not normalized_request_id:
            return None
        return hashlib.sha256(normalized_request_id.encode("utf-8")).hexdigest()[:32]

    def _request_id_tag(self, request_id: str | None) -> str | None:
        token = self._request_id_token(request_id)
        if token is None:
            return None
        return f"{self._settings.default_note_tag_prefix}:req:{token}"

    def _request_signature_tag(
        self,
        *,
        request_id: str | None,
        payload_fingerprint: str | None,
    ) -> str | None:
        token = self._request_id_token(request_id)
        if token is None or not payload_fingerprint:
            return None
        return f"{self._settings.default_note_tag_prefix}:reqsig:{token}:{payload_fingerprint}"

    def _request_metadata_tags(
        self,
        *,
        request_id: str | None,
        payload_fingerprint: str | None,
        outcome: str | None = None,
    ) -> list[str]:
        tags: list[str] = []
        request_tag = self._request_id_tag(request_id)
        signature_tag = self._request_signature_tag(
            request_id=request_id,
            payload_fingerprint=payload_fingerprint,
        )
        outcome_tag = self._request_outcome_tag(
            request_id=request_id,
            outcome=outcome,
        )
        if request_tag is not None:
            tags.append(request_tag)
        if isinstance(signature_tag, str):
            tags.append(signature_tag)
        if isinstance(outcome_tag, str):
            tags.append(outcome_tag)
        return tags

    def _request_outcome_tag(
        self,
        *,
        request_id: str | None,
        outcome: str | None,
    ) -> str | None:
        token = self._request_id_token(request_id)
        normalized_outcome = (outcome or "").strip().lower()
        if token is None or not normalized_outcome:
            return None
        return f"{self._settings.default_note_tag_prefix}:reqstatus:{token}:{normalized_outcome}"

    def _identity_note_tags(self, tags: list[str]) -> list[str]:
        identity = self._note_renderer.extract_identity(tags)
        if identity is None:
            return []
        agent, note_type, slot = identity
        return self._note_renderer.identity_tags(
            agent=agent,
            note_type=note_type,
            slot=slot,
        )

    def _mutable_note_tags(self, tags: list[str]) -> list[str]:
        hidden_tags = set(self._identity_note_tags(tags))
        hidden_tags.update(self._request_metadata_tags_from_tags(tags))
        return [tag for tag in tags if tag not in hidden_tags]

    def _public_note_tags(self, tags: list[str]) -> list[str]:
        request_tags = set(self._request_metadata_tags_from_tags(tags))
        return [tag for tag in tags if tag not in request_tags]

    def _request_metadata_tags_from_tags(self, tags: list[str]) -> list[str]:
        req_prefix = f"{self._settings.default_note_tag_prefix}:req:"
        reqsig_prefix = f"{self._settings.default_note_tag_prefix}:reqsig:"
        reqstatus_prefix = f"{self._settings.default_note_tag_prefix}:reqstatus:"
        return [
            tag
            for tag in tags
            if tag.startswith(req_prefix)
            or tag.startswith(reqsig_prefix)
            or tag.startswith(reqstatus_prefix)
        ]

    def _request_outcome_from_tags(
        self,
        *,
        tags: list[str],
        request_id: str,
    ) -> str | None:
        token = self._request_id_token(request_id)
        if token is None:
            return None
        prefix = f"{self._settings.default_note_tag_prefix}:reqstatus:{token}:"
        for tag in tags:
            if tag.startswith(prefix):
                outcome = tag[len(prefix) :].strip().lower()
                if outcome:
                    return outcome
        return None

    def _request_replay_state(
        self,
        *,
        tags: list[str],
        request_id: str,
        payload_fingerprint: str,
    ) -> str:
        request_tag = self._request_id_tag(request_id)
        if request_tag is None or request_tag not in tags:
            return "absent"
        expected_signature = self._request_signature_tag(
            request_id=request_id,
            payload_fingerprint=payload_fingerprint,
        )
        if expected_signature is None:
            return "conflict"
        return "matched" if expected_signature in tags else "conflict"

    def _note_write_payload_fingerprint(
        self,
        *,
        operation: str,
        payload: NoteWriteRequest,
    ) -> str:
        canonical_payload = {
            "operation": operation,
            "title": payload.title or None,
            "bodyMarkdown": payload.bodyMarkdown,
            "mode": payload.mode.value,
            "tags": self._canonicalize_tags(payload.tags),
        }
        raw = json.dumps(
            canonical_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _ai_note_payload_fingerprint(self, payload: UpsertAINoteRequest) -> str:
        canonical_payload = {
            "agent": payload.agent,
            "noteType": payload.noteType,
            "slot": payload.slot,
            "mode": payload.mode.value,
            "title": payload.title or None,
            "bodyMarkdown": payload.bodyMarkdown,
            "tags": self._canonicalize_tags(payload.tags),
            "model": payload.model or None,
            "sourceAttachmentKey": payload.sourceAttachmentKey or None,
            "sourceCursorStart": payload.sourceCursorStart,
            "sourceCursorEnd": payload.sourceCursorEnd,
        }
        raw = json.dumps(
            canonical_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _assert_create_replay_matches(
        self,
        *,
        existing_note: dict[str, Any],
        request_id: str,
        payload_fingerprint: str,
        rendered_html: str,
        user_tags: list[str],
    ) -> None:
        data = existing_note.get("data", {})
        existing_tags = self._normalize_tags(data.get("tags"))
        replay_state = self._request_replay_state(
            tags=existing_tags,
            request_id=request_id,
            payload_fingerprint=payload_fingerprint,
        )
        if replay_state == "matched":
            return
        if replay_state == "absent":
            self._raise_request_id_conflict()

        existing_html = str(data.get("note") or "")
        existing_public_tags = self._canonicalize_tags(self._mutable_note_tags(existing_tags)) or []
        expected_user_tags = self._canonicalize_tags(user_tags) or []
        if existing_html == rendered_html and existing_public_tags == expected_user_tags:
            return
        self._raise_request_id_conflict()

    def _assert_ai_note_replay_matches(
        self,
        *,
        existing_note: dict[str, Any],
        request_id: str,
        payload_fingerprint: str,
        identity_tags: list[str],
    ) -> list[str]:
        data = existing_note.get("data", {})
        if data.get("itemType") != "note":
            self._raise_request_id_conflict()
        existing_tags = self._normalize_tags(data.get("tags"))
        replay_state = self._request_replay_state(
            tags=existing_tags,
            request_id=request_id,
            payload_fingerprint=payload_fingerprint,
        )
        if replay_state != "matched":
            self._raise_request_id_conflict()
        if not set(identity_tags).issubset(existing_tags):
            self._raise_request_id_conflict()
        return existing_tags

    def _upsert_ai_note_replay_status(
        self,
        *,
        tags: list[str],
        request_id: str,
    ) -> UpsertAINoteStatus:
        outcome = self._request_outcome_from_tags(
            tags=tags,
            request_id=request_id,
        )
        if outcome == UpsertAINoteStatus.CREATED.value:
            return UpsertAINoteStatus.CREATED
        return UpsertAINoteStatus.UPDATED

    def _raise_request_id_conflict(self) -> None:
        raise BridgeError(
            code="REQUEST_ID_REUSED",
            message="requestId has already been used with a different note write payload",
            status_code=409,
        )

    def _raise_item_update_conflict(self) -> None:
        raise BridgeError(
            code="WRITE_CONFLICT",
            message="Zotero item update conflicted after retry",
            status_code=409,
            upstream_status=412,
        )

    @staticmethod
    def _canonicalize_tags(tags: list[str] | None) -> list[str] | None:
        if tags is None:
            return None
        unique = {str(tag).strip() for tag in tags if str(tag).strip()}
        return sorted(unique)

    @staticmethod
    def _build_write_token(request_id: str | None) -> str:
        if request_id:
            return hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
        return secrets.token_hex(16)

    @staticmethod
    def _scoped_request_id(request_id: str | None, *, scope: str) -> str | None:
        if not request_id:
            return None
        return f"{scope}:{request_id}"

    @staticmethod
    def _looks_like_pdf(content: bytes) -> bool:
        header = content[:1024]
        marker_index = header.find(b"%PDF-")
        if marker_index == -1:
            return False
        return header[:marker_index].strip(b"\x00\t\n\r\f ") == b""
