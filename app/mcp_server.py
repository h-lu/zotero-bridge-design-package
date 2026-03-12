from __future__ import annotations

import mimetypes
import re
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import Settings as AppSettings
from app.errors import BridgeError
from app.services.zotero_client import ZoteroClient

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BRIDGE_BASE_URL = "https://hblu.top:8888"
DEFAULT_DOWNLOAD_DIR = Path("/tmp/zotero-bridge-mcp")
MAX_API_LIMIT = 25
MAX_REVIEW_PACK_KEYS = 20
MAX_DELETE_KEYS = 100
PDF_EXTENSION = ".pdf"

LibraryScope = Literal["library", "openalex", "both"]
SearchMode = Literal["keyword", "fielded", "doi", "recent", "tag", "collection"]
WorkspaceMode = Literal["reading", "review", "gap_scan"]
PdfSelection = Literal["auto", "exact_attachment"]
IngestSource = Literal["doi", "metadata", "openalex_hit"]
AttachSource = Literal["file_path", "file_url"]
DeleteRecordKind = Literal["item", "attachment", "note"]
PatchClearField = Literal["abstract", "venue", "date", "doi"]


class MCPSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bridge_base_url: str = Field(DEFAULT_BRIDGE_BASE_URL, alias="BRIDGE_BASE_URL")
    zotero_api_key: str | None = Field(default=None, alias="ZOTERO_API_KEY")
    agent_name: str = Field(default="codex-mcp", alias="ZOTERO_MCP_AGENT_NAME")
    request_timeout_seconds: float = Field(
        default=60.0,
        alias="ZOTERO_MCP_REQUEST_TIMEOUT_SECONDS",
    )
    download_dir: Path = Field(default=DEFAULT_DOWNLOAD_DIR, alias="ZOTERO_MCP_DOWNLOAD_DIR")


@lru_cache(maxsize=1)
def get_settings() -> MCPSettings:
    return MCPSettings()


@lru_cache(maxsize=1)
def get_app_settings() -> AppSettings:
    return AppSettings(_env_file=REPO_ROOT / ".env")  # type: ignore[call-arg]


class ToolFailure(Exception):
    def __init__(
        self,
        kind: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retryable = retryable
        self.details = details or {}

    def to_result(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "kind": self.kind,
                "message": self.message,
                "retryable": self.retryable,
                "details": self.details,
            },
        }


class IngestPaperInput(BaseModel):
    source: IngestSource
    doi: str | None = None
    metadata: dict[str, Any] | None = None
    openalex_hit: dict[str, Any] | None = None
    collection_key: str | None = None
    tags: list[str] = Field(default_factory=list)


class PdfTargetInput(BaseModel):
    item_key: str | None = None
    attachment_key: str | None = None
    selection: PdfSelection = "auto"


class AnalysisNoteInput(BaseModel):
    note_type: str
    slot: str = "default"
    title: str | None = None
    body_markdown: str
    payload: dict[str, Any] | None = None
    schema_version: str | None = "1.0"
    provenance: list[dict[str, Any]] | None = None
    tags: list[str] = Field(default_factory=list)
    model: str | None = None
    source_attachment_key: str | None = None
    source_cursor_start: int | None = None
    source_cursor_end: int | None = None


class PatchCreatorInput(BaseModel):
    creator_type: str = "author"
    first_name: str | None = None
    last_name: str | None = None
    name: str | None = None


class BridgeMcpClient:
    def __init__(self, settings: MCPSettings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.bridge_base_url.rstrip("/"),
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
        )

    async def __aenter__(self) -> BridgeMcpClient:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self._client.aclose()

    def _headers(self, *, authenticated: bool = True) -> dict[str, str]:
        headers = {"User-Agent": f"{self._settings.agent_name}/1.0"}
        if not authenticated:
            return headers
        if not self._settings.zotero_api_key:
            raise ToolFailure(
                "auth",
                "ZOTERO_API_KEY is not configured for the MCP server",
            )
        headers["X-Zotero-API-Key"] = self._settings.zotero_api_key
        return headers

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        response = await self._request(
            "GET",
            path,
            params=params,
            authenticated=authenticated,
        )
        return self._decode_json(response)

    async def post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            path,
            json=payload,
            authenticated=authenticated,
        )
        return self._decode_json(response)

    async def post_multipart(
        self,
        path: str,
        *,
        data: dict[str, str],
        files: dict[str, tuple[str, bytes, str]],
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            path,
            data=data,
            files=files,
            authenticated=True,
        )
        return self._decode_json(response)

    async def download_bytes(self, url: str) -> tuple[bytes, httpx.Headers]:
        response = await self._request("GET", url, authenticated=False)
        return response.content, response.headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        authenticated: bool = True,
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method,
                path,
                params=params,
                json=json,
                data=data,
                files=files,
                headers=self._headers(authenticated=authenticated),
            )
        except httpx.HTTPError as exc:
            raise ToolFailure(
                "upstream",
                f"Bridge request failed: {exc}",
                retryable=True,
            ) from exc
        if response.status_code >= 400:
            raise self._map_http_error(response)
        return response

    def _decode_json(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ToolFailure(
                "upstream",
                "Bridge returned a non-JSON response where JSON was expected",
            ) from exc
        if not isinstance(payload, dict):
            raise ToolFailure("upstream", "Bridge returned an unexpected JSON payload")
        return payload

    def _map_http_error(self, response: httpx.Response) -> ToolFailure:
        payload: dict[str, Any] | None = None
        message = f"Bridge returned HTTP {response.status_code}"
        details: dict[str, Any] = {"status_code": response.status_code}
        try:
            decoded = response.json()
            if isinstance(decoded, dict):
                payload = decoded
        except ValueError:
            payload = None
        if payload:
            error_payload = payload.get("error")
            if isinstance(error_payload, dict):
                details["code"] = error_payload.get("code")
                if error_payload.get("upstreamStatus") is not None:
                    details["upstream_status"] = error_payload.get("upstreamStatus")
                message = error_payload.get("message") or message
            else:
                message = payload.get("message") or message
        if response.status_code in {401, 403}:
            kind = "auth"
        elif response.status_code == 404:
            kind = "not_found"
        elif response.status_code == 409:
            kind = "conflict"
        elif response.status_code in {400, 422}:
            kind = "validation"
        else:
            kind = "upstream"
        return ToolFailure(
            kind,
            message,
            retryable=response.status_code >= 500,
            details=details,
        )


def _ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}


def _coerce_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _first_non_empty(*values: Any) -> str | None:
    for value in values:
        coerced = _coerce_string(value)
        if coerced:
            return coerced
    return None


def _extract_year(record: dict[str, Any]) -> str | None:
    year = _coerce_string(record.get("year"))
    if year:
        return year
    raw_date = _coerce_string(record.get("date")) or _coerce_string(record.get("publicationYear"))
    if not raw_date:
        return None
    match = re.search(r"(19|20)\d{2}", raw_date)
    return match.group(0) if match else None


def _strip_doi_prefix(value: str | None) -> str | None:
    doi = _coerce_string(value)
    if not doi:
        return None
    lowered = doi.lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if lowered.startswith(prefix):
            return doi[len(prefix) :]
    return doi


