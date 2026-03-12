# Zotero Bridge MCP Design

Version: `draft-v1`

## Goal

This MCP layer turns Zotero Bridge into a research workstation for:

- storing and retrieving papers in Zotero
- storing model outputs for each paper back into Zotero
- supporting literature review writing
- supporting exploration of new research questions
- searching both the local Zotero library and OpenAlex

The MCP layer is not a raw OpenAPI mirror. It is a task-oriented adapter over the existing bridge HTTP API.

## Product Boundary

Keep the existing repository boundary unchanged:

- Zotero Bridge owns paper metadata I/O, ingest, attachment handoff, secure download, and structured note persistence
- Codex or other agents own local PDF reading, analysis, synthesis, and reasoning
- the bridge is not a PDF reading engine
- removed fulltext APIs stay removed

## Why MCP

The current repository already has:

- a stable HTTP API
- OpenAPI contracts
- agent integration docs
- a Codex skill

The main remaining problem is startup friction in Codex CLI:

- too many endpoint choices
- too much repeated guessing about parameters
- repeated reconstruction of the same workflow
- tool calls that are too low-level for the actual task

The MCP layer fixes this by exposing a very small number of high-semantic tools.

## Design Principles

1. Zotero is the single source of truth for papers and notes.
2. OpenAlex is a discovery source, not the storage backend.
3. All model outputs that matter should be written back to Zotero structured notes.
4. Notes must remain searchable from the library side.
5. Default workflows should require as few tool calls as possible.
6. MCP should hide low-level HTTP details such as handoff token creation.
7. Tool outputs should be compact and normalized to reduce context waste.

## Runtime Shape

```text
Codex CLI
  -> local MCP server
    -> Zotero Bridge HTTP API
      -> Zotero Web API
```

## Authentication and Configuration

The MCP server reads configuration only from environment variables:

- `BRIDGE_BASE_URL`
- `ZOTERO_API_KEY`

Tool inputs must not accept:

- `X-Zotero-API-Key`
- `base_url`

That keeps secrets out of model-visible tool arguments.

## Scope of v1

Expose `7` default workflow tools and `2` maintenance tools:

1. `zotero_find_papers`
2. `zotero_ingest_papers`
3. `zotero_build_workspace`
4. `zotero_prepare_pdf`
5. `zotero_attach_pdf`
6. `zotero_record_paper_analysis`
7. `zotero_read_records`
8. `zotero_delete_records`
9. `zotero_patch_item_metadata`

The default workflow set is the smallest set that still covers:

- paper discovery
- paper ingest
- paper retrieval
- local PDF reading preparation
- per-paper model output persistence
- cross-paper synthesis persistence and retrieval

The maintenance tools are intentionally separate in spirit:

- they are part of the MCP surface because a real paper warehouse needs cleanup and correction
- they remain constrained so the MCP layer does not become a raw CRUD mirror

## Tool Catalog

### 1. `zotero_find_papers`

Purpose:

- unified search over the local Zotero library and OpenAlex
- support multiple search styles without making the model choose raw endpoints

Primary user intents:

- find papers already in my library
- discover new papers from OpenAlex
- search by keyword, field, DOI, tag, collection, or recency
- search previous model outputs written into notes

Input:

- `scope`: `library | openalex | both`
- `search_mode`: `keyword | fielded | doi | recent | tag | collection`
- `query`
- `title`
- `author`
- `abstract`
- `venue`
- `doi`
- `tag`
- `collection_key`
- `year_from`
- `year_to`
- `oa_only`
- `exclude_existing`
- `sort`
- `limit`

Behavior:

- `library` mode maps to existing library search/list endpoints
- `openalex` mode maps to existing discovery search
- `both` runs both sides and returns a normalized combined result
- library search must support note-aware search through the `note` field, so prior model outputs become searchable assets

OpenAlex note:

- this design assumes the current discovery backend is OpenAlex
- if the backend later changes, the MCP tool name can stay stable while returning a `provider` field in results

Output:

- `results[]`
- each result is normalized as:
  - `source`: `library | openalex`
  - `provider`
  - `itemKey`
  - `title`
  - `authors[]`
  - `year`
  - `doi`
  - `venue`
  - `itemType`
  - `hasDownloadablePdf`
  - `noteTypes[]`
  - `existingItemKey`
  - `importHandle`

Back-end mapping:

- `GET /v1/items`
- `GET /v1/items/search`
- `GET /v1/items/search-advanced`
- `GET /v1/discovery/search`

