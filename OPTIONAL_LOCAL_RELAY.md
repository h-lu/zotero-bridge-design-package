# Optional Local Relay Design (Phase 2)

This module is **not required** for MVP.

## Why add a local relay later?

The Zotero Web API can return full text only when extracted text is present in the synced library. In practice, that is enough for many workflows if Zotero Desktop has **Sync full-text content** enabled.

A local relay becomes useful when you want:

- access to very recent local PDFs before sync catches up
- faster access to local-only content
- a bridge that can still read while the public library copy is stale

## Constraints

- Zotero Desktop local API is on `localhost:23119`
- it is intended for local access on the same machine
- do **not** expose `localhost:23119` directly to the public internet

## Recommended phase 2 shape

```text
Zotero Desktop ─── localhost:23119 ───▶ desktop-relay-agent
                                              │
                                              │ HTTPS + relay token
                                              ▼
                                        zotero-bridge internal endpoints
```

## Relay responsibilities

The relay agent runs on the same computer as Zotero Desktop and can:

- query local items
- query local child attachments
- fetch local full text
- push selected full-text payloads to the server cache

## Suggested server-side internal endpoints

These are **internal only** and should not be part of the public Actions API.

- `POST /internal/relay/fulltext`
- `POST /internal/relay/items/search`
- `POST /internal/relay/ping`

## Suggested public behavior after relay exists

For `GET /v1/items/{itemKey}/fulltext`:

1. if `preferSource=cache`, use cached local relay data first
2. if cache miss, use Zotero Web API
3. if Web API misses and relay is marked online, optionally queue a refresh hint

## Minimal relay contract

### POST /internal/relay/fulltext
Body:
```json
{
  "itemKey": "ABCD1234",
  "attachmentKey": "EFGH5678",
  "source": "local_zotero_api",
  "content": "....",
  "indexedPages": 12,
  "totalPages": 12,
  "collectedAt": "2026-03-09T12:34:56Z"
}
```

## Relay auth

Use a separate relay token:
- `RELAY_SHARED_TOKEN`

The relay must never receive the public bridge bearer token used by ChatGPT/Codex.

## Why this is phase 2 only

Adding a relay increases:
- operational complexity
- machine-to-machine trust
- cache invalidation complexity

MVP should first prove that:
- DOI add works
- PDF upload works
- note upsert works
- full text is usable from Zotero Web API in your real workflow
