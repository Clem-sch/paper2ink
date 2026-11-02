"""Span handling in the PyMuPDF backend."""

from __future__ import annotations

import pytest

from paper2ink.parsers.pymupdf_parser import _join_spans

SUP = 1
PLAIN = 4


def span(text: str, flags: int = PLAIN) -> dict:
    return {"text": text, "flags": flags, "size": 10.0, "font": "Times"}


def sup(text: str) -> dict:
    return span(text, SUP)


# --------------------------------------------------------------------------
# superscript citations become bracketed ones
# --------------------------------------------------------------------------


def test_single_superscript_citation():
    assert _join_spans([span("co-workers,"), sup("40")]) == "co-workers,[40]"


def test_superscript_citation_list():
    line = [span("method."), sup("41"), sup(","), sup("42"), sup(" "), span("Next")]
    assert _join_spans(line) == "method.[41,42] Next"


def test_superscript_range_with_a_minus_sign():
    line = [span("methods,"), sup("1"), sup("\u2212"), sup("5"), sup(" "), span("and")]
    assert _join_spans(line) == "methods,[1\u22125] and"


def test_several_runs_in_one_line():
    line = [
        span("Werner"), sup("22"), sup("\u2212"), sup("33"), sup(" "),
        span("and "), span("H\u00e4ttig"), sup("34"), sup("\u2212"), sup("40"),
    ]
    assert _join_spans(line) == "Werner[22\u221233] and H\u00e4ttig[34\u221240]"


def test_trailing_space_stays_outside_the_brackets():
    assert _join_spans([span("x."), sup("7"), sup("  ")]) == "x.[7]  "


# --------------------------------------------------------------------------
# things that must not be rewritten
# --------------------------------------------------------------------------


def test_already_bracketed_superscripts_are_left_alone():
    """Wiley sets its markers as superscript "[73]" already."""
    assert _join_spans([span("literature."), sup("[73]")]) == "literature.[73]"


def test_a_footnote_dagger_is_not_a_citation():
    assert _join_spans([span("Author"), sup("\u2020")]) == "Author\u2020"


def test_a_superscript_letter_is_not_a_citation():
    assert _join_spans([span("Table"), sup("a")]) == "Tablea"


def test_an_exponent_is_not_rewritten():
    """"10\u207b\u2079" style exponents start with a sign, not a digit."""
    assert _join_spans([span("10"), sup("\u22129")]) == "10\u22129"


def test_a_line_without_superscripts_is_unchanged():
    line = [span("plain "), span("text "), span("only")]
    assert _join_spans(line) == "plain text only"


def test_empty_span_list():
    assert _join_spans([]) == ""
