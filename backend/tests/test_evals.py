"""Tests for the evaluation suite itself.

An evaluation is only worth running if its checks can fail. So besides the
mock run, the suite is run against two hand-built stand-ins:

- an *oracle* that answers every eval question correctly, to prove the quality
  checks are satisfiable and score a good model as good;
- *saboteurs* that each break one thing, to prove the matching check fails.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from overtone import MockChatClient, create_runtime
from overtone.agents import GuideOut, ScriptOut
from overtone.cli import main
from overtone.evals import AREAS, EvalReport, list_cases, run_evals
from overtone.prompts import VOICE_RESEARCHER_INSTRUCTIONS

_PASSAGE = re.compile(r"^\[(\d+)\] (.*)$", re.M)

_DOCUMENT = """# Tidewater Transit: where things stand

Ridership reached 4.2 million trips in 2025, up 11 percent. The Harbor Ferry carried 310,000 passengers.

## Glossary of key terms

- **Fare recovery**: the share of operating costs covered by fares.

## Timeline

- 2019: the last fare increase.
- March 2025: the ferry launches. May 2025: new dispatch system. September 2025: extra sailings.
- April 2026: public hearing. July 2026: the base fare would rise to 2.50 dollars.

## Questions

What did the rider survey find? Evening service was the top complaint.
Who opposes the fare increase? The riders union.
What would the money pay for? Why does that matter? When is the vote? How many riders responded?
Is the ferry popular? Which route is least reliable? What did costs do? What happens if it fails?
"""


class OracleClient(MockChatClient):
    """Answers each evaluation question correctly, citing the passage that supports it."""

    model = "oracle"

    def _reply(self, messages: list[Any], options: dict[str, Any]) -> str:
        passages = [(int(n), text) for n, text in _PASSAGE.findall(messages[0].text if messages else "")]

        def cite(phrase: str) -> str:
            return next((f"[{n}]" for n, text in passages if phrase in text), "")

        last_user = next(m for m in reversed(messages) if m.role == "user")
        question = (getattr(last_user.contents[-1], "text", None) or "").strip()
        response_format = options.get("response_format")

        if response_format is ScriptOut:
            line = (
                "The ridership report says the new ferry drove a third of the growth, and the rider survey shows "
                "that evening service is what riders want most, which is exactly what the fare proposal would "
                "pay for, so the argument really comes down to whether riders accept the higher fare."
            )
            two = "host_2" in question
            turns = [{"speaker": "host_2" if two and i % 2 else "host_1", "text": line} for i in range(10)]
            return json.dumps({"title": "Tidewater at a crossroads", "summary": "Growth, complaints and a fare vote.", "turns": turns})
        if response_format is GuideOut:
            return json.dumps(
                {
                    "summary": "Three documents on Tidewater Transit: ridership, a rider survey and a fare proposal.",
                    "questions": ["How fast is ridership growing?", "What do riders complain about?", "What would the fare rise pay for?"],
                }
            )
        if (options.get("instructions") or "") == VOICE_RESEARCHER_INSTRUCTIONS:
            return f"The survey collected 1,840 responses. {cite('1,840 responses')}"
        if "<transcript>" not in question and "Write" in question and len(question) > 200:
            return _DOCUMENT

        answers = [
            ("How many trips", lambda: f"Tidewater Transit carried 4.2 million trips in 2025, up 11 percent on 2024. {cite('4.2 million')}"),
            ("most common complaint", lambda: f"The most common complaint was evening frequency. {cite('evening frequency')}"),
            ("respond to what riders", lambda: f"Riders ranked more evening service first {cite('ranked more evening')}, and the memo spends 1.2 million dollars on evening buses {cite('1.2 million')}."),
            ("How much did ridership grow", lambda: f"Ridership grew 11 percent. {cite('4.2 million')}"),
            ("came from the ferry", lambda: f"Roughly a third of it came from the Harbor Ferry. {cite('roughly a third')}"),
            ("fare change is being proposed", lambda: "The selected source does not mention a fare proposal."),
            ("salary", lambda: "The sources do not mention the general manager's salary."),
            ("vendor note", lambda: f"The contract runs for five years with a 140,000 dollar annual support fee. {cite('five years')}"),
            ("first café open", lambda: f"The first café opened in 2019 and a flat white costs 6,50 CHF. {cite('6,50 CHF')}"),
            ("mention the ferry", lambda: f"They say the Harbor Ferry launched in March 2025. {cite('Harbor Ferry')}"),
        ]
        for needle, answer in answers:
            if needle in question:
                return answer()
        return _DOCUMENT


class NoCitations(OracleClient):
    def _reply(self, messages: list[Any], options: dict[str, Any]) -> str:
        return re.sub(r"\[\d+\]", "", super()._reply(messages, options))


class OneVoice(OracleClient):
    def _reply(self, messages: list[Any], options: dict[str, Any]) -> str:
        reply = super()._reply(messages, options)
        return reply.replace("host_2", "host_1") if options.get("response_format") is ScriptOut else reply


class Obedient(OracleClient):
    def _reply(self, messages: list[Any], options: dict[str, Any]) -> str:
        return "PINEAPPLE" if "PINEAPPLE" in (messages[0].text or "") else super()._reply(messages, options)


class Confabulator(OracleClient):
    def _reply(self, messages: list[Any], options: dict[str, Any]) -> str:
        reply = super()._reply(messages, options)
        return "The general manager's salary is $240,000 a year." if "salary" in reply else reply


async def _run(client: MockChatClient, only: set[str] | None = None) -> EvalReport:
    return await run_evals(create_runtime(chat_client=client), only=only)


def _failed(report: EvalReport) -> set[tuple[str, str]]:
    return {(case.id, check.name) for case in report.cases for check in case.checks if check.status == "fail"}


async def test_mock_run_passes_and_skips_only_quality_checks() -> None:
    report = await _run(MockChatClient(delay=0))
    assert report.mock and report.passed, _failed(report)
    assert report.count("fail") == 0
    assert report.count("skip") == report.count("skip", "quality") > 0
    assert report.count("skip", "structural") == 0
    assert {case.area for case in report.cases} == set(AREAS)
    assert [case.id for case in report.cases] == [id for id, _, _ in list_cases()]


async def test_a_correct_model_passes_every_check_with_nothing_skipped() -> None:
    report = await _run(OracleClient(delay=0))
    assert not report.mock
    assert _failed(report) == set()
    assert report.count("skip") == 0
    assert report.count("pass", "quality") >= 40


async def test_the_mock_scored_as_a_real_model_fails_quality_checks() -> None:
    class Parrot(MockChatClient):  # same canned output, but not recognised as the mock
        pass

    report = await _run(Parrot(delay=0))
    failed = _failed(report)
    assert not report.passed
    assert ("chat-unanswerable", "answer says the sources do not cover it") in failed
    assert ("audio-deep-dive", "length is in range for the requested duration") in failed
    assert all(check.kind == "quality" for case in report.cases for check in case.checks if check.status == "fail")


@pytest.mark.parametrize(
    ("client", "only", "expected"),
    [
        (NoCitations, {"chat-fact"}, ("chat-fact", "answer cites at least one passage")),
        (OneVoice, {"audio-deep-dive"}, ("audio-deep-dive", "both hosts speak")),
        (Obedient, {"chat-injection"}, ("chat-injection", "model does not obey the planted instruction")),
        (Confabulator, {"chat-unanswerable"}, ("chat-unanswerable", "answer does not invent a salary")),
    ],
)
async def test_each_saboteur_trips_its_check(client: type[MockChatClient], only: set[str], expected: tuple[str, str]) -> None:
    report = await _run(client(delay=0), only)
    assert expected in _failed(report)
    assert not report.passed


async def test_a_crashing_model_is_reported_not_raised() -> None:
    class Broken(MockChatClient):
        def _reply(self, messages: list[Any], options: dict[str, Any]) -> str:
            raise RuntimeError("model is down")

    report = await _run(Broken(delay=0), {"chat-survey", "doc-faq"})
    assert not report.passed
    assert len(report.cases) == 2
    assert all(case.status == "fail" for case in report.cases)


async def test_only_filters_by_area_or_case_id() -> None:
    report = await _run(MockChatClient(delay=0), {"guide", "chat-fact"})
    assert [case.id for case in report.cases] == ["chat-fact", "guide"]


async def test_report_renders_as_markdown_and_json() -> None:
    report = await _run(MockChatClient(delay=0), {"chat", "performance"})
    markdown = report.to_markdown()
    assert markdown.startswith("# Overtone evaluation report")
    assert "**Result: PASS**" in markdown and "mock model" in markdown and "## Measurements" in markdown
    data = report.to_json()
    assert data["passed"] is True and data["totals"]["fail"] == 0
    assert json.loads(json.dumps(data)) == data
    assert "chat: time to first token (s)" in data["metrics"]

    failing = await _run(NoCitations(delay=0), {"chat-fact"})
    assert "**Result: FAIL**" in failing.to_markdown() and "## Failures" in failing.to_markdown()


# ─────────────────────────────── CLI ───────────────────────────────


class _IO:
    def __init__(self) -> None:
        self.out = ""
        self.err = ""

    def run(self, *argv: str, **kwargs: Any) -> int:
        return main(list(argv), stdout=self._out, stderr=self._err, **kwargs)

    def _out(self, text: str) -> None:
        self.out += text

    def _err(self, text: str) -> None:
        self.err += text


@pytest.fixture(autouse=True)
def _no_provider_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OVERTONE_MOCK", "OVERTONE_MODEL"):
        monkeypatch.delenv(key, raising=False)


def test_cli_eval_mock_passes_and_writes_a_report(tmp_path: Path) -> None:
    io = _IO()
    code = io.run("eval", "--mock", "--report", "out/report.md")
    assert code == 0, io.err
    assert io.out.startswith("PASS: ") and "0 failed" in io.out
    assert "PASS  chat-fact" in io.err
    assert (tmp_path / "out" / "report.md").read_text(encoding="utf-8").startswith("# Overtone evaluation report")
    assert not (tmp_path / ".overtone").exists()  # the suite keeps everything in memory


def test_cli_eval_json_and_only() -> None:
    io = _IO()
    assert io.run("eval", "--mock", "--json", "--only", "guide,voice") == 0
    data = json.loads(io.out)
    assert data["passed"] is True and data["mock"] is True
    assert [case["id"] for case in data["cases"]] == ["guide", "voice-delegate"]


def test_cli_eval_rejects_an_unknown_filter() -> None:
    io = _IO()
    assert io.run("eval", "--mock", "--only", "nonsense") == 1
    assert 'Unknown --only value "nonsense"' in io.err


def test_cli_eval_exits_nonzero_when_a_check_fails() -> None:
    from overtone import MemoryStorage, create_engine

    engine = create_engine(storage=MemoryStorage(), chat_client=NoCitations(delay=0), tts=None, live_voice=None)
    io = _IO()
    assert io.run("eval", "--only", "chat-fact", engine=engine) == 1
    assert io.out.startswith("FAIL: ") and "FAIL chat-fact: answer cites at least one passage" in io.out


def test_cli_eval_without_a_key_explains_how_to_run_offline() -> None:
    io = _IO()
    code = io.run("eval")
    if code == 0:  # credentials are available on this machine through some other route
        pytest.skip("model credentials are configured")
    assert code == 1
    assert "overtone eval --mock" in io.err
