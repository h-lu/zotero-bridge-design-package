# Architecture

## Product Boundary

`zotero-bridge is a Zotero I/O and workflow layer, not a PDF reading engine.`

本项目是 Zotero 的存取与工作流桥接层，不是 PDF 阅读引擎。

The bridge owns:

- Zotero metadata read/write
- item ingest and dedupe
- attachment upload and secure delivery
- citations
- structured AI note persistence
- discovery-to-library workflows
- duplicate detection and merge

The bridge does not own:

- PDF text extraction
- OCR
- fulltext chunking
- preview generation
- paper understanding or literature synthesis

## Main Components

- [`app/main.py`](/home/ubuntu/zotero-bridge-design-package/app/main.py): FastAPI app wiring and lifespan
- [`app/services/bridge_service.py`](/home/ubuntu/zotero-bridge-design-package/app/services/bridge_service.py): façade across library, ingest, notes, and discovery flows
- [`app/services/attachment_service.py`](/home/ubuntu/zotero-bridge-design-package/app/services/attachment_service.py): attachment metadata, handoff tokens, proxy download
- [`app/services/remote_fetch_guard.py`](/home/ubuntu/zotero-bridge-design-package/app/services/remote_fetch_guard.py): guarded remote PDF fetch for `fileUrl`
- [`app/services/note_renderer.py`](/home/ubuntu/zotero-bridge-design-package/app/services/note_renderer.py): human-readable + machine-readable structured note encoding
- [`app/services/zotero_client.py`](/home/ubuntu/zotero-bridge-design-package/app/services/zotero_client.py): Zotero Web API transport
- [`app/services/local_search_index.py`](/home/ubuntu/zotero-bridge-design-package/app/services/local_search_index.py): metadata + notes search cache, no fulltext

## Attachment Delivery Flow

1. Agent lists or resolves an item.
2. Agent calls `/v1/items/{itemKey}/attachments` or `/v1/attachments/{attachmentKey}`.
3. Agent requests `/v1/attachments/{attachmentKey}/handoff`.
4. Bridge issues a short-lived tokenized download URL.
5. Tokenized download proxies the Zotero attachment file and returns safe headers.

## Structured Note Flow

1. Agent calls `/v1/items/{itemKey}/notes/upsert-ai-note`.
2. Bridge renders human-readable HTML for Zotero.
3. Bridge embeds machine-readable structured data in the note body.
4. Bridge reads the note back by parsing the embedded block.
5. Local search index flattens note text plus structured payload text for note-aware search.

## Review Pack

`review-pack` is now a research workspace packet:

- item metadata
- attachments
- citations
- notes
- related items

It no longer returns article body preview.
