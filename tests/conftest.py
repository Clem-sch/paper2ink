"""Shared fixtures.

The end-to-end tests build their own PDF with PyMuPDF instead of shipping a
paper in the repository: no downloads, no licensing questions, and the
fixture can be shaped to exercise exactly the features under test.  Real
papers are fetched on demand by ``scripts/fetch_test_papers.py``.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

BODY = 10
HEADING = 13
TITLE = 20


def _write(page: pymupdf.Page, x: float, y: float, text: str, size: float = BODY,
           width: float = 500) -> float:
    """Insert a wrapped text box and return the y below it."""
    rect = pymupdf.Rect(x, y, x + width, y + 700)
    leftover = page.insert_textbox(rect, text, fontsize=size, fontname="helv",
                                   align=pymupdf.TEXT_ALIGN_LEFT)
    used = 700 - leftover if leftover > 0 else 700
    lines = max(1, len(text) // int(width / (size * 0.5)) + text.count("\n") + 1)
    return y + lines * size * 1.35 + 6


@pytest.fixture(scope="session")
def sample_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A small single-column paper with a figure, a table and references."""
    path = tmp_path_factory.mktemp("fixtures") / "sample paper äö.pdf"
    doc = pymupdf.open()

    page = doc.new_page(width=595, height=842)
    y = _write(page, 50, 60, "A Minimal Paper About Nothing", size=TITLE)
    y = _write(page, 50, y + 6, "Alice Example, Bob Sample")
    y = _write(page, 50, y + 10, "Abstract", size=HEADING)
    y = _write(
        page, 50, y,
        "This paper demonstrates the conversion pipeline. It contains a figure, "
        "a table and a short bibliography so that every stage of paper2ink has "
        "something to chew on. The abstract is deliberately long enough to be "
        "recognised as the abstract of this document rather than as a caption.",
    )
    y = _write(page, 50, y + 10, "1. Introduction", size=HEADING)
    y = _write(
        page, 50, y,
        "The setup is shown in Figure 1 and the numbers are listed in Table 1. "
        "Earlier work [1] used a different approach, and so did others [2, 3]. "
        "We refer to Figure 1 a second time here, and to Section 2 below.",
    )
    y = _write(page, 50, y + 10, "2. Results", size=HEADING)
    y = _write(
        page, 50, y,
        "Everything worked as expected. See Fig. 1 once more for the geometry, "
        "and the flux follows from Eq. (1) below.",
    )
    y = _write(page, 180, y + 14, "J = -D * dc / dx + v * c                 (1)")
    _write(
        page, 50, y + 14,
        "Inserting (1) into the balance gives the result quoted above.",
    )

    page = doc.new_page(width=595, height=842)
    shape = page.new_shape()
    shape.draw_rect(pymupdf.Rect(80, 80, 500, 330))
    shape.draw_circle(pymupdf.Point(290, 205), 80)
    shape.finish(color=(0, 0, 0), fill=(0.85, 0.85, 0.85), width=1.5)
    shape.commit()
    y = _write(page, 50, 345,
               "Figure 1. A rectangle and a circle, drawn as vector graphics.")

    table_top = y + 30
    shape = page.new_shape()
    for row in range(4):
        shape.draw_line(pymupdf.Point(80, table_top + row * 22),
                        pymupdf.Point(400, table_top + row * 22))
    shape.finish(color=(0, 0, 0), width=0.8)
    shape.commit()
    _write(page, 50, table_top - 20, "Table 1. Measured values.")
    for row, label in enumerate(("sample A 1.0", "sample B 2.0", "sample C 3.0")):
        _write(page, 90, table_top + row * 22 + 4, label, size=9, width=300)

    page = doc.new_page(width=595, height=842)
    y = _write(page, 50, 60, "References", size=HEADING)
    _write(
        page, 50, y,
        "[1] A. Author, B. Writer, Journal of Things 2020, 12, 345. "
        "doi:10.1000/example1\n"
        "[2] C. Scholar, Another Journal 2019, 3, 10.\n"
        "[3] D. Researcher, Third Journal 2018, 1, 1.",
    )

    doc.save(path)
    doc.close()
    return path
