"""Tests for overtone.grounding: passage splitting, the grounding block, and citation rewriting."""

from __future__ import annotations

import random

import pytest

from overtone import grounding
from overtone.errors import OvertoneError
from overtone.grounding import (
    CitationStream,
    Passage,
    SourceDoc,
    render_sources,
    split_passages,
    strip_markers,
    usable_docs,
)

MAX_PASSAGE_CHARS = 1_000


# ───────────────────────────── helpers ─────────────────────────────


def _fixed_texts() -> dict[str, str]:
    return {
        "short_note": "Remember to call the supplier.",
        "lines_no_blank": "Line one\nLine two\nLine three",
        "padded": "\n\n\n   \n  Leading and trailing blank lines.  \n\n\n",
        "many_headings": "\n".join(f"## Heading {i}" for i in range(300)),
        "many_list_items": "\n\n".join(f"- item {i}" for i in range(400)),
        "long_sentences": ("The committee reviewed the budget. Costs rose by 4 percent! Was that expected? " * 80)[:5000],
        "long_no_punctuation_no_spaces": "x" * 5000,
        "long_no_punctuation": ("alpha beta gamma delta " * 300)[:5000],
        "crlf": "First paragraph here.\r\n\r\nSecond paragraph\r\ncontinues here.\r\n\r\n\r\nThird.\r\n",
        "unicode": "Café naïve — 日本語のテキスト。 🙂🚀 𝔘𝔫𝔦𝔠𝔬𝔡𝔢 text.\n\n" * 40,
        "whitespace_only": " \n\t\n   \n",
        "empty": "",
    }


def _random_text(rng: random.Random) -> str:
    alphabet = "abcdefghij klmnop. ,!?\n\r\t 日🙂"
    parts: list[str] = []
    for _ in range(rng.randint(1, 30)):
        length = rng.choice([0, 3, 40, 300, 950])
        parts.append("".join(rng.choice(alphabet) for _ in range(length)))
        parts.append(rng.choice(["\n", "\n\n", "\r\n\r\n", "\n  \n", " "]))
    return "".join(parts)


def _check_invariants(text: str, passages: list[Passage]) -> None:
    covered: set[int] = set()
    previous_end = 0
    for p in passages:
        assert p.text == text[p.start : p.end], "passage text must be the exact source slice"
        assert p.text.strip(), "no passage may be empty or whitespace-only"
        assert p.text == p.text.strip(), "passages are trimmed"
        assert len(p.text) <= MAX_PASSAGE_CHARS
        assert p.start >= previous_end, "passages are ordered and non-overlapping"
        previous_end = p.end
        covered.update(range(p.start, p.end))
    non_whitespace = {i for i, c in enumerate(text) if not c.isspace()}
    missing = non_whitespace - covered
    assert not missing, f"{len(missing)} non-whitespace characters are not in any passage"


def _passage(number: int, text: str = "Quoted passage text.", *, source_id: str = "src-1", title: str = "Source One", start: int = 0) -> Passage:
    return Passage(
        number=number,
        source_id=source_id,
        source_title=title,
        start=start,
        end=start + len(text),
        text=text,
    )


def _run(chunks: list[str], passages: list[Passage]) -> tuple[str, list[dict], CitationStream]:
    stream = CitationStream(passages)
    events: list[dict] = []
    for chunk in chunks:
        events.extend(stream.feed(chunk))
    events.extend(stream.flush())
    text = "".join(e["delta"] for e in events if e["type"] == "text")
    citations = [e["citation"] for e in events if e["type"] == "citation"]
    return text, citations, stream


# ───────────────────────────── split_passages ─────────────────────────────


@pytest.mark.parametrize("name", sorted(_fixed_texts()))
def test_split_passages_invariants_for_fixed_inputs(name: str) -> None:
    text = _fixed_texts()[name]
    passages = split_passages([SourceDoc("s", "Doc", text)])
    _check_invariants(text, passages)


