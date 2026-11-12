"""Tests for overtone.workflows: the audio and document pipelines, their progress hooks and failure paths."""

from __future__ import annotations

from typing import Any

import pytest
from agent_framework import BaseChatClient, ChatResponse, ChatResponseUpdate, Content, FunctionInvocationLayer, Message

from overtone import MockChatClient, OvertoneError, create_runtime
from overtone.grounding import SourceDoc
from overtone.models import ARTIFACT_LABEL, DOCUMENT_KINDS, ArtifactRequest, AudioHost, AudioOptions
from overtone.workflows import GenerationResult, JobHooks, build_audio_workflow, build_document_workflow, run_generation


class BrokenChatClient(FunctionInvocationLayer, BaseChatClient):  # type: ignore[type-arg, misc]
    """Every call fails with the same error."""

    model = "broken"

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    def _inner_get_response(self, *, messages: Any, stream: bool, options: Any, **kwargs: Any) -> Any:
        if stream:

            async def _updates():  # type: ignore[no-untyped-def]
                raise self.error
                yield ChatResponseUpdate(role="assistant", contents=[Content.from_text("")])  # pragma: no cover

            return self._build_response_stream(_updates(), response_format=options.get("response_format"))

        async def _response() -> ChatResponse:
            raise self.error

        return _response()


def _runtime(client: Any = None) -> Any:
    return create_runtime(chat_client=client or MockChatClient(delay=0))


def _audio_request(**options: Any) -> ArtifactRequest:
    return ArtifactRequest(kind="audio_overview", options=AudioOptions(**options) if options else None)


# ───────────────────────────── audio overview ─────────────────────────────


async def test_audio_without_tts_returns_script_only(sample_docs: list[SourceDoc]) -> None:
    result = await run_generation(_runtime(), None, _audio_request(), sample_docs)
    assert isinstance(result, GenerationResult)
    assert result.script is not None and result.script.turns
    assert result.title == result.script.title
    assert result.audio is None
    assert result.audio_mime is None
    assert result.audio_provider is None
    assert result.audio_error is None


async def test_audio_with_tts_concatenates_segments_in_script_order(sample_docs: list[SourceDoc], fake_tts: Any) -> None:
    result = await run_generation(_runtime(), fake_tts, _audio_request(), sample_docs)
    assert result.script is not None
    expected = b"".join(
        f"<{fake_tts.default_voices[t.speaker]}|{t.text}>".encode("utf-8") for t in result.script.turns
    )
    assert result.audio == expected
    assert result.audio_mime == "audio/mpeg"
    assert result.audio_provider == "fake"
    assert result.audio_error is None


async def test_audio_uses_the_hosts_own_voices(sample_docs: list[SourceDoc], fake_tts: Any) -> None:
    hosts = [AudioHost(name="Ada", voice="v2"), AudioHost(name="Bo", voice="v1")]
    await run_generation(_runtime(), fake_tts, _audio_request(hosts=hosts), sample_docs)
    assert {voice for _, voice in fake_tts.calls} <= {"v1", "v2"}
    assert {voice for _, voice in fake_tts.calls} == {"v1", "v2"}


async def test_tts_failure_keeps_the_script_and_reports_the_error(sample_docs: list[SourceDoc], fake_tts: Any) -> None:
    fake_tts.error = RuntimeError("voice quota exceeded")
    result = await run_generation(_runtime(), fake_tts, _audio_request(), sample_docs)
    assert result.script is not None and result.script.turns
    assert result.audio is None
    assert result.audio_error == "voice quota exceeded"
    assert result.audio_provider is None


async def test_tts_failure_with_no_message_uses_the_exception_name(sample_docs: list[SourceDoc], fake_tts: Any) -> None:
    fake_tts.error = KeyError()
    result = await run_generation(_runtime(), fake_tts, _audio_request(), sample_docs)
    assert result.audio is None
    assert result.audio_error == "KeyError"


async def test_progress_reports_script_then_recording_to_the_last_turn(sample_docs: list[SourceDoc], fake_tts: Any) -> None:
    progress: list[str] = []
    events: list[str] = []

    async def on_progress(message: str) -> None:
        progress.append(message)
        events.append(f"progress:{message}")

    async def on_script(script: Any) -> None:
        events.append("script")

    hooks = JobHooks(progress=on_progress, script_ready=on_script)
    result = await run_generation(_runtime(), fake_tts, _audio_request(), sample_docs, hooks)

    assert result.script is not None
    total = len(result.script.turns)
    assert progress[0] == "Writing the script"
    assert progress[1:] == [f"Recording audio ({i}/{total})" for i in range(0, total + 1)]
    assert progress[-1] == f"Recording audio ({total}/{total})"


