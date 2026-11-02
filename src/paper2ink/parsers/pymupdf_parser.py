"""A dependency-free layout parser built on PyMuPDF.

Docling is the better parser for difficult layouts, but it drags in a large
ML stack and needs a model download on first use.  This backend works with
nothing but PyMuPDF, is fast, and is a useful fallback when Docling mis-reads
a layout.  It is heuristic by design:

* columns are found by looking for a vertical whitespace gutter;
* reading order is "column by column between full-width blocks";
* headings, captions, equations and reference entries are classified from
  font size, position and the caption label regexes in :mod:`paper2ink.refs`;
* figure regions come from clustering vector drawings and raster images,
  tables from :meth:`Page.find_tables`.
"""

from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from ..cleanup import (
    FRONT_MATTER_LABEL_RE,
    FRONT_MATTER_RE,
    detect_running_heads,
    head_signature,
    heading_key,
    join_lines,
    normalize_text,
)
from ..bibliography import (
    AUTHOR_YEAR_ENTRY_RE,
    ENTRY_MARKER_RE,
    harvest_list_bibliography,
    harvest_numeric_bibliography,
    is_bracketed_reference_run,
    make_reference,
    split_bracketed_references,
)
from ..model import Asset, BBox, Block, Document, Metadata
from ..refs import extract_doi, normalize_key, parse_caption_label
from .base import BaseParser, ParseOptions

log = logging.getLogger("paper2ink.parser.pymupdf")

# Headings that papers use regardless of numbering.
_KNOWN_HEADINGS = {
    "abstract", "introduction", "background", "results", "discussion",
    "results and discussion", "conclusion", "conclusions", "methods",
    "materials and methods", "experimental section", "experimental",
    "methodology", "references", "bibliography", "acknowledgement",
    "acknowledgements", "acknowledgment", "acknowledgments",
    "supporting information", "supplementary information", "author contributions",
    "conflict of interest", "data availability", "funding", "notes",
    "theory", "model", "simulation details", "computational details",
    "summary", "outlook", "literature cited",
}

_NUMBERED_HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+(\S.*)$")
_EQUATION_TAIL_RE = re.compile(r"\(\s*(S?\d+[a-z]?)\s*\)\s*$")
_MATH_CHARS = set("=+−×÷±∑∫∂∇√≈≤≥≠∞αβγδεζηθκλμνξπρστφχψωΔΘΛΞΠΣΦΨΩ⟨⟩‖∥→←↔∈∝⋅·^_/")
_AUTHOR_SPLIT_RE = re.compile(r"\s*(?:,| and |&|;)\s*", re.IGNORECASE)

# --------------------------------------------------------------------------
# intermediate page structures
# --------------------------------------------------------------------------


@dataclass
class _TextBlock:
    page: int
    rect: pymupdf.Rect
    lines: list[str]
    size: float
    bold: bool
    column: int = 0
    spanning: bool = False
    role: str = ""          # "title" / "authors" once used for metadata

    @property
    def text(self) -> str:
        return join_lines(self.lines)

    @property
    def bbox(self) -> BBox:
        return BBox(self.page, *self.rect)


@dataclass
class _PageInfo:
    number: int
    rect: pymupdf.Rect
    blocks: list[_TextBlock] = field(default_factory=list)
    graphics: list[pymupdf.Rect] = field(default_factory=list)
    tables: list[tuple[pymupdf.Rect, list[list[str | None]]]] = field(
        default_factory=list
    )
    columns: int = 1
    gutter: float = 0.0
    # Rects of running heads / front matter; asset crops must avoid them.
    excluded: list[pymupdf.Rect] = field(default_factory=list)


# --------------------------------------------------------------------------
# the parser
# --------------------------------------------------------------------------


