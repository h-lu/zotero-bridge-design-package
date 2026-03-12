from __future__ import annotations

from enum import StrEnum
from typing import Any

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


class CreatorInput(StrictModel):
    firstName: str | None = None
    lastName: str | None = None
    name: str | None = None
    creatorType: str = "author"


class AttachmentRecord(StrictModel):
    attachmentKey: str
    parentItemKey: str | None = None
    title: str
    contentType: str
    filename: str | None = None
    linkMode: str
    md5: str | None = None
    mtime: str | None = None
    isPdf: bool
    downloadable: bool


AttachmentSummary = AttachmentRecord


class AINoteSummary(StrictModel):
    noteKey: str
    agent: str
    noteType: str
    slot: str
    dateModified: str
    tags: list[str] = Field(default_factory=list)
    schemaVersion: str | None = None


class ProvenanceRecord(StrictModel):
    attachmentKey: str | None = None
    page: int | None = None
    locator: str | None = None
    cursorStart: int | None = None
    cursorEnd: int | None = None
    quote: str | None = None


class SearchHint(StrictModel):
    field: str
    snippet: str | None = None


class SearchItem(StrictModel):
    itemKey: str
    itemType: str
    title: str
    date: str | None = None
    dateAdded: str | None = None
    dateModified: str | None = None
    year: str | None = None
    DOI: str | None = None
    abstractNote: str | None = None
    publicationTitle: str | None = None
    venue: str | None = None
    url: str | None = None
    publisher: str | None = None
    bookTitle: str | None = None
    proceedingsTitle: str | None = None
    conferenceName: str | None = None
    language: str | None = None
    extra: str | None = None
    relations: list[str] = Field(default_factory=list)
    creators: list[Creator]
    tags: list[str] = Field(default_factory=list)
    collectionKeys: list[str] = Field(default_factory=list)
    attachments: list[AttachmentRecord] = Field(default_factory=list)
    aiNotes: list[AINoteSummary] = Field(default_factory=list)


class SearchResultItem(SearchItem):
    searchHints: list[SearchHint] = Field(default_factory=list)
    score: float | None = None


class SearchResponse(StrictModel):
    items: list[SearchResultItem]
    count: int
    total: int
    start: int
    limit: int
    nextStart: int | None = None


class ItemListResponse(StrictModel):
    items: list[SearchItem]
    count: int
    total: int
    start: int
    limit: int
    nextStart: int | None = None


class BatchItemRequest(StrictModel):
    itemKeys: list[str] = Field(min_length=1, max_length=50)
    includeAttachments: bool = False
    includeNotes: bool = False


class BatchItemResponse(StrictModel):
    items: list[SearchItem]
    count: int
    notFoundKeys: list[str] = Field(default_factory=list)


class ResolveItemsResponse(StrictModel):
    strategy: str
    items: list[SearchItem]
    count: int


class DuplicateGroup(StrictModel):
    field: str
    value: str
    items: list[SearchItem]
    count: int


class DuplicateGroupsResponse(StrictModel):
    groups: list[DuplicateGroup]
    count: int
    total: int
    start: int
    limit: int
    nextStart: int | None = None


class CollectionSummary(StrictModel):
    collectionKey: str
    name: str
    parentCollectionKey: str | None = None
    path: str
    depth: int
    numCollections: int | None = None
    numItems: int | None = None


class CollectionListResponse(StrictModel):
    collections: list[CollectionSummary]
    count: int
    total: int
    start: int
    limit: int
    nextStart: int | None = None


class TagSummary(StrictModel):
    tag: str
    type: int | None = None
    numItems: int | None = None


class TagListResponse(StrictModel):
    tags: list[TagSummary]
    count: int
    total: int
    start: int
    limit: int
    nextStart: int | None = None


class ItemTypeCount(StrictModel):
    itemType: str
    count: int


class DuplicateStats(StrictModel):
    titleGroups: int
    doiGroups: int


class SearchIndexStats(StrictModel):
    enabled: bool
    ready: bool
    recordCount: int = 0
    refreshedAt: str | None = None
    lastModifiedVersion: int | None = None
    lastSyncMethod: str | None = None
    lastError: str | None = None
    lastErrorAt: str | None = None


class LibraryStatsResponse(StrictModel):
    totalItems: int
    itemTypeCounts: list[ItemTypeCount]
    collectionCount: int
    tagCount: int
    duplicateGroups: DuplicateStats
    lastModifiedVersion: int | None = None
    searchIndex: SearchIndexStats


class ItemDetailResponse(StrictModel):
    item: SearchItem


class ItemChangesResponse(StrictModel):
    items: list[SearchItem]
    count: int
    total: int
    start: int
    limit: int
    nextStart: int | None = None
    deletedItemKeys: list[str] = Field(default_factory=list)
    deletedCount: int = 0
    sinceVersion: int | None = None
    sinceTimestamp: str | None = None
    latestVersion: int | None = None


class AdvancedSearchResponse(StrictModel):
    items: list[SearchResultItem]
    count: int
    total: int
    start: int
    limit: int
    nextStart: int | None = None


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
    schemaVersion: str | None = None
    payload: dict[str, Any] | None = None
    provenance: list[ProvenanceRecord] | None = None
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
    schemaVersion: str | None = None


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
    schemaVersion: str | None = None
    structuredPayload: dict[str, Any] | None = None
    provenance: list[ProvenanceRecord] = Field(default_factory=list)


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


class ReviewPackRequest(StrictModel):
    itemKeys: list[str] = Field(min_length=1, max_length=20)
    citationStyle: str | None = None
    citationLocale: str | None = None
    includeRelated: bool = True
    includeNotes: bool = True


