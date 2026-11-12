"""The Overtone agents, built on Microsoft Agent Framework.

Three agents share one chat client:

* **researcher**: answers questions from the notebook's sources with citations;
* **producer**: writes audio scripts, documents and the notebook guide;
* **voice researcher**: answers questions delegated by the live voice model.

`ModelRuntime` owns the chat client. The default is Claude through
`agent_framework_anthropic.AnthropicClient`, but any Agent Framework chat client
(Azure OpenAI, OpenAI, Bedrock, Ollama, …) can be passed in instead.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

from agent_framework import Agent, Message
from agent_framework import exceptions as af_exceptions
from pydantic import BaseModel, ValidationError

from .errors import OvertoneError
from .grounding import CitationStream, SourceDoc, render_sources, split_passages, strip_markers, usable_docs
from .models import AudioOptions, AudioScript, Citation, DialogueTurn
from .prompts import (
    GENERATION_INSTRUCTIONS,
    GUIDE_PROMPT,
    RESEARCHER_INSTRUCTIONS,
    VOICE_RESEARCHER_INSTRUCTIONS,
    audio_script_prompt,
    document_prompt,
    resolve_hosts,
)

DEFAULT_MODEL = "claude-opus-5-5"
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

Purpose = Literal["chat", "generation", "guide", "voice"]

# (max_tokens, effort) per kind of request. Effort is Claude's depth/cost dial.
_BUDGET: dict[str, tuple[int, str]] = {
    "chat": (16_000, "medium"),
    "generation": (32_000, "high"),
    "guide": (4_000, "low"),
    "voice": (2_000, "low"),
}


# ───────────────────────── Structured output schemas ─────────────────────────


class ScriptTurnOut(BaseModel):
    speaker: Literal["host_1", "host_2"]
    text: str


class ScriptOut(BaseModel):
    """Episode script. `title` under 70 characters; `summary` one or two sentences; `turns` in speaking order,
    each a host's plain spoken prose with no markup or stage directions."""

    title: str
    summary: str
    turns: list[ScriptTurnOut]


class GuideOut(BaseModel):
    summary: str
    questions: list[str]


# ───────────────────────────────── Runtime ───────────────────────────────────


@dataclass
class ModelRuntime:
    """The chat client the agents run on, plus the request options that suit it."""

    provider: str  # "anthropic" | "mock" | "custom"
    model: str
    _client: Any = None
    _extra: dict[str, Any] = field(default_factory=dict)

    def client(self) -> Any:
        """The Agent Framework chat client, created on first use so a missing key fails at call time
        with a clear message instead of preventing the server from starting."""
        if self._client is None:
            if self.provider == "mock":
                from .mock import MockChatClient

                self._client = MockChatClient()
            else:
                self._client = _anthropic_client(self.model)
        return self._client

    def options(self, purpose: Purpose) -> dict[str, Any]:
        max_tokens, effort = _BUDGET[purpose]
        if self.provider != "anthropic":
            return {"max_tokens": max_tokens}
        options: dict[str, Any] = {
            "max_tokens": max_tokens,
            # Thinking is adaptive by default on current Claude models; effort controls its depth.
            "output_config": {"effort": effort},
            # The sources open every request, so cache that prefix across turns.
            "cache_control": {"type": "ephemeral"},
        }
        if os.environ.get("OVERTONE_FALLBACKS") != "0":
            # If the model declines a request, let the API re-run it on its default fallback model.
            options["fallbacks"] = "default"
            options["additional_beta_flags"] = [_FALLBACK_BETA]
        return options


def create_runtime(*, chat_client: Any = None, model: str | None = None) -> ModelRuntime:
    """Pick the chat client: an explicit one, the mock (OVERTONE_MOCK=1), or Claude."""
    if chat_client is not None:
        name = model or str(getattr(chat_client, "model", None) or getattr(chat_client, "model_id", None) or "custom")
        provider = "mock" if type(chat_client).__name__ == "MockChatClient" else "custom"
        return ModelRuntime(provider=provider, model=name, _client=chat_client)
    if os.environ.get("OVERTONE_MOCK") == "1":
        return ModelRuntime(provider="mock", model="mock")
    return ModelRuntime(provider="anthropic", model=model or os.environ.get("OVERTONE_MODEL") or DEFAULT_MODEL)


