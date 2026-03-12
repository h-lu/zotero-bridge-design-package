from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape, unescape
from typing import Any

import bleach
from markdown_it import MarkdownIt

from app.models import ProvenanceRecord

STRUCTURED_BLOCK_MARKER = "zbridge:structured:v1:"
STRUCTURED_BLOCK_PATTERN = re.compile(
    rf"<!--\s*{re.escape(STRUCTURED_BLOCK_MARKER)}(?P<payload>[A-Za-z0-9_-]+)\s*-->",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ParsedStructuredNote:
    human_html: str
    schema_version: str | None
    payload: dict[str, Any] | None
    provenance: list[ProvenanceRecord]


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
        schema_version: str | None,
        payload: dict[str, Any] | None,
        provenance: list[ProvenanceRecord],
        mode: str,
        existing_html: str | None = None,
    ) -> str:
        existing_parsed = self.parse(existing_html or "")
        effective_schema_version = schema_version
        effective_payload = payload
        effective_provenance = provenance
        if mode == "append":
            if effective_schema_version is None:
                effective_schema_version = existing_parsed.schema_version
            if effective_payload is None:
                effective_payload = existing_parsed.payload
            if not effective_provenance:
                effective_provenance = existing_parsed.provenance
        section_html = self._render_section(
            title=title,
            body_markdown=body_markdown,
            agent=agent,
            note_type=note_type,
            model=model,
            source_attachment_key=source_attachment_key,
            source_cursor_start=source_cursor_start,
            source_cursor_end=source_cursor_end,
            provenance=effective_provenance,
        )
        if mode == "append" and existing_parsed.human_html:
            human_html = f"{existing_parsed.human_html}\n<hr />\n{section_html}"
        else:
            human_html = section_html
        sanitized_human_html = self._sanitize(human_html)
        machine_block = self._render_machine_block(
            schema_version=effective_schema_version,
            payload=effective_payload,
            provenance=effective_provenance,
        )
        if machine_block:
            return f"{sanitized_human_html}\n{machine_block}"
        return sanitized_human_html

    def render_user_note(
        self,
        *,
        title: str | None,
        body_markdown: str,
        mode: str,
        existing_html: str | None = None,
    ) -> str:
        existing_parsed = self.parse(existing_html or "")
        section_html = self._render_basic_section(
            title=title,
            body_markdown=body_markdown,
        )
        if mode == "append" and existing_parsed.human_html:
            combined = f"{existing_parsed.human_html}\n<hr />\n{section_html}"
        else:
            combined = section_html
        return self._sanitize(combined)

    def parse(self, html: str) -> ParsedStructuredNote:
        normalized_html = html or ""
        matches = list(STRUCTURED_BLOCK_PATTERN.finditer(normalized_html))
        if not matches:
            return ParsedStructuredNote(
                human_html=normalized_html.strip(),
                schema_version=None,
                payload=None,
                provenance=[],
            )
        last_match = matches[-1]
        decoded = self._decode_machine_payload(last_match.group("payload"))
        human_html = STRUCTURED_BLOCK_PATTERN.sub("", normalized_html).strip()
        return ParsedStructuredNote(
            human_html=human_html,
            schema_version=self._clean_optional_str(decoded.get("schemaVersion")),
            payload=self._coerce_payload(decoded.get("payload")),
            provenance=self._coerce_provenance(decoded.get("provenance")),
        )

    def structured_payload_text(self, payload: dict[str, Any] | None) -> str:
        if payload is None:
            return ""
        parts: list[str] = []
        self._collect_text_parts(payload, parts)
        return "\n".join(part for part in parts if part)

    def to_plain_text(self, html: str) -> str:
        stripped_comments = STRUCTURED_BLOCK_PATTERN.sub("", html)
        normalized = re.sub(r"(?i)<br\s*/?>", "\n", stripped_comments)
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
        provenance: list[ProvenanceRecord],
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
        elif provenance:
            first = provenance[0]
            if first.attachmentKey:
                locator = f" {escape(first.locator)}" if first.locator else ""
                provenance_parts.append(f"attachment={escape(first.attachmentKey)}{locator}")

        provenance_html = "<p><em>" + " | ".join(provenance_parts) + "</em></p>"
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
            strip_comments=False,
        )

    def _render_machine_block(
        self,
        *,
        schema_version: str | None,
        payload: dict[str, Any] | None,
        provenance: list[ProvenanceRecord],
    ) -> str:
        if schema_version is None and payload is None and not provenance:
            return ""
        record = {
            "schemaVersion": schema_version,
            "payload": payload,
            "provenance": [entry.model_dump(exclude_none=True) for entry in provenance],
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )
        ).decode("ascii")
        return f"<!-- {STRUCTURED_BLOCK_MARKER}{encoded.rstrip('=')} -->"

    def _decode_machine_payload(self, encoded: str) -> dict[str, Any]:
        try:
            padding = "=" * (-len(encoded) % 4)
            payload = base64.urlsafe_b64decode(f"{encoded}{padding}".encode("ascii"))
            decoded = json.loads(payload.decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}

    @staticmethod
    def _coerce_payload(value: Any) -> dict[str, Any] | None:
        return value if isinstance(value, dict) else None

    @staticmethod
    def _coerce_provenance(value: Any) -> list[ProvenanceRecord]:
        if not isinstance(value, list):
            return []
        provenance: list[ProvenanceRecord] = []
        for entry in value:
            if not isinstance(entry, dict):
                continue
            try:
                provenance.append(ProvenanceRecord.model_validate(entry))
            except Exception:
                continue
        return provenance

    def _collect_text_parts(self, value: Any, parts: list[str]) -> None:
        if value is None:
            return
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned:
                parts.append(cleaned)
            return
        if isinstance(value, (int, float, bool)):
            parts.append(str(value))
            return
        if isinstance(value, list):
            for item in value:
                self._collect_text_parts(item, parts)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str) and key.strip():
                    parts.append(key.strip())
                self._collect_text_parts(item, parts)

    @staticmethod
    def _extract_tag_value(tags: list[str], prefix: str) -> str | None:
        for tag in tags:
            if tag.startswith(prefix):
                return tag[len(prefix) :]
        return None

    @staticmethod
    def _clean_optional_str(value: Any) -> str | None:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None
