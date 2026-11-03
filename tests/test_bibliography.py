"""Bibliography extraction, shared by both parser backends."""

from __future__ import annotations

from paper2ink.bibliography import (
    harvest_list_bibliography,
    harvest_numeric_bibliography,
    is_bracketed_reference_run,
    make_reference,
    split_bracketed_references,
)
from paper2ink.model import Block, Document


def block(id_: str, text: str, type_: str = "paragraph") -> Block:
    return Block(id=id_, type=type_, text=text)


# --------------------------------------------------------------------------
# single entries
# --------------------------------------------------------------------------


def test_make_reference_pulls_out_doi_and_year():
    reference = make_reference(
        "7", "A. Author, B. Writer, Journal of Things 2020, 12, 345. "
             "doi:10.1000/example1"
    )
    assert reference.key == "bib-7"
    assert reference.number == "7"
    assert reference.year == "2020"
    assert reference.doi == "10.1000/example1"


def test_make_reference_without_doi_or_year():
    reference = make_reference("1", "Some untraceable source")
    assert reference.doi is None
    assert reference.year == ""


# --------------------------------------------------------------------------
# splitting a run of entries
# --------------------------------------------------------------------------


def test_split_bracketed_references():
    text = "[1] First source, 2020. [2] Second source, 2021. [3] Third, 2022."
    pairs = split_bracketed_references(text)
    assert [number for number, _ in pairs] == ["1", "2", "3"]
    assert pairs[0][1] == "First source, 2020."
    assert pairs[2][1] == "Third, 2022."


def test_split_keeps_numbers_that_are_not_sequential():
    pairs = split_bracketed_references("[12] A. [13] B. [20] C.")
    assert [number for number, _ in pairs] == ["12", "13", "20"]


def test_is_bracketed_reference_run():
    assert is_bracketed_reference_run("[1] A. [2] B. [3] C.")
    assert not is_bracketed_reference_run("[1] only one entry here")
    assert not is_bracketed_reference_run("As shown in [1], [2] and [3] before")


# --------------------------------------------------------------------------
# harvesting a bibliography with no heading
# --------------------------------------------------------------------------


def body(count: int = 12) -> list[Block]:
    return [block(f"p{i}", f"Body paragraph number {i}.") for i in range(count)]


def test_bibliography_without_a_heading_is_harvested():
    """Wiley prints no "References" heading and breaks entries mid-sentence."""
    doc = Document()
    doc.blocks = body() + [
        block("r1", "[1] A. K. Thakur, L. Movileanu, ACS Sens. 2019, 4, 2320. "
                    "[2] M. Ahmad, J.-H. Ha, L. A. Mayse, Nat. Commun. 2023,"),
        block("r2", "14, 1374. [3] N. S. Galenkamp, M. Soskine, Nat. Commun. "
                    "2018, 9, 4085. [4] G. Huang, A. Voorspoels, Angew. Chem."),
        block("r3", "2022, 61, 202206227. [5] S. Straathof, ACS Nano 2023, 17, "
                    "13685. [6] S. J. Gr, Nat. Methods 2021, 18, 1."),
    ]
    entries = harvest_numeric_bibliography(doc)
    assert [e.number for e in entries] == ["1", "2", "3", "4", "5", "6"]
    # An entry split across two layout blocks is put back together.
    assert entries[1].raw.endswith("14, 1374.")
    # The consumed blocks leave the reading flow.
    assert all(b.id.startswith("p") for b in doc.blocks)


def test_citations_early_in_the_paper_are_not_mistaken_for_the_list():
    doc = Document()
    doc.blocks = [
        block("p0", "As shown before [1] and [2] and [3] this holds.")
    ] + body()
    assert harvest_numeric_bibliography(doc) == []
    assert len(doc.blocks) == 13


def test_too_few_entries_is_not_a_bibliography():
    doc = Document()
    doc.blocks = body() + [block("r1", "[1] One source. [2] Another one.")]
    assert harvest_numeric_bibliography(doc) == []
    assert len(doc.blocks) == 13  # nothing was consumed


