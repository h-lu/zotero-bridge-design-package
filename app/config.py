from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = Field("production", alias="APP_ENV")
    app_host: str = Field("0.0.0.0", alias="APP_HOST")
    app_port: int = Field(8080, alias="APP_PORT")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    public_base_url: str | None = Field(None, alias="PUBLIC_BASE_URL")
    enable_request_scoped_zotero_key: bool = Field(
        True,
        alias="ENABLE_REQUEST_SCOPED_ZOTERO_KEY",
    )

    zotero_api_base: str = Field("https://api.zotero.org", alias="ZOTERO_API_BASE")
    zotero_api_version: int = Field(3, alias="ZOTERO_API_VERSION")
    zotero_library_type: Literal["user", "group"] = Field(
        "user",
        alias="ZOTERO_LIBRARY_TYPE",
    )
    zotero_library_id: str = Field(..., alias="ZOTERO_LIBRARY_ID")
    zotero_api_key: str = Field(..., alias="ZOTERO_API_KEY")
    openalex_api_base: str = Field("https://api.openalex.org", alias="OPENALEX_API_BASE")
    openalex_api_key: str | None = Field(None, alias="OPENALEX_API_KEY")

    default_collection_key: str | None = Field(None, alias="DEFAULT_COLLECTION_KEY")
    default_note_tag_prefix: str = Field("zbridge", alias="DEFAULT_NOTE_TAG_PREFIX")
    default_citation_style: str = Field("apa", alias="DEFAULT_CITATION_STYLE")
    default_citation_locale: str = Field("en-US", alias="DEFAULT_CITATION_LOCALE")
    enable_local_search_index: bool = Field(True, alias="ENABLE_LOCAL_SEARCH_INDEX")
    local_search_index_dir: str = Field(
        ".cache/search-index",
        alias="LOCAL_SEARCH_INDEX_DIR",
    )
    local_search_index_refresh_seconds: int = Field(
        300,
        alias="LOCAL_SEARCH_INDEX_REFRESH_SECONDS",
    )
    note_search_cache_ttl_seconds: int = Field(
        120,
        alias="NOTE_SEARCH_CACHE_TTL_SECONDS",
    )

    max_action_request_chars: int = Field(100000, alias="MAX_ACTION_REQUEST_CHARS")
    max_upload_file_mb: int = Field(15, alias="MAX_UPLOAD_FILE_MB")
    allow_insecure_http_file_url: bool = Field(False, alias="ALLOW_INSECURE_HTTP_FILE_URL")
    max_file_url_redirects: int = Field(3, alias="MAX_FILE_URL_REDIRECTS")
    allowed_file_source_hosts_raw: str | None = Field(None, alias="ALLOWED_FILE_SOURCE_HOSTS")
    download_handoff_ttl_seconds: int = Field(900, alias="DOWNLOAD_HANDOFF_TTL_SECONDS")

    startup_validate_zotero_key: bool = Field(False, alias="STARTUP_VALIDATE_ZOTERO_KEY")

    @property
    def zotero_library_path(self) -> str:
        return f"/{self.zotero_library_type}s/{self.zotero_library_id}"

    @property
    def max_upload_file_bytes(self) -> int:
        return self.max_upload_file_mb * 1024 * 1024

    @property
    def zotero_configured(self) -> bool:
        return bool(self.zotero_api_key and self.zotero_library_id)

    @property
    def local_search_index_path(self) -> Path:
        return Path(self.local_search_index_dir)

    @property
    def allowed_file_source_hosts(self) -> set[str] | None:
        raw_value = (self.allowed_file_source_hosts_raw or "").strip()
        if not raw_value:
            return None
        hosts = {host.strip().lower() for host in raw_value.split(",") if host.strip()}
        return hosts or None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
