# Workflows

## Search -> Import -> Read -> Write Back

1. Discover candidates.

```bash
curl -H "Authorization: Bearer $BRIDGE_API_KEY" \
  --get "$BRIDGE_BASE_URL/v1/discovery/search" \
  --data-urlencode "q=software engineering agents" \
  --data-urlencode "limit=5"
```

2. Import the selected record.

```bash
curl -H "Authorization: Bearer $BRIDGE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"title":"...","publicationYear":2025,"authors":[{"name":"..."}]}' \
  "$BRIDGE_BASE_URL/v1/papers/import-discovery-hit"
```

3. Ask the bridge for attachment delivery.

```bash
curl -H "Authorization: Bearer $BRIDGE_API_KEY" \
  "$BRIDGE_BASE_URL/v1/items/ITEMKEY/attachments"
```

```bash
curl -H "Authorization: Bearer $BRIDGE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"mode":"proxy_download","expiresInSeconds":900}' \
  "$BRIDGE_BASE_URL/v1/attachments/ATTACHMENTKEY/handoff"
```

The response contains a tokenized `downloadUrl`. That download URL can be fetched directly and does not need the bridge bearer token.

4. Download the PDF and read it locally.

- The bridge only delivers the file.
- Codex does the reading, extraction, comparison, and synthesis.

5. Write structured notes back.

```bash
curl -H "Authorization: Bearer $BRIDGE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"agent":"codex-cli","noteType":"paper.summary","slot":"default","bodyMarkdown":"...","schemaVersion":"1.0","payload":{"summary":"..."}}' \
  "$BRIDGE_BASE_URL/v1/items/ITEMKEY/notes/upsert-ai-note"
```

6. Confirm round-trip.

```bash
curl -H "Authorization: Bearer $BRIDGE_API_KEY" \
  "$BRIDGE_BASE_URL/v1/notes/NOTEKEY"
```

## Review Workflow

When the task is a literature review:

1. Search discovery for candidates.
2. Import the papers with consistent tags.
3. Read attachments locally.
4. Write per-paper notes:
   - `paper.summary`
   - `paper.methods`
   - `paper.findings`
   - `paper.limitations`
   - `paper.relevance`
5. Write cross-paper notes:
   - `synthesis.theme`
   - `synthesis.conflict`
   - `synthesis.gap_candidate`
   - `workflow.todo`
6. Pull `POST /v1/items/review-pack` for a compact bundle of metadata, citations, attachments, and notes.
