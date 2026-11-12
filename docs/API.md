# Overtone REST API

The REST API is a FastAPI app built by `overtone.api.create_app`. It wraps the Python engine, so every route calls `engine.*` directly. The contract (routes, status codes, JSON shapes, SSE framing) is the same one the TypeScript handler serves, so the existing web UI and TypeScript client work against this server unchanged. Differences are listed at the end.

Interactive docs are served at `/docs`, and the OpenAPI schema at `/openapi.json`. Request bodies are parsed by hand, so the schema does not describe them. This document is the reference for request and response shapes.

## Running it

From the command line (blocking, binds `127.0.0.1` by default):

```sh
overtone serve --port 8787
```

Or with uvicorn, using the app factory (the engine is created on the first request, from the environment):

```sh
uvicorn overtone.api:create_app --factory --port 8787
```

Or from Python, with options:

```python
from overtone.api import create_app, serve

serve(port=8787, api_key="s3cret", cors=["https://app.example"])   # blocking

app = create_app(api_key="s3cret", cors=["https://app.example"])   # any ASGI server
```

Create it with an engine you built yourself to control storage, the chat model, TTS and live voice:

```python
from overtone import create_engine, FileStorage
from overtone.api import create_app

engine = create_engine(storage=FileStorage("./data"))
app = create_app(engine, api_key="s3cret", max_upload_bytes=100 * 1024 * 1024)
```

`create_app` takes these options:

| Option | Default | Meaning |
| --- | --- | --- |
| `engine` | `create_engine()` on first request | The engine to serve. It is available as `app.state.engine` |
| `api_key` | none | When set, requests need `Authorization: Bearer <api_key>` (see Auth) |
| `cors` | `False` | `True` allows any origin. A string or list allows only those origins (see CORS) |
| `max_upload_bytes` | 50 MB | Largest accepted upload per file |

`serve()` takes `host` (default `127.0.0.1`), `port` (default `8787`) and the same options as `create_app`.

## Mounting in an existing app

Routes are at the root of the app, so it can be mounted under any path of a parent ASGI app:

```python
from fastapi import FastAPI
from overtone.api import create_app

parent = FastAPI()
parent.mount("/overtone", create_app(api_key="s3cret"))
# Routes are now /overtone/health, /overtone/notebooks, and so on.
```

Mounting is covered by the test suite for FastAPI parents; other Starlette-based parents use the same `Mount` mechanism.

## Auth and CORS

- **Auth.** With `api_key` set, every request must send `Authorization: Bearer <api_key>`, except `OPTIONS` (preflight) and `GET /health`. Audio is the one exception: `GET /artifacts/{id}/audio` also accepts `?key=<api_key>`, because an `<audio src>` element cannot send headers. A missing or wrong key returns `401` with `WWW-Authenticate: Bearer`. Keys are compared in constant time.
- **CORS.** Off by default. `cors=True` sends `Access-Control-Allow-Origin: *`. A string or list echoes the request `Origin` only when it is listed, and sends `Vary: Origin`. Preflight `OPTIONS` requests return `204` with `Access-Control-Allow-Methods: GET,POST,PATCH,DELETE,OPTIONS`, `Access-Control-Allow-Headers: Authorization, Content-Type` and `Access-Control-Max-Age: 86400`. Every response, including errors and SSE streams, carries `Access-Control-Expose-Headers: Content-Range, Content-Length`.
- **Trailing slashes** are accepted on every route (`/notebooks/{id}/` works).

## Errors

Every error is JSON:

```json
{ "error": { "message": "Notebook 3f2... not found.", "code": "not_found" } }
```

| Status | Code | Meaning |
| --- | --- | --- |
| 400 | `bad_json` | The body is not valid JSON |
| 400 | `bad_request` | The body has the wrong shape. The message names the first bad field, for example `options.format: Input should be ...` |
| 400 | `empty_message`, `no_sources`, `multipart_required`, `unsupported_source`, `bad_kind`, `missing_instructions`, `bad_sdp`, `missing_artifact`, `no_script`, `empty_transcript`, `bad_multipart` | The request is well formed but cannot be served as sent |
| 401 | `unauthorized` | Missing or wrong API key |
| 404 | `not_found` | Unknown notebook, source, artifact or route |
| 404 | `no_audio` | The artifact has no rendered audio |
| 405 | `method_not_allowed` | Known path, wrong method. The `Allow` header lists the valid methods |
| 413 | `too_large` | A file exceeds `max_upload_bytes`. Reported per file inside `errors` |
| 416 | (none) | Unsatisfiable `Range`. The response has `Content-Range: bytes */<total>` |
| 422 | `empty_source` | No readable text could be extracted |
| 500 | `internal` | Anything else. The exception is logged on the server (`overtone.api` logger) and never sent |
| 501 | `live_unavailable` | Live voice is not configured (no OpenAI key, or `OVERTONE_LIVE=0`) |
| 502 | `chat_failed`, `generation_failed` | The model call failed |
| 502 | `live_unreachable`, `live_auth`, `live_failed` | The live voice service could not be reached, rejected the key, or refused the session |

