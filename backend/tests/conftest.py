"""Shared fixtures for the Overtone core tests.

Everything here is local and deterministic: no network, no real model, no
environment variables are changed globally (use monkeypatch in the tests).
"""

from __future__ import annotations

import asyncio

import pytest

from overtone import MemoryStorage, MockChatClient, create_engine
from overtone.grounding import SourceDoc
from overtone.models import TtsVoice


class FakeTts:
    """A text-to-speech provider that returns recognisable bytes: b"<voice|text>"."""

    id = "fake"
    label = "Fake speech"
    voices = [TtsVoice(id="v1", label="Voice one"), TtsVoice(id="v2", label="Voice two")]
    default_voices = ["v1", "v2"]
    mime_type = "audio/mpeg"
    max_chars = 4096

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def synthesize(self, text: str, voice: str) -> bytes:
        self.calls.append((text, voice))
        # Finish requests out of order so the tests prove the output keeps script order.
        await asyncio.sleep(0.001 * (len(text) % 3))
        if self.error is not None:
            raise self.error
        return f"<{voice}|{text}>".encode("utf-8")


def _paragraph(topic: str) -> str:
    return (f"{topic} is covered in detail here, with enough words to stand as its own passage. " * 5).strip()


@pytest.fixture
def fake_tts() -> FakeTts:
    return FakeTts()


@pytest.fixture
def sample_docs() -> list[SourceDoc]:
    """Two sources, five passages in all, each paragraph long enough to stay its own passage."""
    report = "\n\n".join([_paragraph("Revenue"), _paragraph("Hiring"), _paragraph("Costs")])
    memo = "\n\n".join([_paragraph("Launch plan"), _paragraph("Risks")])
    return [SourceDoc("src-report", "Annual report", report), SourceDoc("src-memo", "Board memo", memo)]


@pytest.fixture
def engine():
    """An engine on in-memory storage and the deterministic mock model, with no TTS and no live voice."""
    return create_engine(
        storage=MemoryStorage(),
        chat_client=MockChatClient(delay=0),
        tts=None,
        live_voice=None,
    )
