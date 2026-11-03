"""Docling backend: the default parser.

Docling runs layout, reading-order, table-structure and (optionally) formula
models on the CPU.  The models are downloaded from Hugging Face the first
time it runs, which takes a few minutes and a few hundred megabytes.

Only the structure comes from Docling.  Figures, tables and equations are
still cropped out of the PDF with PyMuPDF from Docling's bounding boxes,
because re-rendering the page region is the only way to catch vector art and
multi-panel figures faithfully.

Docling's output needs real post-processing before it fits the model:

* captions come back as separate items that are *not* linked to the picture
  they belong to, so captions and pictures are paired up here by page and
  distance;
* a caption is often split, with the label ("FIGURE 2") in one item and the
  text in the next;
* journal logos and badges are reported as pictures, so a picture without a
  caption is dropped instead of being numbered as a figure;
* every heading comes back at level 1, including journal front matter, so
  levels are re-derived and front matter is dropped.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..bibliography import (
    AUTHOR_YEAR_ENTRY_RE,
    ENTRY_MARKER_RE,
    harvest_list_bibliography,
    harvest_numeric_bibliography,
    make_reference,
)
from ..cleanup import (
    FRONT_MATTER_LABEL_RE,
    FRONT_MATTER_RE,
    guess_year,
    heading_key,
    normalize_text,
)
from ..model import Asset, BBox, Block, Document, Footnote, Metadata, Reference
from ..refs import extract_doi, normalize_key, parse_caption_label
from .base import BaseParser, ParseOptions, ParserError

log = logging.getLogger("paper2ink.parser.docling")

# Docling labels -> our block types.
_LABEL_MAP = {
    "title": "title",
    "section_header": "heading",
    "text": "paragraph",
    "paragraph": "paragraph",
    "list_item": "list_item",
    "caption": "caption",
    "footnote": "footnote",
    "formula": "equation",
    "reference": "reference_entry",
    "code": "paragraph",
}

_GRAPHIC_LABELS = {"picture", "chart"}
_SKIP_LABELS = {"page_header", "page_footer", "document_index", "form",
                "key_value_region", "checkbox_selected", "checkbox_unselected"}

_NUMBERED_HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+\S")
_EQUATION_TAIL_RE = re.compile(r"\(\s*(S?\d+[a-z]?)\s*\)\s*$")

#: How far below/above a caption a graphic may sit and still belong to it.
_CAPTION_DISTANCE = 420.0


@dataclass
class _Item:
    """One Docling item, flattened into what the rest of the code needs."""

    label: str
    text: str
    bbox: BBox | None
    nesting: int = 0
    item: object = None
    used: bool = False

    @property
    def page(self) -> int:
        return self.bbox.page if self.bbox else 0


@dataclass
class _CaptionGroup:
    kind: str
    number: str
    supplementary: bool
    label: str
    text: str
    bbox: BBox | None
    items: list[_Item] = field(default_factory=list)


class DoclingParser(BaseParser):
    """Layout parsing with Docling's ML models."""

    name = "docling"
    description = "Docling layout models (downloads models on first run)"

    @classmethod
    def available(cls) -> bool:
        try:
            import docling  # noqa: F401
        except ImportError:
            return False
        return True

    def __init__(self, options: ParseOptions | None = None) -> None:
        super().__init__(options)
        self._counter = 0
        self._asset_order = 0

    # -- entry point -------------------------------------------------------
    def parse(self, pdf_path: Path) -> Document:
        converter = self._make_converter()
        log.info("running Docling on %s (first run downloads models)", pdf_path.name)
        try:
            result = converter.convert(str(pdf_path))
        except Exception as exc:  # pragma: no cover - depends on the model stack
            raise ParserError(f"Docling failed on {pdf_path.name}: {exc}") from exc

        dl_doc = result.document
        doc = Document(
            source_path=str(pdf_path), parser=self.name, metadata=Metadata()
        )
        pages = getattr(dl_doc, "pages", {}) or {}
        doc.page_count = len(pages)
        page_heights = {
            number: getattr(getattr(page, "size", None), "height", 0.0)
            for number, page in pages.items()
        }

        items = self._flatten(dl_doc, page_heights)
        captions = self._group_captions(items)
        self._build_assets(doc, items, captions, dl_doc)
        self._emit_blocks(doc, items)
        self._finish_metadata(doc, pdf_path)
        return doc

    # -- Docling setup -----------------------------------------------------
    def _make_converter(self):
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        pipeline = PdfPipelineOptions()
        pipeline.do_table_structure = True
        pipeline.do_formula_enrichment = (
            self.options.formulas == "latex"
            and self.options.formula_backend == "docling"
        )
        pipeline.do_ocr = self.options.ocr
        pipeline.generate_page_images = False
        pipeline.generate_picture_images = False
        return DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline)
            }
        )

    # -- stage 1: flatten --------------------------------------------------
    def _flatten(self, dl_doc, page_heights: dict[int, float]) -> list[_Item]:
        items: list[_Item] = []
        for item, nesting in dl_doc.iterate_items():
            label = _label_of(item)
            if label in _SKIP_LABELS:
                continue
            items.append(
                _Item(
                    label=label,
                    text=normalize_text(getattr(item, "text", "") or ""),
                    bbox=_bbox_of(item, page_heights),
                    nesting=nesting,
                    item=item,
                )
            )
        return items

    # -- stage 2: captions -------------------------------------------------
    def _group_captions(self, items: list[_Item]) -> list[_CaptionGroup]:
        """Find caption labels and glue split captions back together.

        Docling emits "FIGURE 2" and its text as two items, and sometimes
        classifies the label as a section header.  Both cases are handled.
        """
        groups: list[_CaptionGroup] = []
        for index, entry in enumerate(items):
            if entry.used or entry.label in _GRAPHIC_LABELS or entry.label == "table":
                continue
            if entry.label not in ("caption", "section_header", "text", "paragraph"):
                continue
            parsed = parse_caption_label(entry.text)
            if not parsed:
                continue
            kind, number, supplementary, label, rest = parsed
            if entry.label in ("text", "paragraph"):
                # Docling did not call this a caption, so be strict: a real
                # caption starts a new sentence after the label, while
                # "Figure 3 shows the result" carries straight on.
                if len(rest) < 10 or not rest[:1].isupper():
                    continue
            members = [entry]
            if len(rest) < 10:
                rest = self._caption_continuation(items, index, members)
            if not rest:
                continue
            for member in members:
                member.used = True
            groups.append(
                _CaptionGroup(
                    kind=kind,
                    number=number,
                    supplementary=supplementary,
                    label=label,
                    text=rest,
                    bbox=entry.bbox,
                    items=members,
                )
            )
        return groups

    @staticmethod
    def _caption_continuation(
        items: list[_Item], index: int, members: list[_Item]
    ) -> str:
        """Take the next item as the caption body when the label stands alone."""
        for candidate in items[index + 1 : index + 3]:
            if candidate.used or candidate.label not in ("caption", "text", "paragraph"):
                continue
            if parse_caption_label(candidate.text):
                break
            if candidate.bbox and items[index].bbox:
                if candidate.bbox.page != items[index].bbox.page:
                    break
            if len(candidate.text) < 10:
                continue
            members.append(candidate)
            return candidate.text
        return ""

    # -- stage 3: assets ---------------------------------------------------
    def _build_assets(
        self,
        doc: Document,
        items: list[_Item],
        captions: list[_CaptionGroup],
        dl_doc,
    ) -> None:
        graphics = [
            entry for entry in items
            if entry.label in _GRAPHIC_LABELS and entry.bbox is not None
        ]
        tables = [
            entry for entry in items
            if entry.label == "table" and entry.bbox is not None
        ]

        for group in captions:
            pool = tables if group.kind == "table" else graphics
            match = _nearest_graphic(pool, group)
            if match is None and group.kind != "table":
                match = _nearest_graphic(tables, group)
            if match is None:
                doc.add_warning(
                    f"Docling found no graphic for {group.label}; "
                    "the caption is kept as text"
                )
                continue
            match.used = True
            self._asset_order += 1
            asset = Asset(
                key=normalize_key(group.kind, group.number, group.supplementary),
                kind=group.kind,  # type: ignore[arg-type]
                label=group.label,
                number=group.number,
                caption=group.text,
                supplementary=group.supplementary,
                bboxes=[match.bbox] if match.bbox else [],
                order=self._asset_order,
            )
            if group.kind == "table" and self.options.tables == "html":
                asset.html_table = _table_html(match.item, dl_doc)
            doc.assets.append(asset)

        for entry in graphics + tables:
            if not entry.used:
                # Journal logos, badges and decorative rules come back as
                # pictures; without a caption they are not figures.
                log.debug("dropping uncaptioned graphic on page %s", entry.page + 1)
                entry.used = True

    # -- stage 4: blocks ---------------------------------------------------
    def _emit_blocks(self, doc: Document, items: list[_Item]) -> None:
        skip_next_front_matter = False
        for entry in items:
            if entry.label in _GRAPHIC_LABELS or entry.label == "table":
                continue
            if entry.used:
                continue  # consumed as a caption
            if not entry.text:
                continue

            first_page = entry.page == 0
            if skip_next_front_matter and first_page:
                skip_next_front_matter = False
                continue
            if first_page and FRONT_MATTER_LABEL_RE.match(entry.text):
                skip_next_front_matter = True
                continue
            if first_page and FRONT_MATTER_RE.match(entry.text):
                continue

            block_type = _LABEL_MAP.get(entry.label)
            if block_type is None:
                continue

            if block_type == "title":
                if not doc.metadata.title:
                    doc.metadata.title = entry.text
                continue

            block = self._make_block(block_type, entry.text, entry.bbox)
            if block_type == "heading":
                if (
                    not doc.metadata.title
                    and entry.page == 0
                    and not doc.blocks
                    and _could_be_a_title(entry.text)
                ):
                    doc.metadata.title = entry.text
                    continue
                block.level = _heading_level(entry)
            elif block_type == "equation":
                self._add_equation_asset(doc, block, entry.text)
            elif block_type == "footnote":
                doc.footnotes.append(
                    Footnote(key=f"fn-{len(doc.footnotes) + 1}", text=entry.text)
                )
            doc.blocks.append(block)

    def _add_equation_asset(self, doc: Document, block: Block, text: str) -> None:
        tail = _EQUATION_TAIL_RE.search(text)
        number = tail.group(1) if tail else str(len(doc.equations) + 1)
        key = normalize_key("equation", number)
        block.asset_key = key
        self._asset_order += 1
        doc.assets.append(
            Asset(
                key=key,
                kind="equation",
                label=f"Equation {number}",
                number=number,
                bboxes=[block.bbox] if block.bbox else [],
                order=self._asset_order,
                home_block_id=block.id,
            )
        )

    # -- helpers -----------------------------------------------------------
    def _make_block(self, block_type: str, text: str, bbox: BBox | None) -> Block:
        self._counter += 1
        return Block(
            id=self._block_id(self._counter),
            type=block_type,  # type: ignore[arg-type]
            text=text,
            page=bbox.page if bbox else 0,
            bbox=bbox,
        )

    def _finish_metadata(self, doc: Document, pdf_path: Path) -> None:
        import pymupdf

        meta = doc.metadata
        with pymupdf.open(pdf_path) as pdf:
            pdf_meta = pdf.metadata or {}
            doc.page_count = doc.page_count or pdf.page_count
            first_page_text = pdf[0].get_text() if pdf.page_count else ""

        if not meta.title:
            meta.title = normalize_text(pdf_meta.get("title") or "") or pdf_path.stem
        if not meta.authors:
            meta.authors = _authors_from_blocks(doc) or [
                a.strip()
                for a in (pdf_meta.get("author") or "").replace(";", ",").split(",")
                if a.strip()
            ]
        meta.doi = meta.doi or extract_doi(first_page_text)
        meta.year = meta.year or guess_year(first_page_text)
        if not meta.abstract:
            meta.abstract = _extract_abstract(doc)
        _extract_references(doc)


