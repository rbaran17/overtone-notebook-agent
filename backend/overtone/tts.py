"""Text-to-speech: provider resolution, HTTP providers with retry, dialogue rendering and MP3 joining.

Port of packages/core/src/tts/{index,openai,elevenlabs,mp3}.ts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

import httpx

from .errors import OvertoneError
from .models import AudioScript, TtsConfig, TtsProvider, TtsVoice

logger = logging.getLogger(__name__)

DEFAULT_MAX_CHARS = 4000
DEFAULT_CONCURRENCY = 4
HTTP_TIMEOUT_S = 120.0

# Retry policy shared by every HTTP provider.
MAX_RETRIES = 2
BACKOFF_S: tuple[float, float] = (0.8, 2.0)
MAX_RETRY_AFTER_S = 10.0
EXCERPT_CHARS = 200

# Injectable delay so tests can run retry paths instantly.
_sleep: Callable[[float], Awaitable[object]] = asyncio.sleep

# ───────────────────────────── OpenAI ─────────────────────────────

OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini-tts"
OPENAI_DEFAULT_VOICES: tuple[str, ...] = ("coral", "ash")
OPENAI_MAX_CHARS = 4096
OPENAI_VOICE_IDS: tuple[str, ...] = (
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "fable",
    "nova",
    "onyx",
    "sage",
    "shimmer",
    "verse",
)

# ─────────────────────────── ElevenLabs ───────────────────────────

ELEVENLABS_BASE_URL = "https://api.elevenlabs.io"
ELEVENLABS_DEFAULT_MODEL = "eleven_multilingual_v2"
ELEVENLABS_MAX_CHARS = 4500
ELEVENLABS_BUILT_IN_VOICES: tuple[TtsVoice, ...] = (
    TtsVoice(id="21m00Tcm4TlvDq8ikWAM", label="Rachel"),
    TtsVoice(id="pNInz6obpgDQGcFmaJgB", label="Adam"),
    TtsVoice(id="EXAVITQu4vr4xnSDxMaL", label="Bella"),
    TtsVoice(id="ErXwobaYiN019PkySvjV", label="Antoni"),
    TtsVoice(id="TxGEqnHWrfWFTfGW9XjX", label="Josh"),
    TtsVoice(id="AZnzlk1XvdvUeBnXmlld", label="Domi"),
)
ELEVENLABS_DEFAULT_VOICES: tuple[str, ...] = ("21m00Tcm4TlvDq8ikWAM", "pNInz6obpgDQGcFmaJgB")


def _capitalize(s: str) -> str:
    return s[:1].upper() + s[1:]


def _voice_list(builtin: Iterable[TtsVoice], extra: Iterable[str]) -> list[TtsVoice]:
    """Built-in voices first, then any extra configured ids not already listed."""
    voices = list(builtin)
    known = {v.id for v in voices}
    voices.extend(TtsVoice(id=v, label=v) for v in extra if v not in known)
    return voices


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "[redacted]") if secret else text


def _retry_after_s(header: str | None) -> float | None:
    """Parse a Retry-After header (seconds), capped at MAX_RETRY_AFTER_S. None when unusable."""
    if not header:
        return None
    try:
        seconds = float(header)
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_S)


def _http_client(transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, transport=transport)


async def _post_with_retry(
    client: httpx.AsyncClient,
    *,
    label: str,
    api_key: str,
    url: str,
    headers: dict[str, str],
    body: object,
) -> bytes:
    """POST a JSON body and return the response bytes. Retries 429, 5xx and network errors."""
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    attempt = 0
    while True:
        try:
            res = await client.post(url, headers=headers, content=payload)
        except httpx.RequestError as err:
            if attempt < MAX_RETRIES:
                await _sleep(BACKOFF_S[attempt])
                attempt += 1
                continue
            # `from None`: the chained transport error must not leak into tracebacks.
            reason = _redact(str(err) or "unknown error", api_key)
            raise OvertoneError(
                f"{label} text-to-speech failed (network error): {reason}",
                status=502,
                code="tts_failed",
            ) from None

        if res.is_success:
            return res.content

        retryable = res.status_code == 429 or res.status_code >= 500
        if retryable and attempt < MAX_RETRIES:
            delay = _retry_after_s(res.headers.get("retry-after"))
            await _sleep(delay if delay is not None else BACKOFF_S[attempt])
            attempt += 1
            continue

        excerpt = " ".join(_redact(res.text, api_key).split())[:EXCERPT_CHARS]
        detail = excerpt or res.reason_phrase or "no response body"
        if res.status_code in (401, 403):
            message = f"{label} text-to-speech failed ({res.status_code}): API key was rejected: {detail}"
        else:
            message = f"{label} text-to-speech failed ({res.status_code}): {detail}"
        raise OvertoneError(message, status=502, code="tts_failed")


class OpenAITts:
    """OpenAI speech endpoint (POST {base}/audio/speech, mp3 output)."""

    id = "openai"
    label = "OpenAI"
    mime_type = "audio/mpeg"
    max_chars = OPENAI_MAX_CHARS
    voices: list[TtsVoice]
    default_voices: list[str]

    def __init__(
        self,
        api_key: str,
        *,
        model: str | None = None,
        base_url: str | None = None,
        voices: list[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self.model = model or OPENAI_DEFAULT_MODEL
        self.base_url = (base_url or OPENAI_BASE_URL).rstrip("/")
        self.default_voices = list(voices) if voices else list(OPENAI_DEFAULT_VOICES)
        self.voices = _voice_list(
            (TtsVoice(id=v, label=_capitalize(v)) for v in OPENAI_VOICE_IDS),
            self.default_voices,
        )
        self._transport = transport

    async def synthesize(self, text: str, voice: str) -> bytes:
        """Render one utterance to mp3 bytes."""
        async with _http_client(self._transport) as client:
            return await _post_with_retry(
                client,
                label=self.label,
                api_key=self._api_key,
                url=f"{self.base_url}/audio/speech",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                body={"model": self.model, "voice": voice, "input": text, "response_format": "mp3"},
            )


class ElevenLabsTts:
    """ElevenLabs text-to-speech (POST {base}/v1/text-to-speech/{voice}, mp3 output)."""

    id = "elevenlabs"
    label = "ElevenLabs"
    mime_type = "audio/mpeg"
    max_chars = ELEVENLABS_MAX_CHARS
    voices: list[TtsVoice]
    default_voices: list[str]

    def __init__(
        self,
        api_key: str,
        *,
        model: str | None = None,
        base_url: str | None = None,
        voices: list[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self.model = model or ELEVENLABS_DEFAULT_MODEL
        self.base_url = (base_url or ELEVENLABS_BASE_URL).rstrip("/")
        self.default_voices = list(voices) if voices else list(ELEVENLABS_DEFAULT_VOICES)
        self.voices = _voice_list(ELEVENLABS_BUILT_IN_VOICES, self.default_voices)
        self._transport = transport

    async def synthesize(self, text: str, voice: str) -> bytes:
        """Render one utterance to mp3 bytes. Any voice id is accepted so custom voices work."""
        # encodeURIComponent-compatible escaping of the voice id.
        voice_path = quote(voice, safe="!*'()")
        async with _http_client(self._transport) as client:
            return await _post_with_retry(
                client,
                label=self.label,
                api_key=self._api_key,
                url=f"{self.base_url}/v1/text-to-speech/{voice_path}?output_format=mp3_44100_128",
                headers={
                    "xi-api-key": self._api_key,
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg",
                },
                body={"text": text, "model_id": self.model},
            )


# ───────────────────────── Provider resolution ─────────────────────────


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    t = value.strip()
    return t or None


def _split_voices(items: Iterable[str]) -> list[str]:
    return [v for v in (item.strip() for item in items) if v]


def create_tts_provider(config: TtsConfig | None = None) -> TtsProvider | None:
    """Build a provider from explicit config and/or environment variables. None when not configured.

    Explicit config wins over the environment. `provider: "auto"` (and any unrecognised
    value) picks OpenAI when OPENAI_API_KEY is set, else ElevenLabs when ELEVENLABS_API_KEY is set.
    A chosen provider without a key yields None rather than an error.
    """
    cfg = config or TtsConfig()
    env = os.environ
    requested = (_clean(cfg.provider) or _clean(env.get("OVERTONE_TTS_PROVIDER")) or "auto").lower()
    if requested == "none":
        return None

    model = _clean(cfg.model) or _clean(env.get("OVERTONE_TTS_MODEL"))
    base_url = _clean(cfg.base_url)
    env_voices_raw = _clean(env.get("OVERTONE_TTS_VOICES"))
    voices = _split_voices(cfg.voices or []) or (
        _split_voices(env_voices_raw.split(",")) if env_voices_raw else []
    ) or None

    env_openai_key = _clean(env.get("OPENAI_API_KEY"))
    env_eleven_key = _clean(env.get("ELEVENLABS_API_KEY"))

    provider_id: Literal["openai", "elevenlabs"]
    api_key: str | None
    if requested in ("openai", "elevenlabs"):
        provider_id = "openai" if requested == "openai" else "elevenlabs"
        api_key = _clean(cfg.api_key) or (env_openai_key if provider_id == "openai" else env_eleven_key)
    elif env_openai_key:
        provider_id, api_key = "openai", env_openai_key
    elif env_eleven_key:
        provider_id, api_key = "elevenlabs", env_eleven_key
    else:
        return None

    if not api_key:
        return None

    if provider_id == "openai":
        return OpenAITts(api_key, model=model, base_url=base_url, voices=voices)
    return ElevenLabsTts(api_key, model=model, base_url=base_url, voices=voices)


# ───────────────────────────── Text splitting ─────────────────────────────

_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")


def split_text(text: str, max_chars: int) -> list[str]:
    """Split text into pieces of at most `max_chars` characters.

    Prefers sentence boundaries, then whitespace, then hard cuts. No words are dropped.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be at least 1")
    t = text.strip()
    if not t:
        return []
    if len(t) <= max_chars:
        return [t]
    sentences: list[str] = []
    for sentence in _SENTENCE_BREAK.split(t):
        if len(sentence) <= max_chars:
            sentences.append(sentence)
        else:
            sentences.extend(_split_words(sentence, max_chars))
    return _pack(sentences, max_chars)


