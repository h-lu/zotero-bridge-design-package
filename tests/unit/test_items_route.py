from __future__ import annotations

import importlib
from inspect import signature

import pytest

from app.config import get_settings


def test_fulltext_route_uses_configured_query_limits(test_env: None) -> None:
    import app.routes.items as items_module

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("FULLTEXT_DEFAULT_MAX_CHARS", "9000")
        monkeypatch.setenv("FULLTEXT_MAX_CHARS_HARD_LIMIT", "15000")
        get_settings.cache_clear()
        items_module = importlib.reload(items_module)
        query = signature(items_module.get_item_fulltext).parameters["maxChars"].default
        constraints = {type(item).__name__: item for item in query.metadata}

        assert query.default == 9000
        assert constraints["Ge"].ge == 1000
        assert constraints["Le"].le == 15000

    get_settings.cache_clear()
    importlib.reload(items_module)