class PyMuPDFParser(BaseParser):
    """Heuristic layout parser that needs no machine-learning models."""

    name = "pymupdf"
    description = "fast heuristic layout parser (no ML models needed)"

    def __init__(self, options: ParseOptions | None = None) -> None:
        super().__init__(options)
        self._counter = 0

    # -- entry point -------------------------------------------------------
    def parse(self, pdf_path: Path) -> Document:
        doc = Document(
            source_path=str(pdf_path), parser=self.name, metadata=Metadata()
        )
        with pymupdf.open(pdf_path) as pdf:
            doc.page_count = pdf.page_count
            pages = [self._read_page(pdf[i]) for i in range(pdf.page_count)]
            heads = self._running_heads(pages)
            self._apply_metadata(doc, pdf, pages, heads)
            ordered = self._ordered_blocks(pages, heads)
            self._build_blocks(doc, ordered, pages)
            self._attach_assets(doc, pages)
        return doc

    # -- page reading ------------------------------------------------------
    def _read_page(self, page: pymupdf.Page) -> _PageInfo:
        info = _PageInfo(number=page.number, rect=page.rect)
        raw = page.get_text("dict")
        for raw_block in raw.get("blocks", []):
            if raw_block.get("type") != 0:
                continue
            lines: list[str] = []
            sizes: list[float] = []
            bold = 0
            total = 0
            for line in raw_block.get("lines", []):
                text = _join_spans(line.get("spans", [])).rstrip()
                if not text.strip():
                    continue
                lines.append(text)
                for span in line.get("spans", []):
                    weight = max(1, len(span["text"]))
                    sizes.extend([span["size"]] * weight)
                    total += weight
                    if span.get("flags", 0) & 16 or "bold" in span["font"].lower():
                        bold += weight
            if not lines:
                continue
            info.blocks.append(
                _TextBlock(
                    page=page.number,
                    rect=pymupdf.Rect(raw_block["bbox"]),
                    lines=lines,
                    size=statistics.median(sizes) if sizes else 0.0,
                    bold=total > 0 and bold / total > 0.6,
                )
            )

        info.graphics = _graphic_clusters(page)
        info.tables = _find_tables(page)
        info.columns, info.gutter = _detect_columns(info)
        _assign_columns(info)
        return info

    # -- metadata ----------------------------------------------------------
    def _apply_metadata(
        self,
        doc: Document,
        pdf: pymupdf.Document,
        pages: list[_PageInfo],
        heads: set[str],
    ) -> None:
        meta = doc.metadata
        pdf_meta = pdf.metadata or {}

        first = pages[0] if pages else None
        title_block = None
        if first and first.blocks:
            # The title is normally the largest text in the upper half.
            upper = [
                b
                for b in first.blocks
                if b.rect.y0 < first.rect.height * 0.55 and len(b.text) > 12
            ]
            if upper:
                title_block = max(upper, key=lambda b: (round(b.size, 1), -b.rect.y0))
        if title_block and len(title_block.text) < 400:
            meta.title = normalize_text(title_block.text)
            title_block.role = "title"
        if not meta.title:
            meta.title = normalize_text(pdf_meta.get("title") or "")
        if not meta.title:
            meta.title = Path(doc.source_path).stem

        if first:
            meta.authors = _guess_authors(first, title_block)
            meta.abstract = _guess_abstract(first)
            full_text = "\n".join(b.text for b in first.blocks)
            meta.doi = extract_doi(full_text) or extract_doi(
                pdf_meta.get("subject") or ""
            )
            meta.journal = _journal_from_running_heads(pages, heads) or _guess_journal(
                first
            )
            meta.year = _guess_year(full_text)
        if not meta.authors:
            author = pdf_meta.get("author") or ""
            meta.authors = [a for a in _AUTHOR_SPLIT_RE.split(author) if a.strip()]

    # -- layout ------------------------------------------------------------
    def _running_heads(self, pages: list[_PageInfo]) -> set[str]:
        """Signatures of blocks that sit in the header/footer band everywhere."""
        candidates: dict[int, list[str]] = {}
        for info in pages:
            height = info.rect.height
            texts: list[str] = []
            for block in info.blocks:
                near_top = block.rect.y0 < info.rect.y0 + height * 0.09
                near_bottom = block.rect.y1 > info.rect.y1 - height * 0.08
                if (near_top or near_bottom) and len(block.lines) <= 3:
                    texts.append(" ".join(block.lines))
            candidates[info.number] = texts
        return detect_running_heads(candidates)

    def _ordered_blocks(
        self, pages: list[_PageInfo], heads: set[str]
    ) -> list[_TextBlock]:
        ordered: list[_TextBlock] = []
        for info in pages:
            keep = []
            for block in info.blocks:
                dropped = head_signature(" ".join(block.lines)) in heads or (
                    _is_front_matter(block, info)
                )
                if dropped:
                    info.excluded.append(block.rect)
                    continue
                if _is_inside_graphic(block, info):
                    continue
                keep.append(block)
            ordered.extend(_drop_label_values(_reading_order(keep, info), info))
        return ordered

    # -- block classification ---------------------------------------------
    def _build_blocks(
        self, doc: Document, blocks: list[_TextBlock], pages: list[_PageInfo]
    ) -> None:
        body_size = _body_font_size(pages)
        page_rects = {info.number: info.rect for info in pages}
        in_references = False
        expect_abstract = False
        seen_heading = False

        for raw in blocks:
            text = raw.text
            if not text or raw.role:
                continue

            caption = parse_caption_label(text)
            heading_level = 0 if caption else _heading_level(raw, body_size)
            lowered = heading_key(text)

            if lowered in _KNOWN_HEADINGS or heading_level:
                if lowered in {"references", "bibliography", "literature cited"}:
                    # Sticky: back matter after the bibliography must not
                    # switch reference detection off again.
                    in_references = True
                if lowered == "abstract":
                    expect_abstract = True
                    continue
                if lowered in _KNOWN_HEADINGS or _NUMBERED_HEADING_RE.match(text):
                    seen_heading = True
                doc.blocks.append(
                    self._make_block(raw, "heading", text, level=heading_level or 1)
                )
                continue

            if caption and len(text) > len(caption[3]) + 4:
                kind, number, supplementary, _label, _rest = caption
                block = self._make_block(raw, "caption", text)
                block.asset_key = normalize_key(kind, number, supplementary)
                doc.blocks.append(block)
                continue

            if expect_abstract and len(text) > 60:
                expect_abstract = False
                doc.metadata.abstract = _strip_abstract_label(text)
                continue

            if _looks_like_abstract(text):
                doc.metadata.abstract = _strip_abstract_label(text)
                continue

            # Papers that print no "References" heading still start the
            # bibliography with "[1] ...".
            if is_bracketed_reference_run(text):
                in_references = True
                doc.blocks.append(self._make_block(raw, "reference_entry", text))
                continue

            if in_references and _looks_like_reference_entry(raw, text, body_size):
                doc.blocks.append(self._make_block(raw, "reference_entry", text))
                continue

            equation = _EQUATION_TAIL_RE.search(text)
            if equation and _looks_like_equation(text):
                block = self._make_block(raw, "equation", text)
                block.asset_key = normalize_key("equation", equation.group(1))
                doc.blocks.append(block)
                continue

            if _is_footnote(raw, text, body_size, page_rects.get(raw.page)):
                doc.blocks.append(self._make_block(raw, "footnote", text))
                continue

            # The first long block before any heading is the abstract.
            if (
                not seen_heading
                and not doc.metadata.abstract
                and raw.page == 0
                and len(text) > 300
            ):
                doc.metadata.abstract = _strip_abstract_label(text)
                continue

            doc.blocks.append(self._make_block(raw, "paragraph", text))

        _extract_references(doc)

    def _make_block(
        self, raw: _TextBlock, type_: str, text: str, level: int = 0
    ) -> Block:
        self._counter += 1
        return Block(
            id=self._block_id(self._counter),
            type=type_,  # type: ignore[arg-type]
            text=text,
            level=level,
            page=raw.page,
            bbox=raw.bbox,
            column=raw.column,
            font_size=raw.size,
        )

    # -- assets ------------------------------------------------------------
    def _attach_assets(self, doc: Document, pages: list[_PageInfo]) -> None:
        by_page = {info.number: info for info in pages}
        order = 0
        for block in doc.blocks:
            if block.type == "caption" and block.asset_key:
                parsed = parse_caption_label(block.text)
                if not parsed:
                    continue
                kind, number, supplementary, label, rest = parsed
                info = by_page.get(block.page)
                if info is None or block.bbox is None:
                    continue
                region = _asset_region(info, block, kind)
                if region is None:
                    # Keep the asset anyway: its caption and anchor still
                    # make the cross references work, which beats leaving
                    # every "see Table 1" in the text pointing nowhere.
                    doc.add_warning(
                        f"no image region found for {label} on page "
                        f"{block.page + 1}; keeping the caption only"
                    )
                order += 1
                asset = Asset(
                    key=block.asset_key,
                    kind=kind,  # type: ignore[arg-type]
                    label=label,
                    number=number,
                    caption=rest or block.text,
                    supplementary=supplementary,
                    bboxes=[BBox(block.page, *region)] if region else [],
                    order=order,
                    home_block_id=block.id,
                )
                if kind == "table" and region and self.options.tables == "html":
                    asset.html_table = _table_html(info, region)
                doc.assets.append(asset)

            elif block.type == "equation" and block.asset_key:
                order += 1
                doc.assets.append(
                    Asset(
                        key=block.asset_key,
                        kind="equation",
                        label=f"Equation {block.asset_key.split('-', 1)[1]}",
                        number=block.asset_key.split("-", 1)[1],
                        caption="",
                        bboxes=[block.bbox] if block.bbox else [],
                        order=order,
                        home_block_id=block.id,
                    )
                )

        _merge_duplicate_assets(doc)


