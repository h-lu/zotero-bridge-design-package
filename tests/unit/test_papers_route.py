from __future__ import annotations

import pytest

from app.errors import BridgeError
from app.routes.papers import _read_upload_content


class FakeUploadFile:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.read_calls = 0

    async def read(self, _: int = -1) -> bytes:
        self.read_calls += 1
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


@pytest.mark.asyncio
async def test_read_upload_content_stops_when_size_limit_is_exceeded() -> None:
    upload = FakeUploadFile([b"%PDF", b"12345", b"67890"])

    with pytest.raises(BridgeError) as exc_info:
        await _read_upload_content(upload, max_bytes=8)

    assert exc_info.value.code == "FILE_TOO_LARGE"
    assert exc_info.value.status_code == 413
    assert upload.read_calls == 2
