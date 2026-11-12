"""Tests for overtone.agents: runtime selection, request options, the agents, and error mapping.

Every test runs on MockChatClient or on a small chat client defined here that
returns canned output or raises on purpose. No test reaches a real model.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from agent_framework import BaseChatClient, ChatResponse, ChatResponseUpdate, Content, FunctionInvocationLayer, Message
from agent_framework import exceptions as af_exceptions

from overtone import MockChatClient, OvertoneError, create_runtime
from overtone.agents import (
    DEFAULT_MODEL,
    ModelRuntime,
    stream_answer,
    to_overtone_error,
    voice_answer,
    write_document,
    write_guide,
    write_script,
)
from overtone.grounding import SourceDoc
from overtone.models import AudioHost, AudioOptions

ENV_VARS = ("OVERTONE_MOCK", "OVERTONE_MODEL", "OVERTONE_FALLBACKS", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class ScriptedChatClient(FunctionInvocationLayer, BaseChatClient):  # type: ignore[type-arg, misc]
    """A chat client with canned text. It can raise, or report a finish reason, and it records what it was sent."""

    model = "scripted"

    def __init__(
        self, *, text: str = "ok", error: BaseException | None = None, finish_reason: str | None = None
    ) -> None:
        super().__init__()
        self.text = text
        self.error = error
        self.finish_reason = finish_reason
        self.seen: list[list[Message]] = []

    def _inner_get_response(self, *, messages: Any, stream: bool, options: Any, **kwargs: Any) -> Any:
        self.seen.append(list(messages))
        response_format = options.get("response_format")
        if stream:

            async def _updates():  # type: ignore[no-untyped-def]
                if self.error is not None:
                    raise self.error
                yield ChatResponseUpdate(
                    role="assistant", contents=[Content.from_text(self.text)], finish_reason=self.finish_reason
                )

            return self._build_response_stream(_updates(), response_format=response_format)

        async def _response() -> ChatResponse:
            if self.error is not None:
                raise self.error
            return ChatResponse(
                messages=[Message(role="assistant", contents=[self.text])],
                response_id="scripted",
                finish_reason=self.finish_reason,
                response_format=response_format,
            )

        return _response()


def _runtime(client: Any = None) -> ModelRuntime:
    return create_runtime(chat_client=client or MockChatClient(delay=0))


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


async def _collect(runtime: ModelRuntime, docs: list[SourceDoc], history: list[tuple[str, str]], message: str) -> list[dict]:
    return [event async for event in stream_answer(runtime, docs, history, message)]


# ───────────────────────────── create_runtime ─────────────────────────────


def test_explicit_mock_client_is_reported_as_mock() -> None:
    client = MockChatClient(delay=0)
    runtime = create_runtime(chat_client=client)
    assert runtime.provider == "mock"
    assert runtime.client() is client


def test_explicit_other_client_is_reported_as_custom() -> None:
    client = ScriptedChatClient()
    runtime = create_runtime(chat_client=client)
    assert runtime.provider == "custom"
    assert runtime.model == "scripted"
    assert runtime.client() is client


def test_overtone_mock_env_selects_the_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("OVERTONE_MOCK", "1")
    runtime = create_runtime()
    assert runtime.provider == "mock"
    assert isinstance(runtime.client(), MockChatClient)


def test_default_runtime_is_anthropic_with_the_default_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    runtime = create_runtime()
    assert (runtime.provider, runtime.model) == ("anthropic", DEFAULT_MODEL) == ("anthropic", "claude-opus-5-5")


def test_model_argument_beats_environment_which_beats_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("OVERTONE_MODEL", "env-model")
    assert create_runtime(model="arg-model").model == "arg-model"
    assert create_runtime().model == "env-model"


def test_creating_the_runtime_does_not_construct_the_anthropic_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    import overtone.agents as agents

    def _boom(model: str) -> None:
        raise AssertionError("client constructed eagerly")

    monkeypatch.setattr(agents, "_anthropic_client", _boom)
    runtime = create_runtime()
    assert runtime._client is None


def test_anthropic_client_without_credentials_fails_with_llm_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    runtime = create_runtime()
    with pytest.raises(OvertoneError) as info:
        runtime.client()
    assert (info.value.status, info.value.code) == (401, "llm_auth")
    assert "ANTHROPIC_API_KEY" in info.value.message


# ───────────────────────────── ModelRuntime.options ─────────────────────────────


def test_anthropic_options_carry_budget_effort_and_cache_control(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    runtime = create_runtime()
    opts = runtime.options("chat")
    assert opts["max_tokens"] == 16_000
    assert opts["output_config"] == {"effort": "medium"}
    assert opts["cache_control"] == {"type": "ephemeral"}
    assert opts["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in opts["additional_beta_flags"]


@pytest.mark.parametrize(
    ("purpose", "max_tokens", "effort"),
    [("chat", 16_000, "medium"), ("generation", 32_000, "high"), ("guide", 4_000, "low"), ("voice", 2_000, "low")],
)
def test_each_purpose_has_its_own_budget(monkeypatch: pytest.MonkeyPatch, purpose: str, max_tokens: int, effort: str) -> None:
    _clear_env(monkeypatch)
    opts = create_runtime().options(purpose)  # type: ignore[arg-type]
    assert (opts["max_tokens"], opts["output_config"]["effort"]) == (max_tokens, effort)


def test_fallbacks_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("OVERTONE_FALLBACKS", "0")
    opts = create_runtime().options("chat")
    assert "fallbacks" not in opts
    assert "additional_beta_flags" not in opts


@pytest.mark.parametrize("purpose", ["chat", "generation", "guide", "voice"])
def test_non_anthropic_providers_only_get_max_tokens(purpose: str) -> None:
    assert set(_runtime().options(purpose)) == {"max_tokens"}  # type: ignore[arg-type]
    assert set(_runtime(ScriptedChatClient()).options(purpose)) == {"max_tokens"}  # type: ignore[arg-type]


# ───────────────────────────── stream_answer ─────────────────────────────


async def test_stream_answer_yields_text_and_citations_that_agree(sample_docs: list[SourceDoc]) -> None:
    events = await _collect(_runtime(), sample_docs, [], "What do the sources say about revenue?")
    text = "".join(e["delta"] for e in events if e["type"] == "text")
    citations = [e["citation"] for e in events if e["type"] == "citation"]

    assert citations, "the mock cites the first passage of each source"
    indices = sorted({int(n) for n in re.findall(r"\[(\d+)\]", text)})
    assert indices == [c["index"] for c in citations] == list(range(1, len(citations) + 1))
    by_index = {c["index"]: c for c in citations}
    for idx in indices:
        cited = by_index[idx]
        doc = next(d for d in sample_docs if d.id == cited["sourceId"])
        assert doc.text[cited["startChar"] : cited["endChar"]] == cited["quote"]


async def test_stream_answer_text_is_not_leaked_as_raw_passage_numbers(sample_docs: list[SourceDoc]) -> None:
    events = await _collect(_runtime(), sample_docs, [], "Summarise.")
    text = "".join(e["delta"] for e in events if e["type"] == "text")
    assert "This is a mock answer" in text
    assert "[1]" in text


async def test_history_with_old_markers_is_replayed_without_them(sample_docs: list[SourceDoc]) -> None:
    client = ScriptedChatClient(text="Fine.")
    history = [("user", "First question"), ("assistant", "Revenue grew [1][2] and costs fell [3]."), ]
    await _collect(_runtime(client), sample_docs, history, "Follow-up?")

    sent = client.seen[-1]
    user_turns = [m for m in sent if m.role == "user"]
    assistant_turns = [m for m in sent if m.role == "assistant"]
    assert assistant_turns, "history is replayed"
    for message in assistant_turns:
        assert not re.search(r"\[\d+\]", message.text)
    assert "Revenue grew and costs fell." in assistant_turns[0].text
    # The sources ride in the first user turn only, so the grounded prefix stays stable.
    assert "<sources>" in user_turns[0].text
    assert all("<sources>" not in m.text for m in user_turns[1:])


async def test_empty_sources_raise_no_sources() -> None:
    docs = [SourceDoc("a", "Empty", "   "), SourceDoc("b", "Blank", "")]
    with pytest.raises(OvertoneError) as info:
        await _collect(_runtime(), docs, [], "Anything?")
    assert (info.value.status, info.value.code) == (400, "no_sources")


async def test_content_filter_finish_is_a_refusal(sample_docs: list[SourceDoc]) -> None:
    client = ScriptedChatClient(text="", finish_reason="content_filter")
    with pytest.raises(OvertoneError) as info:
        await _collect(_runtime(client), sample_docs, [], "Question?")
    assert (info.value.status, info.value.code) == (422, "refusal")


async def test_provider_exception_during_stream_maps_to_llm_error(sample_docs: list[SourceDoc]) -> None:
    client = ScriptedChatClient(error=RuntimeError("upstream exploded"))
    with pytest.raises(OvertoneError) as info:
        await _collect(_runtime(client), sample_docs, [], "Question?")
    assert (info.value.status, info.value.code) == (502, "llm_error")
    assert "upstream exploded" in info.value.message


# ───────────────────────────── write_script ─────────────────────────────


async def test_script_has_two_hosts_by_default(sample_docs: list[SourceDoc]) -> None:
    script = await write_script(_runtime(), sample_docs, AudioOptions())
    assert [h.name for h in script.hosts] == ["Maya", "Jonah"]
    assert {t.speaker for t in script.turns} <= {0, 1}
    assert {t.speaker for t in script.turns} == {0, 1}
    assert script.title and script.summary
    assert all(t.text.strip() for t in script.turns)


async def test_solo_format_has_one_host_and_only_speaker_zero(sample_docs: list[SourceDoc]) -> None:
    script = await write_script(_runtime(), sample_docs, AudioOptions(format="solo"))
    assert len(script.hosts) == 1
    assert {t.speaker for t in script.turns} == {0}


async def test_custom_host_names_and_voices_are_carried_through(sample_docs: list[SourceDoc]) -> None:
    options = AudioOptions(hosts=[AudioHost(name="Ada", voice="sage"), AudioHost(name="Bo", voice="ash")])
    script = await write_script(_runtime(), sample_docs, options)
    assert [(h.name, h.voice) for h in script.hosts] == [("Ada", "sage"), ("Bo", "ash")]


async def test_solo_keeps_only_the_first_custom_host(sample_docs: list[SourceDoc]) -> None:
    options = AudioOptions(format="solo", hosts=[AudioHost(name="Ada", voice="sage"), AudioHost(name="Bo")])
    script = await write_script(_runtime(), sample_docs, options)
    assert [(h.name, h.voice) for h in script.hosts] == [("Ada", "sage")]


async def test_malformed_structured_output_is_invalid_model_output(sample_docs: list[SourceDoc]) -> None:
    with pytest.raises(OvertoneError) as info:
        await write_script(_runtime(ScriptedChatClient(text="this is not json")), sample_docs, AudioOptions())
    assert (info.value.status, info.value.code) == (502, "invalid_model_output")


async def test_wrong_shape_structured_output_is_invalid_model_output(sample_docs: list[SourceDoc]) -> None:
    client = ScriptedChatClient(text='{"title": "Only a title"}')
    with pytest.raises(OvertoneError) as info:
        await write_script(_runtime(client), sample_docs, AudioOptions())
    assert info.value.code == "invalid_model_output"


async def test_script_with_no_speakable_turns_is_empty_script(sample_docs: list[SourceDoc]) -> None:
    client = ScriptedChatClient(text='{"title": "T", "summary": "S", "turns": [{"speaker": "host_1", "text": "   "}]}')
    with pytest.raises(OvertoneError) as info:
        await write_script(_runtime(client), sample_docs, AudioOptions())
    assert info.value.code == "empty_script"


async def test_truncated_generation_is_reported(sample_docs: list[SourceDoc]) -> None:
    client = ScriptedChatClient(text="# Half a doc", finish_reason="length")
    with pytest.raises(OvertoneError) as info:
        await write_document(_runtime(client), sample_docs, "summary", None)
    assert info.value.code == "truncated"


# ───────────────────────────── write_document / write_guide / voice_answer ─────────────────────────────


async def test_document_returns_title_and_markdown(sample_docs: list[SourceDoc]) -> None:
    title, content = await write_document(_runtime(), sample_docs, "briefing", None)
    assert title == "Mock document"
    assert content.startswith("# Mock document")


async def test_custom_document_passes_its_directions_to_the_model(sample_docs: list[SourceDoc]) -> None:
    client = ScriptedChatClient(text="# Plan\n\nBody.")
    title, content = await write_document(_runtime(client), sample_docs, "custom", "Write a haiku about revenue")
    assert (title, content) == ("Plan", "# Plan\n\nBody.")
    prompt = client.seen[-1][-1].text
    assert "Write a haiku about revenue" in prompt


async def test_guide_returns_summary_and_questions(sample_docs: list[SourceDoc]) -> None:
    summary, questions = await write_guide(_runtime(), sample_docs)
    assert "2 sources" in summary
    assert 1 <= len(questions) <= 5
    assert all(q.strip() for q in questions)


async def test_guide_drops_blank_questions_and_caps_at_five(sample_docs: list[SourceDoc]) -> None:
    import json

    payload = json.dumps({"summary": " S ", "questions": ["", "a", " b ", "c", "d", "e", "f", "  "]})
    summary, questions = await write_guide(_runtime(ScriptedChatClient(text=payload)), sample_docs)
    assert summary == "S"
    assert questions == ["a", "b", "c", "d", "e"]


async def test_voice_answer_is_speakable_and_cited(sample_docs: list[SourceDoc]) -> None:
    answer, citations = await voice_answer(
        _runtime(), sample_docs, [("user", "What does the report say about hiring?")]
    )
    assert answer
    assert not re.search(r"\[\d+\]", answer)
    assert "\n" not in answer
    assert citations and citations[0].index == 1
    assert citations[0].quote in " ".join(d.text for d in sample_docs)


# ───────────────────────────── to_overtone_error ─────────────────────────────


class RateLimitError(Exception):
    """Named like a provider SDK error; to_overtone_error matches on the class name."""


class APIConnectionError(Exception):
    pass


class APITimeoutError(Exception):
    pass


def test_overtone_errors_pass_through_unchanged() -> None:
    original = OvertoneError("mine", status=418, code="teapot")
    assert to_overtone_error(original) is original


def test_invalid_auth_maps_to_llm_auth() -> None:
    mapped = to_overtone_error(af_exceptions.ChatClientInvalidAuthException("bad key"))
    assert (mapped.status, mapped.code) == (401, "llm_auth")


def test_content_filter_maps_to_refusal() -> None:
    mapped = to_overtone_error(af_exceptions.ChatClientContentFilterException("filtered"))
    assert (mapped.status, mapped.code) == (422, "refusal")


def test_rate_limit_maps_to_429() -> None:
    mapped = to_overtone_error(RateLimitError("slow down"))
    assert (mapped.status, mapped.code) == (429, "llm_rate_limited")


@pytest.mark.parametrize("cls", [APIConnectionError, APITimeoutError])
def test_connection_failures_map_to_unreachable(cls: type[Exception]) -> None:
    mapped = to_overtone_error(cls("socket closed"))
    assert (mapped.status, mapped.code) == (502, "llm_unreachable")


def test_network_error_hidden_in_the_cause_chain_is_still_found() -> None:
    try:
        try:
            raise APIConnectionError("reset")
        except APIConnectionError as inner:
            raise RuntimeError("workflow step failed") from inner
    except RuntimeError as outer:
        mapped = to_overtone_error(outer)
    assert mapped.code == "llm_unreachable"


def test_unknown_failures_become_llm_error_with_the_detail() -> None:
    mapped = to_overtone_error(ValueError("weird payload"))
    assert (mapped.status, mapped.code) == (502, "llm_error")
    assert "weird payload" in mapped.message


def test_empty_message_falls_back_to_the_exception_name() -> None:
    mapped = to_overtone_error(KeyError())
    assert mapped.message.endswith("KeyError")


def test_long_failure_detail_is_truncated() -> None:
    mapped = to_overtone_error(ValueError("x" * 5_000))
    assert len(mapped.message) < 600