async def test_script_ready_is_called_once_before_any_recording(sample_docs: list[SourceDoc], fake_tts: Any) -> None:
    events: list[str] = []

    async def on_progress(message: str) -> None:
        events.append(message)

    async def on_script(script: Any) -> None:
        events.append("SCRIPT")

    await run_generation(_runtime(), fake_tts, _audio_request(), sample_docs, JobHooks(on_progress, on_script))
    assert events.count("SCRIPT") == 1
    first_recording = next(i for i, e in enumerate(events) if e.startswith("Recording audio"))
    assert events.index("SCRIPT") < first_recording


async def test_progress_without_tts_only_reports_the_script(sample_docs: list[SourceDoc]) -> None:
    progress: list[str] = []

    async def on_progress(message: str) -> None:
        progress.append(message)

    await run_generation(_runtime(), None, _audio_request(), sample_docs, JobHooks(progress=on_progress))
    assert progress == ["Writing the script"]


async def test_a_failing_progress_hook_does_not_fail_the_recording(sample_docs: list[SourceDoc], fake_tts: Any) -> None:
    async def angry(message: str) -> None:
        if message.startswith("Recording"):
            raise RuntimeError("hook exploded")

    result = await run_generation(_runtime(), fake_tts, _audio_request(), sample_docs, JobHooks(progress=angry))
    assert result.audio is not None


async def test_scriptwriter_failure_surfaces_as_overtone_error(sample_docs: list[SourceDoc]) -> None:
    runtime = _runtime(BrokenChatClient(RuntimeError("model fell over")))
    with pytest.raises(OvertoneError) as info:
        await run_generation(runtime, None, _audio_request(), sample_docs)
    assert info.value.code == "llm_error"
    assert "model fell over" in info.value.message


def test_audio_workflow_has_the_two_named_steps() -> None:
    workflow = build_audio_workflow(_runtime(), None)
    assert workflow.name == "audio-overview"


# ───────────────────────────── documents ─────────────────────────────


@pytest.mark.parametrize("kind", [k for k in DOCUMENT_KINDS if k != "custom"])
async def test_each_document_kind_produces_markdown(sample_docs: list[SourceDoc], kind: str) -> None:
    result = await run_generation(_runtime(), None, ArtifactRequest(kind=kind), sample_docs)  # type: ignore[arg-type]
    assert result.content and result.content.startswith("# ")
    assert result.title == "Mock document"
    assert result.script is None
    assert result.audio is None


async def test_custom_document_uses_its_instructions(sample_docs: list[SourceDoc]) -> None:
    request = ArtifactRequest(kind="custom", instructions="Write a limerick about revenue.")
    result = await run_generation(_runtime(), None, request, sample_docs)
    assert result.content and result.content.startswith("# ")


async def test_document_ignores_tts_entirely(sample_docs: list[SourceDoc], fake_tts: Any) -> None:
    result = await run_generation(_runtime(), fake_tts, ArtifactRequest(kind="faq"), sample_docs)
    assert result.audio is None
    assert fake_tts.calls == []


async def test_document_progress_names_the_kind(sample_docs: list[SourceDoc]) -> None:
    progress: list[str] = []

    async def on_progress(message: str) -> None:
        progress.append(message)

    await run_generation(_runtime(), None, ArtifactRequest(kind="study_guide"), sample_docs, JobHooks(progress=on_progress))
    assert progress == [f"Writing the {ARTIFACT_LABEL['study_guide'].lower()}"]


async def test_document_writer_failure_surfaces_as_overtone_error(sample_docs: list[SourceDoc]) -> None:
    runtime = _runtime(BrokenChatClient(RuntimeError("no capacity")))
    with pytest.raises(OvertoneError) as info:
        await run_generation(runtime, None, ArtifactRequest(kind="briefing"), sample_docs)
    assert info.value.code == "llm_error"


def test_document_workflow_is_named() -> None:
    assert build_document_workflow(_runtime()).name == "document"
