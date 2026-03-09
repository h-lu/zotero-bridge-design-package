from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ErrorDetail(StrictModel):
    code: str
    message: str
    upstreamStatus: int | None = None
    requestId: str | None = None


class ErrorEnvelope(StrictModel):
    error: ErrorDetail


class HealthConfig(StrictModel):
    zoteroConfigured: bool
    libraryType: str
    libraryId: str


class HealthResponse(StrictModel):
    ok: bool
    service: str
    version: str
    config: HealthConfig


class Creator(StrictModel):
    displayName: str
    creatorType: str | None = None


class AttachmentSummary(StrictModel):
    attachmentKey: str
    title: str
    contentType: str
    filename: str | None = None
    linkMode: str
    md5: str | None = None
    mtime: str | None = None
    isPdf: bool
    hasFulltext: bool


class AINoteSummary(StrictModel):
    noteKey: str
    agent: str
    noteType: str
    slot: str
    dateModified: str
    tags: list[str] = Field(default_factory=list)


class SearchItem(StrictModel):
    itemKey: str
    itemType: str
    title: str
    year: str | None = None
    DOI: str | None = None
    creators: list[Creator]
    tags: list[str] = Field(default_factory=list)
    collectionKeys: list[str] = Field(default_factory=list)
    attachments: list[AttachmentSummary] = Field(default_factory=list)
    aiNotes: list[AINoteSummary] = Field(default_factory=list)


class SearchResponse(StrictModel):
    items: list[SearchItem]
    count: int


class ItemDetailResponse(StrictModel):
    item: SearchItem


class AddByDOIRequest(StrictModel):
    doi: str
    collectionKey: str | None = None
    tags: list[str] = Field(default_factory=list)
    requestId: str | None = None


class AddByDOIStatus(StrEnum):
    CREATED = "created"
    EXISTING = "existing"


class AddByDOIResponse(StrictModel):
    status: AddByDOIStatus
    itemKey: str
    title: str
    DOI: str | None = None


class OpenAIFileRef(StrictModel):
    name: str
    id: str
    mime_type: str
    download_link: HttpUrl


class UploadPdfActionRequest(StrictModel):
    itemKey: str | None = None
    doi: str | None = None
    fileUrl: HttpUrl | None = None
    openaiFileIdRefs: list[OpenAIFileRef] = Field(default_factory=list)
    collectionKey: str | None = None
    tags: list[str] = Field(default_factory=list)
    createTopLevelAttachmentIfNeeded: bool = False
    requestId: str | None = None


class UploadPdfStatus(StrEnum):
    CREATED = "created"
    UPDATED = "updated"


class UploadPdfResponse(StrictModel):
    status: UploadPdfStatus
    itemKey: str | None = None
    attachmentKey: str
    filename: str | None = None
    contentType: str
    title: str | None = None


class NoteMode(StrEnum):
    REPLACE = "replace"
    APPEND = "append"


class UpsertAINoteRequest(StrictModel):
    agent: str
    noteType: str
    slot: str = "default"
    mode: NoteMode = NoteMode.REPLACE
    title: str | None = None
    bodyMarkdown: str
    tags: list[str] = Field(default_factory=list)
    model: str | None = None
    sourceAttachmentKey: str | None = None
    sourceCursorStart: int | None = None
    sourceCursorEnd: int | None = None
    requestId: str | None = None


class UpsertAINoteStatus(StrEnum):
    CREATED = "created"
    UPDATED = "updated"


class UpsertAINoteResponse(StrictModel):
    status: UpsertAINoteStatus
    noteKey: str
    itemKey: str
    agent: str
    noteType: str
    slot: str


class NoteRecord(StrictModel):
    noteKey: str
    itemKey: str | None = None
    bodyHtml: str
    bodyText: str
    tags: list[str] = Field(default_factory=list)
    dateAdded: str | None = None
    dateModified: str | None = None
    isAiNote: bool
    agent: str | None = None
    noteType: str | None = None
    slot: str | None = None


class ItemNotesResponse(StrictModel):
    itemKey: str
    notes: list[NoteRecord]
    count: int


class NoteDetailResponse(StrictModel):
    note: NoteRecord


class NoteWriteRequest(StrictModel):
    title: str | None = None
    bodyMarkdown: str
    mode: NoteMode = NoteMode.REPLACE
    tags: list[str] | None = None
    requestId: str | None = None


class NoteWriteStatus(StrEnum):
    CREATED = "created"
    UPDATED = "updated"


class NoteWriteResponse(StrictModel):
    status: NoteWriteStatus
    noteKey: str
    itemKey: str | None = None


class NoteDeleteStatus(StrEnum):
    DELETED = "deleted"


class NoteDeleteResponse(StrictModel):
    status: NoteDeleteStatus
    noteKey: str
    itemKey: str | None = None


class FulltextSource(StrEnum):
    ZOTERO_WEB_API = "zotero_web_api"
    LOCAL_CACHE = "local_cache"


class FulltextResponse(StrictModel):
    itemKey: str
    attachmentKey: str
    cursor: int
    nextCursor: int | None = None
    done: bool
    content: str
    source: FulltextSource
    indexedPages: int | None = None
    totalPages: int | None = None
    attachmentCandidates: list[str] = Field(default_factory=list)


class CitationResponse(StrictModel):
    itemKey: str
    style: str
    locale: str
    citationHtml: str
    bibliographyHtml: str
