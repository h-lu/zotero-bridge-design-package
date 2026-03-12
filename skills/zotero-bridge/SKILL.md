---
name: zotero-bridge
description: Use this skill when working with the Zotero Bridge API to discover papers, import metadata, hand off attachments, read PDFs locally, and write structured notes back to Zotero. This skill is for Codex-style literature workflows on the hblu.top:8888 bridge endpoint.
---

# Zotero Bridge

Use this skill when the task involves Zotero-backed paper workflows through the bridge API.

## Required Environment

- `BRIDGE_BASE_URL`
  Default: `https://hblu.top:8888`
- `ZOTERO_API_KEY`
  Send as `X-Zotero-API-Key: $ZOTERO_API_KEY`

## Core Rules

- The bridge is a Zotero I/O and workflow layer, not a PDF reading engine.
- Do not use bridge-side fulltext endpoints for reading. They have been removed.
- To read a paper, request attachment handoff, download the PDF, then read it locally in Codex.
- Write findings back as structured notes instead of keeping analysis only in local scratch output.

## Default Workflow

1. Search discovery or Zotero.
2. Import the paper if it is not already in the library.
3. List attachments and request handoff for the target PDF.
4. Download the PDF and analyze it locally.
5. Write back `paper.summary`, `paper.methods`, `paper.findings`, `paper.limitations`, `paper.relevance`, or `synthesis.*` notes.
6. Re-query Zotero to confirm the note round-trip.

## High-Value Endpoints

- Discovery and search:
  - `GET /v1/discovery/search`
  - `GET /v1/items/search`
  - `GET /v1/items/search-advanced`
- Ingest:
  - `POST /v1/papers/import-discovery-hit`
  - `POST /v1/papers/import-metadata`
  - `POST /v1/papers/add-by-doi`
- Attachments:
  - `GET /v1/items/{itemKey}/attachments`
  - `POST /v1/attachments/{attachmentKey}/handoff`
  - `GET /v1/attachments/download/{token}`
- Notes:
  - `POST /v1/items/{itemKey}/notes/upsert-ai-note`
  - `GET /v1/notes/{noteKey}`
  - `POST /v1/items/review-pack`

## Recommended Note Types

- `paper.summary`
- `paper.methods`
- `paper.findings`
- `paper.limitations`
- `paper.future_work`
- `paper.relevance`
- `synthesis.theme`
- `synthesis.conflict`
- `synthesis.gap_candidate`
- `workflow.todo`

## References

- For connection details and curl examples, read [`references/setup.md`](/home/ubuntu/zotero-bridge-design-package/skills/zotero-bridge/references/setup.md).
- For the default literature workflow and note-writing pattern, read [`references/workflows.md`](/home/ubuntu/zotero-bridge-design-package/skills/zotero-bridge/references/workflows.md).
