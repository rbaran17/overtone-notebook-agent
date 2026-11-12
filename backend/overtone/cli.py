"""Command-line interface: the Python port of the TypeScript `overtone` CLI.

Results go to stdout; progress and errors go to stderr so stdout stays pipeable.
Exit codes: 0 success, 1 for errors reported as `Error: ...`, 2 for bad usage.

    overtone podcast ./report.pdf https://example.com/post --format deep_dive --out episode.mp3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import traceback
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NoReturn

from . import __version__
from .engine import Engine, OneShotResult, create_engine
from .errors import OvertoneError
from .models import (
    Artifact,
    ArtifactRequest,
    AudioHost,
    AudioOptions,
    AudioScript,
    Citation,
    FileInput,
    SourceInput,
    TextInput,
    UrlInput,
)
from .storage import FileStorage, MemoryStorage

FORMATS: tuple[str, ...] = ("deep_dive", "brief", "debate", "critique", "solo")
LENGTHS: tuple[str, ...] = ("short", "medium", "long")
KINDS: tuple[str, ...] = ("summary", "briefing", "study_guide", "faq", "timeline", "custom")
DEFAULT_DATA_DIR = ".overtone"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

USAGE = f"""overtone {__version__} - headless notebook engine (sources in, Claude answers, podcasts and documents out)

Usage:
  overtone <command> [args] [options]

Commands:
  info                                 Show model, TTS provider, data dir and version
  serve                                Run the HTTP API (--port, --host, --api-key, --cors)
  podcast <source...>                  Make an audio overview (podcast) from sources
  doc <source...>                      Make a document (summary, briefing, study_guide, faq, timeline, custom)
  ask <source...> -q "question"        Ask a question about sources; prints a cited answer
  notebooks list                       List notebooks
  notebooks create <title>             Create a notebook
  notebooks delete <id>                Delete a notebook
  sources list <notebookId>            List a notebook's sources
  sources add <notebookId> <source...> Add sources to a notebook
  sources delete <sourceId>            Delete a source
  chat <notebookId> <message...>       Chat in an existing notebook (keeps its history)
  audio <notebookId>                   Make an audio overview from a notebook's sources
  generate <notebookId>                Make a document from a notebook's sources
  artifacts list <notebookId>          List a notebook's generated artifacts
  eval                                 Run the evaluation suite (--mock, --report, --only)

Sources: a file path, an http(s):// URL, or - to read plain text from stdin.

Global options (accepted before or after the command):
  --data-dir <dir>   Where notebooks are stored (default: $OVERTONE_DATA_DIR or ./.overtone)
  --ephemeral        Keep everything in memory; nothing is written to disk
  --model <id>       Claude model id
  --json             Print machine-readable JSON to stdout
  -h, --help         Show this help
  -v, --version      Print the version

Audio options (podcast, audio):
  --format <deep_dive|brief|debate|critique|solo>  --length <short|medium|long>
  --prompt "..."     Steering instructions          --hosts "Ana,Ben"  --voices "v1,v2"
  --language <L>     Output language                --out <file.mp3>   --script-out <file>

Document options (doc, generate):
  --kind <summary|briefing|study_guide|faq|timeline|custom>  --prompt "..." (required for custom)
  --out <file.md>

Other:
  serve: --port <n> --host <h> --api-key <k> (or $OVERTONE_API_KEY) --cors[=origin,...]
  ask:   -q, --question "..."
  eval:  --mock (offline stand-in model)  --report <file.md>  --only <area|case,...>

Examples:
  overtone podcast ./report.pdf https://example.com/post --format deep_dive --out episode.mp3
  overtone doc ./notes.md --kind study_guide --out guide.md
  overtone ask ./paper.pdf -q "What are the main limitations?"
  cat notes.txt | overtone podcast - --format brief
  overtone serve --port 8787 --cors http://localhost:3000
  overtone eval --mock --report eval-report.md
"""

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


class _UsageError(Exception):
    """Raised by the argument parser on bad command-line usage (exit 2)."""


class _ArgParser(argparse.ArgumentParser):
    """ArgumentParser that raises instead of exiting, so `main` controls the exit code."""

    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


# ───────────────────────────── Helpers ─────────────────────────────


def slugify(text: str) -> str:
    """Lowercase, non-alphanumerics to "-", trimmed, max 60 chars; "overtone" when empty."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower())
    slug = re.sub(r"^-+|-+$", "", slug)[:60]
    slug = re.sub(r"-+$", "", slug)
    return slug or "overtone"