# --------------------------------------------------------------------------
# module level helpers
# --------------------------------------------------------------------------


def _label_of(item) -> str:
    label = getattr(item, "label", "")
    return str(getattr(label, "value", label) or "").lower()


def _bbox_of(item, page_heights: dict[int, float]) -> BBox | None:
    provenance = getattr(item, "prov", None) or []
    if not provenance:
        return None
    first = provenance[0]
    box = getattr(first, "bbox", None)
    page_no = getattr(first, "page_no", 1)
    if box is None:
        return None
    height = page_heights.get(page_no, 0.0)
    origin = str(getattr(box, "coord_origin", "TOPLEFT"))
    if "BOTTOM" in origin.upper() and height:
        # Docling reports bottom-left coordinates; PyMuPDF wants top-left.
        y0, y1 = height - box.t, height - box.b
    else:
        y0, y1 = box.t, box.b
    return BBox(
        page=max(0, page_no - 1),
        x0=min(box.l, box.r),
        y0=min(y0, y1),
        x1=max(box.l, box.r),
        y1=max(y0, y1),
    )


def _nearest_graphic(pool: list[_Item], group: _CaptionGroup) -> _Item | None:
    """The closest unused graphic on the caption's page."""
    if group.bbox is None:
        return None
    best: tuple[float, _Item] | None = None
    for entry in pool:
        if entry.used or entry.bbox is None or entry.bbox.page != group.bbox.page:
            continue
        if entry.bbox.y1 <= group.bbox.y0:
            distance = group.bbox.y0 - entry.bbox.y1      # graphic above
        elif entry.bbox.y0 >= group.bbox.y1:
            distance = entry.bbox.y0 - group.bbox.y1      # graphic below
        else:
            distance = 0.0                                 # overlapping
        if distance > _CAPTION_DISTANCE:
            continue
        if best is None or distance < best[0]:
            best = (distance, entry)
    return best[1] if best else None


