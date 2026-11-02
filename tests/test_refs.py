"""Reference detection: the table of cases paper2ink has to get right."""

from __future__ import annotations

import pytest

from paper2ink.refs import (
    Linker,
    find_refs,
    normalize_key,
    parse_caption_label,
)

ALL_KEYS = {
    f"fig-{n}" for n in list(range(1, 12)) + ["s1", "s2", "s3"]
} | {
    f"tab-{n}" for n in list(range(1, 6)) + ["s1"]
} | {
    f"eq-{n}" for n in range(1, 12)
} | {
    f"sec-{n}" for n in ("1", "2", "2.1", "3", "a", "b")
} | {
    f"sch-{n}" for n in ("1", "2")
} | {
    f"bib-{n}" for n in range(1, 40)
}


def keys_of(text: str, **kwargs) -> list[str]:
    kwargs.setdefault("known_keys", ALL_KEYS)
    out: list[str] = []
    for match in find_refs(text, **kwargs):
        out.extend(match.keys)
    return out


# --------------------------------------------------------------------------
# figures
# --------------------------------------------------------------------------

FIGURE_CASES = [
    ("as shown in Fig. 3 the signal", ["fig-3"]),
    ("as shown in Figure 3 the signal", ["fig-3"]),
    ("as shown in Fig 3 the signal", ["fig-3"]),
    ("as shown in FIG. 3 the signal", ["fig-3"]),
    ("see Figs. 3 and 4", ["fig-3", "fig-4"]),
    ("see Figs. 2–4", ["fig-2", "fig-4"]),
    ("see Figures 2, 3", ["fig-2", "fig-3"]),
    ("compare Fig. 3a", ["fig-3"]),
    ("compare Fig. 3(a)", ["fig-3"]),
    ("compare Fig. 3a,b", ["fig-3"]),
    ("compare Fig. 3a-c", ["fig-3"]),
    ("see Fig. S2", ["fig-s2"]),
    ("see Supplementary Fig. 2", ["fig-s2"]),
    ("see Supplementary Figure 2", ["fig-s2"]),
    ("see Extended Data Fig. 1", ["fig-s1"]),
    ("see Supporting Figure 3", ["fig-s3"]),
    ("(Fig. 5)", ["fig-5"]),
    ("Fig. 1, Fig. 2 and Fig. 3", ["fig-1", "fig-2", "fig-3"]),
]


@pytest.mark.parametrize("text,expected", FIGURE_CASES)
def test_figure_references(text, expected):
    assert keys_of(text) == expected


# --------------------------------------------------------------------------
# tables, schemes, equations, sections
# --------------------------------------------------------------------------

OTHER_CASES = [
    ("values in Table 1", ["tab-1"]),
    ("values in Tab. 2", ["tab-2"]),
    ("values in Tables 1 and 2", ["tab-1", "tab-2"]),
    ("values in Table S1", ["tab-s1"]),
    ("the route in Scheme 1", ["sch-1"]),
    ("see Eq. 5", ["eq-5"]),
    ("see Eq. (5)", ["eq-5"]),
    ("see Eqs. (2)–(4)", ["eq-2", "eq-4"]),
    ("see Equation 5", ["eq-5"]),
    ("see Equations 1 and 2", ["eq-1", "eq-2"]),
    ("discussed in Section 2.1", ["sec-2.1"]),
    ("discussed in Sec. 3", ["sec-3"]),
    ("listed in Appendix A", ["sec-a"]),
]


@pytest.mark.parametrize("text,expected", OTHER_CASES)
def test_other_references(text, expected):
    assert keys_of(text) == expected


def test_bare_equation_needs_context():
    assert keys_of("as given by (5) the flux", allow_bare_equations=True) == ["eq-5"]
    assert keys_of("the sample (5) was dried", allow_bare_equations=True) == []
    # Off by default.
    assert keys_of("as given by (5) the flux") == []


# --------------------------------------------------------------------------
# citations
# --------------------------------------------------------------------------

CITATION_CASES = [
    ("earlier work [12] showed", ["bib-12"]),
    ("earlier work [3, 5–7] showed", ["bib-3", "bib-5", "bib-7"]),
    ("earlier work [1, 2, 3] showed", ["bib-1", "bib-2", "bib-3"]),
]


@pytest.mark.parametrize("text,expected", CITATION_CASES)
def test_numeric_citations(text, expected):
    assert keys_of(text) == expected


def test_author_year_citation():
    index = {("smith", "2020"): "bib-7"}
    assert keys_of(
        "as reported earlier (Smith et al., 2020)", author_year_index=index
    ) == ["bib-7"]
    # Unknown author-year pairs stay untouched.
    assert keys_of("as reported earlier (Jones et al., 1999)", author_year_index=index) == []


# --------------------------------------------------------------------------
# negative cases
# --------------------------------------------------------------------------

