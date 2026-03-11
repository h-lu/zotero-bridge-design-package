from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.auth import require_bearer_auth
from app.config import get_settings
from app.dependencies import get_bridge_service
from app.errors import BridgeError
from app.models import (
    AddByDOIRequest,
    AddByDOIResponse,
    ImportDiscoveryHitRequest,
    ImportDiscoveryHitResponse,
    ImportMetadataRequest,
    ImportMetadataResponse,
    UploadPdfActionRequest,
    UploadPdfResponse,
)
from app.services.bridge_service import BridgeService

MULTIPART_READ_CHUNK_SIZE = 1024 * 1024

router = APIRouter(
    prefix="/v1/papers",
    tags=["Papers"],
    dependencies=[Depends(require_bearer_auth)],
)
BridgeDep = Annotated[BridgeService, Depends(get_bridge_service)]
FileDep = Annotated[UploadFile, File(...)]


async def _read_upload_content(file: UploadFile, *, max_bytes: int) -> bytes:
    content = bytearray()
    while True:
        chunk = await file.read(MULTIPART_READ_CHUNK_SIZE)
        if not chunk:
            return bytes(content)
        content.extend(chunk)
        if len(content) > max_bytes:
            raise BridgeError(
                code="FILE_TOO_LARGE",
                message="Uploaded file exceeds configured size limit",
                status_code=413,
            )


@router.post("/add-by-doi", response_model=AddByDOIResponse, operation_id="addByDOI")
async def add_by_doi(
    payload: AddByDOIRequest,
    bridge: BridgeDep,
) -> AddByDOIResponse:
    return await bridge.add_by_doi(payload)


@router.post(
    "/import-metadata",
    response_model=ImportMetadataResponse,
    operation_id="importPaperMetadata",
)
async def import_metadata(
    payload: ImportMetadataRequest,
    bridge: BridgeDep,
) -> ImportMetadataResponse:
    return await bridge.import_metadata(payload)


@router.post(
    "/import-discovery-hit",
    response_model=ImportDiscoveryHitResponse,
    operation_id="importDiscoveryHit",
)
async def import_discovery_hit(
    payload: ImportDiscoveryHitRequest,
    bridge: BridgeDep,
) -> ImportDiscoveryHitResponse:
    return await bridge.import_discovery_hit(payload)


@router.post(
    "/upload-pdf-action",
    response_model=UploadPdfResponse,
    operation_id="uploadPdfAction",
)
async def upload_pdf_action(
    payload: UploadPdfActionRequest,
    bridge: BridgeDep,
) -> UploadPdfResponse:
    return await bridge.upload_pdf_from_action(payload)


@router.post(
    "/upload-pdf-multipart",
    response_model=UploadPdfResponse,
    operation_id="uploadPdfMultipart",
)
async def upload_pdf_multipart(
    bridge: BridgeDep,
    file: FileDep,
    itemKey: Annotated[str | None, Form()] = None,
    doi: Annotated[str | None, Form()] = None,
    collectionKey: Annotated[str | None, Form()] = None,
    tags: Annotated[str | None, Form()] = None,
    createTopLevelAttachmentIfNeeded: Annotated[bool, Form()] = False,
    requestId: Annotated[str | None, Form()] = None,
) -> UploadPdfResponse:
    content = await _read_upload_content(
        file,
        max_bytes=get_settings().max_upload_file_bytes,
    )
    if not content:
        raise BridgeError(
            code="BAD_REQUEST",
            message="file is required",
            status_code=400,
        )
    parsed_tags = [tag.strip() for tag in (tags or "").split(",") if tag.strip()]
    return await bridge.upload_pdf_bytes(
        content=content,
        filename=file.filename or "upload.pdf",
        content_type=file.content_type or "application/pdf",
        item_key=itemKey,
        doi=doi,
        collection_key=collectionKey,
        tags=parsed_tags,
        create_top_level=createTopLevelAttachmentIfNeeded,
        request_id=requestId,
    )
