# Codex Handoff Prompt

Use this prompt as-is in Codex for implementation.

---

Build a production-ready service called `zotero-bridge` from the files in this directory.

## Objectives

Implement a public HTTPS-ready REST bridge that:

1. writes to Zotero using the Zotero Web API v3
2. reads from Zotero using the Zotero Web API v3
3. is usable by:
   - ChatGPT via GPT Actions (`openapi.actions.yaml`)
   - Codex/direct HTTP via `openapi.full.yaml`
4. stores AI reading outputs back into Zotero as child notes

## Hard requirements

- Language: Python 3.12
- Framework: FastAPI
- HTTP client: httpx (async)
- Settings: pydantic-settings
- Package manager: uv
- Tests: pytest
- Mock upstream HTTP: respx
- Lint/type checks: ruff + mypy
- Use `openapi.full.yaml` as the source of truth for endpoint behavior
- Ensure `openapi.actions.yaml` stays importable into ChatGPT Actions
- Do NOT implement MCP in this repo
- Do NOT expose the Zotero API key to clients
- Do NOT expose `localhost:23119`
- Keep the service stateless for MVP
- All create endpoints must use `Zotero-Write-Token`
- All write operations must be idempotent when `requestId` is provided
- Use markdown-to-sanitized-HTML conversion for AI notes
- Add structured JSON error responses

## Deliverables

Create:

- `app/main.py`
- `app/config.py`
- `app/auth.py`
- `app/models.py`
- `app/routes/*.py`
- `app/services/zotero_client.py`
- `app/services/doi_resolver.py`
- `app/services/note_renderer.py`
- `app/services/fulltext.py`
- `tests/unit/*`
- `tests/integration/*`
- `pyproject.toml`
- `uv.lock` if applicable
- `Dockerfile`
- `README_IMPLEMENTATION.md`

## Endpoint implementation scope

Implement these public endpoints:

- `GET /healthz`
- `POST /v1/papers/add-by-doi`
- `POST /v1/papers/upload-pdf-action`
- `POST /v1/papers/upload-pdf-multipart`
- `GET /v1/items/search`
- `GET /v1/items/{itemKey}`
- `GET /v1/items/{itemKey}/fulltext`
- `POST /v1/items/{itemKey}/notes/upsert-ai-note`
- `GET /v1/items/{itemKey}/citation`

## Upstream details

For all Zotero requests:
- send `Zotero-API-Version: 3`
- send `Authorization: Bearer <ZOTERO_API_KEY>`

Use official Zotero item templates where required.

## DOI resolution logic

Implement:
1. DOI normalization
2. DOI content negotiation first
3. Crossref fallback if needed
4. mapping into valid Zotero JSON

## AI note logic

Identity of an AI note is:
- parent item key
- agent
- note type
- slot

Use these tags:
- `zbridge`
- `zbridge:agent:<agent>`
- `zbridge:type:<noteType>`
- `zbridge:slot:<slot>`

`replace` mode updates the same note.
`append` mode appends to the same note.

## Fulltext logic

- resolve PDF attachment
- fetch full text from Zotero Web API
- chunk by character offset with paragraph-friendly splitting
- default `maxChars=8000`, hard max `12000`

## Error behavior

Return JSON:
```json
{
  "error": {
    "code": "SOME_CODE",
    "message": "Human readable message",
    "upstreamStatus": 404,
    "requestId": "..."
  }
}
```

## Verification tasks

After implementation:

1. run unit tests
2. run integration tests
3. validate OpenAPI generation
4. run local smoke tests against mocked upstream
5. document how to run live tests with real Zotero credentials

## Important constraints for ChatGPT Actions

Design responses to stay compact:
- no large note bodies in search or detail responses
- full text only from `/fulltext`
- keep payloads comfortably below the Actions 100k character limit

## Nice-to-have

- file hash utility helpers
- retry wrapper honoring `Backoff` and `Retry-After`
- startup validation that checks `/keys/<key>`
- small Python CLI helper under `tools/` for local PDF upload

When finished, output:
- what you implemented
- any deviations from the spec
- test results
- any unresolved edge cases

---