def _split_words(s: str, max_chars: int) -> list[str]:
    pieces: list[str] = []
    for word in s.split():
        if len(word) <= max_chars:
            pieces.append(word)
        else:
            pieces.extend(word[i : i + max_chars] for i in range(0, len(word), max_chars))
    return _pack(pieces, max_chars)


def _pack(pieces: Iterable[str], max_chars: int) -> list[str]:
    """Greedily join pieces (each already <= max_chars) with spaces without exceeding max_chars."""
    out: list[str] = []
    cur = ""
    for piece in pieces:
        if not piece:
            continue
        if not cur:
            cur = piece
        elif len(cur) + 1 + len(piece) <= max_chars:
            cur += " " + piece
        else:
            out.append(cur)
            cur = piece
    if cur:
        out.append(cur)
    return out


# ───────────────────────────── Dialogue rendering ─────────────────────────────


@dataclass(frozen=True)
class _Job:
    turn: int
    text: str
    voice: str


def _voice_for(provider: TtsProvider, script: AudioScript, speaker: int) -> str:
    """The host's voice, else the provider default for that host index (wrapping)."""
    hosts = script.hosts
    host = hosts[speaker] if 0 <= speaker < len(hosts) else None
    voice: str | None = host.voice if host and host.voice else None
    defaults = provider.default_voices
    if not voice and defaults and speaker >= 0:
        voice = defaults[speaker % len(defaults)]
    if not voice:
        raise OvertoneError(f"No voice available for speaker {speaker}", status=500, code="tts_no_voice")
    return voice


