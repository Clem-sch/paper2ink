"""Chapter splitting, anchors and the backlink rules."""

from __future__ import annotations

import pytest

from paper2ink.epub_builder import EpubBuilder
from paper2ink.model import Asset, BBox, Block, Document


def heading(id_: str, text: str, level: int = 1) -> Block:
    return Block(id=id_, type="heading", text=text, level=level)


def para(id_: str, text: str) -> Block:
    return Block(id=id_, type="paragraph", text=text)


@pytest.fixture
def doc() -> Document:
    document = Document(source_path="x.pdf")
    document.metadata.title = "A Paper"
    document.blocks = [
        heading("h1", "1. Introduction"),
        para("p1", "The setup is in Figure 1 and again in Figure 1."),
        heading("h2", "2. Results"),
        para("p2", "Table 1 lists the values. Figure 1 is referenced here too."),
        Block(id="cap3", type="caption", text="Figure 3. Never mentioned.",
              asset_key="fig-3"),
    ]
    for key, kind, label, number, home in (
        ("fig-1", "figure", "Figure 1", "1", "p1"),
        ("tab-1", "table", "Table 1", "1", "p2"),
        ("fig-3", "figure", "Figure 3", "3", "cap3"),
    ):
        document.assets.append(
            Asset(key=key, kind=kind, label=label, number=number,
                  caption="caption text", image_path=f"{key}.png",
                  bboxes=[BBox(0, 0, 0, 10, 10)], home_block_id=home)
        )
    return document


@pytest.fixture
def built(doc: Document) -> EpubBuilder:
    builder = EpubBuilder(doc)
    builder._split_chapters()
    builder._collect_section_anchors()
    builder._link_everything()
    return builder


def test_chapters_split_at_level_one_headings(built):
    assert [c.file_name for c in built.chapters] == ["ch-001.xhtml", "ch-002.xhtml"]
    assert built.chapters[0].title == "1. Introduction"
    assert built.chapters[1].title == "2. Results"


def test_numbered_headings_become_section_anchors(built):
    assert built.section_keys["sec-1"] == "ch-001.xhtml"
    assert built.section_keys["sec-2"] == "ch-002.xhtml"


def test_mentions_are_numbered_across_chapters(built):
    mentions = built.linker.mentions_for("fig-1")
    assert [m.index for m in mentions] == [1, 2, 3]
    assert [m.file for m in mentions] == [
        "ch-001.xhtml", "ch-001.xhtml", "ch-002.xhtml"
    ]


def test_several_mentions_give_numbered_backlinks(built, doc):
    html = built._render_backlinks(doc.asset_by_key("fig-1"))
    assert "ch-001.xhtml#ref-fig-1-1" in html
    assert "ch-002.xhtml#ref-fig-1-3" in html
    assert ">1</a>" in html and ">3</a>" in html


def test_single_mention_gives_a_plain_backlink(built, doc):
    html = built._render_backlinks(doc.asset_by_key("tab-1"))
    assert "back to text" in html
    assert "ch-002.xhtml#ref-tab-1-1" in html


def test_unmentioned_asset_links_to_its_original_section(built, doc):
    html = built._render_backlinks(doc.asset_by_key("fig-3"))
    assert "back to text" in html
    # fig-3's caption sat in chapter two, so that is where the reader lands.
    assert "ch-002.xhtml#top-ch-002" in html


def test_appendix_order_is_figures_then_tables_then_supplementary(doc):
    doc.assets.append(
        Asset(key="fig-s1", kind="figure", label="Figure S1", number="S1",
              supplementary=True, order=9)
    )
    doc.assets[0].order = 1
    doc.assets[1].order = 2
    doc.assets[2].order = 3
    builder = EpubBuilder(doc)
    assert [a.key for a in builder._appendix_assets()] == [
        "fig-1", "fig-3", "tab-1", "fig-s1"
    ]


def test_href_resolution(built):
    assert built._href_for("fig-1") == "figures.xhtml#fig-1"
    assert built._href_for("sec-2") == "ch-002.xhtml#sec-2"
    assert built._href_for("fig-99") is None
    assert built._href_for("bib-1") is None  # no bibliography in this document


# --------------------------------------------------------------------------
# bibliography rendering
# --------------------------------------------------------------------------


def with_references(numbers: list[str]) -> EpubBuilder:
    from paper2ink.model import Reference

    document = Document()
    document.blocks = [heading("h1", "1. Introduction"), para("p1", "Text.")]
    document.references = [
        Reference(key=f"bib-{n}", number=n, raw=f"Author {n}, Journal 2020.")
        for n in numbers
    ]
    builder = EpubBuilder(document)
    builder._split_chapters()
    builder._collect_section_anchors()
    builder._link_everything()
    return builder


def test_numeric_references_are_shown_in_numeric_order():
    """Reading order put 108-113 between 66 and 67 in a real ACS paper."""
    builder = with_references(["66", "108", "67", "68"])
    html = builder._render_references()
    positions = [html.index(f'id="bib-{n}"') for n in ("66", "67", "68", "108")]
    assert positions == sorted(positions)


def test_the_printed_number_is_the_real_one():
    builder = with_references(["66", "108", "67"])
    html = builder._render_references()
    assert '<span class="refnum">66.</span>' in html
    assert '<span class="refnum">108.</span>' in html
    # No <ol>, whose automatic numbering would disagree with those.
    assert "<ol" not in html


def test_author_year_references_keep_the_printed_order():
    builder = with_references(["", "", ""])
    builder.doc.references[0].raw = "Zuber, A. (2019). Last alphabetically."
    builder.doc.references[0].key = "bib-z"
    html = builder._render_references()
    assert "bib-z" in html


def test_doi_links_are_emitted():
    builder = with_references(["1"])
    builder.doc.references[0].doi = "10.1000/example"
    html = builder._render_references()
    assert 'href="https://doi.org/10.1000/example"' in html


def test_an_asset_without_an_image_still_renders_and_anchors(doc):
    """A caption whose region could not be found must not kill the links."""
    from paper2ink.model import Asset

    doc.assets.append(
        Asset(key="tab-9", kind="table", label="Table 9", number="9",
              caption="Measured values.", image_path=None, bboxes=[], order=9)
    )
    doc.blocks.append(para("p9", "The values are listed in Table 9."))
    builder = EpubBuilder(doc)
    builder._split_chapters()
    builder._collect_section_anchors()
    builder._link_everything()
    html = builder._render_assets_page(builder._appendix_assets())
    assert 'id="tab-9"' in html
    assert "could not be extracted" in html
    assert "Measured values." in html


def test_a_chapter_with_nothing_but_a_heading_is_dropped():
    """"References" keeps its heading after the entries move to the
    bibliography, which would leave a hollow chapter and a duplicate in
    the table of contents."""
    document = Document()
    document.blocks = [
        heading("h1", "1. Introduction"),
        para("p1", "Some real content."),
        heading("h2", "References"),
    ]
    builder = EpubBuilder(document)
    builder._split_chapters()
    assert [c.title for c in builder.chapters] == ["1. Introduction"]


def test_a_short_chapter_with_content_is_kept():
    document = Document()
    document.blocks = [
        heading("h1", "1. Introduction"),
        para("p1", "Content."),
        heading("h2", "Funding"),
        para("p2", "Funded by a grant."),
    ]
    builder = EpubBuilder(document)
    builder._split_chapters()
    assert [c.title for c in builder.chapters] == ["1. Introduction", "Funding"]