class ReviewPackItem(StrictModel):
    item: SearchItem
    citation: CitationResponse
    notes: list[NoteRecord] = Field(default_factory=list)
    relatedItems: list[SearchItem] = Field(default_factory=list)


class ReviewPackResponse(StrictModel):
    items: list[ReviewPackItem]
    count: int
    notFoundKeys: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CitationResponse(StrictModel):
    itemKey: str
    style: str
    locale: str
    citationHtml: str
    bibliographyHtml: str


class DiscoveryAuthor(StrictModel):
    name: str
    openAlexId: str | None = None


class DiscoveryTopic(StrictModel):
    name: str
    openAlexId: str | None = None
    score: float | None = None


class DiscoveryWork(StrictModel):
    openAlexId: str
    title: str
    doi: str | None = None
    publicationYear: int | None = None
    publicationDate: str | None = None
    workType: str | None = None
    citedByCount: int | None = None
    venue: str | None = None
    landingPageUrl: str | None = None
    pdfUrl: str | None = None
    isOpenAccess: bool | None = None
    abstract: str | None = None
    authors: list[DiscoveryAuthor] = Field(default_factory=list)
    topics: list[DiscoveryTopic] = Field(default_factory=list)
    alreadyInLibrary: bool = False
    libraryItemKey: str | None = None
    libraryMatchStrategy: str | None = None


class DiscoverySearchResponse(StrictModel):
    items: list[DiscoveryWork]
    count: int
    total: int
    start: int
    limit: int
    nextStart: int | None = None


class ItemTagsWriteRequest(StrictModel):
    tags: list[str] = Field(min_length=1, max_length=50)


class ItemTagsResponse(StrictModel):
    itemKey: str
    tags: list[str]
    addedTags: list[str] = Field(default_factory=list)
    removedTag: str | None = None


class ItemCollectionsWriteRequest(StrictModel):
    collectionKeys: list[str] = Field(min_length=1, max_length=50)


class ItemCollectionsResponse(StrictModel):
    itemKey: str
    collectionKeys: list[str]
    addedCollectionKeys: list[str] = Field(default_factory=list)


class RelatedItemsResponse(StrictModel):
    itemKey: str
    items: list[SearchItem]
    count: int


class MergeDuplicateItemsRequest(StrictModel):
    primaryItemKey: str
    duplicateItemKeys: list[str] = Field(min_length=1, max_length=20)
    dryRun: bool = True
    moveAttachments: bool = True
    moveNotes: bool = True
    mergeTags: bool = True
    mergeCollections: bool = True


class MergeDuplicateItemsStatus(StrEnum):
    DRY_RUN = "dry_run"
    MERGED = "merged"


class MergeDuplicateItemsResponse(StrictModel):
    status: MergeDuplicateItemsStatus
    primaryItem: SearchItem
    duplicateItemKeys: list[str]
    movedAttachmentKeys: list[str] = Field(default_factory=list)
    movedNoteKeys: list[str] = Field(default_factory=list)
    addedTags: list[str] = Field(default_factory=list)
    addedCollectionKeys: list[str] = Field(default_factory=list)
    deletedItemKeys: list[str] = Field(default_factory=list)


class AttachmentListResponse(StrictModel):
    itemKey: str
    attachments: list[AttachmentRecord]
    count: int


class AttachmentDetailResponse(StrictModel):
    attachment: AttachmentRecord


class AttachmentHandoffMode(StrEnum):
    PROXY_DOWNLOAD = "proxy_download"


class AttachmentHandoffRequest(StrictModel):
    mode: AttachmentHandoffMode = AttachmentHandoffMode.PROXY_DOWNLOAD
    expiresInSeconds: int | None = Field(default=None, ge=60, le=86400)
    requestId: str | None = None


class AttachmentHandoffResponse(StrictModel):
    attachmentKey: str
    filename: str | None = None
    contentType: str
    mode: AttachmentHandoffMode
    downloadUrl: str
    expiresAt: str


class ImportStatus(StrEnum):
    CREATED = "created"
    EXISTING = "exists"
    UPDATED = "updated"


class ImportMetadataRequest(StrictModel):
    itemType: str
    title: str
    creators: list[CreatorInput] = Field(default_factory=list)
    abstractNote: str | None = None
    publicationTitle: str | None = None
    date: str | None = None
    doi: str | None = None
    url: str | None = None
    tags: list[str] = Field(default_factory=list)
    collectionKey: str | None = None
    extra: str | None = None
    updateIfExists: bool = False
    requestId: str | None = None


class ImportMetadataResponse(StrictModel):
    status: ImportStatus
    itemKey: str
    title: str
    dedupeStrategy: str


class ImportDiscoveryHitRequest(StrictModel):
    openAlexId: str | None = None
    title: str
    doi: str | None = None
    publicationYear: int | None = None
    publicationDate: str | None = None
    workType: str | None = None
    venue: str | None = None
    landingPageUrl: str | None = None
    pdfUrl: str | None = None
    isOpenAccess: bool | None = None
    abstract: str | None = None
    authors: list[DiscoveryAuthor] = Field(default_factory=list)
    topics: list[DiscoveryTopic] = Field(default_factory=list)
    attachPdfFromOpenAccessUrl: bool = False
    collectionKey: str | None = None
    tags: list[str] = Field(default_factory=list)
    updateIfExists: bool = False
    requestId: str | None = None


class ImportDiscoveryHitResponse(StrictModel):
    status: ImportStatus
    itemKey: str
    title: str
    dedupeStrategy: str
    attachment: UploadPdfResponse | None = None
