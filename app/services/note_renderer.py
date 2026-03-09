from __future__ import annotations

import re
from datetime import UTC, datetime
from html import escape, unescape

import bleach
from markdown_it import MarkdownIt


class NoteRenderer:
    def __init__(self, tag_prefix: str) -> None:
        self._tag_prefix = tag_prefix
        self._markdown = MarkdownIt("commonmark", {"breaks": True, "html": False})
        self._allowed_tags = sorted(
            set(bleach.sanitizer.ALLOWED_TAGS).union(
                {"p", "pre", "code", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "br"}
            )
        )
        self._allowed_attributes = {
            **bleach.sanitizer.ALLOWED_ATTRIBUTES,
            "a": ["href", "title", "rel", "target"],
        }

    def identity_tags(self, *, agent: str, note_type: str, slot: str) -> list[str]:
        return [
            self._tag_prefix,
            f"{self._tag_prefix}:agent:{agent}",
            f"{self._tag_prefix}:type:{note_type}",
            f"{self._tag_prefix}:slot:{slot}",
        ]

    def extract_identity(self, tags: list[str]) -> tuple[str, str, str] | None:
        tag_set = set(tags)
        if self._tag_prefix not in tag_set:
            return None

        agent = self._extract_tag_value(tags, f"{self._tag_prefix}:agent:")
        note_type = self._extract_tag_value(tags, f"{self._tag_prefix}:type:")
        slot = self._extract_tag_value(tags, f"{self._tag_prefix}:slot:")
        if agent and note_type and slot:
            return agent, note_type, slot
        return None

    def render(
        self,
        *,
        title: str | None,
        body_markdown: str,
        agent: str,
        note_type: str,
        model: str | None,
        source_attachment_key: str | None,
        source_cursor_start: int | None,
        source_cursor_end: int | None,
        mode: str,
        existing_html: str | None = None,
    ) -> str:
        section_html = self._render_section(
            title=title,
            body_markdown=body_markdown,
            agent=agent,
            note_type=note_type,
            model=model,
            source_attachment_key=source_attachment_key,
            source_cursor_start=source_cursor_start,
            source_cursor_end=source_cursor_end,
        )
        if mode == "append" and existing_html:
            combined = f"{existing_html}\n<hr />\n{section_html}"
        else:
            combined = section_html
        return self._sanitize(combined)

    def render_user_note(
        self,
        *,
        title: str | None,
        body_markdown: str,
        mode: str,
        existing_html: str | None = None,
    ) -> str:
        section_html = self._render_basic_section(
            title=title,
            body_markdown=body_markdown,
        )
        if mode == "append" and existing_html:
            combined = f"{existing_html}\n<hr />\n{section_html}"
        else:
            combined = section_html
        return self._sanitize(combined)

    def to_plain_text(self, html: str) -> str:
        normalized = re.sub(r"(?i)<br\s*/?>", "\n", html)
        normalized = re.sub(r"(?i)</(p|div|h[1-6]|li|pre|blockquote)>", "\n", normalized)
        normalized = re.sub(r"(?i)<hr\s*/?>", "\n---\n", normalized)
        cleaned = bleach.clean(normalized, tags=[], attributes={}, strip=True)
        text = unescape(cleaned).replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _render_section(
        self,
        *,
        title: str | None,
        body_markdown: str,
        agent: str,
        note_type: str,
        model: str | None,
        source_attachment_key: str | None,
        source_cursor_start: int | None,
        source_cursor_end: int | None,
    ) -> str:
        body_html = self._markdown.render(body_markdown)
        title_html = f"<h2>{escape(title)}</h2>\n" if title else ""

        provenance_parts = [
            f"Generated {datetime.now(UTC).replace(microsecond=0).isoformat()}",
            f"agent={escape(agent)}",
            f"type={escape(note_type)}",
        ]
        if model:
            provenance_parts.append(f"model={escape(model)}")
        if source_attachment_key:
            span = ""
            if source_cursor_start is not None or source_cursor_end is not None:
                start = "" if source_cursor_start is None else str(source_cursor_start)
                end = "" if source_cursor_end is None else str(source_cursor_end)
                span = f" ({start}-{end})"
            provenance_parts.append(f"attachment={escape(source_attachment_key)}{span}")

        provenance_html = (
            "<p><em>"
            + " | ".join(provenance_parts)
            + "</em></p>"
        )
        return f"{title_html}{body_html}\n{provenance_html}"

    def _render_basic_section(
        self,
        *,
        title: str | None,
        body_markdown: str,
    ) -> str:
        body_html = self._markdown.render(body_markdown)
        title_html = f"<h2>{escape(title)}</h2>\n" if title else ""
        return f"{title_html}{body_html}"

    def _sanitize(self, html: str) -> str:
        return bleach.clean(
            html,
            tags=self._allowed_tags,
            attributes=self._allowed_attributes,
            protocols=bleach.sanitizer.ALLOWED_PROTOCOLS,
            strip=True,
        )

    @staticmethod
    def _extract_tag_value(tags: list[str], prefix: str) -> str | None:
        for tag in tags:
            if tag.startswith(prefix):
                return tag[len(prefix) :]
        return None
