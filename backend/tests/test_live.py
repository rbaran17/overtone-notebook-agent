"""Tests for overtone.live and the engine's live-voice entry points.

The OpenAI endpoint is replaced with httpx.MockTransport, so every request and
response is inspected locally. The API key used here is a dummy value.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from overtone import MemoryStorage, MockChatClient, OvertoneError, create_engine
from overtone.live import DEFAULT_LIVE_MODEL, DEFAULT_LIVE_VOICE, LIVE_VOICES, LiveVoice, create_live_voice
from overtone.models import ArtifactRequest, AudioHost, AudioOptions, LiveSessionRequest, TextInput

KEY = "test-key"
CLIENT_EVENTS = ["session.close", "session.commentary.append", "session.thinking.append"]


def _voice(handler: Any, **kwargs: Any) -> LiveVoice:
    return LiveVoice(api_key=KEY, transport=httpx.MockTransport(handler), **kwargs)


def _created(captured: list[httpx.Request] | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        return httpx.Response(201, json={"session": {"id": "sess_123"}, "transport": {"sdp": "v=0 answer"}})

    return handler


async def _start(client: LiveVoice, **overrides: Any) -> tuple[str, str]:
    args = {"instructions": "Be brief.", "voice": "marin", "sdp_offer": "v=0 offer"}
    args.update(overrides)
    return await client.start_webrtc_session(**args)


def _body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


@pytest.fixture(autouse=False)
def live_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in (
        "OPENAI_API_KEY",
        "OVERTONE_LIVE_API_KEY",
        "OVERTONE_LIVE",
        "OVERTONE_LIVE_VOICE",
        "OVERTONE_LIVE_MODEL",
        "OVERTONE_LIVE_BASE_URL",
        "OVERTONE_LIVE_CLIENT_EVENTS",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# ───────────────────────────── request shape ─────────────────────────────


async def test_request_goes_to_the_sessions_endpoint_with_a_bearer_key() -> None:
    captured: list[httpx.Request] = []
    await _start(_voice(_created(captured)))
    (request,) = captured
    assert request.method == "POST"
    assert request.url.path.endswith("/live/sessions")
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert request.headers["Content-Type"].startswith("application/json")


async def test_request_body_has_the_expected_session_shape() -> None:
    captured: list[httpx.Request] = []
    await _start(_voice(_created(captured), default_voice="marin"), instructions="Talk about revenue.", voice="sage")
    body = _body(captured[0])

    session = body["session"]
    assert session["model"] == DEFAULT_LIVE_MODEL == "gpt-live-1"
    assert session["instructions"] == "Talk about revenue."
    assert session["audio"] == {"output": {"voice": "sage"}}
    assert session["delegation"] == {"type": "client", "acknowledgement_delay_ms": 400}
    assert session["client"]["data_channel"]["allowed_server_events"] == "all"
    assert body["transport"] == {"type": "webrtc", "sdp": "v=0 offer"}
    assert "safety_identifier" not in session


async def test_client_events_are_restricted_by_default(live_env: pytest.MonkeyPatch) -> None:
    captured: list[httpx.Request] = []
    await _start(_voice(_created(captured)))
    allowed = _body(captured[0])["session"]["client"]["data_channel"]["allowed_client_events"]
    assert allowed == CLIENT_EVENTS
    assert "session.update" not in allowed


async def test_client_events_can_be_opened_with_an_environment_switch(live_env: pytest.MonkeyPatch) -> None:
    live_env.setenv("OVERTONE_LIVE_CLIENT_EVENTS", "all")
    captured: list[httpx.Request] = []
    await _start(_voice(_created(captured)))
    assert _body(captured[0])["session"]["client"]["data_channel"]["allowed_client_events"] == "all"


async def test_safety_identifier_is_sent_and_truncated_to_128() -> None:
    captured: list[httpx.Request] = []
    await _start(_voice(_created(captured)), safety_identifier="x" * 200)
    assert _body(captured[0])["session"]["safety_identifier"] == "x" * 128


async def test_trailing_slash_on_base_url_does_not_double_up() -> None:
    captured: list[httpx.Request] = []
    await _start(_voice(_created(captured), base_url="https://example.test/v1/"))
    assert captured[0].url.path == "/v1/live/sessions"


async def test_created_session_returns_the_id_and_sdp_answer() -> None:
    session_id, sdp = await _start(_voice(_created()))
    assert (session_id, sdp) == ("sess_123", "v=0 answer")


# ───────────────────────────── failures ─────────────────────────────


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_key_is_live_auth_without_echoing_the_key(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": f"Incorrect key {KEY}"}})

    with pytest.raises(OvertoneError) as info:
        await _start(_voice(handler))
    assert info.value.code == "live_auth"
    assert KEY not in info.value.message


async def test_bad_request_includes_the_service_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "SDP offer is malformed"}})

    with pytest.raises(OvertoneError) as info:
        await _start(_voice(handler))
    assert (info.value.status, info.value.code) == (502, "live_failed")
    assert "SDP offer is malformed" in info.value.message
    assert "(400)" in info.value.message


async def test_bad_request_with_a_plain_text_body_includes_the_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="oops, no session for you")

    with pytest.raises(OvertoneError) as info:
        await _start(_voice(handler))
    assert info.value.code == "live_failed"
    assert "oops, no session for you" in info.value.message


async def test_bad_request_with_an_empty_body_still_reports_the_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400)

    with pytest.raises(OvertoneError) as info:
        await _start(_voice(handler))
    assert info.value.code == "live_failed"
    assert info.value.message.endswith("(400)")


async def test_key_echoed_by_the_service_is_redacted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": f"Key {KEY} was rejected for this model"}})

    with pytest.raises(OvertoneError) as info:
        await _start(_voice(handler))
    assert KEY not in info.value.message
    assert "[redacted]" in info.value.message


async def test_server_error_is_live_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "internal"}})

    with pytest.raises(OvertoneError) as info:
        await _start(_voice(handler))
    assert info.value.code == "live_failed"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(201, json={"unexpected": True}),
        httpx.Response(201, json={"session": {"id": "s"}}),
        httpx.Response(201, text="<html>not json</html>"),
    ],
)
async def test_malformed_success_body_is_live_failed(response: httpx.Response) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    with pytest.raises(OvertoneError) as info:
        await _start(_voice(handler))
    assert (info.value.status, info.value.code) == (502, "live_failed")


async def test_network_failure_is_live_unreachable_and_hides_the_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connection refused for key {KEY}")

    with pytest.raises(OvertoneError) as info:
        await _start(_voice(handler))
    assert (info.value.status, info.value.code) == (502, "live_unreachable")
    assert KEY not in info.value.message


async def test_string_error_field_is_still_live_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "plain string error"})

    with pytest.raises(OvertoneError) as info:
        await _start(_voice(handler))
    assert info.value.code == "live_failed"
    assert "plain string error" in info.value.message


# ───────────────────────────── voices ─────────────────────────────


def test_known_voice_is_kept_and_unknown_falls_back_to_default() -> None:
    voice = LiveVoice(api_key=KEY)
    assert voice.pick_voice("sage") == "sage"
    assert voice.pick_voice("not-a-voice") == DEFAULT_LIVE_VOICE == "marin"
    assert voice.pick_voice(None) == "marin"
    assert voice.pick_voice("") == "marin"


def test_fallback_uses_the_configured_default_voice() -> None:
    assert LiveVoice(api_key=KEY, default_voice="sage").pick_voice("bogus") == "sage"


def test_voice_catalogue_contains_the_default_and_is_unique() -> None:
    assert DEFAULT_LIVE_VOICE in LIVE_VOICES
    assert len(set(LIVE_VOICES)) == len(LIVE_VOICES)


# ───────────────────────────── create_live_voice ─────────────────────────────


def test_no_key_means_no_live_voice(live_env: pytest.MonkeyPatch) -> None:
    assert create_live_voice() is None


def test_openai_key_enables_live_voice_with_defaults(live_env: pytest.MonkeyPatch) -> None:
    live_env.setenv("OPENAI_API_KEY", "sk-env")
    voice = create_live_voice()
    assert voice is not None
    assert (voice.api_key, voice.model, voice.default_voice) == ("sk-env", "gpt-live-1", "marin")
    assert voice.base_url == "https://api.openai.com/v1"


def test_switch_off_disables_live_voice_even_with_a_key(live_env: pytest.MonkeyPatch) -> None:
    live_env.setenv("OPENAI_API_KEY", "sk-env")
    live_env.setenv("OVERTONE_LIVE", "0")
    assert create_live_voice() is None


def test_live_specific_key_takes_precedence(live_env: pytest.MonkeyPatch) -> None:
    live_env.setenv("OPENAI_API_KEY", "sk-general")
    live_env.setenv("OVERTONE_LIVE_API_KEY", "sk-live")
    voice = create_live_voice()
    assert voice is not None and voice.api_key == "sk-live"


def test_live_voice_and_model_can_be_configured(live_env: pytest.MonkeyPatch) -> None:
    live_env.setenv("OPENAI_API_KEY", "sk-env")
    live_env.setenv("OVERTONE_LIVE_VOICE", "sage")
    live_env.setenv("OVERTONE_LIVE_MODEL", "gpt-live-2")
    live_env.setenv("OVERTONE_LIVE_BASE_URL", "https://proxy.test/v1")
    voice = create_live_voice()
    assert voice is not None
    assert (voice.default_voice, voice.model, voice.base_url) == ("sage", "gpt-live-2", "https://proxy.test/v1")


def test_unknown_configured_voice_falls_back_to_marin(live_env: pytest.MonkeyPatch) -> None:
    live_env.setenv("OPENAI_API_KEY", "sk-env")
    live_env.setenv("OVERTONE_LIVE_VOICE", "nope")
    voice = create_live_voice()
    assert voice is not None and voice.default_voice == "marin"


# ───────────────────────────── engine.live.create_session ─────────────────────────────


def _engine_with_live(captured: list[httpx.Request]) -> Any:
    return create_engine(
        storage=MemoryStorage(),
        chat_client=MockChatClient(delay=0),
        tts=None,
        live_voice=_voice(_created(captured)),
    )


async def _notebook(engine: Any, title: str = "Field Notes") -> Any:
    notebook = await engine.notebooks.create(title=title)
    await engine.sources.add(
        notebook.id,
        TextInput("Alpha results improved sharply after the pilot. " * 10, title="Report A"),
    )
    await engine.sources.add(
        notebook.id,
        TextInput("Beta memo recommends a phased rollout to limit risk. " * 10, title="Memo B"),
    )
    return notebook


async def test_conversation_session_names_the_notebook_and_its_sources() -> None:
    captured: list[httpx.Request] = []
    engine = _engine_with_live(captured)
    notebook = await _notebook(engine)
    session = await engine.live.create_session(notebook.id, LiveSessionRequest(sdp="v=0 offer"))

    instructions = _body(captured[0])["session"]["instructions"]
    assert "Field Notes" in instructions
    assert "Report A" in instructions
    assert "Memo B" in instructions
    assert (session.session_id, session.sdp, session.mode) == ("sess_123", "v=0 answer", "conversation")
    assert session.model == "gpt-live-1"
    assert session.voice == "marin"


async def test_conversation_session_uses_a_valid_requested_voice_only() -> None:
    captured: list[httpx.Request] = []
    engine = _engine_with_live(captured)
    notebook = await _notebook(engine)
    ok = await engine.live.create_session(notebook.id, LiveSessionRequest(sdp="v=0 offer", voice="sage"))
    bad = await engine.live.create_session(notebook.id, LiveSessionRequest(sdp="v=0 offer", voice="nope"))
    assert (ok.voice, bad.voice) == ("sage", "marin")


async def test_conversation_session_includes_the_guide_summary_when_cached() -> None:
    captured: list[httpx.Request] = []
    engine = _engine_with_live(captured)
    notebook = await _notebook(engine)
    guide = await engine.notebooks.guide(notebook.id)
    await engine.live.create_session(notebook.id, LiveSessionRequest(sdp="v=0 offer"))
    assert guide.summary in _body(captured[0])["session"]["instructions"]


async def test_conversation_session_needs_a_source() -> None:
    engine = _engine_with_live([])
    notebook = await engine.notebooks.create(title="Empty")
    with pytest.raises(OvertoneError) as info:
        await engine.live.create_session(notebook.id, LiveSessionRequest(sdp="v=0 offer"))
    assert (info.value.status, info.value.code) == (400, "no_sources")


async def test_session_needs_an_offer() -> None:
    engine = _engine_with_live([])
    notebook = await _notebook(engine)
    with pytest.raises(OvertoneError) as info:
        await engine.live.create_session(notebook.id, LiveSessionRequest(sdp="   "))
    assert (info.value.status, info.value.code) == (400, "bad_sdp")


async def test_podcast_session_needs_an_artifact_id() -> None:
    engine = _engine_with_live([])
    notebook = await _notebook(engine)
    with pytest.raises(OvertoneError) as info:
        await engine.live.create_session(notebook.id, LiveSessionRequest(sdp="v=0 offer", mode="podcast"))
    assert (info.value.status, info.value.code) == (400, "missing_artifact")


async def test_podcast_session_rejects_an_artifact_without_a_script() -> None:
    engine = _engine_with_live([])
    notebook = await _notebook(engine)
    summary = await engine.artifacts.generate(notebook.id, ArtifactRequest(kind="summary"))
    with pytest.raises(OvertoneError) as info:
        await engine.live.create_session(
            notebook.id, LiveSessionRequest(sdp="v=0 offer", mode="podcast", artifact_id=summary.id)
        )
    assert (info.value.status, info.value.code) == (400, "no_script")


async def test_podcast_session_rejects_an_artifact_from_another_notebook() -> None:
    engine = _engine_with_live([])
    owner = await _notebook(engine, "Owner")
    other = await _notebook(engine, "Other")
    episode = await engine.artifacts.generate(owner.id, ArtifactRequest(kind="audio_overview"))
    with pytest.raises(OvertoneError) as info:
        await engine.live.create_session(
            other.id, LiveSessionRequest(sdp="v=0 offer", mode="podcast", artifact_id=episode.id)
        )
    assert info.value.code == "no_script"


async def _podcast(engine: Any, notebook: Any, options: AudioOptions | None = None) -> Any:
    return await engine.artifacts.generate(notebook.id, ArtifactRequest(kind="audio_overview", options=options))


async def test_podcast_session_carries_the_episode_and_only_the_turns_heard_so_far() -> None:
    captured: list[httpx.Request] = []
    engine = _engine_with_live(captured)
    notebook = await _notebook(engine)
    episode = await _podcast(engine, notebook)
    assert episode.script is not None and len(episode.script.turns) >= 4
    turn_index = 2

    session = await engine.live.create_session(
        notebook.id,
        LiveSessionRequest(sdp="v=0 offer", mode="podcast", artifact_id=episode.id, turn_index=turn_index),
    )
    instructions = _body(captured[0])["session"]["instructions"]

    assert session.mode == "podcast"
    assert episode.script.title in instructions
    assert episode.script.summary in instructions
    assert "Field Notes" in instructions
    heard = [t.text for t in episode.script.turns[: turn_index + 1]]
    unheard = [t.text for t in episode.script.turns[turn_index + 1 :]]
    assert all(text in instructions for text in heard)
    assert not any(text in instructions for text in unheard), "turns after the pause must not leak in"


async def test_podcast_session_speaks_as_the_host_of_the_paused_turn() -> None:
    captured: list[httpx.Request] = []
    engine = _engine_with_live(captured)
    notebook = await _notebook(engine)
    episode = await _podcast(
        engine,
        notebook,
        AudioOptions(hosts=[AudioHost(name="Ada", voice="sage"), AudioHost(name="Bo", voice="ash")]),
    )
    assert episode.script is not None

    first = await engine.live.create_session(
        notebook.id, LiveSessionRequest(sdp="v=0 offer", mode="podcast", artifact_id=episode.id, turn_index=0)
    )
    assert first.voice == "sage"
    assert "You are Ada" in _body(captured[0])["session"]["instructions"]

    later = next(i for i, t in enumerate(episode.script.turns) if t.speaker == 1)
    second = await engine.live.create_session(
        notebook.id, LiveSessionRequest(sdp="v=0 offer", mode="podcast", artifact_id=episode.id, turn_index=later)
    )
    assert second.voice == "ash"
    assert "You are Bo" in _body(captured[1])["session"]["instructions"]


async def test_podcast_session_ignores_host_voices_that_are_not_live_voices() -> None:
    captured: list[httpx.Request] = []
    engine = _engine_with_live(captured)
    notebook = await _notebook(engine)
    episode = await _podcast(
        engine,
        notebook,
        AudioOptions(hosts=[AudioHost(name="Ada", voice="en-GB-Wavenet-A"), AudioHost(name="Bo")]),
    )
    session = await engine.live.create_session(
        notebook.id, LiveSessionRequest(sdp="v=0 offer", mode="podcast", artifact_id=episode.id, turn_index=0)
    )
    assert session.voice == "marin"


async def test_podcast_turn_index_is_clamped_to_the_script() -> None:
    captured: list[httpx.Request] = []
    engine = _engine_with_live(captured)
    notebook = await _notebook(engine)
    episode = await _podcast(engine, notebook)
    assert episode.script is not None
    session = await engine.live.create_session(
        notebook.id,
        LiveSessionRequest(sdp="v=0 offer", mode="podcast", artifact_id=episode.id, turn_index=10_000),
    )
    assert session.mode == "podcast"
    instructions = _body(captured[0])["session"]["instructions"]
    assert episode.script.turns[-1].text in instructions
