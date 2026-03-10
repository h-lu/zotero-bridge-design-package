# Zotero Bridge Design Package

This package specifies a production-ready **REST bridge** between ChatGPT/Codex and a Zotero library.

## What this bridge does

- **Write to Zotero** through the **Zotero Web API v3**
  - create bibliographic items from DOI metadata
  - upload PDF attachments
  - add tags and collections to existing items
  - write AI reading outputs back to Zotero as child notes
  - merge duplicate bibliographic items
- **Read from Zotero** through the **Zotero Web API v3**
  - browse library items with pagination and filters
  - inspect library stats and recent changes
  - search library items
  - run advanced fielded searches across title, creator, abstract, venue, DOI, tags, notes, and full text
  - resolve exact items by DOI/title
  - list collections and tags
  - batch fetch item records
  - find likely duplicate items
  - follow related-item links
  - resolve attachments
  - retrieve chunked full text
  - preview full text across multiple items in one request
  - build review-ready bundles with citations, notes, related items, and full-text previews
  - retrieve formatted citations/bibliographies
- **Discover papers outside Zotero**
  - query OpenAlex for recent or highly cited papers related to a topic
- **Expose a public HTTPS API**
  - ChatGPT uses the API as a **GPT Action**
  - Codex can use the same API directly over HTTP
- **Optionally add a local desktop relay later**
  - for faster access to Zotero Desktop `localhost:23119` when web full-text sync is unavailable

## Why this design

1. **No MCP is required for v1.**
   - ChatGPT Actions are the shortest path for ChatGPT.
   - Codex can call the same REST API directly.
2. **All writes stay server-side.**
   - The Zotero API key never goes to ChatGPT or Codex.
3. **AI reading results go back into Zotero cleanly.**
   - Each AI note is stored as a Zotero **child note** under the parent paper.
4. **The package is optimized for implementation by Codex.**
   - two OpenAPI files
   - endpoint semantics
   - verification plan
   - smoke tests
   - deployment example

## Scope

### MVP (build now)
- `/healthz`
- `GET /v1/library/stats`
- `GET /v1/items`
- `POST /v1/items/batch`
- `GET /v1/items/changes`
- `GET /v1/items/resolve`
- `GET /v1/items/duplicates`
- `POST /v1/items/duplicates/merge`
- `GET /v1/items/search-advanced`
- `POST /v1/items/review-pack`
- `POST /v1/papers/add-by-doi`
- `POST /v1/papers/upload-pdf-action`
- `POST /v1/papers/upload-pdf-multipart`
- `GET /v1/collections`
- `GET /v1/discovery/search`
- `GET /v1/tags`
- `GET /v1/items/search`
- `POST /v1/items/fulltext/batch-preview`
- `GET /v1/items/{itemKey}`
- `GET /v1/items/{itemKey}/related`
- `POST /v1/items/{itemKey}/tags`
- `DELETE /v1/items/{itemKey}/tags/{tag}`
- `POST /v1/items/{itemKey}/collections`
- `GET /v1/items/{itemKey}/fulltext`
- `GET /v1/items/{itemKey}/notes`
- `POST /v1/items/{itemKey}/notes`
- `POST /v1/items/{itemKey}/notes/upsert-ai-note`
- `GET /v1/items/{itemKey}/citation`

### Phase 2 (optional)
- desktop relay against Zotero Desktop local API on `localhost:23119`
- background full-text cache warmup
- remote MCP wrapper for Codex/ChatGPT Apps if you later want one

## Recommended stack

- Python 3.12
- FastAPI
- httpx
- pydantic v2 + pydantic-settings
- uvicorn
- markdown-it-py or mistune for Markdown → HTML conversion
- bleach for HTML sanitization
- pytest + respx for tests
- uv for dependency management

## Main design choices

### 1) Writes always use Zotero Web API v3
This is the canonical write path for:
- creating items
- updating items
- creating child notes
- uploading PDF attachments

### 2) Reads prefer Zotero Web API full text
If `Sync full-text content` is enabled in Zotero Desktop, extracted PDF text is synced and becomes available through the Web API. That keeps v1 simple and avoids exposing a local-only service to the public internet.

### 3) AI outputs are stored as child notes
The bridge writes ChatGPT/Codex outputs back into Zotero as child notes with machine-readable tags:

- `zbridge`
- `zbridge:agent:<agent>`
- `zbridge:type:<noteType>`
- `zbridge:slot:<slot>`

These tags make note upsert deterministic without relying on brittle text search.

### 4) The API is public-HTTPS only
Use your Singapore server as the public bridge endpoint. Put TLS in front with Caddy/Nginx. Standard port 443 is preferred, but the bridge also works behind an HTTPS listener on a non-standard port such as `:8888`.

## File guide

- `README.md` — overview
- `ARCHITECTURE.md` — detailed behavior and data flow
- `OPTIONAL_LOCAL_RELAY.md` — future design for `localhost:23119`
- `openapi.full.yaml` — full bridge contract for implementation and direct HTTP use
- `openapi.actions.yaml` — trimmed GPT Actions contract
- `CODEX_HANDOFF.md` — exact implementation prompt for Codex
- `TEST_PLAN.md` — verification strategy and acceptance criteria
- `.env.example` — required environment variables
- `scripts/smoke_test.sh` — curl-based smoke tests
- `deploy/Caddyfile.example` — minimal HTTPS reverse proxy example

## Suggested implementation order

1. project scaffold + config
2. Zotero client wrapper
3. DOI metadata resolver
4. search + item detail endpoints
5. add-by-doi
6. note upsert
7. PDF upload
8. full-text chunking
9. citation endpoint
10. tests + deployment

## Notes on ChatGPT vs Codex

### ChatGPT
Use `openapi.actions.yaml` in a Custom GPT Action:
- auth type: **API Key**
- auth style: **Bearer**
- include the browse/resolve endpoints so the model can page, filter, and inspect the library without guessing search keywords
- expose the stats/changes/related/fulltext-preview endpoints too if you want the model to maintain a lightweight sync view of the library
- include `search-advanced`, `review-pack`, and `discovery/search` if the model will do literature review or topic scouting

### Codex
For v1, do **not** build MCP first.
Use:
- `curl`
- a tiny Python client
- or a repo-local helper script

Later, if you want, you can wrap the same REST API with a small HTTP MCP server.