def _extract_authors(record: dict[str, Any]) -> list[str]:
    authors: list[str] = []
    creators = record.get("creators")
    if isinstance(creators, list):
        for creator in creators:
            if not isinstance(creator, dict):
                continue
            display = _first_non_empty(
                creator.get("displayName"),
                creator.get("name"),
                " ".join(
                    part
                    for part in [
                        _coerce_string(creator.get("firstName")),
                        _coerce_string(creator.get("lastName")),
                    ]
                    if part
                ),
            )
            if display:
                authors.append(display)
    raw_authors = record.get("authors")
    if not authors and isinstance(raw_authors, list):
        for author in raw_authors:
            if isinstance(author, str) and author.strip():
                authors.append(author.strip())
            elif isinstance(author, dict):
                display = _first_non_empty(
                    author.get("displayName"),
                    author.get("name"),
                    author.get("author"),
                )
                if display:
                    authors.append(display)
    authorships = record.get("authorships")
    if not authors and isinstance(authorships, list):
        for authorship in authorships:
            if not isinstance(authorship, dict):
                continue
            author = authorship.get("author")
            if isinstance(author, dict):
                display = _coerce_string(author.get("display_name"))
                if display:
                    authors.append(display)
    author_names = record.get("authorNames")
    if not authors and isinstance(author_names, list):
        authors.extend(name.strip() for name in author_names if isinstance(name, str) and name.strip())
    return authors


def _authors_as_creator_inputs(record: dict[str, Any]) -> list[dict[str, str]]:
    return [{"name": author} for author in _extract_authors(record)]


def _normalize_creator_inputs(record: dict[str, Any]) -> list[dict[str, str]]:
    creators = record.get("creators")
    normalized_creators: list[dict[str, str]] = []
    if isinstance(creators, list):
        for creator in creators:
            if not isinstance(creator, dict):
                continue
            normalized: dict[str, str] = {}
            for key in ("firstName", "lastName", "name", "creatorType"):
                value = _coerce_string(creator.get(key))
                if value:
                    normalized[key] = value
            if normalized:
                normalized.setdefault("creatorType", "author")
                normalized_creators.append(normalized)
    if normalized_creators:
        return normalized_creators
    return _authors_as_creator_inputs(record)


def _metadata_to_import_payload(
    metadata: dict[str, Any],
    *,
    collection_key: str | None,
    tags: Sequence[str],
) -> dict[str, Any]:
    title = _first_non_empty(metadata.get("title"), metadata.get("displayName"))
    if not title:
        raise ToolFailure("validation", "metadata requires title")
    item_type = _first_non_empty(metadata.get("itemType"), "journalArticle")
    payload: dict[str, Any] = {
        "itemType": item_type,
        "title": title,
    }
    creators = _normalize_creator_inputs(metadata)
    if creators:
        payload["creators"] = creators
    abstract_note = _first_non_empty(metadata.get("abstractNote"), metadata.get("abstract"))
    if abstract_note:
        payload["abstractNote"] = abstract_note
    publication_title = _first_non_empty(metadata.get("publicationTitle"), metadata.get("venue"))
    if publication_title:
        payload["publicationTitle"] = publication_title
    date = _first_non_empty(metadata.get("date"))
    if not date:
        publication_year = metadata.get("publicationYear")
        if isinstance(publication_year, int):
            date = str(publication_year)
        else:
            date = _coerce_string(metadata.get("year"))
    if date:
        payload["date"] = date
    doi = _strip_doi_prefix(_first_non_empty(metadata.get("doi"), metadata.get("DOI")))
    if doi:
        payload["doi"] = doi
    url = _coerce_string(metadata.get("url"))
    if url:
        payload["url"] = url
    merged_tags = list(dict.fromkeys([*(metadata.get("tags") or []), *tags]))
    if merged_tags:
        payload["tags"] = merged_tags
    if collection_key:
        payload["collectionKey"] = collection_key
    extra = _coerce_string(metadata.get("extra"))
    if extra:
        payload["extra"] = extra
    if isinstance(metadata.get("updateIfExists"), bool):
        payload["updateIfExists"] = metadata["updateIfExists"]
    return payload