@pytest.mark.parametrize("seed", range(40))
def test_split_passages_invariants_for_random_inputs(seed: int) -> None:
    text = _random_text(random.Random(seed))
    passages = split_passages([SourceDoc("s", "Doc", text)])
    _check_invariants(text, passages)


def test_passages_are_numbered_one_based_and_continuous_across_sources() -> None:
    a = SourceDoc("a", "A", "\n\n".join(["First source paragraph. " * 20, "Second source paragraph. " * 20]))
    b = SourceDoc("b", "B", "\n\n".join(["Third. " * 60, "Fourth. " * 60, "Fifth. " * 60]))
    passages = split_passages([a, b])
    assert [p.number for p in passages] == list(range(1, len(passages) + 1))
    assert {p.source_id for p in passages} == {"a", "b"}
    assert [p.source_id for p in passages] == sorted(p.source_id for p in passages), "reading order kept"
    for doc in (a, b):
        _check_invariants(doc.text, [p for p in passages if p.source_id == doc.id])


def test_tiny_paragraphs_are_merged_rather_than_one_passage_each() -> None:
    passages = split_passages([SourceDoc("s", "Doc", _fixed_texts()["many_headings"])])
    assert 0 < len(passages) < 100


def test_long_paragraph_is_split_into_passages_of_sane_size() -> None:
    text = _fixed_texts()["long_sentences"]
    passages = split_passages([SourceDoc("s", "Doc", text)])
    assert len(passages) >= 5
    assert max(len(p.text) for p in passages) <= MAX_PASSAGE_CHARS


def test_run_without_spaces_or_punctuation_is_still_split() -> None:
    text = _fixed_texts()["long_no_punctuation_no_spaces"]
    passages = split_passages([SourceDoc("s", "Doc", text)])
    assert len(passages) >= 5
    assert "".join(p.text for p in passages) == text


def test_crlf_line_endings_split_on_blank_lines() -> None:
    first = "First paragraph, long enough to stand alone. " * 8
    second = "Second paragraph\r\nwrapped over two lines, also long enough. " * 8
    third = "Third paragraph, again long enough to be its own passage. " * 8
    text = f"{first.strip()}\r\n\r\n{second.strip()}\r\n\r\n\r\n{third.strip()}\r\n"
    passages = split_passages([SourceDoc("s", "Doc", text)])
    assert [p.text for p in passages] == [first.strip(), second.strip(), third.strip()]


def test_short_crlf_paragraphs_are_merged_into_one_passage() -> None:
    text = _fixed_texts()["crlf"]
    passages = split_passages([SourceDoc("s", "Doc", text)])
    assert len(passages) == 1
    assert passages[0].text == text.strip()


def test_unicode_text_round_trips_exactly() -> None:
    text = _fixed_texts()["unicode"]
    passages = split_passages([SourceDoc("s", "Doc", text)])
    assert "🙂" in "".join(p.text for p in passages)
    _check_invariants(text, passages)


@pytest.mark.parametrize("name", ["empty", "whitespace_only"])
def test_blank_source_yields_no_passages(name: str) -> None:
    assert split_passages([SourceDoc("s", "Doc", _fixed_texts()[name])]) == []


# ───────────────────────────── usable_docs ─────────────────────────────


def test_usable_docs_drops_blank_sources() -> None:
    docs = [SourceDoc("a", "A", ""), SourceDoc("b", "B", "   \n"), SourceDoc("c", "C", "kept")]
    assert [d.id for d in usable_docs(docs)] == ["c"]


def test_usable_docs_with_nothing_readable_is_a_400() -> None:
    with pytest.raises(OvertoneError) as info:
        usable_docs([SourceDoc("a", "A", ""), SourceDoc("b", "B", " ")])
    assert (info.value.status, info.value.code) == (400, "no_sources")


