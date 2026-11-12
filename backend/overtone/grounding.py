"""Turning sources into numbered passages, and passage numbers back into citations.

Grounding is provider-agnostic on purpose: any chat model that Agent Framework
can drive sees the same numbered passages and cites them the same way, so
citations work whether the engine runs on Claude, Azure OpenAI or anything else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .errors import OvertoneError
from .models import Citation

# Rough guard so an oversized notebook fails with a clear message instead of an
# opaque API error. A 1M-token context is roughly 3M+ characters of English prose;
# leave headroom for passage numbering, the conversation and the answer.
MAX_SOURCE_CHARS = 2_400_000

_TARGET = 700  # preferred passage size in characters
_MAX = 900  # paragraphs longer than this are split at sentence boundaries
_MIN = 280  # paragraphs shorter than this are merged with their neighbours


@dataclass
class SourceDoc:
    """A source as the agents see it."""

    id: str
    title: str
    text: str


@dataclass
class Passage:
    number: int
    source_id: str
    source_title: str
    start: int
    end: int
    text: str  # exact slice of the source text


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pos = 0
    for gap in re.finditer(r"\n\s*\n", text):
        spans.append((pos, gap.start()))
        pos = gap.end()
    spans.append((pos, len(text)))
    trimmed: list[tuple[int, int]] = []
    for start, end in spans:
        chunk = text[start:end]
        lead = len(chunk) - len(chunk.lstrip())
        tail = len(chunk) - len(chunk.rstrip())
        if end - tail > start + lead:
            trimmed.append((start + lead, end - tail))
    return trimmed


def _split_long(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Split one long paragraph at sentence boundaries (then whitespace) into pieces near the target size."""
    breaks = [start + m.end() for m in re.finditer(r"(?<=[.!?])\s+", text[start:end])]
    pieces: list[tuple[int, int]] = []
    piece_start = start
    last_break: int | None = None
    for b in [*breaks, end]:
        if b - piece_start > _TARGET and last_break is not None and last_break > piece_start:
            pieces.append((piece_start, last_break))
            piece_start = last_break
        last_break = b
    pieces.append((piece_start, end))

    result: list[tuple[int, int]] = []
    for s, e in pieces:
        # A single enormous "sentence" (tables, code, text without punctuation).
        while e - s > _MAX:
            cut = text.rfind(" ", s + _MIN, s + _TARGET)
            cut = cut if cut > s else s + _TARGET
            result.append((s, cut))
            s = cut
        result.append((s, e))
    return [(s, e) for s, e in result if text[s:e].strip()]


def split_passages(docs: list[SourceDoc]) -> list[Passage]:
    """Number every passage across all sources, 1-based, in reading order."""
    passages: list[Passage] = []
    for doc in docs:
        spans: list[tuple[int, int]] = []
        for start, end in _paragraph_spans(doc.text):
            if end - start > _MAX:
                spans.extend(_split_long(doc.text, start, end))
            elif spans and (spans[-1][1] - spans[-1][0]) < _MIN and (end - spans[-1][0]) <= _TARGET:
                spans[-1] = (spans[-1][0], end)  # merge short paragraphs (headings, list items)
            else:
                spans.append((start, end))
        for start, end in spans:
            chunk = doc.text[start:end]
            lead = len(chunk) - len(chunk.lstrip())
            tail = len(chunk) - len(chunk.rstrip())
            if end - tail <= start + lead:
                continue
            passages.append(
                Passage(
                    number=len(passages) + 1,
                    source_id=doc.id,
                    source_title=doc.title,
                    start=start + lead,
                    end=end - tail,
                    text=doc.text[start + lead : end - tail],
                )
            )
    return passages


def usable_docs(docs: list[SourceDoc]) -> list[SourceDoc]:
    """Drop empty sources and fail clearly when nothing (or too much) is left."""
    usable = [d for d in docs if d.text.strip()]
    if not usable:
        raise OvertoneError("Add at least one source with readable text first.", status=400, code="no_sources")
    total = sum(len(d.text) for d in usable)
    if total > MAX_SOURCE_CHARS:
        raise OvertoneError(
            f"These sources total {total:,} characters, which is more than fits in one request. "
            "Select fewer sources and try again.",
            status=413,
            code="sources_too_large",
        )
    return usable


def _attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def render_sources(docs: list[SourceDoc], passages: list[Passage]) -> str:
    """The grounding block that opens every conversation with an agent."""
    by_source: dict[str, list[Passage]] = {}
    for p in passages:
        by_source.setdefault(p.source_id, []).append(p)
    blocks: list[str] = []
    for doc in docs:
        lines = [f"[{p.number}] " + " ".join(p.text.split()) for p in by_source.get(doc.id, [])]
        blocks.append(f'<source title="{_attr(doc.title)}">\n' + "\n".join(lines) + "\n</source>")
    return "<sources>\n" + "\n\n".join(blocks) + "\n</sources>"


_MARKER = re.compile(r" ?\[\s*[0-9]+(?:[,\s]+[0-9]+)*\s*\]")


def strip_markers(text: str) -> str:
    """Remove `[n]` citation markers (used before replaying history, and before speaking)."""
    return _MARKER.sub("", text)


class CitationStream:
    """Rewrites passage numbers in a streamed answer into message-local citation indices.

    The model writes `[212]` (a passage number). The reader should see `[1]`, `[2]`, …
    in order of first appearance, each backed by a `Citation` carrying the passage.
    `feed` accepts text deltas of any shape (a marker may be split across deltas) and
    returns chat events: `{"type": "text", "delta": …}` and `{"type": "citation", "citation": …}`.
    """

    _MAX_PENDING = 48

    def __init__(self, passages: list[Passage]) -> None:
        self._passages = {p.number: p for p in passages}
        self._index: dict[int, int] = {}
        self._pending = ""
        self.citations: list[Citation] = []

    def feed(self, delta: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        out: list[str] = []

        def flush_text() -> None:
            if out:
                events.append({"type": "text", "delta": "".join(out)})
                out.clear()

        for ch in delta:
            if not self._pending:
                if ch == "[":
                    self._pending = "["
                else:
                    out.append(ch)
                continue
            if ch == "]":
                body = self._pending[1:]
                self._pending = ""
                numbers = [t for t in re.split(r"[,\s]+", body) if t]
                if numbers and all(t.isascii() and t.isdigit() for t in numbers):
                    for token in numbers:
                        passage = self._passages.get(int(token))
                        if passage is None:
                            continue  # a number the model made up: drop it rather than show a dead marker
                        index = self._index.get(passage.number)
                        if index is None:
                            index = len(self._index) + 1
                            self._index[passage.number] = index
                            citation = Citation(
                                index=index,
                                source_id=passage.source_id,
                                source_title=passage.source_title,
                                quote=passage.text,
                                start_char=passage.start,
                                end_char=passage.end,
                            )
                            self.citations.append(citation)
                            flush_text()
                            events.append({"type": "citation", "citation": citation.to_json()})
                        out.append(f"[{index}]")
                else:
                    out.append(f"[{body}]")  # ordinary bracketed text, e.g. a Markdown link label
            elif ch == "[":
                out.append(self._pending)
                self._pending = "["
            elif ch in "0123456789, " and len(self._pending) < self._MAX_PENDING:
                self._pending += ch
            else:
                out.append(self._pending + ch)
                self._pending = ""
        flush_text()
        return events

    def flush(self) -> list[dict[str, Any]]:
        """Call when the stream ends: releases any half-open bracket as plain text."""
        if not self._pending:
            return []
        text, self._pending = self._pending, ""
        return [{"type": "text", "delta": text}]