# --------------------------------------------------------------------------
# layout helpers
# --------------------------------------------------------------------------


#: PyMuPDF sets bit 0 of a span's flags for superscript text.
_SUPERSCRIPT_FLAG = 1

#: A superscript citation run: "1", "41,42", "6−21" (ACS uses the true
#: minus sign, not a hyphen).
_SUPERSCRIPT_CITATION_RE = re.compile(r"^\d[\d\s,;−–—-]*$")


def _join_spans(spans: list[dict]) -> str:
    """Join a line's spans, turning superscript citations into "[1,2]".

    ACS and Nature set citations as superscript digits, which extract as
    digits glued to the preceding word ("method.41,42") and are then
    indistinguishable from ordinary numbers.  Rewriting the run in bracket
    form keeps the meaning, reads well on an e-ink screen, and lets the
    normal citation detection in :mod:`paper2ink.refs` do the rest.
    """
    parts: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if not run:
            return
        text = "".join(run)
        run.clear()
        core = text.strip()
        if not core or core.startswith("["):
            parts.append(text)          # Wiley already brackets its markers
            return
        if not _SUPERSCRIPT_CITATION_RE.match(core):
            parts.append(text)          # a footnote dagger or an exponent
            return
        leading = text[: len(text) - len(text.lstrip())]
        trailing = text[len(text.rstrip()) :]
        parts.append(f"{leading}[{core}]{trailing}")

    for span in spans:
        if span.get("flags", 0) & _SUPERSCRIPT_FLAG:
            run.append(span["text"])
        else:
            flush()
            parts.append(span["text"])
    flush()
    return "".join(parts)


