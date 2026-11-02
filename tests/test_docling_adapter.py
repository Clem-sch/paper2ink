"""The post-processing the Docling backend does on Docling's output.

Docling itself is not run here: it needs a multi-gigabyte ML stack and a
model download.  What is tested is the adapter logic that turns its output
into the intermediate model, which is where the bugs were.
"""

from __future__ import annotations

import pytest

pytest.importorskip("docling", reason="docling backend is an optional extra")

from paper2ink.parsers.docling_parser import (  # noqa: E402
    DoclingParser,
    _CaptionGroup,
    _Item,
    _authors_from_blocks,
    _extract_abstract,
    _extract_references,
    _heading_level,
    _nearest_graphic,
)
from paper2ink.model import BBox, Block, Document  # noqa: E402


def item(label: str, text: str = "", page: int = 0, y0: float = 0.0,
         y1: float = 10.0) -> _Item:
    return _Item(label=label, text=text, bbox=BBox(page, 0, y0, 500, y1))


def block(id_: str, type_: str, text: str, page: int = 0) -> Block:
    return Block(id=id_, type=type_, text=text, page=page)


# --------------------------------------------------------------------------
# pairing captions with graphics
# --------------------------------------------------------------------------


def group(kind="figure", number="1", page=0, y0=300.0, y1=320.0) -> _CaptionGroup:
    return _CaptionGroup(
        kind=kind, number=number, supplementary=False, label=f"Figure {number}",
        text="a caption", bbox=BBox(page, 0, y0, 500, y1),
    )


def test_graphic_above_the_caption_is_matched():
    above = item("picture", page=0, y0=100, y1=290)
    assert _nearest_graphic([above], group()) is above


def test_graphic_below_the_caption_is_matched():
    below = item("table", page=0, y0=330, y1=500)
    assert _nearest_graphic([below], group(kind="table")) is below


def test_the_closest_graphic_wins():
    far = item("picture", page=0, y0=10, y1=60)
    near = item("picture", page=0, y0=150, y1=290)
    assert _nearest_graphic([far, near], group()) is near


def test_graphics_on_another_page_are_ignored():
    other = item("picture", page=3, y0=100, y1=290)
    assert _nearest_graphic([other], group()) is None


def test_distant_graphics_are_ignored():
    distant = item("picture", page=0, y0=0, y1=5)
    assert _nearest_graphic([distant], group(y0=700, y1=720)) is None


def test_already_used_graphics_are_not_matched_twice():
    picture = item("picture", page=0, y0=100, y1=290)
    picture.used = True
    assert _nearest_graphic([picture], group()) is None


def test_uncaptioned_graphics_do_not_become_figures():
    """Journal logos come back as pictures and must not be numbered."""
    parser = DoclingParser()
    doc = Document()
    logo = item("picture", page=0, y0=10, y1=40)
    parser._build_assets(doc, [logo], [], dl_doc=None)
    assert doc.assets == []


def test_caption_and_picture_become_one_asset():
    parser = DoclingParser()
    doc = Document()
    picture = item("picture", page=2, y0=100, y1=400)
    caption = group(number="2", page=2, y0=410, y1=430)
    parser._build_assets(doc, [picture], [caption], dl_doc=None)
    assert len(doc.assets) == 1
    asset = doc.assets[0]
    assert asset.key == "fig-2"
    assert asset.caption == "a caption"
    assert asset.bboxes[0].y0 == 100


def test_caption_without_a_graphic_is_reported():
    parser = DoclingParser()
    doc = Document()
    parser._build_assets(doc, [], [group()], dl_doc=None)
    assert doc.assets == []
    assert any("no graphic" in w for w in doc.warnings)


# --------------------------------------------------------------------------
# split captions
# --------------------------------------------------------------------------


def test_split_caption_is_glued_back_together():
    """Docling emits "FIGURE 2" and its text as two separate items."""
    parser = DoclingParser()
    items = [
        item("caption", "FIGURE 2", page=4, y0=400, y1=410),
        item("caption", "Whole-cell Martini model of JCVI-syn3A.", page=4,
             y0=412, y1=440),
    ]
    groups = parser._group_captions(items)
    assert len(groups) == 1
    assert groups[0].number == "2"
    assert groups[0].text.startswith("Whole-cell Martini")
    assert all(entry.used for entry in items)


def test_caption_label_misfiled_as_a_heading_still_counts():
    parser = DoclingParser()
    items = [
        item("section_header", "FIGURE 1", page=2, y0=400, y1=410),
        item("text", "Integrative modeling workflow for whole-cell models.",
             page=2, y0=412, y1=440),
    ]
    groups = parser._group_captions(items)
    assert len(groups) == 1 and groups[0].number == "1"


@pytest.mark.parametrize(
    "prose",
    [
        "Figure 3 shows the result of the simulation run.",
        "Table 2 lists every measured value for the samples.",
        "Figure 1 and Figure 2 together make the point clearly.",
    ],
)
def test_a_figure_mention_in_prose_is_not_a_caption(prose):
    parser = DoclingParser()
    items = [item("text", prose, page=1, y0=100, y1=120)]
    assert parser._group_captions(items) == []


def test_a_real_caption_in_a_text_item_is_still_found():
    parser = DoclingParser()
    items = [
        item("text", "Figure 4. Overview of the measurement setup.", page=1,
             y0=400, y1=430)
    ]
    groups = parser._group_captions(items)
    assert len(groups) == 1 and groups[0].number == "4"


def test_complete_caption_needs_no_continuation():
    parser = DoclingParser()
    items = [
        item("caption", "Figure 5. The complete caption sits in one item.",
             page=1, y0=400, y1=430),
        item("text", "Unrelated body text follows here.", page=1, y0=440, y1=470),
    ]
    groups = parser._group_captions(items)
    assert len(groups) == 1
    assert groups[0].text == "The complete caption sits in one item."
    assert not items[1].used


