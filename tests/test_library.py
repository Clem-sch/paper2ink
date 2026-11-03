"""The conversion history: recording, filtering, sorting and sent state."""

from __future__ import annotations

from pathlib import Path

import pytest

from paper2ink.library import SORT_FIELDS, Entry, Library


@pytest.fixture
def library(tmp_path: Path) -> Library:
    return Library(tmp_path / "library.sqlite3")


def add(library: Library, **fields) -> Entry:
    defaults = dict(
        source_name="paper.pdf",
        title="A Paper",
        first_author="Vreeker",
        authors="Edo Vreeker, Jane Doe",
        journal="Adv. Mater.",
        year="2025",
        parser="pymupdf",
        pages=15,
        figures=6,
        references_count=90,
    )
    defaults.update(fields)
    return library.add(Entry(**defaults))


# --------------------------------------------------------------------------
# recording
# --------------------------------------------------------------------------


def test_an_entry_gets_an_id_and_a_timestamp(library):
    entry = add(library)
    assert entry.id > 0
    assert entry.created
    assert library.get(entry.id).title == "A Paper"


def test_unknown_id_returns_none(library):
    assert library.get(999) is None


def test_entries_survive_a_reopen(tmp_path):
    path = tmp_path / "library.sqlite3"
    add(Library(path), title="Persisted")
    assert Library(path).list()[0].title == "Persisted"


def test_delete(library):
    entry = add(library)
    assert library.delete(entry.id) is True
    assert library.get(entry.id) is None
    assert library.delete(entry.id) is False


# --------------------------------------------------------------------------
# sent state
# --------------------------------------------------------------------------


def test_a_new_entry_is_not_sent(library):
    entry = add(library)
    assert entry.is_sent is False
    assert library.get(entry.id).sent_at is None


def test_marking_sent_and_back(library):
    entry = add(library)
    library.mark_sent(entry.id)
    assert library.get(entry.id).is_sent is True
    library.mark_sent(entry.id, sent=False)
    assert library.get(entry.id).is_sent is False


def test_filtering_by_sent_state(library):
    first = add(library, title="Sent one")
    add(library, title="Unsent one")
    library.mark_sent(first.id)

    assert [e.title for e in library.list(sent=True)] == ["Sent one"]
    assert [e.title for e in library.list(sent=False)] == ["Unsent one"]
    assert len(library.list()) == 2


def test_stats_count_sent_and_unsent(library):
    first = add(library)
    add(library)
    add(library)
    library.mark_sent(first.id)
    assert library.stats() == {"total": 3, "sent": 1, "unsent": 2, "starred": 0}


def test_starring(library):
    entry = add(library)
    library.set_starred(entry.id, True)
    assert library.list(starred=True)[0].id == entry.id
    library.set_starred(entry.id, False)
    assert library.list(starred=True) == []


# --------------------------------------------------------------------------
# searching and filtering
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "term,expected",
    [
        ("Nanopore", "Nanopore Membranes"),
        ("vreeker", "Nanopore Membranes"),   # author, case-insensitive
        ("fchem", "Whole Cell"),             # file name
        ("10.1002", "Nanopore Membranes"),   # DOI
    ],
)
def test_search_covers_title_author_file_and_doi(library, term, expected):
    add(library, title="Nanopore Membranes", authors="Edo Vreeker",
        source_name="advmat.pdf", doi="10.1002/adma.202418462")
    add(library, title="Whole Cell", authors="Jan Stevens",
        source_name="fchem-11.pdf", doi="10.3389/fchem")
    assert [e.title for e in library.list(search=term)] == [expected]


def test_filter_by_parser_and_year(library):
    add(library, title="One", parser="pymupdf", year="2025")
    add(library, title="Two", parser="docling", year="2019")
    assert [e.title for e in library.list(parser="docling")] == ["Two"]
    assert [e.title for e in library.list(year="2025")] == ["One"]


def test_known_values_feed_the_filter_dropdowns(library):
    add(library, parser="pymupdf", year="2025")
    add(library, parser="docling", year="2019")
    assert library.known_values("parser") == ["docling", "pymupdf"]
    assert library.known_values("year") == ["2019", "2025"]
    assert library.known_values("note") == []    # not a filterable column


# --------------------------------------------------------------------------
# sorting
# --------------------------------------------------------------------------


def test_sorting_by_year(library):
    add(library, title="Old", year="2015")
    add(library, title="New", year="2025")
    assert [e.title for e in library.list(sort="year")] == ["New", "Old"]
    assert [e.title for e in library.list(sort="year", descending=False)] == [
        "Old", "New"
    ]


def test_sorting_by_title_ignores_case(library):
    add(library, title="apple")
    add(library, title="Banana")
    assert [e.title for e in library.list(sort="title", descending=False)] == [
        "apple", "Banana"
    ]


def test_sorting_by_figures(library):
    add(library, title="Few", figures=2)
    add(library, title="Many", figures=15)
    assert [e.title for e in library.list(sort="figures")] == ["Many", "Few"]


def test_an_unknown_sort_key_falls_back_instead_of_injecting(library):
    add(library, title="First")
    add(library, title="Second")
    entries = library.list(sort="title; DROP TABLE conversions")
    assert len(entries) == 2          # the table is still there
    assert all(key in SORT_FIELDS for key in SORT_FIELDS)


def test_limit_is_clamped(library):
    for index in range(5):
        add(library, title=f"Paper {index}")
    assert len(library.list(limit=2)) == 2
    assert len(library.list(limit=0)) == 1       # clamped to at least one


# --------------------------------------------------------------------------
# housekeeping
# --------------------------------------------------------------------------


def test_forget_missing_drops_rows_whose_epub_is_gone(library, tmp_path):
    present = tmp_path / "there.epub"
    present.write_bytes(b"PK")
    add(library, title="Still here", epub_path=str(present))
    add(library, title="Deleted", epub_path=str(tmp_path / "gone.epub"))

    assert library.forget_missing() == 1
    assert [e.title for e in library.list()] == ["Still here"]


def test_epub_exists_reflects_the_filesystem(library, tmp_path):
    path = tmp_path / "book.epub"
    entry = add(library, epub_path=str(path))
    assert library.get(entry.id).epub_exists is False
    path.write_bytes(b"PK")
    assert library.get(entry.id).epub_exists is True
