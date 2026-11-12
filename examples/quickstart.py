"""Quickstart: sources in; a cited answer, a podcast script and an FAQ out.

    python examples/quickstart.py

Uses Claude when ANTHROPIC_API_KEY is set. Without a key it runs offline on the
built-in stand-in model, so you can see the whole flow before you configure anything.
"""

import asyncio
import os
import sys
from pathlib import Path

if not os.environ.get("ANTHROPIC_API_KEY"):
    os.environ.setdefault("OVERTONE_MOCK", "1")

from overtone import ArtifactRequest, AudioHost, AudioOptions, FileInput, MemoryStorage, create_engine

SOURCES = Path(__file__).parent / "sources"


async def main() -> None:
    # MemoryStorage keeps this run off the disk. Drop the argument to persist to ./.overtone.
    engine = create_engine(storage=MemoryStorage())
    model = engine.capabilities().model
    print(f"Model: {model}" + ("  (stand-in model: set ANTHROPIC_API_KEY for real answers)" if model == "mock" else ""))

    notebook = await engine.notebooks.create(title="Tidewater Transit")
    for path in sorted(SOURCES.glob("*.md")):
        source = await engine.sources.add(notebook.id, FileInput(filename=path.name, data=path.read_bytes()))
        print(f"Added source: {source.title} ({source.char_count:,} characters)")

    print("\n=== Grounded answer ===\n")
    question = "What do riders complain about most, and what is being proposed to fix it?"
    async for event in engine.chat.stream(notebook.id, question):
        if event["type"] == "text":
            print(event["delta"], end="", flush=True)
        elif event["type"] == "done":
            print("\n")
            for citation in event["message"].get("citations", []):
                print(f'  [{citation["index"]}] {citation["sourceTitle"]}: "{citation["quote"][:80]}..."')
        elif event["type"] == "error":
            raise SystemExit(f"Chat failed: {event['error']}")

    print("\n=== Audio overview ===\n")
    stages: list[str] = []

    def on_progress(artifact) -> None:
        if artifact.progress and artifact.progress not in stages:
            stages.append(artifact.progress)
            print(f"  ... {artifact.progress}")

    episode = await engine.artifacts.generate(
        notebook.id,
        ArtifactRequest(
            kind="audio_overview",
            options=AudioOptions(
                format="debate",
                length="short",
                hosts=[AudioHost(name="Ana"), AudioHost(name="Ben")],
                instructions="Argue over whether the fare increase is fair to riders.",
            ),
        ),
        on_progress=on_progress,
    )
    script = episode.script
    print(f"\n{script.title}\n{script.summary}\n")
    for turn in script.turns[:4]:
        print(f"{script.hosts[turn.speaker].name}: {turn.text}\n")
    print(f"({len(script.turns)} turns in total)")

    audio = await engine.artifacts.get_audio(episode.id)
    if audio:
        Path("episode.mp3").write_bytes(audio[0])
        print("Wrote episode.mp3")
    else:
        print("No speech key is set, so there is no MP3. Set OPENAI_API_KEY or ELEVENLABS_API_KEY to render one.")

    print("\n=== FAQ ===\n")
    faq = await engine.artifacts.generate(notebook.id, ArtifactRequest(kind="faq"))
    print(faq.content[:700].rstrip() + ("\n..." if len(faq.content) > 700 else ""))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