def test_hyphenation_across_blocks_is_repaired():
    doc = Document()
    doc.blocks = body() + [
        block("r1", "[1] A. Author, Journal of Membra-"),
        block("r2", "ne Science 2020, 1, 2. [2] B. Writer, Soft Matter 2021, 3, 4. "
                    "[3] C. Third, Soft Matter 2018, 9, 4085. "
                    "[4] D. Fourth, Langmuir 2020, 16, 1035. "
                    "[5] E. Fifth, Macromolecules 2021, 35, 8203."),
    ]
    entries = harvest_numeric_bibliography(doc)
    assert len(entries) == 5
    assert "Membrane Science" in entries[0].raw


def test_duplicate_numbers_are_collapsed():
    doc = Document()
    doc.blocks = body() + [
        block(
            "r1",
            "[1] A. Author, Journal of Things 2019, 4, 2320. "
            "[2] B. Writer, Nature Communications 2023, 14, 1374. "
            "[3] C. Third, Soft Matter 2018, 9, 4085. "
            "[4] D. Fourth, Langmuir 2020, 16, 1035. "
            "[5] E. Fifth, Macromolecules 2021, 35, 8203. "
            "[5] E. Fifth, Macromolecules 2021, 35, 8203.",
        )
    ]
    entries = harvest_numeric_bibliography(doc)
    assert [e.number for e in entries] == ["1", "2", "3", "4", "5"]


def test_document_without_a_bibliography_is_untouched():
    doc = Document()
    doc.blocks = body()
    assert harvest_numeric_bibliography(doc) == []
    assert len(doc.blocks) == 12


def test_bibliography_starting_mid_block_is_found():
    """A layout block can end a paragraph and run straight into "[1] ..."."""
    doc = Document()
    doc.blocks = body() + [
        block("r1", "...which concludes the discussion. "
                    "[1] A. Author, Journal 2019, 4, 2320. "
                    "[2] B. Writer, Nat. Commun. 2023, 14, 1374. "
                    "[3] C. Third, Soft Matter 2018, 9, 4085. "
                    "[4] D. Fourth, Langmuir 2020, 16, 1035. "
                    "[5] E. Fifth, Macromolecules 2021, 35, 8203."),
    ]
    entries = harvest_numeric_bibliography(doc)
    assert [e.number for e in entries] == ["1", "2", "3", "4", "5"]
    assert entries[0].raw.startswith("A. Author")
    # The prose before the list is kept, not swallowed into entry one.
    assert doc.blocks[-1].text == "...which concludes the discussion."


def test_prose_before_the_list_is_dropped_when_empty():
    doc = Document()
    doc.blocks = body() + [
        block(
            "r1",
            "[1] A. Author, Journal of Things 2019, 4, 2320. "
            "[2] B. Writer, Nature Communications 2023, 14, 1374. "
            "[3] C. Third, Soft Matter 2018, 9, 4085. "
            "[4] D. Fourth, Langmuir 2020, 16, 1035. "
            "[5] E. Fifth, Macromolecules 2021, 35, 8203.",
        ),
    ]
    harvest_numeric_bibliography(doc)
    assert all(b.id.startswith("p") for b in doc.blocks)


# --------------------------------------------------------------------------
# bibliographies the parser already split into list items
# --------------------------------------------------------------------------


def entry(i: int) -> Block:
    return block(
        f"l{i}",
        f"A. Author{i}, B. Writer, Journal of Things {2000 + i % 20}, {i}, {i * 7}.",
        "list_item",
    )


def test_list_items_at_the_end_become_the_bibliography():
    """Docling strips the [n] markers and hands over one item per entry."""
    doc = Document()
    doc.blocks = body() + [entry(i) for i in range(1, 9)]
    entries = harvest_list_bibliography(doc)
    assert [e.number for e in entries] == [str(i) for i in range(1, 9)]
    assert entries[0].raw.startswith("A. Author1")
    assert all(b.id.startswith("p") for b in doc.blocks)


