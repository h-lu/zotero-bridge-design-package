from __future__ import annotations

import ipaddress
import sys
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402


@pytest.fixture
def test_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ZOTERO_API_BASE", "https://api.zotero.org")
    monkeypatch.setenv("ZOTERO_LIBRARY_TYPE", "user")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "123456")
    monkeypatch.setenv("ZOTERO_API_KEY", "zotero-test-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "")
    monkeypatch.setenv("ENABLE_REQUEST_SCOPED_ZOTERO_KEY", "true")
    monkeypatch.setenv("DEFAULT_CITATION_STYLE", "apa")
    monkeypatch.setenv("DEFAULT_CITATION_LOCALE", "en-US")
    monkeypatch.setenv("ENABLE_LOCAL_SEARCH_INDEX", "true")
    monkeypatch.setenv("LOCAL_SEARCH_INDEX_DIR", str(tmp_path / "search-index"))
    monkeypatch.setenv("LOCAL_SEARCH_INDEX_REFRESH_SECONDS", "300")
    monkeypatch.setenv("MAX_UPLOAD_FILE_MB", "15")
    monkeypatch.setenv("MAX_FILE_URL_REDIRECTS", "3")
    monkeypatch.setenv("DOWNLOAD_HANDOFF_TTL_SECONDS", "900")
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
    return {"X-Zotero-API-Key": "zotero-test-key"}


@pytest.fixture(autouse=True)
def patch_example_download_host_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.remote_fetch_guard import RemoteFetchGuard
    from app.services.zotero_scope_resolver import ZoteroScopeResolver, ZoteroUserScope

    original = RemoteFetchGuard._resolve_download_host_ips
    original_resolve_scope = ZoteroScopeResolver.resolve_user_scope

    async def patched(
        self: RemoteFetchGuard,
        host: str,
        *,
        port: int,
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        if host.endswith(".example.com"):
            return [ipaddress.ip_address("93.184.216.34")]
        return await original(self, host, port=port)

    async def patched_scope_resolver(
        self: ZoteroScopeResolver,
        api_key: str,
    ) -> ZoteroUserScope:
        if api_key == "zotero-test-key":
            return ZoteroUserScope(
                api_key="zotero-test-key",
                library_type="user",
                library_id="123456",
            )
        return await original_resolve_scope(self, api_key)

    monkeypatch.setattr(RemoteFetchGuard, "_resolve_download_host_ips", patched)
    monkeypatch.setattr(ZoteroScopeResolver, "resolve_user_scope", patched_scope_resolver)