def _notify(on_progress: Callable[[int, int], None] | None, done: int, total: int) -> None:
    """Call the progress hook; an exception from it must never break synthesis."""
    if on_progress is None:
        return
    try:
        on_progress(done, total)
    except Exception:
        logger.warning("TTS progress callback raised; ignoring", exc_info=True)


def _concurrency_limit(value: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_CONCURRENCY
    return max(1, n)


async def synthesize_dialogue(
    provider: TtsProvider,
    script: AudioScript,
    *,
    on_progress: Callable[[int, int], None] | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> tuple[bytes, str]:
    """Render every turn of a script and join the results. Returns (audio bytes, mime type).

    Long turns are split under the provider's `max_chars`. Requests run with bounded
    concurrency; output is assembled in script order. On the first failure the remaining
    work is cancelled and that error is re-raised. Cancelling the caller's task propagates.
    """
    turns = list(script.turns)
    if not turns:
        raise OvertoneError("The script has no dialogue turns to render", status=400, code="empty_script")

    max_chars = getattr(provider, "max_chars", None) or DEFAULT_MAX_CHARS
    total = len(turns)

    # Plan every request up front, in script order.
    jobs: list[_Job] = []
    remaining = [0] * total
    done = 0
    for i, turn in enumerate(turns):
        voice = _voice_for(provider, script, turn.speaker)
        chunks = split_text(turn.text or "", max_chars)
        remaining[i] = len(chunks)
        if not chunks:
            done += 1  # nothing to say: the turn is complete already
        jobs.extend(_Job(turn=i, text=chunk, voice=voice) for chunk in chunks)

    if done > 0:
        _notify(on_progress, done, total)
    if not jobs:
        return concat_mp3([]), provider.mime_type

    results: list[bytes] = [b""] * len(jobs)
    gate = asyncio.Semaphore(min(_concurrency_limit(concurrency), len(jobs)))
    failed = False

    async def run(j: int) -> None:
        nonlocal done, failed
        job = jobs[j]
        if failed:
            return  # another segment already failed: schedule nothing more
        async with gate:
            if failed:
                return
            try:
                results[j] = await provider.synthesize(job.text, job.voice)
            except Exception:
                failed = True
                raise
        remaining[job.turn] -= 1
        if remaining[job.turn] == 0:
            done += 1
            _notify(on_progress, done, total)

    tasks = [asyncio.create_task(run(j)) for j in range(len(jobs))]
    try:
        pending: set[asyncio.Task[None]] = set(tasks)
        while pending:
            finished, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_EXCEPTION)
            for task in finished:
                if task.cancelled():
                    continue
                err = task.exception()
                if err is not None:
                    raise err
    finally:
        for task in tasks:
            task.cancel()
        # Drain so no task is left pending or with an unretrieved exception.
        await asyncio.gather(*tasks, return_exceptions=True)

    return concat_mp3(results), provider.mime_type


# ───────────────────────────── MP3 helpers ─────────────────────────────

_ID3V2_HEADER_LENGTH = 10
_ID3V2_FOOTER_LENGTH = 10
_ID3V2_FOOTER_FLAG = 0x10
_ID3V1_LENGTH = 128


def _has_ascii(data: bytes, offset: int, text: bytes) -> bool:
    return offset >= 0 and data[offset : offset + len(text)] == text


def strip_id3(data: bytes) -> bytes:
    """Remove a leading ID3v2 tag and a trailing ID3v1 tag. Frame data is left untouched."""
    start = 0
    end = len(data)

    if _has_ascii(data, 0, b"ID3") and len(data) >= _ID3V2_HEADER_LENGTH:
        # Tag size is a 28-bit "synchsafe" integer: 7 bits per byte, MSB first.
        size = (
            ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) | ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
        )
        total = _ID3V2_HEADER_LENGTH + size
        if data[5] & _ID3V2_FOOTER_FLAG:
            total += _ID3V2_FOOTER_LENGTH
        start = min(total, len(data))

    if end - start >= _ID3V1_LENGTH and _has_ascii(data, end - _ID3V1_LENGTH, b"TAG"):
        end -= _ID3V1_LENGTH

    return data[start : max(start, end)]


def concat_mp3(segments: Iterable[bytes]) -> bytes:
    """Join same-format MP3 segments by concatenating their frames. ID3 tags are stripped; no re-encoding."""
    return b"".join(strip_id3(segment) for segment in segments)
