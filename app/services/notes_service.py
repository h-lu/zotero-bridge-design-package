from __future__ import annotations

from typing import Any

from app.errors import BridgeError
from app.models import (
    ItemNotesResponse,
    NoteDeleteResponse,
    NoteDeleteStatus,
    NoteDetailResponse,
    NoteWriteRequest,
    NoteWriteResponse,
    NoteWriteStatus,
    UpsertAINoteRequest,
    UpsertAINoteResponse,
    UpsertAINoteStatus,
)

NOTE_UPDATE_MAX_ATTEMPTS = 3


class NotesService:
    def __init__(self, bridge: Any) -> None:
        self._bridge = bridge

    def _note_schema_version(self, raw_note: dict[str, Any]) -> str | None:
        note_html = str(raw_note.get("data", {}).get("note") or "")
        return self._bridge._note_renderer.parse(note_html).schema_version

    def _rendered_schema_version(self, rendered_html: str) -> str | None:
        return self._bridge._note_renderer.parse(rendered_html).schema_version

    async def list_item_notes(self, item_key: str) -> ItemNotesResponse:
        bridge = self._bridge
        await bridge._zotero.get_item(item_key)
        children = await bridge._zotero.get_children(item_key)
        notes = bridge._normalize_note_records(children)
        return ItemNotesResponse(itemKey=item_key, notes=notes, count=len(notes))

    async def create_item_note(
        self,
        *,
        item_key: str,
        payload: NoteWriteRequest,
    ) -> NoteWriteResponse:
        bridge = self._bridge
        bridge._validate_note_body(payload.bodyMarkdown)
        await bridge._zotero.get_item(item_key)
        rendered_html = bridge._note_renderer.render_user_note(
            title=payload.title,
            body_markdown=payload.bodyMarkdown,
            mode=payload.mode.value,
        )
        payload_fingerprint = bridge._note_write_payload_fingerprint(
            operation="create",
            payload=payload,
        )
        if payload.requestId:
            existing_note = await bridge._find_note_by_request_id(
                item_key=item_key,
                request_id=payload.requestId,
            )
            if existing_note is not None:
                bridge._assert_create_replay_matches(
                    existing_note=existing_note,
                    request_id=payload.requestId,
                    payload_fingerprint=payload_fingerprint,
                    rendered_html=rendered_html,
                    user_tags=payload.tags or [],
                )
                return NoteWriteResponse(
                    status=NoteWriteStatus.CREATED,
                    noteKey=str(existing_note.get("key") or ""),
                    itemKey=item_key,
                )
        note_tags = bridge._merge_tags(
            bridge._request_metadata_tags(
                request_id=payload.requestId,
                payload_fingerprint=payload_fingerprint,
            ),
            payload.tags or [],
        )
        note_payload = {
            "itemType": "note",
            "parentItem": item_key,
            "note": rendered_html,
            "tags": [{"tag": tag} for tag in note_tags],
        }
        try:
            note_key = (
                await bridge._zotero.create_items(
                    [note_payload],
                    write_token=bridge._build_write_token(payload.requestId),
                )
            )[0]
        except BridgeError as exc:
            if exc.code == "WRITE_CONFLICT" and payload.requestId:
                existing_note = await bridge._find_note_by_request_id(
                    item_key=item_key,
                    request_id=payload.requestId,
                )
                if existing_note is not None:
                    bridge._assert_create_replay_matches(
                        existing_note=existing_note,
                        request_id=payload.requestId,
                        payload_fingerprint=payload_fingerprint,
                        rendered_html=rendered_html,
                        user_tags=payload.tags or [],
                    )
                    return NoteWriteResponse(
                        status=NoteWriteStatus.CREATED,
                        noteKey=str(existing_note.get("key") or ""),
                        itemKey=item_key,
                    )
            raise
        await bridge._refresh_local_search_index_item(item_key)
        bridge._invalidate_note_search_cache()
        return NoteWriteResponse(
            status=NoteWriteStatus.CREATED,
            noteKey=note_key,
            itemKey=item_key,
        )

    async def get_note_detail(self, note_key: str) -> NoteDetailResponse:
        bridge = self._bridge
        raw_note = await bridge._get_note_item(note_key)
        return NoteDetailResponse(note=bridge._normalize_note_record(raw_note))

    async def update_note(
        self,
        *,
        note_key: str,
        payload: NoteWriteRequest,
    ) -> NoteWriteResponse:
        bridge = self._bridge
        bridge._validate_note_body(payload.bodyMarkdown)
        payload_fingerprint = bridge._note_write_payload_fingerprint(
            operation="update",
            payload=payload,
        )
        for _ in range(NOTE_UPDATE_MAX_ATTEMPTS):
            raw_note = await bridge._get_note_item(note_key)
            data = raw_note.get("data", {})
            existing_tags = bridge._normalize_tags(data.get("tags"))
            if payload.requestId:
                replay_state = bridge._request_replay_state(
                    tags=existing_tags,
                    request_id=payload.requestId,
                    payload_fingerprint=payload_fingerprint,
                )
                if replay_state == "matched":
                    return NoteWriteResponse(
                        status=NoteWriteStatus.UPDATED,
                        noteKey=note_key,
                        itemKey=bridge._clean_optional_str(data.get("parentItem")),
                    )
                if replay_state == "conflict":
                    bridge._raise_request_id_conflict()
            rendered_html = bridge._note_renderer.render_user_note(
                title=payload.title,
                body_markdown=payload.bodyMarkdown,
                mode=payload.mode.value,
                existing_html=str(data.get("note") or ""),
            )
            user_tags = payload.tags
            if user_tags is None:
                user_tags = bridge._mutable_note_tags(existing_tags)
            tags = bridge._merge_tags(
                bridge._identity_note_tags(existing_tags),
                bridge._request_metadata_tags(
                    request_id=payload.requestId,
                    payload_fingerprint=payload_fingerprint,
                ),
            )
            tags = bridge._merge_tags(tags, user_tags)
            try:
                await bridge._zotero.update_item(
                    item_key=note_key,
                    version=int(raw_note.get("version", 0)),
                    data={
                        "note": rendered_html,
                        "tags": [{"tag": tag} for tag in tags],
                    },
                )
            except BridgeError as exc:
                if exc.code == "WRITE_CONFLICT":
                    continue
                raise
            parent_item_key = bridge._clean_optional_str(data.get("parentItem"))
            if parent_item_key:
                await bridge._refresh_local_search_index_item(parent_item_key)
            bridge._invalidate_note_search_cache()
            return NoteWriteResponse(
                status=NoteWriteStatus.UPDATED,
                noteKey=note_key,
                itemKey=parent_item_key,
            )
        bridge._raise_item_update_conflict()
        raise AssertionError("unreachable")

    async def delete_note(self, *, note_key: str) -> NoteDeleteResponse:
        bridge = self._bridge
        raw_note = await bridge._get_note_item(note_key)
        data = raw_note.get("data", {})
        await bridge._zotero.delete_item(
            item_key=note_key,
            version=int(raw_note.get("version", 0)),
        )
        parent_item_key = bridge._clean_optional_str(data.get("parentItem"))
        if parent_item_key:
            await bridge._refresh_local_search_index_item(parent_item_key)
        bridge._invalidate_note_search_cache()
        return NoteDeleteResponse(
            status=NoteDeleteStatus.DELETED,
            noteKey=note_key,
            itemKey=parent_item_key,
        )

    async def upsert_ai_note(
        self,
        *,
        item_key: str,
        payload: UpsertAINoteRequest,
    ) -> UpsertAINoteResponse:
        bridge = self._bridge
        bridge._validate_note_body(payload.bodyMarkdown)

        await bridge._zotero.get_item(item_key)
        identity_tags = bridge._note_renderer.identity_tags(
            agent=payload.agent,
            note_type=payload.noteType,
            slot=payload.slot,
        )
        provenance = bridge._effective_provenance(payload)
        payload_fingerprint = bridge._ai_note_payload_fingerprint(payload)
        for _ in range(NOTE_UPDATE_MAX_ATTEMPTS):
            children = await bridge._zotero.get_children(item_key)
            existing_note = bridge._find_matching_note(children, identity_tags)
            replayed_note = bridge._find_note_in_children_by_request_id(
                children=children,
                request_id=payload.requestId,
            )
            if existing_note is None and replayed_note is not None and payload.requestId:
                replay_tags = bridge._assert_ai_note_replay_matches(
                    existing_note=replayed_note,
                    request_id=payload.requestId,
                    payload_fingerprint=payload_fingerprint,
                    identity_tags=identity_tags,
                )
                return UpsertAINoteResponse(
                    status=bridge._upsert_ai_note_replay_status(
                        tags=replay_tags,
                        request_id=payload.requestId,
                    ),
                    noteKey=str(replayed_note.get("key") or ""),
                    itemKey=item_key,
                    agent=payload.agent,
                    noteType=payload.noteType,
                    slot=payload.slot,
                    schemaVersion=self._note_schema_version(replayed_note),
                )

            existing_html = None
            if existing_note is not None:
                existing_tags = bridge._normalize_tags(existing_note.get("data", {}).get("tags"))
                if payload.requestId:
                    if (
                        replayed_note is not None
                        and replayed_note.get("key") != existing_note.get("key")
                    ):
                        bridge._raise_request_id_conflict()
                    replay_state = bridge._request_replay_state(
                        tags=existing_tags,
                        request_id=payload.requestId,
                        payload_fingerprint=payload_fingerprint,
                    )
                    if replay_state == "matched":
                        return UpsertAINoteResponse(
                            status=bridge._upsert_ai_note_replay_status(
                                tags=existing_tags,
                                request_id=payload.requestId,
                            ),
                            noteKey=str(existing_note.get("key") or ""),
                            itemKey=item_key,
                            agent=payload.agent,
                            noteType=payload.noteType,
                            slot=payload.slot,
                            schemaVersion=self._note_schema_version(existing_note),
                        )
                    if replay_state == "conflict":
                        bridge._raise_request_id_conflict()
                existing_html = str(existing_note.get("data", {}).get("note") or "")

            rendered_html = bridge._note_renderer.render(
                title=payload.title,
                body_markdown=payload.bodyMarkdown,
                agent=payload.agent,
                note_type=payload.noteType,
                model=payload.model,
                source_attachment_key=payload.sourceAttachmentKey,
                source_cursor_start=payload.sourceCursorStart,
                source_cursor_end=payload.sourceCursorEnd,
                schema_version=payload.schemaVersion,
                payload=payload.payload,
                provenance=provenance,
                mode=payload.mode.value,
                existing_html=existing_html,
            )
            request_tags = bridge._request_metadata_tags(
                request_id=payload.requestId,
                payload_fingerprint=payload_fingerprint,
                outcome=(
                    UpsertAINoteStatus.CREATED.value
                    if existing_note is None
                    else UpsertAINoteStatus.UPDATED.value
                ),
            )
            all_tags = bridge._merge_tags(identity_tags, request_tags)
            all_tags = bridge._merge_tags(all_tags, payload.tags)

            if existing_note is None:
                note_payload = {
                    "itemType": "note",
                    "parentItem": item_key,
                    "note": rendered_html,
                    "tags": [{"tag": tag} for tag in all_tags],
                }
                try:
                    note_key = (
                        await bridge._zotero.create_items(
                            [note_payload],
                            write_token=bridge._build_write_token(payload.requestId),
                        )
                    )[0]
                except BridgeError as exc:
                    if exc.code == "WRITE_CONFLICT" and payload.requestId:
                        replayed_note = await bridge._find_note_by_request_id(
                            item_key=item_key,
                            request_id=payload.requestId,
                        )
                        if replayed_note is not None:
                            replay_tags = bridge._assert_ai_note_replay_matches(
                                existing_note=replayed_note,
                                request_id=payload.requestId,
                                payload_fingerprint=payload_fingerprint,
                                identity_tags=identity_tags,
                            )
                            return UpsertAINoteResponse(
                                status=bridge._upsert_ai_note_replay_status(
                                    tags=replay_tags,
                                    request_id=payload.requestId,
                                ),
                                noteKey=str(replayed_note.get("key") or ""),
                                itemKey=item_key,
                                agent=payload.agent,
                                noteType=payload.noteType,
                                slot=payload.slot,
                                schemaVersion=self._note_schema_version(replayed_note),
                            )
                    raise
                effective_schema_version = self._rendered_schema_version(rendered_html)
                await bridge._refresh_local_search_index_item(item_key)
                bridge._invalidate_note_search_cache()
                return UpsertAINoteResponse(
                    status=UpsertAINoteStatus.CREATED,
                    noteKey=note_key,
                    itemKey=item_key,
                    agent=payload.agent,
                    noteType=payload.noteType,
                    slot=payload.slot,
                    schemaVersion=effective_schema_version,
                )

            note_key = str(existing_note.get("key"))
            try:
                await bridge._zotero.update_item(
                    item_key=note_key,
                    version=int(existing_note.get("version", 0)),
                    data={
                        "note": rendered_html,
                        "tags": [{"tag": tag} for tag in all_tags],
                    },
                )
            except BridgeError as exc:
                if exc.code == "WRITE_CONFLICT":
                    continue
                raise
            effective_schema_version = self._rendered_schema_version(rendered_html)
            await bridge._refresh_local_search_index_item(item_key)
            bridge._invalidate_note_search_cache()
            return UpsertAINoteResponse(
                status=UpsertAINoteStatus.UPDATED,
                noteKey=note_key,
                itemKey=item_key,
                agent=payload.agent,
                noteType=payload.noteType,
                slot=payload.slot,
                schemaVersion=effective_schema_version,
            )
        bridge._raise_item_update_conflict()
        raise AssertionError("unreachable")
