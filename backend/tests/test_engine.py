"""Tests for overtone.engine: notebooks, sources, guides, chat, artifacts, one-shot, capabilities and live delegation."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from agent_framework import BaseChatClient, ChatResponse, ChatResponseUpdate, Content, FunctionInvocationLayer, Message

from overtone import MemoryStorage, MockChatClient, OvertoneError, create_engine
from overtone.models import (
    Artifact,
    ArtifactRequest,
    AudioOptions,
    LiveDelegateRequest,
    LiveSessionRequest,
    LiveTranscriptLine,
    TextInput,
    now_iso,
)
from overtone.live import LiveVoice

REPORT = (
    "Revenue grew eight percent in the third quarter, driven by enterprise renewals. "
    "Hiring slowed to a crawl while the team absorbed the new billing system. "
    "Costs were flat against plan, with infrastructure spend slightly over budget."
)
MEMO = (
    "The launch moves to spring. Risks include vendor delays and a thin support rota. "
    "The board asked for a monthly check-in until the launch is stable."
)


class BrokenChatClient(FunctionInvocationLayer, BaseChatClient):  # type: ignore[type-arg, misc]
    """Every model call fails with "boom"."""

    model = "broken"

    def _inner_get_response(self, *, messages: Any, stream: bool, options: Any, **kwargs: Any) -> Any:
        if stream:

            async def _updates():  # type: ignore[no-untyped-def]
                raise RuntimeError("boom")
                yield ChatResponseUpdate(role="assistant", contents=[Content.from_text("")])  # pragma: no cover

            return self._build_response_stream(_updates(), response_format=options.get("response_format"))

        async def _response() -> ChatResponse:
            raise RuntimeError("boom")

        return _response()


async def _notebook_with_sources(engine: Any) -> tuple[Any, list[Any]]:
    notebook = await engine.notebooks.create(title="Quarterly notes")
    report = await engine.sources.add(notebook.id, TextInput(REPORT, title="Q3 report"))
    memo = await engine.sources.add(notebook.id, TextInput(MEMO, title="Board memo"))
    return notebook, [report, memo]


# ───────────────────────────── notebooks ─────────────────────────────


async def test_create_uses_a_default_title_and_trims_given_titles(engine: Any) -> None:
    assert (await engine.notebooks.create()).title == "Untitled notebook"
    trimmed = await engine.notebooks.create(title="   Plans  ")
    assert trimmed.title == "Plans"
    assert trimmed.source_count == 0


async def test_get_reports_source_count_and_update_changes_fields(engine: Any) -> None:
    notebook, _ = await _notebook_with_sources(engine)
    fetched = await engine.notebooks.get(notebook.id)
    assert fetched.source_count == 2

    renamed = await engine.notebooks.update(notebook.id, title=" New name ", description="About it", icon="book")
    assert renamed.title == "New name"
    assert renamed.description == "About it"
    assert renamed.icon == "book"

    cleared = await engine.notebooks.update(notebook.id, description="", icon="")
    assert cleared.description is None
    assert cleared.icon is None


async def test_list_orders_by_last_update_newest_first(engine: Any) -> None:
    first = await engine.notebooks.create(title="First")
    await asyncio.sleep(0.005)
    second = await engine.notebooks.create(title="Second")
    await asyncio.sleep(0.005)
    await engine.notebooks.update(first.id, title="First, edited")
    listed = await engine.notebooks.list()
    assert [n.id for n in listed] == [first.id, second.id]


async def test_list_counts_sources_per_notebook(engine: Any) -> None:
    busy, _ = await _notebook_with_sources(engine)
    empty = await engine.notebooks.create(title="Empty")
    counts = {n.id: n.source_count for n in await engine.notebooks.list()}
    assert counts == {busy.id: 2, empty.id: 0}


async def test_unknown_notebook_is_404(engine: Any) -> None:
    with pytest.raises(OvertoneError) as info:
        await engine.notebooks.get("nope")
    assert (info.value.status, info.value.code) == (404, "not_found")


async def test_deleting_a_notebook_removes_its_sources_messages_artifacts_and_blobs(fake_tts: Any) -> None:
    storage = MemoryStorage()
    engine = create_engine(storage=storage, chat_client=MockChatClient(delay=0), tts=fake_tts, live_voice=None)
    notebook, (report, memo) = await _notebook_with_sources(engine)
    await engine.chat.send(notebook.id, "What grew?")
    audio = await engine.artifacts.generate(notebook.id, ArtifactRequest(kind="audio_overview"))
    summary = await engine.artifacts.generate(notebook.id, ArtifactRequest(kind="summary"))
    assert audio.audio is not None
    assert await storage.get_blob(audio.audio.blob_key) is not None

    other = await engine.notebooks.create(title="Keep me")
    await engine.sources.add(other.id, TextInput("Unrelated text that must survive."))

    await engine.notebooks.delete(notebook.id)

    assert await storage.get("notebooks", notebook.id) is None
    for source in (report, memo):
        assert await storage.get("sources", source.id) is None
        assert await storage.get_blob(f"sources/{source.id}.txt") is None
    assert [m for m in await storage.list("messages") if m["notebookId"] == notebook.id] == []
    assert await storage.get("artifacts", audio.id) is None
    assert await storage.get("artifacts", summary.id) is None
    assert await storage.get_blob(audio.audio.blob_key) is None
    # The other notebook is untouched.
    assert [n.id for n in await engine.notebooks.list()] == [other.id]
    assert len(await engine.sources.list(other.id)) == 1


async def test_deleting_an_unknown_notebook_is_404(engine: Any) -> None:
    with pytest.raises(OvertoneError) as info:
        await engine.notebooks.delete("nope")
    assert info.value.status == 404


# ───────────────────────────── sources ─────────────────────────────


async def test_add_text_source_records_metadata_and_text(engine: Any) -> None:
    notebook = await engine.notebooks.create(title="N")
    source = await engine.sources.add(notebook.id, TextInput("  Hello   world.  ", title="Greeting"))
    assert source.kind == "text"
    assert source.title == "Greeting"
    assert source.notebook_id == notebook.id
    # Ingest normalises whitespace, so the stored text is the collapsed form.
    assert source.char_count == len("Hello world.")
    assert source.excerpt == "Hello world."
    assert await engine.sources.get_text(source.id) == "Hello world."


async def test_title_defaults_when_not_given(engine: Any) -> None:
    notebook = await engine.notebooks.create(title="N")
    source = await engine.sources.add(notebook.id, TextInput("Some body text."))
    assert source.title
    assert source.title != ""


async def test_blank_text_source_is_422_empty_source(engine: Any) -> None:
    notebook = await engine.notebooks.create(title="N")
    with pytest.raises(OvertoneError) as info:
        await engine.sources.add(notebook.id, TextInput("   \n\t  ", title="Blank"))
    assert (info.value.status, info.value.code) == (422, "empty_source")
    assert await engine.sources.list(notebook.id) == []


async def test_adding_a_source_to_an_unknown_notebook_is_404(engine: Any) -> None:
    with pytest.raises(OvertoneError) as info:
        await engine.sources.add("nope", TextInput("text"))
    assert (info.value.status, info.value.code) == (404, "not_found")


async def test_sources_are_listed_in_the_order_they_were_added(engine: Any) -> None:
    notebook = await engine.notebooks.create(title="N")
    added = []
    for i in range(3):
        added.append(await engine.sources.add(notebook.id, TextInput(f"Source body {i}.", title=f"S{i}")))
        await asyncio.sleep(0.002)
    assert [s.id for s in await engine.sources.list(notebook.id)] == [s.id for s in added]


async def test_source_lookup_and_delete(engine: Any) -> None:
    notebook, (report, _) = await _notebook_with_sources(engine)
    assert (await engine.sources.get(report.id)).title == "Q3 report"
    await engine.sources.delete(report.id)
    with pytest.raises(OvertoneError) as info:
        await engine.sources.get(report.id)
    assert (info.value.status, info.value.code) == (404, "not_found")
    assert await engine.storage.get_blob(f"sources/{report.id}.txt") is None
    assert (await engine.notebooks.get(notebook.id)).source_count == 1


async def test_unknown_source_is_404(engine: Any) -> None:
    with pytest.raises(OvertoneError) as info:
        await engine.sources.get("nope")
    assert info.value.status == 404


# ───────────────────────────── guide ─────────────────────────────


async def test_guide_is_cached_until_the_sources_change(engine: Any) -> None:
    notebook, _ = await _notebook_with_sources(engine)
    first = await engine.notebooks.guide(notebook.id)
    assert "2 sources" in first.summary
    assert 1 <= len(first.questions) <= 5

    again = await engine.notebooks.guide(notebook.id)
    assert again.generated_at == first.generated_at


async def test_adding_a_source_invalidates_the_guide(engine: Any) -> None:
    notebook, _ = await _notebook_with_sources(engine)
    await engine.notebooks.guide(notebook.id)
    await engine.sources.add(notebook.id, TextInput("A third body of text."))
    assert (await engine.notebooks.get(notebook.id)).guide is None
    refreshed = await engine.notebooks.guide(notebook.id)
    assert "3 sources" in refreshed.summary


async def test_removing_a_source_invalidates_the_guide(engine: Any) -> None:
    notebook, (report, _) = await _notebook_with_sources(engine)
    await engine.notebooks.guide(notebook.id)
    await engine.sources.delete(report.id)
    assert (await engine.notebooks.get(notebook.id)).guide is None
    assert "1 source" in (await engine.notebooks.guide(notebook.id)).summary


async def test_guide_for_a_notebook_without_sources_is_400(engine: Any) -> None:
    notebook = await engine.notebooks.create(title="Empty")
    with pytest.raises(OvertoneError) as info:
        await engine.notebooks.guide(notebook.id)
    assert (info.value.status, info.value.code) == (400, "no_sources")


async def test_guide_refresh_regenerates_even_when_cached(engine: Any) -> None:
    notebook, _ = await _notebook_with_sources(engine)
    first = await engine.notebooks.guide(notebook.id)
    await asyncio.sleep(0.005)
    fresh = await engine.notebooks.guide(notebook.id, refresh=True)
    assert fresh.generated_at != first.generated_at


# ───────────────────────────── chat ─────────────────────────────


async def test_send_returns_the_assistant_reply_with_citations(engine: Any) -> None:
    notebook, (report, _) = await _notebook_with_sources(engine)
    reply = await engine.chat.send(notebook.id, "What happened to revenue?")
    assert reply.role == "assistant"
    assert reply.content.strip()
    assert reply.citations, "the mock cites the opening passage of each source"
    assert [c.index for c in reply.citations] == list(range(1, len(reply.citations) + 1))
    for citation in reply.citations:
        assert citation.quote in REPORT or citation.quote in MEMO
        assert citation.source_title in {"Q3 report", "Board memo"}


async def test_history_is_persisted_in_order_user_first(engine: Any) -> None:
    notebook, _ = await _notebook_with_sources(engine)
    await engine.chat.send(notebook.id, "First question?")
    await engine.chat.send(notebook.id, "Second question?")
    history = await engine.chat.history(notebook.id)
    assert [m.role for m in history] == ["user", "assistant", "user", "assistant"]
    assert history[0].content == "First question?"
    assert history[2].content == "Second question?"
    assert history[0].created_at < history[1].created_at


async def test_history_stays_in_order_when_the_clock_does_not_advance(
    engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Turns that land in the same millisecond (a fast model on a fast machine) must still sort correctly."""
    import datetime as real

    class FrozenClock(real.datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "FrozenClock":
            return cls(2026, 1, 1, 12, 0, 0, tzinfo=tz)

    notebook, _ = await _notebook_with_sources(engine)
    monkeypatch.setattr("overtone.engine.datetime", FrozenClock)
    monkeypatch.setattr("overtone.engine.now_iso", lambda: "2026-01-01T12:00:00.000Z")
    for question in ("First question?", "Second question?", "Third question?"):
        await engine.chat.send(notebook.id, question)
    history = await engine.chat.history(notebook.id)
    assert [m.role for m in history] == ["user", "assistant"] * 3
    assert [m.content for m in history[::2]] == ["First question?", "Second question?", "Third question?"]
    stamps = [m.created_at for m in history]
    assert stamps == sorted(set(stamps)), "timestamps must be strictly increasing"


async def test_stream_yields_text_citations_then_done(engine: Any) -> None:
    notebook, _ = await _notebook_with_sources(engine)
    events = [e async for e in engine.chat.stream(notebook.id, "Summarise the sources.")]
    kinds = [e["type"] for e in events]
    assert kinds[-1] == "done"
    assert "text" in kinds and "citation" in kinds
    assert "error" not in kinds
    done = events[-1]["message"]
    assert done["role"] == "assistant"
    streamed = "".join(e["delta"] for e in events if e["type"] == "text")
    assert streamed == done["content"]


async def test_clear_removes_history(engine: Any) -> None:
    notebook, _ = await _notebook_with_sources(engine)
    await engine.chat.send(notebook.id, "Anything?")
    await engine.chat.clear(notebook.id)
    assert await engine.chat.history(notebook.id) == []


async def test_source_ids_restrict_what_the_answer_is_grounded_on(engine: Any) -> None:
    notebook, (report, memo) = await _notebook_with_sources(engine)
    reply = await engine.chat.send(notebook.id, "Anything?", source_ids=[memo.id])
    assert reply.citations
    assert {c.source_id for c in reply.citations} == {memo.id}


async def test_unknown_source_ids_fail_the_stream_with_no_sources(engine: Any) -> None:
    notebook, _ = await _notebook_with_sources(engine)
    events = [e async for e in engine.chat.stream(notebook.id, "Anything?", source_ids=["nope"])]
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "at least one source" in events[0]["error"]


async def test_empty_message_yields_one_error_event_and_stores_nothing(engine: Any) -> None:
    notebook, _ = await _notebook_with_sources(engine)
    for text in ["", "   \n"]:
        events = [e async for e in engine.chat.stream(notebook.id, text)]
        assert [e["type"] for e in events] == ["error"]
        assert events[0]["error"] == "Message is empty."
    assert await engine.chat.history(notebook.id) == []


async def test_stream_for_an_unknown_notebook_yields_an_error_not_an_exception(engine: Any) -> None:
    events = [e async for e in engine.chat.stream("nope", "Hello?")]
    assert [e["type"] for e in events] == ["error"]


async def test_provider_failure_during_chat_is_an_error_event(fake_tts: Any) -> None:
    engine = create_engine(storage=MemoryStorage(), chat_client=BrokenChatClient(), tts=None, live_voice=None)
    notebook, _ = await _notebook_with_sources(engine)
    events = [e async for e in engine.chat.stream(notebook.id, "Anything?")]
    assert events[-1]["type"] == "error"
    assert "boom" in events[-1]["error"]


async def test_send_keeps_the_client_error_status_and_code(engine: Any) -> None:
    notebook, _ = await _notebook_with_sources(engine)
    with pytest.raises(OvertoneError) as info:
        await engine.chat.send(notebook.id, "   ")
    assert (info.value.status, info.value.code) == (400, "empty_message")


# ───────────────────────────── artifacts ─────────────────────────────


async def test_create_returns_a_queued_artifact_that_later_becomes_ready(engine: Any) -> None:
    notebook, _ = await _notebook_with_sources(engine)
    queued = await engine.artifacts.create(notebook.id, ArtifactRequest(kind="summary"))
    assert queued.status == "queued"
    assert queued.progress == "Queued"
    assert "audio" not in queued.to_json()

    done = await engine.artifacts.wait(queued.id)
    assert done.status == "ready"
    assert done.content and done.content.startswith("# ")
    assert done.title == "Mock document"
    stored = await engine.artifacts.get(queued.id)
    assert stored.status == "ready"


async def test_generate_raises_when_generation_fails() -> None:
    engine = create_engine(storage=MemoryStorage(), chat_client=BrokenChatClient(), tts=None, live_voice=None)
    notebook, _ = await _notebook_with_sources(engine)
    with pytest.raises(OvertoneError) as info:
        await engine.artifacts.generate(notebook.id, ArtifactRequest(kind="faq"))
    assert (info.value.status, info.value.code) == (502, "generation_failed")
    assert "boom" in info.value.message

    listed = await engine.artifacts.list(notebook.id)
    assert [a.status for a in listed] == ["error"]
    assert "boom" in (listed[0].error or "")


async def test_custom_without_instructions_is_400(engine: Any) -> None:
    notebook, _ = await _notebook_with_sources(engine)
    for instructions in [None, "   "]:
        with pytest.raises(OvertoneError) as info:
            await engine.artifacts.create(notebook.id, ArtifactRequest(kind="custom", instructions=instructions))
        assert (info.value.status, info.value.code) == (400, "missing_instructions")


async def test_artifact_without_sources_is_400(engine: Any) -> None:
    notebook = await engine.notebooks.create(title="Empty")
    with pytest.raises(OvertoneError) as info:
        await engine.artifacts.create(notebook.id, ArtifactRequest(kind="summary"))
    assert (info.value.status, info.value.code) == (400, "no_sources")


async def test_artifact_records_which_sources_it_used(engine: Any) -> None:
    notebook, (report, _) = await _notebook_with_sources(engine)
    artifact = await engine.artifacts.generate(
        notebook.id, ArtifactRequest(kind="briefing", source_ids=[report.id])
    )
    assert artifact.source_ids == [report.id]


async def test_audio_without_tts_has_no_audio_but_explicit_null_in_json(engine: Any) -> None:
    notebook, _ = await _notebook_with_sources(engine)
    artifact = await engine.artifacts.generate(notebook.id, ArtifactRequest(kind="audio_overview"))
    assert artifact.status == "ready"
    assert artifact.script is not None
    assert artifact.audio is None
    assert await engine.artifacts.get_audio(artifact.id) is None
    data = artifact.to_json()
    assert "audio" in data and data["audio"] is None


async def test_audio_with_tts_renders_and_serves_the_bytes(fake_tts: Any) -> None:
    engine = create_engine(storage=MemoryStorage(), chat_client=MockChatClient(delay=0), tts=fake_tts, live_voice=None)
    notebook, _ = await _notebook_with_sources(engine)
    artifact = await engine.artifacts.generate(notebook.id, ArtifactRequest(kind="audio_overview"))
    assert artifact.audio is not None
    assert artifact.audio.provider == "fake"
    assert artifact.audio.mime_type == "audio/mpeg"
    assert artifact.audio.bytes > 0
    data, mime = await engine.artifacts.get_audio(artifact.id)  # type: ignore[misc]
    assert mime == "audio/mpeg"
    assert len(data) == artifact.audio.bytes
    assert artifact.to_json()["audio"]["blobKey"] == f"audio/{artifact.id}.mp3"


async def test_audio_tts_failure_keeps_the_script(fake_tts: Any) -> None:
    fake_tts.error = RuntimeError("no voice today")
    engine = create_engine(storage=MemoryStorage(), chat_client=MockChatClient(delay=0), tts=fake_tts, live_voice=None)
    notebook, _ = await _notebook_with_sources(engine)
    artifact = await engine.artifacts.generate(notebook.id, ArtifactRequest(kind="audio_overview"))
    assert artifact.status == "ready"
    assert artifact.script is not None
    assert artifact.audio is None
    assert artifact.audio_error == "no voice today"


async def test_deleting_an_audio_artifact_removes_its_blob(fake_tts: Any) -> None:
    storage = MemoryStorage()
    engine = create_engine(storage=storage, chat_client=MockChatClient(delay=0), tts=fake_tts, live_voice=None)
    notebook, _ = await _notebook_with_sources(engine)
    artifact = await engine.artifacts.generate(notebook.id, ArtifactRequest(kind="audio_overview"))
    key = artifact.audio.blob_key  # type: ignore[union-attr]
    assert await storage.get_blob(key) is not None
    await engine.artifacts.delete(artifact.id)
    assert await storage.get_blob(key) is None
    assert await storage.get("artifacts", artifact.id) is None


async def test_artifact_left_generating_by_an_earlier_process_reads_as_interrupted(engine: Any) -> None:
    notebook, _ = await _notebook_with_sources(engine)
    ts = now_iso()
    orphan = Artifact(
        id=str(uuid.uuid4()),
        notebook_id=notebook.id,
        kind="summary",
        title="Summary",
        status="generating",
        progress="Writing the summary",
        request=ArtifactRequest(kind="summary"),
        created_at=ts,
        updated_at=ts,
    )
    await engine.storage.put("artifacts", orphan.to_json())

    fetched = await engine.artifacts.get(orphan.id)
    assert fetched.status == "error"
    assert "interrupted" in (fetched.error or "")
    assert fetched.progress is None
    listed = await engine.artifacts.list(notebook.id)
    assert [a.status for a in listed] == ["error"]
    stored = await engine.storage.get("artifacts", orphan.id)
    assert stored is not None and stored["status"] == "error"


async def test_listeners_receive_progress_snapshots_and_the_final_state(engine: Any) -> None:
    notebook, _ = await _notebook_with_sources(engine)
    seen: list[Artifact] = []
    artifact = await engine.artifacts.generate(
        notebook.id, ArtifactRequest(kind="summary"), on_progress=seen.append
    )
    assert seen, "the listener was called"
    progress = [a.progress for a in seen]
    assert "Reading sources" in progress
    assert "Writing the summary" in progress
    assert seen[-1].status == "ready"
    assert seen[-1].id == artifact.id


async def test_concurrent_artifacts_all_finish(engine: Any) -> None:
    notebook, _ = await _notebook_with_sources(engine)
    kinds = ["summary", "faq", "timeline", "study_guide"]
    results = await asyncio.gather(
        *(engine.artifacts.generate(notebook.id, ArtifactRequest(kind=k)) for k in kinds)  # type: ignore[arg-type]
    )
    assert [a.status for a in results] == ["ready"] * len(kinds)
    assert len({a.id for a in results}) == len(kinds)
    assert len(await engine.artifacts.list(notebook.id)) == len(kinds)


async def test_unknown_artifact_is_404(engine: Any) -> None:
    with pytest.raises(OvertoneError) as info:
        await engine.artifacts.get("nope")
    assert info.value.status == 404


async def test_audio_options_reach_the_script(engine: Any) -> None:
    notebook, _ = await _notebook_with_sources(engine)
    artifact = await engine.artifacts.generate(
        notebook.id, ArtifactRequest(kind="audio_overview", options=AudioOptions(format="solo", length="short"))
    )
    assert artifact.script is not None
    assert len(artifact.script.hosts) == 1
    assert {t.speaker for t in artifact.script.turns} == {0}


# ───────────────────────────── one_shot ─────────────────────────────


async def test_one_shot_runs_end_to_end_without_notebook_bookkeeping(engine: Any) -> None:
    result = await engine.one_shot([TextInput(REPORT, title="Report")], ArtifactRequest(kind="faq"))
    assert result.artifact.status == "ready"
    assert result.artifact.content and result.artifact.content.startswith("# ")
    assert [s.title for s in result.sources] == ["Report"]
    assert result.notebook.title == result.artifact.title
    assert result.audio is None


async def test_one_shot_default_request_is_an_audio_overview_and_returns_audio(fake_tts: Any) -> None:
    engine = create_engine(storage=MemoryStorage(), chat_client=MockChatClient(delay=0), tts=fake_tts, live_voice=None)
    result = await engine.one_shot([TextInput(REPORT)], title="Given title")
    assert result.artifact.kind == "audio_overview"
    assert result.notebook.title == "Given title"
    assert result.audio is not None
    data, mime = result.audio
    assert mime == "audio/mpeg" and data


async def test_one_shot_needs_at_least_one_source(engine: Any) -> None:
    with pytest.raises(OvertoneError) as info:
        await engine.one_shot([])
    assert (info.value.status, info.value.code) == (400, "no_sources")


# ───────────────────────────── capabilities ─────────────────────────────


def test_capabilities_without_tts_or_live_voice_are_null(engine: Any) -> None:
    data = engine.capabilities().to_json()
    assert data["name"] == "overtone"
    assert data["model"] == "mock"
    assert data["framework"] == "microsoft-agent-framework"
    assert data["tts"] is None
    assert data["live"] is None
    assert "version" in data


def test_capabilities_describe_tts_and_live_when_configured(fake_tts: Any) -> None:
    live = LiveVoice(api_key="test-key")
    engine = create_engine(
        storage=MemoryStorage(), chat_client=MockChatClient(delay=0), tts=fake_tts, live_voice=live
    )
    data = engine.capabilities().to_json()
    assert data["tts"]["provider"] == "fake"
    assert data["tts"]["defaultVoices"] == ["v1", "v2"]
    assert [v["id"] for v in data["tts"]["voices"]] == ["v1", "v2"]
    assert data["live"]["model"] == "gpt-live-1"
    assert data["live"]["defaultVoice"] == "marin"
    assert "marin" in data["live"]["voices"]


# ───────────────────────────── live (engine side) ─────────────────────────────


async def test_create_session_without_live_voice_is_501(engine: Any) -> None:
    notebook, _ = await _notebook_with_sources(engine)
    with pytest.raises(OvertoneError) as info:
        await engine.live.create_session(notebook.id, LiveSessionRequest(sdp="v=0 offer"))
    assert (info.value.status, info.value.code) == (501, "live_unavailable")


async def test_delegate_works_without_a_live_provider_and_cites_sources(engine: Any) -> None:
    notebook, _ = await _notebook_with_sources(engine)
    result = await engine.live.delegate(
        notebook.id,
        LiveDelegateRequest(transcript=[LiveTranscriptLine(role="user", text="What did revenue do?")]),
    )
    assert result.answer.strip()
    assert "[" not in result.answer
    assert result.citations and result.citations[0].index == 1


async def test_delegate_without_a_user_line_is_400(engine: Any) -> None:
    notebook, _ = await _notebook_with_sources(engine)
    for transcript in [[], [LiveTranscriptLine(role="assistant", text="Hi there.")]]:
        with pytest.raises(OvertoneError) as info:
            await engine.live.delegate(notebook.id, LiveDelegateRequest(transcript=transcript))
        assert (info.value.status, info.value.code) == (400, "empty_transcript")


async def test_delegate_uses_the_artifacts_sources_when_given_an_artifact(engine: Any) -> None:
    notebook, (report, memo) = await _notebook_with_sources(engine)
    artifact = await engine.artifacts.generate(notebook.id, ArtifactRequest(kind="summary", source_ids=[memo.id]))
    result = await engine.live.delegate(
        notebook.id,
        LiveDelegateRequest(
            transcript=[LiveTranscriptLine(role="user", text="What about the launch?")],
            artifact_id=artifact.id,
        ),
    )
    assert result.citations
    assert {c.source_id for c in result.citations} == {memo.id}


async def test_delegate_for_an_unknown_notebook_is_404(engine: Any) -> None:
    with pytest.raises(OvertoneError) as info:
        await engine.live.delegate(
            "nope", LiveDelegateRequest(transcript=[LiveTranscriptLine(role="user", text="Hi?")])
        )
    assert info.value.status == 404
