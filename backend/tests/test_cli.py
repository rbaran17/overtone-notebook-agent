"""Tests for overtone.cli (port of packages/core/test/cli.test.ts, plus real-engine integration cases).

Unit-style tests drive `main` with a fake engine to pin exact output. Integration-style tests
use a real engine over MemoryStorage with the no-network mock chat client.
"""

from __future__ import annotations

import json
import os
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from overtone import MemoryStorage, MockChatClient, __version__, create_engine
from overtone import cli
from overtone.cli import load_env_file, main, slugify, transcript_of
from overtone.engine import OneShotResult
from overtone.errors import OvertoneError
from overtone.models import (
    Artifact,
    ArtifactRequest,
    AudioHost,
    AudioScript,
    Capabilities,
    DialogueTurn,
    FileInput,
    Notebook,
    Source,
    TextInput,
    TtsCapabilities,
    TtsVoice,
    UrlInput,
)

TS = "2026-01-01T00:00:00.000Z"

ENV_KEYS = (
    "OPENAI_API_KEY",
    "ELEVENLABS_API_KEY",
    "OVERTONE_API_KEY",
    "OVERTONE_DATA_DIR",
    "OVERTONE_DEBUG",
    "OVERTONE_MOCK",
    "OVERTONE_MODEL",
    "OVERTONE_LIVE",
    "OVERTONE_LIVE_API_KEY",
    "OVERTONE_TTS_PROVIDER",
)

