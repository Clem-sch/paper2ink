"""Parser-independent intermediate document model.

Everything downstream of the parsers (cleanup, assets, refs, epub_builder)
works on these dataclasses only, so a new parser backend just has to produce a
:class:`Document`.  The whole model is JSON-serialisable via :meth:`Document.to_json`
so ``--dump-json`` can be used for debugging.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

BlockType = Literal[
    "title",
    "author",
    "abstract",
    "heading",
    "paragraph",
    "list_item",
    "caption",
    "equation",
    "footnote",
    "reference_entry",
    "other",
]

AssetKind = Literal["figure", "table", "equation"]


@dataclass
class BBox:
    """A rectangle on a single PDF page, in PDF points, origin top-left."""

    page: int
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    def union(self, other: "BBox") -> "BBox":
        if other.page != self.page:
            raise ValueError("cannot union boxes from different pages")
        return BBox(
            self.page,
            min(self.x0, other.x0),
            min(self.y0, other.y0),
            max(self.x1, other.x1),
            max(self.y1, other.y1),
        )

    def intersects(self, other: "BBox") -> bool:
        return (
            other.page == self.page
            and self.x0 < other.x1
            and other.x0 < self.x1
            and self.y0 < other.y1
            and other.y0 < self.y1
        )

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


@dataclass
class Block:
    """One logical chunk of text in reading order."""

    id: str
    type: BlockType = "paragraph"
    text: str = ""
    level: int = 0           # heading level, 1 = chapter
    page: int = 0
    bbox: BBox | None = None
    column: int = 0
    font_size: float = 0.0
    # Asset key this block belongs to, for captions ("fig-3", "tab-1", ...).
    asset_key: str | None = None
    # Populated by refs.py during the linking pass.
    html: str | None = None

    @property
    def is_text(self) -> bool:
        return self.type in {
            "abstract",
            "heading",
            "paragraph",
            "list_item",
            "caption",
            "footnote",
            "reference_entry",
            "title",
            "author",
        }


@dataclass
class Asset:
    """A figure, table or displayed equation lifted out of the page."""

    key: str                       # normalised anchor id, e.g. "fig-3", "tab-s1"
    kind: AssetKind = "figure"
    label: str = ""                # human label, e.g. "Figure 3"
    number: str = ""               # "3", "S1", "2.1"
    caption: str = ""
    caption_html: str | None = None
    bboxes: list[BBox] = field(default_factory=list)
    image_path: str | None = None  # file name inside the EPUB image folder
    latex: str | None = None       # optional recognised LaTeX
    html_table: str | None = None  # optional reflowable HTML (--tables html)
    supplementary: bool = False
    order: int = 0                 # order of first appearance in the document
    # Id of the block the asset sat next to; used for the "never mentioned"
    # fallback backlink.
    home_block_id: str | None = None
    inline: bool = False           # inline equations stay in the text flow

    @property
    def sort_key(self) -> tuple[int, int, float, str]:
        kind_rank = {"figure": 0, "table": 1, "equation": 2}[self.kind]
        digits = "".join(ch for ch in self.number if ch.isdigit())
        return (
            1 if self.supplementary else 0,
            kind_rank,
            float(digits) if digits else 1e9,
            self.number,
        )


@dataclass
class Reference:
    """One entry of the bibliography."""

    key: str          # anchor id, e.g. "bib-12"
    number: str = ""  # "12" for numeric styles, "" for author-year
    raw: str = ""
    doi: str | None = None
    authors: str = ""
    year: str = ""


@dataclass
class Footnote:
    key: str          # anchor id, e.g. "fn-3"
    marker: str = ""  # "1", "a", "*"
    text: str = ""


@dataclass
class DroppedBlock:
    """Text the pipeline threw away, kept so a reviewer can check on it.

    Every stage that discards text records it here with a reason.  Most
    drops are correct (running heads, page numbers, publisher boilerplate),
    but a highlighted text box or a footnote can look exactly like one, and
    silently losing those is the failure mode that is hardest to notice.
    """

    reason: str          # see DROP_REASONS
    text: str
    page: int = 0
    bbox: BBox | None = None

    @property
    def is_suspicious(self) -> bool:
        """Whether this drop deserves a second look.

        Only two reasons can swallow real content: text inside a graphic
        (a highlighted box reads as a figure to the layout heuristics) and
        an over-eager boilerplate pattern.  Running heads and journal front
        matter are page furniture and practically never body text, so they
        are not flagged however long they are.

        Within those two, a long passage or one ending in a full stop reads
        like prose rather than an axis label.
        """
        if self.reason not in ("inside graphic", "boilerplate"):
            return False
        stripped = self.text.strip()
        return len(stripped) > 60 or stripped.endswith((".", "!", "?"))


#: Reasons blocks get dropped, in the order the pipeline applies them.
DROP_REASONS = (
    "running head",      # repeats in the header/footer band on most pages
    "front matter",      # journal metadata on the first page
    "inside graphic",    # text sitting inside a figure, cropped with it
    "label value",       # the value belonging to a "KEYWORDS" style label
    "boilerplate",       # matched a publisher boilerplate pattern
    "page number",       # a bare number on its own line
)


@dataclass
class Metadata:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    doi: str | None = None
    journal: str | None = None
    year: str | None = None
    abstract: str = ""
    language: str = "en"

    @property
    def first_author_surname(self) -> str:
        if not self.authors:
            return "Unknown"
        first = self.authors[0].strip()
        if "," in first:
            return first.split(",")[0].strip()
        parts = [p for p in first.split() if p]
        return parts[-1] if parts else "Unknown"


@dataclass
class Document:
    """The intermediate format shared by all pipeline stages."""

    source_path: str = ""
    page_count: int = 0
    parser: str = ""
    metadata: Metadata = field(default_factory=Metadata)
    blocks: list[Block] = field(default_factory=list)
    assets: list[Asset] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    footnotes: list[Footnote] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dropped: list[DroppedBlock] = field(default_factory=list)
    #: Reference targets the text mentions but the document does not hold,
    #: as key -> number of mentions.  Filled in by the EPUB builder.
    unresolved: dict[str, int] = field(default_factory=dict)

    # -- convenience views -------------------------------------------------
    @property
    def figures(self) -> list[Asset]:
        return [a for a in self.assets if a.kind == "figure"]

    @property
    def tables(self) -> list[Asset]:
        return [a for a in self.assets if a.kind == "table"]

    @property
    def equations(self) -> list[Asset]:
        return [a for a in self.assets if a.kind == "equation"]

    def asset_by_key(self, key: str) -> Asset | None:
        for asset in self.assets:
            if asset.key == key:
                return asset
        return None

    def block_by_id(self, block_id: str) -> Block | None:
        for block in self.blocks:
            if block.id == block_id:
                return block
        return None

    @property
    def suspicious_drops(self) -> list[DroppedBlock]:
        """Dropped text that looks like prose rather than page furniture."""
        return [drop for drop in self.dropped if drop.is_suspicious]

    def drop(
        self, reason: str, text: str, page: int = 0, bbox: "BBox | None" = None
    ) -> None:
        """Record that *text* was discarded, so a reviewer can check it."""
        if text.strip():
            self.dropped.append(DroppedBlock(reason, text, page, bbox))

    def add_warning(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    def dump_json(self, path: Path) -> None:
        path.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Document":
        doc = cls(
            source_path=data.get("source_path", ""),
            page_count=data.get("page_count", 0),
            parser=data.get("parser", ""),
            metadata=Metadata(**data.get("metadata", {})),
            warnings=list(data.get("warnings", [])),
        )
        for raw in data.get("blocks", []):
            raw = dict(raw)
            bbox = raw.get("bbox")
            raw["bbox"] = BBox(**bbox) if bbox else None
            doc.blocks.append(Block(**raw))
        for raw in data.get("assets", []):
            raw = dict(raw)
            raw["bboxes"] = [BBox(**b) for b in raw.get("bboxes", [])]
            doc.assets.append(Asset(**raw))
        for raw in data.get("references", []):
            doc.references.append(Reference(**raw))
        for raw in data.get("footnotes", []):
            doc.footnotes.append(Footnote(**raw))
        doc.unresolved = dict(data.get("unresolved", {}))
        for raw in data.get("dropped", []):
            raw = dict(raw)
            bbox = raw.get("bbox")
            raw["bbox"] = BBox(**bbox) if bbox else None
            doc.dropped.append(DroppedBlock(**raw))
        return doc

    @classmethod
    def load_json(cls, path: Path) -> "Document":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
