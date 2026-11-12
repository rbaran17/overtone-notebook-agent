"""Live voice sessions on OpenAI's GPT-Live (`gpt-live-1`).

GPT-Live is a full-duplex speech model: it listens and talks at the same time
and hands anything that needs real work to a backend. Here the division is:

* GPT-Live is the voice. The browser connects to it directly over WebRTC.
* Overtone is the brain. When the voice model delegates a question, the browser
  relays it to `Engine.live.delegate`, the Agent Framework voice-researcher
  answers from the notebook's sources, and the answer is appended to the live
  session for the voice model to say.

This module only knows how to start a session. The API key never leaves the
server: the browser sends its SDP offer here, and gets the SDP answer back.

Reference: https://developers.openai.com/api/docs/guides/voice-webrtc?api=live
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import httpx

from .errors import OvertoneError

DEFAULT_LIVE_MODEL = "gpt-live-1"
DEFAULT_LIVE_VOICE = "marin"

# Built-in GPT-Live voices (API reference, "Create session").
LIVE_VOICES: tuple[str, ...] = (
    "alloy", "ash", "ballad", "beacon", "bossa", "brise", "calandria", "cedar", "cinder", "compas", "coral",
    "delta", "destello", "echo", "flitz", "gleam", "halo", "harema", "hornero", "juni", "luciernaga", "marin",
    "meridian", "nira", "noeul", "nuri", "pearl", "quartz", "ripple", "sage", "shida", "shimmer", "sillage",
    "stone", "tempo", "verse", "vesper", "willow",
)  # fmt: skip

# What the browser may send on the WebRTC data channel: close the session, and
# hand back delegated results. Nothing that rewrites the session's instructions.
_CLIENT_EVENTS = ["session.close", "session.commentary.append", "session.thinking.append"]


@dataclass
class LiveVoice:
    """Client for the GPT-Live session endpoint."""

    api_key: str
    model: str = DEFAULT_LIVE_MODEL
    default_voice: str = DEFAULT_LIVE_VOICE
    base_url: str = "https://api.openai.com/v1"
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    def pick_voice(self, requested: str | None) -> str:
        return requested if requested and requested in LIVE_VOICES else self.default_voice

    async def start_webrtc_session(
        self, *, instructions: str, voice: str, sdp_offer: str, safety_identifier: str | None = None
    ) -> tuple[str, str]:
        """Create a session from the browser's SDP offer. Returns (session id, SDP answer)."""
        session: dict[str, object] = {
            "model": self.model,
            "instructions": instructions,
            "audio": {"output": {"voice": voice}},
            # Delegated work comes to us (via the browser), not to an OpenAI backend model.
            # The short delay lets the voice model say a natural "let me check" first.
            "delegation": {"type": "client", "acknowledgement_delay_ms": 400},
            "client": {
                "data_channel": {
                    "allowed_client_events": "all"
                    if os.environ.get("OVERTONE_LIVE_CLIENT_EVENTS") == "all"
                    else _CLIENT_EVENTS,
                    "allowed_server_events": "all",
                }
            },
        }
        if safety_identifier:
            session["safety_identifier"] = safety_identifier[:128]
        body = {"session": session, "transport": {"type": "webrtc", "sdp": sdp_offer}}

        try:
            async with httpx.AsyncClient(timeout=30, transport=self.transport) as client:
                response = await client.post(
                    f"{self.base_url.rstrip('/')}/live/sessions",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise OvertoneError(
                "Could not reach the live voice service. Check your network connection.",
                status=502,
                code="live_unreachable",
            ) from None if self.api_key in str(exc) else exc

        if response.status_code in (401, 403):
            raise OvertoneError(
                "The live voice service rejected the API key. Check OPENAI_API_KEY and that the account has "
                f"access to {self.model}.",
                status=502,
                code="live_auth",
            )
        if response.status_code >= 400:
            detail = ""
            try:
                payload = response.json()
                error = payload.get("error") if isinstance(payload, dict) else None
                detail = str(error.get("message", "")) if isinstance(error, dict) else str(error or "")
            except ValueError:
                detail = response.text[:300]
            detail = detail.replace(self.api_key, "[redacted]")
            raise OvertoneError(
                f"The live voice service refused the session ({response.status_code}): {detail}".rstrip(": "),
                status=502,
                code="live_failed",
            )
        try:
            data = response.json()
            return str(data["session"]["id"]), str(data["transport"]["sdp"])
        except (ValueError, KeyError, TypeError) as exc:
            raise OvertoneError(
                "The live voice service returned an unexpected response.", status=502, code="live_failed"
            ) from exc


def create_live_voice() -> LiveVoice | None:
    """Live voice is available when an OpenAI key is configured (and not switched off)."""
    if os.environ.get("OVERTONE_LIVE") == "0":
        return None
    api_key = os.environ.get("OVERTONE_LIVE_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    voice = os.environ.get("OVERTONE_LIVE_VOICE") or DEFAULT_LIVE_VOICE
    return LiveVoice(
        api_key=api_key,
        model=os.environ.get("OVERTONE_LIVE_MODEL") or DEFAULT_LIVE_MODEL,
        default_voice=voice if voice in LIVE_VOICES else DEFAULT_LIVE_VOICE,
        base_url=os.environ.get("OVERTONE_LIVE_BASE_URL") or "https://api.openai.com/v1",
    )
