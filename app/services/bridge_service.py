from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import secrets
import socket
import time
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
    AINoteSummary,
    AttachmentSummary,
    CitationResponse,
    Creator,
    FulltextResponse,
    HealthConfig,
    HealthResponse,
    ItemDetailResponse,
    ItemNotesResponse,
    NoteDeleteResponse,
    NoteDeleteStatus,
    NoteDetailResponse,
    NoteRecord,
    NoteWriteRequest,
    NoteWriteResponse,
    NoteWriteStatus,
    SearchItem,
    SearchResponse,
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
from app.services.note_renderer import NoteRenderer
from app.services.zotero_client import ZoteroClient

REMOTE_DOWNLOAD_MAX_REDIRECTS = 5
NOTE_UPDATE_MAX_ATTEMPTS = 3


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
    ) -> None:
        self._settings = settings
        self._http_client = http_client
        self._zotero = zotero_client
        self._doi_resolver = doi_resolver
        self._note_renderer = note_renderer
        self._fulltext = fulltext_service
        self._local_fulltext_store = local_fulltext_store

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
        limit: int,
        include_fulltext: bool,
        include_attachments: bool,
        include_notes: bool,
    ) -> SearchResponse:
        upstream_limit = min(max(limit * 4, limit), 100)
        raw_items = await self._zotero.search_items_raw(
            q=q,
            limit=upstream_limit,
            include_fulltext=include_fulltext,
        )

        parent_keys: list[str] = []
        local_cache_keys: set[str] = set()
        seen: set[str] = set()
        for raw_item in raw_items:
            data = raw_item.get("data", {})
            item_type = data.get("itemType")
            if item_type in {"attachment", "note"}:
                key = data.get("parentItem")
            else:
                key = raw_item.get("key")
            if not isinstance(key, str) or key in seen:
                continue
            seen.add(key)
            parent_keys.append(key)

        if include_fulltext:
            for item_key in self._search_local_fulltext_item_keys(
                q,
                limit=upstream_limit,
            ):
                if item_key in seen:
                    continue
                seen.add(item_key)
                local_cache_keys.add(item_key)
                parent_keys.append(item_key)

        items: list[SearchItem] = []
        for item_key in parent_keys:
            try:
                item = await self.get_parent_item(
                    item_key=item_key,
                    include_attachments=include_attachments,
                    include_notes=include_notes,
                )
            except BridgeError as exc:
                if exc.code != "ITEM_NOT_FOUND":
                    raise
                if item_key in local_cache_keys:
                    self._prune_cached_fulltext_records_for_item(item_key)
                continue
            items.append(item)
            if len(items) >= limit:
                break
        return SearchResponse(items=items, count=len(items))

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
            return NoteWriteResponse(
                status=NoteWriteStatus.UPDATED,
                noteKey=note_key,
                itemKey=self._clean_optional_str(data.get("parentItem")),
            )
        self._raise_item_update_conflict()

    async def delete_note(self, *, note_key: str) -> NoteDeleteResponse:
        raw_note = await self._get_note_item(note_key)
        data = raw_note.get("data", {})
        await self._zotero.delete_item(
            item_key=note_key,
            version=int(raw_note.get("version", 0)),
        )
        return NoteDeleteResponse(
            status=NoteDeleteStatus.DELETED,
            noteKey=note_key,
            itemKey=self._clean_optional_str(data.get("parentItem")),
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
            if not isinstance(item_doi, str):
                continue
            try:
                matches = self._doi_resolver.normalize_doi(item_doi) == normalized_doi
            except BridgeError:
                matches = False
            if matches:
                if data.get("itemType") in {"attachment", "note"} and data.get("parentItem"):
                    return await self._zotero.get_item(str(data["parentItem"]))
                return item
        return None

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
        include_attachments: bool,
        include_notes: bool,
    ) -> SearchItem:
        data = raw_item.get("data", {})
        children: list[dict[str, Any]] = []
        if include_attachments or include_notes:
            children = await self._zotero.get_children(str(raw_item.get("key")))
        attachments = self._normalize_attachments(children) if include_attachments else []
        ai_notes = self._normalize_ai_notes(children) if include_notes else []
        return SearchItem(
            itemKey=str(raw_item.get("key")),
            itemType=str(data.get("itemType") or ""),
            title=str(data.get("title") or "(untitled)"),
            year=self._extract_year(data.get("date")),
            DOI=self._clean_optional_str(data.get("DOI")),
            creators=self._normalize_creators(data.get("creators")),
            tags=self._normalize_tags(data.get("tags")),
            collectionKeys=[
                str(value) for value in data.get("collections", []) if isinstance(value, str)
            ],
            attachments=attachments,
            aiNotes=ai_notes,
        )

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
    def _extract_year(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        for token in value.replace("/", " ").replace("-", " ").split():
            if len(token) == 4 and token.isdigit():
                return token
        return None

    @staticmethod
    def _clean_optional_str(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

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

    def _search_local_fulltext_item_keys(self, query: str, *, limit: int) -> list[str]:
        if self._local_fulltext_store is None or limit <= 0:
            return []
        return self._local_fulltext_store.search_item_keys(query, limit=None)

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
                item_key=item_key,
                filename=filename,
                content=content,
            )
        except Exception:
            return

    def _prune_cached_fulltext_records_for_item(self, item_key: str) -> None:
        if self._local_fulltext_store is None:
            return
        self._local_fulltext_store.delete_item_records(item_key)

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
