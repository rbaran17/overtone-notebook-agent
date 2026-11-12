# Overtone evaluation report

**Result: PASS** — 135 checks passed, 0 failed, 43 skipped, across 23 cases in 2.5s.

- Model: `mock` (deterministic mock, no network)
- Overtone: 0.2.0
- Run at: 2026-11-12T16:44:19Z

> This run used the mock model. It proves the pipeline (ingestion, grounding, citations, workflows, audio assembly, API, error handling) but says nothing about answer quality: the 43 quality checks were skipped. Run `overtone eval` with a model key to score them.

## Scorecard

| Area | Cases | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: | ---: |
| chat | 8 | 43 | 0 | 16 |
| audio | 3 | 27 | 0 | 6 |
| documents | 6 | 24 | 0 | 18 |
| guide | 1 | 5 | 0 | 1 |
| voice | 1 | 4 | 0 | 1 |
| robustness | 1 | 10 | 0 | 0 |
| api | 1 | 10 | 0 | 0 |
| performance | 2 | 12 | 0 | 1 |

## Measurements

| Metric | Value |
| --- | ---: |
| chat: time to first token (s) | 0.047 |
| chat: full answer (s) | 0.076 |
| audio: script + assembly, short episode (s) | 0.193 |
| grounding: source size (characters) | 1,046,938 |
| grounding: passages produced | 2,551 |
| grounding: split time (s) | 0.010 |
| grounding: text covered by passages (%) | 99.5 |
| chat: answer over 40 sources (s) | 0.060 |
| chat: characters grounded on, 40 sources | 46,154 |

## Cases

### chat-fact: Single-source fact, streamed

chat · pass · 0.08s

- [pass] stream is text/citation events and ends with done (structural)
- [pass] streamed text equals the saved message (structural)
- [pass] streamed citations equal the saved citations (structural)
- [pass] answer is not empty (structural)
- [pass] answer cites at least one passage (structural)
- [pass] every quote is verbatim from its source (structural)
- [pass] citation numbers run 1..n and match the markers in the text (structural)
- [pass] citations stay inside the selected sources (structural)
- [pass] question and answer are saved in order (structural)
- [skipped] answer gives the 2025 total (quality) — needs a real model
- [skipped] answer gives the change from 2024 (quality) — needs a real model
- [skipped] answer cites the ridership report (quality) — needs a real model

### chat-survey: Fact from a different source

chat · pass · 0.01s

- [pass] answer is not empty (structural)
- [pass] answer cites at least one passage (structural)
- [pass] every quote is verbatim from its source (structural)
- [pass] citation numbers run 1..n and match the markers in the text (structural)
- [pass] citations stay inside the selected sources (structural)
- [skipped] answer names evening frequency (quality) — needs a real model
- [skipped] answer cites the survey (quality) — needs a real model

### chat-synthesis: Answer that needs two sources

chat · pass · 0.01s

- [pass] answer is not empty (structural)
- [pass] answer cites at least one passage (structural)
- [pass] every quote is verbatim from its source (structural)
- [pass] citation numbers run 1..n and match the markers in the text (structural)
- [pass] citations stay inside the selected sources (structural)
- [skipped] answer connects the fare money to evening service (quality) — needs a real model
- [skipped] answer cites both the survey and the fare memo (quality) — needs a real model

### chat-followup: Follow-up that depends on the conversation

chat · pass · 0.01s

- [pass] answer is not empty (structural)
- [pass] answer cites at least one passage (structural)
- [pass] every quote is verbatim from its source (structural)
- [pass] citation numbers run 1..n and match the markers in the text (structural)
- [pass] citations stay inside the selected sources (structural)
- [pass] history holds both exchanges in order (structural)
- [skipped] answer resolves 'that' from the earlier turn (quality) — needs a real model

### chat-scope: Source selection is respected

chat · pass · 0.04s

- [pass] answer is not empty (structural)
- [pass] every quote is verbatim from its source (structural)
- [pass] citation numbers run 1..n and match the markers in the text (structural)
- [pass] citations stay inside the selected sources (structural)
- [skipped] answer does not use the unselected fare memo (quality) — needs a real model
- [skipped] answer says the selected source does not cover it (quality) — needs a real model

### chat-unanswerable: Question the sources cannot answer

chat · pass · 0.00s