def _graphic_clusters(page: pymupdf.Page, pad: float = 6.0) -> list[pymupdf.Rect]:
    """Cluster vector drawings and images into candidate figure regions."""
    rects: list[pymupdf.Rect] = []
    try:
        for xref, *_ in page.get_images(full=True):
            rects.extend(page.get_image_rects(xref))
    except Exception:  # pragma: no cover - defensive, some PDFs lie
        log.debug("image rects failed on page %s", page.number)
    try:
        for drawing in page.get_drawings():
            rect = pymupdf.Rect(drawing["rect"])
            if rect.width > 3 and rect.height > 3:
                rects.append(rect)
    except Exception:  # pragma: no cover
        log.debug("drawings failed on page %s", page.number)

    rects = [r for r in rects if r.width > 3 and r.height > 3 and r.is_valid]
    if not rects:
        return []

    clusters: list[pymupdf.Rect] = []
    for rect in sorted(rects, key=lambda r: (r.y0, r.x0)):
        padded = pymupdf.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad)
        for i, cluster in enumerate(clusters):
            if cluster.intersects(padded):
                clusters[i] = cluster | rect
                break
        else:
            clusters.append(pymupdf.Rect(rect))

    # A second pass merges clusters that only became adjacent above.
    merged = True
    while merged:
        merged = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                a = pymupdf.Rect(clusters[i])
                a.x0 -= pad
                a.y0 -= pad
                a.x1 += pad
                a.y1 += pad
                if a.intersects(clusters[j]):
                    clusters[i] = clusters[i] | clusters[j]
                    del clusters[j]
                    merged = True
                    break
            if merged:
                break

    page_area = page.rect.get_area()
    return [c for c in clusters if c.get_area() > page_area * 0.004]


def _find_tables(page: pymupdf.Page) -> list[tuple[pymupdf.Rect, list]]:
    try:
        finder = page.find_tables()
    except Exception:  # pragma: no cover - find_tables is best effort
        return []
    out = []
    for table in getattr(finder, "tables", []):
        try:
            out.append((pymupdf.Rect(table.bbox), table.extract()))
        except Exception:  # pragma: no cover
            continue
    return out