SCRIPT = AudioScript(
    title="My Show",
    summary="A short summary.",
    hosts=[AudioHost(name="Ana", voice="v1"), AudioHost(name="Ben", voice="v2")],
    turns=[DialogueTurn(speaker=0, text="Welcome."), DialogueTurn(speaker=1, text="Glad to be here.")],
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test in its own working directory with no provider settings leaking in."""
    monkeypatch.chdir(tmp_path)
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def source(tmp_path: Path) -> str:
    path = tmp_path / "notes.txt"
    path.write_text("Some notes about the topic.", encoding="utf-8")
    return str(path)


class Io:
    """Collects stdout and stderr text."""

    def __init__(self) -> None:
        self._out: list[str] = []
        self._err: list[str] = []

    def stdout(self, text: str) -> None:
        self._out.append(text)

    def stderr(self, text: str) -> None:
        self._err.append(text)

    @property
    def out(self) -> str:
        return "".join(self._out)

    @property
    def err(self) -> str:
        return "".join(self._err)


def run(argv: list[str], *, engine: Any = None, stdin: Any = None) -> tuple[int, Io]:
    io = Io()
    code = main(argv, stdout=io.stdout, stderr=io.stderr, engine=engine, stdin=stdin)
    return code, io


def fake(**methods: Any) -> Any:
    return SimpleNamespace(**methods)


def make_artifact(**over: Any) -> Artifact:
    data: dict[str, Any] = {
        "id": "a1",
        "notebook_id": "nb1",
        "kind": "audio_overview",
        "title": "My Show: Part 1!",
        "status": "ready",
        "request": ArtifactRequest(kind="audio_overview"),
        "created_at": TS,
        "updated_at": TS,
    }
    data.update(over)
    return Artifact(**data)


def make_notebook(**over: Any) -> Notebook:
    data: dict[str, Any] = {"id": "nb1", "title": "Research", "created_at": TS, "updated_at": TS, "source_count": 2}
    data.update(over)
    return Notebook(**data)


def one_shot_result(artifact: Artifact, audio: tuple[bytes, str] | None = None) -> OneShotResult:
    return OneShotResult(notebook=make_notebook(id="nb1"), sources=[], artifact=artifact, audio=audio)


# ───────────────────────── Unit: usage and version ─────────────────────────


def test_help_prints_usage_with_exit_0() -> None:
    code, io = run(["--help"])
    assert code == 0
    assert "Usage:" in io.out
    assert "podcast" in io.out
    assert "ask" in io.out


def test_no_arguments_prints_usage_with_exit_0() -> None:
    code, io = run([])
    assert code == 0
    assert "Commands:" in io.out


def test_unknown_command_exits_2_with_usage_on_stderr() -> None:
    code, io = run(["frobnicate"])
    assert code == 2
    assert "Unknown command: frobnicate" in io.err
    assert "Usage:" in io.err
    assert io.out == ""


def test_version_prints_package_version() -> None:
    code, io = run(["--version"])
    assert code == 0
    assert io.out.strip() == __version__
    code, io = run(["-v"])
    assert code == 0
    assert io.out.strip() == __version__


def test_unrecognized_option_exits_2() -> None:
    code, io = run(["notebooks", "list", "--bogus"], engine=fake())
    assert code == 2
    assert "unrecognized arguments: --bogus" in io.err


# ───────────────────────── Unit: info and notebooks ─────────────────────────


def test_info_prints_capabilities_in_prose_and_as_json() -> None:
    engine = fake(capabilities=lambda: Capabilities(version=__version__, model="claude-test", tts=None))

    code, prose = run(["info", "--data-dir", "elsewhere"], engine=engine)
    assert code == 0, prose.err
    assert "claude-test" in prose.out
    assert "OPENAI_API_KEY" in prose.out
    assert "Data dir:  " in prose.out

    code, js = run(["info", "--json"], engine=engine)
    assert code == 0
    parsed = json.loads(js.out)
    assert parsed["model"] == "claude-test"
    assert parsed["tts"] is None
    assert parsed["dataDir"] is not None


def test_info_names_the_tts_provider_when_configured() -> None:
    tts = TtsCapabilities(
        provider="openai", label="OpenAI TTS", voices=[TtsVoice(id="alloy", label="Alloy")], default_voices=["alloy"]
    )
    engine = fake(capabilities=lambda: Capabilities(version=__version__, model="m", tts=tts))
    code, io = run(["info", "--ephemeral"], engine=engine)
    assert code == 0
    assert "OpenAI TTS (openai)" in io.out
    assert "(ephemeral, in memory)" in io.out


def test_notebooks_list_prints_rows_and_an_array_with_json() -> None:
    notebooks = [make_notebook()]
    engine = fake(notebooks=SimpleNamespace(list=_async_return(notebooks)))

    code, prose = run(["notebooks", "list"], engine=engine)
    assert code == 0
    assert "nb1" in prose.out
    assert "Research" in prose.out

    code, js = run(["notebooks", "list", "--json"], engine=engine)
    assert code == 0
    assert json.loads(js.out) == [notebooks[0].to_json()]


def test_notebooks_list_reports_when_empty() -> None:
    engine = fake(notebooks=SimpleNamespace(list=_async_return([])))
    code, io = run(["notebooks", "list"], engine=engine)
    assert code == 0
    assert io.out == "No notebooks yet.\n"


def test_notebooks_create_joins_the_title_and_prints_the_id() -> None:
    seen: list[str | None] = []

    async def create(*, title: str | None = None, **_: Any) -> Notebook:
        seen.append(title)
        return make_notebook(id="nb7", title=title or "")

    engine = fake(notebooks=SimpleNamespace(create=create))
    code, io = run(["notebooks", "create", "My", "Notes"], engine=engine)
    assert code == 0
    assert seen == ["My Notes"]
    assert io.out == "nb7\n"

    code, js = run(["--json", "notebooks", "create", "Solo"], engine=engine)
    assert code == 0
    assert json.loads(js.out)["id"] == "nb7"


def test_notebooks_delete_and_missing_arguments() -> None:
    deleted: list[str] = []

    async def delete(id: str) -> None:
        deleted.append(id)

    engine = fake(notebooks=SimpleNamespace(delete=delete))
    code, io = run(["notebooks", "delete", "nb1"], engine=engine)
    assert code == 0
    assert deleted == ["nb1"]
    assert io.out == "Deleted notebook nb1.\n"

    code, io = run(["notebooks", "delete"], engine=engine)
    assert code == 1
    assert "Missing notebook id" in io.err

    code, io = run(["notebooks", "bogus"], engine=engine)
    assert code == 1
    assert "Usage: overtone notebooks" in io.err


def test_global_flags_are_accepted_before_or_after_the_command() -> None:
    engine = fake(notebooks=SimpleNamespace(list=_async_return([make_notebook()])))
    code, before = run(["--json", "notebooks", "list"], engine=engine)
    code2, after = run(["notebooks", "list", "--json", "--data-dir", "x"], engine=engine)
    assert code == code2 == 0
    assert json.loads(before.out) == json.loads(after.out)


def test_sources_list_add_and_delete() -> None:
    src = Source(
        id="s1", notebook_id="nb1", kind="text", title="Notes", char_count=5, excerpt="hi", created_at=TS
    )
    deleted: list[str] = []

    async def add(notebook_id: str, item: Any) -> Source:
        return src

    async def delete(id: str) -> None:
        deleted.append(id)

    engine = fake(
        sources=SimpleNamespace(list=_async_return([src]), add=add, delete=delete),
    )
    code, io = run(["sources", "list", "nb1"], engine=engine)
    assert code == 0
    assert io.out == "s1  text  Notes\n"

    code, io = run(["sources", "add", "nb1", "-"], engine=engine, stdin=lambda: "pasted text")
    assert code == 0
    assert "s1  text  Notes" in io.out

    code, io = run(["sources", "delete", "s1"], engine=engine)
    assert code == 0
    assert deleted == ["s1"]
    assert io.out == "Deleted source s1.\n"


def test_artifacts_list_prints_rows_and_rejects_other_subcommands() -> None:
    engine = fake(artifacts=SimpleNamespace(list=_async_return([make_artifact(kind="faq", status="ready", title="FAQ")])))
    code, io = run(["artifacts", "list", "nb1"], engine=engine)
    assert code == 0
    assert "a1  faq  ready  FAQ" in io.out

    code, io = run(["artifacts", "delete", "nb1"], engine=engine)
    assert code == 1
    assert "Usage: overtone artifacts list" in io.err


# ───────────────────────── Unit: podcast and doc ─────────────────────────


def test_podcast_writes_the_mp3_and_reports_progress_on_stderr(tmp_path: Path, source: str) -> None:
    audio_path = tmp_path / "episode.mp3"
    seen: list[ArtifactRequest] = []

    async def one_shot(sources: list[Any], request: ArtifactRequest | None = None, *, on_progress: Any = None, **_: Any) -> OneShotResult:
        assert request is not None
        seen.append(request)
        on_progress(make_artifact(status="generating", progress="Writing the script"))
        on_progress(make_artifact(status="generating", progress="Recording audio (1/2)"))
        return one_shot_result(make_artifact(script=SCRIPT), audio=(b"\x01\x02\x03", "audio/mpeg"))

    code, io = run(
        ["podcast", source, "--format", "brief", "--voices", "v1,v2", "--hosts", "Ana,Ben", "--out", str(audio_path)],
        engine=fake(one_shot=one_shot),
    )
    assert code == 0, io.err
    assert audio_path.read_bytes() == b"\x01\x02\x03"
    assert "Writing the script" in io.err
    assert "Recording audio (1/2)" in io.err
    assert "episode.mp3" in io.out
    request = seen[0]
    assert request.kind == "audio_overview"
    assert request.options is not None
    assert request.options.format == "brief"
    assert [h.to_json() for h in request.options.hosts or []] == [
        {"name": "Ana", "voice": "v1"},
        {"name": "Ben", "voice": "v2"},
    ]


def test_podcast_without_audio_writes_the_transcript_and_exits_0(tmp_path: Path, source: str) -> None:
    script_path = tmp_path / "transcript.txt"

    async def one_shot(*_: Any, **__: Any) -> OneShotResult:
        return one_shot_result(make_artifact(script=SCRIPT), audio=None)

    code, io = run(["podcast", source, "--script-out", str(script_path)], engine=fake(one_shot=one_shot))
    assert code == 0, io.err
    assert "No audio rendered" in io.err
    text = script_path.read_text(encoding="utf-8")
    assert re.match(r"^My Show\n\nA short summary\.", text)
    assert "Ana: Welcome." in text
    assert "Ben: Glad to be here." in text


def test_podcast_without_script_out_falls_back_to_slug_txt_in_cwd(tmp_path: Path, source: str) -> None:
    async def one_shot(*_: Any, **__: Any) -> OneShotResult:
        return one_shot_result(make_artifact(script=SCRIPT, audio_error="quota exceeded"), audio=None)

    code, io = run(["podcast", source], engine=fake(one_shot=one_shot))
    assert code == 0, io.err
    assert "quota exceeded" in io.err
    assert (tmp_path / "my-show-part-1.txt").is_file()


def test_podcast_script_out_json_writes_the_audio_script(tmp_path: Path, source: str) -> None:
    json_path = tmp_path / "script.json"

    async def one_shot(*_: Any, **__: Any) -> OneShotResult:
        return one_shot_result(make_artifact(script=SCRIPT), audio=None)

    code, io = run(["podcast", source, "--script-out", str(json_path)], engine=fake(one_shot=one_shot))
    assert code == 0, io.err
    assert json.loads(json_path.read_text(encoding="utf-8")) == SCRIPT.to_json()


def test_podcast_json_output_describes_the_result(source: str) -> None:
    async def one_shot(*_: Any, **__: Any) -> OneShotResult:
        return one_shot_result(make_artifact(script=SCRIPT), audio=None)

    code, io = run(["podcast", source, "--json", "--script-out", "out.json"], engine=fake(one_shot=one_shot))
    assert code == 0, io.err
    parsed = json.loads(io.out)
    assert parsed["artifact"]["kind"] == "audio_overview"
    assert parsed["audioPath"] is None
    assert parsed["scriptPath"].endswith("out.json")


def test_doc_prints_markdown_to_stdout(source: str) -> None:
    async def one_shot(sources: list[Any], request: ArtifactRequest, **_: Any) -> OneShotResult:
        assert request.kind == "faq"
        return one_shot_result(make_artifact(kind="faq", content="# Summary\n\nHello."), audio=None)

    code, io = run(["doc", source, "--kind", "faq"], engine=fake(one_shot=one_shot))
    assert code == 0, io.err
    assert io.out == "# Summary\n\nHello.\n"


def test_doc_writes_to_out_and_prints_the_path(tmp_path: Path, source: str) -> None:
    out = tmp_path / "nested" / "guide.md"

    async def one_shot(*_: Any, **__: Any) -> OneShotResult:
        return one_shot_result(make_artifact(kind="study_guide", content="# Guide"), audio=None)

    code, io = run(["doc", source, "--kind", "study_guide", "--out", str(out)], engine=fake(one_shot=one_shot))
    assert code == 0, io.err
    assert out.read_text(encoding="utf-8") == "# Guide\n"
    assert io.out == f"{os.path.abspath(out)}\n"


def test_doc_custom_without_prompt_is_rejected(source: str) -> None:
    code, io = run(["doc", source, "--kind", "custom"], engine=fake())
    assert code == 1
    assert "requires --prompt" in io.err


def test_invalid_format_is_rejected(source: str) -> None:
    code, io = run(["podcast", source, "--format", "rap"], engine=fake())
    assert code == 1
    assert 'Invalid --format "rap"' in io.err


# ───────────────────────── Unit: ask, chat, errors ─────────────────────────


def _chat_engine(events: list[dict[str, Any]], created: list[str] | None = None) -> Any:
    async def create(*, title: str | None = None, **_: Any) -> Notebook:
        if created is not None and title is not None:
            created.append(title)
        return make_notebook(id="nb9", title=title or "Untitled notebook")

    async def add(notebook_id: str, item: Any) -> Source:
        return Source(id="s1", notebook_id=notebook_id, kind="file", title="notes.txt", char_count=1, excerpt="x", created_at=TS)

    async def stream(notebook_id: str, message: str, **_: Any):  # type: ignore[no-untyped-def]
        for event in events:
            yield event

    return fake(
        notebooks=SimpleNamespace(create=create),
        sources=SimpleNamespace(add=add),
        chat=SimpleNamespace(stream=stream),
    )


def test_ask_creates_a_notebook_streams_deltas_and_prints_footnotes(source: str) -> None:
    citation = {"index": 1, "sourceId": "s1", "sourceTitle": "notes.txt", "quote": "Some notes   about the topic."}
    events = [
        {"type": "text", "delta": "Hello "},
        {"type": "text", "delta": "world[1]."},
        {"type": "citation", "citation": citation},
        {
            "type": "done",
            "message": {
                "id": "m1",
                "notebookId": "nb9",
                "role": "assistant",
                "content": "Hello world[1].",
                "citations": [citation],
                "createdAt": TS,
            },
        },
    ]
    code, io = run(["ask", source, "-q", "What is it?"], engine=_chat_engine(events))
    assert code == 0, io.err
    assert "Hello world[1]." in io.out
    assert "Sources:" in io.out
    assert '[1] notes.txt — "Some notes about the topic."' in io.out
    assert "Notebook: nb9" in io.err


def test_ask_json_prints_one_document_and_does_not_stream(source: str) -> None:
    events = [
        {"type": "text", "delta": "Hi"},
        {"type": "done", "message": {"id": "m1", "notebookId": "nb9", "role": "assistant", "content": "Hi", "createdAt": TS}},
    ]
    code, io = run(["ask", source, "-q", "Q", "--json"], engine=_chat_engine(events))
    assert code == 0, io.err
    parsed = json.loads(io.out)
    assert parsed["message"]["content"] == "Hi"
    assert "Notebook:" not in io.err


def test_ask_names_the_notebook_after_the_first_file(source: str) -> None:
    created: list[str] = []
    events = [{"type": "done", "message": {"id": "m1", "notebookId": "nb9", "role": "assistant", "content": "", "createdAt": TS}}]
    code, _ = run(["ask", source, "-q", "Q"], engine=_chat_engine(events, created))
    assert code == 0
    assert created == ["notes.txt"]


def test_ask_exits_1_with_the_message_when_the_stream_reports_an_error(source: str) -> None:
    events = [{"type": "error", "error": "model overloaded"}]
    code, io = run(["ask", source, "--question", "hi"], engine=_chat_engine(events))
    assert code == 1
    assert "Error: model overloaded" in io.err


def test_ask_requires_a_question(source: str) -> None:
    code, io = run(["ask", source], engine=_chat_engine([]))
    assert code == 1
    assert 'ask needs a question' in io.err


def test_ask_reads_stdin_for_the_dash_source() -> None:
    added: list[Any] = []

    async def add(notebook_id: str, item: Any) -> Source:
        added.append(item)
        return Source(id="s1", notebook_id=notebook_id, kind="text", title="Notes", char_count=1, excerpt="x", created_at=TS)

    async def create(*, title: str | None = None, **_: Any) -> Notebook:
        return make_notebook(id="nb9", title=title or "")

    async def stream(notebook_id: str, message: str, **_: Any):  # type: ignore[no-untyped-def]
        yield {"type": "done", "message": {"id": "m1", "notebookId": "nb9", "role": "assistant", "content": "", "createdAt": TS}}

    engine = fake(
        notebooks=SimpleNamespace(create=create), sources=SimpleNamespace(add=add), chat=SimpleNamespace(stream=stream)
    )
    code, io = run(["ask", "-", "-q", "Q"], engine=engine, stdin=lambda: "Pasted notes")
    assert code == 0, io.err
    assert len(added) == 1
    assert isinstance(added[0], TextInput)
    assert added[0].text == "Pasted notes"


def test_url_sources_become_url_inputs() -> None:
    added: list[Any] = []

    async def add(notebook_id: str, item: Any) -> Source:
        added.append(item)
        return Source(id="s1", notebook_id=notebook_id, kind="url", title="x", char_count=1, excerpt="x", created_at=TS)

    engine = fake(sources=SimpleNamespace(add=add))
    code, io = run(["sources", "add", "nb1", "https://example.com/post"], engine=engine)
    assert code == 0, io.err
    assert isinstance(added[0], UrlInput)
    assert added[0].url == "https://example.com/post"


def test_chat_streams_the_answer_and_requires_a_message() -> None:
    events = [
        {"type": "text", "delta": "Answer."},
        {"type": "done", "message": {"id": "m1", "notebookId": "nb1", "role": "assistant", "content": "Answer.", "createdAt": TS}},
    ]
    code, io = run(["chat", "nb1", "what", "is", "this"], engine=_chat_engine(events))
    assert code == 0, io.err
    assert io.out.startswith("Answer.")

    code, io = run(["chat", "nb1"], engine=_chat_engine(events))
    assert code == 1
    assert "chat needs a message" in io.err


def test_a_missing_source_file_exits_1_with_a_clear_message(tmp_path: Path) -> None:
    code, io = run(["doc", str(tmp_path / "does-not-exist.pdf")], engine=fake())
    assert code == 1
    assert re.search(r"Source file not found: .*does-not-exist\.pdf", io.err)


def test_an_overtone_error_from_the_engine_exits_1_with_error_on_stderr() -> None:
    async def list_notebooks() -> list[Notebook]:
        raise OvertoneError("Storage is unavailable.", status=503, code="unavailable")

    code, io = run(["notebooks", "list"], engine=fake(notebooks=SimpleNamespace(list=list_notebooks)))
    assert code == 1
    assert io.err == "Error: Storage is unavailable.\n"


def test_unexpected_exceptions_also_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    async def list_notebooks() -> list[Notebook]:
        raise RuntimeError("boom")

    code, io = run(["notebooks", "list"], engine=fake(notebooks=SimpleNamespace(list=list_notebooks)))
    assert code == 1
    assert io.err == "Error: boom\n"


def test_slugify_lowercases_collapses_punctuation_trims_and_falls_back() -> None:
    assert slugify("  My Show: Part 1!  ") == "my-show-part-1"
    assert slugify("!!!") == "overtone"
    assert len(slugify("a" * 100)) == 60


def test_transcript_of_handles_missing_script_and_speaker_fallback() -> None:
    assert transcript_of(None, "Title") == "Title\n"
    orphan = AudioScript(title="T", summary="", hosts=[], turns=[DialogueTurn(speaker=0, text="Hi")])
    assert transcript_of(orphan) == "T\n\nSpeaker 1: Hi\n"


# ───────────────────────── Unit: audio and generate ─────────────────────────


def test_audio_command_generates_and_writes_the_mp3(tmp_path: Path) -> None:
    out = tmp_path / "show.mp3"
    seen: list[ArtifactRequest] = []

    async def generate(notebook_id: str, request: ArtifactRequest, **_: Any) -> Artifact:
        seen.append(request)
        return make_artifact(script=SCRIPT)

    async def get_audio(artifact_id: str) -> tuple[bytes, str]:
        return (b"mp3", "audio/mpeg")

    engine = fake(artifacts=SimpleNamespace(generate=generate, get_audio=get_audio))
    code, io = run(["audio", "nb1", "--out", str(out), "--length", "short"], engine=engine)
    assert code == 0, io.err
    assert out.read_bytes() == b"mp3"
    assert seen[0].options is not None and seen[0].options.length == "short"


def test_generate_command_prints_markdown() -> None:
    async def generate(notebook_id: str, request: ArtifactRequest, **_: Any) -> Artifact:
        assert request.kind == "timeline"
        return make_artifact(kind="timeline", content="- 2020: start")

    code, io = run(["generate", "nb1", "--kind", "timeline"], engine=fake(artifacts=SimpleNamespace(generate=generate)))
    assert code == 0, io.err
    assert io.out == "- 2020: start\n"


# ───────────────────────── Unit: serve ─────────────────────────


def test_serve_wires_the_app_and_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    created: dict[str, Any] = {}
    served: dict[str, Any] = {}

    def create_app(engine: Any = None, *, api_key: Any = None, cors: Any = None, **_: Any) -> str:
        created.update(engine=engine, api_key=api_key, cors=cors)
        return "APP"

    async def run_server(app: Any, *, host: str, port: int) -> None:
        served.update(app=app, host=host, port=port)

    fake_api = types.ModuleType("overtone.api")
    fake_api.create_app = create_app  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "overtone.api", fake_api)
    monkeypatch.setattr(cli, "_run_server", run_server)

    engine = fake()
    code, io = run(
        ["serve", "--port", "9000", "--host", "0.0.0.0", "--api-key", "k", "--cors", "http://a.test,http://b.test"],
        engine=engine,
    )
    assert code == 0, io.err
    assert created == {"engine": engine, "api_key": "k", "cors": ["http://a.test", "http://b.test"]}
    assert served == {"app": "APP", "host": "0.0.0.0", "port": 9000}
    assert "http://0.0.0.0:9000 (bearer token required)" in io.err


def test_serve_bare_cors_allows_any_origin_and_api_key_falls_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: dict[str, Any] = {}

    def create_app(engine: Any = None, *, api_key: Any = None, cors: Any = None, **_: Any) -> str:
        created.update(api_key=api_key, cors=cors)
        return "APP"

    async def run_server(app: Any, *, host: str, port: int) -> None:
        return None

    fake_api = types.ModuleType("overtone.api")
    fake_api.create_app = create_app  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "overtone.api", fake_api)
    monkeypatch.setattr(cli, "_run_server", run_server)
    monkeypatch.setenv("OVERTONE_API_KEY", "from-env")

    code, io = run(["serve", "--cors"], engine=fake())
    assert code == 0, io.err
    assert created == {"api_key": "from-env", "cors": True}
    assert "http://127.0.0.1:8787" in io.err


def test_serve_rejects_an_invalid_port() -> None:
    code, io = run(["serve", "--port", "99999"], engine=fake())
    assert code == 1
    assert 'Invalid --port "99999"' in io.err


# ───────────────────────── Unit: .env loader ─────────────────────────


def test_env_loader_does_not_override_existing_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OVT_CLI_TEST_EXISTING", "keep-me")
    monkeypatch.setenv("OVT_CLI_TEST_NEW", "placeholder")
    monkeypatch.delenv("OVT_CLI_TEST_NEW")  # so teardown removes the variable again
    env = tmp_path / "custom.env"
    env.write_text(
        "OVT_CLI_TEST_EXISTING=from-file\nOVT_CLI_TEST_NEW=fresh\n",
        encoding="utf-8",
    )
    loaded = load_env_file(env)
    assert "OVT_CLI_TEST_NEW" in loaded
    assert "OVT_CLI_TEST_EXISTING" not in loaded
    assert __import__("os").environ["OVT_CLI_TEST_EXISTING"] == "keep-me"
    assert __import__("os").environ["OVT_CLI_TEST_NEW"] == "fresh"


def test_env_loader_ignores_comments_blank_and_malformed_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for key in ("OVT_CLI_TEST_A", "OVT_CLI_TEST_B", "OVT_CLI_TEST_C", "OVT_CLI_TEST_D", "OVT_CLI_TEST_E"):
        monkeypatch.setenv(key, "x")
        monkeypatch.delenv(key)
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                "NOEQUALSSIGN",
                "=no-key",
                "1BAD=value",
                "OVT_CLI_TEST_A=plain",
                'OVT_CLI_TEST_B="double quoted"',
                "OVT_CLI_TEST_C='single quoted'",
                "export OVT_CLI_TEST_D=exported",
                "OVT_CLI_TEST_E=value # trailing comment",
            ]
        ),
        encoding="utf-8",
    )
    loaded = load_env_file(env)
    assert sorted(loaded) == ["OVT_CLI_TEST_A", "OVT_CLI_TEST_B", "OVT_CLI_TEST_C", "OVT_CLI_TEST_D", "OVT_CLI_TEST_E"]
    assert os.environ["OVT_CLI_TEST_A"] == "plain"
    assert os.environ["OVT_CLI_TEST_B"] == "double quoted"
    assert os.environ["OVT_CLI_TEST_C"] == "single quoted"
    assert os.environ["OVT_CLI_TEST_D"] == "exported"
    assert os.environ["OVT_CLI_TEST_E"] == "value"
    assert not any(k.startswith("NOEQUALS") or k == "1BAD" for k in os.environ)


def test_env_loader_tolerates_a_missing_file(tmp_path: Path) -> None:
    assert load_env_file(tmp_path / "absent.env") == []


def test_main_loads_dotenv_from_cwd_when_no_engine_is_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    monkeypatch.setenv("OVT_CLI_TEST_CWD", "placeholder")
    monkeypatch.delenv("OVT_CLI_TEST_CWD")
    (tmp_path / ".env").write_text("OVT_CLI_TEST_CWD=from-dotenv\n", encoding="utf-8")
    code, io = run(["--version"])
    assert code == 0
    assert os.environ["OVT_CLI_TEST_CWD"] == "from-dotenv"


# ───────────────────────── Integration: real engine, mock model ─────────────────────────


@pytest.fixture
def real_engine() -> Any:
    return create_engine(storage=MemoryStorage(), chat_client=MockChatClient(delay=0), tts=None, live_voice=None)


def test_podcast_with_real_engine_writes_script_json_and_explains_missing_tts(
    tmp_path: Path, source: str, real_engine: Any
) -> None:
    script_path = tmp_path / "x.json"
    code, io = run(["podcast", source, "--script-out", str(script_path)], engine=real_engine)
    assert code == 0, io.err
    assert "No audio rendered" in io.err
    assert "text-to-speech provider" in io.err
    parsed = json.loads(script_path.read_text(encoding="utf-8"))
    assert parsed["turns"], "the mock model should produce dialogue turns"
    assert "hosts" in parsed


def test_podcast_transcript_with_real_engine_when_no_script_out(tmp_path: Path, source: str, real_engine: Any) -> None:
    code, io = run(["podcast", source], engine=real_engine)
    assert code == 0, io.err
    assert list(tmp_path.glob("*.txt")), "a transcript file should be written in the working directory"


def test_doc_with_real_engine_prints_markdown(source: str, real_engine: Any) -> None:
    code, io = run(["doc", source, "--kind", "faq"], engine=real_engine)
    assert code == 0, io.err
    assert io.out.strip(), "the mock model should produce a document"
    assert io.out.endswith("\n")


def test_ask_with_real_engine_prints_answer_and_sources(source: str, real_engine: Any) -> None:
    code, io = run(["ask", source, "-q", "What are the notes about?"], engine=real_engine)
    assert code == 0, io.err
    assert io.out.strip()
    assert "Sources:" in io.out


def test_notebook_pipeline_with_real_engine(source: str, real_engine: Any) -> None:
    code, io = run(["notebooks", "create", "Pipeline", "Test"], engine=real_engine)
    assert code == 0, io.err
    notebook_id = io.out.strip()
    assert notebook_id

    code, io = run(["sources", "add", notebook_id, source], engine=real_engine)
    assert code == 0, io.err
    assert "notes" in io.out

    code, io = run(["chat", notebook_id, "hello", "there"], engine=real_engine)
    assert code == 0, io.err
    assert io.out.strip()

    code, io = run(["generate", notebook_id, "--kind", "summary"], engine=real_engine)
    assert code == 0, io.err

    code, io = run(["artifacts", "list", notebook_id], engine=real_engine)
    assert code == 0, io.err
    assert "summary" in io.out

    code, io = run(["notebooks", "list"], engine=real_engine)
    assert code == 0, io.err
    assert "Pipeline Test" in io.out


def test_json_variants_with_real_engine_parse(source: str, real_engine: Any) -> None:
    code, io = run(["notebooks", "create", "Json", "Check", "--json"], engine=real_engine)
    assert code == 0, io.err
    notebook_id = json.loads(io.out)["id"]

    code, io = run(["sources", "add", notebook_id, source, "--json"], engine=real_engine)
    assert code == 0, io.err
    assert json.loads(io.out)[0]["title"] == "notes"

    code, io = run(["notebooks", "list", "--json"], engine=real_engine)
    assert code == 0, io.err
    assert json.loads(io.out)[0]["id"] == notebook_id

    code, io = run(["chat", notebook_id, "hi", "--json"], engine=real_engine)
    assert code == 0, io.err
    assert json.loads(io.out)["message"]["role"] == "assistant"

    code, io = run(["artifacts", "list", notebook_id, "--json"], engine=real_engine)
    assert code == 0, io.err
    assert json.loads(io.out) == []

    code, io = run(["doc", source, "--kind", "faq", "--json"], engine=real_engine)
    assert code == 0, io.err
    assert json.loads(io.out)["artifact"]["kind"] == "faq"


# ───────────────────────── helpers ─────────────────────────


def _async_return(value: Any) -> Any:
    async def fn(*_: Any, **__: Any) -> Any:
        return value

    return fn
