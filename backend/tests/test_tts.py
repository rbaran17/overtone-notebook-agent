"""Tests for overtone.tts (port of packages/core/test/tts.test.ts)."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable

import httpx
import pytest

from overtone import tts as tts_mod
from overtone.errors import OvertoneError
from overtone.models import AudioHost, AudioScript, DialogueTurn, TtsConfig, TtsProvider
from overtone.tts import (
    ElevenLabsTts,
    OpenAITts,
    concat_mp3,
    create_tts_provider,
    split_text,
    strip_id3,
    synthesize_dialogue,
)

ENV_KEYS = (
    "OPENAI_API_KEY",
    "ELEVENLABS_API_KEY",
    "OVERTONE_TTS_PROVIDER",
    "OVERTONE_TTS_MODEL",
    "OVERTONE_TTS_VOICES",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with the TTS env vars unset."""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record backoff delays instead of sleeping, so retry tests run instantly."""
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(tts_mod, "_sleep", fake_sleep)
    return recorded


class FakeProvider:
    """In-memory provider: records calls, tracks concurrency, optional delay and failure."""

    id = "fake"
    label = "Fake"
    voices: list = []
    mime_type = "audio/mpeg"

    def __init__(
        self,
        *,
        max_chars: int | None = None,
        default_voices: list[str] | None = None,
        delay: Callable[[str], float] | None = None,
        fail: Callable[[str], bool] | None = None,
    ) -> None:
        self.default_voices = default_voices if default_voices is not None else ["d0", "d1"]
        if max_chars is not None:
            self.max_chars = max_chars  # absent otherwise, exercising the default
        self._delay = delay
        self._fail = fail
        self.calls: list[tuple[str, str]] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def synthesize(self, text: str, voice: str) -> bytes:
        self.calls.append((text, voice))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            ms = self._delay(text) if self._delay else 0
            if ms > 0:
                await asyncio.sleep(ms / 1000)
            if self._fail and self._fail(text):
                raise RuntimeError(f"boom: {text}")
            return f"[{text}]".encode()
        finally:
            self.in_flight -= 1


def make_script(
    turns: list[tuple[int, str]],
    hosts: list[AudioHost] | None = None,
) -> AudioScript:
    if hosts is None:
        hosts = [AudioHost(name="A"), AudioHost(name="B")]
    return AudioScript(
        title="t",
        summary="s",
        hosts=hosts,
        turns=[DialogueTurn(speaker=speaker, text=text) for speaker, text in turns],
    )


class Recorder:
    """httpx.MockTransport handler that replays queued responses and records requests."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.queue = list(responses)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.queue:
            raise AssertionError("unexpected extra request")
        return self.queue.pop(0)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def audio() -> httpx.Response:
    return httpx.Response(200, content=bytes([0xFF, 0xFB, 1, 2]))


# ───────────────────────── create_tts_provider ─────────────────────────


def _env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_none_returns_null_even_with_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, OPENAI_API_KEY="k1", ELEVENLABS_API_KEY="k2", OVERTONE_TTS_PROVIDER="none")
    assert create_tts_provider() is None

    monkeypatch.delenv("OVERTONE_TTS_PROVIDER")
    _env(monkeypatch, OPENAI_API_KEY="k1")
    assert create_tts_provider(TtsConfig(provider="none")) is None


def test_auto_with_no_keys_returns_none() -> None:
    assert create_tts_provider() is None
    assert create_tts_provider(TtsConfig(provider="auto")) is None


def test_auto_prefers_openai_when_both_keys_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, OPENAI_API_KEY="o", ELEVENLABS_API_KEY="e")
    provider = create_tts_provider()
    assert provider is not None
    assert provider.id == "openai"


def test_auto_falls_back_to_elevenlabs(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, ELEVENLABS_API_KEY="e")
    provider = create_tts_provider()
    assert provider is not None
    assert provider.id == "elevenlabs"
    assert provider.label == "ElevenLabs"


def test_explicit_provider_without_key_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, ELEVENLABS_API_KEY="e")
    assert create_tts_provider(TtsConfig(provider="openai")) is None

    monkeypatch.delenv("ELEVENLABS_API_KEY")
    _env(monkeypatch, OPENAI_API_KEY="o")
    assert create_tts_provider(TtsConfig(provider="elevenlabs")) is None


def test_explicit_provider_uses_config_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    p1 = create_tts_provider(TtsConfig(provider="openai", api_key="cfg"))
    p2 = create_tts_provider(TtsConfig(provider="elevenlabs", api_key="cfg"))
    assert p1 is not None and p1.id == "openai"
    assert p2 is not None and p2.id == "elevenlabs"


