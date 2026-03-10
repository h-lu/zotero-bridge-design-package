from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.config import Settings
from app.errors import BridgeError


class ZoteroClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    async def validate_key(self) -> bool:
        response = await self._request(
            "GET",
            self._library_path("items"),
            params={"limit": 1},
            expected_statuses={200},
        )
        return response.status_code == 200

    async def search_items_raw(
        self,
        *,
        q: str,
        limit: int,
        include_fulltext: bool,
    ) -> list[dict[str, Any]]:
        items, _ = await self.search_items_page_raw(
            q=q,
            start=0,
            limit=limit,
            include_fulltext=include_fulltext,
            item_type=None,
            tag=None,
            collection_key=None,
            sort=None,
            direction=None,
        )
        return items

    async def search_items_page_raw(
        self,
        *,
        q: str,
        start: int,
        limit: int,
        include_fulltext: bool,
        item_type: str | None,
        tag: str | None,
        collection_key: str | None,
        sort: str | None,
        direction: str | None,
    ) -> tuple[list[dict[str, Any]], int]:
        params: dict[str, Any] = {
            "q": q,
            "start": start,
            "limit": limit,
            "format": "json",
        }
        if include_fulltext:
            params["qmode"] = "everything"
        if item_type:
            params["itemType"] = item_type
        if tag:
            params["tag"] = tag
        if sort:
            params["sort"] = sort
        if direction:
            params["direction"] = direction

        response = await self._request(
            "GET",
            self._items_path(collection_key=collection_key, top_level=False),
            params=params,
            expected_statuses={200},
        )
        payload = response.json()
        items = payload if isinstance(payload, list) else []
        return items, self._total_results(response)

    async def list_top_level_items_raw(
        self,
        *,
        start: int,
        limit: int,
        item_type: str | None,
        collection_key: str | None,
        tag: str | None,
        sort: str,
        direction: str,
    ) -> tuple[list[dict[str, Any]], int]:
        params: dict[str, Any] = {
            "format": "json",
            "start": start,
            "limit": limit,
            "sort": sort,
            "direction": direction,
        }
        if item_type:
            params["itemType"] = item_type
        if tag:
            params["tag"] = tag

        response = await self._request(
            "GET",
            self._items_path(collection_key=collection_key, top_level=True),
            params=params,
            expected_statuses={200},
        )
        payload = response.json()
        items = payload if isinstance(payload, list) else []
        return items, self._total_results(response)

    async def list_top_level_item_versions(
        self,
        *,
        since_version: int | None,
    ) -> tuple[dict[str, int], int | None]:
        params: dict[str, Any] = {"format": "versions"}
        if since_version is not None:
            params["since"] = since_version
        response = await self._request(
            "GET",
            self._library_path("items", "top"),
            params=params,
            expected_statuses={200},
        )
        payload = response.json()
        versions: dict[str, int] = {}
        if isinstance(payload, dict):
            for key, value in payload.items():
                if isinstance(key, str):
                    try:
                        versions[key] = int(value)
                    except (TypeError, ValueError):
                        continue
        return versions, self._last_modified_version(response)

    async def get_items_by_keys_raw(self, item_keys: list[str]) -> list[dict[str, Any]]:
        normalized_keys = [key.strip() for key in item_keys if key.strip()]
        if not normalized_keys:
            return []
        response = await self._request(
            "GET",
            self._library_path("items"),
            params={
                "format": "json",
                "itemKey": ",".join(normalized_keys),
            },
            expected_statuses={200},
        )
        payload = response.json()
        return payload if isinstance(payload, list) else []

    async def list_collections_raw(
        self,
        *,
        start: int,
        limit: int,
        top_level_only: bool,
    ) -> tuple[list[dict[str, Any]], int]:
        response = await self._request(
            "GET",
            self._library_path("collections", "top" if top_level_only else ""),
            params={"format": "json", "start": start, "limit": limit},
            expected_statuses={200},
        )
        payload = response.json()
        collections = payload if isinstance(payload, list) else []
        return collections, self._total_results(response)

    async def get_deleted_item_keys(self, *, since_version: int) -> list[str]:
        response = await self._request(
            "GET",
            self._library_path("deleted"),
            params={"since": since_version},
            expected_statuses={200},
        )
        payload = response.json()
        if not isinstance(payload, dict):
            return []
        deleted_items = payload.get("items", [])
        if not isinstance(deleted_items, list):
            return []
        return [str(value) for value in deleted_items if isinstance(value, str)]

    async def list_tags_raw(
        self,
        *,
        start: int,
        limit: int,
        q: str | None,
        top_level_only: bool,
        collection_key: str | None,
    ) -> tuple[list[dict[str, Any]], int]:
        params: dict[str, Any] = {
            "format": "json",
            "start": start,
            "limit": limit,
        }
        if q:
            params["q"] = q
        if collection_key:
            path = self._library_path(
                "collections",
                collection_key,
                "items",
                "top" if top_level_only else "",
                "tags",
            )
        else:
            path = self._library_path("items", "top" if top_level_only else "", "tags")
        response = await self._request(
            "GET",
            path,
            params=params,
            expected_statuses={200},
        )
        payload = response.json()
        tags = payload if isinstance(payload, list) else []
        return tags, self._total_results(response)

    async def get_item(self, item_key: str) -> dict[str, Any]:
        response = await self._request(
            "GET",
            self._library_path("items", item_key),
            params={"format": "json"},
            expected_statuses={200},
            not_found_code="ITEM_NOT_FOUND",
            not_found_message="Item not found",
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise BridgeError(
                code="UPSTREAM_ERROR",
                message="Unexpected Zotero item payload",
                status_code=502,
            )
        return payload

    async def get_children(self, item_key: str) -> list[dict[str, Any]]:
        children: list[dict[str, Any]] = []
        start = 0
        page_size = 100
        while True:
            response = await self._request(
                "GET",
                self._library_path("items", item_key, "children"),
                params={"format": "json", "limit": page_size, "start": start},
                expected_statuses={200},
                not_found_code="ITEM_NOT_FOUND",
                not_found_message="Item not found",
            )
            payload = response.json()
            if not isinstance(payload, list):
                return children
            children.extend(payload)
            if len(payload) < page_size:
                return children
            start += len(payload)

    async def get_item_template(
        self,
        item_type: str,
        link_mode: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"itemType": item_type}
        if link_mode:
            params["linkMode"] = link_mode
        response = await self._request(
            "GET",
            "/items/new",
            params=params,
            expected_statuses={200},
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise BridgeError(
                code="UPSTREAM_ERROR",
                message="Unexpected Zotero template payload",
                status_code=502,
            )
        return payload

    async def create_items(
        self,
        items: list[dict[str, Any]],
        *,
        write_token: str,
    ) -> list[str]:
        response = await self._request(
            "POST",
            self._library_path("items"),
            json=items,
            expected_statuses={200, 201, 412},
            write_token=write_token,
            retryable=True,
        )
        if response.status_code == 412:
            raise BridgeError(
                code="WRITE_CONFLICT",
                message="Write token conflict",
                status_code=409,
                upstream_status=412,
            )
        payload = response.json()
        successful = payload.get("successful", {})
        if not isinstance(successful, dict):
            raise BridgeError(
                code="UPSTREAM_ERROR",
                message="Zotero create response did not include success mapping",
                status_code=502,
            )
        keys = [
            self._extract_created_key(value)
            for _, value in sorted(successful.items(), key=lambda entry: entry[0])
        ]
        if not keys:
            raise BridgeError(
                code="UPSTREAM_ERROR",
                message="Zotero did not report a created item key",
                status_code=502,
            )
        return keys

    async def update_item(
        self,
        *,
        item_key: str,
        data: dict[str, Any],
        version: int,
    ) -> None:
        response = await self._request(
            "PATCH",
            self._library_path("items", item_key),
            json=data,
            headers={"If-Unmodified-Since-Version": str(version)},
            expected_statuses={204, 200, 412},
            retryable=True,
            not_found_code="ITEM_NOT_FOUND",
            not_found_message="Item not found",
        )
        if response.status_code != 412:
            return
        raise BridgeError(
            code="WRITE_CONFLICT",
            message="Zotero item update conflicted",
            status_code=409,
            upstream_status=412,
        )

    async def delete_item(
        self,
        *,
        item_key: str,
        version: int,
    ) -> None:
        current_version = version
        for _ in range(2):
            response = await self._request(
                "DELETE",
                self._library_path("items", item_key),
                headers={"If-Unmodified-Since-Version": str(current_version)},
                expected_statuses={204, 200, 412},
                retryable=True,
                not_found_code="ITEM_NOT_FOUND",
                not_found_message="Item not found",
            )
            if response.status_code != 412:
                return
            latest = await self.get_item(item_key)
            current_version = int(latest.get("version", current_version))
        raise BridgeError(
            code="WRITE_CONFLICT",
            message="Zotero item delete conflicted after retry",
            status_code=409,
            upstream_status=412,
        )

    async def get_fulltext(self, attachment_key: str) -> dict[str, Any]:
        response = await self._request(
            "GET",
            self._library_path("items", attachment_key, "fulltext"),
            expected_statuses={200},
            not_found_code="FULLTEXT_NOT_AVAILABLE",
            not_found_message="Full text is not available for this attachment",
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise BridgeError(
                code="UPSTREAM_ERROR",
                message="Unexpected Zotero fulltext payload",
                status_code=502,
            )
        return payload

    async def get_citation(
        self,
        *,
        item_key: str,
        style: str,
        locale: str,
        linkwrap: bool,
    ) -> dict[str, Any]:
        response = await self._request(
            "GET",
            self._library_path("items", item_key),
            params={
                "format": "json",
                "include": "citation,bib",
                "style": style,
                "locale": locale,
                "linkwrap": int(linkwrap),
            },
            expected_statuses={200},
            not_found_code="ITEM_NOT_FOUND",
            not_found_message="Item not found",
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise BridgeError(
                code="UPSTREAM_ERROR",
                message="Unexpected Zotero citation payload",
                status_code=502,
            )
        return payload

    async def authorize_upload(
        self,
        *,
        attachment_key: str,
        filename: str,
        md5: str,
        filesize: int,
        mtime_ms: int,
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            self._library_path("items", attachment_key, "file"),
            data={
                "md5": md5,
                "filename": filename,
                "filesize": str(filesize),
                "mtime": str(mtime_ms),
            },
            headers={"If-None-Match": "*"},
            expected_statuses={200, 201},
            retryable=True,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise BridgeError(
                code="UPSTREAM_ERROR",
                message="Unexpected Zotero upload authorization payload",
                status_code=502,
            )
        return payload

    async def upload_to_authorized_url(
        self,
        authorization: dict[str, Any],
        content: bytes,
    ) -> None:
        upload_url = authorization.get("url")
        if not isinstance(upload_url, str) or not upload_url:
            raise BridgeError(
                code="UPSTREAM_ERROR",
                message="Upload authorization did not include a target URL",
                status_code=502,
            )

        prefix = authorization.get("prefix", "")
        suffix = authorization.get("suffix", "")
        upload_body = self._coerce_binary(prefix) + content + self._coerce_binary(suffix)

        headers = {}
        content_type = authorization.get("contentType")
        if isinstance(content_type, str) and content_type:
            headers["Content-Type"] = content_type

        params = authorization.get("params")
        data: Any = None
        files: Any = None
        body: bytes | None = upload_body
        if isinstance(params, dict) and params:
            data = {str(key): str(value) for key, value in params.items()}
            files = {"file": ("upload.bin", upload_body)}
            body = None

        try:
            response = await self._client.post(
                upload_url,
                content=body,
                data=data,
                files=files,
                headers=headers,
                timeout=120.0,
                follow_redirects=True,
            )
        except httpx.RequestError as exc:
            raise BridgeError(
                code="UPLOAD_FAILED",
                message="Authorized file upload failed",
                status_code=502,
            ) from exc
        if response.status_code not in {200, 201, 204}:
            raise BridgeError(
                code="UPLOAD_FAILED",
                message="Authorized file upload failed",
                status_code=502,
                upstream_status=response.status_code,
            )

    async def register_upload(self, *, attachment_key: str, upload_key: str) -> None:
        await self._request(
            "POST",
            self._library_path("items", attachment_key, "file"),
            data={"upload": upload_key},
            headers={"If-None-Match": "*"},
            expected_statuses={200, 201, 204},
            retryable=True,
        )

    async def find_top_level_attachments(self, *, filename: str) -> list[dict[str, Any]]:
        raw_items = await self.search_items_raw(
            q=filename,
            limit=25,
            include_fulltext=False,
        )
        attachments: list[dict[str, Any]] = []
        for item in raw_items:
            data = item.get("data", {})
            if data.get("itemType") == "attachment" and not data.get("parentItem"):
                attachments.append(item)
        return attachments

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
        expected_statuses: set[int],
        retryable: bool = False,
        write_token: str | None = None,
        not_found_code: str | None = None,
        not_found_message: str | None = None,
    ) -> httpx.Response:
        merged_headers = self._default_headers()
        if headers:
            merged_headers.update(headers)
        if write_token:
            merged_headers["Zotero-Write-Token"] = write_token

        max_retries = 3 if retryable or method.upper() == "GET" else 0
        attempt = 0
        while True:
            try:
                response = await self._client.request(
                    method,
                    f"{self._settings.zotero_api_base.rstrip('/')}{path}",
                    params=params,
                    json=json,
                    data=data,
                    headers=merged_headers,
                    timeout=60.0,
                    follow_redirects=True,
                )
            except httpx.RequestError as exc:
                if attempt < max_retries:
                    await asyncio.sleep(min(2**attempt, 8))
                    attempt += 1
                    continue
                raise BridgeError(
                    code="UPSTREAM_ERROR",
                    message="Zotero upstream request failed",
                    status_code=502,
                ) from exc
            if response.status_code in expected_statuses:
                return response
            if response.status_code == 404 and not_found_code and not_found_message:
                raise BridgeError(
                    code=not_found_code,
                    message=not_found_message,
                    status_code=404,
                    upstream_status=404,
                )
            if response.status_code == 412:
                return response
            if attempt < max_retries and response.status_code in {429, 500, 502, 503, 504}:
                delay = self._backoff_seconds(response, attempt)
                attempt += 1
                await asyncio.sleep(delay)
                continue
            raise self._map_error(response)

    def _default_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.zotero_api_key}",
            "Zotero-API-Version": str(self._settings.zotero_api_version),
            "Accept": "application/json",
        }

    def _library_path(self, *parts: str) -> str:
        suffix = "/".join(part.strip("/") for part in parts if part)
        base = self._settings.zotero_library_path.rstrip("/")
        return f"{base}/{suffix}" if suffix else base

    def _items_path(self, *, collection_key: str | None, top_level: bool) -> str:
        if collection_key:
            return self._library_path(
                "collections",
                collection_key,
                "items",
                "top" if top_level else "",
            )
        return self._library_path("items", "top" if top_level else "")

    def _backoff_seconds(self, response: httpx.Response, attempt: int) -> float:
        for header_name in ("Backoff", "Retry-After"):
            header_value = response.headers.get(header_name)
            if header_value:
                try:
                    return min(float(header_value), 30.0)
                except ValueError:
                    pass
        return min(2**attempt, 8)

    @staticmethod
    def _total_results(response: httpx.Response) -> int:
        header_value = response.headers.get("Total-Results")
        if header_value is None:
            return 0
        try:
            return max(int(header_value), 0)
        except ValueError:
            return 0

    @staticmethod
    def _last_modified_version(response: httpx.Response) -> int | None:
        header_value = response.headers.get("Last-Modified-Version")
        if header_value is None:
            return None
        try:
            return max(int(header_value), 0)
        except ValueError:
            return None

    def _map_error(self, response: httpx.Response) -> BridgeError:
        if response.status_code == 401:
            return BridgeError(
                code="UPSTREAM_UNAUTHORIZED",
                message="Zotero credentials were rejected",
                status_code=502,
                upstream_status=401,
            )
        if response.status_code == 403:
            return BridgeError(
                code="UPSTREAM_FORBIDDEN",
                message="Zotero access is forbidden",
                status_code=502,
                upstream_status=403,
            )
        if response.status_code == 409:
            return BridgeError(
                code="WRITE_CONFLICT",
                message="Zotero reported a write conflict",
                status_code=409,
                upstream_status=409,
            )
        if response.status_code in {429, 500, 502, 503, 504}:
            return BridgeError(
                code="UPSTREAM_ERROR",
                message="Zotero upstream request failed",
                status_code=502,
                upstream_status=response.status_code,
            )
        detail = response.text[:300].strip()
        if detail:
            message = f"Unexpected Zotero response: {detail}"
        else:
            message = "Unexpected Zotero response"
        return BridgeError(
            code="UPSTREAM_ERROR",
            message=message,
            status_code=502,
            upstream_status=response.status_code,
        )

    @staticmethod
    def _coerce_binary(value: Any) -> bytes:
        if isinstance(value, bytes):
            return value
        if value is None:
            return b""
        return str(value).encode("utf-8")

    @staticmethod
    def _extract_created_key(value: Any) -> str:
        if isinstance(value, dict):
            key = value.get("key")
            if isinstance(key, str) and key:
                return key
        if isinstance(value, str) and value:
            return value
        raise BridgeError(
            code="UPSTREAM_ERROR",
            message="Zotero did not report a usable created item key",
            status_code=502,
        )