def _detect_columns(info: _PageInfo) -> tuple[int, float]:
    """Return ``(number of columns, x position of the gutter)``.

    The gutter is the widest vertical band of whitespace that no text block
    crosses.  Looking for it instead of assuming the middle of the page is
    what makes journal layouts with a narrow metadata sidebar (Frontiers,
    PLOS) come out in the right reading order.
    """
    width = info.rect.width
    mid = info.rect.x0 + width / 2
    if len(info.blocks) < 6:
        return 1, mid

    # Full-width running heads, footers and spanning figures must not veto a
    # gutter: they cross every candidate position by design.
    candidates_blocks = [
        b for b in info.blocks if b.rect.width <= width * 0.8
    ]
    if len(candidates_blocks) < 6:
        return 1, mid

    best: tuple[float, float] | None = None  # (clearance, position)
    step = 2.0
    position = info.rect.x0 + width * 0.12
    limit = info.rect.x0 + width * 0.88
    while position <= limit:
        clearance = width
        straddled = False
        left = right = 0
        for block in candidates_blocks:
            if block.rect.x0 < position - 1 and block.rect.x1 > position + 1:
                straddled = True
                break
            if block.rect.x1 <= position:
                left += 1
                clearance = min(clearance, position - block.rect.x1)
            else:
                right += 1
                clearance = min(clearance, block.rect.x0 - position)
        if not straddled and left >= 3 and right >= 3:
            if best is None or clearance > best[0]:
                best = (clearance, position)
        position += step

    if best and best[0] >= 4.0:
        return 2, best[1]

    # No clean gutter (a wide abstract or table crosses every candidate):
    # fall back to the middle of the page but keep two-column ordering if
    # both halves are populated.
    tolerance = width * 0.04
    left_blocks = [b for b in info.blocks if b.rect.x1 <= mid + tolerance]
    right_blocks = [b for b in info.blocks if b.rect.x0 >= mid - tolerance]
    if len(left_blocks) >= 3 and len(right_blocks) >= 3:
        return 2, mid
    return 1, mid


def _assign_columns(info: _PageInfo) -> None:
    if info.columns == 1:
        for block in info.blocks:
            block.column = 0
            block.spanning = True
        return
    width = info.rect.width
    for block in info.blocks:
        if block.rect.width > width * 0.62:
            block.spanning = True
            block.column = 0
        else:
            # Decide by the block centre: a wide left-column block may reach
            # past the gutter without belonging to the right column.
            centre = (block.rect.x0 + block.rect.x1) / 2
            block.column = 0 if centre < info.gutter else 1


def _reading_order(blocks: list[_TextBlock], info: _PageInfo) -> list[_TextBlock]:
    """Column-by-column order, with full-width blocks acting as separators."""
    if info.columns == 1:
        return sorted(blocks, key=lambda b: (round(b.rect.y0, 1), b.rect.x0))

    ordered: list[_TextBlock] = []
    band: list[_TextBlock] = []

    def flush() -> None:
        if not band:
            return
        for column in (0, 1):
            ordered.extend(
                sorted(
                    (b for b in band if b.column == column),
                    key=lambda b: (round(b.rect.y0, 1), b.rect.x0),
                )
            )
        band.clear()

    for block in sorted(blocks, key=lambda b: (round(b.rect.y0, 1), b.rect.x0)):
        if block.spanning:
            flush()
            ordered.append(block)
        else:
            band.append(block)
    flush()
    return ordered


def _body_font_size(pages: list[_PageInfo]) -> float:
    sizes: list[float] = []
    for info in pages:
        for block in info.blocks:
            if len(block.text) > 120:
                sizes.extend([block.size] * len(block.lines))
    if not sizes:
        sizes = [b.size for info in pages for b in info.blocks if b.size]
    return statistics.median(sizes) if sizes else 10.0


def _heading_level(raw: _TextBlock, body_size: float) -> int:
    text = raw.text.strip()
    if len(text) > 160 or not text:
        return 0
    numbered = _NUMBERED_HEADING_RE.match(text)
    if numbered and (raw.bold or raw.size > body_size * 1.02):
        depth = numbered.group(1).count(".") + 1
        return min(depth, 4)
    if text.lower().rstrip(" .:") in _KNOWN_HEADINGS:
        return 1
    if raw.size > body_size * 1.12 and len(text) < 90 and not text.endswith("."):
        return 1 if raw.size > body_size * 1.3 else 2
    if raw.bold and len(text) < 70 and not text.endswith("."):
        return 3
    return 0


