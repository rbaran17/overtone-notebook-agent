# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- **Slow calls through the web UI.** The `/api` proxy gave up after 30 seconds
  without a byte from the engine and answered `500 Internal Server Error`. That
  cut off notebook guides, non-streaming chat, live-voice answers and streamed
  chat whenever the model paused for longer. It now waits 10 minutes, the read
  timeout of the engine's Claude client. `OVERTONE_API_TIMEOUT` changes it.
- **Uploads over 10 MB through the web UI.** The proxy forwarded only the first
  10 MB of a request, so a larger upload hung and then failed with a 500. It now
  forwards up to 51 MB, enough for one file at the engine's 50 MB limit.
  `addFiles` in `@overtone/client` sends one request per file instead of one for
  the whole batch, and the web UI turns away a file over 50 MB with a message
  that says so, before anything is sent.

## [0.2.0] - 2026-11-12

### Added

- Python engine on Microsoft Agent Framework: researcher, producer and
  voice-researcher agents, and a scriptwriter to sound-engineer workflow.
- Source ingestion for PDF, DOCX, web pages, and Markdown and plain text.
- Grounded chat with passage-level citations, streamed over SSE.
- Audio overviews in deep dive, brief, debate, critique and solo formats, in
  three lengths, using OpenAI or ElevenLabs text-to-speech with a browser-voice
  fallback.
- Documents in summary, briefing, study guide, FAQ, timeline and custom formats.
- Live voice with GPT-Live, in conversation and join-in podcast modes.
- REST API, `overtone` CLI and the `@overtone/client` TypeScript client.
- Next.js web UI.
- Offline mock model, test suites, and the `overtone eval` evaluation suite.
