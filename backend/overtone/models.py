"""Shared data contracts for the Overtone engine.

These mirror the TypeScript client types (`packages/client/src/types.ts`) field
for field. Python code uses snake_case attributes; JSON (the REST API and the
files on disk) uses camelCase, handled by the alias generator on `Model`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

SourceKind = Literal["file", "url", "text"]
DocumentKind = Literal["summary", "briefing", "study_guide", "faq", "timeline", "custom"]
ArtifactKind = Literal["audio_overview", "summary", "briefing", "study_guide", "faq", "timeline", "custom"]
AudioFormat = Literal["deep_dive", "brief", "debate", "critique", "solo"]
AudioLength = Literal["short", "medium", "long"]
ArtifactStatus = Literal["queued", "generating", "ready", "error"]
LiveMode = Literal["conversation", "podcast"]

DOCUMENT_KINDS: tuple[str, ...] = ("summary", "briefing", "study_guide", "faq", "timeline", "custom")

ARTIFACT_LABEL: dict[str, str] = {
    "audio_overview": "Audio overview",
    "summary": "Summary",
    "briefing": "Briefing",
    "study_guide": "Study guide",
    "faq": "FAQ",
    "timeline": "Timeline",
    "custom": "Custom note",
}


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string with millisecond precision and a Z suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Model(BaseModel):
    """Base model: snake_case in Python, camelCase in JSON."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    def to_json(self) -> dict[str, Any]:
        """The camelCase dict that goes over the wire and onto disk. None fields are omitted."""
        return self.model_dump(by_alias=True, exclude_none=True)


# ───────────────────────────── Notebooks ─────────────────────────────


class NotebookGuide(Model):
    summary: str
    questions: list[str]
    generated_at: str


class Notebook(Model):
    id: str
    title: str
    description: str | None = None
    icon: str | None = None
    created_at: str
    updated_at: str
    # Filled in by list/get; never persisted.
    source_count: int | None = None
    # Cached guide; cleared whenever the sources change.
    guide: NotebookGuide | None = None


# ────────────────────────────── Sources ──────────────────────────────


class Source(Model):
    id: str
    notebook_id: str
    kind: SourceKind
    title: str
    origin: str | None = None
    mime_type: str | None = None
    char_count: int
    excerpt: str
    created_at: str


@dataclass
class TextInput:
    text: str
    title: str | None = None


@dataclass
class UrlInput:
    url: str
    title: str | None = None


@dataclass
class FileInput:
    filename: str
    data: bytes
    mime_type: str | None = None
    title: str | None = None


SourceInput = TextInput | UrlInput | FileInput


@dataclass
class ExtractedSource:
    """Result of turning a SourceInput into plain text (see ingest.py)."""

    kind: SourceKind
    title: str
    text: str
    origin: str | None = None
    mime_type: str | None = None


# ─────────────────────────────── Chat ────────────────────────────────


class Citation(Model):
    # 1-based; matches the `[n]` marker embedded in the message text.
    index: int
    source_id: str
    source_title: str
    # The exact passage from the source that supports the claim.
    quote: str
    start_char: int | None = None
    end_char: int | None = None


class ChatMessage(Model):
    id: str
    notebook_id: str
    role: Literal["user", "assistant"]
    # Markdown. Assistant messages contain `[n]` citation markers.
    content: str
    citations: list[Citation] | None = None
    created_at: str


class ChatInput(Model):
    message: str
    source_ids: list[str] | None = None


# Chat stream events are plain dicts so they serialise straight to SSE:
#   {"type": "text", "delta": str}
#   {"type": "citation", "citation": <Citation JSON>}
#   {"type": "done", "message": <ChatMessage JSON>}
#   {"type": "error", "error": str}
ChatEvent = dict[str, Any]


# ───────────────────────────── Artifacts ─────────────────────────────


class AudioHost(Model):
    name: str = ""
    voice: str | None = None


class AudioOptions(Model):
    format: AudioFormat | None = None
    length: AudioLength | None = None
    instructions: str | None = None
    hosts: list[AudioHost] | None = None
    language: str | None = None


class DialogueTurn(Model):
    # Index into AudioScript.hosts.
    speaker: int
    text: str


class AudioScript(Model):
    title: str
    summary: str
    hosts: list[AudioHost]
    turns: list[DialogueTurn]


class ArtifactAudio(Model):
    mime_type: str
    blob_key: str
    bytes: int
    provider: str


