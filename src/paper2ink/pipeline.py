"""The conversion pipeline, free of any CLI concerns.

``convert()`` is what a GUI would call too: it takes a PDF and settings and
returns a :class:`ConversionResult`.  Progress is reported through an
optional callback so the caller decides how to display it.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .assets import AssetRenderer, RenderOptions, render_cover, render_debug_layout
from .cleanup import CleanupConfig, clean_document
from .epub_builder import BuildOptions, BuildResult, EpubBuilder
from .formulas import get_formula_recognizer
from .model import Document
from .parsers import ParseOptions, get_parser
from .validate import ValidationReport, validate_epub

log = logging.getLogger("paper2ink")

__all__ = ["ConversionSettings", "ConversionResult", "convert", "output_name_for"]

ProgressCallback = Callable[[str], None]

STAGES = (
    "parse",
    "clean",
    "assets",
    "formulas",
    "link",
    "epub",
    "validate",
)


@dataclass
class ConversionSettings:
    parser: str = "auto"
    dpi: int = 250
    color: bool = False
    tables: str = "image"           # "image" | "html"
    formulas: str = "off"           # "off" | "latex"
    formula_backend: str = "docling"
    cover: bool = True
    azw3: bool = False
    dump_json: bool = False
    keep_temp: bool = False
    debug_layout: bool = False
    validate: bool = True
    boilerplate: list[str] | None = None

    def parse_options(self) -> ParseOptions:
        return ParseOptions(
            tables=self.tables,
            formulas=self.formulas,
            formula_backend=self.formula_backend,
        )

    def render_options(self) -> RenderOptions:
        return RenderOptions(dpi=self.dpi, color=self.color)

    def cleanup_config(self) -> CleanupConfig:
        return CleanupConfig(boilerplate=self.boilerplate)


@dataclass
class ConversionResult:
    source: Path
    epub_path: Path | None = None
    azw3_path: Path | None = None
    json_path: Path | None = None
    debug_dir: Path | None = None
    build: BuildResult | None = None
    validation: ValidationReport | None = None
    document: Document | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and self.epub_path is not None


# --------------------------------------------------------------------------


def convert(
    pdf_path: Path,
    out_path: Path | None = None,
    settings: ConversionSettings | None = None,
    progress: ProgressCallback | None = None,
) -> ConversionResult:
    """Convert one PDF into an EPUB.  Never raises for per-document problems."""
    settings = settings or ConversionSettings()
    pdf_path = Path(pdf_path)
    result = ConversionResult(source=pdf_path)

    def step(message: str) -> None:
        log.info(message)
        if progress:
            progress(message)

    if not pdf_path.is_file():
        result.error = f"not a file: {pdf_path}"
        return result

    work_dir = Path(tempfile.mkdtemp(prefix="paper2ink-"))
    image_dir = work_dir / "images"
    try:
        step(f"parsing with {settings.parser}")
        parser = get_parser(settings.parser, settings.parse_options())
        doc = parser.parse(pdf_path)
        result.document = doc

        step("cleaning up text")
        doc = clean_document(doc, settings.cleanup_config())

        step("rendering figures, tables and equations")
        with AssetRenderer(pdf_path, settings.render_options()) as renderer:
            renderer.render_document(doc, image_dir)
            if settings.formulas == "latex":
                step("recognising formulas (slow on CPU)")
                recognizer = get_formula_recognizer(settings.formula_backend)
                recognizer.annotate(doc, image_dir)

        if settings.debug_layout:
            debug_dir = (out_path.parent if out_path else pdf_path.parent) / (
                f"{pdf_path.stem}-layout"
            )
            step("writing layout debug images")
            render_debug_layout(doc, pdf_path, debug_dir)
            result.debug_dir = debug_dir

        cover_path = None
        if settings.cover:
            cover_path = work_dir / "cover.png"
            render_cover(doc, cover_path)

        if out_path is None:
            out_path = pdf_path.parent / output_name_for(doc)
        out_path = Path(out_path)

        step("building EPUB")
        builder = EpubBuilder(doc, BuildOptions(tables=settings.tables))
        build = builder.build(out_path, image_dir, cover_path)
        result.build = build
        result.epub_path = out_path

        if settings.dump_json:
            json_path = out_path.with_suffix(".json")
            doc.dump_json(json_path)
            result.json_path = json_path

        # Collect the document's warnings before the optional steps, which
        # append warnings of their own.
        result.warnings.extend(doc.warnings)

        if settings.validate:
            step("validating")
            result.validation = validate_epub(out_path)

        if settings.azw3:
            step("converting to AZW3 with Calibre")
            result.azw3_path = _convert_azw3(out_path, result)
    except Exception as exc:  # one bad PDF must not stop a batch
        log.exception("conversion failed for %s", pdf_path)
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        if settings.keep_temp:
            log.info("keeping temporary files in %s", work_dir)
            result.warnings.append(f"temporary files kept in {work_dir}")
        else:
            shutil.rmtree(work_dir, ignore_errors=True)

    return result


# --------------------------------------------------------------------------


def output_name_for(doc: Document) -> str:
    """``<FirstAuthor>_<Year>_<ShortTitle>.epub``, safe on every filesystem."""
    author = _slug(doc.metadata.first_author_surname) or "Unknown"
    year = doc.metadata.year or "n.d."
    words = [w for w in re.split(r"\W+", doc.metadata.title or "") if len(w) > 3]
    title = _slug("-".join(words[:5])) or "paper"
    return f"{author}_{year}_{title}.epub"[:160]


def _slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w\-]+", "-", text, flags=re.ASCII)
    return re.sub(r"-{2,}", "-", text).strip("-")


def _convert_azw3(epub_path: Path, result: ConversionResult) -> Path | None:
    executable = shutil.which("ebook-convert")
    if not executable:
        result.warnings.append(
            "Calibre's ebook-convert was not found, so no AZW3 was written. "
            "Install Calibre and make sure ebook-convert is on PATH."
        )
        return None
    azw3_path = epub_path.with_suffix(".azw3")
    try:
        completed = subprocess.run(
            [executable, str(epub_path), str(azw3_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        result.warnings.append(f"ebook-convert failed: {exc}")
        return None
    if completed.returncode != 0:
        result.warnings.append(
            "ebook-convert failed: " + (completed.stderr or "").strip()[:500]
        )
        return None
    return azw3_path
