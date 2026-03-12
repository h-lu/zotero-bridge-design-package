# Zotero Bridge 2.0

`zotero-bridge is a Zotero I/O and workflow layer, not a PDF reading engine.`

本项目是 Zotero 的存取与工作流桥接层，不是 PDF 阅读引擎。

## What It Does

- Read and search Zotero items, collections, tags, notes, citations, related items, and duplicates.
- Ingest papers by DOI, structured metadata, or discovery hits.
- Upload and manage PDF attachments.
- Hand off attachments to ChatGPT/Codex through short-lived proxy-download URLs.
- Write, update, read, and delete structured AI notes stored as Zotero child notes.

## What It No Longer Does

- No bridge-side PDF parsing.
- No OCR.
- No fulltext chunking.
- No fulltext preview.
- No bridge-side paper understanding or synthesis.

## Breaking Changes in 2.0.0

- `GET /v1/items/{itemKey}/fulltext` has been removed.
- `POST /v1/items/fulltext/batch-preview` has been removed.
- `review-pack` no longer returns `fulltextPreview`.
- advanced search no longer exposes `fulltext` as a searchable field.
- bridge-side `pypdf` and local fulltext cache have been removed.

## New/Upgraded APIs

- `GET /v1/items/{itemKey}/attachments`
- `GET /v1/attachments/{attachmentKey}`
- `POST /v1/attachments/{attachmentKey}/handoff`
- `GET /v1/attachments/download/{token}`
- `POST /v1/papers/import-metadata`
- `POST /v1/papers/import-discovery-hit`
- structured AI note fields: `schemaVersion`, `payload`, `provenance`

## Attachment Handoff

The bridge does not read PDFs for the model. It securely delivers attachments so the agent can read them directly.

- handoff tokens are short-lived
- proxy download hides Zotero credentials
- `fileUrl` uploads are guarded against SSRF, unsafe redirects, oversized files, and non-PDF payloads

## Simple Multi-User Mode

All normal API calls now authenticate directly with the caller's Zotero key:

- `X-Zotero-API-Key: <caller_zotero_key>`

The bridge resolves that key to the caller's personal Zotero user library and executes the request against that library. This is the simplest multi-user mode.

Constraints:

- requests without `X-Zotero-API-Key` are rejected
- it targets the personal user library behind the supplied Zotero key
- the per-request mode disables the local search index to avoid mixing data across libraries
- attachment handoff tokens remain scoped to the originating Zotero key and library

## Structured Notes

Structured AI notes are stored in Zotero child notes as:

- human-readable HTML content for Zotero users
- a machine-readable embedded block for round-trip parsing

Recommended canonical note types:

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

## Contracts

- live docs: `https://hblu.top:8888/docs`
- live OpenAPI JSON: `https://hblu.top:8888/openapi.json`
- full contract: [`openapi.full.yaml`](/home/ubuntu/zotero-bridge-design-package/openapi.full.yaml)
- GPT Actions subset: [`openapi.actions.yaml`](/home/ubuntu/zotero-bridge-design-package/openapi.actions.yaml)
- agent integration guide: [`AGENT_INTEGRATION.md`](/home/ubuntu/zotero-bridge-design-package/AGENT_INTEGRATION.md)
- repo-local Codex skill: [`skills/zotero-bridge/SKILL.md`](/home/ubuntu/zotero-bridge-design-package/skills/zotero-bridge/SKILL.md)

## Verification

The repo is expected to pass:

```bash
uv sync
pytest
ruff check .
mypy app tests
```