def test_explicit_config_provider_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(
        monkeypatch,
        OVERTONE_TTS_PROVIDER="elevenlabs",
        ELEVENLABS_API_KEY="e",
        OPENAI_API_KEY="o",
    )
    p = create_tts_provider(TtsConfig(provider="openai"))
    assert p is not None and p.id == "openai"
    q = create_tts_provider()
    assert q is not None and q.id == "elevenlabs"


def test_explicit_provider_chooses_its_own_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, OPENAI_API_KEY="o", ELEVENLABS_API_KEY="e")
    p = create_tts_provider(TtsConfig(provider="elevenlabs"))
    assert p is not None and p.id == "elevenlabs"


def test_model_config_overrides_env_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, OPENAI_API_KEY="o", OVERTONE_TTS_MODEL="env-model")
    from_env = create_tts_provider()
    assert isinstance(from_env, OpenAITts)
    assert from_env.model == "env-model"

    from_cfg = create_tts_provider(TtsConfig(model="cfg-model"))
    assert isinstance(from_cfg, OpenAITts)
    assert from_cfg.model == "cfg-model"

    monkeypatch.delenv("OVERTONE_TTS_MODEL")
    default = create_tts_provider()
    assert isinstance(default, OpenAITts)
    assert default.model == "gpt-4o-mini-tts"


async def test_model_is_sent_in_request_body() -> None:
    rec = Recorder([audio(), audio()])
    await OpenAITts("o", model="env-model", transport=rec.transport).synthesize("hi", "coral")
    await OpenAITts("o", model="cfg-model", transport=rec.transport).synthesize("hi", "coral")
    assert json.loads(rec.requests[0].content)["model"] == "env-model"
    assert json.loads(rec.requests[1].content)["model"] == "cfg-model"


def test_voices_config_overrides_env_and_env_replaces_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, OPENAI_API_KEY="o", OVERTONE_TTS_VOICES=" nova , onyx ,,")
    p = create_tts_provider()
    assert p is not None and p.default_voices == ["nova", "onyx"]
    q = create_tts_provider(TtsConfig(voices=["sage"]))
    assert q is not None and q.default_voices == ["sage"]


def test_elevenlabs_default_voices(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, ELEVENLABS_API_KEY="e")
    p = create_tts_provider()
    assert p is not None
    assert p.default_voices == ["21m00Tcm4TlvDq8ikWAM", "pNInz6obpgDQGcFmaJgB"]


def test_unknown_provider_value_falls_back_to_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, OVERTONE_TTS_PROVIDER="bogus", ELEVENLABS_API_KEY="e")
    p = create_tts_provider()
    assert p is not None and p.id == "elevenlabs"


def test_providers_satisfy_protocol() -> None:
    assert isinstance(OpenAITts("k"), TtsProvider)
    assert isinstance(ElevenLabsTts("k"), TtsProvider)


# ───────────────────────── synthesize_dialogue ─────────────────────────


async def test_rejects_empty_script_with_empty_script_code() -> None:
    provider = FakeProvider()
    with pytest.raises(OvertoneError) as exc:
        await synthesize_dialogue(provider, make_script([]))
    assert exc.value.code == "empty_script"
    assert exc.value.status == 400


async def test_keeps_output_in_script_order_when_early_turns_finish_last() -> None:
    delays = {"a": 60, "b": 1, "c": 30, "d": 5}
    provider = FakeProvider(delay=lambda t: delays.get(t, 0))
    data, mime = await synthesize_dialogue(
        provider,
        make_script([(0, "a"), (1, "b"), (0, "c"), (1, "d")]),
        concurrency=4,
    )
    assert data == b"[a][b][c][d]"
    assert mime == "audio/mpeg"


async def test_never_exceeds_concurrency_limit() -> None:
    provider = FakeProvider(delay=lambda _t: 5)
    turns = [(i % 2, f"t{i}") for i in range(12)]
    await synthesize_dialogue(provider, make_script(turns), concurrency=2)
    assert provider.max_in_flight == 2


async def test_defaults_concurrency_to_4() -> None:
    provider = FakeProvider(delay=lambda _t: 5)
    turns = [(0, f"t{i}") for i in range(12)]
    await synthesize_dialogue(provider, make_script(turns))
    assert provider.max_in_flight == 4