## Routes

| Method | Path | Success | Body |
| --- | --- | --- | --- |
| GET | `/health` | 200 | Capabilities (no auth). See below |
| GET | `/notebooks` | 200 | `{ notebooks }` |
| POST | `/notebooks` | 201 | `{ notebook }`. Body `{ title?, description?, icon? }`, may be empty |
| GET | `/notebooks/{id}` | 200 | `{ notebook }` |
| PATCH | `/notebooks/{id}` | 200 | `{ notebook }`. Body `{ title?, description?, icon? }`. `null` clears a field |
| DELETE | `/notebooks/{id}` | 204 | |
| GET | `/notebooks/{id}/guide` | 200 | `{ guide }`. `?refresh=1` regenerates it |
| GET | `/notebooks/{id}/sources` | 200 | `{ sources }` |
| POST | `/notebooks/{id}/sources` | 201 | `{ sources, errors }`. JSON or multipart (see Sources) |
| GET | `/sources/{id}` | 200 | `{ source }` |
| GET | `/sources/{id}/text` | 200 | `text/plain; charset=utf-8` |
| DELETE | `/sources/{id}` | 204 | |
| GET | `/notebooks/{id}/messages` | 200 | `{ messages }` |
| DELETE | `/notebooks/{id}/messages` | 204 | |
| POST | `/notebooks/{id}/chat` | 200 | SSE stream, or `{ message }` when `stream: false` |
| GET | `/notebooks/{id}/artifacts` | 200 | `{ artifacts }` |
| POST | `/notebooks/{id}/artifacts` | 202 | `{ artifact }`, status `queued`. Body is an artifact request |
| GET | `/artifacts/{id}` | 200 | `{ artifact }` |
| DELETE | `/artifacts/{id}` | 204 | |
| GET | `/artifacts/{id}/audio` | 200 / 206 | Audio bytes (see Artifacts and audio) |
| POST | `/oneshot` | 200 | `{ notebook, sources, artifact }`. Audio bytes are omitted |
| POST | `/notebooks/{id}/live/sessions` | 201 | `{ session }`. Starts a live voice session (see Live voice) |
| POST | `/notebooks/{id}/live/delegate` | 200 | `{ answer, citations }`. Answers a question from the sources (see Live voice) |

`GET /health` returns the engine's capabilities: `name` (`"overtone"`), `version`, `model`, `framework`, `tts` (`null` when no TTS provider is configured) and `live` (`null` when live voice is not configured; otherwise `{ model, voices, defaultVoice }`).

Unknown paths return `404 not_found`. Known paths with the wrong method return `405` with an `Allow` header.

## Sources

`POST /notebooks/{id}/sources` accepts two formats.

**JSON**: one source, or `{ "sources": [...] }`. Only `text` and `url` are allowed here.

```sh
curl -X POST "$BASE/notebooks/$NB/sources" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{ "type": "text", "text": "Overtone grounds answers in sources.", "title": "Notes" }'
```

```json
{ "sources": [ { "type": "url", "url": "https://example.com/report" }, { "type": "text", "text": "..." } ] }
```

A `type: "file"` in JSON returns `400 multipart_required`.

**Multipart** (`multipart/form-data`): any number of `file` (or `files`) parts, plus optional repeated `url` and `text` fields. A `title` field pairs with `text` fields by position.

```sh
curl -X POST "$BASE/notebooks/$NB/sources" \
  -H "Authorization: Bearer $KEY" \
  -F file=@paper.pdf \
  -F url=https://example.com/report \
  -F text="Meeting notes..." -F title="Standup"
```

Each input is processed in turn. The results:

- At least one succeeded: `201` with `{ "sources": [...], "errors": [...] }`. Failures appear in `errors` as `{ name, error, code }`.
- All failed: the status of the first failure (`413` for an oversized file, `422` for empty text). The body is `{ "error": {...}, "sources": [], "errors": [...] }`.

## Chat

`POST /notebooks/{id}/chat` with `{ "message": "...", "sourceIds": ["..."]?, "stream": false? }`. A missing notebook is a plain `404` JSON error, not a stream.

By default the response is `text/event-stream` with `Cache-Control: no-cache, no-transform` and `X-Accel-Buffering: no`:

```sh
curl -N -X POST "$BASE/notebooks/$NB/chat" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{ "message": "What works?" }'
```

Each event is one `data:` line holding a JSON object, followed by a blank line:

```
data: {"type":"text","delta":"Based on the sources "}

data: {"type":"text","delta":"[1] it works."}

data: {"type":"citation","citation":{"index":1,"sourceId":"9b1c...","sourceTitle":"Notes","quote":"it works"}}

data: {"type":"done","message":{"id":"4d2e...","notebookId":"...","role":"assistant","content":"Based on the sources [1] it works.","citations":[{"index":1,"sourceId":"9b1c...","sourceTitle":"Notes","quote":"it works"}],"createdAt":"2026-11-12T10:00:00.000Z"}}

```