Why it exists:

- this tool is the entry point for discovery, re-finding stored work, and re-finding past model outputs

### 2. `zotero_ingest_papers`

Purpose:

- add papers into Zotero, regardless of the original source

Primary user intents:

- import by DOI
- import a discovery result
- import structured metadata

Input:

- `papers[]`
- each paper entry supports:
  - `source`: `doi | metadata | openalex_hit`
  - `doi`
  - `metadata`
  - `openalex_hit`
  - `collection_key`
  - `tags[]`

Behavior:

- batch import is required
- the tool accepts multiple papers in one call
- each item returns whether it was newly created or already existed

Output:

- `results[]`
- per result:
  - `status`: `created | existing | failed`
  - `itemKey`
  - `title`
  - `doi`
  - `message`

Back-end mapping:

- `POST /v1/papers/add-by-doi`
- `POST /v1/papers/import-metadata`
- `POST /v1/papers/import-discovery-hit`

Why it exists:

- the model should think in terms of "ingest this paper", not "choose among three import endpoints"

### 3. `zotero_build_workspace`

Purpose:

- prepare structured working context for reading, review writing, and research-question exploration

Primary user intents:

- show me what I already know about these papers
- gather notes, citations, attachments, and relationships before synthesis
- compare multiple papers in one working set

Input:

- `item_keys[]`
- `mode`: `reading | review | gap_scan`
- `include_notes`
- `include_attachments`
- `include_related`
- `include_citation`

Behavior:

- `reading` emphasizes a single paper or a small set for close reading
- `review` emphasizes normalized comparison fields across multiple papers
- `gap_scan` emphasizes limitations, conflicts, and existing gap candidates
- the tool should return a compact, synthesis-friendly representation instead of raw review-pack payloads

Output:

- `workspaceMode`
- `items[]`
- per item:
  - `itemKey`
  - `title`
  - `authors[]`
  - `year`
  - `doi`
  - `abstract`
  - `attachments[]`
  - `notes[]`
  - `relatedItems[]`
  - `citation`
  - `analysisSummary`

Recommended normalized note extraction:

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

Back-end mapping:

- `POST /v1/items/review-pack`
- optional enrichment from existing item detail endpoints if needed inside the adapter

Why it exists:

- literature review and gap exploration need a working set, not a pile of unrelated raw API responses

### 4. `zotero_prepare_pdf`

Purpose:

- turn a Zotero paper or attachment into a local PDF path ready for agent-side reading

Primary user intents:

- download the PDF for this paper so I can read it locally
- resolve the best attachment automatically

Input:

- `targets[]`
- each target supports:
  - `item_key`
  - `attachment_key`
  - `selection`: `auto | exact_attachment`
- `download_dir`

Behavior:

- if `item_key` is given, the adapter lists attachments and picks the best PDF in `auto` mode
- if multiple candidates are ambiguous, return a normalized ambiguity error with candidates
- the adapter performs handoff creation and tokenized download internally
- the model only sees the final local file path

Output:

- `results[]`
- per result:
  - `itemKey`
  - `attachmentKey`
  - `filename`
  - `contentType`
  - `localPath`
  - `expiresAt`

Back-end mapping:

- `GET /v1/items/{itemKey}/attachments`
- `GET /v1/attachments/{attachmentKey}`
- `POST /v1/attachments/{attachmentKey}/handoff`
- `GET /v1/attachments/download/{token}`

Why it exists:

- this collapses the highest-friction chain in the current workflow into a single tool

### 5. `zotero_attach_pdf`

Purpose:

- attach a PDF from a local file or remote file URL into the Zotero paper store

Primary user intents:

- add a PDF to an existing item
- upload a PDF and connect it to a DOI or metadata-defined paper

Input:

- `source`: `file_path | file_url`
- `file_path`
- `file_url`
- `item_key`
- `doi`
- `metadata`
- `collection_key`
- `tags[]`
- `create_top_level_attachment_if_needed`

Behavior:

- local files use multipart upload
- remote URLs use guarded remote fetch
- the tool should be treated as storage-oriented, not analysis-oriented

Output:

- `itemKey`
- `attachmentKey`
- `filename`
- `status`

Back-end mapping:

- `POST /v1/papers/upload-pdf-multipart`
- `POST /v1/papers/upload-pdf-action`

Why it exists:

- storing PDFs is part of using Zotero as the paper warehouse

