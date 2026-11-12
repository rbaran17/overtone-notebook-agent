# Overtone

Turn a pile of sources into grounded answers, audio overviews and podcasts, and talk to them out loud.

Overtone is two things that work together or apart:

- **A headless notebook agent** (`backend/`, Python) built on [Microsoft Agent Framework](https://learn.microsoft.com/agent-framework/overview/). Use it as a Python library, a REST API, or a CLI, and plug it into your own application.
- **A web app** (`src/`, Next.js) that is just one client of that API.

```
┌──────────────────────┐   REST + SSE    ┌──────────────────────────────────────────────┐
│ Web UI (Next.js)     │ ──────────────▶ │ overtone (Python)                            │
│ Your app / scripts   │                 │                                              │
│ @overtone/client     │                 │  FastAPI ─▶ Engine                           │
└──────────┬───────────┘                 │              ├─ Agent Framework agents       │
           │ WebRTC (voice)              │              │    researcher · producer ·    │
           ▼                             │              │    voice-researcher           │
┌──────────────────────┐   delegation    │              ├─ Agent Framework workflow     │
│ GPT-Live (gpt-live-1)│ ◀─────────────▶ │              │    scriptwriter ─▶ sound eng. │
└──────────────────────┘   via browser   │              ├─ ingestion (PDF, DOCX, web)   │
                                         │              ├─ TTS (OpenAI / ElevenLabs)    │
                                         │              └─ storage (files, pluggable)   │
                                         └──────────────────────────────────────────────┘
```

## What it does

- **Sources**: PDFs, Word documents, web pages, Markdown/plain text, pasted text.
- **Grounded chat**: answers come from your sources, with numbered citations that open the exact passage.
- **Audio overviews**: a two-host deep dive, brief, debate or critique, or a single-narrator overview, in three lengths, steerable with your own prompt, host names, voices and language. Rendered to MP3 when a text-to-speech key is configured; otherwise the web UI reads the script aloud with the browser's voices.
- **Documents**: summary, briefing, study guide, FAQ, timeline, or anything you describe.
- **Live voice**: talk to your notebook, or pause an episode and join the conversation. GPT-Live is the voice; the notebook agents answer from your sources.

## Quick start

Requirements: Python 3.11+, Node 20+.

```bash
# 1. Backend
cd backend
python -m venv .venv
.venv/Scripts/activate            # Windows;  source .venv/bin/activate elsewhere
pip install -e ".[dev]"
cp ../.env.example .env           # add ANTHROPIC_API_KEY (and optionally OPENAI_API_KEY)
overtone serve                    # REST API on http://127.0.0.1:8787

# 2. Web UI (second terminal, repo root)
npm install
npm run dev                       # http://localhost:3000
```

No keys yet? `OVERTONE_MOCK=1 overtone serve` runs the whole pipeline with a deterministic stand-in model so you can click through everything.

| Key | Unlocks |
|---|---|
| `ANTHROPIC_API_KEY` | Chat, scripts and documents (Claude, via Agent Framework's Anthropic client) |
| `OPENAI_API_KEY` | Rendered podcast audio **and** live voice (`gpt-live-1`) |
| `ELEVENLABS_API_KEY` | Rendered podcast audio (alternative) |

All settings are listed in [.env.example](.env.example).

## Use it headless

### CLI

```bash
overtone podcast report.pdf https://example.com/post --format debate --length short --out episode.mp3
overtone doc notes.md --kind study_guide --out guide.md
overtone ask paper.pdf -q "What were the main findings?"
overtone serve --port 8787 --api-key "$OVERTONE_API_KEY" --cors https://app.example.com
overtone --help
```

Results go to stdout and progress to stderr, so commands pipe cleanly; `--json` gives machine-readable output and `--ephemeral` keeps nothing on disk.

### Python

```python
import asyncio
from overtone import create_engine, ArtifactRequest, AudioOptions, FileInput, UrlInput

async def main():
    engine = create_engine()

    # One call: sources in, finished episode out.
    result = await engine.one_shot(
        [UrlInput("https://example.com/report"), FileInput("notes.md", open("notes.md", "rb").read())],
        ArtifactRequest(kind="audio_overview", options=AudioOptions(format="deep_dive", length="medium")),
    )
    print(result.artifact.title)
    if result.audio:
        open("episode.mp3", "wb").write(result.audio[0])

    # Or drive a notebook yourself.
    notebook = await engine.notebooks.create(title="Q3 research")
    await engine.sources.add(notebook.id, UrlInput("https://example.com/q3"))
    async for event in engine.chat.stream(notebook.id, "What changed since Q2?"):
        if event["type"] == "text":
            print(event["delta"], end="")

asyncio.run(main())
```

### REST

`overtone serve` exposes everything over HTTP; chat streams as Server-Sent Events. See [docs/API.md](docs/API.md). To mount it inside an existing FastAPI/ASGI app:

```python
from overtone.api import create_app
app.mount("/overtone", create_app(api_key="..."))
```

### TypeScript

`packages/client` is a small typed client for the REST API (browser and Node 20+), including `LiveVoiceSession`, which runs the whole live voice flow over WebRTC.

```ts
import { createClient } from "@overtone/client";
const overtone = createClient({ baseUrl: "http://127.0.0.1:8787" });
const notebook = await overtone.notebooks.create({ title: "Research" });
```

## Built on Microsoft Agent Framework

The engine is ordinary Agent Framework code, so it composes with the rest of that ecosystem.

- **Agents** (`overtone/agents.py`): `researcher`, `producer` and `voice_researcher` are `agent_framework.Agent` instances. Import them into your own workflows.
- **Workflow** (`overtone/workflows.py`): an audio overview is a graph, `scriptwriter ─▶ sound_engineer`. Each step is an `Executor`; insert your own (a fact-check, a human review) with `WorkflowBuilder`.
- **Any model**: Claude is the default, but `create_engine(chat_client=...)` accepts any Agent Framework chat client (Azure OpenAI, OpenAI, Bedrock, Ollama, …). Citations are provider-agnostic: sources are split into numbered passages and the model cites passage numbers, which the engine maps back to exact quotes.
- **Pluggable storage and speech**: implement the `Storage` or `TtsProvider` protocols in `overtone/models.py` to use your own database, object store or voice engine.

```python
from agent_framework import WorkflowBuilder
from overtone import create_runtime
from overtone.workflows import Scriptwriter, SoundEngineer, JobHooks

runtime, hooks = create_runtime(), JobHooks()
writer, engineer = Scriptwriter(runtime, hooks), SoundEngineer(tts=None, hooks=hooks)
workflow = WorkflowBuilder(start_executor=writer).add_edge(writer, engineer).build()
```

## Live voice

Live voice uses OpenAI's full-duplex model `gpt-live-1` as the voice and Overtone as the brain:

1. The browser creates a WebRTC offer and posts it to `POST /notebooks/{id}/live/sessions`. The server opens the GPT-Live session (the OpenAI key never reaches the browser) and returns the answer.
2. Audio flows directly between the browser and GPT-Live.
3. When the user asks about their sources, GPT-Live delegates. The browser forwards the recent transcript to `POST /notebooks/{id}/live/delegate`, the `voice_researcher` agent answers from the sources, and the browser appends the answer to the session for the voice model to say.

Two modes: **conversation** (talk to the notebook) and **podcast** (pause an episode and ask the host a question; the host knows what has been said so far, then you resume).

## Project layout

```
backend/overtone/     Python package: engine, agents, workflows, ingestion, tts, live, api, cli
backend/tests/        pytest suite (runs offline against the mock model)
packages/client/      @overtone/client: typed REST client + LiveVoiceSession
src/                  Next.js web UI
docs/API.md           REST reference
```

## Tests

```bash
cd backend && python -m pytest          # engine, agents, workflows, API, CLI: no network needed
npm run test:client                     # TypeScript client
npm run typecheck && npm run lint       # web UI
```

## Status and limits

- Everything above is exercised offline against a mock model, a fake TTS provider and a fake WebRTC stack. The calls to Claude, OpenAI text-to-speech, ElevenLabs and GPT-Live are written from each provider's documentation and have **not** been run against the live services in this repository's test suite. Expect to shake out small issues the first time you add real keys.
- A GPT-Live session has a single voice, so the join-in podcast mode brings one host into the live conversation rather than performing the whole two-host episode live.
- Scanned (image-only) PDFs are not OCR'd.
- Storage is local files by default and there is no user/auth model beyond a single API key; put it behind your own auth for multi-user use.