def _anthropic_client(model: str) -> Any:
    from agent_framework_anthropic import AnthropicClient

    try:
        return AnthropicClient(model=model)
    except Exception as exc:  # missing/invalid settings surface here
        raise OvertoneError(
            "No Claude credentials found. Set ANTHROPIC_API_KEY and restart "
            "(or set OVERTONE_MOCK=1 to try the app without a model).",
            status=401,
            code="llm_auth",
        ) from exc


def _agent(runtime: ModelRuntime, name: str, description: str, instructions: str) -> Agent:
    return Agent(client=runtime.client(), name=name, description=description, instructions=instructions)


def researcher(runtime: ModelRuntime) -> Agent:
    return _agent(
        runtime,
        "researcher",
        "Answers questions from the notebook's sources, citing the passages it relies on.",
        RESEARCHER_INSTRUCTIONS,
    )


def producer(runtime: ModelRuntime) -> Agent:
    return _agent(
        runtime,
        "producer",
        "Writes audio scripts, briefings, study guides and other documents from the notebook's sources.",
        GENERATION_INSTRUCTIONS,
    )


def voice_researcher(runtime: ModelRuntime) -> Agent:
    return _agent(
        runtime,
        "voice-researcher",
        "Answers questions delegated by the live voice model, briefly and in speakable prose.",
        VOICE_RESEARCHER_INSTRUCTIONS,
    )


# ────────────────────────────── Running agents ───────────────────────────────


def _grounded(docs: list[SourceDoc]) -> tuple[str, list[Any]]:
    usable = usable_docs(docs)
    passages = split_passages(usable)
    return render_sources(usable, passages), passages


def to_overtone_error(exc: BaseException) -> Exception:
    """Map framework and provider failures onto caller-facing errors, most specific first."""
    if isinstance(exc, OvertoneError):
        return exc
    chain: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and cur not in chain:
        chain.append(cur)
        cur = getattr(cur, "inner_exception", None) or cur.__cause__
    names = {type(e).__name__ for e in chain}
    if any(isinstance(e, (af_exceptions.ChatClientInvalidAuthException, af_exceptions.AgentInvalidAuthException)) for e in chain):
        return OvertoneError(
            "The model provider rejected the credentials. Check ANTHROPIC_API_KEY and restart.",
            status=401,
            code="llm_auth",
        )
    if any(
        isinstance(e, (af_exceptions.ChatClientContentFilterException, af_exceptions.AgentContentFilterException))
        for e in chain
    ):
        return OvertoneError("The model declined this request.", status=422, code="refusal")
    if "RateLimitError" in names:
        return OvertoneError(
            "The model provider is rate limiting requests right now. Wait a moment and try again.",
            status=429,
            code="llm_rate_limited",
        )
    if names & {"APIConnectionError", "APITimeoutError", "ConnectError"}:
        return OvertoneError(
            "Could not reach the model provider. Check your network connection.", status=502, code="llm_unreachable"
        )
    detail = str(exc).strip() or type(exc).__name__
    return OvertoneError(f"The model request failed: {detail[:500]}", status=502, code="llm_error")


async def _run_to_completion(agent: Agent, messages: list[Message], options: dict[str, Any]) -> Any:
    """Run an agent with streaming (long generations would time out otherwise) and return the final response."""
    finish: str | None = None
    try:
        stream = agent.run(messages, stream=True, options=options)
        async for update in stream:
            finish = getattr(update, "finish_reason", None) or finish
        final = await stream.get_final_response()
    except Exception as exc:
        raise to_overtone_error(exc) from exc
    finish = getattr(final, "finish_reason", None) or finish
    if finish == "content_filter":
        raise OvertoneError("The model declined to generate this.", status=422, code="refusal")
    if finish == "length":
        raise OvertoneError(
            "The result was cut off before it finished. Try a shorter length.", status=502, code="truncated"
        )
    return final


def _structured(final: Any, schema: type[BaseModel]) -> Any:
    try:
        # Reading `.value` makes the framework parse the text, which raises on malformed output.
        value = getattr(final, "value", None)
        if isinstance(value, schema):
            return value
        return schema.model_validate(json.loads(final.text))
    except (ValueError, ValidationError, TypeError) as exc:
        raise OvertoneError(
            "The model returned output that did not match the expected format.", status=502, code="invalid_model_output"
        ) from exc