async def test_uses_per_host_voices_and_falls_back_to_defaults_by_index() -> None:
    provider = FakeProvider(default_voices=["d0", "d1"])
    await synthesize_dialogue(
        provider,
        make_script(
            [(0, "x0"), (1, "x1"), (2, "x2")],
            [AudioHost(name="A", voice="custom"), AudioHost(name="B"), AudioHost(name="C")],
        ),
        concurrency=1,
    )
    assert [voice for _text, voice in provider.calls] == ["custom", "d1", "d0"]


async def test_empty_host_voice_uses_provider_default() -> None:
    provider = FakeProvider(default_voices=["d0", "d1"])
    await synthesize_dialogue(
        provider,
        make_script([(0, "x0"), (1, "x1")], [AudioHost(name="A", voice=""), AudioHost(name="B", voice=None)]),
        concurrency=1,
    )
    assert [voice for _text, voice in provider.calls] == ["d0", "d1"]


async def test_no_voice_available_raises_tts_no_voice() -> None:
    provider = FakeProvider(default_voices=[])
    with pytest.raises(OvertoneError) as exc:
        await synthesize_dialogue(provider, make_script([(0, "x")], [AudioHost(name="A")]))
    assert exc.value.code == "tts_no_voice"
    assert exc.value.status == 500


async def test_reports_progress_monotonically_up_to_total() -> None:
    provider = FakeProvider(delay=lambda t: 30 if t == "a" else 0)
    seen: list[tuple[int, int]] = []
    await synthesize_dialogue(
        provider,
        make_script([(0, "a"), (1, "b"), (0, "c")]),
        on_progress=lambda d, t: seen.append((d, t)),
        concurrency=3,
    )
    assert len(seen) == 3
    assert seen[-1] == (3, 3)
    for prev, cur in zip(seen, seen[1:]):
        assert cur[0] >= prev[0]
    assert all(t == 3 for _d, t in seen)


async def test_empty_turns_are_skipped_but_counted_in_progress() -> None:
    provider = FakeProvider()
    seen: list[tuple[int, int]] = []
    data, _mime = await synthesize_dialogue(
        provider,
        make_script([(0, "   "), (1, "hello"), (0, "")]),
        on_progress=lambda d, t: seen.append((d, t)),
        concurrency=1,
    )
    assert [text for text, _voice in provider.calls] == ["hello"]
    assert data == b"[hello]"
    assert seen[0] == (2, 3)  # both empty turns counted up front
    assert seen[-1] == (3, 3)


async def test_all_empty_turns_yield_empty_audio() -> None:
    provider = FakeProvider()
    data, mime = await synthesize_dialogue(provider, make_script([(0, " "), (1, "")]))
    assert data == b""
    assert mime == "audio/mpeg"
    assert provider.calls == []


async def test_progress_callback_exception_does_not_break_synthesis() -> None:
    provider = FakeProvider()

    def broken(_done: int, _total: int) -> None:
        raise ValueError("ui went away")

    data, _mime = await synthesize_dialogue(provider, make_script([(0, "a"), (1, "b")]), on_progress=broken)
    assert data == b"[a][b]"


async def test_chunks_long_turns_under_max_chars_without_losing_words_in_order() -> None:
    sentences = [f"Sentence number {i} is here." for i in range(30)]
    long_text = " ".join(sentences)
    extra_word = "x" * 120  # longer than max_chars: hard split
    text = f"{long_text} {extra_word} tail words end."
    provider = FakeProvider(max_chars=50)
    data, _mime = await synthesize_dialogue(provider, make_script([(0, text)]), concurrency=1)

    calls = provider.calls
    assert len(calls) > 1
    assert all(len(t) <= 50 for t, _v in calls)
    assert all(v == "d0" for _t, v in calls)

    def squash(s: str) -> str:
        return re.sub(r"\s+", "", s)

    # No characters lost (whitespace between chunks is the only difference allowed).
    assert squash(" ".join(t for t, _v in calls)) == squash(text)
    joined = " ".join(t for t, _v in calls)
    assert "Sentence number 29 is here. " in joined
    assert calls[-1][0].endswith("tail words end.")
    assert data == "".join(f"[{t}]" for t, _v in calls).encode()


def test_split_text_prefers_sentence_boundaries_and_packs_greedily() -> None:
    assert split_text("One. Two. Three. Four.", 12) == ["One. Two.", "Three. Four."]
    assert split_text("", 10) == []
    assert split_text("short", 10) == ["short"]