### 6. `zotero_record_paper_analysis`

Purpose:

- persist model outputs for a specific paper back into Zotero structured notes

Primary user intents:

- write a summary for this paper
- record methods, findings, limitations, future work, and relevance
- store outputs so they can be found and reused later

Input:

- `item_key`
- `notes[]`
- each note supports:
  - `note_type`
  - `slot`
  - `title`
  - `body_markdown`
  - `payload`
  - `schema_version`
  - `provenance`

Required note types:

- `paper.summary`
- `paper.methods`
- `paper.findings`
- `paper.limitations`
- `paper.future_work`
- `paper.relevance`
- `workflow.todo`
- `synthesis.theme`
- `synthesis.conflict`
- `synthesis.gap_candidate`

Recommended payload patterns for per-paper notes:

- `summary`
- `key_points[]`
- `methods[]`
- `findings[]`
- `limitations[]`
- `future_work[]`
- `relevance`
- `confidence`

Recommended payload patterns for cross-paper synthesis notes:

- `source_item_keys[]`
- `themes[]`
- `agreements[]`
- `conflicts[]`
- `gap_candidates[]`
- `candidate_questions[]`
- `next_search_queries[]`

Behavior:

- batch note writing is required
- `slot` allows a stable location per note type
- synthesis notes remain item-scoped in Zotero but reference multiple source papers through `source_item_keys[]`

Output:

- `results[]`
- per result:
  - `noteKey`
  - `itemKey`
  - `noteType`
  - `slot`
  - `updatedAt`

Back-end mapping:

- `POST /v1/items/{itemKey}/notes/upsert-ai-note`

Why it exists:

- this tool turns Zotero from a paper store into a reusable model-output store

### 7. `zotero_read_records`

Purpose:

- read back stored notes and paper records for reuse, synthesis, and review drafting

Primary user intents:

- show notes on a paper
- retrieve specific notes by key
- gather previously stored summaries or gap candidates

Input:

- `item_keys[]`
- `note_keys[]`
- `note_types[]`
- `query`
- `limit`

Behavior:

- if `note_keys[]` is provided, fetch exact note records
- if `item_keys[]` is provided, return note summaries grouped by paper
- if `query` is provided, prefer note-aware library search and then hydrate matching records
- this tool is for retrieval of stored analysis artifacts, not for initial paper search

Output:

- `records[]`
- each record includes:
  - `noteKey`
  - `itemKey`
  - `paperTitle`
  - `noteType`
  - `slot`
  - `bodyMarkdown`
  - `payload`
  - `schemaVersion`
  - `provenance`
  - `updatedAt`

Back-end mapping:

- `GET /v1/items/{itemKey}/notes`
- `GET /v1/notes/{noteKey}`
- optional note-aware search through existing item search endpoints inside the adapter

Why it exists:

- stored model outputs only become useful if they can be pulled back into future review and gap workflows

### 8. `zotero_delete_records`

Purpose:

- safely delete notes, attachments, or top-level items by explicit key

Primary user intents:

- remove smoke-test data
- clean up bad attachments
- delete notes that should not remain in the research store
- delete an incorrectly ingested top-level item

Input:

- `item_keys[]`
- `attachment_keys[]`
- `note_keys[]`
- `dry_run`
- `confirm`

Behavior:

- deletion is always key-based
- there is no query-driven or search-driven bulk delete
- `dry_run=true` is the default
- top-level item deletion requires `confirm=true`
- notes delete through the bridge note route
- attachments and top-level items delete directly through the Zotero Web API

Output:

- `dryRun`
- `count`
- `deletedCount`
- `wouldDeleteCount`
- `alreadyMissingCount`
- `results[]`
- each result includes:
  - `key`
  - `requestedKind`
  - `resolvedKind`
  - `itemType`
  - `title`
  - `parentItem`
  - `status`
  - optional `warning`

Back-end mapping:

- `DELETE /v1/notes/{noteKey}`
- direct Zotero Web API item deletion for attachments and top-level items

Why it exists:

- a practical research store needs cleanup
- keeping deletion constrained by explicit keys avoids turning MCP into a dangerous raw CRUD surface

### 9. `zotero_patch_item_metadata`

Purpose:

- safely correct top-level paper metadata without exposing arbitrary item mutation

Primary user intents:

- fix a bad title
- correct authors
- update DOI, venue, date, tags, or collections
- clear incorrect abstract or DOI values