The stream ends after `done`, or after `error` (`{"type":"error","error":"...","code":"...","status":400}`; `code` and `status` are present for known failures such as `no_sources`). Failures after the stream has started arrive as an `error` event, not as an HTTP error. Disconnecting the client closes the engine's generator, which stops generation. With `"stream": false` you get `200 { "message": ChatMessage }`, or the failure's own status and code (for example `400 no_sources`, `401 llm_auth`, `502 llm_error`).

The history is kept per notebook: `GET /notebooks/{id}/messages` returns `{ messages }`, and `DELETE /notebooks/{id}/messages` clears it.

## Artifacts and audio

Create an audio overview, then poll until it is `ready` or `error`:

```sh
curl -X POST "$BASE/notebooks/$NB/artifacts" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"kind":"audio_overview","options":{"format":"deep_dive","length":"short"}}'
# 202 { "artifact": { "id": "a1...", "status": "queued", ... } }

curl "$BASE/artifacts/a1..." -H "Authorization: Bearer $KEY"
# 200 { "artifact": { "status": "generating", "progress": "Recording audio (4/22)", ... } }
```

Document kinds (`summary`, `briefing`, `study_guide`, `faq`, `timeline`, `custom`) take `{ "kind": "summary", "instructions"?: "..." }` and return Markdown in `content`. `custom` needs `instructions`. A ready audio overview has `script` and `audio`. `audio` is `null` when no TTS provider is configured, and clients can play the script instead.

Download the audio. The endpoint supports one `Range: bytes=` header, so `<audio>` can seek:

```sh
curl -o overview.mp3 "$BASE/artifacts/a1.../audio?download=1" -H "Authorization: Bearer $KEY"
```

- `200` with the full body, or `206` with `Content-Range` for a range. Suffix ranges (`bytes=-500`) work.
- `416` if the range is out of bounds.
- `404 no_audio` if the artifact has no audio.
- `?download=1` adds `Content-Disposition: attachment; filename="<slug of title>.mp3"`.
- `Cache-Control: private, max-age=3600`, `Accept-Ranges: bytes`.

## Live voice

Live voice connects the browser to OpenAI's GPT-Live over WebRTC. The API key stays on the server. The browser sends its SDP offer, and the server returns the SDP answer. Overtone is the brain: when the voice model delegates a question, the browser relays it to the notebook and speaks the answer.

Live routes return `501 live_unavailable` unless the server has an OpenAI key (`OPENAI_API_KEY`, or `OVERTONE_LIVE_API_KEY`) and `OVERTONE_LIVE` is not `0`.

The flow:

1. The browser creates an `RTCPeerConnection`, adds a microphone track and a data channel, and creates an SDP offer.
2. The browser posts the offer to the server:

   ```sh
   curl -X POST "$BASE/notebooks/$NB/live/sessions" \
     -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{ "sdp": "v=0\r\n...", "mode": "conversation", "voice": "marin" }'
   # 201 { "session": { "sessionId": "live_123", "sdp": "v=0\r\n...", "mode": "conversation", "model": "gpt-live-1", "voice": "marin" } }
   ```

   Body fields: `sdp` (required), `mode` (`conversation` by default, or `podcast`), `sourceIds` (conversation: limit the sources), `artifactId` and `turnIndex` (podcast: the audio overview being played and where the listener paused), and `voice`. Conversation mode needs at least one source. Podcast mode needs an artifact that has a script.
3. The browser applies `session.sdp` with `setRemoteDescription`. The voice model then talks with the listener.
4. When the voice model delegates a question, the data channel receives a `session.delegation.created` event. The browser sends the recent transcript to the server and gets the answer:

   ```sh
   curl -X POST "$BASE/notebooks/$NB/live/delegate" \
     -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{ "mode": "conversation", "transcript": [ { "role": "user", "text": "What does the report conclude?" } ], "sourceIds": ["..."] }'
   # 200 { "answer": "The report concludes ...", "citations": [ { "index": 1, "sourceId": "...", "sourceTitle": "...", "quote": "..." } ] }
   ```

   The transcript is oldest first, and its last user line is the question. Only the last 16 lines are used. Pass `artifactId` instead of `sourceIds` to answer from that artifact's sources. The answer is short and speakable.
5. The browser sends the answer back into the live session on the data channel as a `session.commentary.append` event, so the voice model can say it.

Delegation works without a live provider configured, so you can use `/live/delegate` with any voice front end.

## Typed client

`packages/client` (`@overtone/client`) wraps these routes for TypeScript in the browser and Node 20+, including `LiveVoiceSession`, which runs the live voice flow above over WebRTC:

```ts
import { createClient, LiveVoiceSession } from "@overtone/client";

const overtone = createClient({ baseUrl: "http://127.0.0.1:8787", apiKey: "..." });
const session = new LiveVoiceSession({ client: overtone, notebookId, onState: render });
await session.start();   // call from a click handler: it asks for the microphone
```

From Python, call the engine in-process instead (`from overtone import create_engine`).
