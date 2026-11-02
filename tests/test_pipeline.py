"""End-to-end tests over the whole pipeline, using the synthetic sample PDF."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from paper2ink.model import Document
from paper2ink.pipeline import ConversionSettings, convert, output_name_for
from paper2ink.validate import validate_epub


@pytest.fixture(scope="module")
def converted(sample_pdf: Path, tmp_path_factory: pytest.TempPathFactory):
    out_dir = tmp_path_factory.mktemp("out")
    settings = ConversionSettings(
        parser="pymupdf", dpi=110, dump_json=True, validate=False
    )
    result = convert(sample_pdf, out_dir / "sample.epub", settings)
    assert result.error is None, result.error
    return result


def test_epub_is_written(converted):
    assert converted.epub_path is not None
    assert converted.epub_path.exists()
    assert converted.epub_path.stat().st_size > 2000


def test_filename_with_umlauts_and_spaces_works(sample_pdf):
    assert " " in sample_pdf.name and "ä" in sample_pdf.name


def test_structure_was_detected(converted):
    doc = converted.document
    assert doc is not None
    assert "Minimal Paper" in doc.metadata.title
    assert doc.metadata.abstract.startswith("This paper demonstrates")
    headings = [b.text for b in doc.blocks if b.type == "heading"]
    assert any(h.startswith("1.") for h in headings)
    assert any(h.startswith("2.") for h in headings)


def test_figure_and_table_were_cropped(converted):
    doc = converted.document
    keys = {asset.key for asset in doc.assets}
    assert "fig-1" in keys
    figure = doc.asset_by_key("fig-1")
    assert figure.image_path == "fig-1.png"
    assert figure.caption


def test_displayed_equation_becomes_an_image_in_place(converted):
    doc = converted.document
    equation = doc.asset_by_key("eq-1")
    assert equation is not None, [a.key for a in doc.assets]
    assert equation.image_path == "eq-1.png"
    # Equations stay in the text flow, they do not move to the appendix.
    assert any(b.type == "equation" and b.asset_key == "eq-1" for b in doc.blocks)


def test_equation_references_are_linked(converted):
    import zipfile

    with zipfile.ZipFile(converted.epub_path) as archive:
        chapters = {
            name: archive.read(name).decode("utf-8")
            for name in archive.namelist()
            if name.startswith("EPUB/ch-")
        }
    joined = "".join(chapters.values())
    assert 'id="eq-1"' in joined
    assert "#eq-1" in joined                 # "Eq. (1)" links to the image
    assert "ref-eq-1-1" in joined
    # The bare "(1)" after "Inserting" is recognised as an equation reference.
    assert "ref-eq-1-2" in joined


def test_references_were_extracted(converted):
    doc = converted.document
    assert len(doc.references) == 3
    assert doc.references[0].number == "1"
    assert doc.references[0].doi == "10.1000/example1"


def test_internal_links_and_backlinks(converted):
    with zipfile.ZipFile(converted.epub_path) as archive:
        figures = archive.read("EPUB/figures.xhtml").decode("utf-8")
        chapters = "".join(
            archive.read(name).decode("utf-8")
            for name in archive.namelist()
            if name.startswith("EPUB/ch-")
        )
    # The text links to the figure ...
    assert 'href="figures.xhtml#fig-1"' in chapters
    assert 'id="ref-fig-1-1"' in chapters
    # ... and the figure links back to every mention.
    assert 'id="fig-1"' in figures
    assert "ref-fig-1-1" in figures
    assert "ref-fig-1-2" in figures
    # Citations resolve to the bibliography.
    assert 'href="references.xhtml#bib-1"' in chapters


def test_epub_passes_internal_validation(converted):
    report = validate_epub(converted.epub_path, run_external=False)
    assert report.ok, report.errors


def test_build_summary_counts(converted):
    build = converted.build
    assert build.figures >= 1
    assert build.references == 3
    assert build.links >= 4
    assert build.chapters >= 2


def test_dump_json_roundtrips(converted):
    assert converted.json_path is not None and converted.json_path.exists()
    doc = Document.load_json(converted.json_path)
    assert doc.metadata.title == converted.document.metadata.title
    assert len(doc.assets) == len(converted.document.assets)


def test_output_name_is_filesystem_safe():
    doc = Document()
    doc.metadata.title = "Nanopore-Functionalized Hybrid Membranes: Part 1"
    doc.metadata.authors = ["Edo Vreeker", "Jane Doe"]
    doc.metadata.year = "2025"
    name = output_name_for(doc)
    assert name.startswith("Vreeker_2025_")
    assert name.endswith(".epub")
    assert not set(name) & set('<>:"/\\|?*')


def test_missing_file_is_reported_not_raised(tmp_path):
    result = convert(tmp_path / "nope.pdf", tmp_path / "out.epub")
    assert not result.ok
    assert "not a file" in (result.error or "")


def test_batch_keeps_going_after_a_bad_pdf(tmp_path, sample_pdf):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not really a pdf")
    settings = ConversionSettings(parser="pymupdf", dpi=72, validate=False)
    results = [
        convert(broken, tmp_path / "broken.epub", settings),
        convert(sample_pdf, tmp_path / "good.epub", settings),
    ]
    assert not results[0].ok
    assert results[1].ok


def test_azw3_without_calibre_warns_instead_of_failing(sample_pdf, tmp_path, monkeypatch):
    import paper2ink.pipeline as pipeline

    monkeypatch.setattr(pipeline.shutil, "which", lambda name: None)
    settings = ConversionSettings(parser="pymupdf", dpi=72, validate=False, azw3=True)
    result = convert(sample_pdf, tmp_path / "a.epub", settings)
    assert result.ok
    assert result.azw3_path is None
    assert any("ebook-convert" in w for w in result.warnings)


def test_keep_temp_is_reported(sample_pdf, tmp_path):
    settings = ConversionSettings(
        parser="pymupdf", dpi=72, validate=False, keep_temp=True
    )
    result = convert(sample_pdf, tmp_path / "b.epub", settings)
    assert any("temporary files kept" in w for w in result.warnings)