def _looks_like_abstract(text: str) -> bool:
    return bool(re.match(r"^\s*abstract\b", text, re.IGNORECASE)) and len(text) > 80


def _strip_abstract_label(text: str) -> str:
    return normalize_text(
        re.sub(r"^\s*abstract[:.\s—–-]*", "", text, flags=re.IGNORECASE)
    )


def _is_front_matter(block: _TextBlock, info: _PageInfo) -> bool:
    """Journal front matter (editor, copyright, received dates, ...)."""
    if info.number != 0:
        return False
    if FRONT_MATTER_LABEL_RE.match(block.text):
        # Handled by _drop_label_values, which also removes the value block.
        return False
    return bool(FRONT_MATTER_RE.match(block.text))


def _drop_label_values(
    blocks: list[_TextBlock], info: _PageInfo
) -> list[_TextBlock]:
    """Drop "KEYWORDS" style labels together with the block that follows."""
    if info.number != 0:
        return blocks
    out: list[_TextBlock] = []
    skip_next = False
    for block in blocks:
        if skip_next:
            skip_next = False
            continue
        if FRONT_MATTER_LABEL_RE.match(block.text):
            skip_next = True
            continue
        out.append(block)
    return out


def _is_inside_graphic(block: _TextBlock, info: _PageInfo, share: float = 0.75) -> bool:
    """Drop axis labels and other text that belongs inside a figure crop."""
    if parse_caption_label(block.text):
        return False
    if len(block.text) > 180:
        return False
    area = block.rect.get_area()
    if area <= 0:
        return False
    for cluster in info.graphics:
        overlap = (block.rect & cluster).get_area()
        if overlap / area >= share:
            return True
    return False


def _is_footnote(
    raw: _TextBlock, text: str, body_size: float, page_rect: pymupdf.Rect | None
) -> bool:
    if not raw.size or raw.size >= body_size * 0.86:
        return False
    if page_rect is not None:
        in_bottom_band = raw.rect.y0 > page_rect.y1 - page_rect.height * 0.22
        if not in_bottom_band:
            return False
    return len(text) < 1200


def _looks_like_reference_entry(
    raw: _TextBlock, text: str, body_size: float
) -> bool:
    if ENTRY_MARKER_RE.match(text):
        return True
    if AUTHOR_YEAR_ENTRY_RE.match(text):
        return True
    return bool(raw.size) and raw.size < body_size * 0.92




def _looks_like_equation(text: str) -> bool:
    body = _EQUATION_TAIL_RE.sub("", text).strip()
    if not body or len(body) > 400:
        return False
    if body.endswith((".", ",", ";")) and len(body) > 120:
        return False
    math_chars = sum(1 for ch in body if ch in _MATH_CHARS)
    letters = sum(1 for ch in body if ch.isalpha())
    spaces = body.count(" ")
    if math_chars >= 1 and letters < 160:
        return True
    # Equations rarely read like prose: few spaces per character.
    return len(body) < 90 and spaces <= len(body) / 8


def _asset_region(
    info: _PageInfo, caption: Block, kind: str
) -> tuple[float, float, float, float] | None:
    """Find the page region belonging to a caption."""
    assert caption.bbox is not None
    cap = pymupdf.Rect(caption.bbox.as_tuple())

    bounds = _column_bounds(info, cap)
    above = kind != "table"

    if kind == "table":
        region = _table_region(info, cap)
        if region is not None:
            return _clip_region(info, tuple(region), cap, above)

    candidates = [
        rect
        for rect in info.graphics
        if _horizontal_overlap(rect, bounds) > 0.35
        and (rect.y1 <= cap.y0 + 4 if above else rect.y0 >= cap.y1 - 4)
        and not rect.intersects(cap)
    ]
    if candidates:
        candidates.sort(key=lambda r: abs(cap.y0 - r.y1) if above else abs(r.y0 - cap.y1))
        region = pymupdf.Rect(candidates[0])
        for rect in candidates[1:]:
            gap = (
                region.y0 - rect.y1 if above and rect.y1 <= region.y0
                else rect.y0 - region.y1 if not above and rect.y0 >= region.y1
                else 0.0
            )
            if -4 <= gap <= 45:
                region |= rect
        return _clip_region(info, tuple(region), cap, above)

    # Fall back to the whitespace gap next to the caption.
    gap_region = _gap_region(info, cap, bounds, above)
    if gap_region is None:
        return None
    return _clip_region(info, gap_region, cap, above)


