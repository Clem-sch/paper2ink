"""Parser backends.

A backend turns a PDF into the parser-independent :class:`~paper2ink.model.Document`.
Add a new one by subclassing :class:`~paper2ink.parsers.base.BaseParser` and
registering it in :data:`PARSERS`.
"""

from __future__ import annotations

from .base import BaseParser, ParseOptions, ParserError

PARSER_NAMES = ("docling", "pymupdf", "auto")


def get_parser(name: str, options: ParseOptions | None = None) -> BaseParser:
    """Instantiate a parser backend by name.

    ``auto`` prefers Docling and falls back to the dependency-free PyMuPDF
    backend when Docling is not installed.
    """
    options = options or ParseOptions()
    name = name.lower()

    if name == "auto":
        from .docling_parser import DoclingParser

        name = "docling" if DoclingParser.available() else "pymupdf"

    if name == "docling":
        from .docling_parser import DoclingParser

        if not DoclingParser.available():
            raise ParserError(
                "The docling backend is not installed. Install it with "
                "'pip install paper2ink[docling]' or use --parser pymupdf."
            )
        return DoclingParser(options)

    if name == "pymupdf":
        from .pymupdf_parser import PyMuPDFParser

        return PyMuPDFParser(options)

    raise ParserError(f"Unknown parser backend: {name!r}")


__all__ = ["BaseParser", "ParseOptions", "ParserError", "PARSER_NAMES", "get_parser"]