async def stream_answer(
    runtime: ModelRuntime,
    docs: list[SourceDoc],
    history: list[tuple[str, str]],
    message: str,
) -> AsyncIterator[dict[str, Any]]:
    """Stream a grounded answer as chat events (`text` and `citation`).

    `history` is prior turns as (role, content), oldest first. The sources ride in
    the first user turn so the grounded prefix stays stable across the conversation.
    """
    sources_block, passages = _grounded(docs)
    turns = [*history, ("user", message)]
    messages: list[Message] = []
    for i, (role, content) in enumerate(turns):
        text = strip_markers(content) if role == "assistant" else content
        contents = [sources_block, text] if i == 0 else [text]
        messages.append(Message(role=role, contents=contents))

    citations = CitationStream(passages)
    finish: str | None = None
    try:
        stream = researcher(runtime).run(messages, stream=True, options=runtime.options("chat"))
        async for update in stream:
            finish = getattr(update, "finish_reason", None) or finish
            if update.text:
                for event in citations.feed(update.text):
                    yield event
        for event in citations.flush():
            yield event
        final = await stream.get_final_response()
        finish = getattr(final, "finish_reason", None) or finish
    except Exception as exc:
        raise to_overtone_error(exc) from exc
    if finish == "content_filter":
        raise OvertoneError("The model declined to answer this request.", status=422, code="refusal")


async def write_script(runtime: ModelRuntime, docs: list[SourceDoc], options: AudioOptions) -> AudioScript:
    sources_block, _ = _grounded(docs)
    hosts = resolve_hosts(options)
    final = await _run_to_completion(
        producer(runtime),
        [Message(role="user", contents=[sources_block, audio_script_prompt(options, hosts)])],
        {**runtime.options("generation"), "response_format": ScriptOut},
    )
    raw: ScriptOut = _structured(final, ScriptOut)
    turns = [
        # A solo narration only ever has host_1.
        DialogueTurn(speaker=0 if len(hosts) == 1 or t.speaker == "host_1" else 1, text=strip_markers(t.text).strip())
        for t in raw.turns
        if t.text.strip()
    ]
    if not turns:
        raise OvertoneError("The model returned an empty script.", status=502, code="empty_script")
    return AudioScript(title=raw.title.strip(), summary=raw.summary.strip(), hosts=hosts, turns=turns)


async def write_document(
    runtime: ModelRuntime, docs: list[SourceDoc], kind: str, instructions: str | None
) -> tuple[str, str]:
    """Returns (title, markdown)."""
    sources_block, _ = _grounded(docs)
    final = await _run_to_completion(
        producer(runtime),
        [Message(role="user", contents=[sources_block, document_prompt(kind, instructions)])],
        runtime.options("generation"),
    )
    content = final.text.strip()
    title = next((line[2:].strip() for line in content.splitlines() if line.startswith("# ")), "")
    return title, content


async def write_guide(runtime: ModelRuntime, docs: list[SourceDoc]) -> tuple[str, list[str]]:
    """Returns (summary, suggested questions)."""
    sources_block, _ = _grounded(docs)
    final = await _run_to_completion(
        producer(runtime),
        [Message(role="user", contents=[sources_block, GUIDE_PROMPT])],
        {**runtime.options("guide"), "response_format": GuideOut},
    )
    guide: GuideOut = _structured(final, GuideOut)
    return guide.summary.strip(), [q.strip() for q in guide.questions if q.strip()][:5]


async def voice_answer(
    runtime: ModelRuntime, docs: list[SourceDoc], transcript: list[tuple[str, str]]
) -> tuple[str, list[Citation]]:
    """Answer the latest question in a live voice transcript. Returns (speakable answer, citations)."""
    sources_block, passages = _grounded(docs)
    lines = "\n".join(f"{'User' if role == 'user' else 'Voice assistant'}: {text}" for role, text in transcript)
    prompt = (
        "Here is the recent conversation, oldest first:\n<transcript>\n"
        f"{lines}\n</transcript>\n\nAnswer the user's latest question."
    )
    final = await _run_to_completion(
        voice_researcher(runtime),
        [Message(role="user", contents=[sources_block, prompt])],
        runtime.options("voice"),
    )
    citations = CitationStream(passages)
    citations.feed(final.text)
    citations.flush()
    return " ".join(strip_markers(final.text).split()), citations.citations