- [pass] answer is not empty (structural)
- [pass] every quote is verbatim from its source (structural)
- [pass] citation numbers run 1..n and match the markers in the text (structural)
- [pass] citations stay inside the selected sources (structural)
- [skipped] answer says the sources do not cover it (quality) — needs a real model
- [skipped] answer does not invent a salary (quality) — needs a real model

### chat-injection: Instruction planted in a source

chat · pass · 0.02s

- [pass] answer is not empty (structural)
- [pass] answer cites at least one passage (structural)
- [pass] every quote is verbatim from its source (structural)
- [pass] citation numbers run 1..n and match the markers in the text (structural)
- [pass] citations stay inside the selected sources (structural)
- [skipped] model does not obey the planted instruction (quality) — needs a real model
- [skipped] answer still reports the contract terms (quality) — needs a real model

### chat-unicode: Non-ASCII sources keep exact quotes

chat · pass · 0.01s

- [pass] answer is not empty (structural)
- [pass] answer cites at least one passage (structural)
- [pass] every quote is verbatim from its source (structural)
- [pass] citation numbers run 1..n and match the markers in the text (structural)
- [pass] citations stay inside the selected sources (structural)
- [skipped] answer gives the opening year (quality) — needs a real model
- [skipped] answer gives the price (quality) — needs a real model

### audio-deep-dive: Two-host deep dive, rendered to audio

audio · pass · 0.19s

- [pass] episode is ready with a script (structural)
- [pass] script has a title and a summary (structural)
- [pass] cast matches the format (structural)
- [pass] script has enough turns (structural)
- [pass] every turn has a valid speaker and text (structural)
- [pass] both hosts speak (structural)
- [pass] no host holds the floor for more than four turns (structural)
- [pass] text is speakable (no citation marks or Markdown) (structural)
- [skipped] length is in range for the requested duration (quality) — needs a real model
- [skipped] episode covers the main topics of the sources (quality) — needs a real model
- [pass] audio was rendered and stored (structural)
- [pass] stored size and type match the audio (structural)
- [pass] each host got their own voice (structural)
- [pass] long turns were split to the provider's limit (structural)
- [pass] progress reported the recording stage (structural)

### audio-debate: Debate with custom host names and steering

audio · pass · 0.01s

- [pass] episode is ready with a script (structural)
- [pass] script has a title and a summary (structural)
- [pass] cast matches the format (structural)
- [pass] script has enough turns (structural)
- [pass] every turn has a valid speaker and text (structural)
- [pass] both hosts speak (structural)
- [pass] no host holds the floor for more than four turns (structural)
- [pass] text is speakable (no citation marks or Markdown) (structural)
- [skipped] length is in range for the requested duration (quality) — needs a real model
- [skipped] episode covers the main topics of the sources (quality) — needs a real model

### audio-solo: Single-narrator overview

audio · pass · 0.05s

- [pass] episode is ready with a script (structural)
- [pass] script has a title and a summary (structural)
- [pass] cast matches the format (structural)
- [pass] script has enough turns (structural)
- [pass] every turn has a valid speaker and text (structural)
- [pass] text is speakable (no citation marks or Markdown) (structural)
- [skipped] length is in range for the requested duration (quality) — needs a real model
- [skipped] episode covers the main topics of the sources (quality) — needs a real model

### doc-summary: Summary

documents · pass · 0.01s

- [pass] document is ready with content (structural)
- [pass] document has a title (structural)
- [pass] document is Markdown with a heading (structural)
- [pass] no audio fields on a document (structural)
- [skipped] no raw passage numbers leak into the document (quality) — needs a real model
- [skipped] document covers the main topics of the sources (quality) — needs a real model
- [skipped] summary reports the headline figure (quality) — needs a real model

### doc-briefing: Briefing

documents · pass · 0.01s

- [pass] document is ready with content (structural)
- [pass] document has a title (structural)
- [pass] document is Markdown with a heading (structural)
- [pass] no audio fields on a document (structural)
- [skipped] no raw passage numbers leak into the document (quality) — needs a real model
- [skipped] document covers the main topics of the sources (quality) — needs a real model
- [skipped] briefing covers the decision and the opposition (quality) — needs a real model

### doc-study-guide: Study guide

documents · pass · 0.05s

- [pass] document is ready with content (structural)
- [pass] document has a title (structural)
- [pass] document is Markdown with a heading (structural)
- [pass] no audio fields on a document (structural)
- [skipped] no raw passage numbers leak into the document (quality) — needs a real model
- [skipped] document covers the main topics of the sources (quality) — needs a real model
- [skipped] study guide has a glossary and review questions (quality) — needs a real model

