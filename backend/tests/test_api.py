"""REST API tests: a port of packages/core/test/http.test.ts, driven in-process.

Run from backend/:  ./.venv/Scripts/python -m pytest tests/test_api.py -q
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from overtone import MemoryStorage, MockChatClient, create_engine
from overtone import api as api_mod
from overtone.api import create_app
from overtone.live import LiveVoice
from overtone.models import TtsVoice


class FakeTts:
    """Returns a few known bytes per utterance, so range requests can be checked exactly."""

    id = "fake"
    label = "Fake TTS"
    voices = [TtsVoice(id="a", label="A")]
    default_voices = ["a", "a"]
    mime_type = "audio/mpeg"
    max_chars = 4096

    async def synthesize(self, text: str, voice: str) -> bytes:
        # An MPEG frame header followed by the text. (An ID3 prefix would be stripped when episodes are joined.)
        return b"\xff\xfb\x90\x64" + text.encode()


def make_engine(tts: Any = None, live_voice: LiveVoice | None = None) -> Any:
    return create_engine(
        storage=MemoryStorage(), chat_client=MockChatClient(delay=0), tts=tts, live_voice=live_voice
    )


def make_app(tts: Any = None, live_voice: LiveVoice | None = None, **options: Any) -> tuple[Any, FastAPI]:
    engine = make_engine(tts, live_voice)
    return engine, create_app(engine, **options)


def client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def parse_sse(text: str) -> list[dict[str, Any]]:
    """Splits an event stream on blank lines and decodes each `data:` block."""
    blocks = [block for block in text.split("\n\n") if block.strip()]
    assert all(block.startswith("data: ") for block in blocks), blocks
    return [json.loads(block[len("data: ") :]) for block in blocks]


async def notebook_with_source(engine: Any, text: str = "Overtone grounds answers in sources. It works.") -> str:
    nb = await engine.notebooks.create(title="Research")
    await engine.sources.add(nb.id, _text(text))
    return nb.id


def _text(text: str, title: str | None = None) -> Any:
    from overtone import TextInput

    return TextInput(text=text, title=title)


# ───────────────────────────── notebooks & routing ─────────────────────────────


async def test_notebook_crud() -> None:
    _, app = make_app()
    async with client(app) as c:
        created = await c.post("/notebooks", json={"title": "Research", "icon": "R"})
        assert created.status_code == 201
        notebook = created.json()["notebook"]
        assert notebook["title"] == "Research"

        blank = await c.post("/notebooks")
        assert blank.status_code == 201, "empty body is allowed"
        assert blank.json()["notebook"]["title"] == "Untitled notebook"

        listed = (await c.get("/notebooks")).json()
        assert len(listed["notebooks"]) == 2

        got = await c.get(f"/notebooks/{notebook['id']}/")
        assert got.status_code == 200, "trailing slash is tolerated"

        patched = await c.patch(f"/notebooks/{notebook['id']}", json={"title": "Renamed"})
        assert patched.status_code == 200
        assert patched.json()["notebook"]["title"] == "Renamed"

        deleted = await c.delete(f"/notebooks/{notebook['id']}")
        assert deleted.status_code == 204
        assert deleted.content == b""

        gone = await c.get(f"/notebooks/{notebook['id']}")
        assert gone.status_code == 404
        assert gone.json()["error"]["code"] == "not_found"


async def test_routing_404_405_bad_json_and_missing() -> None:
    _, app = make_app()
    async with client(app) as c:
        unknown = await c.get("/nope")
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "not_found"

        wrong_method = await c.put("/notebooks")
        assert wrong_method.status_code == 405
        allow = wrong_method.headers.get("Allow", "")
        assert "GET" in allow and "POST" in allow
        assert wrong_method.json()["error"]["code"] == "method_not_allowed"

        bad = await c.post("/notebooks", content=b"{bad", headers={"Content-Type": "application/json"})
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "bad_json"

        missing = await c.get("/notebooks/does-not-exist")
        assert missing.status_code == 404


async def test_unhandled_errors_become_generic_500_without_leaking() -> None:
    engine = make_engine()

    async def explode() -> None:
        raise RuntimeError("secret database password")

    engine.notebooks.list = explode  # type: ignore[method-assign]
    app = create_app(engine)
    async with client(app) as c:
        res = await c.get("/notebooks")
    assert res.status_code == 500
    assert res.json() == {"error": {"message": "Internal error", "code": "internal"}}
    assert "secret database password" not in res.text


# ───────────────────────────── sources (no ingestion needed) ─────────────────────────────


async def test_multipart_and_json_source_rejections() -> None:
    engine, app = make_app(max_upload_bytes=4)
    nb = await engine.notebooks.create(title="Uploads")
    path = f"/notebooks/{nb.id}/sources"
    async with client(app) as c:
        json_file = await c.post(path, json={"type": "file", "filename": "a.pdf", "data": [1, 2]})
        assert json_file.status_code == 400
        assert json_file.json()["error"]["code"] == "multipart_required"

        empty = await c.post(path, json={"sources": []})
        assert empty.status_code == 400

        # Both files exceed the 4-byte limit, so every source fails: the first failure's status wins.
        all_failed = await c.post(
            path,
            files=[
                ("file", ("one.txt", b"0123456789", "text/plain")),
                ("file", ("two.txt", b"abcdefghij", "text/plain")),
            ],
        )
        assert all_failed.status_code == 413
        body = all_failed.json()
        assert body["error"]["code"] == "too_large"
        assert body["sources"] == []
        assert len(body["errors"]) == 2
        assert body["errors"][0]["name"] == "one.txt"
        assert body["errors"][0]["code"] == "too_large"


async def test_adding_a_source_to_a_missing_notebook_is_404() -> None:
    _, app = make_app()
    async with client(app) as c:
        res = await c.post("/notebooks/nope/sources", json={"type": "text", "text": "hi"})
    assert res.status_code == 404


# ───────────────────────────── chat & messages (no ingestion needed) ─────────────────────────────


async def test_chat_without_sources_streams_an_error_event_and_stream_false_is_400() -> None:
    engine, app = make_app()
    nb = await engine.notebooks.create(title="Empty")
    async with client(app) as c:
        res = await c.post(f"/notebooks/{nb.id}/chat", json={"message": "hello"})
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        assert res.headers["cache-control"] == "no-cache, no-transform"
        assert res.headers["x-accel-buffering"] == "no"
        events = parse_sse(res.text)
        assert events[-1]["type"] == "error"

        non_streaming = await c.post(f"/notebooks/{nb.id}/chat", json={"message": "hello", "stream": False})
        # The underlying client error keeps its own status and code.
        assert non_streaming.status_code == 400
        assert non_streaming.json()["error"]["code"] == "no_sources"

        empty = await c.post(f"/notebooks/{nb.id}/chat", json={"message": "   "})
        assert empty.status_code == 400
        assert empty.json()["error"]["code"] == "empty_message"

        messages = (await c.get(f"/notebooks/{nb.id}/messages")).json()
        assert messages["messages"] == []
        cleared = await c.delete(f"/notebooks/{nb.id}/messages")
        assert cleared.status_code == 204


async def test_chat_rejects_bad_source_ids_and_missing_message() -> None:
    engine, app = make_app()
    nb = await engine.notebooks.create(title="x")
    async with client(app) as c:
        no_message = await c.post(f"/notebooks/{nb.id}/chat", json={"stream": False})
        assert no_message.status_code == 400
        assert no_message.json()["error"]["code"] == "empty_message"
        bad_ids = await c.post(f"/notebooks/{nb.id}/chat", json={"message": "hi", "sourceIds": "abc"})
        assert bad_ids.status_code == 400
        missing = await c.post("/notebooks/nope/chat", json={"message": "hi"})
        assert missing.status_code == 404
        assert missing.headers["content-type"].startswith("application/json")


async def test_artifact_create_validation_and_missing_audio() -> None:
    engine, app = make_app()
    nb = await engine.notebooks.create(title="No sources yet")
    async with client(app) as c:
        no_sources = await c.post(f"/notebooks/{nb.id}/artifacts", json={"kind": "summary"})
        assert no_sources.status_code == 400
        assert no_sources.json()["error"]["code"] == "no_sources"

        missing_kind = await c.post(f"/notebooks/{nb.id}/artifacts", json={"title": "x"})
        assert missing_kind.status_code == 400

        audio = await c.get("/artifacts/nope/audio")
        assert audio.status_code == 404
        assert audio.json()["error"]["code"] == "not_found"


# ───────────────────────────── auth and CORS ─────────────────────────────


async def test_api_key_auth_header_required_query_key_only_for_audio_health_open() -> None:
    _, app = make_app(api_key="s3cret")
    async with client(app) as c:
        anonymous = await c.get("/notebooks")
        assert anonymous.status_code == 401
        assert anonymous.headers["WWW-Authenticate"] == "Bearer"
        assert anonymous.json()["error"]["code"] == "unauthorized"

        wrong = await c.get("/notebooks", headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401

        right = await c.get("/notebooks", headers={"Authorization": "Bearer s3cret"})
        assert right.status_code == 200

        health = await c.get("/health")
        assert health.status_code == 200, "/health is open"

        preflight = await c.options("/notebooks")
        assert preflight.status_code != 401, "OPTIONS skips auth"

        query_on_notebooks = await c.get("/notebooks?key=s3cret")
        assert query_on_notebooks.status_code == 401, "?key= is not accepted outside audio"

        audio_with_key = await c.get("/artifacts/none/audio?key=s3cret")
        assert audio_with_key.status_code == 404
        assert audio_with_key.json()["error"]["code"] == "not_found"

        audio_wrong_key = await c.get("/artifacts/none/audio?key=nope")
        assert audio_wrong_key.status_code == 401


async def test_cors_wildcard_allow_list_preflight_and_headers_on_errors() -> None:
    engine = make_engine()

    async with client(create_app(engine, cors=True)) as c:
        health = await c.get("/health", headers={"Origin": "https://anywhere.example"})
        assert health.headers["Access-Control-Allow-Origin"] == "*"
        assert "Content-Range" in health.headers["Access-Control-Expose-Headers"]

        not_found = await c.get("/nope")
        assert not_found.headers["Access-Control-Allow-Origin"] == "*", "errors carry CORS headers too"

        preflight = await c.options("/notebooks", headers={"Origin": "https://x.example"})
        assert preflight.status_code == 204
        assert preflight.headers["Access-Control-Allow-Methods"] == "GET,POST,PATCH,DELETE,OPTIONS"
        assert preflight.headers["Access-Control-Allow-Headers"] == "Authorization, Content-Type"
        assert preflight.headers["Access-Control-Max-Age"] == "86400"
        assert preflight.headers["Access-Control-Allow-Origin"] == "*"

    async with client(create_app(engine, cors=["https://app.example"])) as c:
        allowed = await c.get("/health", headers={"Origin": "https://app.example"})
        assert allowed.headers["Access-Control-Allow-Origin"] == "https://app.example"
        assert "Origin" in allowed.headers["Vary"]

        denied = await c.get("/health", headers={"Origin": "https://evil.example"})
        assert "Access-Control-Allow-Origin" not in denied.headers
        assert "Origin" in denied.headers["Vary"]

    async with client(create_app(engine)) as c:
        none = await c.get("/health", headers={"Origin": "https://app.example"})
        assert "Access-Control-Allow-Origin" not in none.headers


# ───────────────────────────── sources (ingestion) ─────────────────────────────


async def test_text_source_via_json_read_back_list_and_delete() -> None:
    engine, app = make_app()
    nb = await engine.notebooks.create(title="Notes")
    async with client(app) as c:
        res = await c.post(
            f"/notebooks/{nb.id}/sources", json={"type": "text", "text": "Hello source.", "title": "Greeting"}
        )
        assert res.status_code == 201
        body = res.json()
        assert len(body["sources"]) == 1
        assert body["errors"] == []
        source = body["sources"][0]
        assert source["title"] == "Greeting"

        text = await c.get(f"/sources/{source['id']}/text")
        assert text.status_code == 200
        assert text.headers["content-type"].startswith("text/plain; charset=utf-8")
        assert text.text == "Hello source."

        listed = (await c.get(f"/notebooks/{nb.id}/sources")).json()
        assert len(listed["sources"]) == 1

        deleted = await c.delete(f"/sources/{source['id']}")
        assert deleted.status_code == 204


async def test_multipart_txt_upload() -> None:
    engine, app = make_app()
    nb = await engine.notebooks.create(title="Upload")
    async with client(app) as c:
        res = await c.post(
            f"/notebooks/{nb.id}/sources", files=[("file", ("notes.txt", b"file body text", "text/plain"))]
        )
        assert res.status_code == 201
        sources = res.json()["sources"]
        assert sources[0]["kind"] == "file"
        text = await c.get(f"/sources/{sources[0]['id']}/text")
        assert "file body text" in text.text


async def test_partial_source_failure_still_returns_201_with_errors() -> None:
    engine, app = make_app(max_upload_bytes=8)
    nb = await engine.notebooks.create(title="Mixed")
    async with client(app) as c:
        res = await c.post(
            f"/notebooks/{nb.id}/sources",
            data={"text": "short note"},
            files=[("file", ("big.txt", b"this file is much too large", "text/plain"))],
        )
        assert res.status_code == 201
        body = res.json()
        assert len(body["sources"]) == 1
        assert len(body["errors"]) == 1
        assert body["errors"][0]["name"] == "big.txt"
        assert body["errors"][0]["code"] == "too_large"


async def test_chat_sse_stream_non_streaming_chat_and_history() -> None:
    engine, app = make_app()
    nb_id = await notebook_with_source(engine)
    async with client(app) as c:
        res = await c.post(f"/notebooks/{nb_id}/chat", json={"message": "What works?"})
        assert res.status_code == 200
        events = parse_sse(res.text)
        types = [e["type"] for e in events]
        assert "text" in types
        assert "citation" in types
        assert types[-1] == "done"
        assert re.search(r"\[1\]", events[-1]["message"]["content"])
        citation = next(e for e in events if e["type"] == "citation")
        assert citation["citation"]["index"] == 1
        assert citation["citation"]["sourceId"]

        plain = await c.post(f"/notebooks/{nb_id}/chat", json={"message": "Again?", "stream": False})
        assert plain.status_code == 200
        assert plain.json()["message"]["role"] == "assistant"

        history = (await c.get(f"/notebooks/{nb_id}/messages")).json()
        assert len(history["messages"]) == 4

        cleared = await c.delete(f"/notebooks/{nb_id}/messages")
        assert cleared.status_code == 204
        after = (await c.get(f"/notebooks/{nb_id}/messages")).json()
        assert after["messages"] == []


async def test_sse_closes_the_engine_generator_when_the_client_goes_away() -> None:
    closed: list[bool] = []

    async def events() -> AsyncIterator[dict[str, Any]]:
        try:
            yield {"type": "text", "delta": "a"}
            yield {"type": "text", "delta": "b"}
        finally:
            closed.append(True)

    frames = api_mod._sse(events())
    assert (await frames.__anext__()).startswith(b"data: ")
    await frames.aclose()
    assert closed == [True]


async def test_document_artifact_create_poll_and_audio_overview_without_tts() -> None:
    engine, app = make_app()
    nb_id = await notebook_with_source(engine)
    async with client(app) as c:
        created = await c.post(f"/notebooks/{nb_id}/artifacts", json={"kind": "summary"})
        assert created.status_code == 202
        queued = created.json()["artifact"]
        assert queued["status"] in ("queued", "generating", "ready")

        doc = await engine.artifacts.wait(queued["id"])
        assert doc.status == "ready"
        fetched = (await c.get(f"/artifacts/{queued['id']}")).json()["artifact"]
        assert fetched["status"] == "ready"
        assert "# Mock document" in fetched["content"]

        overview = await c.post(
            f"/notebooks/{nb_id}/artifacts", json={"kind": "audio_overview", "options": {"format": "brief"}}
        )
        audio_id = overview.json()["artifact"]["id"]
        audio_artifact = await engine.artifacts.wait(audio_id)
        assert audio_artifact.status == "ready"
        ready_json = (await c.get(f"/artifacts/{audio_id}")).json()["artifact"]
        assert "audio" in ready_json and ready_json["audio"] is None, "no TTS means audio: null"

        no_audio = await c.get(f"/artifacts/{audio_id}/audio")
        assert no_audio.status_code == 404
        assert no_audio.json()["error"]["code"] == "no_audio"

        listed = (await c.get(f"/notebooks/{nb_id}/artifacts")).json()
        assert len(listed["artifacts"]) == 2

        deleted = await c.delete(f"/artifacts/{queued['id']}")
        assert deleted.status_code == 204


async def test_guide_and_one_shot() -> None:
    engine, app = make_app()
    nb_id = await notebook_with_source(engine)
    async with client(app) as c:
        guide = (await c.get(f"/notebooks/{nb_id}/guide")).json()
        assert len(guide["guide"]["questions"]) == 3
        refreshed = await c.get(f"/notebooks/{nb_id}/guide?refresh=1")
        assert refreshed.status_code == 200

        shot = await c.post(
            "/oneshot",
            json={"sources": [{"type": "text", "text": "One shot content."}], "request": {"kind": "faq"}},
        )
        assert shot.status_code == 200
        body = shot.json()
        assert body["artifact"]["status"] == "ready"
        assert len(body["sources"]) == 1
        assert body["notebook"]["id"]
        assert "audio" not in body, "audio bytes are omitted from one-shot responses"

        file_rejected = await c.post("/oneshot", json={"sources": [{"type": "file", "filename": "a.txt"}]})
        assert file_rejected.status_code == 400
        assert file_rejected.json()["error"]["code"] == "unsupported_source"


async def test_audio_range_requests_and_download() -> None:
    engine, app = make_app(tts=FakeTts())
    nb_id = await notebook_with_source(engine)
    async with client(app) as c:
        created = await c.post(f"/notebooks/{nb_id}/artifacts", json={"kind": "audio_overview"})
        artifact_id = created.json()["artifact"]["id"]
        artifact = await engine.artifacts.wait(artifact_id)
        assert artifact.status == "ready"
        assert artifact.audio is not None, "fake TTS should render audio"

        url = f"/artifacts/{artifact_id}/audio"
        full = await c.get(url)
        assert full.status_code == 200
        assert full.headers["content-type"] == "audio/mpeg"
        assert full.headers["accept-ranges"] == "bytes"
        assert full.headers["cache-control"] == "private, max-age=3600"
        data = full.content
        total = len(data)
        assert full.headers["content-length"] == str(total)

        head = await c.get(url, headers={"Range": "bytes=0-3"})
        assert head.status_code == 206
        assert head.headers["content-range"] == f"bytes 0-3/{total}"
        assert head.content == data[:4]

        tail = await c.get(url, headers={"Range": "bytes=4-"})
        assert tail.status_code == 206
        assert tail.content == data[4:]

        suffix = await c.get(url, headers={"Range": "bytes=-5"})
        assert suffix.status_code == 206
        assert suffix.headers["content-range"] == f"bytes {total - 5}-{total - 1}/{total}"

        unsatisfiable = await c.get(url, headers={"Range": f"bytes={total + 10}-"})
        assert unsatisfiable.status_code == 416
        assert unsatisfiable.headers["content-range"] == f"bytes */{total}"

        inverted = await c.get(url, headers={"Range": "bytes=5-2"})
        assert inverted.status_code == 200, "a range whose end precedes its start is ignored"
        assert inverted.content == data

        download = await c.get(f"{url}?download=1")
        assert re.fullmatch(r'attachment; filename="[a-z0-9-]+\.mp3"', download.headers["content-disposition"])


# ───────────────────────────── live voice ─────────────────────────────


async def test_live_session_is_unavailable_without_a_live_voice() -> None:
    engine, app = make_app(live_voice=None)
    nb_id = await notebook_with_source(engine)
    async with client(app) as c:
        res = await c.post(f"/notebooks/{nb_id}/live/sessions", json={"sdp": "OFFER"})
    assert res.status_code == 501
    assert res.json()["error"]["code"] == "live_unavailable"


async def test_live_session_creates_a_gpt_live_session_without_leaking_the_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            201,
            json={"session": {"id": "live_123"}, "transport": {"type": "webrtc", "sdp": "ANSWER"}},
        )

    live = LiveVoice(api_key="test-key", transport=httpx.MockTransport(handler))
    engine, app = make_app(live_voice=live)
    nb_id = await notebook_with_source(engine)
    async with client(app) as c:
        res = await c.post(
            f"/notebooks/{nb_id}/live/sessions", json={"sdp": "OFFER", "mode": "conversation", "voice": "marin"}
        )

    assert len(seen) == 1
    upstream = seen[0]
    assert upstream.url.path.endswith("/live/sessions")
    assert upstream.headers["Authorization"] == "Bearer test-key"
    sent = json.loads(upstream.content)
    assert sent["session"]["model"] == "gpt-live-1"
    assert sent["session"]["delegation"]["type"] == "client"
    assert sent["transport"]["type"] == "webrtc"

    assert res.status_code == 201
    session = res.json()["session"]
    assert session["sessionId"] == "live_123"
    assert session["sdp"] == "ANSWER"
    assert session["mode"] == "conversation"
    assert session["model"] == "gpt-live-1"
    assert "test-key" not in res.text


async def test_live_session_rejects_a_missing_offer() -> None:
    live = LiveVoice(api_key="test-key", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    engine, app = make_app(live_voice=live)
    nb_id = await notebook_with_source(engine)
    async with client(app) as c:
        res = await c.post(f"/notebooks/{nb_id}/live/sessions", json={"mode": "conversation"})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "bad_request"


async def test_live_delegate_answers_from_the_sources() -> None:
    engine, app = make_app(live_voice=LiveVoice(api_key="test-key"))
    nb_id = await notebook_with_source(engine)
    async with client(app) as c:
        res = await c.post(
            f"/notebooks/{nb_id}/live/delegate",
            json={"transcript": [{"role": "user", "text": "What does Overtone do?"}]},
        )
        assert res.status_code == 200
        body = res.json()
        assert isinstance(body["answer"], str) and body["answer"]
        assert isinstance(body["citations"], list)

        no_question = await c.post(
            f"/notebooks/{nb_id}/live/delegate", json={"transcript": [{"role": "assistant", "text": "Hi."}]}
        )
        assert no_question.status_code == 400
        assert no_question.json()["error"]["code"] == "empty_transcript"


# ───────────────────────────── app wiring ─────────────────────────────


async def test_engine_is_exposed_on_app_state() -> None:
    engine = make_engine()
    app = create_app(engine)
    assert app.state.engine is engine


async def test_engine_is_created_lazily_on_first_request(monkeypatch: pytest.MonkeyPatch) -> None:
    built = make_engine()
    monkeypatch.setattr(api_mod, "create_engine", lambda: built)
    app = create_app()
    assert app.state.engine is None
    async with client(app) as c:
        assert (await c.get("/health")).status_code == 200
    assert app.state.engine is built


async def test_app_can_be_mounted_under_another_asgi_app() -> None:
    engine = make_engine()
    parent = FastAPI()
    parent.mount("/overtone", create_app(engine, api_key="k"))
    async with client(parent) as c:
        assert (await c.get("/overtone/health")).status_code == 200
        assert (await c.get("/overtone/notebooks")).status_code == 401
        listed = await c.get("/overtone/notebooks", headers={"Authorization": "Bearer k"})
        assert listed.status_code == 200
        assert listed.json() == {"notebooks": []}
        assert (await c.get("/overtone/nope", headers={"Authorization": "Bearer k"})).status_code == 404