def test_usable_docs_over_the_limit_is_a_413(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(grounding, "MAX_SOURCE_CHARS", 100)
    with pytest.raises(OvertoneError) as info:
        usable_docs([SourceDoc("a", "A", "x" * 60), SourceDoc("b", "B", "y" * 41)])
    assert (info.value.status, info.value.code) == (413, "sources_too_large")


def test_usable_docs_exactly_at_the_limit_is_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(grounding, "MAX_SOURCE_CHARS", 100)
    docs = [SourceDoc("a", "A", "x" * 60), SourceDoc("b", "B", "y" * 40)]
    assert len(usable_docs(docs)) == 2


# ───────────────────────────── render_sources ─────────────────────────────


def test_render_sources_groups_passages_by_source_and_escapes_titles() -> None:
    a = SourceDoc("a", 'Report "Q3" <draft> & more', "First point about revenue.\n\nSecond point about costs.")
    b = SourceDoc("b", "Memo", "A memo paragraph that stands alone.")
    docs = usable_docs([a, b])
    passages = split_passages(docs)
    block = render_sources(docs, passages)

    assert block.startswith("<sources>\n") and block.endswith("\n</sources>")
    assert block.count('<source title="') == 2
    assert 'title="Report &quot;Q3&quot; &lt;draft&gt; &amp; more"' in block
    memo_header = block.index('<source title="Memo">')
    for p in passages:
        line = f"[{p.number}] " + " ".join(p.text.split())
        assert line in block
        if p.source_id == "b":
            assert block.index(line) > memo_header
        else:
            assert block.index(line) < memo_header


def test_render_sources_puts_each_passage_on_one_line() -> None:
    doc = SourceDoc("a", "A", "Line one\nline two\n\nSecond paragraph.")
    passages = split_passages([doc])
    block = render_sources([doc], passages)
    for line in block.splitlines():
        if line.startswith("[") and "] " in line:
            assert "\n" not in line


# ───────────────────────────── CitationStream ─────────────────────────────


def test_marker_is_rewritten_and_emits_one_citation_with_the_passage() -> None:
    passage = _passage(12, "Revenue grew eight percent.", source_id="s9", title="Annual report", start=40)
    text, citations, stream = _run(["Revenue grew eight percent [12]."], [passage])
    assert text == "Revenue grew eight percent [1]."
    assert citations == [
        {
            "index": 1,
            "sourceId": "s9",
            "sourceTitle": "Annual report",
            "quote": "Revenue grew eight percent.",
            "startChar": 40,
            "endChar": 40 + len("Revenue grew eight percent."),
        }
    ]
    assert [c.index for c in stream.citations] == [1]


def test_repeated_passage_reuses_its_index_without_a_second_citation_event() -> None:
    passages = [_passage(12, "Twelve."), _passage(7, "Seven.")]
    text, citations, _ = _run(["A [12] and [12] again [7] and [12]"], passages)
    assert text == "A [1] and [1] again [2] and [1]"
    assert [c["quote"] for c in citations] == ["Twelve.", "Seven."]


def test_indices_follow_order_of_first_appearance() -> None:
    passages = [_passage(3, "Three."), _passage(7, "Seven.")]
    text, citations, _ = _run(["[7] then [3] then [7]"], passages)
    assert text == "[1] then [2] then [1]"
    assert [c["quote"] for c in citations] == ["Seven.", "Three."]


@pytest.mark.parametrize("marker", ["[3][7]", "[3, 7]", "[3,7]", "[3 7]"])
def test_multiple_numbers_in_one_marker(marker: str) -> None:
    passages = [_passage(3, "Three."), _passage(7, "Seven.")]
    text, citations, _ = _run([marker], passages)
    assert text == "[1][2]"
    assert [c["index"] for c in citations] == [1, 2]


def test_unknown_numbers_are_dropped_silently() -> None:
    passages = [_passage(3, "Three.")]
    text, citations, _ = _run(["a [99] b [3, 99] c"], passages)
    assert text == "a  b [1] c"
    assert len(citations) == 1


@pytest.mark.parametrize(
    "bracketed",
    ["[link](http://x.test)", "[TODO]", "[]", "[a1]", "[ ]", "[1a]", "[-1]", "[1.5]", "[note: see below]"],
)
def test_non_numeric_brackets_pass_through_unchanged(bracketed: str) -> None:
    text, citations, _ = _run([f"Read {bracketed} now."], [_passage(1)])
    assert text == f"Read {bracketed} now."
    assert citations == []


def test_double_bracket_keeps_outer_brackets() -> None:
    text, citations, _ = _run(["[[5]]"], [_passage(5, "Five.")])
    assert text == "[[1]]"
    assert len(citations) == 1


def test_unclosed_bracket_at_end_of_stream_is_released_by_flush() -> None:
    text, citations, stream = _run(["ends with [12"], [_passage(12)])
    assert text == "ends with [12"
    assert citations == []
    assert stream.flush() == []


def test_spaces_inside_a_numeric_bracket_are_tolerated() -> None:
    text, citations, _ = _run(["see [ 5 ] here"], [_passage(5, "Five.")])
    assert text == "see [1] here"
    assert len(citations) == 1


def test_very_long_bracket_body_passes_through_without_error() -> None:
    body = ", ".join(str(i) for i in range(1, 80))
    text, citations, _ = _run([f"list [{body}] done"], [_passage(i) for i in range(1, 80)])
    assert text == f"list [{body}] done"
    assert citations == []


def test_superscript_digits_in_brackets_do_not_crash_the_stream() -> None:
    text, citations, _ = _run(["x [²] y"], [_passage(2)])
    assert text == "x [²] y"
    assert citations == []


def test_feeding_text_split_at_every_position_gives_the_same_result() -> None:
    passages = [_passage(3, "Three."), _passage(7, "Seven."), _passage(12, "Twelve.")]
    whole = (
        "Growth [12] was strong[3][12]; costs [3, 7] fell [99]. See [link](http://x.test) and [TODO] and []. "
        "Odd [a1] and [[7]] then [ 3 ] and tail [5"
    )
    expected_text, expected_citations, _ = _run([whole], passages)
    for i in range(len(whole) + 1):
        text, citations, _ = _run([whole[:i], whole[i:]], passages)
        assert text == expected_text, f"split at {i}"
        assert citations == expected_citations, f"split at {i}"
    char_text, char_citations, _ = _run(list(whole), passages)
    assert char_text == expected_text
    assert char_citations == expected_citations


def test_character_by_character_matches_whole_for_a_long_answer() -> None:
    passages = [_passage(n, f"Passage {n}.") for n in range(1, 6)]
    answer = " ".join(f"Point {n} is true [{n}][{(n % 5) + 1}]." for n in range(1, 6)) + " Done [9]."
    expected, expected_citations, _ = _run([answer], passages)
    actual, actual_citations, _ = _run(list(answer), passages)
    assert actual == expected
    assert actual_citations == expected_citations
    assert [c["index"] for c in expected_citations] == [1, 2, 3, 4, 5]


def test_citations_on_the_stream_match_the_emitted_events() -> None:
    passages = [_passage(1, "One."), _passage(2, "Two.")]
    _, events_citations, stream = _run(["[2] then [1]"], passages)
    assert [c["index"] for c in events_citations] == [c.index for c in stream.citations] == [1, 2]


def test_feed_emits_text_before_a_citation_so_order_is_preserved() -> None:
    stream = CitationStream([_passage(1, "One.")])
    events = stream.feed("Hello [1] world")
    kinds = [e["type"] for e in events]
    assert kinds == ["text", "citation", "text"]
    assert events[0]["delta"] == "Hello "
    assert events[2]["delta"] == "[1] world"


# ───────────────────────────── strip_markers ─────────────────────────────


def test_strip_markers_removes_markers_and_the_space_before_them() -> None:
    assert strip_markers("Revenue grew [12]. Costs [3][7] fell.") == "Revenue grew. Costs fell."
    assert strip_markers("Done [1]") == "Done"
    assert strip_markers("[212] leading") == " leading"


def test_strip_markers_leaves_ordinary_brackets_alone() -> None:
    text = "See [link](http://x.test) and [TODO] and [] and [a1]."
    assert strip_markers(text) == text


def test_strip_markers_removes_comma_separated_markers() -> None:
    assert strip_markers("Answer [3, 7].") == "Answer."
