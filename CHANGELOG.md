# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Security

- **Keyless API and browsers.** Without an API key, any web page you visited could
  send requests to `http://127.0.0.1:8787` (a `text/plain` POST needs no CORS
  preflight), and a DNS-rebinding page could read, change and delete every
  notebook. A keyless API now answers only when `Host` is `localhost`, an IP
  address or a name in `allowed_hosts` / `OVERTONE_ALLOWED_HOSTS`, and refuses
  requests that change something when `Origin` is another website.
- **Web UI.** `npm run dev` and `npm start` listened on every network interface
  and forwarded `/api` to the engine, so anyone on the same network had the whole
  API. They now listen on 127.0.0.1, and `/api` goes through a proxy that checks
  `Host` and `Origin` and attaches `OVERTONE_API_KEY` on the server.
- **`OVERTONE_API_KEY` outside the CLI.** `create_app()` ignored the variable, so
  `uvicorn overtone.api:create_app --factory` served an open API with the key
  exported. It is now the default for `api_key`.
- **URL sources.** The private-address check and the connection each resolved the
  host name, so a name that answered differently the second time reached
  `127.0.0.1` or a cloud metadata address. The connection now goes to the address
  that was checked. `100.64.0.0/10`, site-local and multicast addresses are
  blocked as well.
- **Crafted pages and documents.** A 200 KB HTML page could freeze the server for
  most of a minute; conversion is now linear and runs off the event loop. A
  `.docx` that unpacks far larger than it is, an oversized request body and a
  multipart body with thousands of parts are refused.
- **Model-written Markdown** no longer loads images: a source could make the
  model emit an image URL carrying notebook content to another server. The UI
  also sends a Content-Security-Policy and related headers.
- Internal error text (file paths and the like) is no longer sent to clients for
  chat and artifact failures.
- Dependency floors raised past known advisories (`fastapi`, `python-multipart`,
  `pypdf`).

### Changed

- `overtone serve --cors` with no origin list needs an API key; name the origins
  instead (`--cors http://localhost:3000`) or add `--api-key`.
- `overtone serve --allow-host name,...` and the `allowed_hosts` option of
  `create_app()` name extra hosts a keyless API answers to (`"*"` for any).
- A keyless API behind a reverse proxy or under a DNS name now needs that name in
  `OVERTONE_ALLOWED_HOSTS`, or an API key.
- `overtone podcast` without `--out` no longer overwrites an existing file; it
  writes `name-2.mp3` (or `.txt`) instead.

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
