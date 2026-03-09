# Test Plan and Acceptance Criteria

## 1. Test layers

### A. Unit tests
Mock Zotero upstream and DOI metadata resolver.

Coverage:
- DOI normalization
- DOI duplicate detection
- CSL JSON → Zotero item mapping
- attachment selection
- full-text chunking
- AI note identity tag generation
- Markdown → sanitized HTML rendering
- error mapping

### B. Contract tests
Validate that the FastAPI app matches:
- `openapi.full.yaml`
- `openapi.actions.yaml`

### C. Integration tests (mocked upstream)
Use `respx` to mock:
- Zotero item create
- Zotero item search
- Zotero file upload authorization
- upload register
- fulltext read
- citation read

### D. Live integration tests (your real credentials)
Run only when:
- `LIVE_TESTS=1`
- real Zotero API key provided
- real library id provided

## 2. Acceptance criteria by endpoint

## 2.1 /healthz
- returns `200`
- returns `ok=true`
- reports config presence
- does not leak secrets

## 2.2 add-by-doi
- first call with a new DOI returns `status=created`
- second call with same DOI returns `status=existing`
- tags are persisted
- collection membership is persisted if requested

## 2.3 upload-pdf-action
- rejects zero files
- rejects more than one file in MVP
- uploads a PDF and returns attachment metadata
- associates attachment with parent item when `itemKey` is provided
- returns `400` when no parent resolution path is available

## 2.4 upload-pdf-multipart
- accepts a local PDF
- computes MD5 and file size
- successfully completes authorize → upload → register flow

## 2.5 search
- respects `limit`
- when `includeFulltext=true`, bridge uses the upstream full-text search mode
- response is compact and excludes note bodies

## 2.6 item detail
- returns parent item
- returns attachment summaries
- returns AI note summaries
- does not return raw full-text payloads

## 2.7 fulltext
- returns chunk 0 with `nextCursor`
- returns subsequent chunk with stable cursor semantics
- returns `done=true` on final chunk
- returns `404` with `FULLTEXT_NOT_AVAILABLE` when no full text exists

## 2.8 upsert-ai-note
- first call creates a child note
- second call with same identity updates same note key
- `replace` overwrites
- `append` adds content to same note
- machine tags are always present

## 2.9 citation
- returns `citationHtml`
- returns `bibliographyHtml`
- respects `style` and `locale`

## 3. Suggested live test sequence

1. `POST /v1/papers/add-by-doi`
2. `GET /v1/items/search`
3. `GET /v1/items/{itemKey}/citation`
4. `POST /v1/items/{itemKey}/notes/upsert-ai-note`
5. `POST /v1/papers/upload-pdf-multipart`
6. wait for Zotero full-text indexing/sync if needed
7. `GET /v1/items/{itemKey}/fulltext`

## 4. Expected eventual-consistency caveat

Full text may not be immediately available right after PDF upload if extracted text has not yet been indexed and synced by Zotero Desktop. Integration tests for `/fulltext` should therefore support polling or a delayed verification step.

## 5. Exit criteria

The bridge is ready for your server when all are true:

- all unit tests pass
- contract tests pass
- mocked integration tests pass
- live smoke test passes for:
  - add-by-doi
  - citation
  - upsert-ai-note
  - multipart PDF upload
- ChatGPT Action imports `openapi.actions.yaml` successfully
- ChatGPT can:
  - add a DOI item
  - search the library
  - write an AI note back to Zotero
