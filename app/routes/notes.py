from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.dependencies import get_bridge_service
from app.models import (
    NoteDeleteResponse,
    NoteDetailResponse,
    NoteWriteRequest,
    NoteWriteResponse,
)
from app.services.bridge_service import BridgeService

router = APIRouter(
    prefix="/v1/notes",
    tags=["Notes"],
)
BridgeDep = Annotated[BridgeService, Depends(get_bridge_service)]


@router.get("/{noteKey}", response_model=NoteDetailResponse, operation_id="getNoteDetail")
async def get_note_detail(
    noteKey: str,
    bridge: BridgeDep,
) -> NoteDetailResponse:
    return await bridge.get_note_detail(noteKey)


@router.patch("/{noteKey}", response_model=NoteWriteResponse, operation_id="updateNote")
async def update_note(
    noteKey: str,
    payload: NoteWriteRequest,
    bridge: BridgeDep,
) -> NoteWriteResponse:
    return await bridge.update_note(note_key=noteKey, payload=payload)


@router.delete("/{noteKey}", response_model=NoteDeleteResponse, operation_id="deleteNote")
async def delete_note(
    noteKey: str,
    bridge: BridgeDep,
) -> NoteDeleteResponse:
    return await bridge.delete_note(note_key=noteKey)
