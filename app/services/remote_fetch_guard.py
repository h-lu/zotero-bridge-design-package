from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from app.config import Settings
from app.errors import BridgeError


@dataclass(slots=True)
class RemoteFile:
    content: bytes
    content_type: str | None


class RemoteFetchGuard:
    def __init__(self, *, settings: Settings, http_client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http_client = http_client

    async def fetch_pdf(self, url: str) -> RemoteFile:
        current_url = self._normalize_remote_download_url(url)
        redirect_limit = max(self._settings.max_file_url_redirects, 0)
        for _ in range(redirect_limit + 1):
            await self._assert_safe_remote_download_url(current_url)
            try:
                async with self._http_client.stream(
                    "GET",
                    str(current_url),
                    timeout=120.0,
                    follow_redirects=False,
                    headers={"Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.1"},
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
                    self._validate_response_size(response.headers)
                    content = await self._read_limited_response(response)
                    content_type = self._normalize_content_type(
                        response.headers.get("Content-Type")
                    )
                    if not self._is_pdf_content(content=content, content_type=content_type):
                        raise BridgeError(
                            code="BAD_REQUEST",
                            message="Remote file must be a PDF",
                            status_code=400,
                        )
                    return RemoteFile(
                        content=content,
                        content_type=content_type or "application/pdf",
                    )
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
        if not parsed.host:
            raise BridgeError(
                code="BAD_REQUEST",
                message="Remote file URL must include a host",
                status_code=400,
            )
        if parsed.scheme == "https":
            return parsed
        if parsed.scheme == "http" and self._settings.allow_insecure_http_file_url:
            return parsed
        raise BridgeError(
            code="BAD_REQUEST",
            message="Remote file URL must use https",
            status_code=400,
        )

    async def _assert_safe_remote_download_url(self, url: httpx.URL) -> None:
        host = (url.host or "").rstrip(".").lower()
        if not host or host == "localhost" or host.endswith(".localhost"):
            self._raise_unsafe_remote_download_url()

        allowed_hosts = self._settings.allowed_file_source_hosts
        if allowed_hosts is not None and host not in allowed_hosts:
            raise BridgeError(
                code="BAD_REQUEST",
                message="Remote file URL host is not allowed",
                status_code=400,
            )

        resolved_addresses = await self._resolve_download_host_ips(
            host,
            port=url.port or self._default_port_for_scheme(url.scheme),
        )
        if not resolved_addresses:
            self._raise_unsafe_remote_download_url()
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

    def _validate_response_size(self, headers: httpx.Headers) -> None:
        content_length = headers.get("Content-Length")
        if content_length is None:
            return
        try:
            declared_size = int(content_length)
        except ValueError:
            return
        if declared_size > self._settings.max_upload_file_bytes:
            raise BridgeError(
                code="FILE_TOO_LARGE",
                message="Downloaded file exceeds configured size limit",
                status_code=413,
            )

    async def _read_limited_response(self, response: httpx.Response) -> bytes:
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > self._settings.max_upload_file_bytes:
                raise BridgeError(
                    code="FILE_TOO_LARGE",
                    message="Downloaded file exceeds configured size limit",
                    status_code=413,
                )
        return bytes(content)

    @staticmethod
    def _normalize_content_type(value: str | None) -> str | None:
        if not value:
            return None
        return value.split(";", 1)[0].strip().lower() or None

    @staticmethod
    def _looks_like_pdf(content: bytes) -> bool:
        header = content[:1024]
        marker_index = header.find(b"%PDF-")
        if marker_index == -1:
            return False
        return header[:marker_index].strip(b"\x00\t\n\r\f ") == b""

    def _is_pdf_content(self, *, content: bytes, content_type: str | None) -> bool:
        looks_like_pdf = self._looks_like_pdf(content)
        if looks_like_pdf:
            return True
        return content_type == "application/pdf"

    @staticmethod
    def _default_port_for_scheme(scheme: str) -> int:
        return 443 if scheme == "https" else 80

    @staticmethod
    def _raise_unsafe_remote_download_url() -> None:
        raise BridgeError(
            code="BAD_REQUEST",
            message="Remote file URL must resolve to a public host",
            status_code=400,
        )
