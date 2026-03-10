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
    attachments: list[AttachmentSummary] = Field(default_factory=list)
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


class BatchFulltextPreviewRequest(StrictModel):
    itemKeys: list[str] = Field(min_length=1, max_length=20)
    maxChars: int = Field(default=3000, ge=1000)
    preferSource: str = Field(default="auto", pattern="^(auto|web|cache)$")


class FulltextPreviewItem(StrictModel):
    itemKey: str
    attachmentKey: str | None = None
    content: str | None = None
    source: FulltextSource | None = None
    nextCursor: int | None = None
    attachmentCandidates: list[str] = Field(default_factory=list)
    errorCode: str | None = None
    errorMessage: str | None = None


class BatchFulltextPreviewResponse(StrictModel):
    items: list[FulltextPreviewItem]
    count: int


class ReviewPackRequest(StrictModel):
    itemKeys: list[str] = Field(min_length=1, max_length=20)
    maxFulltextChars: int = Field(default=3000, ge=1000)
    citationStyle: str | None = None
    citationLocale: str | None = None
    includeRelated: bool = True
    includeNotes: bool = True
    includeFulltextPreview: bool = True


class ReviewPackItem(StrictModel):
    item: SearchItem
    citation: CitationResponse
    fulltextPreview: FulltextPreviewItem | None = None
    notes: list[NoteRecord] = Field(default_factory=list)
    relatedItems: list[SearchItem] = Field(default_factory=list)


class ReviewPackResponse(StrictModel):
    items: list[ReviewPackItem]
    count: int
    notFoundKeys: list[str] = Field(default_factory=list)


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