class ArtifactRequest(Model):
    """What to generate. `options` applies to audio overviews, `instructions` to documents."""

    kind: ArtifactKind
    source_ids: list[str] | None = None
    options: AudioOptions | None = None
    instructions: str | None = None


class Artifact(Model):
    id: str
    notebook_id: str
    kind: ArtifactKind
    title: str
    status: ArtifactStatus
    progress: str | None = None
    error: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    request: ArtifactRequest
    content: str | None = None
    script: AudioScript | None = None
    audio: ArtifactAudio | None = None
    audio_error: str | None = None
    created_at: str
    updated_at: str

    def to_json(self) -> dict[str, Any]:
        data = super().to_json()
        # Clients distinguish "audio": null (finished, but no rendered file: play the
        # script locally) from a missing key (not finished yet).
        if self.kind == "audio_overview" and self.status == "ready" and self.audio is None:
            data["audio"] = None
        return data


# ──────────────────────────── Capabilities ───────────────────────────


class TtsVoice(Model):
    id: str
    label: str


class TtsCapabilities(Model):
    provider: str
    label: str
    voices: list[TtsVoice]
    default_voices: list[str]


class LiveCapabilities(Model):
    """Present when a live voice provider (OpenAI GPT-Live) is configured."""

    model: str
    voices: list[str]
    default_voice: str


class Capabilities(Model):
    name: Literal["overtone"] = "overtone"
    version: str
    # Model id used for chat and generation.
    model: str
    # Agent runtime the engine is built on.
    framework: str = "microsoft-agent-framework"
    tts: TtsCapabilities | None = None
    live: LiveCapabilities | None = None

    def to_json(self) -> dict[str, Any]:
        data = super().to_json()
        data.setdefault("tts", None)
        data.setdefault("live", None)
        return data


# ─────────────────────────────── TTS ─────────────────────────────────


@dataclass
class TtsConfig:
    # "auto" picks from whichever API key is present in the environment.
    provider: str | None = None  # auto | openai | elevenlabs | none
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None
    voices: list[str] | None = None


@runtime_checkable
class TtsProvider(Protocol):
    id: str
    label: str
    voices: list[TtsVoice]
    # Voice ids for host 0, host 1, … when a host has no explicit voice.
    default_voices: list[str]
    mime_type: str
    # Largest text accepted per request.
    max_chars: int

    async def synthesize(self, text: str, voice: str) -> bytes:
        """Render one utterance to encoded audio bytes."""
        ...


# ────────────────────────────── Storage ──────────────────────────────


@runtime_checkable
class Storage(Protocol):
    """Pluggable persistence. Documents are camelCase JSON dicts with an "id" key.

    Collections used by the engine: "notebooks", "sources", "messages",
    "artifacts". Blob keys look like "sources/<id>.txt" and "audio/<id>.mp3".
    """

    async def get(self, collection: str, id: str) -> dict[str, Any] | None: ...

    async def list(self, collection: str) -> list[dict[str, Any]]: ...

    async def put(self, collection: str, item: dict[str, Any]) -> None: ...

    async def delete(self, collection: str, id: str) -> None: ...

    async def put_blob(self, key: str, data: bytes) -> None: ...

    async def get_blob(self, key: str) -> bytes | None: ...

    async def delete_blob(self, key: str) -> None: ...


# ─────────────────────────────── Live ────────────────────────────────


class LiveSessionRequest(Model):
    """Browser → server: start a GPT-Live voice session over WebRTC."""

    # The browser's SDP offer.
    sdp: str
    mode: LiveMode = "conversation"
    source_ids: list[str] | None = None
    # Podcast mode: the audio overview being listened to, and where the listener paused.
    artifact_id: str | None = None
    turn_index: int | None = None
    voice: str | None = None


class LiveSession(Model):
    session_id: str
    # The SDP answer to hand to setRemoteDescription.
    sdp: str
    mode: LiveMode
    model: str
    voice: str


class LiveTranscriptLine(Model):
    role: Literal["user", "assistant"]
    text: str


class LiveDelegateRequest(Model):
    """Browser → server: GPT-Live delegated a question; answer it from the sources."""

    mode: LiveMode = "conversation"
    # Recent conversation, oldest first. The last user line is the question.
    transcript: list[LiveTranscriptLine]
    source_ids: list[str] | None = None
    artifact_id: str | None = None


class LiveDelegateResult(Model):
    # Short, speakable answer for the voice model to relay.
    answer: str
    citations: list[Citation] = Field(default_factory=list)
