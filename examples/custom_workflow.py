"""Extend the podcast pipeline with your own Microsoft Agent Framework step.

    python examples/custom_workflow.py

Overtone's audio overview is an Agent Framework workflow:

    scriptwriter ──▶ sound_engineer

This example inserts a "standards desk" between the two. It is an ordinary
Executor, so it could just as well call another agent, a fact-checker, or wait
for a human to approve the script.

Note: no `from __future__ import annotations` here. Agent Framework reads the
handler's type hints at import time to work out which messages an executor accepts.
"""

import asyncio
import os
import sys
from pathlib import Path

if not os.environ.get("ANTHROPIC_API_KEY"):
    os.environ.setdefault("OVERTONE_MOCK", "1")

from agent_framework import Executor, WorkflowBuilder, WorkflowContext, handler

from overtone import ArtifactRequest, AudioOptions, SourceDoc, create_runtime
from overtone.models import DialogueTurn
from overtone.workflows import GenerationJob, GenerationResult, JobHooks, ScriptDraft, Scriptwriter, SoundEngineer

SOURCES = Path(__file__).parent / "sources"

DISCLOSURE = (
    "One note before we go. This episode was produced by AI from the documents you provided, "
    "so check the originals before you rely on a figure."
)


class StandardsDesk(Executor):
    """Reviews the script before it is voiced: here, it adds a closing disclosure."""

    def __init__(self) -> None:
        super().__init__(id="standards_desk")

    @handler
    async def review(self, draft: ScriptDraft, ctx: WorkflowContext[ScriptDraft]) -> None:
        script = draft.script
        closing = DialogueTurn(speaker=script.turns[-1].speaker, text=DISCLOSURE)
        await ctx.send_message(ScriptDraft(script.model_copy(update={"turns": [*script.turns, closing]})))


async def main() -> None:
    runtime = create_runtime()  # Claude, or the stand-in model when no key is set
    hooks = JobHooks()
    writer, desk, engineer = Scriptwriter(runtime, hooks), StandardsDesk(), SoundEngineer(tts=None, hooks=hooks)
    workflow = (
        WorkflowBuilder(start_executor=writer, name="reviewed-audio-overview", description="Write, review, voice.")
        .add_edge(writer, desk)
        .add_edge(desk, engineer)
        .build()
    )

    docs = [SourceDoc(id=path.stem, title=path.stem, text=path.read_text(encoding="utf-8")) for path in sorted(SOURCES.glob("*.md"))]
    request = ArtifactRequest(kind="audio_overview", options=AudioOptions(format="brief", length="short"))
    run = await workflow.run(GenerationJob(request=request, docs=docs))
    result = next(output for output in run.get_outputs() if isinstance(output, GenerationResult))

    script = result.script
    print(f"{script.title} ({len(script.turns)} turns)\n")
    for turn in script.turns[-2:]:
        print(f"{script.hosts[turn.speaker].name}: {turn.text}\n")
    assert script.turns[-1].text == DISCLOSURE


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
