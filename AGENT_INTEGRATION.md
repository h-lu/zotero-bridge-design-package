# Agent Integration

Use these artifacts when connecting ChatGPT, Codex CLI, or another agent to the bridge.

## Endpoint

- base URL: `https://hblu.top:8888`
- live docs: `https://hblu.top:8888/docs`
- live OpenAPI JSON: `https://hblu.top:8888/openapi.json`

## Auth

All routes require:

```text
Authorization: Bearer <BRIDGE_API_KEY>
```

The bridge never exposes the underlying Zotero API key. Agents authenticate only with the bridge bearer token.

## OpenAPI Files

- [`openapi.actions.yaml`](/home/ubuntu/zotero-bridge-design-package/openapi.actions.yaml)
  Recommended for ChatGPT Actions and other agent tool integrations.
- [`openapi.full.yaml`](/home/ubuntu/zotero-bridge-design-package/openapi.full.yaml)
  Full contract for development and debugging.

## Recommended Agent Workflow

1. Search or discover papers.
   - `GET /v1/discovery/search`
   - `GET /v1/items/search`
   - `GET /v1/items/search-advanced`
2. Import papers into Zotero.
   - `POST /v1/papers/import-discovery-hit`
   - `POST /v1/papers/import-metadata`
   - `POST /v1/papers/add-by-doi`
3. Deliver attachments to the agent.
   - `GET /v1/items/{itemKey}/attachments`
   - `POST /v1/attachments/{attachmentKey}/handoff`
   - `GET /v1/attachments/download/{token}`
4. Let the agent read and analyze the PDF locally.
   - Do not call bridge-side fulltext endpoints for reading. They no longer exist.
5. Write results back as structured notes.
   - `POST /v1/items/{itemKey}/notes/upsert-ai-note`
   - `GET /v1/notes/{noteKey}`
6. Pull a workspace packet for downstream synthesis.
   - `POST /v1/items/review-pack`

## Important Constraints

- The bridge is a Zotero I/O and workflow layer, not a PDF reading engine.
- `GET /v1/items/{itemKey}/fulltext` and `POST /v1/items/fulltext/batch-preview` have been removed.
- `review-pack` no longer returns `fulltextPreview`.
- For PDF analysis, use attachment handoff and read the file in ChatGPT/Codex CLI.

## ChatGPT Attachment Flow

For ChatGPT-style usage, the intended sequence is:

1. call `POST /v1/attachments/{attachmentKey}/handoff`
2. read `downloadUrl` from the JSON response
3. call `GET /v1/attachments/download/{token}`

`download/{token}` is tokenized and does not require the bridge bearer token. The token itself is the capability.

## Minimal Curl Examples

List items:

```bash
curl -H "Authorization: Bearer $BRIDGE_API_KEY" \
  "$BRIDGE_BASE_URL/v1/items?limit=5"
```

Discover papers:

```bash
curl -H "Authorization: Bearer $BRIDGE_API_KEY" \
  --get "$BRIDGE_BASE_URL/v1/discovery/search" \
  --data-urlencode "q=software engineering agents" \
  --data-urlencode "limit=5"
```

Import a discovery hit:

```bash
curl -H "Authorization: Bearer $BRIDGE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"title":"SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering","publicationYear":2024,"authors":[{"name":"John Yang"}]}' \
  "$BRIDGE_BASE_URL/v1/papers/import-discovery-hit"
```

Write a structured note:

```bash
curl -H "Authorization: Bearer $BRIDGE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"agent":"codex-cli","noteType":"paper.summary","slot":"default","bodyMarkdown":"Short summary","schemaVersion":"1.0","payload":{"summary":"Short summary"}}' \
  "$BRIDGE_BASE_URL/v1/items/ITEMKEY/notes/upsert-ai-note"
```

## Codex Skill

The repo-local skill lives at [`skills/zotero-bridge/SKILL.md`](/home/ubuntu/zotero-bridge-design-package/skills/zotero-bridge/SKILL.md). It is intended for Codex-style workflows and should be used together with the OpenAPI contract, not instead of it.
