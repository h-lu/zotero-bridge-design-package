from __future__ import annotations

import re
from html import unescape
from typing import Any
from urllib.parse import quote

import httpx

from app.errors import BridgeError

DOI_PATTERN = re.compile(r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", re.IGNORECASE)
CSL_JSON_ACCEPT = "application/vnd.citationstyles.csl+json"

CSL_TO_ZOTERO_ITEM_TYPE = {
    "article": "journalArticle",
    "article-journal": "journalArticle",
    "article-magazine": "magazineArticle",
    "article-newspaper": "newspaperArticle",
    "bill": "bill",
    "book": "book",
    "chapter": "bookSection",
    "entry": "encyclopediaArticle",
    "entry-dictionary": "dictionaryEntry",
    "motion_picture": "film",
    "paper-conference": "conferencePaper",
    "post": "forumPost",
    "post-weblog": "blogPost",
    "report": "report",
    "speech": "presentation",
    "thesis": "thesis",
    "webpage": "webpage",
}


class DOIResolver:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    @staticmethod
    def _lookup_failed(message: str, *, upstream_status: int | None = None) -> BridgeError:
        return BridgeError(
            code="DOI_RESOLUTION_FAILED",
            message=message,
            status_code=502,
            upstream_status=upstream_status,
        )

    @staticmethod
    def normalize_doi(value: str) -> str:
        candidate = value.strip()
        candidate = re.sub(r"^doi:\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"^https?://(dx\.)?doi\.org/", "", candidate, flags=re.IGNORECASE)
        candidate = candidate.strip()
        match = DOI_PATTERN.search(candidate)
        if not match:
            raise BridgeError(
                code="INVALID_DOI",
                message="Invalid DOI",
                status_code=400,
            )
        return match.group(1).rstrip(".,);]").lower()

    async def resolve(self, doi: str) -> dict[str, Any]:
        normalized = self.normalize_doi(doi)
        doi_payload = await self._fetch_content_negotiated_csl(normalized)
        if doi_payload is not None:
            return doi_payload
        crossref_payload = await self._fetch_crossref(normalized)
        if crossref_payload is not None:
            return crossref_payload
        raise BridgeError(
            code="DOI_RESOLUTION_FAILED",
            message="Unable to resolve DOI metadata",
            status_code=502,
        )

    async def _fetch_content_negotiated_csl(self, doi: str) -> dict[str, Any] | None:
        try:
            response = await self._client.get(
                f"https://doi.org/{quote(doi, safe='')}",
                headers={
                    "Accept": CSL_JSON_ACCEPT,
                    "User-Agent": "zotero-bridge/2.0.0",
                },
                timeout=20.0,
                follow_redirects=True,
            )
        except httpx.RequestError as exc:
            raise self._lookup_failed("DOI metadata lookup failed") from exc
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError as exc:
                raise self._lookup_failed(
                    "DOI metadata lookup failed",
                    upstream_status=response.status_code,
                ) from exc
            if isinstance(payload, dict):
                return payload
        if response.status_code in {404, 406}:
            return None
        return None

    async def _fetch_crossref(self, doi: str) -> dict[str, Any] | None:
        try:
            response = await self._client.get(
                f"https://api.crossref.org/works/{quote(doi, safe='')}",
                headers={"User-Agent": "zotero-bridge/2.0.0"},
                timeout=20.0,
            )
        except httpx.RequestError as exc:
            raise self._lookup_failed("Crossref metadata lookup failed") from exc
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise self._lookup_failed(
                "Crossref metadata lookup failed",
                upstream_status=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise self._lookup_failed(
                "Crossref metadata lookup failed",
                upstream_status=response.status_code,
            ) from exc
        message = payload.get("message")
        if not isinstance(message, dict):
            return None
        return message

    def guess_zotero_item_type(self, metadata: dict[str, Any]) -> str:
        raw_type = str(metadata.get("type", "")).strip()
        return CSL_TO_ZOTERO_ITEM_TYPE.get(raw_type, "journalArticle")

    def build_zotero_item(
        self,
        *,
        metadata: dict[str, Any],
        template: dict[str, Any],
        doi: str,
        collection_key: str | None,
        default_collection_key: str | None,
        tags: list[str],
    ) -> dict[str, Any]:
        item = dict(template)
        item_type = self.guess_zotero_item_type(metadata)
        item["itemType"] = item_type
        item["title"] = self._first_text(metadata.get("title")) or f"DOI {doi}"
        item["DOI"] = doi
        item["creators"] = self._map_creators(metadata, item_type)
        item["date"] = self._extract_date(metadata)
        item["url"] = self._extract_url(metadata, doi)
        item["tags"] = [{"tag": tag} for tag in tags]
        item["collections"] = [
            key
            for key in [collection_key or default_collection_key]
            if key is not None and key != ""
        ]

        for field_name, field_value in {
            "abstractNote": (
                self._first_text(metadata.get("abstract")) or metadata.get("abstract")
            ),
            "volume": self._first_text(metadata.get("volume")),
            "issue": self._first_text(metadata.get("issue")),
            "pages": self._first_text(metadata.get("page"))
            or self._first_text(metadata.get("pages")),
            "publicationTitle": self._first_text(metadata.get("container-title")),
            "bookTitle": self._first_text(metadata.get("container-title")),
            "websiteTitle": self._first_text(metadata.get("container-title")),
            "conferenceName": self._first_text(metadata.get("event-title")),
            "reportType": self._first_text(metadata.get("genre")),
            "publisher": self._first_text(metadata.get("publisher")),
            "place": self._first_text(metadata.get("publisher-place")),
            "language": self._first_text(metadata.get("language")),
            "ISSN": self._first_text(metadata.get("ISSN")),
            "ISBN": self._first_text(metadata.get("ISBN")),
        }.items():
            if field_name in item and field_value:
                item[field_name] = field_value

        if "url" not in template:
            item.pop("url", None)
        return item

    @staticmethod
    def _first_text(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, list):
            for item in value:
                text = DOIResolver._first_text(item)
                if text:
                    return text
            return None
        if isinstance(value, str):
            stripped = unescape(value).strip()
            return stripped or None
        return str(value)

    def _map_creators(self, metadata: dict[str, Any], item_type: str) -> list[dict[str, str]]:
        creator_type = "author"
        fallback_type = (
            "editor"
            if item_type in {"bookSection", "encyclopediaArticle", "dictionaryEntry"}
            else "author"
        )
        creators: list[dict[str, str]] = []

        for source_name, current_type in (
            ("author", creator_type),
            ("editor", "editor"),
            ("container-author", fallback_type),
        ):
            values = metadata.get(source_name)
            if not isinstance(values, list):
                continue
            for person in values:
                if not isinstance(person, dict):
                    continue
                if person.get("literal"):
                    creators.append({"creatorType": current_type, "name": str(person["literal"])})
                    continue
                family = self._first_text(person.get("family"))
                given = self._first_text(person.get("given"))
                if family:
                    creator: dict[str, str] = {"creatorType": current_type, "lastName": family}
                    if given:
                        creator["firstName"] = given
                    creators.append(creator)
                elif given:
                    creators.append({"creatorType": current_type, "name": given})
            if creators:
                break

        if creators:
            return creators
        return [{"creatorType": creator_type, "name": "Unknown"}]

    def _extract_date(self, metadata: dict[str, Any]) -> str:
        for field_name in ("issued", "published-print", "published-online", "created"):
            field_value = metadata.get(field_name)
            if not isinstance(field_value, dict):
                continue
            date_parts = field_value.get("date-parts")
            if isinstance(date_parts, list) and date_parts and isinstance(date_parts[0], list):
                parts = [str(part) for part in date_parts[0] if part is not None]
                if parts:
                    return "-".join(parts)
        year = metadata.get("year")
        if isinstance(year, str) and year.strip():
            return year.strip()
        return ""

    def _extract_url(self, metadata: dict[str, Any], doi: str) -> str:
        for field_name in ("URL", "url"):
            text = self._first_text(metadata.get(field_name))
            if text:
                return text
        return f"https://doi.org/{doi}"
