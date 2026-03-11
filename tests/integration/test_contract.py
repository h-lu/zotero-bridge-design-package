from __future__ import annotations

from pathlib import Path

import yaml


def test_generated_openapi_matches_full_contract(test_env: None) -> None:
    from app.main import app

    generated = app.openapi()
    full_contract = yaml.safe_load(Path("openapi.full.yaml").read_text(encoding="utf-8"))

    assert "servers" not in generated
    assert "servers" not in full_contract
    assert set(generated["paths"]) == set(full_contract["paths"])
    assert "/v1/items/{itemKey}/fulltext" not in generated["paths"]
    assert "/v1/items/fulltext/batch-preview" not in generated["paths"]
    review_pack_properties = generated["components"]["schemas"]["ReviewPackRequest"]["properties"]
    assert "includeFulltextPreview" not in review_pack_properties
    assert "maxFulltextChars" not in review_pack_properties
    for path, operations in full_contract["paths"].items():
        for method, operation in operations.items():
            assert generated["paths"][path][method]["operationId"] == operation["operationId"]


def test_actions_openapi_is_parseable_and_subset() -> None:
    full_contract = yaml.safe_load(Path("openapi.full.yaml").read_text(encoding="utf-8"))
    actions_contract = yaml.safe_load(Path("openapi.actions.yaml").read_text(encoding="utf-8"))

    assert "servers" not in actions_contract
    assert set(actions_contract["paths"]).issubset(set(full_contract["paths"]))
    assert "/v1/papers/upload-pdf-multipart" not in actions_contract["paths"]
    assert "/v1/attachments/download/{token}" in actions_contract["paths"]
