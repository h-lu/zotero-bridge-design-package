from __future__ import annotations

import json
import re
import time
from io import BytesIO
from pathlib import Path
from typing import Any

from pypdf import PdfReader


class LocalFulltextStore:
    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, dict[str, Any]] | None = None

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
            "cachedAt": time.time(),
        }
        self._ensure_records_loaded()
        assert self._records is not None
        self._records[attachment_key] = record
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

        self._ensure_records_loaded()
        assert self._records is not None

        matches: list[tuple[float, str]] = []
        for record in self._records.values():
            item_key = self._record_item_key(record)
            if not item_key:
                continue
            content = self._normalize_search_text(str(record.get("content") or ""))
            filename = self._normalize_search_text(str(record.get("filename") or ""))
            content_matches = normalized_query in content.casefold()
            filename_matches = normalized_query in filename.casefold()
            if not content_matches and not filename_matches:
                continue
            score = self._coerce_optional_float(record.get("cachedAt")) or 0.0
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

    def first_match_snippet(self, *, item_key: str, query: str, radius: int = 80) -> str | None:
        normalized_item_key = item_key.strip()
        normalized_query = query.strip().casefold()
        if not normalized_item_key or not normalized_query:
            return None

        self._ensure_records_loaded()
        assert self._records is not None

        best_match: tuple[float, str] | None = None
        for record in self._records.values():
            if self._record_item_key(record) != normalized_item_key:
                continue
            content = self._normalize_search_text(str(record.get("content") or ""))
            snippet = self._match_snippet(content, normalized_query, radius=radius)
            if snippet is None:
                continue
            score = self._coerce_optional_float(record.get("cachedAt")) or 0.0
            if best_match is None or score > best_match[0]:
                best_match = (score, snippet)
        return None if best_match is None else best_match[1]

    def item_search_text(self, item_key: str) -> str | None:
        normalized_item_key = item_key.strip()
        if not normalized_item_key:
            return None

        self._ensure_records_loaded()
        assert self._records is not None

        contents: list[tuple[float, str]] = []
        for record in self._records.values():
            if self._record_item_key(record) != normalized_item_key:
                continue
            content = self._normalize_search_text(str(record.get("content") or ""))
            if not content:
                continue
            score = self._coerce_optional_float(record.get("cachedAt")) or 0.0
            contents.append((score, content))
        if not contents:
            return None
        ordered = [
            content
            for _, content in sorted(contents, key=lambda entry: entry[0], reverse=True)
        ]
        return "\n\n".join(ordered)

    def delete_attachment(self, attachment_key: str) -> None:
        self._ensure_records_loaded()
        assert self._records is not None
        self._records.pop(attachment_key, None)
        try:
            self._record_path(attachment_key).unlink(missing_ok=True)
        except OSError:
            return

    def delete_item_records(self, item_key: str) -> int:
        normalized_item_key = item_key.strip()
        if not normalized_item_key:
            return 0

        self._ensure_records_loaded()
        assert self._records is not None

        deleted = 0
        for attachment_key, record in list(self._records.items()):
            if self._record_item_key(record) != normalized_item_key:
                continue
            path = self._record_path(attachment_key)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            self._records.pop(attachment_key, None)
            deleted += 1
        return deleted

    @staticmethod
    def _record_item_key(record: dict[str, Any]) -> str:
        return str(record.get("itemKey") or record.get("attachmentKey") or "").strip()

    def _record_path(self, attachment_key: str) -> Path:
        return self._cache_dir / f"{attachment_key}.json"

    def _ensure_records_loaded(self) -> None:
        if self._records is not None:
            return
        records: dict[str, dict[str, Any]] = {}
        for path in self._cache_dir.glob("*.json"):
            record = self._read_record(path)
            if record is None:
                continue
            attachment_key = str(record.get("attachmentKey") or path.stem).strip()
            if not attachment_key:
                continue
            if record.get("cachedAt") is None:
                try:
                    record["cachedAt"] = path.stat().st_mtime
                except OSError:
                    record["cachedAt"] = 0.0
            records[attachment_key] = record
        self._records = records

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

    @staticmethod
    def _coerce_optional_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_search_text(text: str) -> str:
        normalized = text.replace("\u00ad", "")
        normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
        normalized = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", normalized)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        return normalized.strip()

    @staticmethod
    def _match_snippet(text: str, normalized_query: str, *, radius: int) -> str | None:
        lowered = text.casefold()
        index = lowered.find(normalized_query)
        if index == -1:
            return None
        start = max(index - radius, 0)
        end = min(index + len(normalized_query) + radius, len(text))
        snippet = text[start:end].strip().replace("\r", " ").replace("\n", " ")
        if start > 0:
            snippet = f"...{snippet}"
        if end < len(text):
            snippet = f"{snippet}..."
        return snippet
