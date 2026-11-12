# Contributing to Overtone

Thanks for your interest in Overtone. This guide covers local setup, the checks
CI runs, and how to send a change.

## Getting set up

Overtone has two parts: a Python backend in `backend/` and a Next.js web UI at
the repository root. You need Python 3.11+ and Node.js 20+ (CI uses Node 22).

### Backend

Create and activate a virtual environment in `backend/`, then install the
package with its development extras.

```bash
cd backend
python -m venv .venv
```

Activate it:

- Windows: `.venv\Scriptsctivate`
- macOS / Linux: `source .venv/bin/activate`

```bash
pip install -e ".[dev]"
```

### Web UI

```bash
npm ci
```

### Mock mode

Mock mode runs everything without API keys, so you can work on Overtone
offline:

```bash
OVERTONE_MOCK=1 overtone serve
```

Real provider keys are optional. They go in `backend/.env` (copy
`.env.example` from the repository root), which is never committed.

## Running the checks

Run these before opening a pull request.

```bash
# backend tests (from backend/)
cd backend && python -m pytest

# offline evaluation suite (from backend/); exits non-zero when a check fails
cd backend && overtone eval --mock --report eval-report.md

# web and client checks (from the repository root)
npm run lint
npm run typecheck
npm run test:client
npm run build
```

The backend tests and the evaluation suite use a mock model and need no API
keys and no network access.

## Project layout

- `backend/overtone/`: the Python package (FastAPI REST API, `overtone` CLI,
  agent engine).
- `backend/tests/`: the pytest suite.
- `packages/client/`: `@overtone/client`, the TypeScript REST client.
- `src/`: the Next.js web UI.
- `examples/`: runnable examples and sample sources.
- `docs/`: REST reference, evaluation notes and screenshots.

## Making a change

1. Create a branch for your change.
2. Keep each pull request focused on one change. Keep refactors separate from
   behaviour changes.
3. Add or adjust tests alongside the change.
4. Run the checks above. The offline suite must stay green with no network
   access.
5. Fill in the pull request template.

Never commit API keys, tokens, or `.overtone/` data. Keys belong in
`backend/.env`, and `.overtone/` holds local runtime data.

Provider integrations (Claude, OpenAI TTS, ElevenLabs, GPT-Live) are exercised
against fakes in the test suite. If your pull request touches one of them, say
in the description how you checked the change against the real service.

## Reporting bugs

Open a bug report or feature request using the issue templates in
`.github/ISSUE_TEMPLATE/`. Blank issues are disabled.

Do not report security vulnerabilities in a public issue. See
[SECURITY.md](SECURITY.md).
