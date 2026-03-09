from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import Any

from pypdf import PdfReader


class LocalFulltextStore:
    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def cache_pdf(
        self,
        *,
        attachment_key: str,
        item_key: str | None,
        filename: str | None,
        content: bytes,
    ) -> bool:
        try:
            payload = self._extract_payload(content)
        except Exception:
            return False
        if not str(payload.get("content") or "").strip():
            return False
        self.write_payload(
            attachment_key=attachment_key,
            item_key=item_key,
            filename=filename,
            fulltext_payload=payload,
        )
        return True

    def write_payload(
        self,
        *,
        attachment_key: str,
        item_key: str | None,
        filename: str | None,
        fulltext_payload: dict[str, Any],
    ) -> None:
        record = {
            "attachmentKey": attachment_key,
            "itemKey": item_key,
            "filename": filename,
            "content": str(fulltext_payload.get("content") or ""),
            "indexedPages": self._coerce_optional_int(fulltext_payload.get("indexedPages")),
            "totalPages": self._coerce_optional_int(fulltext_payload.get("totalPages")),
        }
        self._record_path(attachment_key).write_text(
            json.dumps(record, ensure_ascii=False),
            encoding="utf-8",
        )

    def get_payload(self, attachment_key: str) -> dict[str, Any] | None:
        path = self._record_path(attachment_key)
        if not path.exists():
            return None
        payload = self._read_record(path)
        if payload is None:
            return None
        content = str(payload.get("content") or "")
        if not content.strip():
            return None
        normalized: dict[str, Any] = {"content": content}
        indexed_pages = self._coerce_optional_int(payload.get("indexedPages"))
        total_pages = self._coerce_optional_int(payload.get("totalPages"))
        if indexed_pages is not None:
            normalized["indexedPages"] = indexed_pages
        if total_pages is not None:
            normalized["totalPages"] = total_pages
        return normalized

    def search_item_keys(self, query: str, *, limit: int | None) -> list[str]:
        normalized_query = query.strip().casefold()
        if not normalized_query:
            return []
        if limit is not None and limit <= 0:
            return []

        matches: list[tuple[float, str]] = []
        for path in self._cache_dir.glob("*.json"):
            record = self._read_record(path)
            if record is None:
                continue
            item_key = str(record.get("itemKey") or "").strip()
            if not item_key:
                continue
            content = str(record.get("content") or "")
            filename = str(record.get("filename") or "")
            content_matches = normalized_query in content.casefold()
            filename_matches = normalized_query in filename.casefold()
            if not content_matches and not filename_matches:
                continue
            try:
                score = path.stat().st_mtime
            except OSError:
                score = 0.0
            matches.append((score, item_key))

        item_keys: list[str] = []
        seen: set[str] = set()
        for _, item_key in sorted(matches, key=lambda item: item[0], reverse=True):
            if item_key in seen:
                continue
            seen.add(item_key)
            item_keys.append(item_key)
            if limit is not None and len(item_keys) >= limit:
                break
        return item_keys

    def delete_attachment(self, attachment_key: str) -> None:
        try:
            self._record_path(attachment_key).unlink(missing_ok=True)
        except OSError:
            return

    def delete_item_records(self, item_key: str) -> int:
        normalized_item_key = item_key.strip()
        if not normalized_item_key:
            return 0

        deleted = 0
        for path in self._cache_dir.glob("*.json"):
            record = self._read_record(path)
            if record is None:
                continue
            if str(record.get("itemKey") or "").strip() != normalized_item_key:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            deleted += 1
        return deleted

    def _record_path(self, attachment_key: str) -> Path:
        return self._cache_dir / f"{attachment_key}.json"

    @staticmethod
    def _read_record(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _extract_payload(content: bytes) -> dict[str, Any]:
        reader = PdfReader(BytesIO(content))
        page_texts: list[str] = []
        indexed_pages = 0
        for page in reader.pages:
            text = str(page.extract_text() or "").strip()
            if not text:
                continue
            indexed_pages += 1
            page_texts.append(text)
        return {
            "content": "\n\n".join(page_texts),
            "indexedPages": indexed_pages or None,
            "totalPages": len(reader.pages),
        }

    @staticmethod
    def _coerce_optional_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
