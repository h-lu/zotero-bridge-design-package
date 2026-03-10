from __future__ import annotations

import ipaddress
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings


@pytest.fixture
def test_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BRIDGE_API_KEY", "bridge-test-token")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ZOTERO_API_BASE", "https://api.zotero.org")
    monkeypatch.setenv("ZOTERO_LIBRARY_TYPE", "user")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "123456")
    monkeypatch.setenv("ZOTERO_API_KEY", "zotero-test-key")
    monkeypatch.setenv("DEFAULT_CITATION_STYLE", "apa")
    monkeypatch.setenv("DEFAULT_CITATION_LOCALE", "en-US")
    monkeypatch.setenv("FULLTEXT_DEFAULT_MAX_CHARS", "8000")
    monkeypatch.setenv("FULLTEXT_MAX_CHARS_HARD_LIMIT", "12000")
    monkeypatch.setenv("ENABLE_LOCAL_FULLTEXT_CACHE", "true")
    monkeypatch.setenv("LOCAL_FULLTEXT_CACHE_DIR", str(tmp_path / "fulltext-cache"))
    monkeypatch.setenv("ENABLE_LOCAL_SEARCH_INDEX", "true")
    monkeypatch.setenv("LOCAL_SEARCH_INDEX_DIR", str(tmp_path / "search-index"))
    monkeypatch.setenv("LOCAL_SEARCH_INDEX_REFRESH_SECONDS", "300")
    monkeypatch.setenv("MAX_UPLOAD_FILE_MB", "15")
    monkeypatch.setenv("STARTUP_VALIDATE_ZOTERO_KEY", "false")
    get_settings.cache_clear()


@pytest.fixture
async def async_client(test_env: None) -> AsyncGenerator[AsyncClient, None]:
    from app.main import app

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield client
    get_settings.cache_clear()


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer bridge-test-token"}


@pytest.fixture(autouse=True)
def patch_example_download_host_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.bridge_service import BridgeService

    original = BridgeService._resolve_download_host_ips

    async def patched(
        self: BridgeService,
        host: str,
        *,
        port: int,
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        if host.endswith(".example.com"):
            return [ipaddress.ip_address("93.184.216.34")]
        return await original(self, host, port=port)

    monkeypatch.setattr(BridgeService, "_resolve_download_host_ips", patched)
