"""The parser interface every backend implements."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ..model import Document

log = logging.getLogger("paper2ink.parser")


class ParserError(RuntimeError):
    """Raised when a backend is unavailable or cannot handle a PDF."""


@dataclass
class ParseOptions:
    """Knobs that influence parsing (not rendering)."""

    tables: str = "image"          # "image" | "html"
    formulas: str = "off"          # "off" | "latex"
    formula_backend: str = "docling"
    ocr: bool = False
    extra: dict[str, object] = field(default_factory=dict)


class BaseParser(ABC):
    """Common base for all parser backends.

    A backend only has to fill the intermediate model; cropping, cleaning,
    reference linking and EPUB generation are shared.
    """

    name: str = "base"
    #: human readable note shown by ``--help``/the summary
    description: str = ""

    def __init__(self, options: ParseOptions | None = None) -> None:
        self.options = options or ParseOptions()

    @classmethod
    def available(cls) -> bool:
        """Whether this backend can run in the current environment."""
        return True

    @abstractmethod
    def parse(self, pdf_path: Path) -> Document:
        """Parse *pdf_path* into the intermediate model."""

    # -- helpers shared by backends ---------------------------------------
    @staticmethod
    def _block_id(index: int) -> str:
        return f"blk-{index:04d}"
