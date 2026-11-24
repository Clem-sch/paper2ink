"""Manual corrections to a parsed document.

Layout parsing is heuristic, and some mistakes are obvious to a reader and
invisible to a heuristic: a tinted text box looks exactly like a figure, a
figure without a caption is not recognised as one, and a two-column page can
be read in the wrong order.  Rather than pile on special cases, the review
view lets the reader say what is wrong, and those statements are collected
here.

Corrections are applied to the *parsed* document, before clean-up, so the
rest of the pipeline runs exactly as it would have if the parser had got it
right.  They are declarative and name blocks by id, which means they can be
replayed against the same parse any number of times without compounding:
rebuilding twice gives the same book as rebuilding once.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from .model import Asset, BBox, Block, Document
from .refs import normalize_key, parse_caption_label

log = logging.getLogger("paper2ink.corrections")

__all__ = [
    "Region",
    "TextBlock",
    "Corrections",
    "apply_corrections",
    "drop_id",
]

#: Reason recorded for a block the reader took out of the flow.
MANUAL_REASON = "removed by hand"


def drop_id(index: int) -> str:
    """Stable id for the *index*-th dropped block of a parse."""
    return f"drop-{index:04d}"


@dataclass
class Region:
    """A rectangle the reader drew and declared to be a figure or table."""

    page: int
    bbox: list[float]           # x0, y0, x1, y1 in PDF points
    label: str = ""
    caption: str = ""
    kind: str = "figure"        # figure | table | scheme

    def as_bbox(self) -> BBox:
        x0, y0, x1, y1 = self.bbox
        return BBox(self.page, min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


@dataclass
class TextBlock:
    """Text the reader picked up from a rectangle on the page.

    The text itself is carried here rather than re-read from the PDF when
    the correction is applied: the reader sees and can fix what was
    captured before it goes in, and a rebuild then stays deterministic.
    """

    page: int
    bbox: list[float]
    text: str
    type: str = "paragraph"

    def as_bbox(self) -> BBox:
        x0, y0, x1, y1 = self.bbox
        return BBox(self.page, min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


@dataclass
class Corrections:
    """What the reader changed about one parse."""

    #: Ids of blocks to take out of the running text.
    removed: list[str] = field(default_factory=list)
    #: Ids of dropped blocks (``drop-0007``) to bring back as body text.
    restored: list[str] = field(default_factory=list)
    #: Regions the reader drew on a page.
    regions: list[Region] = field(default_factory=list)
    #: Text the reader picked up from a rectangle the parser skipped.
    text_blocks: list[TextBlock] = field(default_factory=list)
    #: Block id -> the text the reader corrected it to.  Parsing merges a
    #: tinted box with a neighbouring caption often enough that fixing the
    #: text by hand is the only sensible repair.
    edited: dict[str, str] = field(default_factory=dict)
    #: Page number -> block ids in the order the reader wants them read.
    order: dict[int, list[str]] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (
            self.removed
            or self.restored
            or self.regions
            or self.text_blocks
            or self.edited
            or self.order
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["order"] = {str(page): ids for page, ids in self.order.items()}
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "Corrections":
        raw = raw or {}
        regions = [
            Region(
                page=int(item["page"]),
                bbox=[float(value) for value in item["bbox"]],
                label=str(item.get("label", "")).strip(),
                caption=str(item.get("caption", "")).strip(),
                kind=str(item.get("kind", "figure")),
            )
            for item in raw.get("regions", [])
            if item.get("bbox") and len(item["bbox"]) == 4
        ]
        text_blocks = [
            TextBlock(
                page=int(item["page"]),
                bbox=[float(value) for value in item["bbox"]],
                text=str(item.get("text", "")),
                type=str(item.get("type", "paragraph")),
            )
            for item in raw.get("text_blocks", [])
            if item.get("bbox") and len(item["bbox"]) == 4 and item.get("text")
        ]
        order: dict[int, list[str]] = {}
        for page, ids in (raw.get("order") or {}).items():
            try:
                order[int(page)] = [str(block_id) for block_id in ids]
            except (TypeError, ValueError):
                continue
        edited = {
            str(block_id): str(text)
            for block_id, text in (raw.get("edited") or {}).items()
            if str(text).strip()
        }
        return cls(
            removed=[str(x) for x in raw.get("removed", [])],
            restored=[str(x) for x in raw.get("restored", [])],
            regions=regions,
            text_blocks=text_blocks,
            edited=edited,
            order=order,
        )


# --------------------------------------------------------------------------
# applying
# --------------------------------------------------------------------------


def apply_corrections(document: Document, corrections: Corrections) -> Document:
    """Return a copy of *document* with the reader's corrections applied."""
    if corrections.is_empty:
        return document

    doc = copy.deepcopy(document)
    _restore(doc, corrections.restored)
    _remove(doc, corrections.removed)
    _add_regions(doc, corrections.regions)
    _add_text_blocks(doc, corrections.text_blocks)
    # Last of the content changes, so a block that was restored or picked
    # up can be corrected in the same pass.
    _apply_edits(doc, corrections.edited)
    _reorder(doc, corrections.order)
    return doc


