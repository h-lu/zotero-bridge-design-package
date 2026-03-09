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

    bridge_api_key: str = Field(..., alias="BRIDGE_API_KEY")

    app_env: str = Field("production", alias="APP_ENV")
    app_host: str = Field("0.0.0.0", alias="APP_HOST")
    app_port: int = Field(8080, alias="APP_PORT")
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    zotero_api_base: str = Field("https://api.zotero.org", alias="ZOTERO_API_BASE")
    zotero_api_version: int = Field(3, alias="ZOTERO_API_VERSION")
    zotero_library_type: Literal["user", "group"] = Field(
        "user",
        alias="ZOTERO_LIBRARY_TYPE",
    )
    zotero_library_id: str = Field(..., alias="ZOTERO_LIBRARY_ID")
    zotero_api_key: str = Field(..., alias="ZOTERO_API_KEY")

    default_collection_key: str | None = Field(None, alias="DEFAULT_COLLECTION_KEY")
    default_note_tag_prefix: str = Field("zbridge", alias="DEFAULT_NOTE_TAG_PREFIX")
    default_citation_style: str = Field("apa", alias="DEFAULT_CITATION_STYLE")
    default_citation_locale: str = Field("en-US", alias="DEFAULT_CITATION_LOCALE")
    fulltext_default_max_chars: int = Field(8000, alias="FULLTEXT_DEFAULT_MAX_CHARS")
    fulltext_max_chars_hard_limit: int = Field(12000, alias="FULLTEXT_MAX_CHARS_HARD_LIMIT")
    enable_local_fulltext_cache: bool = Field(True, alias="ENABLE_LOCAL_FULLTEXT_CACHE")
    local_fulltext_cache_dir: str = Field(
        ".cache/fulltext",
        alias="LOCAL_FULLTEXT_CACHE_DIR",
    )

    max_action_request_chars: int = Field(100000, alias="MAX_ACTION_REQUEST_CHARS")
    max_upload_file_mb: int = Field(15, alias="MAX_UPLOAD_FILE_MB")

    enable_local_relay: bool = Field(False, alias="ENABLE_LOCAL_RELAY")
    relay_shared_token: str | None = Field(None, alias="RELAY_SHARED_TOKEN")
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
    def local_fulltext_cache_path(self) -> Path:
        return Path(self.local_fulltext_cache_dir)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
