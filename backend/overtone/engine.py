"""The headless engine. The REST API, the CLI and your own code all go through this.

    from overtone import create_engine, UrlInput, ArtifactRequest

    engine = create_engine()
    result = await engine.one_shot(
        [UrlInput("https://example.com/report")],
        ArtifactRequest(kind="audio_overview"),
    )
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import __version__
from .agents import ModelRuntime, create_runtime, stream_answer, voice_answer, write_guide
from .errors import OvertoneError
from .grounding import SourceDoc
from .ingest import extract_source
from .live import LIVE_VOICES, LiveVoice, create_live_voice
from .models import (
    ARTIFACT_LABEL,
    Artifact,
    ArtifactAudio,
    ArtifactRequest,
    AudioScript,
    Capabilities,
    ChatMessage,
    Citation,
    LiveCapabilities,
    LiveDelegateRequest,
    LiveDelegateResult,
    LiveSession,
    LiveSessionRequest,
    Notebook,
    NotebookGuide,
    Source,
    SourceInput,
    Storage,
    TtsCapabilities,
    TtsConfig,
    TtsProvider,
    now_iso,
)
from .prompts import live_conversation_instructions, live_podcast_instructions
from .storage import FileStorage
from .tts import create_tts_provider
from .workflows import JobHooks, run_generation

NOTEBOOKS, SOURCES, MESSAGES, ARTIFACTS = "notebooks", "sources", "messages", "artifacts"

ProgressCallback = Callable[[Artifact], None]

_UNSET: Any = object()


def _not_found(what: str, id: str) -> OvertoneError:
    return OvertoneError(f"{what} {id} not found.", status=404, code="not_found")


def _stamp_after(previous: str | None) -> str:
    """A timestamp for a new chat message that always sorts after `previous`.

    History is ordered by timestamp at millisecond resolution, so two messages written within the
    same millisecond (a fast model, a fast machine) would otherwise tie or sort out of order.
    """
    now = datetime.now(timezone.utc)
    if previous:
        now = max(now, datetime.fromisoformat(previous.replace("Z", "+00:00")) + timedelta(milliseconds=1))
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class OneShotResult:
    notebook: Notebook
    sources: list[Source]
    artifact: Artifact
    # (audio bytes, mime type) when a TTS provider rendered the episode.
    audio: tuple[bytes, str] | None


class Engine:
    """Notebook engine. Use `create_engine()` to build one."""

    def __init__(
        self,
        *,
        storage: Storage,
        runtime: ModelRuntime,
        tts: TtsProvider | None,
        live_voice: LiveVoice | None,
        on_error: Callable[[BaseException, str], None] | None = None,
    ) -> None:
        self.storage = storage
        self.runtime = runtime
        self.tts = tts
        self.live_voice = live_voice
        self._on_error = on_error or (lambda exc, context: print(f"[overtone] {context}: {exc!r}"))
        # Background generation jobs owned by this process, by artifact id.
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._listeners: dict[str, list[ProgressCallback]] = {}

        self.notebooks = _Notebooks(self)
        self.sources = _Sources(self)
        self.chat = _Chat(self)
        self.artifacts = _Artifacts(self)
        self.live = _Live(self)

    def capabilities(self) -> Capabilities:
        tts = self.tts
        live = self.live_voice
        return Capabilities(
            version=__version__,
            model=self.runtime.model,
            tts=TtsCapabilities(provider=tts.id, label=tts.label, voices=tts.voices, default_voices=tts.default_voices)
            if tts
            else None,
            live=LiveCapabilities(model=live.model, voices=list(LIVE_VOICES), default_voice=live.default_voice)
            if live
            else None,
        )

    async def one_shot(
        self,
        sources: list[SourceInput],
        request: ArtifactRequest | None = None,
        *,
        title: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> OneShotResult:
        """Sources in, finished artifact out, with no notebook bookkeeping for the caller."""
        if not sources:
            raise OvertoneError("Provide at least one source.", status=400, code="no_sources")
        notebook = await self.notebooks.create(title=title)
        added = [await self.sources.add(notebook.id, item) for item in sources]
        artifact = await self.artifacts.generate(
            notebook.id, request or ArtifactRequest(kind="audio_overview"), on_progress=on_progress
        )
        if not title:
            await self.notebooks.update(notebook.id, title=artifact.title)
        audio = await self.artifacts.get_audio(artifact.id)
        return OneShotResult(
            notebook=await self.notebooks.get(notebook.id), sources=added, artifact=artifact, audio=audio
        )

    # ── shared helpers ──────────────────────────────────────────────────────

    async def _notebook(self, id: str) -> Notebook:
        raw = await self.storage.get(NOTEBOOKS, id)
        if raw is None:
            raise _not_found("Notebook", id)
        return Notebook.model_validate(raw)

    async def _save_notebook(self, notebook: Notebook) -> None:
        data = notebook.to_json()
        data.pop("sourceCount", None)  # derived, never persisted
        await self.storage.put(NOTEBOOKS, data)

    async def _touch_notebook(self, id: str, *, sources_changed: bool = False) -> None:
        raw = await self.storage.get(NOTEBOOKS, id)
        if raw is None:
            return
        notebook = Notebook.model_validate(raw)
        notebook.updated_at = now_iso()
        if sources_changed:
            notebook.guide = None
        await self._save_notebook(notebook)

    async def _list_sources(self, notebook_id: str) -> list[Source]:
        items = [Source.model_validate(s) for s in await self.storage.list(SOURCES)]
        return sorted((s for s in items if s.notebook_id == notebook_id), key=lambda s: s.created_at)

    async def _source_text(self, id: str) -> str:
        blob = await self.storage.get_blob(f"sources/{id}.txt")
        return blob.decode("utf-8") if blob else ""

    async def _load_docs(self, notebook_id: str, source_ids: list[str] | None = None) -> list[SourceDoc]:
        """The sources a request is grounded on, with their text."""
        sources = await self._list_sources(notebook_id)
        if source_ids:
            wanted = set(source_ids)
            sources = [s for s in sources if s.id in wanted]
        if not sources:
            raise OvertoneError("Add at least one source to this notebook first.", status=400, code="no_sources")
        texts = await asyncio.gather(*(self._source_text(s.id) for s in sources))
        return [SourceDoc(id=s.id, title=s.title, text=t) for s, t in zip(sources, texts)]


class _Notebooks:
    def __init__(self, engine: Engine) -> None:
        self._e = engine

    async def list(self) -> list[Notebook]:
        e = self._e
        raw_notebooks, raw_sources = await asyncio.gather(e.storage.list(NOTEBOOKS), e.storage.list(SOURCES))
        counts: dict[str, int] = {}
        for s in raw_sources:
            counts[s.get("notebookId", "")] = counts.get(s.get("notebookId", ""), 0) + 1
        notebooks = [Notebook.model_validate(n) for n in raw_notebooks]
        for nb in notebooks:
            nb.source_count = counts.get(nb.id, 0)
        return sorted(notebooks, key=lambda n: n.updated_at, reverse=True)

    async def create(
        self, *, title: str | None = None, description: str | None = None, icon: str | None = None
    ) -> Notebook:
        ts = now_iso()
        notebook = Notebook(
            id=str(uuid.uuid4()),
            title=(title or "").strip() or "Untitled notebook",
            description=(description or "").strip() or None,
            icon=icon or None,
            created_at=ts,
            updated_at=ts,
        )
        await self._e._save_notebook(notebook)
        notebook.source_count = 0
        return notebook

    async def get(self, id: str) -> Notebook:
        notebook = await self._e._notebook(id)
        notebook.source_count = len(await self._e._list_sources(id))
        return notebook

    async def update(
        self, id: str, *, title: str | None = None, description: str | None = None, icon: str | None = None
    ) -> Notebook:
        """Change the given fields. Pass an empty string to clear `description` or `icon`."""
        notebook = await self._e._notebook(id)
        if title is not None:
            notebook.title = title.strip() or notebook.title
        if description is not None:
            notebook.description = description.strip() or None
        if icon is not None:
            notebook.icon = icon or None
        notebook.updated_at = now_iso()
        await self._e._save_notebook(notebook)
        notebook.source_count = len(await self._e._list_sources(id))
        return notebook

    async def delete(self, id: str) -> None:
        e = self._e
        await e._notebook(id)
        sources = await e._list_sources(id)
        messages = [m for m in await e.storage.list(MESSAGES) if m.get("notebookId") == id]
        artifacts = [a for a in await e.storage.list(ARTIFACTS) if a.get("notebookId") == id]
        for a in artifacts:
            job = e._jobs.get(a["id"])
            if job:
                job.cancel()
        work: list[Awaitable[None]] = []
        for s in sources:
            work += [e.storage.delete(SOURCES, s.id), e.storage.delete_blob(f"sources/{s.id}.txt")]
        work += [e.storage.delete(MESSAGES, m["id"]) for m in messages]
        for a in artifacts:
            work.append(e.storage.delete(ARTIFACTS, a["id"]))
            if a.get("audio"):
                work.append(e.storage.delete_blob(a["audio"]["blobKey"]))
        await asyncio.gather(*work)
        await e.storage.delete(NOTEBOOKS, id)

    async def guide(self, id: str, *, refresh: bool = False) -> NotebookGuide:
        """Summary plus suggested questions. Cached until the sources change."""
        e = self._e
        notebook = await e._notebook(id)
        if notebook.guide and not refresh:
            return notebook.guide
        docs = await e._load_docs(id)
        summary, questions = await write_guide(e.runtime, docs)
        guide = NotebookGuide(summary=summary, questions=questions, generated_at=now_iso())
        # Re-read so a concurrent rename isn't overwritten, and don't cache a guide
        # for a source list that changed while it was being written.
        fresh = await e._notebook(id)
        current = {s.id for s in await e._list_sources(id)}
        if current == {d.id for d in docs}:
            fresh.guide = guide
            await e._save_notebook(fresh)
        return guide


class _Sources:
    def __init__(self, engine: Engine) -> None:
        self._e = engine

    async def list(self, notebook_id: str) -> list[Source]:
        return await self._e._list_sources(notebook_id)

    async def add(self, notebook_id: str, input: SourceInput) -> Source:
        e = self._e
        await e._notebook(notebook_id)
        extracted = await extract_source(input)
        text = extracted.text.strip()
        if not text:
            raise OvertoneError(
                f'No readable text was found in "{extracted.title}". '
                "Scanned or image-only documents are not supported yet.",
                status=422,
                code="empty_source",
            )
        given_title = (getattr(input, "title", None) or "").strip()
        source = Source(
            id=str(uuid.uuid4()),
            notebook_id=notebook_id,
            kind=extracted.kind,
            title=(given_title or extracted.title)[:200],
            origin=extracted.origin,
            mime_type=extracted.mime_type,
            char_count=len(text),
            excerpt=" ".join(text[:280].split()),
            created_at=now_iso(),
        )
        await e.storage.put_blob(f"sources/{source.id}.txt", text.encode("utf-8"))
        await e.storage.put(SOURCES, source.to_json())
        await e._touch_notebook(notebook_id, sources_changed=True)
        return source

    async def get(self, id: str) -> Source:
        raw = await self._e.storage.get(SOURCES, id)
        if raw is None:
            raise _not_found("Source", id)
        return Source.model_validate(raw)

    async def get_text(self, id: str) -> str:
        await self.get(id)
        return await self._e._source_text(id)

    async def delete(self, id: str) -> None:
        source = await self.get(id)
        await self._e.storage.delete(SOURCES, id)
        await self._e.storage.delete_blob(f"sources/{id}.txt")
        await self._e._touch_notebook(source.notebook_id, sources_changed=True)


class _Chat:
    def __init__(self, engine: Engine) -> None:
        self._e = engine

    async def history(self, notebook_id: str) -> list[ChatMessage]:
        items = [ChatMessage.model_validate(m) for m in await self._e.storage.list(MESSAGES)]
        return sorted((m for m in items if m.notebook_id == notebook_id), key=lambda m: m.created_at)

    async def clear(self, notebook_id: str) -> None:
        messages = await self.history(notebook_id)
        await asyncio.gather(*(self._e.storage.delete(MESSAGES, m.id) for m in messages))

    async def stream(
        self, notebook_id: str, message: str, *, source_ids: list[str] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream the grounded answer as chat events. The last event is `done` or `error`; this never raises."""
        e = self._e
        question: ChatMessage | None = None
        content: list[str] = []
        try:
            await e._notebook(notebook_id)
            text = (message or "").strip()
            if not text:
                raise OvertoneError("Message is empty.", status=400, code="empty_message")
            docs = await e._load_docs(notebook_id, source_ids)
            history = await self.history(notebook_id)

            question = ChatMessage(
                id=str(uuid.uuid4()),
                notebook_id=notebook_id,
                role="user",
                content=text,
                created_at=_stamp_after(history[-1].created_at if history else None),
            )
            await e.storage.put(MESSAGES, question.to_json())

            citations: list[Citation] = []
            async for event in stream_answer(e.runtime, docs, [(m.role, m.content) for m in history], text):
                if event["type"] == "text":
                    content.append(event["delta"])
                elif event["type"] == "citation":
                    citations.append(Citation.model_validate(event["citation"]))
                yield event

            reply = ChatMessage(
                id=str(uuid.uuid4()),
                notebook_id=notebook_id,
                role="assistant",
                content="".join(content),
                citations=citations,
                created_at=_stamp_after(question.created_at),
            )
            await e.storage.put(MESSAGES, reply.to_json())
            await e._touch_notebook(notebook_id)
            yield {"type": "done", "message": reply.to_json()}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if question is not None and not content:
                # Nothing was answered: don't leave an orphaned question in the history.
                try:
                    await e.storage.delete(MESSAGES, question.id)
                except Exception:
                    pass
            event: dict[str, Any] = {"type": "error", "error": str(exc) or type(exc).__name__}
            if isinstance(exc, OvertoneError):
                event["code"], event["status"] = exc.code, exc.status
            yield event

    async def send(self, notebook_id: str, message: str, *, source_ids: list[str] | None = None) -> ChatMessage:
        """Non-streaming convenience over `stream`."""
        async for event in self.stream(notebook_id, message, source_ids=source_ids):
            if event["type"] == "done":
                return ChatMessage.model_validate(event["message"])
            if event["type"] == "error":
                raise OvertoneError(
                    event["error"], status=event.get("status", 502), code=event.get("code", "chat_failed")
                )
        raise OvertoneError("The chat stream ended unexpectedly.", status=502, code="chat_failed")


