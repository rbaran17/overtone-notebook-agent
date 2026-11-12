"""A deterministic Agent Framework chat client that makes no network calls.

It assembles its replies from the passages it is given, so the whole pipeline
(agents → citations → workflow → audio → API) can be exercised in tests, demos
and CI without an API key. Enable with OVERTONE_MOCK=1 or
`create_engine(chat_client=MockChatClient())`.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterable
from typing import Any

from agent_framework import (
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    FunctionInvocationLayer,
    Message,
)

_SOURCE = re.compile(r'<source title="([^"]*)">\n(.*?)\n</source>', re.S)
_PASSAGE = re.compile(r"^\[(\d+)\] (.*)$", re.M)


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"(^|\s)#+\s", r"\1", text)  # passages may carry Markdown heading marks
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


_STOPWORDS = frozenset(
    "about after again also been being both does doing each from have here into just more most much only other "
    "over said same should some such than that their them then there these they this those through under very "
    "want what when where which while will with would your".split()
)


def _keywords(text: str) -> set[str]:
    """Lowercased content words, crudely stemmed so "riders" matches "rider"."""
    words = re.findall(r"[^\W\d_]{4,}", text.lower())
    return {w[:-1] if w.endswith("s") else w for w in words if w not in _STOPWORDS}


def _best_matches(
    question: str, sources: list[tuple[str, list[tuple[str, str]]]], limit: int = 3
) -> list[tuple[str, str, str]]:
    """The passages sharing the most words with the question, as (source title, number, text).

    Plain keyword overlap: enough to make the offline demo point at relevant passages.
    """
    wanted = _keywords(question)
    scored = [
        (len(wanted & _keywords(text)), -int(number), title, number, text)
        for title, passages in sources
        for number, text in passages
    ]
    best = sorted((s for s in scored if s[0] > 0), reverse=True)[:limit]
    return [(title, number, text) for _, _, title, number, text in best]


class MockChatClient(FunctionInvocationLayer, BaseChatClient):  # type: ignore[type-arg, misc]
    """Stands in for a real model. `delay` is the pause between streamed words, in seconds."""

    model = "mock"

    def __init__(self, delay: float = 0.01) -> None:
        super().__init__()
        self.delay = delay

    def _inner_get_response(self, *, messages: Any, stream: bool, options: Any, **kwargs: Any) -> Any:
        text = self._reply(list(messages), dict(options))
        response_format = options.get("response_format")

        if stream:

            async def _updates() -> AsyncIterable[ChatResponseUpdate]:
                for word in re.findall(r"\S+\s*", text):
                    if self.delay:
                        await asyncio.sleep(self.delay)
                    yield ChatResponseUpdate(role="assistant", contents=[Content.from_text(word)])

            return self._build_response_stream(_updates(), response_format=response_format)

        async def _response() -> ChatResponse:
            return ChatResponse(
                messages=[Message(role="assistant", contents=[text])],
                response_id="mock-response",
                response_format=response_format,
            )

        return _response()

    def _reply(self, messages: list[Message], options: dict[str, Any]) -> str:
        from .agents import GuideOut, ScriptOut  # local import: agents imports this module lazily
        from .prompts import RESEARCHER_INSTRUCTIONS, VOICE_RESEARCHER_INSTRUCTIONS

        first = messages[0].text if messages else ""
        sources = [(title, _PASSAGE.findall(body)) for title, body in _SOURCE.findall(first)]
        last_user = next((m for m in reversed(messages) if m.role == "user"), None)
        question = ""
        if last_user is not None and last_user.contents:
            question = (getattr(last_user.contents[-1], "text", None) or "").strip()
        instructions = options.get("instructions") or ""
        response_format = options.get("response_format")
        titles = [title for title, _ in sources]

        if response_format is ScriptOut:
            two_hosts = "host_2" in question
            turns = [
                {
                    "speaker": "host_1",
                    "text": f"Welcome. This is a mock episode, made without calling a model, covering "
                    f"{len(sources)} source{'s' if len(sources) != 1 else ''}.",
                }
            ]
            for title, passages in sources[:4]:
                turns.append(
                    {"speaker": "host_2" if two_hosts else "host_1", "text": f"Let's look at {title}. What does it say?"}
                )
                turns.append({"speaker": "host_1", "text": _clip(passages[0][1], 320) if passages else "It is empty."})
            turns.append(
                {"speaker": "host_2" if two_hosts else "host_1", "text": "And that's the overview. Thanks for listening."}
            )
            return json.dumps(
                {
                    "title": f"Mock overview: {titles[0] if titles else 'your sources'}",
                    "summary": "A placeholder episode assembled from the opening lines of each source.",
                    "turns": turns,
                }
            )

        if response_format is GuideOut:
            return json.dumps(
                {
                    "summary": f"**Mock guide.** This notebook has {len(sources)} source"
                    f"{'s' if len(sources) != 1 else ''}: {', '.join(titles)}.",
                    "questions": [
                        "What are the main points of these sources?",
                        f"What does {titles[0] if titles else 'the first source'} conclude?",
                        "Where do the sources disagree?",
                    ],
                }
            )

        if instructions == VOICE_RESEARCHER_INSTRUCTIONS:
            for title, passages in sources:
                if passages:
                    number, text = passages[0]
                    return f"This is a mock answer. {title} opens like this: {_clip(text, 160)} [{number}]"
            return "This is a mock answer, and the sources are empty."

        if instructions == RESEARCHER_INSTRUCTIONS:
            matches = _best_matches(question, sources)
            if matches:
                intro, picked = "These passages match your question most closely.", matches
            else:
                intro = "Here is what the selected sources open with."
                picked = [(title, *passages[0]) for title, passages in sources[:3] if passages]
            lines = [f'This is a mock answer (no model was called). You asked: "{question}". {intro}', ""]
            for title, number, text in picked:
                lines.append(f"- **{title}**: {_clip(text, 220)} [{number}]")
            return "\n".join(lines)

        heading = "Mock document"
        body = "\n\n".join(
            f"## {title}\n\n{' '.join(_clip(text, 300) for _, text in passages[:2])}" for title, passages in sources
        )
        return f"# {heading}\n\n*Generated without calling a model.*\n\n{body}"