def _remove(doc: Document, block_ids: list[str]) -> None:
    """Take blocks out of the running text, recording why."""
    wanted = set(block_ids)
    if not wanted:
        return
    kept: list[Block] = []
    for block in doc.blocks:
        if block.id in wanted:
            doc.drop(MANUAL_REASON, block.text, block.page, block.bbox)
        else:
            kept.append(block)
    missing = wanted - {block.id for block in doc.blocks}
    for block_id in sorted(missing):
        log.debug("nothing to remove for %s", block_id)
    doc.blocks = kept


def _restore(doc: Document, drop_ids: list[str]) -> None:
    """Bring dropped text back into the flow at its place on the page."""
    wanted = set(drop_ids)
    if not wanted:
        return
    survivors = []
    for index, dropped in enumerate(doc.dropped):
        if drop_id(index) not in wanted:
            survivors.append(dropped)
            continue
        block = Block(
            id=f"restored-{index:04d}",
            type="paragraph",
            text=dropped.text,
            page=dropped.page,
            bbox=dropped.bbox,
        )
        _insert_in_reading_order(doc, block)
    doc.dropped = survivors


def _insert_in_reading_order(doc: Document, block: Block) -> None:
    """Put *block* where it sat on the page, by vertical position."""
    if block.bbox is None:
        doc.blocks.append(block)
        return
    for index, existing in enumerate(doc.blocks):
        if existing.page < block.page:
            continue
        if existing.page > block.page:
            doc.blocks.insert(index, block)
            return
        if existing.bbox is not None and existing.bbox.y0 > block.bbox.y0:
            doc.blocks.insert(index, block)
            return
    doc.blocks.append(block)


#: Block types the reader may choose for a picked-up text region.
TEXT_BLOCK_TYPES = ("paragraph", "heading", "caption", "footnote", "list_item")


def _apply_edits(doc: Document, edited: dict[str, str]) -> None:
    """Replace the text of blocks the reader corrected.

    An empty correction is ignored rather than treated as a deletion: the
    reader has a button for leaving a block out, and silently losing a
    paragraph to a stray select-all would be the worse surprise.
    """
    if not edited:
        return
    for block in doc.blocks:
        text = edited.get(block.id)
        if text is not None and text.strip():
            block.text = text.strip()


def _add_text_blocks(doc: Document, text_blocks: list[TextBlock]) -> None:
    """Insert text the reader picked up at its place on the page."""
    for index, picked in enumerate(text_blocks):
        if not picked.text.strip():
            continue
        block_type = (
            picked.type if picked.type in TEXT_BLOCK_TYPES else "paragraph"
        )
        block = Block(
            id=f"added-{index:04d}",
            type=block_type,  # type: ignore[arg-type]
            text=picked.text.strip(),
            level=1 if block_type == "heading" else 0,
            page=picked.page,
            bbox=picked.as_bbox(),
        )
        _insert_in_reading_order(doc, block)


def _add_regions(doc: Document, regions: list[Region]) -> None:
    """Turn drawn rectangles into assets the renderer will crop."""
    if not regions:
        return
    taken = {asset.key for asset in doc.assets}
    order = max((asset.order for asset in doc.assets), default=0)
    for index, region in enumerate(regions, start=1):
        order += 1
        kind, number, supplementary, label = _label_for(region, index)
        key = normalize_key(kind, number, supplementary)
        if key in taken:
            key = _free_key(key, taken)
        taken.add(key)
        doc.assets.append(
            Asset(
                key=key,
                kind=kind,  # type: ignore[arg-type]
                label=label,
                number=number,
                caption=region.caption,
                supplementary=supplementary,
                bboxes=[region.as_bbox()],
                order=order,
            )
        )


def _label_for(region: Region, index: int) -> tuple[str, str, bool, str]:
    """Work out kind, number and label for a drawn region."""
    parsed = parse_caption_label(region.label) if region.label else None
    if parsed:
        kind, number, supplementary, label, _rest = parsed
        return kind, number, supplementary, label
    kind = region.kind if region.kind in ("figure", "table", "scheme") else "figure"
    pretty = {"figure": "Figure", "table": "Table", "scheme": "Scheme"}[kind]
    # Hand-drawn regions get their own number space so they cannot collide
    # with the ones the parser found.
    number = f"m{index}"
    return kind, number, False, region.label or f"{pretty} {number.upper()}"


def _free_key(key: str, taken: set[str]) -> str:
    for suffix in range(2, 100):
        candidate = f"{key}-{suffix}"
        if candidate not in taken:
            return candidate
    return key


def _reorder(doc: Document, order: dict[int, list[str]]) -> None:
    """Rearrange the blocks of a page into the order the reader chose."""
    if not order:
        return
    for page, wanted in order.items():
        positions = [i for i, b in enumerate(doc.blocks) if b.page == page]
        if not positions:
            continue
        on_page = [doc.blocks[i] for i in positions]
        by_id = {block.id: block for block in on_page}
        ranked = [by_id.pop(block_id) for block_id in wanted if block_id in by_id]
        # Blocks the reader did not mention keep their relative order.
        ranked.extend(block for block in on_page if block.id in by_id)
        for index, block in zip(positions, ranked):
            doc.blocks[index] = block
