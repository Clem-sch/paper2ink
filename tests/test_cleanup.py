"""Clean-up stage: hyphenation, boilerplate, running heads, paragraph merging."""

from __future__ import annotations

import pytest

from paper2ink.cleanup import (
    CleanupConfig,
    clean_document,
    detect_running_heads,
    guess_year,
    is_line_number,
    join_lines,
    merge_paragraphs,
    normalize_text,
    strip_boilerplate,
)
from paper2ink.model import Block, Document


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------


def test_ligatures_are_expanded():
    assert normalize_text("the ﬁrst ﬂux") == "the first flux"


def test_soft_hyphen_and_nbsp_are_removed():
    assert normalize_text("co­operation 5 nm") == "cooperation 5 nm"


def test_whitespace_is_collapsed():
    assert normalize_text("  a   b \t c  ") == "a b c"


# --------------------------------------------------------------------------
# hyphenation
# --------------------------------------------------------------------------

HYPHEN_CASES = [
    (["transi-", "tion metal"], "transition metal"),
    (["the phase transi-", "tion was"], "the phase transition was"),
    (["state-of-the-", "art methods"], "state-of-the-art methods"),
    (["non-", "linear response"], "non-linear response"),
    (["self-", "assembly of"], "self-assembly of"),
    (["UV-", "Vis spectra"], "UV-Vis spectra"),
    (["type-", "2 diabetes"], "type-2 diabetes"),
    (["mem-", "brane permeability"], "membrane permeability"),
]


@pytest.mark.parametrize("lines,expected", HYPHEN_CASES)
def test_join_lines_dehyphenates(lines, expected):
    assert join_lines(lines) == expected


def test_join_lines_without_hyphen():
    assert join_lines(["the first", "line and", "the second"]) == (
        "the first line and the second"
    )


def test_join_lines_can_be_disabled():
    assert join_lines(["transi-", "tion"], dehyphenate=False) == "transi- tion"


# --------------------------------------------------------------------------
# boilerplate
# --------------------------------------------------------------------------


def test_boilerplate_lines_are_dropped():
    patterns = CleanupConfig().patterns()
    text = (
        "Downloaded from https://example.org on 2024-01-01\n"
        "The real sentence stays.\n"
        "© 2024 The Authors\n"
        "All rights reserved.\n"
    )
    assert strip_boilerplate(text, patterns).strip() == "The real sentence stays."


def test_custom_boilerplate_patterns():
    patterns = CleanupConfig(boilerplate=[r"^\s*SECRET.*$"]).patterns()
    assert strip_boilerplate("SECRET note\nkeep me", patterns) == "keep me"


def test_line_numbers():
    assert is_line_number(" 42 ")
    assert not is_line_number("42 samples")


# --------------------------------------------------------------------------
# running heads
# --------------------------------------------------------------------------


def test_running_heads_are_detected_despite_page_numbers():
    bodies = [
        "we observed a sharp onset",
        "the membranes were washed",
        "fluorescence recovery was slow",
        "pore formation depends on",
        "the copolymer fraction varied",
        "salt gradients were applied",
        "vesicles remained intact",
        "diffusion constants dropped",
    ]
    pages = {
        i: [f"J. Chem. Phys. 158, 1{i:03d} (2023)", bodies[i - 1]]
        for i in range(1, 9)
    }
    heads = detect_running_heads(pages)
    assert any("j. chem. phys." in h for h in heads)
    assert not any("unique body line" in h for h in heads)


def test_running_heads_need_enough_pages():
    assert detect_running_heads({1: ["Journal of Things 1"]}) == set()


# --------------------------------------------------------------------------
# paragraph merging
# --------------------------------------------------------------------------


def block(text: str, type_: str = "paragraph", **kwargs) -> Block:
    return Block(id=f"b{abs(hash(text)) % 10000}", type=type_, text=text, **kwargs)


def test_paragraph_broken_across_columns_is_merged():
    blocks = [
        block("The membrane was prepared by mixing lipids and"),
        block("block copolymers in chloroform."),
    ]
    merged = merge_paragraphs(blocks)
    assert len(merged) == 1
    assert merged[0].text == (
        "The membrane was prepared by mixing lipids and block copolymers in "
        "chloroform."
    )


def test_complete_sentences_are_not_merged():
    blocks = [block("This paragraph is complete."), block("so is this one.")]
    assert len(merge_paragraphs(blocks)) == 2


def test_new_sentence_starting_with_capital_is_not_merged():
    blocks = [block("A dangling clause without a full stop"), block("However we did")]
    assert len(merge_paragraphs(blocks)) == 2


def test_headings_are_never_merged():
    blocks = [block("Results", "heading", level=1), block("we observed that")]
    assert len(merge_paragraphs(blocks)) == 2


def test_hyphenation_survives_the_merge():
    blocks = [block("the phase transi-"), block("tion was observed")]
    merged = merge_paragraphs(blocks)
    assert merged[0].text == "the phase transition was observed"


# --------------------------------------------------------------------------
# document level
# --------------------------------------------------------------------------


def test_clean_document_drops_empty_and_boilerplate_blocks():
    doc = Document()
    doc.blocks = [
        block("Introduction", "heading", level=1),
        block("Downloaded from https://example.org"),
        block("   "),
        block("7"),
        block("Real content here."),
    ]
    cleaned = clean_document(doc)
    texts = [b.text for b in cleaned.blocks]
    assert texts == ["Introduction", "Real content here."]


# --------------------------------------------------------------------------
# publication year
# --------------------------------------------------------------------------


def test_the_copyright_year_wins_over_a_download_stamp():
    """Publishers stamp the download date on the PDF: a 2019 paper fetched
    today must not come out as 2025."""
    page = (
        "Downloaded via UNIV HEIDELBERG on May 6, 2025 at 16:37:15 (UTC).\n"
        "J. Phys. Chem. A 2019, 123, 5579-5589\n"
        "\u00a9 2019 American Chemical Society\n"
    )
    assert guess_year(page) == "2019"


def test_without_a_copyright_line_the_most_common_year_wins():
    page = (
        "Downloaded on November 3, 2025.\n"
        "Received 12 March 2015. Accepted 4 May 2015.\n"
        "J. Chem. Theory Comput. 2015, 11, 1525\n"
    )
    assert guess_year(page) == "2015"


def test_equally_common_years_pick_the_most_recent():
    assert guess_year("cites 2001 and was published 2019") == "2019"


def test_no_year_at_all():
    assert guess_year("a page with no dates on it") is None


def test_implausible_years_are_ignored():
    assert guess_year("figure 1850 and 2099 and 2019") == "2019"