NEGATIVE_CASES = [
    "the table was cleaned before use",
    "a figure of merit was defined",
    "this section describes the setup",
    "we used a flat table surface",
    "the equation balance is maintained",
    "Fig. shows nothing in particular",
    "scheme of the apparatus",
]


@pytest.mark.parametrize("text", NEGATIVE_CASES)
def test_no_false_positives(text):
    assert keys_of(text) == []


def test_unit_after_comma_is_not_a_figure_number():
    assert keys_of("as in Fig. 3, 4 nm thick layers were grown") == ["fig-3"]


def test_unknown_targets_are_dropped():
    assert keys_of("see Fig. 99", known_keys={"fig-1"}) == []


# --------------------------------------------------------------------------
# captions
# --------------------------------------------------------------------------

CAPTION_CASES = [
    ("Figure 3. Overview of the setup.", ("figure", "3", False, "Figure 3")),
    ("Fig. 3 | Overview.", ("figure", "3", False, "Figure 3")),
    ("FIGURE 10. Something.", ("figure", "10", False, "Figure 10")),
    ("Table 2: Measured values", ("table", "2", False, "Table 2")),
    ("Table S1. Supplementary values", ("table", "S1", True, "Table S1")),
    ("Supplementary Figure 2. More data", ("figure", "S2", True, "Figure S2")),
    ("Scheme 1. Synthesis route", ("scheme", "1", False, "Scheme 1")),
]


@pytest.mark.parametrize("text,expected", CAPTION_CASES)
def test_parse_caption_label(text, expected):
    parsed = parse_caption_label(text)
    assert parsed is not None
    kind, number, supp, label, _rest = parsed
    assert (kind, number, supp, label) == expected


def test_parse_caption_label_rejects_prose():
    assert parse_caption_label("The figure above shows the setup.") is None
    assert parse_caption_label("Table of contents") is None


def test_normalize_key():
    assert normalize_key("figure", "3") == "fig-3"
    assert normalize_key("figure", "2", supplementary=True) == "fig-s2"
    assert normalize_key("table", "S1") == "tab-s1"
    assert normalize_key("equation", "5") == "eq-5"


# --------------------------------------------------------------------------
# linking
# --------------------------------------------------------------------------


def make_linker(**kwargs) -> Linker:
    return Linker(
        ALL_KEYS,
        lambda key: f"figures.xhtml#{key}",
        allow_bare_equations=False,
        **kwargs,
    )


def test_linkify_emits_anchor_and_mention():
    linker = make_linker()
    html = linker.linkify("See Fig. 3 for details.", "b1")
    assert '<a class="xref" id="ref-fig-3-1" href="figures.xhtml#fig-3">Fig. 3</a>' in html
    assert html.startswith("See ")
    assert len(linker.mentions_for("fig-3")) == 1


def test_linkify_numbers_repeated_mentions():
    linker = make_linker()
    linker.linkify("See Fig. 3.", "b1")
    linker.linkify("Again Fig. 3 here.", "b2")
    mentions = linker.mentions_for("fig-3")
    assert [m.id for m in mentions] == ["ref-fig-3-1", "ref-fig-3-2"]
    assert [m.block_id for m in mentions] == ["b1", "b2"]


def test_caption_does_not_link_itself():
    linker = make_linker()
    html = linker.linkify(
        "Figure 3. Comparison with Fig. 2.", "cap", self_key="fig-3"
    )
    assert "ref-fig-3" not in html
    assert 'href="figures.xhtml#fig-2"' in html


def test_linkify_escapes_html():
    linker = make_linker()
    html = linker.linkify("a < b & c > d, see Fig. 1", "b1")
    assert "&lt; b &amp; c &gt;" in html


def test_unresolved_is_counted_not_linked():
    linker = Linker({"fig-1"}, lambda key: f"x.xhtml#{key}")
    html = linker.linkify("See Fig. 1 and Fig. 42.", "b1")
    assert "Fig. 42" in html
    assert "ref-fig-42" not in html
    assert linker.unresolved_count == 1
    assert linker.link_count == 1


# --------------------------------------------------------------------------
# ACS / Nature style ranges written with the true minus sign
# --------------------------------------------------------------------------

MINUS_SIGN_CASES = [
    ("earlier work [1\u22125] showed", ["bib-1", "bib-5"]),
    ("earlier work [41,42] showed", ["bib-41", "bib-42"]),
    ("earlier work [6\u221221] showed", ["bib-6", "bib-21"]),
    ("see Figs. 2\u22124", ["fig-2", "fig-4"]),
]


@pytest.mark.parametrize("text,expected", MINUS_SIGN_CASES)
def test_minus_sign_ranges(text, expected):
    keys = {f"bib-{n}" for n in range(1, 50)} | ALL_KEYS
    assert keys_of(text, known_keys=keys) == expected