def transcript_of(script: AudioScript | None, title: str = "") -> str:
    """Plain-text transcript: title, summary, then "Name: text" paragraphs."""
    if script is None:
        return f"{title}\n"
    parts = [script.title or title]
    if script.summary:
        parts.append(script.summary)
    for turn in script.turns:
        index = turn.speaker
        name = script.hosts[index].name if 0 <= index < len(script.hosts) else f"Speaker {index + 1}"
        parts.append(f"{name}: {turn.text}")
    return "\n\n".join(parts) + "\n"


def load_env_file(path: str | os.PathLike[str] = ".env") -> list[str]:
    """Load KEY=VALUE lines into os.environ without overriding existing variables.

    Blank lines, `#` comments and malformed lines are ignored. Values are never printed.
    Returns the names of the variables that were set.
    """
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return []
    loaded: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.split(r"\s#", value, maxsplit=1)[0].rstrip()
        if key in os.environ:
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded


def _jsonable(value: Any) -> Any:
    """Turn models (and containers of models) into plain camelCase JSON data."""
    if hasattr(value, "to_json"):
        return value.to_json()
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def _dumps(value: Any) -> str:
    return json.dumps(_jsonable(value), indent=2, ensure_ascii=False)


def _snippet(text: str, limit: int = 160) -> str:
    one_line = re.sub(r"\s+", " ", text).strip()
    return one_line if len(one_line) <= limit else f"{one_line[: limit - 3]}..."


def _print_rows(print_: Callable[[str], None], rows: list[list[str]], empty: str) -> None:
    if not rows:
        print_(empty)
        return
    widths = [max(len(r[c]) if c < len(r) else 0 for r in rows) for c in range(len(rows[0]))]
    for row in rows:
        cells = [cell.ljust(widths[c]) if c < len(row) - 1 else cell for c, cell in enumerate(row)]
        print_("  ".join(cells))


def _usage(message: str) -> OvertoneError:
    return OvertoneError(message, status=400, code="bad_request")


def _require(args: Sequence[str], what: str) -> None:
    if not args:
        raise _usage(f'Missing {what}. Run "overtone --help" for usage.')


def _check_enum(value: str | None, allowed: Sequence[str], flag: str) -> str | None:
    if value is None:
        return None
    if value not in allowed:
        raise _usage(f'Invalid {flag} "{value}". Expected one of: {", ".join(allowed)}.')
    return value