class _Artifacts:
    def __init__(self, engine: Engine) -> None:
        self._e = engine

    async def _settle_orphan(self, artifact: Artifact) -> Artifact:
        """An artifact left mid-generation by a process that has since exited can never finish."""
        if artifact.status in ("queued", "generating") and artifact.id not in self._e._jobs:
            artifact.status = "error"
            artifact.progress = None
            artifact.error = "Generation was interrupted before it finished."
            artifact.updated_at = now_iso()
            await self._e.storage.put(ARTIFACTS, artifact.to_json())
        return artifact

    async def _save(self, artifact: Artifact) -> None:
        artifact.updated_at = now_iso()
        await self._e.storage.put(ARTIFACTS, artifact.to_json())
        for listener in self._e._listeners.get(artifact.id, []):
            try:
                listener(artifact.model_copy(deep=True))
            except Exception:
                pass  # a listener's failure must not break generation

    async def list(self, notebook_id: str) -> list[Artifact]:
        items = [Artifact.model_validate(a) for a in await self._e.storage.list(ARTIFACTS)]
        mine = [await self._settle_orphan(a) for a in items if a.notebook_id == notebook_id]
        return sorted(mine, key=lambda a: a.created_at, reverse=True)

    async def get(self, id: str) -> Artifact:
        raw = await self._e.storage.get(ARTIFACTS, id)
        if raw is None:
            raise _not_found("Artifact", id)
        return await self._settle_orphan(Artifact.model_validate(raw))

    async def create(self, notebook_id: str, request: ArtifactRequest) -> Artifact:
        """Returns immediately with a queued artifact; generation runs in the background."""
        e = self._e
        await e._notebook(notebook_id)
        if request.kind not in ARTIFACT_LABEL:
            raise OvertoneError("Unknown artifact kind.", status=400, code="bad_kind")
        if request.kind == "custom" and not (request.instructions or "").strip():
            raise OvertoneError(
                "A custom note needs instructions describing what to write.", status=400, code="missing_instructions"
            )
        # Fail fast, before queueing, when there is nothing to ground on.
        sources = await e._list_sources(notebook_id)
        if request.source_ids:
            wanted = set(request.source_ids)
            sources = [s for s in sources if s.id in wanted]
        if not sources:
            raise OvertoneError("Add at least one source to this notebook first.", status=400, code="no_sources")

        ts = now_iso()
        artifact = Artifact(
            id=str(uuid.uuid4()),
            notebook_id=notebook_id,
            kind=request.kind,
            title=ARTIFACT_LABEL[request.kind],
            status="queued",
            progress="Queued",
            source_ids=[s.id for s in sources],
            request=request,
            created_at=ts,
            updated_at=ts,
        )
        await e.storage.put(ARTIFACTS, artifact.to_json())
        queued = artifact.model_copy(deep=True)

        task = asyncio.create_task(self._run(artifact))
        e._jobs[artifact.id] = task

        def _finished(_: asyncio.Task[None], artifact_id: str = artifact.id) -> None:
            e._jobs.pop(artifact_id, None)
            e._listeners.pop(artifact_id, None)

        task.add_done_callback(_finished)
        return queued

    async def _run(self, artifact: Artifact) -> None:
        e = self._e
        try:
            artifact.status, artifact.progress = "generating", "Reading sources"
            await self._save(artifact)
            docs = await e._load_docs(artifact.notebook_id, artifact.source_ids)

            async def progress(message: str) -> None:
                artifact.progress = message
                await self._save(artifact)

            async def script_ready(script: AudioScript) -> None:
                artifact.title, artifact.script = script.title, script
                await self._save(artifact)

            result = await run_generation(
                e.runtime, e.tts, artifact.request, docs, JobHooks(progress=progress, script_ready=script_ready)
            )

            artifact.title = result.title or artifact.title
            artifact.content = result.content
            artifact.script = result.script
            artifact.audio_error = result.audio_error
            if result.audio is not None:
                blob_key = f"audio/{artifact.id}.mp3"
                await e.storage.put_blob(blob_key, result.audio)
                artifact.audio = ArtifactAudio(
                    mime_type=result.audio_mime or "audio/mpeg",
                    blob_key=blob_key,
                    bytes=len(result.audio),
                    provider=result.audio_provider or "unknown",
                )
            artifact.status, artifact.progress = "ready", None
            await self._save(artifact)
            await e._touch_notebook(artifact.notebook_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not isinstance(exc, OvertoneError):
                e._on_error(exc, f"artifact {artifact.id}")
            artifact.status, artifact.progress = "error", None
            artifact.error = str(exc) or type(exc).__name__
            try:
                await self._save(artifact)
            except Exception:
                pass

    async def wait(self, id: str, *, on_progress: ProgressCallback | None = None) -> Artifact:
        """Resolves when the artifact is ready or errored."""
        job = self._e._jobs.get(id)
        if job is not None:
            if on_progress:
                self._e._listeners.setdefault(id, []).append(on_progress)
            await asyncio.wait([job])
        return await self.get(id)

    async def generate(
        self, notebook_id: str, request: ArtifactRequest, *, on_progress: ProgressCallback | None = None
    ) -> Artifact:
        """`create`, then wait for completion. Raises if generation fails."""
        created = await self.create(notebook_id, request)
        done = await self.wait(created.id, on_progress=on_progress)
        if done.status == "error":
            raise OvertoneError(done.error or "Generation failed.", status=502, code="generation_failed")
        return done

    async def get_audio(self, id: str) -> tuple[bytes, str] | None:
        """(audio bytes, mime type), or None when the artifact has no rendered audio."""
        artifact = await self.get(id)
        if artifact.audio is None:
            return None
        data = await self._e.storage.get_blob(artifact.audio.blob_key)
        return (data, artifact.audio.mime_type) if data is not None else None

    async def delete(self, id: str) -> None:
        artifact = await self.get(id)
        job = self._e._jobs.get(id)
        if job:
            job.cancel()
        await self._e.storage.delete(ARTIFACTS, id)
        if artifact.audio:
            await self._e.storage.delete_blob(artifact.audio.blob_key)


class _Live:
    """Live voice: GPT-Live is the voice, the notebook agents are the brain."""

    # Keep the "episode so far" comfortably inside GPT-Live's instruction budget.
    _HEARD_CHARS = 24_000

    def __init__(self, engine: Engine) -> None:
        self._e = engine

    def _voice(self) -> LiveVoice:
        if self._e.live_voice is None:
            raise OvertoneError(
                "Live voice is not configured. Set OPENAI_API_KEY (with access to gpt-live-1) and restart.",
                status=501,
                code="live_unavailable",
            )
        return self._e.live_voice

    async def create_session(self, notebook_id: str, request: LiveSessionRequest) -> LiveSession:
        """Start a GPT-Live WebRTC session from the browser's SDP offer."""
        e = self._e
        live = self._voice()
        notebook = await e._notebook(notebook_id)
        if not request.sdp.strip():
            raise OvertoneError("Missing SDP offer.", status=400, code="bad_sdp")

        if request.mode == "podcast":
            if not request.artifact_id:
                raise OvertoneError("Podcast mode needs an artifactId.", status=400, code="missing_artifact")
            artifact = await e.artifacts.get(request.artifact_id)
            script = artifact.script
            if artifact.notebook_id != notebook_id or script is None or not script.turns:
                raise OvertoneError("That audio overview has no script to join.", status=400, code="no_script")
            at = min(max(request.turn_index or 0, 0), len(script.turns) - 1)
            host_index = script.turns[at].speaker if script.turns[at].speaker < len(script.hosts) else 0
            host = script.hosts[host_index]
            others = [h.name for i, h in enumerate(script.hosts) if i != host_index]
            heard = "\n".join(
                f"{script.hosts[t.speaker].name if t.speaker < len(script.hosts) else 'Host'}: {t.text}"
                for t in script.turns[: at + 1]
            )
            if len(heard) > self._HEARD_CHARS:
                heard = "[earlier part of the episode omitted]\n" + heard[-self._HEARD_CHARS :]
            instructions = live_podcast_instructions(
                notebook.title, script.title, script.summary, host.name, others[0] if others else None, heard
            )
            # Sound like the same host when the rendered voice is also a GPT-Live voice.
            voice = live.pick_voice(request.voice or host.voice)
        else:
            sources = await e._list_sources(notebook_id)
            if request.source_ids:
                wanted = set(request.source_ids)
                sources = [s for s in sources if s.id in wanted]
            if not sources:
                raise OvertoneError("Add at least one source to this notebook first.", status=400, code="no_sources")
            instructions = live_conversation_instructions(
                notebook.title, [s.title for s in sources], notebook.guide.summary if notebook.guide else None
            )
            voice = live.pick_voice(request.voice)

        session_id, answer = await live.start_webrtc_session(
            instructions=instructions, voice=voice, sdp_offer=request.sdp, safety_identifier=f"notebook-{notebook_id}"
        )
        return LiveSession(session_id=session_id, sdp=answer, mode=request.mode, model=live.model, voice=voice)

    async def delegate(self, notebook_id: str, request: LiveDelegateRequest) -> LiveDelegateResult:
        """Answer a question GPT-Live delegated, from the notebook's sources.

        Works without a live provider too, so integrators can drive it from any voice front end.
        """
        e = self._e
        await e._notebook(notebook_id)
        transcript = [(line.role, " ".join(line.text.split())) for line in request.transcript if line.text.strip()]
        if not any(role == "user" for role, _ in transcript):
            raise OvertoneError("The transcript has no user question to answer.", status=400, code="empty_transcript")
        source_ids = request.source_ids
        if request.artifact_id and not source_ids:
            source_ids = (await e.artifacts.get(request.artifact_id)).source_ids
        docs = await e._load_docs(notebook_id, source_ids)
        answer, citations = await voice_answer(e.runtime, docs, transcript[-16:])
        return LiveDelegateResult(answer=answer, citations=citations)


def create_engine(
    *,
    storage: Storage | None = None,
    data_dir: str | None = None,
    chat_client: Any = None,
    model: str | None = None,
    tts: TtsProvider | TtsConfig | None = _UNSET,
    live_voice: LiveVoice | None = _UNSET,
    on_error: Callable[[BaseException, str], None] | None = None,
) -> Engine:
    """Build an engine.

    Args:
        storage: Where notebooks live. Default: `FileStorage` at `data_dir`, OVERTONE_DATA_DIR or ./.overtone.
        chat_client: Any Microsoft Agent Framework chat client. Default: Claude via `AnthropicClient`
            (or the no-network mock when OVERTONE_MOCK=1).
        model: Model id for the default Claude client. Default: OVERTONE_MODEL or claude-opus-5-5.
        tts: A provider, a `TtsConfig`, or None to disable server-side audio. Default: from the environment.
        live_voice: A `LiveVoice`, or None to disable live voice. Default: from the environment.
    """
    if storage is None:
        storage = FileStorage(data_dir or os.environ.get("OVERTONE_DATA_DIR") or ".overtone")
    if tts is _UNSET:
        provider = create_tts_provider()
    elif tts is None or isinstance(tts, TtsConfig):
        provider = create_tts_provider(tts) if tts is not None else None
    else:
        provider = tts
    return Engine(
        storage=storage,
        runtime=create_runtime(chat_client=chat_client, model=model),
        tts=provider,
        live_voice=create_live_voice() if live_voice is _UNSET else live_voice,
        on_error=on_error,
    )