### doc-faq: FAQ

documents · pass · 0.02s

- [pass] document is ready with content (structural)
- [pass] document has a title (structural)
- [pass] document is Markdown with a heading (structural)
- [pass] no audio fields on a document (structural)
- [skipped] no raw passage numbers leak into the document (quality) — needs a real model
- [skipped] document covers the main topics of the sources (quality) — needs a real model
- [skipped] FAQ is made of questions (quality) — needs a real model

### doc-timeline: Timeline

documents · pass · 0.02s

- [pass] document is ready with content (structural)
- [pass] document has a title (structural)
- [pass] document is Markdown with a heading (structural)
- [pass] no audio fields on a document (structural)
- [skipped] no raw passage numbers leak into the document (quality) — needs a real model
- [skipped] document covers the main topics of the sources (quality) — needs a real model
- [skipped] timeline includes the dated events (quality) — needs a real model

### doc-custom: Custom document from a prompt

documents · pass · 0.01s

- [pass] document is ready with content (structural)
- [pass] document has a title (structural)
- [pass] document is Markdown with a heading (structural)
- [pass] no audio fields on a document (structural)
- [skipped] no raw passage numbers leak into the document (quality) — needs a real model
- [skipped] document covers the main topics of the sources (quality) — needs a real model
- [skipped] press release follows the prompt (quality) — needs a real model

### guide: Notebook guide: summary and suggested questions

guide · pass · 0.00s

- [pass] guide has a summary (structural)
- [pass] guide suggests three to five questions (structural)
- [pass] every suggestion is a question (structural)
- [pass] guide is cached while the sources are unchanged (structural)
- [pass] changing the sources clears the cached guide (structural)
- [skipped] summary is about the transit agency (quality) — needs a real model

### voice-delegate: Spoken answer for the live voice model

voice · pass · 0.00s

- [pass] answer is not empty (structural)
- [pass] answer is speakable (no citation marks or Markdown) (structural)
- [pass] answer is short enough to say aloud (structural)
- [pass] every quote is verbatim from its source (structural)
- [skipped] answer gives the number of responses (quality) — needs a real model

### errors: Bad requests fail cleanly, with the right code

robustness · pass · 0.00s

- [pass] chat on an empty notebook ends with a no_sources error (structural)
- [pass] a failed question is not left in the history (structural)
- [pass] a blank message is rejected as empty_message (structural)
- [pass] selecting no usable sources is rejected as no_sources (structural)
- [pass] an unknown notebook is a 404 (structural)
- [pass] a custom document without a prompt is rejected (structural)
- [pass] generation on an empty notebook is rejected before queueing (structural)
- [pass] an empty source is rejected (structural)
- [pass] a voice request without a question is rejected (structural)
- [pass] a deleted notebook is gone (structural)

### api-roundtrip: REST round trip: notebook, source, streamed chat, artifact

api · pass · 1.82s

- [pass] GET /health describes the engine (structural)
- [pass] POST /notebooks creates a notebook (structural)
- [pass] POST /sources adds a text source (structural)
- [pass] POST /sources accepts a file upload (structural)
- [pass] POST /chat streams server-sent events ending in done (structural)
- [pass] GET /messages returns the saved exchange (structural)
- [pass] POST /artifacts queues generation (structural)
- [pass] GET /artifacts/{id} returns the finished document (structural)
- [pass] errors are JSON with a code (structural)
- [pass] an API key locks every route except /health (structural)

### perf-passages: Splitting a one-megabyte source into passages

performance · pass · 0.02s

- [pass] source is at least a megabyte (structural)
- [pass] splits a megabyte of text in under five seconds (structural)
- [pass] every passage is an exact slice of the source (structural)
- [pass] passages are numbered 1..n in reading order (structural)
- [pass] passages cover at least 98% of the text (the rest is blank lines) (structural)
- [pass] no passage exceeds the 900-character limit (structural)

### perf-large-notebook: Chat over a notebook with forty sources

performance · pass · 0.07s

- [pass] answer is not empty (structural)
- [pass] answer cites at least one passage (structural)
- [pass] every quote is verbatim from its source (structural)
- [pass] citation numbers run 1..n and match the markers in the text (structural)
- [pass] citations stay inside the selected sources (structural)
- [pass] notebook reports its source count (structural)
- [skipped] answer is about the ferry (quality) — needs a real model