def test_split_text_hard_cuts_overlong_words() -> None:
    assert split_text("abcdefghij", 4) == ["abcd", "efgh", "ij"]


async def test_uses_default_max_chars_of_4000_when_absent() -> None:
    provider = FakeProvider()  # no max_chars attribute
    text = ("word " * 1000).strip()  # 4999 chars
    _data, _mime = await synthesize_dialogue(provider, make_script([(0, text)]), concurrency=1)
    assert len(provider.calls) == 2
    assert all(len(t) <= 4000 for t, _v in provider.calls)


async def test_rejects_when_a_segment_fails_and_stops_scheduling_more_work() -> None:
    provider = FakeProvider(fail=lambda t: t == "bad")
    with pytest.raises(RuntimeError, match="boom: bad"):
        await synthesize_dialogue(
            provider,
            make_script([(0, "ok"), (1, "bad"), (0, "never")]),
            concurrency=1,
        )
    assert not any(t == "never" for t, _v in provider.calls)


async def test_cancelling_the_caller_propagates_promptly() -> None:
    provider = FakeProvider(delay=lambda _t: 2000)
    started = time.monotonic()
    task = asyncio.create_task(
        synthesize_dialogue(provider, make_script([(0, "a"), (1, "b")])),
    )
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 1.0


async def test_cancelled_before_start_makes_no_calls() -> None:
    provider = FakeProvider()
    task = asyncio.create_task(synthesize_dialogue(provider, make_script([(0, "a")])))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.calls == []


# ───────────────────────────── mp3 helpers ─────────────────────────────


def id3v2(payload_length: int, footer: bool = False) -> bytes:
    out = bytearray(10 + payload_length + (10 if footer else 0))
    out[0:3] = b"ID3"
    out[3] = 4  # version
    out[4] = 0
    out[5] = 0x10 if footer else 0
    # synchsafe size: 7 bits per byte
    out[6] = (payload_length >> 21) & 0x7F
    out[7] = (payload_length >> 14) & 0x7F
    out[8] = (payload_length >> 7) & 0x7F
    out[9] = payload_length & 0x7F
    out[10 : 10 + payload_length] = b"A" * payload_length  # filler
    return bytes(out)


def id3v1() -> bytes:
    out = bytearray(128)
    out[0:3] = b"TAG"
    out[3:] = b"B" * 125
    return bytes(out)


def test_strips_leading_id3v2_using_synchsafe_size() -> None:
    frame = b"\xff\xfb-FRAME"
    assert strip_id3(id3v2(5) + frame) == frame
    big = id3v2(200) + frame  # 200 = 0x01 0x48 synchsafe
    assert strip_id3(big) == frame


def test_honours_id3v2_footer_flag() -> None:
    frame = b"FRAMEDATA"
    assert strip_id3(id3v2(4, footer=True) + frame) == frame


def test_strips_trailing_id3v1_tag() -> None:
    frame = b"AUDIO-BODY-BYTES"
    assert strip_id3(frame + id3v1()) == frame


def test_leaves_untagged_data_alone() -> None:
    frame = b"\xff\xfbnothing to strip here"
    assert strip_id3(frame) == frame


def test_concat_mp3_strips_tags_and_joins_frames() -> None:
    a = id3v2(3) + b"AAA"
    b = b"BBB" + id3v1()
    c = id3v2(6) + b"CCCCCC" + id3v1()
    assert concat_mp3([a, b, c]) == b"AAABBBCCCCCC"
    assert concat_mp3([]) == b""


# ───────────────────────── providers over mocked HTTP ─────────────────────────


async def test_openai_url_headers_body_and_bytes() -> None:
    rec = Recorder([audio()])
    provider = OpenAITts("sk-test-123", transport=rec.transport)
    data = await provider.synthesize("Hello there", "coral")

    assert len(rec.requests) == 1
    req = rec.requests[0]
    assert req.method == "POST"
    assert str(req.url) == "https://api.openai.com/v1/audio/speech"
    assert req.headers["Authorization"] == "Bearer sk-test-123"
    assert req.headers["Content-Type"] == "application/json"
    assert json.loads(req.content) == {
        "model": "gpt-4o-mini-tts",
        "voice": "coral",
        "input": "Hello there",
        "response_format": "mp3",
    }
    assert list(data) == [0xFF, 0xFB, 1, 2]
    assert provider.mime_type == "audio/mpeg"


