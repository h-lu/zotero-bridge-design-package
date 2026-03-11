from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from app.errors import BridgeError
from app.services.remote_fetch_guard import RemoteFetchGuard

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF"


@pytest.mark.asyncio
async def test_fetch_pdf_rejects_http_by_default(test_env: None) -> None:
    settings = get_settings()
    async with httpx.AsyncClient() as client:
        guard = RemoteFetchGuard(settings=settings, http_client=client)
        with pytest.raises(BridgeError) as exc_info:
            await guard.fetch_pdf("http://files.example.com/paper.pdf")

    assert exc_info.value.code == "BAD_REQUEST"
    assert "https" in exc_info.value.message


@pytest.mark.asyncio
async def test_fetch_pdf_rejects_local_and_private_hosts(test_env: None) -> None:
    settings = get_settings()
    async with httpx.AsyncClient() as client:
        guard = RemoteFetchGuard(settings=settings, http_client=client)

        with pytest.raises(BridgeError) as localhost_exc:
            await guard.fetch_pdf("https://localhost/paper.pdf")
        with pytest.raises(BridgeError) as private_exc:
            await guard.fetch_pdf("https://192.168.1.25/paper.pdf")

    assert localhost_exc.value.code == "BAD_REQUEST"
    assert private_exc.value.code == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_fetch_pdf_rejects_redirect_to_unsafe_host(test_env: None) -> None:
    settings = get_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"Location": "https://127.0.0.1/paper.pdf"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        guard = RemoteFetchGuard(settings=settings, http_client=client)
        with pytest.raises(BridgeError) as exc_info:
            await guard.fetch_pdf("https://files.example.com/paper.pdf")

    assert exc_info.value.code == "BAD_REQUEST"
    assert "public host" in exc_info.value.message


@pytest.mark.asyncio
async def test_fetch_pdf_rejects_large_file(test_env: None) -> None:
    settings = get_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/pdf",
                "Content-Length": str(settings.max_upload_file_bytes + 1),
            },
            content=PDF_BYTES,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        guard = RemoteFetchGuard(settings=settings, http_client=client)
        with pytest.raises(BridgeError) as exc_info:
            await guard.fetch_pdf("https://files.example.com/paper.pdf")

    assert exc_info.value.code == "FILE_TOO_LARGE"


@pytest.mark.asyncio
async def test_fetch_pdf_accepts_pdf_even_when_content_type_is_generic(test_env: None) -> None:
    settings = get_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/octet-stream"},
            content=PDF_BYTES,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        guard = RemoteFetchGuard(settings=settings, http_client=client)
        remote_file = await guard.fetch_pdf("https://files.example.com/paper.pdf")

    assert remote_file.content == PDF_BYTES
