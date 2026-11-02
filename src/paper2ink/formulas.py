"""Optional LaTeX recognition for displayed and inline formulas.

Kindle renders MathML unreliably, so paper2ink always *shows* an image cropped
from the PDF.  Recognition is only used to attach LaTeX as ``alt`` text, which
makes the formula searchable and accessible.  It is therefore off by default
and sits behind an interface so other backends can be added later.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

from .model import Document

log = logging.getLogger("paper2ink.formulas")

__all__ = [
    "BaseFormulaRecognizer",
    "NullFormulaRecognizer",
    "DoclingFormulaRecognizer",
    "Pix2TextFormulaRecognizer",
    "get_formula_recognizer",
    "FORMULA_BACKENDS",
]

FORMULA_BACKENDS = ("docling", "pix2text")


class BaseFormulaRecognizer(ABC):
    """Attach LaTeX to the equation assets of a document."""

    name = "base"
    #: rough order of magnitude of the per-formula cost on a laptop CPU
    speed_note = ""

    @classmethod
    def available(cls) -> bool:
        return True

    @abstractmethod
    def recognize(self, image_path: Path) -> str | None:
        """Return LaTeX for one cropped formula image, or ``None``."""

    def annotate(self, doc: Document, image_dir: Path) -> int:
        """Fill in ``Asset.latex`` for every equation that has an image."""
        done = 0
        for asset in doc.assets:
            if asset.kind != "equation" or not asset.image_path:
                continue
            try:
                latex = self.recognize(image_dir / asset.image_path)
            except Exception as exc:  # pragma: no cover - backend specific
                doc.add_warning(f"formula recognition failed for {asset.key}: {exc}")
                continue
            if latex:
                asset.latex = latex
                done += 1
        return done


class NullFormulaRecognizer(BaseFormulaRecognizer):
    """Does nothing; used when ``--formulas off`` (the default)."""

    name = "off"

    def recognize(self, image_path: Path) -> str | None:
        return None

    def annotate(self, doc: Document, image_dir: Path) -> int:
        return 0


class DoclingFormulaRecognizer(BaseFormulaRecognizer):
    """Docling's CodeFormula model - already in the stack when Docling is used."""

    name = "docling"
    speed_note = "roughly 1-3 s per formula on a laptop CPU"

    def __init__(self) -> None:
        self._model = None

    @classmethod
    def available(cls) -> bool:
        try:
            import docling  # noqa: F401
        except ImportError:
            return False
        return True

    def _load(self):
        if self._model is None:
            from docling.models.code_formula_model import (  # type: ignore
                CodeFormulaModel,
                CodeFormulaModelOptions,
            )
            from docling.datamodel.accelerator_options import AcceleratorOptions

            self._model = CodeFormulaModel(
                enabled=True,
                artifacts_path=None,
                options=CodeFormulaModelOptions(do_code_enrichment=False,
                                                do_formula_enrichment=True),
                accelerator_options=AcceleratorOptions(),
            )
        return self._model

    def recognize(self, image_path: Path) -> str | None:
        from PIL import Image

        model = self._load()
        with Image.open(image_path) as image:
            predictions = list(
                model.predict([image.convert("RGB")], ["<formula>"])
            )
        if not predictions:
            return None
        text = predictions[0]
        return text.strip() or None


class Pix2TextFormulaRecognizer(BaseFormulaRecognizer):
    """Lightweight CPU formula OCR; optional extra ``paper2ink[pix2text]``."""

    name = "pix2text"
    speed_note = "roughly 0.5-2 s per formula on a laptop CPU"

    def __init__(self) -> None:
        self._model = None

    @classmethod
    def available(cls) -> bool:
        try:
            import pix2text  # noqa: F401
        except ImportError:
            return False
        return True

    def _load(self):
        if self._model is None:
            from pix2text import Pix2Text  # type: ignore

            self._model = Pix2Text.from_config(device="cpu")
        return self._model

    def recognize(self, image_path: Path) -> str | None:
        model = self._load()
        latex = model.recognize_formula(str(image_path))
        if isinstance(latex, str):
            return latex.strip() or None
        return None


def get_formula_recognizer(name: str) -> BaseFormulaRecognizer:
    """Instantiate a formula backend by name, falling back to a no-op."""
    name = (name or "").lower()
    if name == "docling":
        if DoclingFormulaRecognizer.available():
            return DoclingFormulaRecognizer()
        log.warning("docling is not installed; formulas keep their image only")
        return NullFormulaRecognizer()
    if name == "pix2text":
        if Pix2TextFormulaRecognizer.available():
            return Pix2TextFormulaRecognizer()
        log.warning(
            "pix2text is not installed (pip install paper2ink[pix2text]); "
            "formulas keep their image only"
        )
        return NullFormulaRecognizer()
    return NullFormulaRecognizer()