async def test_openai_retries_after_429_honouring_retry_after(sleeps: list[float]) -> None:
    rec = Recorder([httpx.Response(429, headers={"retry-after": "0"}, text="slow down"), audio()])
    data = await OpenAITts("sk-test", transport=rec.transport).synthesize("x", "ash")
    assert len(rec.requests) == 2
    assert len(data) == 4
    assert sleeps == [0.0]


async def test_retry_after_is_capped(sleeps: list[float]) -> None:
    rec = Recorder([httpx.Response(429, headers={"retry-after": "3600"}), audio()])
    await OpenAITts("sk-test", transport=rec.transport).synthesize("x", "ash")
    assert sleeps == [tts_mod.MAX_RETRY_AFTER_S]


async def test_openai_401_reports_rejected_key_without_leaking_it() -> None:
    rec = Recorder([httpx.Response(401, text='{"error":"invalid key sk-secret-999"}')])
    provider = OpenAITts("sk-secret-999", transport=rec.transport)
    with pytest.raises(OvertoneError) as exc:
        await provider.synthesize("x", "ash")
    err = exc.value
    assert err.status == 502
    assert err.code == "tts_failed"
    assert re.search(r"OpenAI text-to-speech failed \(401\)", err.message)
    assert "API key was rejected" in err.message
    assert "sk-secret-999" not in err.message
    assert len(rec.requests) == 1


async def test_network_errors_retry_then_report_redacted_message(sleeps: list[float]) -> None:
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ConnectError("connection refused for sk-net-1")

    provider = OpenAITts("sk-net-1", transport=httpx.MockTransport(handler))
    with pytest.raises(OvertoneError) as exc:
        await provider.synthesize("x", "ash")
    assert len(attempts) == 3
    assert sleeps == [0.8, 2.0]
    assert exc.value.status == 502
    assert exc.value.message == (
        "OpenAI text-to-speech failed (network error): connection refused for [redacted]"
    )
    assert "sk-net-1" not in exc.value.message


async def test_network_error_then_success_returns_audio(sleeps: list[float]) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("slow")
        return audio()

    data = await OpenAITts("k", transport=httpx.MockTransport(handler)).synthesize("x", "ash")
    assert calls == 2
    assert len(data) == 4


async def test_elevenlabs_url_headers_body_for_builtin_and_custom_voice() -> None:
    rec = Recorder([audio(), audio()])
    provider = ElevenLabsTts("xi-test", transport=rec.transport)
    await provider.synthesize("Hi", "21m00Tcm4TlvDq8ikWAM")
    await provider.synthesize("Yo", "custom-voice-123")

    assert str(rec.requests[0].url) == (
        "https://api.elevenlabs.io/v1/text-to-speech/21m00Tcm4TlvDq8ikWAM?output_format=mp3_44100_128"
    )
    h = rec.requests[0].headers
    assert h["xi-api-key"] == "xi-test"
    assert h["Content-Type"] == "application/json"
    assert h["Accept"] == "audio/mpeg"
    assert json.loads(rec.requests[0].content) == {"text": "Hi", "model_id": "eleven_multilingual_v2"}
    assert re.search(
        r"/v1/text-to-speech/custom-voice-123\?output_format=mp3_44100_128$",
        str(rec.requests[1].url),
    )


async def test_elevenlabs_retries_429_then_succeeds(sleeps: list[float]) -> None:
    rec = Recorder([httpx.Response(429), audio()])
    await ElevenLabsTts("xi-test", transport=rec.transport).synthesize("Hi", "pNInz6obpgDQGcFmaJgB")
    assert len(rec.requests) == 2
    assert sleeps == [0.8]  # no Retry-After: default backoff


async def test_elevenlabs_401_says_key_was_rejected() -> None:
    rec = Recorder([httpx.Response(401, text="nope")])
    with pytest.raises(OvertoneError, match=r"ElevenLabs text-to-speech failed \(401\): API key was rejected"):
        await ElevenLabsTts("xi-test", transport=rec.transport).synthesize("Hi", "pNInz6obpgDQGcFmaJgB")


async def test_gives_up_after_retry_budget_on_persistent_5xx(sleeps: list[float]) -> None:
    rec = Recorder(
        [httpx.Response(503, headers={"retry-after": "0"}, text="down") for _ in range(3)],
    )
    provider = OpenAITts("sk-test", transport=rec.transport)
    with pytest.raises(OvertoneError, match=re.escape("OpenAI text-to-speech failed (503): down")):
        await provider.synthesize("x", "ash")
    assert len(rec.requests) == 3
    assert sleeps == [0.0, 0.0]