#: Section names that must never be mistaken for the paper's title when
#: Docling reports no title item of its own.
_SECTION_NAMES = {
    "abstract", "introduction", "background", "results", "discussion",
    "results and discussion", "conclusion", "conclusions", "methods",
    "materials and methods", "experimental section", "experimental",
    "methodology", "references", "bibliography", "summary", "theory",
    "acknowledgements", "acknowledgments", "supporting information",
    "graphical abstract", "highlights", "significance", "keywords",
}


def _could_be_a_title(text: str) -> bool:
    if heading_key(text) in _SECTION_NAMES:
        return False
    if _NUMBERED_HEADING_RE.match(text):
        return False          # "1. Introduction" is a section, not a title
    return 10 < len(text) < 400


def _heading_level(entry: _Item) -> int:
    """Docling reports every heading at level 1; recover the real depth."""
    numbered = _NUMBERED_HEADING_RE.match(entry.text)
    if numbered:
        return min(numbered.group(1).count(".") + 1, 4)
    level = getattr(entry.item, "level", None)
    if isinstance(level, int) and level > 1:
        return min(level, 4)
    return 1


def _table_html(item, dl_doc) -> str | None:
    exporter = getattr(item, "export_to_html", None)
    if not callable(exporter):
        return None
    for call in (lambda: exporter(doc=dl_doc), exporter):
        try:
            return call()
        except Exception:  # pragma: no cover - docling-core API drift
            continue
    return None


