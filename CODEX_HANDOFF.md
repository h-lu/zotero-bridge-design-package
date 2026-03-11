# Codex Handoff

## Core Rule

`zotero-bridge is a Zotero I/O and workflow layer, not a PDF reading engine.`

本项目是 Zotero 的存取与工作流桥接层，不是 PDF 阅读引擎。

Do not reintroduce bridge-side PDF parsing, OCR, fulltext chunking, or preview generation.

## Required Focus Areas

- keep Zotero metadata reads/writes stable
- keep DOI ingest and upload flows stable
- preserve duplicate detection/merge, collections, tags, related items, citations
- treat attachments as handoff artifacts for the agent to read directly
- treat structured AI notes as first-class data

## API Expectations

- supported:
  - `/v1/items/{itemKey}/attachments`
  - `/v1/attachments/{attachmentKey}`
  - `/v1/attachments/{attachmentKey}/handoff`
  - `/v1/attachments/download/{token}`
  - `/v1/papers/add-by-doi`
  - `/v1/papers/import-metadata`
  - `/v1/papers/import-discovery-hit`
  - `/v1/papers/upload-pdf-action`
  - `/v1/papers/upload-pdf-multipart`
  - structured note read/write routes

## Structured Notes

When writing AI notes:

- keep Zotero note content human-readable
- embed machine-readable structured data in the note body
- support `schemaVersion`, `payload`, and `provenance`
- support round-trip parsing without any external database

## Upload Security

`fileUrl` uploads must enforce:

- `https` by default
- DNS/IP SSRF protection
- redirect revalidation
- size limits
- PDF-only acceptance

## Testing Bar

Before handoff, verify:

```bash
uv sync
pytest
ruff check .
mypy app tests
```