def test_running_heads_between_entries_do_not_break_the_run():
    doc = Document()
    doc.blocks = (
        body()
        + [entry(i) for i in range(1, 5)]
        + [block("noise", "www.advmat.de")]
        + [entry(i) for i in range(5, 10)]
    )
    entries = harvest_list_bibliography(doc)
    assert len(entries) == 9
    assert all(b.id.startswith("p") for b in doc.blocks)


def test_a_heading_stops_the_run():
    doc = Document()
    doc.blocks = (
        body()
        + [entry(1), entry(2)]
        + [block("h", "References", "heading")]
        + [entry(i) for i in range(3, 9)]
    )
    entries = harvest_list_bibliography(doc)
    assert len(entries) == 6
    assert doc.blocks[-1].text == "References"


def test_a_list_in_the_body_is_not_a_bibliography():
    """A numbered procedure has no years and must be left alone."""
    doc = Document()
    doc.blocks = body() + [
        block(f"s{i}", f"Step {i}: rinse the membrane and wait.", "list_item")
        for i in range(8)
    ]
    assert harvest_list_bibliography(doc) == []
    assert len(doc.blocks) == 20


def test_a_list_far_from_the_end_is_ignored():
    doc = Document()
    doc.blocks = [entry(i) for i in range(1, 9)] + body(20)
    assert harvest_list_bibliography(doc) == []


def test_too_few_list_items():
    doc = Document()
    doc.blocks = body() + [entry(1), entry(2)]
    assert harvest_list_bibliography(doc) == []


def test_no_list_items_at_all():
    doc = Document()
    doc.blocks = body()
    assert harvest_list_bibliography(doc) == []


# --------------------------------------------------------------------------
# RSC style: bare numbers, no brackets
# --------------------------------------------------------------------------


def test_bare_numbered_entries_are_split():
    """RSC prints "4 L. Fenno, Annu. Rev. Neurosci., 2011, 34, 389-412."."""
    doc = Document()
    doc.blocks = body() + [
        block(
            "r1",
            "1 C. Brieke, F. Rohrbach and A. Heckel, Angew. Chem., 2012, 51, 8446. "
            "2 M. Shaaya and J. Fauser, Nat. Commun., 2020, 11, 1. "
            "3 L. Fenno, O. Yizhar and K. Deisseroth, Annu. Rev. Neurosci., "
            "2011, 34, 389. "
            "4 V. Eikemo and B. Holmelid, J. Org. Chem., 2022, 87, 8034. "
            "5 I. Elamri and C. Radzewicz, Chem. Sci., 2021, 12, 100.",
        ),
    ]
    entries = harvest_numeric_bibliography(doc)
    assert [e.number for e in entries] == ["1", "2", "3", "4", "5"]
    assert entries[2].raw.startswith("L. Fenno")
    assert entries[2].year == "2011"


def test_volume_and_page_numbers_do_not_split_an_entry():
    """"2011, 34, 389" must not look like three new entries."""
    doc = Document()
    doc.blocks = body() + [
        block(
            "r1",
            "1 A. Author, J. Chem., 2011, 34, 389. "
            "2 B. Writer, J. Phys., 2015, 120, 1473. "
            "3 C. Third, Nature, 2019, 7, 42. "
            "4 D. Fourth, Science, 2020, 11, 8. "
            "5 E. Fifth, Cell, 2021, 3, 99.",
        ),
    ]
    entries = harvest_numeric_bibliography(doc)
    assert len(entries) == 5
    assert "34, 389" in entries[0].raw


def test_a_numbered_procedure_is_not_a_bibliography():
    """Steps have no years, so the year guard keeps them out."""
    doc = Document()
    doc.blocks = body() + [
        block(
            "r1",
            "1 Rinse the membrane with buffer and let it settle. "
            "2 Apply the voltage ramp until rupture occurs. "
            "3 Record the current trace for five minutes. "
            "4 Wash the chamber twice with fresh buffer. "
            "5 Repeat the measurement with the second sample.",
        ),
    ]
    assert harvest_numeric_bibliography(doc) == []
    assert len(doc.blocks) == 13
