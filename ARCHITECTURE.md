# Architecture and Behavioral Spec

## 1. System architecture

```text
ChatGPT (GPT Action) ───────┐
                            │ HTTPS + Bearer auth
Codex / scripts / curl ─────┼──────────────▶ zotero-bridge
                            │
                            │                       ┌──────────────────────────┐
                            └──────────────────────▶│ Zotero Web API v3       │
                                                    │ https://api.zotero.org  │
                                                    └──────────────────────────┘
                                                              ▲
                                                              │ sync
                                                              │
                                                    ┌──────────────────────────┐
                                                    │ Zotero Desktop           │
                                                    │ + sync full-text content │
                                                    └──────────────────────────┘
```

Optional phase 2:

```text
Zotero Desktop local API (localhost:23119)
        ▲
        │ local-only
        │
desktop relay agent ─────────▶ zotero-bridge internal cache
```

## 2. Trust boundaries

### Public side
- ChatGPT and Codex know only the **bridge URL** and the **bridge bearer token**.
- They never see the Zotero API key.

### Server side
- The bridge stores:
  - `ZOTERO_API_KEY`
  - `ZOTERO_LIBRARY_TYPE`
  - `ZOTERO_LIBRARY_ID`
- The bridge is the only component allowed to call Zotero write endpoints.

## 3. Data model

## 3.1 Bridge item model

The bridge returns a normalized item shape:

- `itemKey`
- `itemType`
- `title`
- `year`
- `DOI`
- `creators`
- `tags`
- `collectionKeys`
- `attachments[]`
- `aiNotes[]`

## 3.2 Attachment model

Each attachment summary includes:

- `attachmentKey`
- `contentType`
- `filename`
- `linkMode`
- `title`
- `md5`
- `mtime`
- `hasFulltext`
- `isPdf`

## 3.3 AI note identity

AI notes are uniquely identified by:

- `parent itemKey`
- `agent`
- `noteType`
- `slot`

That means:
- ChatGPT summary and Codex summary can coexist
- replacing a note is deterministic
- appending is deterministic

Tag convention:

- `zbridge`
- `zbridge:agent:chatgpt`
- `zbridge:type:summary`
- `zbridge:slot:default`

You may extend this with user tags like:
- `ai-summary`
- `paper-review`
- `to-read`

## 4. Endpoint behaviors

## 4.1 GET /healthz

Purpose:
- liveness
- readiness basics

Behavior:
- returns app version
- returns whether required config exists
- optionally performs lightweight key validation on startup and caches the result

## 4.2 POST /v1/papers/add-by-doi

Purpose:
- create a bibliographic item from DOI metadata

Algorithm:
1. Normalize DOI:
   - trim
   - remove leading `https://doi.org/`
   - lowercase for duplicate checking
2. Check duplicates:
   - quick search Zotero using DOI text
   - inspect returned items and compare exact DOI values
3. Resolve DOI metadata:
   - first use DOI content negotiation to fetch CSL JSON
   - fallback to Crossref REST only if needed
4. Map metadata to Zotero item JSON
5. Create item in Zotero:
   - use item template
   - send `Zotero-Write-Token`
6. Return:
   - `created` if new
   - `existing` if duplicate found

Minimum mapped fields:
- `itemType`
- `title`
- `creators`
- `DOI`
- `url`
- `date`
- `publicationTitle`
- `volume`
- `issue`
- `pages`
- `abstractNote` if available
- `tags`
- `collections`

Mapping rule:
- if DOI metadata does not map perfectly, prefer a valid minimal Zotero item over a lossy or invalid object.

## 4.3 POST /v1/papers/upload-pdf-action

Purpose:
- upload a PDF from ChatGPT Actions or a remote URL

Accepted sources:
- `openaiFileIdRefs[0].download_link`
- `fileUrl`

Parent resolution:
1. if `itemKey` is provided:
   - attach to that existing item
2. else if `doi` is provided:
   - call internal add-by-doi flow or find existing DOI item
3. else if `createTopLevelAttachmentIfNeeded=true`:
   - create a top-level attachment
4. else:
   - return `400`

Upload steps:
1. download the PDF to a temp file
2. compute `md5`, `filesize`, `mtime`
3. create attachment item:
   - `itemType=attachment`
   - `linkMode=imported_file`
   - `parentItem=<itemKey>` if attaching to a parent
   - `contentType=application/pdf`
4. request upload authorization
5. upload file to returned URL
6. register upload
7. return attachment metadata

Important choice:
- in MVP, only the first file in `openaiFileIdRefs` is processed
- if more than one file is provided, return `400`

## 4.4 POST /v1/papers/upload-pdf-multipart

Purpose:
- upload a local PDF from curl, Codex, or a local script

Same logic as `upload-pdf-action`, except the source file comes from multipart form-data.

Required:
- `file`
- one of:
  - `itemKey`
  - `doi`
  - `createTopLevelAttachmentIfNeeded=true`

## 4.5 GET /v1/items/search

Purpose:
- search the library for papers

Inputs:
- `q`
- `limit`
- `includeFulltext`
- `includeNotes`
- `includeAttachments`