Input:

- `item_key`
- `title`
- `authors[]` or `creators[]`
- `abstract`
- `venue`
- `date`
- `year`
- `doi`
- `add_tags[]`
- `remove_tags[]`
- `add_collection_keys[]`
- `remove_collection_keys[]`
- `clear_fields[]`
- `dry_run`

Behavior:

- only top-level items can be patched
- patching is limited to a constrained metadata surface
- the tool does not accept arbitrary JSON patch instructions
- `authors[]` is the simple interface; `creators[]` is the structured override
- `clear_fields` is limited to `abstract`, `venue`, `date`, and `doi`
- `dry_run` shows the normalized post-patch state without writing it

Output:

- `status`: `would_update | updated`
- `itemKey`
- `title`
- `authors[]`
- `year`
- `doi`
- `venue`
- `tags[]`
- `collections[]`
- `updatedFields[]`
- optional `warning`

Back-end mapping:

- direct Zotero Web API item patch through the existing Zotero client

Why it exists:

- a paper warehouse is only useful if bad metadata can be corrected
- a constrained patch tool keeps the correction path safe and predictable

## Search Semantics

Search must satisfy the following use cases.

### Local library search

Supported styles:

- keyword search
- fielded search
- DOI lookup
- recent items
- tag search
- collection search
- note-aware search

Searchable field set for fielded mode:

- `title`
- `creator`
- `abstract`
- `venue`
- `doi`
- `tag`
- `note`

This is critical because model outputs stored as notes must become searchable research assets.

### OpenAlex search

Supported styles:

- keyword search
- DOI lookup
- recent search
- year-range filtering
- open-access filtering

Supported sorts:

- `relevance`
- `cited_by`
- `recent`

### Combined search

When `scope = both`:

- run local and external search together
- normalize outputs
- preserve origin metadata
- expose whether the external result already exists in Zotero

## Notes as the Model Output Store

This design intentionally treats structured notes as first-class stored outputs.

There are two main categories.

### Per-paper analysis

Examples:

- summary
- methods
- findings
- limitations
- future work
- relevance

These attach directly to one paper item.

### Cross-paper synthesis

Examples:

- themes
- conflicts
- gap candidates
- candidate research questions
- review outlines

These are still stored as Zotero child notes under an anchor item, but must include `source_item_keys[]` in payload so they remain traceable across the reviewed paper set.

## What MCP Must Hide

These should not be exposed as separate model-facing tools:

- raw handoff creation
- token-based download
- raw import endpoint selection
- raw search endpoint selection
- raw note CRUD variants
- low-level attachment detail steps

These remain implementation details inside the adapter.

## Normalized Error Model

All tools should map failures into a stable error shape:

```json
{
  "error": {
    "kind": "auth|validation|not_found|missing_pdf|ambiguous_attachment|upstream|conflict",
    "message": "human-readable message",
    "retryable": false,
    "details": {}
  }
}
```

Repository-specific errors that must exist:

- `missing_pdf`
- `ambiguous_attachment`

## Output Normalization Rules

To keep Codex efficient:

- never return raw bridge responses unless required
- trim repeated HTML and verbose metadata by default
- normalize papers, attachments, and notes into compact shapes
- support batch operations where repeated single-item calls would otherwise be common

Batch support is required for:

- `zotero_ingest_papers`
- `zotero_build_workspace`
- `zotero_prepare_pdf`
- `zotero_record_paper_analysis`

## Relationship to Existing Repo Assets

This MCP design complements, not replaces:

- OpenAPI contracts
- the existing bridge API
- agent integration docs
- the `zotero-bridge` skill

Recommended division of responsibility:

- OpenAPI: external and generic tool integration
- Skill: workflow guidance and product boundary
- MCP: compact typed tools for Codex-style workflows

## v1 Success Criteria

The MCP design succeeds if Codex can reliably do the following with minimal trial-and-error:

1. search the local library and OpenAlex
2. import papers into Zotero
3. retrieve a compact workspace for selected papers
4. download PDFs locally for reading
5. store paper-level model outputs back into Zotero
6. retrieve stored outputs for later review writing
7. use stored outputs and search to explore new research questions

## Future Extensions

Not part of v1, but compatible with this design:

- collection maintenance tools
- tag maintenance tools
- duplicate detection and merge tools
- dedicated synthesis note tool if cross-paper workflows become much heavier
- saved search or review project abstractions above raw item sets
