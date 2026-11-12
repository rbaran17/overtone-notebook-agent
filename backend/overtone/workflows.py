"""Generation pipelines as Microsoft Agent Framework workflows.

An audio overview is a two-step graph:

    scriptwriter ──ScriptDraft──▶ sound_engineer ──▶ GenerationResult

and a document is a single-node graph around the same producer agent. Each
executor is a unit you can swap, extend (add a fact-check or review step between
the two), or reuse inside a larger Agent Framework workflow of your own.

Note: this module deliberately does not use `from __future__ import annotations`;
Agent Framework inspects the handler signatures at import time to type the edges.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Never

from agent_framework import Executor, Workflow, WorkflowBuilder, WorkflowContext, handler

from .agents import ModelRuntime, to_overtone_error, write_document, write_script
from .errors import OvertoneError
from .grounding import SourceDoc
from .models import ARTIFACT_LABEL, ArtifactRequest, AudioOptions, AudioScript, TtsProvider
from .tts import synthesize_dialogue


@dataclass
class GenerationJob:
    """Workflow input: what to make, and the sources to make it from."""

    request: ArtifactRequest
    docs: list[SourceDoc]


@dataclass
class ScriptDraft:
    """Scriptwriter → sound engineer."""

    script: AudioScript


@dataclass
class GenerationResult:
    """Workflow output."""

    title: str
    content: str | None = None
    script: AudioScript | None = None
    audio: bytes | None = None
    audio_mime: str | None = None
    audio_provider: str | None = None
    audio_error: str | None = None


async def _noop_progress(_: str) -> None:
    return None


async def _noop_script(_: AudioScript) -> None:
    return None


@dataclass
class JobHooks:
    """Callbacks the engine uses to surface progress while a workflow runs."""

    progress: Callable[[str], Awaitable[None]] = field(default=_noop_progress)
    script_ready: Callable[[AudioScript], Awaitable[None]] = field(default=_noop_script)


class Scriptwriter(Executor):
    """Runs the producer agent to turn the sources into a dialogue script."""

    def __init__(self, runtime: ModelRuntime, hooks: JobHooks) -> None:
        super().__init__(id="scriptwriter")
        self._runtime = runtime
        self._hooks = hooks

    @handler
    async def write(self, job: GenerationJob, ctx: WorkflowContext[ScriptDraft]) -> None:
        await self._hooks.progress("Writing the script")
        script = await write_script(self._runtime, job.docs, job.request.options or AudioOptions())
        await self._hooks.script_ready(script)
        await ctx.send_message(ScriptDraft(script))


class SoundEngineer(Executor):
    """Voices the script with the configured text-to-speech provider.

    With no provider, or if rendering fails, the script still ships: clients can
    read it aloud themselves, so a missing audio file is not a failed job.
    """

    def __init__(self, tts: TtsProvider | None, hooks: JobHooks) -> None:
        super().__init__(id="sound_engineer")
        self._tts = tts
        self._hooks = hooks

    @handler
    async def render(self, draft: ScriptDraft, ctx: WorkflowContext[Never, GenerationResult]) -> None:
        script = draft.script
        result = GenerationResult(title=script.title, script=script)
        if self._tts is not None:
            total = len(script.turns)
            # The synthesiser reports progress synchronously; a small pump turns
            # those calls into ordered, awaited reports without blocking it.
            queue: asyncio.Queue[str | None] = asyncio.Queue()

            async def pump() -> None:
                while (message := await queue.get()) is not None:
                    try:
                        await self._hooks.progress(message)
                    except Exception:
                        pass  # progress is best-effort

            pump_task = asyncio.create_task(pump())
            queue.put_nowait(f"Recording audio (0/{total})")
            try:
                audio, mime = await synthesize_dialogue(
                    self._tts,
                    script,
                    on_progress=lambda done, count: queue.put_nowait(f"Recording audio ({done}/{count})"),
                )
                result.audio, result.audio_mime, result.audio_provider = audio, mime, self._tts.id
            except Exception as exc:  # keep the script; report why there is no audio
                result.audio_error = str(exc) or type(exc).__name__
            finally:
                queue.put_nowait(None)
                await pump_task
        await ctx.yield_output(result)


class DocumentWriter(Executor):
    """Runs the producer agent to write a Markdown document."""

    def __init__(self, runtime: ModelRuntime, hooks: JobHooks) -> None:
        super().__init__(id="document_writer")
        self._runtime = runtime
        self._hooks = hooks

    @handler
    async def write(self, job: GenerationJob, ctx: WorkflowContext[Never, GenerationResult]) -> None:
        kind = job.request.kind
        await self._hooks.progress(f"Writing the {ARTIFACT_LABEL[kind].lower()}")
        title, content = await write_document(self._runtime, job.docs, kind, job.request.instructions)
        await ctx.yield_output(GenerationResult(title=title or ARTIFACT_LABEL[kind], content=content))


def build_audio_workflow(runtime: ModelRuntime, tts: TtsProvider | None, hooks: JobHooks | None = None) -> Workflow:
    hooks = hooks or JobHooks()
    scriptwriter = Scriptwriter(runtime, hooks)
    engineer = SoundEngineer(tts, hooks)
    return (
        WorkflowBuilder(
            start_executor=scriptwriter,
            name="audio-overview",
            description="Write an episode script from the sources, then voice it.",
        )
        .add_edge(scriptwriter, engineer)
        .build()
    )


def build_document_workflow(runtime: ModelRuntime, hooks: JobHooks | None = None) -> Workflow:
    return WorkflowBuilder(
        start_executor=DocumentWriter(runtime, hooks or JobHooks()),
        name="document",
        description="Write a Markdown document from the sources.",
    ).build()


async def run_generation(
    runtime: ModelRuntime,
    tts: TtsProvider | None,
    request: ArtifactRequest,
    docs: list[SourceDoc],
    hooks: JobHooks | None = None,
) -> GenerationResult:
    """Build the right workflow for the request, run it, and return its output."""
    workflow = (
        build_audio_workflow(runtime, tts, hooks)
        if request.kind == "audio_overview"
        else build_document_workflow(runtime, hooks)
    )
    try:
        run = await workflow.run(GenerationJob(request=request, docs=docs))
    except Exception as exc:
        raise _unwrap(exc) from exc
    outputs = [o for o in run.get_outputs() if isinstance(o, GenerationResult)]
    if not outputs:
        raise OvertoneError("Generation finished without producing a result.", status=502, code="generation_failed")
    return outputs[-1]


def _unwrap(exc: BaseException) -> Exception:
    """Find the caller-facing error inside whatever the workflow runner wrapped it in."""
    cur: BaseException | None = exc
    seen: list[BaseException] = []
    while cur is not None and cur not in seen:
        if isinstance(cur, OvertoneError):
            return cur
        seen.append(cur)
        cur = getattr(cur, "inner_exception", None) or cur.__cause__ or cur.__context__
    return to_overtone_error(exc)