Behavior:
- bridge issues a Zotero item search with `q`
- use `qmode=everything` when `includeFulltext=true`
- default result set should emphasize parent bibliographic items
- deduplicate by parent item if child items appear in upstream results
- default response should be compact to stay well below Action payload limits

Default filters:
- do not return note bodies
- do not return attachment full text
- include only attachment summaries

## 4.6 GET /v1/items/{itemKey}

Purpose:
- retrieve a normalized parent item plus attachment and AI note summaries

Behavior:
- fetch item
- fetch child items
- split children into:
  - attachments
  - notes
- identify bridge-managed AI notes by tags
- do not return full note bodies by default

## 4.7 GET /v1/items/{itemKey}/fulltext

Purpose:
- return chunked full text for the selected or resolved PDF attachment

Inputs:
- `attachmentKey` optional
- `cursor` optional, default `0`
- `maxChars` optional, default `8000`, max `12000`
- `preferSource` optional:
  - `web`
  - `cache`
  - `auto`

Attachment selection:
1. if `attachmentKey` is supplied, use it
2. else list child attachments and choose:
   - first PDF attachment if there is only one
   - otherwise choose the newest PDF and include all candidate keys in the response

Text source order:
- MVP: Zotero Web API full text
- Phase 2: cached local relay full text when `preferSource=cache|auto`

Chunking rules:
- normalize line endings
- preserve paragraph boundaries where practical
- avoid splitting in the middle of a word if possible
- return:
  - `content`
  - `cursor`
  - `nextCursor`
  - `done`
  - `attachmentKey`
  - `source`
  - `indexedPages`
  - `totalPages`

If full text is unavailable:
- return `404` with code `FULLTEXT_NOT_AVAILABLE`

## 4.8 POST /v1/items/{itemKey}/notes/upsert-ai-note

Purpose:
- write ChatGPT/Codex reading outputs back into Zotero as child notes

Inputs:
- `agent` (required)
- `noteType` (required)
- `slot` (default `default`)
- `mode` (`replace` or `append`)
- `title`
- `bodyMarkdown`
- `tags[]`
- `model`
- `sourceAttachmentKey`
- `sourceCursorStart`
- `sourceCursorEnd`
- `requestId`

Behavior:
1. fetch child notes
2. find note with identity tags:
   - `zbridge`
   - `zbridge:agent:<agent>`
   - `zbridge:type:<noteType>`
   - `zbridge:slot:<slot>`
3. convert `bodyMarkdown` to sanitized HTML
4. render note body:
   - heading from `title`
   - body from converted HTML
   - provenance footer with timestamp and model if provided
5. if target note exists:
   - `replace`: overwrite the note body
   - `append`: append a section divider and new content
6. if target note does not exist:
   - create a new child note under the parent item
7. return `created` or `updated`

Why Markdown input:
- ChatGPT and Codex naturally write Markdown
- the bridge handles HTML conversion centrally

## 4.9 GET /v1/items/{itemKey}/citation

Purpose:
- retrieve formatted citation and bibliography preview for an item

Inputs:
- `style` default `apa`
- `locale` default `en-US`
- `linkwrap` default `false`

Behavior:
- call the Zotero item endpoint with `include=bib,citation`
- return:
  - `citationHtml`
  - `bibliographyHtml`
  - `style`
  - `locale`

## 5. Zotero client behavior

## 5.1 Required request headers
All Zotero requests should send:
- `Zotero-API-Version: 3`
- `Authorization: Bearer <ZOTERO_API_KEY>`

## 5.2 Create requests
For unversioned create requests, the bridge should send a `Zotero-Write-Token` derived from:
- `requestId`, if provided
- otherwise a freshly generated 32-char random token

## 5.3 Update requests
For existing notes/items:
- fetch the latest version first
- use `PATCH` with `If-Unmodified-Since-Version`
- on `412`, refetch once and retry once
- if still conflicting, return `409`

## 5.4 Backoff and retry
The bridge should:
- honor `Backoff`
- honor `Retry-After`
- retry transient 429/503/5xx with capped exponential backoff
- never retry a non-idempotent create without a stable write token

## 6. Security model

## 6.1 Bridge auth
The bridge public API uses bearer auth:
- ChatGPT Action: API Key auth with **Bearer**
- Codex: `Authorization: Bearer <BRIDGE_API_KEY>`

## 6.2 Secret handling
Never expose:
- Zotero API key
- Zotero library id details beyond what is necessary
- internal upstream errors with secrets

## 6.3 Rate limiting
Recommended:
- per-token rate limit at the bridge
- tighter limits on upload and note-write endpoints
- request body size limit (e.g. 15 MB for uploads through the bridge)

## 7. Observability

Each request log should include:
- request id
- route
- bridge auth principal or token hash
- Zotero upstream status
- latency
- retry count

Metrics:
- request count by endpoint
- upload success/failure
- add-by-doi created/existing ratio
- note upsert created/updated ratio
- fulltext hit/miss
- Zotero 429/503 rates

## 8. Non-goals for MVP

- OCR
- PDF parsing on the bridge itself
- automatic DOI extraction from arbitrary PDFs
- vector database / semantic search
- MCP server
- user-level multi-tenant OAuth to Zotero

## 9. Future-compatible extension points

- local desktop relay
- semantic full-text index
- optional item creation from generic CSL JSON
- optional MCP wrapper
