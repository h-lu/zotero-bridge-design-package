from __future__ import annotations

from app.services.note_renderer import NoteRenderer


def test_identity_tags_and_extract() -> None:
    renderer = NoteRenderer("zbridge")
    tags = renderer.identity_tags(agent="codex", note_type="summary", slot="default")
    assert tags == [
        "zbridge",
        "zbridge:agent:codex",
        "zbridge:type:summary",
        "zbridge:slot:default",
    ]
    assert renderer.extract_identity(tags) == ("codex", "summary", "default")


def test_render_sanitizes_html() -> None:
    renderer = NoteRenderer("zbridge")
    html = renderer.render(
        title="Summary",
        body_markdown="Hello <script>alert(1)</script> **world**",
        agent="codex",
        note_type="summary",
        model="gpt-5",
        source_attachment_key="ATTACH1",
        source_cursor_start=0,
        source_cursor_end=100,
        mode="replace",
        existing_html=None,
    )

    assert "<script>" not in html
    assert "<strong>world</strong>" in html
    assert "attachment=ATTACH1" in html


def test_render_user_note_and_plain_text() -> None:
    renderer = NoteRenderer("zbridge")
    html = renderer.render_user_note(
        title="Manual Note",
        body_markdown="Line one\n\nLine two with **bold**",
        mode="replace",
    )

    assert "<h2>Manual Note</h2>" in html
    assert "<strong>bold</strong>" in html
    assert renderer.to_plain_text(html) == "Manual Note\n\nLine one\n\nLine two with bold"
