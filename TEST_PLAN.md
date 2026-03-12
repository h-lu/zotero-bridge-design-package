# Test Plan

## Goal

Validate Zotero Bridge 2.0 as a Zotero I/O and workflow layer.

Do not test bridge-side PDF reading. That capability has been removed by design.

## Must Cover

- attachments list/detail/handoff/proxy download
- handoff token invalid / expired behavior
- PDF upload SSRF guardrails
- metadata import create / exists / update
- discovery-hit import and weak dedupe
- structured AI note round-trip
- note-aware search using structured payload text
- removed fulltext routes stay unroutable
- review-pack rejects removed fulltext compatibility flags

## Security Cases

- reject `http://` file URLs by default
- reject `localhost`
- reject RFC1918/private IP literals
- reject unsafe redirect targets
- reject oversized remote files
- reject non-PDF remote content

## Quality Gate

```bash
uv sync
pytest
ruff check .
mypy app tests
```
