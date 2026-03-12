from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response

from app.dependencies import get_bridge_service, get_default_bridge_service
from app.models import (
    AttachmentDetailResponse,
    AttachmentHandoffRequest,
    AttachmentHandoffResponse,
)
from app.services.bridge_service import BridgeService

router = APIRouter(
    prefix="/v1/attachments",
    tags=["Attachments"],
)
BridgeDep = Annotated[BridgeService, Depends(get_bridge_service)]
DefaultBridgeDep = Annotated[BridgeService, Depends(get_default_bridge_service)]


def _download_url_template(request: Request, *, public_base_url: str | None) -> str:
    token_placeholder = "tkn_PLACEHOLDER"
    if public_base_url:
        path = str(
            request.app.url_path_for(
                "download_attachment_by_token",
                token=token_placeholder,
            )
        )
        return f"{public_base_url.rstrip('/')}{path}".replace(token_placeholder, "{token}")
    return str(
        request.url_for(
            "download_attachment_by_token",
            token=token_placeholder,
        )
    ).replace(token_placeholder, "{token}")


@router.get(
    "/{attachmentKey}",
    response_model=AttachmentDetailResponse,
    operation_id="getAttachmentDetail",
)
async def get_attachment_detail(
    attachmentKey: str,
    bridge: BridgeDep,
) -> AttachmentDetailResponse:
    return await bridge.get_attachment_detail(attachmentKey)


@router.post(
    "/{attachmentKey}/handoff",
    response_model=AttachmentHandoffResponse,
    operation_id="createAttachmentHandoff",
)
async def create_attachment_handoff(
    attachmentKey: str,
    payload: AttachmentHandoffRequest,
    request: Request,
    bridge: BridgeDep,
) -> AttachmentHandoffResponse:
    download_url_template = _download_url_template(
        request,
        public_base_url=bridge._settings.public_base_url,
    )
    return await bridge.create_attachment_handoff(
        attachment_key=attachmentKey,
        payload=payload,
        download_url_template=download_url_template,
    )


@router.get(
    "/download/{token}",
    operation_id="downloadAttachmentByToken",
    name="download_attachment_by_token",
    responses={
        200: {
            "description": "Attachment download",
            "content": {
                "application/pdf": {
                    "schema": {"type": "string", "format": "binary"}
                },
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                },
            },
        }
    },
)
async def download_attachment_by_token(
    token: str,
    bridge: DefaultBridgeDep,
) -> Response:
    download = await bridge.download_attachment_by_token(token)
    filename = download.attachment.filename or f"{download.attachment.attachmentKey}.bin"
    quoted = quote(filename)
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
    }
    return Response(
        content=download.content,
        media_type=download.content_type,
        headers=headers,
    )
