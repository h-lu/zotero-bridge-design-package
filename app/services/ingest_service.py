from __future__ import annotations

import hashlib
import time
from pathlib import PurePosixPath
from typing import Any

from app.errors import BridgeError
from app.models import (
    AddByDOIRequest,
    AddByDOIResponse,
    AddByDOIStatus,
    ImportDiscoveryHitRequest,
    ImportDiscoveryHitResponse,
    ImportMetadataRequest,
    ImportMetadataResponse,
    ImportStatus,
    UploadPdfActionRequest,
    UploadPdfResponse,
    UploadPdfStatus,
)


class IngestService:
    def __init__(self, bridge: Any) -> None:
        self._bridge = bridge

    async def add_by_doi(self, payload: AddByDOIRequest) -> AddByDOIResponse:
        bridge = self._bridge
        normalized_doi = bridge._doi_resolver.normalize_doi(payload.doi)
        existing = await bridge._find_item_by_doi(normalized_doi)
        if existing is not None:
            normalized = await bridge._normalize_parent_item(
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

        metadata = await bridge._doi_resolver.resolve(normalized_doi)
        existing = await bridge._find_item_by_doi(
            normalized_doi,
            title_hint=bridge._doi_resolver._first_text(metadata.get("title")),
        )
        if existing is not None:
            normalized = await bridge._normalize_parent_item(
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
        item_type = bridge._doi_resolver.guess_zotero_item_type(metadata)
        template = await bridge._zotero.get_item_template(item_type)
        item_payload = bridge._doi_resolver.build_zotero_item(
            metadata=metadata,
            template=template,
            doi=normalized_doi,
            collection_key=payload.collectionKey,
            default_collection_key=bridge._settings.default_collection_key,
            tags=payload.tags,
        )
        write_token = bridge._build_write_token(payload.requestId)
        try:
            created_key = (
                await bridge._zotero.create_items([item_payload], write_token=write_token)
            )[0]
        except BridgeError as exc:
            if exc.code == "WRITE_CONFLICT":
                existing = await bridge._find_item_by_doi(normalized_doi)
                if existing is not None:
                    normalized = await bridge._normalize_parent_item(
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

        created_item = await bridge._zotero.get_item(created_key)
        normalized = await bridge._normalize_parent_item(
            created_item,
            include_attachments=False,
            include_notes=False,
        )
        await bridge._refresh_local_search_index_item(normalized.itemKey)
        return AddByDOIResponse(
            status=AddByDOIStatus.CREATED,
            itemKey=normalized.itemKey,
            title=normalized.title,
            DOI=normalized.DOI,
        )

    async def import_metadata(self, payload: ImportMetadataRequest) -> ImportMetadataResponse:
        bridge = self._bridge
        normalized_doi = bridge._normalize_doi_safe(payload.doi)
        existing_item: dict[str, Any] | None = None
        dedupe_strategy = "none"
        if normalized_doi:
            existing_item = await bridge._find_item_by_doi(normalized_doi)
            if existing_item is not None:
                dedupe_strategy = "doi"
        if existing_item is None:
            existing_item = await bridge._find_item_by_weak_metadata_match(payload)
            if existing_item is not None:
                dedupe_strategy = "title_author_year"

        if existing_item is not None:
            item_key = str(existing_item.get("key") or "")
            if payload.updateIfExists:
                await bridge._update_imported_item(existing_item=existing_item, payload=payload)
                refreshed = await bridge._zotero.get_item(item_key)
                title = str(refreshed.get("data", {}).get("title") or payload.title)
                return ImportMetadataResponse(
                    status=ImportStatus.UPDATED,
                    itemKey=item_key,
                    title=title,
                    dedupeStrategy=dedupe_strategy,
                )
            return ImportMetadataResponse(
                status=ImportStatus.EXISTING,
                itemKey=item_key,
                title=str(existing_item.get("data", {}).get("title") or payload.title),
                dedupeStrategy=dedupe_strategy,
            )

        created_key = await bridge._create_metadata_item(payload)
        created_item = await bridge._zotero.get_item(created_key)
        await bridge._refresh_local_search_index_item(created_key)
        return ImportMetadataResponse(
            status=ImportStatus.CREATED,
            itemKey=created_key,
            title=str(created_item.get("data", {}).get("title") or payload.title),
            dedupeStrategy=dedupe_strategy,
        )

    async def import_discovery_hit(
        self,
        payload: ImportDiscoveryHitRequest,
    ) -> ImportDiscoveryHitResponse:
        bridge = self._bridge
        metadata_payload = bridge._metadata_request_from_discovery_hit(payload)
        imported = await self.import_metadata(metadata_payload)
        attachment: UploadPdfResponse | None = None
        if payload.attachPdfFromOpenAccessUrl and payload.pdfUrl:
            attachment = await self.upload_pdf_from_action(
                UploadPdfActionRequest.model_validate(
                    {
                        "itemKey": imported.itemKey,
                        "fileUrl": payload.pdfUrl,
                        "requestId": bridge._scoped_request_id(
                            payload.requestId,
                            scope="discovery-pdf",
                        ),
                    }
                )
            )
        return ImportDiscoveryHitResponse(
            status=imported.status,
            itemKey=imported.itemKey,
            title=imported.title,
            dedupeStrategy=imported.dedupeStrategy,
            attachment=attachment,
        )

    async def upload_pdf_from_action(
        self,
        payload: UploadPdfActionRequest,
    ) -> UploadPdfResponse:
        bridge = self._bridge
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

        remote_file = await bridge._remote_fetch_guard.fetch_pdf(file_url)
        return await self.upload_pdf_bytes(
            content=remote_file.content,
            filename=filename or "upload.pdf",
            content_type=content_type or remote_file.content_type or "application/pdf",
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
        bridge = self._bridge
        if len(content) > bridge._settings.max_upload_file_bytes:
            raise BridgeError(
                code="FILE_TOO_LARGE",
                message="Uploaded file exceeds configured size limit",
                status_code=413,
            )
        if not bridge._looks_like_pdf(content):
            raise BridgeError(
                code="BAD_REQUEST",
                message="Only PDF uploads are supported",
                status_code=400,
            )

        parent_item_key = await bridge._resolve_upload_parent(
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
            existing = await bridge._find_existing_attachment(
                parent_item_key=parent_item_key,
                filename=filename,
                md5=md5,
            )
            if existing is not None:
                existing_data = existing.get("data", {})
                existing_md5 = str(existing_data.get("md5") or "")
                if existing_md5 == md5:
                    await bridge._refresh_local_search_index_item(
                        parent_item_key or str(existing.get("key") or "")
                    )
                    return bridge._attachment_upload_response(
                        status=UploadPdfStatus.UPDATED,
                        attachment=existing,
                        parent_item_key=parent_item_key,
                    )
                attachment_key = str(existing.get("key") or "")
                attachment_status = UploadPdfStatus.UPDATED

        if not attachment_key:
            template = await bridge._zotero.get_item_template(
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
                    key
                    for key in [collection_key or bridge._settings.default_collection_key]
                    if key
                ]

            try:
                attachment_key = (
                    await bridge._zotero.create_items(
                        [attachment_payload],
                        write_token=bridge._build_write_token(request_id),
                    )
                )[0]
            except BridgeError as exc:
                if exc.code == "WRITE_CONFLICT" and request_id:
                    existing = await bridge._find_existing_attachment(
                        parent_item_key=parent_item_key,
                        filename=filename,
                        md5=md5,
                    )
                    if existing is not None:
                        existing_data = existing.get("data", {})
                        existing_md5 = str(existing_data.get("md5") or "")
                        if existing_md5 == md5:
                            return bridge._attachment_upload_response(
                                status=UploadPdfStatus.UPDATED,
                                attachment=existing,
                                parent_item_key=parent_item_key,
                            )
                        attachment_key = str(existing.get("key") or "")
                        attachment_status = UploadPdfStatus.UPDATED
                if not attachment_key:
                    raise

        authorization = await bridge._zotero.authorize_upload(
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
            await bridge._zotero.upload_to_authorized_url(authorization, content)
            await bridge._zotero.register_upload(
                attachment_key=attachment_key,
                upload_key=upload_key,
            )

        attachment = await bridge._zotero.get_item(attachment_key)
        await bridge._refresh_local_search_index_item(parent_item_key or attachment_key)
        return bridge._attachment_upload_response(
            status=attachment_status,
            attachment=attachment,
            parent_item_key=parent_item_key,
        )
