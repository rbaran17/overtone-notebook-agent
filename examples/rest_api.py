"""Drive a running Overtone server over plain HTTP: the same calls any language can make.

    overtone serve                       # in backend/, first (add OVERTONE_MOCK=1 to run without keys)
    python examples/rest_api.py          # walk through the API, then clean up
    python examples/rest_api.py --keep   # leave the notebook in place to open in the web UI

Options: --base http://127.0.0.1:8787   --api-key <key> (or $OVERTONE_API_KEY)
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

SOURCES = Path(__file__).parent / "sources"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=os.environ.get("OVERTONE_API_URL", "http://127.0.0.1:8787"))
    parser.add_argument("--api-key", default=os.environ.get("OVERTONE_API_KEY"))
    parser.add_argument("--keep", action="store_true", help="do not delete the notebook at the end")
    args = parser.parse_args()

    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    with httpx.Client(base_url=args.base, headers=headers, timeout=600) as http:
        try:
            health = http.get("/health").raise_for_status().json()
        except httpx.HTTPError as exc:
            raise SystemExit(f"No Overtone server at {args.base} ({exc}). Start one with: overtone serve")
        print(f"Server: Overtone {health['version']}, model {health['model']}, speech: {(health['tts'] or {}).get('label', 'none')}")

        # 1. A notebook.
        notebook = http.post("/notebooks", json={"title": "Tidewater Transit"}).raise_for_status().json()["notebook"]
        print(f"Notebook: {notebook['id']}")

        # 2. Sources: upload files as multipart. JSON works too: {"type": "url", "url": "..."} or {"type": "text", ...}.
        for path in sorted(SOURCES.glob("*.md")):
            files = {"file": (path.name, path.read_bytes(), "text/markdown")}
            added = http.post(f"/notebooks/{notebook['id']}/sources", files=files).raise_for_status().json()
            print(f"  + {added['sources'][0]['title']}")

        # 3. Chat: a Server-Sent Events stream of text, citation and done events.
        print("\nQ: What do riders want most, and does the fare proposal deliver it?\nA: ", end="")
        body = {"message": "What do riders want most, and does the fare proposal deliver it?"}
        with http.stream("POST", f"/notebooks/{notebook['id']}/chat", json=body) as stream:
            for line in stream.iter_lines():
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[5:])
                if event["type"] == "text":
                    print(event["delta"], end="", flush=True)
                elif event["type"] == "done":
                    print("\n")
                    for citation in event["message"].get("citations", []):
                        print(f"   [{citation['index']}] {citation['sourceTitle']}")
                elif event["type"] == "error":
                    raise SystemExit(f"\nChat failed: {event['error']}")

        # 4. Artifacts: creation returns at once; poll until the artifact is ready.
        requests = [
            {"kind": "audio_overview", "options": {"format": "deep_dive", "length": "short"}},
            {"kind": "briefing"},
            {"kind": "faq"},
        ]
        print()
        for request in requests:
            artifact = http.post(f"/notebooks/{notebook['id']}/artifacts", json=request).raise_for_status().json()["artifact"]
            while artifact["status"] in ("queued", "generating"):
                time.sleep(0.5)
                artifact = http.get(f"/artifacts/{artifact['id']}").raise_for_status().json()["artifact"]
            if artifact["status"] == "error":
                raise SystemExit(f"Generation failed: {artifact.get('error')}")
            if artifact["kind"] == "audio_overview":
                detail = f"{len(artifact['script']['turns'])} turns"
                detail += f", audio at {args.base}/artifacts/{artifact['id']}/audio" if artifact.get("audio") else ", no rendered audio (no speech key)"
            else:
                detail = f"{len(artifact['content']):,} characters of Markdown"
            print(f"Made {artifact['kind']}: {artifact['title']} ({detail})")

        # 5. Clean up.
        if args.keep:
            print(f"\nKept. Open the web UI and look for \"{notebook['title']}\" (notebook {notebook['id']}).")
        else:
            http.delete(f"/notebooks/{notebook['id']}").raise_for_status()
            print("\nDeleted the notebook. Run with --keep to leave it for the web UI.")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
