from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.errors import BridgeError
from app.models import FulltextResponse, FulltextSource


@dataclass(slots=True)
class AttachmentSelection:
    attachment: dict[str, Any]
    candidate_keys: list[str]


class FulltextService:
    def __init__(self, default_max_chars: int, hard_max_chars: int) -> None:
        self._default_max_chars = default_max_chars
        self._hard_max_chars = hard_max_chars

    @property
    def default_max_chars(self) -> int:
        return self._default_max_chars

    @property
    def hard_max_chars(self) -> int:
        return self._hard_max_chars

    @property
    def local_cache_source(self) -> FulltextSource:
        return FulltextSource.LOCAL_CACHE

    def select_attachment(
        self,
        children: list[dict[str, Any]],
        attachment_key: str | None = None,
    ) -> AttachmentSelection:
        attachments = [
            child
            for child in children
            if child.get("data", {}).get("itemType") == "attachment"
            and self._is_pdf_attachment(child)
        ]
        if attachment_key:
            for attachment in attachments:
                if (
                    attachment.get("key") == attachment_key
                    or attachment.get("data", {}).get("key") == attachment_key
                ):
                    return AttachmentSelection(
                        attachment=attachment,
                        candidate_keys=[attachment_key],
                    )
            raise BridgeError(
                code="ATTACHMENT_NOT_FOUND",
                message="Attachment not found",
                status_code=404,
            )

        if not attachments:
            raise BridgeError(
                code="FULLTEXT_NOT_AVAILABLE",
                message="No PDF attachment is available for this item",
                status_code=404,
            )

        if len(attachments) == 1:
            attachment = attachments[0]
            key = self._attachment_key(attachment)
            return AttachmentSelection(attachment=attachment, candidate_keys=[key])

        attachments.sort(
            key=lambda item: str(item.get("data", {}).get("dateModified") or ""),
            reverse=True,
        )
        return AttachmentSelection(
            attachment=attachments[0],
            candidate_keys=[self._attachment_key(item) for item in attachments],
        )

    def build_chunk_response(
        self,
        *,
        item_key: str,
        attachment_key: str,
        fulltext_payload: dict[str, Any],
        cursor: int,
        max_chars: int,
        candidate_keys: list[str],
        source: FulltextSource = FulltextSource.ZOTERO_WEB_API,
    ) -> FulltextResponse:
        bounded_max_chars = min(max_chars, self._hard_max_chars)
        if bounded_max_chars < 1000:
            raise BridgeError(
                code="BAD_REQUEST",
                message="maxChars must be at least 1000",
                status_code=400,
            )
        text = self._normalize_text(str(fulltext_payload.get("content", "")))
        start = min(cursor, len(text))
        end = self._find_chunk_end(text, start, bounded_max_chars)
        content = text[start:end]
        next_cursor = end if end < len(text) else None
        return FulltextResponse(
            itemKey=item_key,
            attachmentKey=attachment_key,
            cursor=start,
            nextCursor=next_cursor,
            done=next_cursor is None,
            content=content,
            source=source,
            indexedPages=self._coerce_optional_int(fulltext_payload.get("indexedPages")),
            totalPages=self._coerce_optional_int(fulltext_payload.get("totalPages")),
            attachmentCandidates=candidate_keys,
        )

    @staticmethod
    def _attachment_key(attachment: dict[str, Any]) -> str:
        return str(attachment.get("key") or attachment.get("data", {}).get("key"))

    @staticmethod
    def _is_pdf_attachment(item: dict[str, Any]) -> bool:
        data = item.get("data", {})
        content_type = str(data.get("contentType") or "").lower()
        filename = str(data.get("filename") or "").lower()
        return content_type == "application/pdf" or filename.endswith(".pdf")

    @staticmethod
    def _normalize_text(text: str) -> str:
        return text.replace("\r\n", "\n").replace("\r", "\n")

    def _find_chunk_end(self, text: str, start: int, max_chars: int) -> int:
        hard_end = min(start + max_chars, len(text))
        if hard_end >= len(text):
            return len(text)

        search_floor = start + int(max_chars * 0.6)
        search_floor = min(search_floor, hard_end)

        for marker in ("\n\n", "\n", " "):
            split = text.rfind(marker, search_floor, hard_end)
            if split > start:
                return split + len(marker.rstrip())
        return hard_end

    @staticmethod
    def _coerce_optional_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