def _openalex_hit_to_import_payload(
    hit: dict[str, Any],
    *,
    collection_key: str | None,
    tags: Sequence[str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": _first_non_empty(hit.get("title"), hit.get("displayName")),
        "publicationYear": _parse_year(
            hit.get("publicationYear") or hit.get("year") or hit.get("publication_year")
        ),
        "authors": _authors_as_creator_inputs(hit),
    }
    doi = _strip_doi_prefix(
        _first_non_empty(
            hit.get("doi"),
            hit.get("DOI"),
            hit.get("paper", {}).get("doi") if isinstance(hit.get("paper"), dict) else None,
        )
    )
    if doi:
        payload["doi"] = doi
    abstract_note = _first_non_empty(hit.get("abstract"), hit.get("abstractNote"))
    if not abstract_note:
        inverted = hit.get("abstract_inverted_index")
        if isinstance(inverted, dict):
            ordered_terms: list[tuple[int, str]] = []
            for token, positions in inverted.items():
                if not isinstance(token, str) or not isinstance(positions, list):
                    continue
                for position in positions:
                    if isinstance(position, int):
                        ordered_terms.append((position, token))
            if ordered_terms:
                abstract_note = " ".join(term for _, term in sorted(ordered_terms))
    if abstract_note:
        payload["abstractNote"] = abstract_note
    venue = _first_non_empty(
        hit.get("venue"),
        hit.get("publicationTitle"),
        hit.get("hostVenueName"),
        hit.get("host_venue", {}).get("display_name") if isinstance(hit.get("host_venue"), dict) else None,
        hit.get("primary_location", {}).get("source", {}).get("display_name")
        if isinstance(hit.get("primary_location"), dict)
        and isinstance(hit.get("primary_location", {}).get("source"), dict)
        else None,
    )
    if venue:
        payload["venue"] = venue
    url = _first_non_empty(
        hit.get("url"),
        hit.get("id"),
        hit.get("primary_location", {}).get("landing_page_url")
        if isinstance(hit.get("primary_location"), dict)
        else None,
    )
    if url:
        payload["url"] = url
    if collection_key:
        payload["collectionKey"] = collection_key
    if tags:
        payload["tags"] = list(tags)
    return {key: value for key, value in payload.items() if value not in (None, [], "")}


def _normalize_attachment(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "attachmentKey": record.get("attachmentKey"),
        "parentItemKey": record.get("parentItemKey"),
        "title": record.get("title"),
        "filename": record.get("filename"),
        "contentType": record.get("contentType"),
        "linkMode": record.get("linkMode"),
        "isPdf": bool(record.get("isPdf")),
        "downloadable": bool(record.get("downloadable")),
    }


def _normalize_note_record(
    note: dict[str, Any],
    *,
    item_key: str | None = None,
    paper_title: str | None = None,
) -> dict[str, Any]:
    payload = note.get("structuredPayload")
    if payload is None:
        payload = note.get("payload")
    return {
        "noteKey": note.get("noteKey"),
        "itemKey": item_key or note.get("itemKey"),
        "paperTitle": paper_title,
        "noteType": note.get("noteType"),
        "slot": note.get("slot"),
        "agent": note.get("agent"),
        "schemaVersion": note.get("schemaVersion"),
        "bodyText": note.get("bodyText"),
        "bodyHtml": note.get("bodyHtml"),
        "payload": payload,
        "provenance": note.get("provenance") or [],
        "tags": note.get("tags") or [],
        "updatedAt": note.get("dateModified"),
        "createdAt": note.get("dateAdded"),
    }


def _normalize_record_keys(keys: Sequence[str] | None, *, limit: int = MAX_DELETE_KEYS) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for key in keys or []:
        value = _coerce_string(key)
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
        if len(normalized) >= limit:
            break
    return normalized


def _normalize_creator_patch_inputs(creators: Sequence[PatchCreatorInput]) -> list[dict[str, str]]:
    normalized_creators: list[dict[str, str]] = []
    for creator in creators:
        normalized: dict[str, str] = {}
        first_name = _coerce_string(creator.first_name)
        last_name = _coerce_string(creator.last_name)
        name = _coerce_string(creator.name)
        creator_type = _coerce_string(creator.creator_type) or "author"
        if name:
            normalized["name"] = name
        else:
            if first_name:
                normalized["firstName"] = first_name
            if last_name:
                normalized["lastName"] = last_name
        if not normalized:
            raise ToolFailure(
                "validation",
                "Each creator requires name or first_name/last_name",
            )
        normalized["creatorType"] = creator_type
        normalized_creators.append(normalized)
    return normalized_creators


def _extract_tag_names(raw_tags: Any) -> list[str]:
    tag_names: list[str] = []
    if not isinstance(raw_tags, list):
        return tag_names
    for tag_record in raw_tags:
        if not isinstance(tag_record, dict):
            continue
        tag_name = _coerce_string(tag_record.get("tag"))
        if tag_name:
            tag_names.append(tag_name)
    return tag_names


def _resolve_venue_field(metadata: dict[str, Any]) -> str:
    item_type = _coerce_string(metadata.get("itemType"))
    if item_type == "conferencePaper":
        return "proceedingsTitle"
    for candidate in ("publicationTitle", "proceedingsTitle", "bookTitle"):
        if candidate in metadata:
            return candidate
    return "publicationTitle"


def _summarize_patched_item(
    *,
    item_key: str,
    metadata: dict[str, Any],
    changed_fields: Sequence[str],
    status: str,
    warning: str | None = None,
) -> dict[str, Any]:
    venue_field = _resolve_venue_field(metadata)
    return {
        "status": status,
        "itemKey": item_key,
        "title": metadata.get("title"),
        "authors": _extract_authors(metadata),
        "year": _extract_year(metadata),
        "doi": _first_non_empty(metadata.get("DOI"), metadata.get("doi")),
        "venue": _first_non_empty(
            metadata.get(venue_field),
            metadata.get("publicationTitle"),
            metadata.get("proceedingsTitle"),
            metadata.get("bookTitle"),
        ),
        "tags": _extract_tag_names(metadata.get("tags")),
        "collections": [value for value in metadata.get("collections") or [] if isinstance(value, str)],
        "updatedFields": list(changed_fields),
        "warning": warning,
    }


def _resolve_record_kind(raw_item: dict[str, Any]) -> DeleteRecordKind:
    data = raw_item.get("data")
    item_type = _coerce_string(data.get("itemType")) if isinstance(data, dict) else None
    if item_type == "note":
        return "note"
    if item_type == "attachment":
        return "attachment"
    return "item"


def _bridge_error_to_tool_failure(exc: BridgeError) -> ToolFailure:
    if exc.status_code in {401, 403}:
        kind = "auth"
    elif exc.status_code == 404:
        kind = "not_found"
    elif exc.status_code == 409:
        kind = "conflict"
    elif exc.status_code in {400, 422}:
        kind = "validation"
    else:
        kind = "upstream"
    details: dict[str, Any] = {"status_code": exc.status_code}
    if exc.code:
        details["code"] = exc.code
    if exc.upstream_status is not None:
        details["upstream_status"] = exc.upstream_status
    return ToolFailure(
        kind,
        exc.message,
        retryable=exc.status_code >= 500,
        details=details,
    )


def _summarize_record_target(
    raw_item: dict[str, Any],
    *,
    key: str,
    requested_kind: DeleteRecordKind,
    resolved_kind: DeleteRecordKind,
) -> dict[str, Any]:
    data = raw_item.get("data")
    metadata = data if isinstance(data, dict) else {}
    return {
        "key": key,
        "requestedKind": requested_kind,
        "resolvedKind": resolved_kind,
        "itemType": metadata.get("itemType"),
        "title": metadata.get("title"),
        "parentItem": metadata.get("parentItem"),
    }


def _normalize_ai_note_summary(note: dict[str, Any]) -> dict[str, Any]:
    return {
        "noteKey": note.get("noteKey"),
        "noteType": note.get("noteType"),
        "slot": note.get("slot"),
        "agent": note.get("agent"),
        "schemaVersion": note.get("schemaVersion"),
        "updatedAt": note.get("dateModified"),
        "tags": note.get("tags") or [],
    }


def _has_downloadable_pdf(attachments: Sequence[dict[str, Any]]) -> bool:
    return any(bool(item.get("isPdf")) and bool(item.get("downloadable")) for item in attachments)


def _normalize_library_result(item: dict[str, Any]) -> dict[str, Any]:
    attachments = item.get("attachments") or []
    ai_notes = item.get("aiNotes") or []
    return {
        "source": "library",
        "provider": "zotero",
        "itemKey": item.get("itemKey"),
        "existingItemKey": item.get("itemKey"),
        "title": item.get("title"),
        "authors": _extract_authors(item),
        "year": _extract_year(item),
        "doi": _first_non_empty(item.get("DOI"), item.get("doi")),
        "venue": _first_non_empty(
            item.get("venue"),
            item.get("publicationTitle"),
            item.get("proceedingsTitle"),
            item.get("conferenceName"),
            item.get("bookTitle"),
            item.get("publisher"),
        ),
        "itemType": item.get("itemType"),
        "hasDownloadablePdf": _has_downloadable_pdf(attachments),
        "noteTypes": sorted(
            {
                note_type
                for note_type in (
                    _coerce_string(note.get("noteType")) for note in ai_notes if isinstance(note, dict)
                )
                if note_type
            }
        ),
        "score": item.get("score"),
        "searchHints": item.get("searchHints") or [],
        "importHandle": None,
    }


def _normalize_openalex_result(item: dict[str, Any]) -> dict[str, Any]:
    doi = _first_non_empty(
        item.get("doi"),
        item.get("DOI"),
        item.get("paper", {}).get("doi") if isinstance(item.get("paper"), dict) else None,
    )
    existing_item_key = _first_non_empty(
        item.get("existingItemKey"),
        item.get("resolvedItemKey"),
        item.get("itemKey"),
    )
    return {
        "source": "openalex",
        "provider": _first_non_empty(item.get("provider"), "openalex"),
        "itemKey": existing_item_key,
        "existingItemKey": existing_item_key,
        "title": _first_non_empty(item.get("title"), item.get("displayName")),
        "authors": _extract_authors(item),
        "year": _extract_year(item),
        "doi": doi,
        "venue": _first_non_empty(
            item.get("venue"),
            item.get("hostVenueName"),
            item.get("publicationTitle"),
        ),
        "itemType": _first_non_empty(item.get("itemType"), "journalArticle"),
        "hasDownloadablePdf": bool(item.get("isOpenAccess")) or bool(item.get("hasPdf")),
        "noteTypes": [],
        "score": item.get("score"),
        "searchHints": [],
        "importHandle": item,
    }


def _normalize_workspace_note(note: dict[str, Any]) -> dict[str, Any]:
    return {
        "noteKey": note.get("noteKey"),
        "noteType": note.get("noteType"),
        "slot": note.get("slot"),
        "agent": note.get("agent"),
        "schemaVersion": note.get("schemaVersion"),
        "updatedAt": note.get("dateModified"),
        "bodyText": note.get("bodyText"),
        "payload": note.get("structuredPayload"),
    }


def _latest_note_by_type(notes: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for note in notes:
        note_type = _coerce_string(note.get("noteType"))
        if not note_type:
            continue
        current = latest.get(note_type)
        candidate_updated = _coerce_string(note.get("updatedAt")) or ""
        current_updated = _coerce_string(current.get("updatedAt")) if current else ""
        if current is None or candidate_updated >= (current_updated or ""):
            latest[note_type] = note
    return latest


def _review_fields(notes: Sequence[dict[str, Any]]) -> dict[str, Any]:
    latest = _latest_note_by_type(notes)
    fields = [
        "paper.summary",
        "paper.methods",
        "paper.findings",
        "paper.limitations",
        "paper.future_work",
        "paper.relevance",
    ]
    return {
        note_type: latest[note_type].get("payload") or latest[note_type].get("bodyText")
        for note_type in fields
        if note_type in latest
    }


def _gap_signals(notes: Sequence[dict[str, Any]]) -> dict[str, Any]:
    latest = _latest_note_by_type(notes)
    fields = [
        "paper.limitations",
        "paper.future_work",
        "synthesis.conflict",
        "synthesis.gap_candidate",
    ]
    return {
        note_type: latest[note_type].get("payload") or latest[note_type].get("bodyText")
        for note_type in fields
        if note_type in latest
    }


def _normalize_workspace_item(pack_item: dict[str, Any], mode: WorkspaceMode) -> dict[str, Any]:
    item = pack_item.get("item") or {}
    attachments = pack_item.get("attachments") or item.get("attachments") or []
    notes = pack_item.get("notes") or []
    normalized_notes = [
        _normalize_workspace_note(note)
        for note in notes
        if isinstance(note, dict)
    ]
    result = {
        "itemKey": item.get("itemKey"),
        "title": item.get("title"),
        "authors": _extract_authors(item),
        "year": _extract_year(item),
        "doi": _first_non_empty(item.get("DOI"), item.get("doi")),
        "abstract": item.get("abstractNote"),
        "venue": _first_non_empty(
            item.get("venue"),
            item.get("publicationTitle"),
            item.get("proceedingsTitle"),
            item.get("conferenceName"),
            item.get("bookTitle"),
            item.get("publisher"),
        ),
        "attachments": [_normalize_attachment(record) for record in attachments if isinstance(record, dict)],
        "notes": normalized_notes,
        "relatedItems": pack_item.get("relatedItems") or [],
        "citation": pack_item.get("citation"),
        "analysisSummary": _latest_note_by_type(normalized_notes),
    }
    if mode == "review":
        result["reviewFields"] = _review_fields(normalized_notes)
    if mode == "gap_scan":
        result["gapSignals"] = _gap_signals(normalized_notes)
    return result


def _chunked(values: Sequence[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield list(values[index : index + size])


def _clean_optional_params(params: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        cleaned[key] = value
    return cleaned


def _parse_year(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        match = re.search(r"(19|20)\d{2}", value)
        if match:
            return int(match.group(0))
    return None


def _filter_results_by_year(
    results: list[dict[str, Any]],
    *,
    year_from: int | None,
    year_to: int | None,
) -> list[dict[str, Any]]:
    if year_from is None and year_to is None:
        return results
    filtered: list[dict[str, Any]] = []
    for result in results:
        year = _parse_year(result.get("year"))
        if year is None:
            continue
        if year_from is not None and year < year_from:
            continue
        if year_to is not None and year > year_to:
            continue
        filtered.append(result)
    return filtered


def _sanitize_filename(filename: str | None, *, default_stem: str) -> str:
    raw_name = filename or f"{default_stem}{PDF_EXTENSION}"
    parsed_name = Path(raw_name).name
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", parsed_name).strip("._")
    if not safe_name:
        safe_name = f"{default_stem}{PDF_EXTENSION}"
    if not safe_name.lower().endswith(PDF_EXTENSION):
        safe_name = f"{safe_name}{PDF_EXTENSION}"
    return safe_name


def _filename_from_headers(headers: httpx.Headers) -> str | None:
    disposition = headers.get("content-disposition")
    if not disposition:
        return None
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition)
    if not match:
        return None
    return match.group(1).strip()


def _ensure_download_dir(download_dir: str | None) -> Path:
    base_dir = Path(download_dir) if download_dir else get_settings().download_dir
    base_dir.mkdir(parents=True, exist_ok=True)
    return Path(mkdtemp(prefix="download-", dir=str(base_dir)))


def _attachment_candidates(attachments: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        record
        for record in attachments
        if isinstance(record, dict) and bool(record.get("isPdf")) and bool(record.get("downloadable"))
    ]


def _select_attachment(
    attachments: Sequence[dict[str, Any]],
    *,
    selection: PdfSelection,
    attachment_key: str | None,
) -> dict[str, Any]:
    candidates = _attachment_candidates(attachments)
    if attachment_key:
        for candidate in candidates:
            if candidate.get("attachmentKey") == attachment_key:
                return candidate
        raise ToolFailure(
            "missing_pdf",
            f"Attachment {attachment_key} is not a downloadable PDF",
        )
    if not candidates:
        raise ToolFailure("missing_pdf", "No downloadable PDF attachment found for the item")
    if selection == "auto" and len(candidates) == 1:
        return candidates[0]
    if selection == "auto":
        raise ToolFailure(
            "ambiguous_attachment",
            "Multiple downloadable PDF attachments matched; pass attachment_key explicitly",
            details={"candidates": [_normalize_attachment(item) for item in candidates]},
        )
    raise ToolFailure("validation", "selection=exact_attachment requires attachment_key")


def _summarize_import_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": result.get("status"),
        "itemKey": result.get("itemKey"),
        "title": result.get("title"),
        "doi": _first_non_empty(result.get("DOI"), result.get("doi")),
        "message": result.get("message"),
    }


async def _fetch_item_notes(
    client: BridgeMcpClient,
    item_key: str,
    *,
    note_types: set[str] | None = None,
    paper_title: str | None = None,
) -> list[dict[str, Any]]:
    payload = await client.get_json(f"/v1/items/{item_key}/notes")
    notes = payload.get("notes") or []
    normalized = [
        _normalize_note_record(note, item_key=item_key, paper_title=paper_title)
        for note in notes
        if isinstance(note, dict)
    ]
    if note_types:
        normalized = [note for note in normalized if note.get("noteType") in note_types]
    return normalized


async def _fetch_item_title(client: BridgeMcpClient, item_key: str) -> str | None:
    payload = await client.get_json(f"/v1/items/{item_key}")
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    return _coerce_string(item.get("title"))


async def _resolve_item_for_upload(
    client: BridgeMcpClient,
    *,
    item_key: str | None,
    doi: str | None,
    metadata: dict[str, Any] | None,
    collection_key: str | None,
    tags: Sequence[str],
) -> tuple[str | None, str | None]:
    if item_key or doi:
        return item_key, doi
    if metadata is None:
        raise ToolFailure(
            "validation",
            "Attach operations require one of item_key, doi, or metadata",
        )
    merged = _metadata_to_import_payload(
        metadata,
        collection_key=collection_key,
        tags=tags,
    )
    imported = await client.post_json("/v1/papers/import-metadata", merged)
    return imported.get("itemKey"), None


async def _run_tool(coro: Any) -> dict[str, Any]:
    try:
        return _ok(**(await coro))
    except ToolFailure as exc:
        return exc.to_result()
    except Exception as exc:  # pragma: no cover - defensive error surface
        return ToolFailure("upstream", f"Unexpected MCP server error: {exc}").to_result()


mcp = FastMCP("zotero-bridge")


@mcp.resource("zotero-bridge://boundary")
def boundary_resource() -> str:
    return (
        "zotero-bridge is a Zotero I/O and workflow layer, not a PDF reading engine.\n"
        "Use Zotero as the paper warehouse and structured note store.\n"
        "Use OpenAlex only for discovery.\n"
        "Read PDFs locally after zotero_prepare_pdf.\n"
        "Write important outputs back through structured notes."
    )


@mcp.resource("zotero-bridge://workflow")
def workflow_resource() -> str:
    return (
        "Default workflow:\n"
        "1. zotero_find_papers\n"
        "2. zotero_ingest_papers\n"
        "3. zotero_build_workspace\n"
        "4. zotero_prepare_pdf\n"
        "5. Read PDFs locally in the agent\n"
        "6. zotero_record_paper_analysis\n"
        "7. zotero_read_records for later synthesis and gap exploration"
    )


@mcp.tool()
async def zotero_find_papers(
    scope: LibraryScope = "both",
    search_mode: SearchMode = "keyword",
    query: str | None = None,
    title: str | None = None,
    author: str | None = None,
    abstract: str | None = None,
    venue: str | None = None,
    doi: str | None = None,
    tag: str | None = None,
    collection_key: str | None = None,
    item_type: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    oa_only: bool = False,
    exclude_existing: bool = False,
    sort: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Search the Zotero library, OpenAlex, or both using one normalized paper finder."""

    async def _impl() -> dict[str, Any]:
        requested_limit = max(1, min(limit, MAX_API_LIMIT))
        if scope == "both" and search_mode not in {"keyword", "doi", "recent"}:
            raise ToolFailure(
                "validation",
                "scope=both only supports search_mode keyword, doi, or recent",
            )
        if search_mode == "doi" and not _coerce_string(doi or query):
            raise ToolFailure("validation", "search_mode=doi requires doi or query")
        if search_mode == "tag" and not _coerce_string(tag):
            raise ToolFailure("validation", "search_mode=tag requires tag")
        if search_mode == "collection" and not _coerce_string(collection_key):
            raise ToolFailure("validation", "search_mode=collection requires collection_key")
        if search_mode == "fielded" and not any(
            _coerce_string(value)
            for value in [query, title, author, abstract, venue, doi, tag]
        ):
            raise ToolFailure(
                "validation",
                "search_mode=fielded requires query or at least one field constraint",
            )
        if scope in {"openalex", "both"} and search_mode == "recent" and not _coerce_string(query):
            raise ToolFailure(
                "validation",
                "OpenAlex recent search still requires a query string",
            )

        library_results: list[dict[str, Any]] = []
        openalex_results: list[dict[str, Any]] = []
        async with BridgeMcpClient(get_settings()) as client:
            if scope in {"library", "both"}:
                if search_mode == "recent":
                    payload = await client.get_json(
                        "/v1/items",
                        params=_clean_optional_params(
                            {
                                "start": 0,
                                "limit": requested_limit,
                                "includeAttachments": True,
                                "includeNotes": True,
                                "itemType": item_type,
                                "collectionKey": collection_key,
                                "tag": tag,
                                "sort": "dateAdded",
                                "direction": "desc",
                            }
                        ),
                    )
                elif search_mode in {"tag", "collection"}:
                    payload = await client.get_json(
                        "/v1/items",
                        params=_clean_optional_params(
                            {
                                "start": 0,
                                "limit": requested_limit,
                                "includeAttachments": True,
                                "includeNotes": True,
                                "itemType": item_type,
                                "collectionKey": collection_key,
                                "tag": tag,
                                "sort": sort if sort in {"dateAdded", "dateModified", "title"} else None,
                                "direction": "desc",
                            }
                        ),
                    )
                elif search_mode == "fielded" or year_from is not None or year_to is not None:
                    fields = ["title", "creator", "abstract", "venue", "doi", "tag", "note"]
                    payload = await client.get_json(
                        "/v1/items/search-advanced",
                        params=_clean_optional_params(
                            {
                                "q": query,
                                "fields": ",".join(fields),
                                "title": title,
                                "author": author,
                                "abstract": abstract,
                                "venue": venue,
                                "doi": doi,
                                "tag": tag,
                                "collectionKey": collection_key,
                                "itemType": item_type,
                                "sort": sort if sort in {"dateAdded", "dateModified", "title"} else None,
                                "direction": "desc",
                                "limit": requested_limit,
                            }
                        ),
                    )
                else:
                    search_query = _coerce_string(query) or _coerce_string(doi)
                    if not search_query:
                        raise ToolFailure(
                            "validation",
                            "keyword or doi searches require query or doi",
                        )
                    payload = await client.get_json(
                        "/v1/items/search",
                        params=_clean_optional_params(
                            {
                                "q": search_query,
                                "start": 0,
                                "limit": requested_limit,
                                "includeAttachments": True,
                                "includeNotes": True,
                                "itemType": item_type,
                                "collectionKey": collection_key,
                                "tag": tag,
                                "sort": sort if sort in {"dateAdded", "dateModified", "title"} else None,
                                "direction": "desc",
                            }
                        ),
                    )
                library_results = [
                    _normalize_library_result(item)
                    for item in payload.get("items") or []
                    if isinstance(item, dict)
                ]
                library_results = _filter_results_by_year(
                    library_results,
                    year_from=year_from,
                    year_to=year_to,
                )

            if scope in {"openalex", "both"}:
                external_query = _coerce_string(doi) or _coerce_string(query)
                if not external_query:
                    raise ToolFailure(
                        "validation",
                        "OpenAlex search requires query or doi",
                    )
                payload = await client.get_json(
                    "/v1/discovery/search",
                    params=_clean_optional_params(
                        {
                            "q": external_query,
                            "start": 0,
                            "limit": requested_limit,
                            "yearFrom": year_from,
                            "yearTo": year_to,
                            "oaOnly": oa_only,
                            "resolveInLibrary": True,
                            "excludeExisting": exclude_existing,
                            "sort": sort if sort in {"relevance", "cited_by", "recent"} else None,
                        }
                    ),
                )
                hits = payload.get("items") or payload.get("hits") or payload.get("results") or []
                openalex_results = [
                    _normalize_openalex_result(hit)
                    for hit in hits
                    if isinstance(hit, dict)
                ]
                openalex_results = _filter_results_by_year(
                    openalex_results,
                    year_from=year_from,
                    year_to=year_to,
                )

        results = [*library_results, *openalex_results]
        return {
            "scope": scope,
            "searchMode": search_mode,
            "count": len(results),
            "results": results[:requested_limit],
        }

    return await _run_tool(_impl())


@mcp.tool()
async def zotero_ingest_papers(papers: list[IngestPaperInput]) -> dict[str, Any]:
    """Ingest papers into Zotero from DOI, metadata, or an OpenAlex discovery hit."""

    async def _impl() -> dict[str, Any]:
        if not papers:
            raise ToolFailure("validation", "papers must contain at least one entry")
        results: list[dict[str, Any]] = []
        async with BridgeMcpClient(get_settings()) as client:
            for paper in papers:
                try:
                    if paper.source == "doi":
                        if not _coerce_string(paper.doi):
                            raise ToolFailure("validation", "source=doi requires doi")
                        response = await client.post_json(
                            "/v1/papers/add-by-doi",
                            {
                                "doi": paper.doi,
                                "collectionKey": paper.collection_key,
                                "tags": paper.tags,
                            },
                        )
                    elif paper.source == "metadata":
                        if paper.metadata is None:
                            raise ToolFailure("validation", "source=metadata requires metadata")
                        payload = _metadata_to_import_payload(
                            paper.metadata,
                            collection_key=paper.collection_key,
                            tags=paper.tags,
                        )
                        response = await client.post_json("/v1/papers/import-metadata", payload)
                    else:
                        if paper.openalex_hit is None:
                            raise ToolFailure(
                                "validation",
                                "source=openalex_hit requires openalex_hit payload",
                            )
                        extracted_doi = _strip_doi_prefix(
                            _first_non_empty(
                                paper.openalex_hit.get("doi"),
                                paper.openalex_hit.get("DOI"),
                                paper.openalex_hit.get("paper", {}).get("doi")
                                if isinstance(paper.openalex_hit.get("paper"), dict)
                                else None,
                            )
                        )
                        if extracted_doi:
                            response = await client.post_json(
                                "/v1/papers/add-by-doi",
                                {
                                    "doi": extracted_doi,
                                    "collectionKey": paper.collection_key,
                                    "tags": paper.tags,
                                },
                            )
                        else:
                            payload = _openalex_hit_to_import_payload(
                                paper.openalex_hit,
                                collection_key=paper.collection_key,
                                tags=paper.tags,
                            )
                            response = await client.post_json(
                                "/v1/papers/import-discovery-hit",
                                payload,
                            )
                    item_result = _summarize_import_result(response)
                    item_result["ok"] = True
                    results.append(item_result)
                except ToolFailure as exc:
                    results.append(
                        {
                            "ok": False,
                            "status": "failed",
                            "source": paper.source,
                            "doi": paper.doi,
                            "error": exc.to_result()["error"],
                        }
                    )
        return {"count": len(results), "results": results}

    return await _run_tool(_impl())


@mcp.tool()
async def zotero_build_workspace(
    item_keys: list[str],
    mode: WorkspaceMode = "reading",
    include_notes: bool = True,
    include_attachments: bool = True,
    include_related: bool = True,
    include_citation: bool = True,
) -> dict[str, Any]:
    """Build a compact reading, review, or gap-scan workspace for Zotero papers."""

    async def _impl() -> dict[str, Any]:
        normalized_item_keys = [item_key for item_key in item_keys if _coerce_string(item_key)]
        if not normalized_item_keys:
            raise ToolFailure("validation", "item_keys must contain at least one item key")
        items: list[dict[str, Any]] = []
        async with BridgeMcpClient(get_settings()) as client:
            for chunk in _chunked(normalized_item_keys, MAX_REVIEW_PACK_KEYS):
                payload = await client.post_json(
                    "/v1/items/review-pack",
                    {
                        "itemKeys": chunk,
                        "includeRelated": include_related,
                        "includeNotes": include_notes,
                    },
                )
                pack_items = payload.get("items") or []
                for pack_item in pack_items:
                    if not isinstance(pack_item, dict):
                        continue
                    normalized = _normalize_workspace_item(pack_item, mode)
                    if not include_attachments:
                        normalized["attachments"] = []
                    if not include_notes:
                        normalized["notes"] = []
                        normalized["analysisSummary"] = {}
                        normalized.pop("reviewFields", None)
                        normalized.pop("gapSignals", None)
                    if not include_related:
                        normalized["relatedItems"] = []
                    if not include_citation:
                        normalized["citation"] = None
                    items.append(normalized)
        return {
            "workspaceMode": mode,
            "count": len(items),
            "items": items,
        }

    return await _run_tool(_impl())


@mcp.tool()
async def zotero_prepare_pdf(
    targets: list[PdfTargetInput],
    download_dir: str | None = None,
) -> dict[str, Any]:
    """Resolve, hand off, and download Zotero PDFs to local paths for agent-side reading."""

    async def _impl() -> dict[str, Any]:
        if not targets:
            raise ToolFailure("validation", "targets must contain at least one entry")
        resolved_download_dir = _ensure_download_dir(download_dir)
        results: list[dict[str, Any]] = []
        async with BridgeMcpClient(get_settings()) as client:
            for index, target in enumerate(targets):
                try:
                    if not target.item_key and not target.attachment_key:
                        raise ToolFailure(
                            "validation",
                            "Each target requires item_key or attachment_key",
                        )
                    selected_attachment: dict[str, Any]
                    item_key = target.item_key
                    if item_key:
                        attachment_payload = await client.get_json(
                            f"/v1/items/{item_key}/attachments"
                        )
                        attachments = attachment_payload.get("attachments") or []
                        selected_attachment = _select_attachment(
                            attachments,
                            selection=target.selection,
                            attachment_key=target.attachment_key,
                        )
                    else:
                        attachment_payload = await client.get_json(
                            f"/v1/attachments/{target.attachment_key}"
                        )
                        selected_attachment = attachment_payload.get("attachment") or {}
                        item_key = _coerce_string(selected_attachment.get("parentItemKey"))
                        if not bool(selected_attachment.get("isPdf")) or not bool(
                            selected_attachment.get("downloadable")
                        ):
                            raise ToolFailure(
                                "missing_pdf",
                                f"Attachment {target.attachment_key} is not a downloadable PDF",
                            )
                    handoff = await client.post_json(
                        f"/v1/attachments/{selected_attachment['attachmentKey']}/handoff",
                        {"mode": "proxy_download"},
                    )
                    content, headers = await client.download_bytes(handoff["downloadUrl"])
                    filename = _sanitize_filename(
                        _first_non_empty(
                            handoff.get("filename"),
                            selected_attachment.get("filename"),
                            _filename_from_headers(headers),
                        ),
                        default_stem=f"paper-{index + 1}",
                    )
                    destination = resolved_download_dir / filename
                    destination.write_bytes(content)
                    results.append(
                        {
                            "ok": True,
                            "itemKey": item_key,
                            "attachmentKey": selected_attachment.get("attachmentKey"),
                            "filename": filename,
                            "contentType": headers.get("content-type")
                            or handoff.get("contentType")
                            or selected_attachment.get("contentType"),
                            "localPath": str(destination.resolve()),
                            "expiresAt": handoff.get("expiresAt"),
                        }
                    )
                except ToolFailure as exc:
                    results.append(
                        {
                            "ok": False,
                            "itemKey": target.item_key,
                            "attachmentKey": target.attachment_key,
                            "error": exc.to_result()["error"],
                        }
                    )
        return {
            "downloadDir": str(resolved_download_dir.resolve()),
            "count": len(results),
            "results": results,
        }

    return await _run_tool(_impl())


@mcp.tool()
async def zotero_attach_pdf(
    source: AttachSource,
    file_path: str | None = None,
    file_url: str | None = None,
    item_key: str | None = None,
    doi: str | None = None,
    metadata: dict[str, Any] | None = None,
    collection_key: str | None = None,
    tags: list[str] | None = None,
    create_top_level_attachment_if_needed: bool = False,
) -> dict[str, Any]:
    """Attach a local PDF file or remote PDF URL into the Zotero paper store."""

    async def _impl() -> dict[str, Any]:
        normalized_tags = tags or []
        async with BridgeMcpClient(get_settings()) as client:
            resolved_item_key, resolved_doi = await _resolve_item_for_upload(
                client,
                item_key=item_key,
                doi=doi,
                metadata=metadata,
                collection_key=collection_key,
                tags=normalized_tags,
            )
            if source == "file_url":
                if not _coerce_string(file_url):
                    raise ToolFailure("validation", "source=file_url requires file_url")
                payload = {
                    "itemKey": resolved_item_key,
                    "doi": resolved_doi,
                    "fileUrl": file_url,
                    "collectionKey": collection_key,
                    "tags": normalized_tags,
                    "createTopLevelAttachmentIfNeeded": create_top_level_attachment_if_needed,
                }
                response = await client.post_json("/v1/papers/upload-pdf-action", payload)
            else:
                if not _coerce_string(file_path):
                    raise ToolFailure("validation", "source=file_path requires file_path")
                local_path = Path(file_path).expanduser()
                if not local_path.is_file():
                    raise ToolFailure("not_found", f"Local file not found: {local_path}")
                content = local_path.read_bytes()
                content_type, _ = mimetypes.guess_type(local_path.name)
                multipart_data = {
                    "itemKey": resolved_item_key or "",
                    "doi": resolved_doi or "",
                    "collectionKey": collection_key or "",
                    "tags": ",".join(normalized_tags),
                    "createTopLevelAttachmentIfNeeded": str(
                        create_top_level_attachment_if_needed
                    ).lower(),
                }
                response = await client.post_multipart(
                    "/v1/papers/upload-pdf-multipart",
                    data=multipart_data,
                    files={
                        "file": (
                            local_path.name,
                            content,
                            content_type or "application/pdf",
                        )
                    },
                )
        return {
            "status": response.get("status"),
            "itemKey": response.get("itemKey"),
            "attachmentKey": response.get("attachmentKey"),
            "filename": response.get("filename"),
            "contentType": response.get("contentType"),
            "title": response.get("title"),
        }

    return await _run_tool(_impl())


@mcp.tool()
async def zotero_record_paper_analysis(
    item_key: str,
    notes: list[AnalysisNoteInput],
) -> dict[str, Any]:
    """Write paper-level or synthesis structured notes back into Zotero."""

    async def _impl() -> dict[str, Any]:
        if not _coerce_string(item_key):
            raise ToolFailure("validation", "item_key is required")
        if not notes:
            raise ToolFailure("validation", "notes must contain at least one note")
        results: list[dict[str, Any]] = []
        async with BridgeMcpClient(get_settings()) as client:
            for note in notes:
                try:
                    response = await client.post_json(
                        f"/v1/items/{item_key}/notes/upsert-ai-note",
                        {
                            "agent": get_settings().agent_name,
                            "noteType": note.note_type,
                            "slot": note.slot,
                            "title": note.title,
                            "bodyMarkdown": note.body_markdown,
                            "tags": note.tags,
                            "model": note.model,
                            "sourceAttachmentKey": note.source_attachment_key,
                            "sourceCursorStart": note.source_cursor_start,
                            "sourceCursorEnd": note.source_cursor_end,
                            "schemaVersion": note.schema_version,
                            "payload": note.payload,
                            "provenance": note.provenance,
                        },
                    )
                    results.append(
                        {
                            "ok": True,
                            "status": response.get("status"),
                            "noteKey": response.get("noteKey"),
                            "itemKey": response.get("itemKey"),
                            "noteType": response.get("noteType"),
                            "slot": response.get("slot"),
                            "schemaVersion": response.get("schemaVersion"),
                        }
                    )
                except ToolFailure as exc:
                    results.append(
                        {
                            "ok": False,
                            "itemKey": item_key,
                            "noteType": note.note_type,
                            "slot": note.slot,
                            "error": exc.to_result()["error"],
                        }
                    )
        return {"count": len(results), "results": results}

    return await _run_tool(_impl())


@mcp.tool()
async def zotero_read_records(
    item_keys: list[str] | None = None,
    note_keys: list[str] | None = None,
    note_types: list[str] | None = None,
    query: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Read stored structured notes by item, by note key, or through note-aware search."""

    async def _impl() -> dict[str, Any]:
        note_type_filter = {note_type for note_type in (note_types or []) if _coerce_string(note_type)}
        records: list[dict[str, Any]] = []
        requested_limit = max(1, min(limit, MAX_API_LIMIT))
        title_cache: dict[str, str | None] = {}
        async with BridgeMcpClient(get_settings()) as client:
            if note_keys:
                for note_key in note_keys[:requested_limit]:
                    payload = await client.get_json(f"/v1/notes/{note_key}")
                    note = payload.get("note")
                    if isinstance(note, dict):
                        normalized = _normalize_note_record(note)
                        item_key = _coerce_string(normalized.get("itemKey"))
                        if item_key:
                            if item_key not in title_cache:
                                title_cache[item_key] = await _fetch_item_title(client, item_key)
                            normalized["paperTitle"] = title_cache[item_key]
                        if note_type_filter and normalized.get("noteType") not in note_type_filter:
                            continue
                        records.append(normalized)
            elif item_keys:
                for item_key in item_keys:
                    if not _coerce_string(item_key):
                        continue
                    if item_key not in title_cache:
                        title_cache[item_key] = await _fetch_item_title(client, item_key)
                    item_records = await _fetch_item_notes(
                        client,
                        item_key,
                        note_types=note_type_filter or None,
                        paper_title=title_cache[item_key],
                    )
                    records.extend(item_records)
            elif _coerce_string(query):
                search_payload = await client.get_json(
                    "/v1/items/search-advanced",
                    params={
                        "q": query,
                        "fields": "note",
                        "limit": requested_limit,
                    },
                )
                for item in search_payload.get("items") or []:
                    if not isinstance(item, dict):
                        continue
                    item_key = item.get("itemKey")
                    if not _coerce_string(item_key):
                        continue
                    paper_title = item.get("title")
                    item_records = await _fetch_item_notes(
                        client,
                        item_key,
                        note_types=note_type_filter or None,
                        paper_title=paper_title,
                    )
                    for record in item_records:
                        body_text = _coerce_string(record.get("bodyText")) or ""
                        payload_text = str(record.get("payload") or "")
                        if query and query.lower() not in body_text.lower() and query.lower() not in payload_text.lower():
                            continue
                        records.append(record)
                        if len(records) >= requested_limit:
                            break
                    if len(records) >= requested_limit:
                        break
            else:
                raise ToolFailure(
                    "validation",
                    "Provide note_keys, item_keys, or query to read records",
                )
        return {
            "count": len(records),
            "records": records[:requested_limit],
        }

    return await _run_tool(_impl())


@mcp.tool()
async def zotero_delete_records(
    item_keys: list[str] | None = None,
    attachment_keys: list[str] | None = None,
    note_keys: list[str] | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Delete records by key. Defaults to dry-run; top-level item deletion requires confirm=true."""

    async def _impl() -> dict[str, Any]:
        normalized_item_keys = _normalize_record_keys(item_keys)
        normalized_attachment_keys = _normalize_record_keys(attachment_keys)
        normalized_note_keys = _normalize_record_keys(note_keys)
        if not normalized_item_keys and not normalized_attachment_keys and not normalized_note_keys:
            raise ToolFailure(
                "validation",
                "Provide item_keys, attachment_keys, or note_keys to delete",
            )

        results: list[dict[str, Any]] = []
        direct_delete_warning = (
            "Deleted directly via Zotero API; bridge local search index may lag until its next sync."
        )
        app_settings = get_app_settings()
        request_timeout = get_settings().request_timeout_seconds

        async with BridgeMcpClient(get_settings()) as bridge_client:
            async with httpx.AsyncClient(timeout=request_timeout) as http_client:
                zotero_client = ZoteroClient(settings=app_settings, client=http_client)

                async def _handle_keys(
                    keys: list[str],
                    *,
                    requested_kind: DeleteRecordKind,
                ) -> None:
                    for key in keys:
                        try:
                            raw_item = await zotero_client.get_item(key)
                        except BridgeError as exc:
                            failure = _bridge_error_to_tool_failure(exc)
                            if failure.kind == "not_found":
                                results.append(
                                    {
                                        "ok": True,
                                        "key": key,
                                        "requestedKind": requested_kind,
                                        "resolvedKind": None,
                                        "status": "already_missing",
                                    }
                                )
                                continue
                            results.append(
                                {
                                    "ok": False,
                                    "key": key,
                                    "requestedKind": requested_kind,
                                    "error": failure.to_result()["error"],
                                }
                            )
                            continue

                        resolved_kind = _resolve_record_kind(raw_item)
                        summary = _summarize_record_target(
                            raw_item,
                            key=key,
                            requested_kind=requested_kind,
                            resolved_kind=resolved_kind,
                        )
                        if resolved_kind != requested_kind:
                            results.append(
                                {
                                    "ok": False,
                                    **summary,
                                    "error": {
                                        "kind": "validation",
                                        "message": (
                                            f"{key} is a {resolved_kind}, not a {requested_kind}"
                                        ),
                                        "retryable": False,
                                        "details": {},
                                    },
                                }
                            )
                            continue
                        if dry_run:
                            results.append({"ok": True, **summary, "status": "would_delete"})
                            continue
                        if resolved_kind == "item" and not confirm:
                            results.append(
                                {
                                    "ok": False,
                                    **summary,
                                    "error": {
                                        "kind": "validation",
                                        "message": "Deleting top-level items requires confirm=true",
                                        "retryable": False,
                                        "details": {},
                                    },
                                }
                            )
                            continue

                        try:
                            if resolved_kind == "note":
                                response = await bridge_client._request("DELETE", f"/v1/notes/{key}")
                                bridge_client._decode_json(response)
                                results.append({"ok": True, **summary, "status": "deleted"})
                            else:
                                await zotero_client.delete_item(
                                    item_key=key,
                                    version=int(raw_item.get("version", 0)),
                                )
                                results.append(
                                    {
                                        "ok": True,
                                        **summary,
                                        "status": "deleted",
                                        "warning": direct_delete_warning,
                                    }
                                )
                        except BridgeError as exc:
                            failure = _bridge_error_to_tool_failure(exc)
                            results.append(
                                {
                                    "ok": False,
                                    **summary,
                                    "error": failure.to_result()["error"],
                                }
                            )
                        except ToolFailure as exc:
                            results.append(
                                {
                                    "ok": False,
                                    **summary,
                                    "error": exc.to_result()["error"],
                                }
                            )

                await _handle_keys(normalized_note_keys, requested_kind="note")
                await _handle_keys(normalized_attachment_keys, requested_kind="attachment")
                await _handle_keys(normalized_item_keys, requested_kind="item")

        return {
            "dryRun": dry_run,
            "count": len(results),
            "deletedCount": sum(1 for result in results if result.get("status") == "deleted"),
            "wouldDeleteCount": sum(1 for result in results if result.get("status") == "would_delete"),
            "alreadyMissingCount": sum(
                1 for result in results if result.get("status") == "already_missing"
            ),
            "results": results,
        }

    return await _run_tool(_impl())


@mcp.tool()
async def zotero_patch_item_metadata(
    item_key: str,
    title: str | None = None,
    authors: list[str] | None = None,
    creators: list[PatchCreatorInput] | None = None,
    abstract: str | None = None,
    venue: str | None = None,
    date: str | None = None,
    year: str | int | None = None,
    doi: str | None = None,
    add_tags: list[str] | None = None,
    remove_tags: list[str] | None = None,
    add_collection_keys: list[str] | None = None,
    remove_collection_keys: list[str] | None = None,
    clear_fields: list[PatchClearField] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Patch a top-level item's metadata through a constrained field set."""

    async def _impl() -> dict[str, Any]:
        normalized_item_key = _coerce_string(item_key)
        if not normalized_item_key:
            raise ToolFailure("validation", "item_key is required")
        if authors and creators:
            raise ToolFailure("validation", "Provide authors or creators, not both")

        cleared_fields = {field for field in (clear_fields or []) if _coerce_string(field)}
        tags_to_add = _normalize_record_keys(add_tags)
        tags_to_remove = _normalize_record_keys(remove_tags)
        collections_to_add = _normalize_record_keys(add_collection_keys)
        collections_to_remove = _normalize_record_keys(remove_collection_keys)

        app_settings = get_app_settings()
        request_timeout = get_settings().request_timeout_seconds
        async with httpx.AsyncClient(timeout=request_timeout) as http_client:
            zotero_client = ZoteroClient(settings=app_settings, client=http_client)
            try:
                raw_item = await zotero_client.get_item(normalized_item_key)
            except BridgeError as exc:
                raise _bridge_error_to_tool_failure(exc) from exc

            resolved_kind = _resolve_record_kind(raw_item)
            if resolved_kind != "item":
                raise ToolFailure(
                    "validation",
                    f"{normalized_item_key} is a {resolved_kind}; only top-level items can be patched",
                )

            metadata = raw_item.get("data")
            if not isinstance(metadata, dict):
                raise ToolFailure("upstream", "Unexpected Zotero item payload")
            updated_data = dict(metadata)
            changed_fields: list[str] = []

            normalized_title = _coerce_string(title)
            if normalized_title and normalized_title != _coerce_string(updated_data.get("title")):
                updated_data["title"] = normalized_title
                changed_fields.append("title")

            if authors is not None:
                normalized_authors = _normalize_record_keys(authors)
                normalized_creators = [{"name": author, "creatorType": "author"} for author in normalized_authors]
                if normalized_creators != (updated_data.get("creators") or []):
                    updated_data["creators"] = normalized_creators
                    changed_fields.append("creators")
            elif creators is not None:
                normalized_creators = _normalize_creator_patch_inputs(creators)
                if normalized_creators != (updated_data.get("creators") or []):
                    updated_data["creators"] = normalized_creators
                    changed_fields.append("creators")

            abstract_note = _coerce_string(abstract)
            if "abstract" in cleared_fields:
                if _coerce_string(updated_data.get("abstractNote")):
                    updated_data["abstractNote"] = ""
                    changed_fields.append("abstractNote")
            elif abstract is not None and abstract_note != _coerce_string(updated_data.get("abstractNote")):
                updated_data["abstractNote"] = abstract_note or ""
                changed_fields.append("abstractNote")

            venue_field = _resolve_venue_field(updated_data)
            normalized_venue = _coerce_string(venue)
            if "venue" in cleared_fields:
                if _coerce_string(updated_data.get(venue_field)):
                    updated_data[venue_field] = ""
                    changed_fields.append(venue_field)
            elif venue is not None and normalized_venue != _coerce_string(updated_data.get(venue_field)):
                updated_data[venue_field] = normalized_venue or ""
                changed_fields.append(venue_field)

            normalized_date = _coerce_string(date)
            normalized_year = str(year).strip() if year is not None and str(year).strip() else None
            if "date" in cleared_fields:
                if _coerce_string(updated_data.get("date")):
                    updated_data["date"] = ""
                    changed_fields.append("date")
            else:
                next_date = normalized_date or normalized_year
                if next_date is not None and next_date != _coerce_string(updated_data.get("date")):
                    updated_data["date"] = next_date
                    changed_fields.append("date")

            normalized_doi = _strip_doi_prefix(doi) if doi is not None else None
            if "doi" in cleared_fields:
                if _coerce_string(updated_data.get("DOI")):
                    updated_data["DOI"] = ""
                    changed_fields.append("DOI")
            elif doi is not None and normalized_doi != _coerce_string(updated_data.get("DOI")):
                updated_data["DOI"] = normalized_doi or ""
                changed_fields.append("DOI")

            existing_tags = _extract_tag_names(updated_data.get("tags"))
            merged_tags = [tag for tag in existing_tags if tag not in set(tags_to_remove)]
            for tag in tags_to_add:
                if tag not in merged_tags:
                    merged_tags.append(tag)
            if merged_tags != existing_tags:
                updated_data["tags"] = [{"tag": tag} for tag in merged_tags]
                changed_fields.append("tags")

            existing_collections = [
                value for value in updated_data.get("collections") or [] if isinstance(value, str)
            ]
            merged_collections = [
                collection_key
                for collection_key in existing_collections
                if collection_key not in set(collections_to_remove)
            ]
            for collection_key in collections_to_add:
                if collection_key not in merged_collections:
                    merged_collections.append(collection_key)
            if merged_collections != existing_collections:
                updated_data["collections"] = merged_collections
                changed_fields.append("collections")

            if not changed_fields:
                raise ToolFailure("validation", "No effective metadata changes were requested")

            warning = (
                "Updated directly via Zotero API; bridge local search index may lag until its next sync."
            )
            if dry_run:
                return _summarize_patched_item(
                    item_key=normalized_item_key,
                    metadata=updated_data,
                    changed_fields=changed_fields,
                    status="would_update",
                )

            try:
                await zotero_client.update_item(
                    item_key=normalized_item_key,
                    version=int(raw_item.get("version", 0)),
                    data=updated_data,
                )
            except BridgeError as exc:
                raise _bridge_error_to_tool_failure(exc) from exc

            return _summarize_patched_item(
                item_key=normalized_item_key,
                metadata=updated_data,
                changed_fields=changed_fields,
                status="updated",
                warning=warning,
            )

    return await _run_tool(_impl())


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
