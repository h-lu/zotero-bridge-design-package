from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx

from app.config import Settings
from app.errors import BridgeError
from app.models import (
    AttachmentDetailResponse,
    AttachmentHandoffMode,
    AttachmentHandoffRequest,
    AttachmentHandoffResponse,
    AttachmentListResponse,
    AttachmentRecord,
)
from app.services.zotero_client import ZoteroClient


class AttachmentZoteroClient(Protocol):
    async def get_item(self, item_key: str) -> dict[str, Any]: ...

    async def get_children(self, item_key: str) -> list[dict[str, Any]]: ...

    async def download_attachment_file(self, attachment_key: str) -> tuple[bytes, str | None]: ...


@dataclass(slots=True)
class AttachmentDownload:
    attachment: AttachmentRecord
    content: bytes
    content_type: str


@dataclass(slots=True)
class _HandoffTokenRecord:
    attachment_key: str
    expires_at: datetime
    zotero_api_key: str
    zotero_library_type: str
    zotero_library_id: str


@dataclass(slots=True)
class _ScopedDownloadContext:
    settings: Settings
    http_client: httpx.AsyncClient


class AttachmentService:
    def __init__(
        self,
        *,
        settings: Settings,
        zotero_client: AttachmentZoteroClient,
        http_client: httpx.AsyncClient,
        tokens: dict[str, _HandoffTokenRecord] | None = None,
    ) -> None:
        self._settings = settings
        self._zotero = zotero_client
        self._http_client = http_client
        self._tokens = tokens if tokens is not None else {}

    async def list_item_attachments(self, item_key: str) -> AttachmentListResponse:
        raw_item = await self._zotero.get_item(item_key)
        item_type = str(raw_item.get("data", {}).get("itemType") or "")
        if item_type == "attachment":
            attachments = [self._normalize_attachment(raw_item)]
        else:
            children = await self._zotero.get_children(item_key)
            attachments = [
                self._normalize_attachment(child)
                for child in children
                if child.get("data", {}).get("itemType") == "attachment"
            ]
        return AttachmentListResponse(
            itemKey=item_key,
            attachments=attachments,
            count=len(attachments),
        )

    async def get_attachment_detail(self, attachment_key: str) -> AttachmentDetailResponse:
        attachment = self._normalize_attachment(await self._get_attachment_item(attachment_key))
        return AttachmentDetailResponse(attachment=attachment)

    async def create_handoff(
        self,
        *,
        attachment_key: str,
        payload: AttachmentHandoffRequest,
        download_url: str,
    ) -> AttachmentHandoffResponse:
        if payload.mode != AttachmentHandoffMode.PROXY_DOWNLOAD:
            raise BridgeError(
                code="BAD_REQUEST",
                message="Only proxy_download handoff is supported",
                status_code=400,
            )
        attachment = self._normalize_attachment(await self._get_attachment_item(attachment_key))
        if not attachment.downloadable:
            raise BridgeError(
                code="BAD_REQUEST",
                message="Attachment is not downloadable through this bridge",
                status_code=400,
            )
        ttl_seconds = payload.expiresInSeconds or self._settings.download_handoff_ttl_seconds
        expires_at = datetime.now(UTC) + timedelta(seconds=max(ttl_seconds, 60))
        token = self._issue_token(attachment_key=attachment_key, expires_at=expires_at)
        return AttachmentHandoffResponse(
            attachmentKey=attachment.attachmentKey,
            filename=attachment.filename,
            contentType=attachment.contentType,
            mode=payload.mode,
            downloadUrl=download_url.format(token=token),
            expiresAt=expires_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        )

    async def download_attachment_by_token(self, token: str) -> AttachmentDownload:
        record = self._consume_token(token)
        scoped_context = self._scoped_download_context(record)
        scoped_zotero = ZoteroClient(
            settings=scoped_context.settings,
            client=scoped_context.http_client,
        )
        attachment = self._normalize_attachment(await self._get_attachment_item_from_client(
            scoped_zotero,
            record.attachment_key,
        ))
        if not attachment.downloadable:
            raise BridgeError(
                code="BAD_REQUEST",
                message="Attachment is not downloadable through this bridge",
                status_code=400,
            )
        content, upstream_content_type = await scoped_zotero.download_attachment_file(
            record.attachment_key
        )
        content_type = (
            self._normalize_content_type(upstream_content_type)
            or self._normalize_content_type(attachment.contentType)
            or "application/octet-stream"
        )
        return AttachmentDownload(
            attachment=attachment,
            content=content,
            content_type=content_type,
        )

    async def _get_attachment_item(self, attachment_key: str) -> dict:
        return await self._get_attachment_item_from_client(self._zotero, attachment_key)

    async def _get_attachment_item_from_client(
        self,
        zotero_client: AttachmentZoteroClient,
        attachment_key: str,
    ) -> dict:
        raw_item = await zotero_client.get_item(attachment_key)
        if raw_item.get("data", {}).get("itemType") != "attachment":
            raise BridgeError(
                code="ATTACHMENT_NOT_FOUND",
                message="Attachment not found",
                status_code=404,
            )
        return raw_item

    def _normalize_attachment(self, raw_item: dict) -> AttachmentRecord:
        data = raw_item.get("data", {})
        content_type = str(data.get("contentType") or "")
        filename = self._clean_optional_str(data.get("filename"))
        title = str(data.get("title") or filename or "Attachment")
        link_mode = str(data.get("linkMode") or "")
        is_pdf = content_type.lower() == "application/pdf" or (filename or "").lower().endswith(
            ".pdf"
        )
        downloadable = link_mode not in {"linked_url", "linked_file"} and bool(
            content_type or filename
        )
        return AttachmentRecord(
            attachmentKey=str(raw_item.get("key") or ""),
            parentItemKey=self._clean_optional_str(data.get("parentItem")),
            title=title,
            contentType=content_type,
            filename=filename,
            linkMode=link_mode,
            md5=self._clean_optional_str(data.get("md5")),
            mtime=self._clean_optional_str(data.get("mtime")),
            isPdf=is_pdf,
            downloadable=downloadable,
        )

    def _issue_token(self, *, attachment_key: str, expires_at: datetime) -> str:
        self._prune_expired_tokens()
        token = f"tkn_{secrets.token_urlsafe(24)}"
        self._tokens[token] = _HandoffTokenRecord(
            attachment_key=attachment_key,
            expires_at=expires_at,
            zotero_api_key=self._settings.zotero_api_key,
            zotero_library_type=self._settings.zotero_library_type,
            zotero_library_id=self._settings.zotero_library_id,
        )
        return token

    def _consume_token(self, token: str) -> _HandoffTokenRecord:
        record = self._tokens.pop(token, None)
        if record is None:
            self._prune_expired_tokens()
            raise BridgeError(
                code="INVALID_DOWNLOAD_TOKEN",
                message="Attachment download token is invalid",
                status_code=404,
            )
        if record.expires_at <= datetime.now(UTC):
            raise BridgeError(
                code="EXPIRED_DOWNLOAD_TOKEN",
                message="Attachment download token has expired",
                status_code=410,
            )
        return record

    def _scoped_download_context(self, record: _HandoffTokenRecord) -> _ScopedDownloadContext:
        scoped_settings = self._settings.model_copy(
            update={
                "zotero_api_key": record.zotero_api_key,
                "zotero_library_type": record.zotero_library_type,
                "zotero_library_id": record.zotero_library_id,
            }
        )
        return _ScopedDownloadContext(
            settings=scoped_settings,
            http_client=self._http_client,
        )

    def _prune_expired_tokens(self) -> None:
        now = datetime.now(UTC)
        expired = [token for token, record in self._tokens.items() if record.expires_at <= now]
        for token in expired:
            self._tokens.pop(token, None)

    @staticmethod
    def _normalize_content_type(value: str | None) -> str | None:
        if not value:
            return None
        normalized = value.split(";", 1)[0].strip()
        return normalized or None

    @staticmethod
    def _clean_optional_str(value: object) -> str | None:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None