def _split_list(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _write_bytes(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def _to_source_input(arg: str, read_stdin: Callable[[], str]) -> SourceInput:
    """A path, an http(s) URL, or "-" (stdin text) as a SourceInput."""
    if arg == "-":
        return TextInput(text=read_stdin())
    if re.match(r"https?://", arg, re.IGNORECASE):
        return UrlInput(url=arg)
    if os.path.isdir(arg):
        raise OvertoneError(f"Source file not found: {arg}", status=404, code="not_found")
    try:
        with open(arg, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        raise OvertoneError(f"Source file not found: {arg}", status=404, code="not_found") from None
    except OSError as exc:
        raise OvertoneError(f"Could not read source {arg}: {exc.strerror or exc}", status=400, code="bad_request") from None
    return FileInput(filename=os.path.basename(arg), data=data)


def _title_of_source(source: SourceInput) -> str:
    if isinstance(source, FileInput):
        return source.filename
    if isinstance(source, UrlInput):
        return source.url
    return "Notes"


def _read_stdin() -> str:
    stream = sys.stdin
    if stream is None:
        return ""
    buffer = getattr(stream, "buffer", None)
    data: Any = buffer.read() if buffer is not None else stream.read()
    return data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data


def _use_utf8(stream: Any) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _write_stdout(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _write_stderr(text: str) -> None:
    sys.stderr.write(text)
    sys.stderr.flush()


async def _run_server(app: Any, *, host: str, port: int) -> None:
    """Serve the ASGI app until the process is interrupted (shares the caller's event loop)."""
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    await uvicorn.Server(config).serve()


# ───────────────────────────── Parser ──────────────────────────────


def _add_global_options(parser: argparse.ArgumentParser) -> None:
    """Options accepted by every command, before or after it. SUPPRESS keeps subcommand defaults from clobbering them."""
    s = argparse.SUPPRESS
    parser.add_argument("--data-dir", dest="data_dir", metavar="DIR", default=s)
    parser.add_argument("--model", metavar="ID", default=s)
    parser.add_argument("--json", action="store_true", default=s)
    parser.add_argument("--ephemeral", action="store_true", default=s)
    parser.add_argument("-h", "--help", dest="help", action="store_true", default=s)
    parser.add_argument("--format", metavar="FORMAT", default=s)
    parser.add_argument("--length", metavar="LENGTH", default=s)
    parser.add_argument("--prompt", metavar="TEXT", default=s)
    parser.add_argument("--hosts", metavar="NAMES", default=s)
    parser.add_argument("--voices", metavar="VOICES", default=s)
    parser.add_argument("--language", metavar="LANG", default=s)
    parser.add_argument("--out", metavar="PATH", default=s)
    parser.add_argument("--script-out", dest="script_out", metavar="PATH", default=s)
    parser.add_argument("--kind", metavar="KIND", default=s)
    parser.add_argument("-q", "--question", metavar="TEXT", default=s)
    parser.add_argument("--port", metavar="N", default=s)
    parser.add_argument("--host", metavar="HOST", default=s)
    parser.add_argument("--api-key", dest="api_key", metavar="KEY", default=s)
    parser.add_argument("--cors", nargs="?", const="*", metavar="ORIGINS", default=s)
    parser.add_argument("--mock", action="store_true", default=s)
    parser.add_argument("--report", metavar="PATH", default=s)
    parser.add_argument("--only", metavar="NAMES", default=s)


_COMMAND_HELP: dict[str, str] = {
    "info": "Show model, TTS provider, data dir and version",
    "serve": "Run the HTTP API",
    "podcast": "Make an audio overview (podcast) from sources",
    "doc": "Make a document from sources",
    "ask": "Ask a question about sources",
    "notebooks": "list | create <title> | delete <id>",
    "sources": "list <nb> | add <nb> <source...> | delete <sourceId>",
    "chat": "Chat in an existing notebook",
    "audio": "Make an audio overview from a notebook's sources",
    "generate": "Make a document from a notebook's sources",
    "artifacts": "list <notebookId>",
    "eval": "Run the evaluation suite",
}


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    _add_global_options(common)

    root = _ArgParser(prog="overtone", add_help=False, parents=[common])
    root.add_argument("-v", "--version", dest="version", action="store_true", default=argparse.SUPPRESS)
    subparsers = root.add_subparsers(dest="command", metavar="command", parser_class=_ArgParser)
    for name, help_text in _COMMAND_HELP.items():
        leaf = subparsers.add_parser(name, parents=[common], add_help=False, help=help_text)
        # Every positional (sources, notebook ids, words) lands here; interleaved extras are merged in main().
        leaf.add_argument("args", nargs="*", default=[], metavar="ARG")
    return root


# ───────────────────────────── Runner ──────────────────────────────


class _Runner:
    """Executes one parsed command. Holds the output callables and the engine."""

    def __init__(
        self,
        ns: argparse.Namespace,
        *,
        stdout: Callable[[str], None],
        stderr: Callable[[str], None],
        read_stdin: Callable[[], str],
        injected: Engine | None,
    ) -> None:
        self.ns = ns
        self.stdout = stdout
        self.stderr = stderr
        self.read_stdin = read_stdin
        self._injected = injected
        self._last_progress: str | None = None
        self.json = bool(self._opt("json", False))
        self.ephemeral = bool(self._opt("ephemeral", False))

    # ── small helpers ──

    def _opt(self, name: str, default: Any = None) -> Any:
        return getattr(self.ns, name, default)

    def print(self, text: str) -> None:
        self.stdout(f"{text}\n")

    def emit(self, value: Any, prose: Callable[[], None]) -> None:
        if self.json:
            self.print(_dumps(value))
        else:
            prose()

    def data_dir(self) -> str:
        return self._opt("data_dir") or os.environ.get("OVERTONE_DATA_DIR") or DEFAULT_DATA_DIR

    def engine(self) -> Engine:
        if self._injected is not None:
            return self._injected
        storage = MemoryStorage() if self.ephemeral else FileStorage(self.data_dir())
        return create_engine(storage=storage, model=self._opt("model") or None)

    def on_progress(self, artifact: Artifact) -> None:
        progress = artifact.progress
        if progress and progress != self._last_progress:
            self._last_progress = progress
            self.stderr(f"  {progress}\n")

    def sources_from(self, args: Sequence[str]) -> list[SourceInput]:
        return [_to_source_input(arg, self.read_stdin) for arg in args]

    def audio_options(self) -> AudioOptions:
        fields: dict[str, Any] = {}
        fmt = _check_enum(self._opt("format"), FORMATS, "--format")
        length = _check_enum(self._opt("length"), LENGTHS, "--length")
        if fmt:
            fields["format"] = fmt
        if length:
            fields["length"] = length
        if self._opt("prompt"):
            fields["instructions"] = self._opt("prompt")
        if self._opt("language"):
            fields["language"] = self._opt("language")

        names = _split_list(self._opt("hosts"))
        voices = _split_list(self._opt("voices"))
        count = max(len(names), len(voices))
        if count > 0:
            fields["hosts"] = [
                AudioHost(
                    name=names[i] if i < len(names) else f"Host {i + 1}",
                    voice=(voices[i] if i < len(voices) else None) or None,
                )
                for i in range(count)
            ]
        return AudioOptions(**fields)

    def document_kind(self) -> str:
        kind = _check_enum(self._opt("kind"), KINDS, "--kind") or "summary"
        if kind == "custom" and not self._opt("prompt"):
            raise _usage("--kind custom requires --prompt.")
        return kind

    # ── shared output paths ──

    async def stream_answer(self, engine: Engine, notebook_id: str, message: str) -> dict[str, Any]:
        """Streams a grounded answer to stdout and prints the footnotes."""
        streamed: list[str] = []
        collected: list[dict[str, Any]] = []
        done: dict[str, Any] | None = None
        async for event in engine.chat.stream(notebook_id, message):
            kind = event.get("type")
            if kind == "text":
                delta = str(event.get("delta", ""))
                streamed.append(delta)
                if not self.json:
                    self.stdout(delta)
            elif kind == "citation":
                collected.append(Citation.model_validate(event["citation"]).to_json())
            elif kind == "done":
                done = event["message"]
            elif kind == "error":
                raise OvertoneError(str(event.get("error")), status=500, code="chat_failed")
        if done is None:
            raise OvertoneError("The chat ended without an answer.", status=500, code="chat_failed")

        if self.json:
            self.print(_dumps({"message": done}))
            return done
        text = "".join(streamed)
        citations = done.get("citations") or collected
        if text and not text.endswith("\n"):
            self.stdout("\n")
        if citations:
            self.print("")
            self.print("Sources:")
            for c in citations:
                self.print(f'[{c["index"]}] {c["sourceTitle"]} — "{_snippet(c["quote"])}"')
        return done

    async def finish_audio(
        self, artifact: Artifact, audio: tuple[bytes, str] | None, extra: dict[str, Any]
    ) -> None:
        """Writes the MP3 (when rendered) and the script, then reports where they went."""
        slug = slugify(artifact.title or "overtone")
        audio_path: str | None = None
        if audio is not None:
            audio_path = os.path.abspath(self._opt("out") or f"{slug}.mp3")
            _write_bytes(audio_path, audio[0])
        elif artifact.audio_error:
            self.stderr(f"No audio rendered: {artifact.audio_error}\n")
        else:
            self.stderr(
                "No audio rendered: no text-to-speech provider is configured. "
                "Set OPENAI_API_KEY or ELEVENLABS_API_KEY to render audio.\n"
            )

        script_path: str | None = None
        script_out = self._opt("script_out")
        if script_out or audio is None:
            script_path = os.path.abspath(script_out or f"{slug}.txt")
            if script_path.lower().endswith(".json"):
                content = _dumps(artifact.script) + "\n"
            else:
                content = transcript_of(artifact.script, artifact.title)
            _write_text(script_path, content)

        def prose() -> None:
            if audio_path:
                self.print(f"Audio: {audio_path}")
            if script_path:
                self.print(f"Script: {script_path}")

        self.emit({**extra, "artifact": artifact, "audioPath": audio_path, "scriptPath": script_path}, prose)

    async def finish_document(self, artifact: Artifact, extra: dict[str, Any]) -> None:
        """Writes a document artifact's Markdown to --out, or to stdout."""
        content = artifact.content or ""
        if self.json:
            self.print(_dumps({**extra, "artifact": artifact}))
            return
        if content and not content.endswith("\n"):
            content += "\n"
        out = self._opt("out")
        if out:
            path = os.path.abspath(out)
            _write_text(path, content)
            self.print(path)
        else:
            self.stdout(content)

    # ── commands ──

    async def dispatch(self, command: str, args: list[str]) -> int:
        if command == "info":
            return self.info()
        if command == "serve":
            return await self.serve()
        if command == "podcast":
            return await self.podcast(args)
        if command == "doc":
            return await self.doc(args)
        if command == "ask":
            return await self.ask(args)
        if command == "notebooks":
            return await self.notebooks(args)
        if command == "sources":
            return await self.sources(args)
        if command == "chat":
            return await self.chat(args)
        if command == "audio":
            return await self.audio(args)
        if command == "generate":
            return await self.generate(args)
        if command == "artifacts":
            return await self.artifacts(args)
        if command == "eval":
            return await self.evaluate()
        self.stderr(f"Unknown command: {command}\n\n{USAGE}")
        return 2

    def info(self) -> int:
        engine = self.engine()
        caps = engine.capabilities()
        data_dir = None if self.ephemeral else os.path.abspath(self.data_dir())
        if self.json:
            self.print(_dumps({**caps.to_json(), "dataDir": data_dir}))
            return 0
        tts = caps.tts
        tts_line = (
            f"{tts.label} ({tts.provider})"
            if tts
            else "none — set OPENAI_API_KEY or ELEVENLABS_API_KEY for rendered audio"
        )
        self.print(f"Overtone {caps.version}")
        self.print(f"Model:     {caps.model}")
        self.print(f"TTS:       {tts_line}")
        self.print(f"Data dir:  {'(ephemeral, in memory)' if self.ephemeral else data_dir}")
        return 0

    async def serve(self) -> int:
        engine = self.engine()
        port = DEFAULT_PORT
        raw_port = self._opt("port")
        if raw_port is not None:
            try:
                port = int(raw_port)
            except ValueError:
                port = -1
            if not 0 <= port <= 65535:
                raise _usage(f'Invalid --port "{raw_port}".')
        host = self._opt("host") or DEFAULT_HOST
        api_key = self._opt("api_key") or os.environ.get("OVERTONE_API_KEY") or None
        cors: bool | list[str] | None = None
        raw_cors = self._opt("cors")
        if raw_cors is not None:
            cors = True if raw_cors.strip() == "*" else _split_list(raw_cors)

        try:
            from overtone.api import create_app
        except ImportError as exc:
            raise OvertoneError(f"The HTTP API is not available: {exc}", status=500, code="unavailable") from exc

        app = create_app(engine, api_key=api_key, cors=cors)
        self.stderr(f"Overtone API listening on http://{host}:{port}{' (bearer token required)' if api_key else ''}\n")
        await _run_server(app, host=host, port=port)
        self.stderr("Shutting down.\n")
        return 0

    async def podcast(self, args: list[str]) -> int:
        _require(args, "source (a file path, URL, or -)")
        sources = self.sources_from(args)
        engine = self.engine()
        options = self.audio_options()
        request = ArtifactRequest(kind="audio_overview", options=options)
        result: OneShotResult = await engine.one_shot(sources, request, on_progress=self.on_progress)
        await self.finish_audio(
            result.artifact,
            result.audio,
            {"notebook": result.notebook, "sources": result.sources},
        )
        return 0

    async def doc(self, args: list[str]) -> int:
        _require(args, "source (a file path, URL, or -)")
        kind = self.document_kind()
        sources = self.sources_from(args)
        engine = self.engine()
        request = ArtifactRequest(kind=kind, instructions=self._opt("prompt") or None)
        result = await engine.one_shot(sources, request, on_progress=self.on_progress)
        await self.finish_document(result.artifact, {"notebook": result.notebook, "sources": result.sources})
        return 0

    async def ask(self, args: list[str]) -> int:
        _require(args, "source (a file path, URL, or -)")
        question = self._opt("question")
        if not question:
            raise _usage('ask needs a question: -q "..."')
        inputs = self.sources_from(args)
        engine = self.engine()
        notebook = await engine.notebooks.create(title=_title_of_source(inputs[0]))
        for item in inputs:
            await engine.sources.add(notebook.id, item)
        if not self.json:
            self.stderr(f"Notebook: {notebook.id}\n")
        await self.stream_answer(engine, notebook.id, question)
        return 0

    async def notebooks(self, args: list[str]) -> int:
        sub = args[0] if args else None
        rest = args[1:]
        engine = self.engine()
        if sub == "list":
            notebooks = await engine.notebooks.list()
            self.emit(
                notebooks,
                lambda: _print_rows(
                    self.print,
                    [[nb.id, str(nb.source_count or 0), nb.title] for nb in notebooks],
                    "No notebooks yet.",
                ),
            )
            return 0
        if sub == "create":
            _require(rest, "notebook title")
            notebook = await engine.notebooks.create(title=" ".join(rest))
            self.emit(notebook, lambda: self.print(notebook.id))
            return 0
        if sub == "delete":
            _require(rest, "notebook id")
            await engine.notebooks.delete(rest[0])
            self.emit({"deleted": rest[0]}, lambda: self.print(f"Deleted notebook {rest[0]}."))
            return 0
        raise _usage("Usage: overtone notebooks list | create <title> | delete <id>")

    async def sources(self, args: list[str]) -> int:
        sub = args[0] if args else None
        rest = args[1:]
        engine = self.engine()
        if sub == "list":
            _require(rest, "notebook id")
            items = await engine.sources.list(rest[0])
            self.emit(
                items,
                lambda: _print_rows(self.print, [[s.id, s.kind, s.title] for s in items], "No sources yet."),
            )
            return 0
        if sub == "add":
            _require(rest, "notebook id")
            notebook_id, source_args = rest[0], rest[1:]
            _require(source_args, "source (a file path, URL, or -)")
            inputs = self.sources_from(source_args)
            added = []
            for item in inputs:
                added.append(await engine.sources.add(notebook_id, item))
            self.emit(added, lambda: _print_rows(self.print, [[s.id, s.kind, s.title] for s in added], ""))
            return 0
        if sub == "delete":
            _require(rest, "source id")
            await engine.sources.delete(rest[0])
            self.emit({"deleted": rest[0]}, lambda: self.print(f"Deleted source {rest[0]}."))
            return 0
        raise _usage("Usage: overtone sources list <notebookId> | add <notebookId> <source...> | delete <sourceId>")

    async def chat(self, args: list[str]) -> int:
        _require(args, "notebook id")
        notebook_id, words = args[0], args[1:]
        if not words:
            raise _usage("chat needs a message after the notebook id.")
        engine = self.engine()
        await self.stream_answer(engine, notebook_id, " ".join(words))
        return 0

    async def audio(self, args: list[str]) -> int:
        _require(args, "notebook id")
        notebook_id = args[0]
        engine = self.engine()
        options = self.audio_options()
        request = ArtifactRequest(kind="audio_overview", options=options)
        artifact = await engine.artifacts.generate(notebook_id, request, on_progress=self.on_progress)
        audio = await engine.artifacts.get_audio(artifact.id)
        await self.finish_audio(artifact, audio, {"notebookId": notebook_id})
        return 0

    async def generate(self, args: list[str]) -> int:
        _require(args, "notebook id")
        notebook_id = args[0]
        kind = self.document_kind()
        engine = self.engine()
        request = ArtifactRequest(kind=kind, instructions=self._opt("prompt") or None)
        artifact = await engine.artifacts.generate(notebook_id, request, on_progress=self.on_progress)
        await self.finish_document(artifact, {"notebookId": notebook_id})
        return 0

    async def artifacts(self, args: list[str]) -> int:
        sub = args[0] if args else None
        if sub != "list":
            raise _usage("Usage: overtone artifacts list <notebookId>")
        _require(args[1:], "notebook id")
        engine = self.engine()
        items = await engine.artifacts.list(args[1])
        self.emit(
            items,
            lambda: _print_rows(
                self.print,
                [[a.id, a.kind, a.status, a.title] for a in items],
                "No artifacts yet.",
            ),
        )
        return 0

    async def evaluate(self) -> int:
        """Run the evaluation suite; exit code 1 when any check fails."""
        from .agents import create_runtime
        from .evals import AREAS, list_cases, run_evals
        from .mock import MockChatClient

        only = set(_split_list(self._opt("only"))) or None
        if only:
            known = set(AREAS) | {case_id for case_id, _, _ in list_cases()}
            unknown = sorted(only - known)
            if unknown:
                raise _usage(f'Unknown --only value "{unknown[0]}". Areas: {", ".join(AREAS)}.')

        if self._injected is not None:
            runtime = self._injected.runtime
        elif self._opt("mock", False):
            runtime = create_runtime(chat_client=MockChatClient(delay=0))
        else:
            runtime = create_runtime(model=self._opt("model") or None)
            try:
                runtime.client()  # fail now, once, rather than in every case
            except OvertoneError as exc:
                raise OvertoneError(
                    f"{exc} To run the suite offline against the stand-in model, use: overtone eval --mock",
                    status=exc.status,
                    code=exc.code,
                ) from None

        mock = runtime.provider == "mock"
        self.stderr(f"Evaluating {runtime.model}{' (mock model, quality checks skipped)' if mock else ''}\n")

        def on_case(case: Any) -> None:
            self.stderr(f"  {case.status.upper():<4}  {case.id}  ({case.seconds:.1f}s)\n")

        report = await run_evals(runtime, only=only, on_case=on_case)
        if not report.cases:
            raise _usage("No evaluation cases matched --only.")
        report_path: str | None = None
        if self._opt("report"):
            report_path = os.path.abspath(self._opt("report"))
            _write_text(report_path, report.to_markdown())

        def prose() -> None:
            self.print(
                f"{'PASS' if report.passed else 'FAIL'}: {report.count('pass')} passed, {report.count('fail')} failed, "
                f"{report.count('skip')} skipped ({len(report.cases)} cases, {report.seconds:.1f}s)"
            )
            for case in report.cases:
                for check in case.checks:
                    if check.status == "fail":
                        self.print(f"  FAIL {case.id}: {check.name}{' - ' + check.detail if check.detail else ''}")
            if report_path:
                self.print(f"Report: {report_path}")

        self.emit({**report.to_json(), "reportPath": report_path}, prose)
        return 0 if report.passed else 1


# ───────────────────────────── Entry point ─────────────────────────────


def _usage_failure(stderr: Callable[[str], None], message: str) -> int:
    match = re.search(r"invalid choice: '([^']*)'", message)
    if match:
        stderr(f"Unknown command: {match.group(1)}\n\n{USAGE}")
    else:
        stderr(f"Error: {message}\n\n{USAGE}")
    return 2


def main(
    argv: list[str] | None = None,
    *,
    stdout: Callable[[str], None] | None = None,
    stderr: Callable[[str], None] | None = None,
    engine: Engine | None = None,
    stdin: Callable[[], str] | None = None,
) -> int:
    """Run the CLI and return the exit code (0 ok, 1 error, 2 bad usage).

    `engine` replaces the engine the commands would build (used by tests and embedding).
    """
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if stdout is None:
        _use_utf8(sys.stdout)
        stdout = _write_stdout
    if stderr is None:
        _use_utf8(sys.stderr)
        stderr = _write_stderr
    if engine is None:
        load_env_file(".env")

    try:
        ns, extras = _build_parser().parse_known_args(raw_args)
        unexpected = [t for t in extras if t.startswith("-") and t != "-"]
        if unexpected:
            raise _UsageError(f"unrecognized arguments: {' '.join(unexpected)}")
    except _UsageError as exc:
        return _usage_failure(stderr, str(exc))

    if getattr(ns, "version", False):
        stdout(f"{__version__}\n")
        return 0
    command = getattr(ns, "command", None)
    if getattr(ns, "help", False) or command is None:
        stdout(USAGE)
        return 0
    # Positionals that appeared after an option were parsed as extras; keep them in order.
    args = [*getattr(ns, "args", []), *extras]

    runner = _Runner(ns, stdout=stdout, stderr=stderr, read_stdin=stdin or _read_stdin, injected=engine)

    async def _guarded() -> int:
        try:
            return await runner.dispatch(command, args)
        except Exception as exc:  # noqa: BLE001 - every failure is reported as `Error: ...`
            stderr(f"Error: {exc}\n")
            if os.environ.get("OVERTONE_DEBUG"):
                stderr(traceback.format_exc())
            return 1

    try:
        return asyncio.run(_guarded())
    except KeyboardInterrupt:
        stderr("Interrupted.\n")
        return 130