def _clip_region(
    info: _PageInfo,
    region: tuple[float, float, float, float],
    cap: pymupdf.Rect,
    above: bool,
) -> tuple[float, float, float, float] | None:
    """Keep running heads, footers and the caption itself out of the crop."""
    x0, y0, x1, y1 = region
    if above:
        y1 = min(y1, cap.y0 - 2)
    else:
        y0 = max(y0, cap.y1 + 2)

    middle = (y0 + y1) / 2
    for rect in info.excluded:
        if rect.x1 <= x0 or rect.x0 >= x1:
            continue
        if rect.y1 <= middle:
            y0 = max(y0, rect.y1 + 2)
        else:
            y1 = min(y1, rect.y0 - 2)

    if y1 - y0 < 24 or x1 - x0 < 24:
        return None
    return (x0, y0, x1, y1)


def _table_region(info: _PageInfo, cap: pymupdf.Rect) -> pymupdf.Rect | None:
    best: tuple[float, pymupdf.Rect] | None = None
    for rect, _cells in info.tables:
        if rect.intersects(cap):
            continue
        distance = min(abs(rect.y0 - cap.y1), abs(cap.y0 - rect.y1))
        if distance > 120:
            continue
        if best is None or distance < best[0]:
            best = (distance, rect)
    return best[1] if best else None


def _table_html(info: _PageInfo, region: tuple[float, float, float, float]) -> str | None:
    import html as _html

    target = pymupdf.Rect(region)
    for rect, cells in info.tables:
        if not rect.intersects(target):
            continue
        if not cells:
            continue
        rows = []
        for i, row in enumerate(cells):
            tag = "th" if i == 0 else "td"
            cols = "".join(
                f"<{tag}>{_html.escape((cell or '').strip())}</{tag}>" for cell in row
            )
            rows.append(f"<tr>{cols}</tr>")
        return "<table>" + "".join(rows) + "</table>"
    return None


def _column_bounds(info: _PageInfo, cap: pymupdf.Rect) -> tuple[float, float]:
    if info.columns == 1 or cap.width > info.rect.width * 0.62:
        return info.rect.x0, info.rect.x1
    if cap.x1 <= info.gutter + info.rect.width * 0.04:
        return info.rect.x0, info.gutter
    return info.gutter, info.rect.x1


def _horizontal_overlap(rect: pymupdf.Rect, bounds: tuple[float, float]) -> float:
    left, right = bounds
    overlap = min(rect.x1, right) - max(rect.x0, left)
    return overlap / rect.width if rect.width else 0.0


def _gap_region(
    info: _PageInfo,
    cap: pymupdf.Rect,
    bounds: tuple[float, float],
    above: bool,
    min_height: float = 45.0,
) -> tuple[float, float, float, float] | None:
    left, right = bounds
    neighbours = [
        b.rect
        for b in info.blocks
        if _horizontal_overlap(b.rect, bounds) > 0.3 and not b.rect.intersects(cap)
    ]
    if above:
        tops = [r.y1 for r in neighbours if r.y1 <= cap.y0]
        y0 = max(tops) if tops else info.rect.y0 + 36
        y1 = cap.y0
    else:
        bottoms = [r.y0 for r in neighbours if r.y0 >= cap.y1]
        y0 = cap.y1
        y1 = min(bottoms) if bottoms else info.rect.y1 - 36
    if y1 - y0 < min_height:
        return None
    return (left, y0 + 1, right, y1 - 1)


def _merge_duplicate_assets(doc: Document) -> None:
    """Fold sub-figures and two-page figures under one label."""
    merged: dict[str, Asset] = {}
    out: list[Asset] = []
    for asset in doc.assets:
        existing = merged.get(asset.key)
        if existing is None:
            merged[asset.key] = asset
            out.append(asset)
            continue
        existing.bboxes.extend(asset.bboxes)
        if len(asset.caption) > len(existing.caption):
            existing.caption = asset.caption
    doc.assets = out


# --------------------------------------------------------------------------
# metadata helpers
# --------------------------------------------------------------------------


