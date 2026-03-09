# Zotero Bridge Implementation

`zotero-bridge` is a FastAPI service that exposes a public HTTPS-ready REST API in front of Zotero Web API v3.

## Implemented endpoints

- `GET /healthz`
- `POST /v1/papers/add-by-doi`
- `POST /v1/papers/upload-pdf-action`
- `POST /v1/papers/upload-pdf-multipart`
- `GET /v1/items/search`
- `GET /v1/items/{itemKey}`
- `GET /v1/items/{itemKey}/notes`
- `POST /v1/items/{itemKey}/notes`
- `GET /v1/items/{itemKey}/fulltext`
- `POST /v1/items/{itemKey}/notes/upsert-ai-note`
- `GET /v1/items/{itemKey}/citation`
- `GET /v1/notes/{noteKey}`
- `PATCH /v1/notes/{noteKey}`
- `DELETE /v1/notes/{noteKey}`

## Main implementation points

- FastAPI app with bearer-token bridge auth.
- Async upstream access with `httpx`.
- `pydantic-settings` configuration from environment variables.
- DOI resolution via DOI content negotiation first, Crossref fallback second.
- AI notes stored as sanitized HTML child notes with deterministic identity tags.
- Generic item notes can be listed, created, read, and updated through dedicated note endpoints.
- Generic item notes can also be deleted through the dedicated note endpoint.
- PDF upload flow implemented as `create attachment -> authorize -> upload -> register`.
- Full text served only through the dedicated `/fulltext` endpoint and chunked by cursor.
- Uploaded PDFs are also extracted into a local cache, so `/fulltext` can fall back to `source=local_cache` before Zotero finishes asynchronous indexing.
- When `GET /v1/items/search` is called with `includeFulltext=true`, the bridge can supplement Zotero results with matches from the local full-text cache.
- Structured JSON error envelope for validation, auth, upstream failures, and conflicts.

## Environment

Copy `.env.example` to `.env` and set:

- `BRIDGE_API_KEY`
- `ZOTERO_LIBRARY_TYPE`
- `ZOTERO_LIBRARY_ID`
- `ZOTERO_API_KEY`

Optional runtime toggles:

- `STARTUP_VALIDATE_ZOTERO_KEY=true`
- `DEFAULT_COLLECTION_KEY=...`
- `DEFAULT_CITATION_STYLE=apa`
- `DEFAULT_CITATION_LOCALE=en-US`
- `ENABLE_LOCAL_FULLTEXT_CACHE=true`
- `LOCAL_FULLTEXT_CACHE_DIR=.cache/fulltext`

## Local run

Install and sync:

```bash
uv python install 3.12
uv sync --python 3.12
```

Start the API:

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## Verification

Run all local checks:

```bash
uv run ruff check app tests
uv run mypy app tests
uv run pytest
```

Run the provided smoke script against a live server:

```bash
export BRIDGE_BASE_URL=https://your-domain.example
export BRIDGE_API_KEY=your_bridge_token
./scripts/smoke_test.sh
```

## Live Zotero testing

Live tests are opt-in. Set real credentials in `.env` and run:

```bash
LIVE_TESTS=1 uv run pytest -m live
```

Recommended live order:

1. `POST /v1/papers/add-by-doi`
2. `GET /v1/items/search`
3. `GET /v1/items/{itemKey}/citation`
4. `POST /v1/items/{itemKey}/notes/upsert-ai-note`
5. `POST /v1/papers/upload-pdf-multipart`
6. `GET /v1/items/{itemKey}/fulltext`

## Notes

- The bridge stays stateless for MVP.
- `localhost:23119` is intentionally not exposed or used.
- The Actions contract remains in `openapi.actions.yaml`; the multipart endpoint is intentionally excluded there.
- `hasFulltext` in search/detail summaries is a PDF capability hint; actual full-text availability is confirmed by `/v1/items/{itemKey}/fulltext`.
- If Zotero Web API has not indexed a newly uploaded PDF yet, `/v1/items/{itemKey}/fulltext` can still succeed from the local cache and report `source=local_cache`.
