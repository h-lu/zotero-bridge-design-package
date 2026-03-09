from __future__ import annotations

from pathlib import Path

import yaml


def test_generated_openapi_matches_full_contract(test_env: None) -> None:
    from app.main import app

    generated = app.openapi()
    full_contract = yaml.safe_load(Path("openapi.full.yaml").read_text(encoding="utf-8"))

    assert set(generated["paths"]) == set(full_contract["paths"])
    for path, operations in full_contract["paths"].items():
        for method, operation in operations.items():
            assert generated["paths"][path][method]["operationId"] == operation["operationId"]


def test_actions_openapi_is_parseable_and_subset() -> None:
    full_contract = yaml.safe_load(Path("openapi.full.yaml").read_text(encoding="utf-8"))
    actions_contract = yaml.safe_load(Path("openapi.actions.yaml").read_text(encoding="utf-8"))

    assert set(actions_contract["paths"]).issubset(set(full_contract["paths"]))
    assert "/v1/papers/upload-pdf-multipart" not in actions_contract["paths"]