def _guess_authors(info: _PageInfo, title_block: _TextBlock | None) -> list[str]:
    if title_block is None:
        return []
    below = [
        b
        for b in info.blocks
        if b.rect.y0 >= title_block.rect.y1 - 2
        and b.rect.y0 < title_block.rect.y1 + 160
        and b is not title_block
        # Authors sit under the title, not in a sidebar next to it.
        and _horizontal_overlap(b.rect, (title_block.rect.x0, title_block.rect.x1))
        > 0.5
        and not FRONT_MATTER_RE.match(b.text)
    ]
    below.sort(key=lambda b: b.rect.y0)
    for block in below:
        text = normalize_text(block.text)
        if not text or len(text) > 500:
            continue
        if re.search(r"\babstract\b", text, re.IGNORECASE):
            continue
        parts = [
            re.sub(r"[\d*†‡§¶#,]+$", "", p).strip()
            for p in _AUTHOR_SPLIT_RE.split(text)
        ]
        parts = [p for p in parts if 3 <= len(p) <= 60 and " " in p]
        upper = sum(1 for p in parts if p[:1].isupper())
        if parts and upper >= max(1, int(len(parts) * 0.8)):
            block.role = "authors"
            return parts[:30]
    return []


def _guess_abstract(info: _PageInfo) -> str:
    for i, block in enumerate(info.blocks):
        text = block.text
        if re.match(r"^\s*abstract\b", text, re.IGNORECASE):
            stripped = re.sub(
                r"^\s*abstract[:.\s—-]*", "", text, flags=re.IGNORECASE
            )
            if len(stripped) > 60:
                return normalize_text(stripped)
            if i + 1 < len(info.blocks):
                return normalize_text(info.blocks[i + 1].text)
    return ""


def _journal_from_running_heads(
    pages: list[_PageInfo], heads: set[str]
) -> str | None:
    """Recover the journal name from the running head or footer.

    "Frontiers in Chemistry frontiersin.org 07" and "Adv. Mater. 2025, 37,
    2418462 ..." both carry the journal in front of the first number.
    """
    best: str | None = None
    for info in pages:
        for block in info.blocks:
            text = normalize_text(" ".join(block.lines))
            if head_signature(text) not in heads:
                continue
            candidate = _journal_candidate(text)
            if candidate and (best is None or len(candidate) > len(best)):
                best = candidate
    return best


def _journal_candidate(text: str) -> str | None:
    text = re.sub(r"\b(?:https?://)?(?:www\.)?\S+\.(?:com|org|de|net)\S*", " ", text)
    text = re.sub(r"\b10\.\d{4,9}/\S+", " ", text)
    words: list[str] = []
    for word in text.split():
        if any(ch.isdigit() for ch in word) or word in {"|", "©"}:
            break
        words.append(word)
    candidate = " ".join(words).strip(" .,|-")
    if not 6 <= len(candidate) <= 70 or len(words) < 2:
        return None
    letters = sum(1 for ch in candidate if ch.isalpha())
    if letters < len(candidate) * 0.7 or not candidate[0].isupper():
        return None
    if candidate.isupper():       # "RESEARCH ARTICLE", "OPEN ACCESS"
        return None
    return candidate


def _guess_journal(info: _PageInfo) -> str | None:
    height = info.rect.height
    for block in sorted(info.blocks, key=lambda b: b.rect.y0):
        if block.rect.y0 >= height * 0.12:
            break
        text = normalize_text(block.text)
        if not (4 < len(text) < 120):
            continue
        if FRONT_MATTER_RE.match(text) or text.lower().startswith("abstract"):
            continue
        return text
    return None


def _guess_year(text: str) -> str | None:
    years = re.findall(r"\b(19[89]\d|20[0-4]\d)\b", text)
    return max(years) if years else None


def _extract_references(doc: Document) -> None:
    """Turn reference_entry blocks into :class:`Reference` objects."""
    entries = harvest_numeric_bibliography(doc) or harvest_list_bibliography(doc)
    if entries:
        doc.references = entries
        return

    seen: set[str] = set()
    auto_number = 0

    def add(number: str, raw: str) -> None:
        reference = make_reference(number, raw)
        if reference.key in seen or not reference.raw:
            return
        seen.add(reference.key)
        entries.append(reference)

    for block in doc.blocks:
        if block.type != "reference_entry":
            continue
        text = block.text.strip()
        if not text:
            continue
        if is_bracketed_reference_run(text):
            for number, raw in split_bracketed_references(text):
                add(number, raw)
            continue
        match = ENTRY_MARKER_RE.match(text)
        if match:
            add(next(g for g in match.groups() if g), text[match.end():])
        else:
            auto_number += 1
            add(str(auto_number), text)

    doc.references = entries
