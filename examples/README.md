# Examples

Each example runs as it is. Without `ANTHROPIC_API_KEY` the Python examples fall back to the offline stand-in model, so you can see the flow before configuring anything.

Install the backend first (`pip install -e "backend[dev]"` from the repository root, in a virtualenv), and run the examples from the repository root.

| Example | What it shows | Run |
| --- | --- | --- |
| [quickstart.py](quickstart.py) | The Python SDK: add sources, stream a cited answer, generate a debate episode and an FAQ | `python examples/quickstart.py` |
| [custom_workflow.py](custom_workflow.py) | Insert your own Microsoft Agent Framework executor between the scriptwriter and the sound engineer | `python examples/custom_workflow.py` |
| [embed_fastapi.py](embed_fastapi.py) | Mount the Overtone API inside an existing FastAPI app | `python examples/embed_fastapi.py` |
| [rest_api.py](rest_api.py) | Plain HTTP against a running server: multipart upload, the SSE chat stream, artifact polling | `python examples/rest_api.py` |
| [node_client.ts](node_client.ts) | The typed TypeScript client | `npx tsx examples/node_client.ts` |

`rest_api.py` and `node_client.ts` talk to a server, so start one first: `overtone serve` in `backend/` (prefix with `OVERTONE_MOCK=1` to run it without keys). `python examples/rest_api.py --keep` leaves its notebook in place, which is a quick way to get demo content into the web UI.

[sources/](sources/) holds three short, fictional documents about a transit agency. They are the same corpus the evaluation suite uses, and their figures are specific enough to check an answer against.
