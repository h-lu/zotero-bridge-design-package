from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class LocalSearchIndex:
    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir
        self._items_dir = cache_dir / "items"
        self._manifest_path = cache_dir / "manifest.json"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._items_dir.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, dict[str, Any]] | None = None
        self._manifest: dict[str, Any] | None = None

    def is_fresh(self, *, max_age_seconds: int) -> bool:
        if max_age_seconds < 0:
            return False
        refreshed_at = self._coerce_optional_float(self.state().get("refreshedAt"))
        if refreshed_at is None:
            return False
        return (time.time() - refreshed_at) <= max_age_seconds

    def is_ready(self) -> bool:
        manifest = self._load_manifest()
        return self._coerce_optional_float(manifest.get("refreshedAt")) is not None and bool(
            str(manifest.get("lastSyncMethod") or "").strip()
        )

    def last_modified_version(self) -> int | None:
        return self._coerce_optional_int(self.state().get("lastModifiedVersion"))

    def record_count(self) -> int:
        self._ensure_records_loaded()
        assert self._records is not None
        return len(self._records)

    def all_records(self) -> list[dict[str, Any]]:
        self._ensure_records_loaded()
        assert self._records is not None
        return [dict(record) for record in self._records.values()]

    def state(self) -> dict[str, Any]:
        manifest = dict(self._load_manifest())
        if "count" not in manifest and self._records is not None:
            manifest["count"] = len(self._records)
        manifest.setdefault("count", 0)
        manifest["ready"] = self.is_ready()
        return manifest

    def replace_records(
        self,
        records: list[dict[str, Any]],
        *,
        refreshed_at: float | None = None,
        last_modified_version: int | None = None,
        sync_method: str = "rebuild",
    ) -> None:
        normalized_records: dict[str, dict[str, Any]] = {}
        for record in records:
            item_key = str(record.get("itemKey") or "").strip()
            if not item_key:
                continue
            normalized_record = dict(record)
            normalized_record["itemKey"] = item_key
            normalized_records[item_key] = normalized_record

        for path in self._items_dir.glob("*.json"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue

        for item_key, record in normalized_records.items():
            self._record_path(item_key).write_text(
                json.dumps(record, ensure_ascii=False),
                encoding="utf-8",
            )

        self._records = normalized_records
        self._write_manifest(
            {
                "refreshedAt": refreshed_at if refreshed_at is not None else time.time(),
                "count": len(normalized_records),
                "lastModifiedVersion": last_modified_version,
                "lastSyncMethod": sync_method,
                "lastError": None,
                "lastErrorAt": None,
            }
        )

    def upsert_record(self, record: dict[str, Any]) -> None:
        item_key = str(record.get("itemKey") or "").strip()
        if not item_key:
            return
        self._ensure_records_loaded()
        normalized_record = dict(record)
        normalized_record["itemKey"] = item_key
        assert self._records is not None
        self._records[item_key] = normalized_record
        self._record_path(item_key).write_text(
            json.dumps(normalized_record, ensure_ascii=False),
            encoding="utf-8",
        )
        self._update_manifest_count()

    def delete_record(self, item_key: str) -> None:
        normalized_item_key = item_key.strip()
        if not normalized_item_key:
            return
        self._ensure_records_loaded()
        assert self._records is not None
        self._records.pop(normalized_item_key, None)
        try:
            self._record_path(normalized_item_key).unlink(missing_ok=True)
        except OSError:
            return
        self._update_manifest_count()

    def mark_synced(
        self,
        *,
        last_modified_version: int | None,
        sync_method: str,
        refreshed_at: float | None = None,
    ) -> None:
        manifest = self.state()
        manifest.update(
            {
                "refreshedAt": refreshed_at if refreshed_at is not None else time.time(),
                "count": self.record_count(),
                "lastModifiedVersion": last_modified_version,
                "lastSyncMethod": sync_method,
                "lastError": None,
                "lastErrorAt": None,
            }
        )
        self._write_manifest(manifest)

    def mark_error(self, message: str) -> None:
        manifest = self.state()
        count = self.record_count() if self._records is not None else manifest.get("count", 0)
        manifest.update(
            {
                "count": count,
                "lastError": message,
                "lastErrorAt": time.time(),
            }
        )
        self._write_manifest(manifest)

    def search(
        self,
        *,
        query: str,
        fields: set[str],
        item_type: str | None,
        collection_key: str | None,
        tag: str | None,
    ) -> list[dict[str, Any]]:
        normalized_query = query.strip().casefold()
        if not normalized_query or not fields:
            return []

        self._ensure_records_loaded()
        assert self._records is not None

        results: list[dict[str, Any]] = []
        for record in self._records.values():
            if item_type and record.get("itemType") != item_type:
                continue
            if collection_key and collection_key not in record.get("collectionKeys", []):
                continue
            if tag and tag not in record.get("tags", []):
                continue

            hints: list[dict[str, str | None]] = []
            for field in fields:
                snippet = self._field_match_snippet(
                    record=record,
                    field=field,
                    normalized_query=normalized_query,
                )
                if snippet is None:
                    continue
                hints.append({"field": field, "snippet": snippet})

            if hints:
                results.append({"record": dict(record), "hints": hints})
        return results

    @staticmethod
    def _field_match_snippet(
        *,
        record: dict[str, Any],
        field: str,
        normalized_query: str,
    ) -> str | None:
        if field == "title":
            return LocalSearchIndex._exact_or_token_snippet(
                str(record.get("title") or ""),
                normalized_query,
            )
        if field == "creator":
            for creator in record.get("creators", []):
                snippet = LocalSearchIndex._exact_or_token_snippet(
                    str(creator or ""),
                    normalized_query,
                )
                if snippet is not None:
                    return snippet
            return None
        if field == "abstract":
            return LocalSearchIndex._exact_or_token_snippet(
                str(record.get("abstractNote") or ""),
                normalized_query,
            )
        if field == "venue":
            return LocalSearchIndex._exact_or_token_snippet(
                str(record.get("venue") or ""),
                normalized_query,
            )
        if field == "doi":
            return LocalSearchIndex._exact_or_token_snippet(
                str(record.get("DOI") or ""),
                normalized_query,
            )
        if field == "tag":
            for raw_tag in record.get("tags", []):
                snippet = LocalSearchIndex._exact_or_token_snippet(
                    str(raw_tag or ""),
                    normalized_query,
                )
                if snippet is not None:
                    return snippet
            return None
        if field == "note":
            return LocalSearchIndex._exact_or_token_snippet(
                str(record.get("noteText") or ""),
                normalized_query,
            )
        return None

    @staticmethod
    def _exact_or_token_snippet(text: str, normalized_query: str, radius: int = 80) -> str | None:
        normalized_text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized_text:
            return None
        lowered = normalized_text.casefold()
        exact_index = lowered.find(normalized_query)
        if exact_index != -1:
            return LocalSearchIndex._snippet_from_index(
                normalized_text,
                index=exact_index,
                match_length=len(normalized_query),
                radius=radius,
            )

        tokens = [token for token in normalized_query.split() if token]
        if not tokens or not all(token in lowered for token in tokens):
            return None

        token_indexes = [lowered.find(token) for token in tokens]
        token_indexes = [index for index in token_indexes if index >= 0]
        if not token_indexes:
            return None
        start_index = min(token_indexes)
        end_index = max(
            index + len(token)
            for index, token in zip(token_indexes, tokens, strict=False)
        )
        return LocalSearchIndex._snippet_from_index(
            normalized_text,
            index=start_index,
            match_length=max(end_index - start_index, len(tokens[0])),
            radius=radius,
        )

    @staticmethod
    def _snippet_from_index(
        text: str,
        *,
        index: int,
        match_length: int,
        radius: int,
    ) -> str:
        start = max(index - radius, 0)
        end = min(index + match_length + radius, len(text))
        snippet = text[start:end].strip().replace("\r", " ").replace("\n", " ")
        if start > 0:
            snippet = f"...{snippet}"
        if end < len(text):
            snippet = f"{snippet}..."
        return snippet

    def _ensure_records_loaded(self) -> None:
        if self._records is not None:
            return
        records: dict[str, dict[str, Any]] = {}
        for path in self._items_dir.glob("*.json"):
            record = self._read_json(path)
            if not isinstance(record, dict):
                continue
            item_key = str(record.get("itemKey") or "").strip()
            if not item_key:
                continue
            records[item_key] = record
        self._records = records

    def _update_manifest_count(self) -> None:
        manifest = self.state()
        manifest["count"] = self.record_count()
        self._write_manifest(manifest)

    def _load_manifest(self) -> dict[str, Any]:
        if self._manifest is not None:
            return self._manifest
        payload = self._read_json(self._manifest_path)
        self._manifest = payload if isinstance(payload, dict) else {}
        return self._manifest

    def _write_manifest(self, payload: dict[str, Any]) -> None:
        self._manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        self._manifest = dict(payload)

    def _record_path(self, item_key: str) -> Path:
        return self._items_dir / f"{item_key}.json"

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _coerce_optional_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_optional_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