# --------------------------------------------------------------------------
# headings
# --------------------------------------------------------------------------


def test_numbered_headings_get_their_real_depth():
    assert _heading_level(item("section_header", "2. Results")) == 1
    assert _heading_level(item("section_header", "2.1. Stability")) == 2
    assert _heading_level(item("section_header", "2.1.3 Details")) == 3


def test_unnumbered_heading_stays_at_level_one():
    assert _heading_level(item("section_header", "Introduction")) == 1


# --------------------------------------------------------------------------
# references
# --------------------------------------------------------------------------


def test_back_matter_after_the_bibliography_does_not_stop_it():
    """The bug: 9 of 84 references survived because a heading reset the state."""
    doc = Document()
    doc.blocks = [
        block("b1", "heading", "References"),
        block("b2", "paragraph", "Abraham, M. J., Murtola, T. (2015). GROMACS."),
        block("b3", "paragraph", "Bereau, T., and Kremer, K. (2015). Martini."),
        block("b4", "heading", "Author contributions"),
        block("b5", "paragraph", "SM and ZL-S conceived of the project."),
        block("b6", "paragraph", "Zhang, X., Sundram, S. (2020). Fast methods."),
    ]
    _extract_references(doc)
    assert [r.key for r in doc.references] == ["bib-1", "bib-2", "bib-3"]
    # The prose of the back matter is kept as text, not swallowed as a source.
    assert [b.id for b in doc.blocks] == ["b4", "b5"]


def test_numbered_entries_keep_their_numbers():
    doc = Document()
    doc.blocks = [
        block("b1", "heading", "References"),
        block("b2", "paragraph", "[7] A. Author, Journal 2020, 1, 2."),
        block("b3", "paragraph", "[8] B. Writer, Journal 2021, 3, 4."),
    ]
    _extract_references(doc)
    assert [r.number for r in doc.references] == ["7", "8"]
    assert doc.references[0].raw.startswith("A. Author")


def test_document_without_a_bibliography_is_left_alone():
    doc = Document()
    doc.blocks = [block("b1", "paragraph", "Just prose, no sources.")]
    _extract_references(doc)
    assert doc.references == []
    assert len(doc.blocks) == 1


# --------------------------------------------------------------------------
# metadata
# --------------------------------------------------------------------------


def test_abstract_from_an_explicit_heading():
    doc = Document()
    doc.blocks = [
        block("b1", "heading", "Abstract"),
        block("b2", "paragraph", "We show that things work."),
    ]
    assert _extract_abstract(doc) == "We show that things work."


def test_abstract_falls_back_to_the_first_long_paragraph():
    doc = Document()
    doc.blocks = [
        block("b1", "paragraph", "x" * 400),
        block("b2", "heading", "Introduction"),
        block("b3", "paragraph", "y" * 400),
    ]
    assert _extract_abstract(doc).startswith("x")


def test_authors_are_read_from_the_first_paragraph():
    doc = Document()
    doc.blocks = [
        block("b1", "paragraph", "Jan A. Stevens, Fabian Grunewald, Melanie Konig"),
    ]
    assert _authors_from_blocks(doc) == [
        "Jan A. Stevens", "Fabian Grunewald", "Melanie Konig"
    ]


def test_prose_is_not_mistaken_for_an_author_list():
    doc = Document()
    doc.blocks = [block("b1", "paragraph", "the cell was simulated, and it worked")]
    assert _authors_from_blocks(doc) == []


@pytest.mark.parametrize(
    "entry",
    [
        "Abraham, M. J., Murtola, T. (2015). GROMACS. SoftwareX 1, 19.",
        "de Jong, D. H., Singh, G. (2013). Improved parameters. JCTC 9, 687.",
        "\u0160tefl, M., Herbst, K. (2020). Metabolic lifetime. Biophys. J. 119, 1359.",
        "Oliveira Bortot, L., Bashardanesh, Z. (2020). Making soup. JCIM 60, 322.",
        "Blender Online Community (2022). Blender, a 3D modelling package.",
        "marrink-lab (2022). Martini_Minimal_Cell. GitHub.",
    ],
)
def test_author_year_entries_with_awkward_names_are_kept(entry):
    """Particles, non-ASCII initials and corporate authors all count."""
    doc = Document()
    doc.blocks = [
        block("b1", "heading", "References"),
        block("b2", "paragraph", entry),
    ]
    _extract_references(doc)
    assert len(doc.references) == 1, entry


@pytest.mark.parametrize(
    "prose",
    [
        "SM and ZL-S conceived of the project and wrote the manuscript.",
        "The authors declare no competing financial interest.",
        "All claims expressed in this article are solely those of the authors.",
    ],
)
def test_back_matter_prose_is_not_taken_for_a_reference(prose):
    doc = Document()
    doc.blocks = [
        block("b1", "heading", "References"),
        block("b2", "paragraph", "Abraham, M. J. (2015). GROMACS. SoftwareX 1, 19."),
        block("b3", "heading", "Author contributions"),
        block("b4", "paragraph", prose),
    ]
    _extract_references(doc)
    assert [r.key for r in doc.references] == ["bib-1"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Molecular dynamics simulation of an entire cell", True),
        ("Introduction", False),
        ("Abstract", False),
        ("1. Introduction", False),
        ("References", False),
        ("Short", False),
    ],
)
def test_only_a_plausible_heading_is_promoted_to_the_title(text, expected):
    from paper2ink.parsers.docling_parser import _could_be_a_title

    assert _could_be_a_title(text) is expected
