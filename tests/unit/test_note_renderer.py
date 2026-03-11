from __future__ import annotations

from app.models import ProvenanceRecord
from app.services.note_renderer import STRUCTURED_BLOCK_PATTERN, NoteRenderer


def test_identity_tags_and_extract() -> None:
    renderer = NoteRenderer("zbridge")
    tags = renderer.identity_tags(agent="codex", note_type="paper.summary", slot="default")
    assert tags == [
        "zbridge",
        "zbridge:agent:codex",
        "zbridge:type:paper.summary",
        "zbridge:slot:default",
    ]
    assert renderer.extract_identity(tags) == ("codex", "paper.summary", "default")


def test_render_and_parse_structured_note_round_trip() -> None:
    renderer = NoteRenderer("zbridge")
    html = renderer.render(
        title="Findings",
        body_markdown="Hello **world**",
        agent="codex",
        note_type="paper.findings",
        model="gpt-5",
        source_attachment_key="ATTACH1",
        source_cursor_start=10,
        source_cursor_end=90,
        schema_version="1.0",
        payload={"findings": ["A", "B"]},
        provenance=[ProvenanceRecord(attachmentKey="ATTACH1", page=5, locator="p.5")],
        mode="replace",
        existing_html=None,
    )

    parsed = renderer.parse(html)

    assert "<strong>world</strong>" in parsed.human_html
    assert parsed.schema_version == "1.0"
    assert parsed.payload == {"findings": ["A", "B"]}
    assert parsed.provenance[0].attachmentKey == "ATTACH1"
    assert parsed.provenance[0].page == 5
    assert renderer.to_plain_text(html).startswith("Findings\n\nHello world\n\nGenerated")


def test_replace_mode_does_not_duplicate_machine_block() -> None:
    renderer = NoteRenderer("zbridge")
    existing = renderer.render(
        title="First",
        body_markdown="Alpha",
        agent="codex",
        note_type="paper.summary",
        model=None,
        source_attachment_key=None,
        source_cursor_start=None,
        source_cursor_end=None,
        schema_version="1.0",
        payload={"summary": "Alpha"},
        provenance=[],
        mode="replace",
        existing_html=None,
    )

    updated = renderer.render(
        title="Second",
        body_markdown="Beta",
        agent="codex",
        note_type="paper.summary",
        model=None,
        source_attachment_key=None,
        source_cursor_start=None,
        source_cursor_end=None,
        schema_version="1.1",
        payload={"summary": "Beta"},
        provenance=[],
        mode="replace",
        existing_html=existing,
    )

    assert len(STRUCTURED_BLOCK_PATTERN.findall(updated)) == 1
    assert renderer.parse(updated).payload == {"summary": "Beta"}


def test_append_mode_preserves_visible_content_and_single_machine_block() -> None:
    renderer = NoteRenderer("zbridge")
    existing = renderer.render(
        title="Methods",
        body_markdown="Step one",
        agent="codex",
        note_type="paper.methods",
        model=None,
        source_attachment_key=None,
        source_cursor_start=None,
        source_cursor_end=None,
        schema_version="1.0",
        payload={"methods": ["step one"]},
        provenance=[],
        mode="replace",
        existing_html=None,
    )

    appended = renderer.render(
        title="Methods",
        body_markdown="Step two",
        agent="codex",
        note_type="paper.methods",
        model=None,
        source_attachment_key=None,
        source_cursor_start=None,
        source_cursor_end=None,
        schema_version="1.1",
        payload={"methods": ["step two"]},
        provenance=[],
        mode="append",
        existing_html=existing,
    )

    plain_text = renderer.to_plain_text(appended)
    assert "Step one" in plain_text
    assert "Step two" in plain_text
    assert len(STRUCTURED_BLOCK_PATTERN.findall(appended)) == 1
    assert renderer.parse(appended).schema_version == "1.1"


def test_append_mode_preserves_existing_structured_payload_when_not_reprovided() -> None:
    renderer = NoteRenderer("zbridge")
    existing = renderer.render(
        title="Findings",
        body_markdown="Initial summary",
        agent="codex",
        note_type="paper.findings",
        model=None,
        source_attachment_key=None,
        source_cursor_start=None,
        source_cursor_end=None,
        schema_version="1.0",
        payload={"findings": ["A"]},
        provenance=[ProvenanceRecord(attachmentKey="ATTACH1", page=2)],
        mode="replace",
        existing_html=None,
    )

    appended = renderer.render(
        title="Findings",
        body_markdown="Follow-up note",
        agent="codex",
        note_type="paper.findings",
        model=None,
        source_attachment_key=None,
        source_cursor_start=None,
        source_cursor_end=None,
        schema_version=None,
        payload=None,
        provenance=[],
        mode="append",
        existing_html=existing,
    )

    parsed = renderer.parse(appended)
    assert "Initial summary" in renderer.to_plain_text(appended)
    assert "Follow-up note" in renderer.to_plain_text(appended)
    assert parsed.schema_version == "1.0"
    assert parsed.payload == {"findings": ["A"]}
    assert parsed.provenance == [ProvenanceRecord(attachmentKey="ATTACH1", page=2)]