def _authors_from_blocks(doc: Document) -> list[str]:
    """The first short paragraph on page one is usually the author list."""
    for block in doc.blocks[:4]:
        if block.page != 0 or block.type not in ("paragraph", "author"):
            continue
        text = block.text
        if not 10 < len(text) < 500 or "." not in text and "," not in text:
            continue
        parts = [
            re.sub(r"[\d*†‡§¶#,]+$", "", part).strip()
            for part in re.split(r"\s*(?:,| and |&|;)\s*", text)
        ]
        parts = [p for p in parts if 3 <= len(p) <= 60 and " " in p]
        upper = sum(1 for p in parts if p[:1].isupper())
        if len(parts) >= 2 and upper >= max(1, int(len(parts) * 0.8)):
            return parts[:30]
    return []




def _extract_abstract(doc: Document) -> str:
    for index, block in enumerate(doc.blocks):
        lowered = heading_key(block.text)
        if block.type == "heading" and lowered.startswith("abstract"):
            if index + 1 < len(doc.blocks):
                return doc.blocks[index + 1].text
        if re.match(r"^\s*abstract\b", block.text, re.IGNORECASE) and len(block.text) > 80:
            return re.sub(
                r"^\s*abstract[:.\s—–-]*", "", block.text, flags=re.IGNORECASE
            )
    # No explicit label: the first long paragraph before the first heading.
    for block in doc.blocks:
        if block.type == "heading":
            break
        if block.type == "paragraph" and block.page == 0 and len(block.text) > 300:
            return block.text
    return ""


def _extract_references(doc: Document) -> None:
    """Build Reference objects from the bibliography blocks.

    Three shapes have to be covered: a "References" heading followed by
    entries (handled below), a run of "[1] ... [2] ..." with no heading at
    all, and a list Docling already split into items with the markers
    stripped. The precise heading-based scan goes first.
    """
    entries: list[Reference] = []
    consumed: set[str] = set()
    in_references = False

    for block in doc.blocks:
        if block.type == "heading":
            if heading_key(block.text) in {
                "references", "bibliography", "literature cited",
            }:
                # Sticky: back matter printed after the bibliography must not
                # switch detection off again.
                in_references = True
                consumed.add(block.id)
            continue
        if block.type != "reference_entry" and not in_references:
            continue
        if block.type not in ("reference_entry", "paragraph", "list_item"):
            continue
        text = block.text.strip()
        if not text:
            continue
        # Past the heading only entry-shaped blocks count, so an "Author
        # contributions" paragraph is not mistaken for a source.
        marker = ENTRY_MARKER_RE.match(text)
        if in_references and block.type != "reference_entry":
            if not (marker or AUTHOR_YEAR_ENTRY_RE.match(text)):
                continue
        consumed.add(block.id)
        number = (
            next((g for g in marker.groups() if g), "")
            if marker
            else str(len(entries) + 1)
        )
        entries.append(
            make_reference(number, text[marker.end():] if marker else text)
        )

    if entries:
        doc.references = entries
        # The bibliography gets its own chapter, so drop it from the flow.
        doc.blocks = [b for b in doc.blocks if b.id not in consumed]
        return

    doc.references = harvest_numeric_bibliography(doc) or harvest_list_bibliography(
        doc
    )
