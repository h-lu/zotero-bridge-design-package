from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.config import get_settings
from app.errors import BridgeError
from app.models import AttachmentHandoffRequest
from app.services.attachment_service import AttachmentService


def zotero_attachment(
    key: str,
    *,
    parent: str = "ITEM0001",
    filename: str = "paper.pdf",
    link_mode: str = "imported_file",
) -> dict[str, Any]:
    return {
        "key": key,
        "version": 1,
        "data": {
            "itemType": "attachment",
            "parentItem": parent,
            "title": filename,
            "filename": filename,
            "contentType": "application/pdf",
            "linkMode": link_mode,
        },
    }


class FakeZoteroClient:
    def __init__(self) -> None:
        self.items = {"ATTACH01": zotero_attachment("ATTACH01")}

    async def get_item(self, item_key: str) -> dict[str, Any]:
        try:
            return self.items[item_key]
        except KeyError as exc:
            raise BridgeError(
                code="ATTACHMENT_NOT_FOUND",
                message="Attachment not found",
                status_code=404,
            ) from exc

    async def get_children(self, item_key: str) -> list[dict[str, Any]]:
        del item_key
        return [self.items["ATTACH01"]]

    async def download_attachment_file(self, attachment_key: str) -> tuple[bytes, str | None]:
        assert attachment_key == "ATTACH01"
        return b"%PDF-1.4\nbinary", "application/pdf"


@pytest.mark.asyncio
async def test_attachment_handoff_generates_download_url(test_env: None) -> None:
    settings = get_settings()
    async with httpx.AsyncClient() as http_client:
        service = AttachmentService(
            settings=settings,
            zotero_client=FakeZoteroClient(),
            http_client=http_client,
        )

        response = await service.create_handoff(
            attachment_key="ATTACH01",
            payload=AttachmentHandoffRequest(),
            download_url="https://bridge.example.com/v1/attachments/download/{token}",
        )

        assert response.attachmentKey == "ATTACH01"
        assert response.downloadUrl.startswith(
            "https://bridge.example.com/v1/attachments/download/tkn_"
        )


@pytest.mark.asyncio
async def test_attachment_handoff_invalid_and_expired_tokens_fail(test_env: None) -> None:
    settings = get_settings()
    async with httpx.AsyncClient() as http_client:
        service = AttachmentService(
            settings=settings,
            zotero_client=FakeZoteroClient(),
            http_client=http_client,
        )

        with pytest.raises(BridgeError) as invalid_exc:
            await service.download_attachment_by_token("bogus")

        assert invalid_exc.value.code == "INVALID_DOWNLOAD_TOKEN"

        response = await service.create_handoff(
            attachment_key="ATTACH01",
            payload=AttachmentHandoffRequest(),
            download_url="https://bridge.example.com/v1/attachments/download/{token}",
        )
        token = response.downloadUrl.rsplit("/", 1)[-1]
        service._tokens[token] = service._tokens[token].__class__(
            attachment_key="ATTACH01",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
            zotero_api_key=settings.zotero_api_key,
            zotero_library_type=settings.zotero_library_type,
            zotero_library_id=settings.zotero_library_id,
        )

        with pytest.raises(BridgeError) as expired_exc:
            await service.download_attachment_by_token(token)

        assert expired_exc.value.code == "EXPIRED_DOWNLOAD_TOKEN"


@pytest.mark.asyncio
async def test_linked_file_attachments_are_not_downloadable(test_env: None) -> None:
    settings = get_settings()
    zotero_client = FakeZoteroClient()
    zotero_client.items["ATTACH02"] = zotero_attachment(
        "ATTACH02",
        filename="local.pdf",
        link_mode="linked_file",
    )
    async with httpx.AsyncClient() as http_client:
        service = AttachmentService(
            settings=settings,
            zotero_client=zotero_client,
            http_client=http_client,
        )

        detail = await service.get_attachment_detail("ATTACH02")

        assert detail.attachment.downloadable is False

        with pytest.raises(BridgeError) as exc:
            await service.create_handoff(
                attachment_key="ATTACH02",
                payload=AttachmentHandoffRequest(),
                download_url="https://bridge.example.com/v1/attachments/download/{token}",
            )

        assert exc.value.code == "BAD_REQUEST"
