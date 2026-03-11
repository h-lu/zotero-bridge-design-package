# Implementation Status

Version: `2.0.0`

`zotero-bridge is a Zotero I/O and workflow layer, not a PDF reading engine.`

本实现只负责 Zotero 文献存取、附件交付、结构化笔记回写和工作流编排，不负责 PDF 正文读取。

## Implemented Capabilities

- health and upstream-key health reporting
- library listing, resolve, changes, duplicates, merge
- item search and advanced search over metadata plus notes
- DOI ingest
- structured metadata ingest
- discovery-hit ingest
- PDF upload through action URL or multipart
- attachment listing, detail, handoff, proxy download
- citations and bibliographies
- structured AI note round-trip
- local note-aware search index without fulltext

## Removed Capabilities

- local fulltext cache
- bridge-side PDF parsing
- fulltext chunk responses
- fulltext preview batching
- review-pack fulltext preview

## Removed API Surface

- `GET /v1/items/{itemKey}/fulltext`
- `POST /v1/items/fulltext/batch-preview`
- deprecated `review-pack` fulltext preview compatibility flags

## Security Notes

- `fileUrl` fetches default to `https` only
- redirects are revalidated hop by hop
- localhost, loopback, RFC1918, link-local, and other non-global targets are rejected
- downloads are size-limited
- uploads accept PDF only, validated by file signature and MIME handling
- attachment proxy download never exposes Zotero credentials
