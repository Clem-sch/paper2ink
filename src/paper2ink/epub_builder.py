"""Assemble the EPUB3.

The builder runs in two passes.  The first assigns blocks to chapters and
linkifies every piece of text, which is what registers the mentions.  The
second renders the XHTML, so the "Figures & Tables" chapter and the
bibliography already know every place that refers to them and can emit the
backlinks the Kindle's unreliable back button makes necessary.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ebooklib import epub

from .model import Asset, Block, Document
from .refs import Linker, author_year_index, normalize_key

log = logging.getLogger("paper2ink.epub")

__all__ = ["BuildOptions", "BuildResult", "EpubBuilder"]

TEMPLATE_DIR = Path(__file__).parent / "templates"
IMAGE_DIR = "images"
FIGURES_FILE = "figures.xhtml"
REFERENCES_FILE = "references.xhtml"

_NUMBERED_HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+\S")
_APPENDIX_HEADING_RE = re.compile(r"^\s*appendix\s+([A-Z0-9]+)\b", re.IGNORECASE)

XHTML_HEAD = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<!DOCTYPE html>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml" '
    'xmlns:epub="http://www.idpf.org/2007/ops" lang="{lang}" xml:lang="{lang}">\n'
    "<head>\n"
    '  <meta charset="utf-8" />\n'
    "  <title>{title}</title>\n"
    '  <link rel="stylesheet" type="text/css" href="style.css" />\n'
    "</head>\n"
    "<body>\n"
)
XHTML_TAIL = "</body>\n</html>\n"


@dataclass
class BuildOptions:
    tables: str = "image"
    include_backlinks: bool = True
    cover: bool = True


@dataclass
class BuildResult:
    path: Path
    chapters: int = 0
    figures: int = 0
    tables: int = 0
    equations: int = 0
    links: int = 0
    unresolved: int = 0
    references: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Chapter:
    file_name: str
    title: str
    blocks: list[Block] = field(default_factory=list)
    level: int = 1

    @property
    def anchor(self) -> str:
        """Id of the invisible anchor at the top of the chapter."""
        return "top-" + self.file_name.rsplit(".", 1)[0]


class EpubBuilder:
    """Turns a cleaned :class:`Document` into an EPUB3 file."""

    def __init__(self, doc: Document, options: BuildOptions | None = None) -> None:
        self.doc = doc
        self.options = options or BuildOptions()
        self.chapters: list[_Chapter] = []
        self.section_keys: dict[str, str] = {}   # key -> file name
        self.equation_files: dict[str, str] = {}
        self.linker: Linker | None = None

    # -- entry point -------------------------------------------------------
    def build(self, out_path: Path, image_dir: Path, cover_path: Path | None) -> BuildResult:
        self._split_chapters()
        self._collect_section_anchors()
        self._link_everything()

        book = epub.EpubBook()
        self._set_metadata(book)

        style = epub.EpubItem(
            uid="style",
            file_name="style.css",
            media_type="text/css",
            content=(TEMPLATE_DIR / "style.css").read_bytes(),
        )
        book.add_item(style)

        spine: list[object] = []
        toc: list[object] = []

        if self.options.cover and cover_path and cover_path.exists():
            book.set_cover("cover.png", cover_path.read_bytes(), create_page=True)
            # ebooklib marks the generated cover page non-linear, which
            # EPUBCheck rejects unless something links to it (OPF-096).
            cover_page = book.get_item_with_id("cover")
            if cover_page is not None:
                cover_page.is_linear = True
            spine.append("cover")

        title_page = self._make_page("title.xhtml", self.doc.metadata.title or "Title",
                                     self._render_title_page(), style)
        book.add_item(title_page)
        spine.append(title_page)

        spine.append("nav")

        self._add_images(book, image_dir)

        for chapter in self.chapters:
            item = self._make_page(
                chapter.file_name, chapter.title, self._render_chapter(chapter), style
            )
            book.add_item(item)
            spine.append(item)
            toc.append(item)

        assets = self._appendix_assets()
        if assets:
            item = self._make_page(
                FIGURES_FILE, "Figures & Tables",
                self._render_assets_page(assets), style,
            )
            book.add_item(item)
            spine.append(item)
            toc.append(item)

        if self.doc.references:
            item = self._make_page(
                REFERENCES_FILE, "References", self._render_references(), style
            )
            book.add_item(item)
            spine.append(item)
            toc.append(item)

        book.toc = tuple(toc)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = spine

        out_path.parent.mkdir(parents=True, exist_ok=True)
        epub.write_epub(str(out_path), book, {})

        linker = self.linker
        if linker is not None:
            # Keep the unresolved targets on the document: the review view
            # needs to know *which* references point nowhere, not just how
            # many.
            self.doc.unresolved = dict(linker.unresolved)
        return BuildResult(
            path=out_path,
            chapters=len(self.chapters),
            figures=len([a for a in self.doc.assets if a.kind in ("figure", "scheme")]),
            tables=len(self.doc.tables),
            equations=len(self.doc.equations),
            links=linker.link_count if linker else 0,
            unresolved=linker.unresolved_count if linker else 0,
            references=len(self.doc.references),
            warnings=list(self.doc.warnings),
        )

    # -- pass 1: structure -------------------------------------------------
    def _split_chapters(self) -> None:
        """One chapter per top-level heading; everything before it is intro."""
        current = _Chapter(file_name="ch-001.xhtml", title="Introduction")
        chapters = [current]
        for block in self.doc.blocks:
            if block.type == "heading" and block.level <= 1 and current.blocks:
                current = _Chapter(
                    file_name=f"ch-{len(chapters) + 1:03d}.xhtml",
                    title=block.text[:90],
                )
                chapters.append(current)
            if block.type == "heading" and block.level <= 1 and not current.blocks:
                current.title = block.text[:90]
            current.blocks.append(block)
        # A heading whose content was lifted elsewhere - "References", whose
        # entries now live in the bibliography - would otherwise leave a
        # hollow chapter in the book and a duplicate in the table of
        # contents.
        self.chapters = [
            chapter
            for chapter in chapters
            if chapter.blocks and not _is_hollow(chapter)
        ]

    def _collect_section_anchors(self) -> None:
        """Record which file holds each numbered section and each equation."""
        for chapter in self.chapters:
            for block in chapter.blocks:
                if block.type == "heading":
                    key = _section_key(block.text)
                    if key and key not in self.section_keys:
                        self.section_keys[key] = chapter.file_name
                        block.asset_key = key
                elif block.type == "equation" and block.asset_key:
                    self.equation_files[block.asset_key] = chapter.file_name

    def _known_keys(self) -> set[str]:
        keys = {asset.key for asset in self.doc.assets}
        keys |= set(self.section_keys)
        keys |= {reference.key for reference in self.doc.references}
        return keys

    def _href_for(self, key: str) -> str | None:
        if key.startswith(("fig-", "tab-", "sch-")):
            if self.doc.asset_by_key(key):
                return f"{FIGURES_FILE}#{key}"
            return None
        if key.startswith("eq-"):
            file_name = self.equation_files.get(key)
            return f"{file_name}#{key}" if file_name else None
        if key.startswith("sec-"):
            file_name = self.section_keys.get(key)
            return f"{file_name}#{key}" if file_name else None
        if key.startswith("bib-"):
            if any(r.key == key for r in self.doc.references):
                return f"{REFERENCES_FILE}#{key}"
        return None

    def _link_everything(self) -> None:
        linker = Linker(
            self._known_keys(),
            self._href_for,
            allow_bare_equations=bool(self.doc.equations),
            author_year_index=author_year_index(self.doc.references),
        )
        self.linker = linker

        for chapter in self.chapters:
            linker.current_file = chapter.file_name
            for block in chapter.blocks:
                # Equations are images and captions are rendered with their
                # asset, so linking them here would register mentions whose
                # anchors never reach the output.
                if block.type in ("equation", "caption"):
                    continue
                block.html = linker.linkify(
                    block.text, block.id, self_key=block.asset_key
                )

        # Captions live in the appendix chapter.
        linker.current_file = FIGURES_FILE
        for asset in self.doc.assets:
            if asset.caption:
                asset.caption_html = linker.linkify(
                    asset.caption, f"cap-{asset.key}", self_key=asset.key
                )

        linker.current_file = REFERENCES_FILE

    # -- pass 2: rendering -------------------------------------------------
    def _render_title_page(self) -> str:
        meta = self.doc.metadata
        parts = ['<div class="titlepage" epub:type="titlepage">']
        parts.append(f"<h1>{html.escape(meta.title or 'Untitled')}</h1>")
        if meta.authors:
            parts.append(
                f'<p class="authors">{html.escape(", ".join(meta.authors))}</p>'
            )
        info = [p for p in (meta.journal, meta.year) if p]
        if info:
            parts.append(f'<p class="pubinfo">{html.escape(" · ".join(info))}</p>')
        if meta.doi:
            parts.append(
                f'<p class="pubinfo"><a class="doi" href="https://doi.org/'
                f'{html.escape(meta.doi)}">doi:{html.escape(meta.doi)}</a></p>'
            )
        parts.append("</div>")
        if meta.abstract:
            parts.append('<div class="abstract" epub:type="abstract">')
            parts.append("<h2>Abstract</h2>")
            parts.append(f"<p>{html.escape(meta.abstract)}</p>")
            parts.append("</div>")
        return "\n".join(parts)

    def _render_chapter(self, chapter: _Chapter) -> str:
        parts: list[str] = [f'<div id="{chapter.anchor}"></div>']
        footnotes: list[Block] = []
        for block in chapter.blocks:
            if block.type == "heading":
                level = min(max(block.level, 1), 4)
                anchor = f' id="{block.asset_key}"' if block.asset_key else ""
                parts.append(
                    f"<h{level}{anchor}>{block.html or html.escape(block.text)}</h{level}>"
                )
            elif block.type == "equation":
                parts.append(self._render_equation(block))
            elif block.type == "caption":
                continue  # captions are rendered with their asset
            elif block.type == "footnote":
                footnotes.append(block)
            elif block.type == "list_item":
                parts.append(f"<p>{block.html or html.escape(block.text)}</p>")
            else:
                parts.append(f"<p>{block.html or html.escape(block.text)}</p>")

        if footnotes:
            parts.append('<section class="footnotes" epub:type="footnotes">')
            parts.append("<h2>Notes</h2>")
            for note in footnotes:
                parts.append(
                    f'<aside epub:type="footnote" id="{note.id}">'
                    f"<p>{note.html or html.escape(note.text)}</p></aside>"
                )
            parts.append("</section>")
        return "\n".join(parts)

    def _render_equation(self, block: Block) -> str:
        asset = self.doc.asset_by_key(block.asset_key or "")
        if asset is None or not asset.image_path:
            return (
                f'<p class="equation" id="{html.escape(block.asset_key or block.id)}">'
                f"{html.escape(block.text)}</p>"
            )
        alt = asset.latex or asset.label or "equation"
        backlinks = self._render_backlinks(asset)
        return (
            f'<p class="equation" id="{html.escape(asset.key)}">'
            f'<img src="{IMAGE_DIR}/{asset.image_path}" alt="{html.escape(alt)}" />'
            f"</p>{backlinks}"
        )

    def _appendix_assets(self) -> list[Asset]:
        assets = [a for a in self.doc.assets if a.kind != "equation"]
        kind_rank = {"figure": 0, "scheme": 1, "table": 2}
        return sorted(
            assets,
            key=lambda a: (1 if a.supplementary else 0, kind_rank.get(a.kind, 3), a.order),
        )

    def _render_assets_page(self, assets: list[Asset]) -> str:
        parts = ["<h1>Figures &amp; Tables</h1>"]
        for asset in assets:
            parts.append(f'<section class="asset" id="{html.escape(asset.key)}">')
            parts.append("<figure>")
            if asset.html_table:
                parts.append(asset.html_table)
            elif asset.image_path:
                alt = asset.latex or f"{asset.label}"
                parts.append(
                    f'<img src="{IMAGE_DIR}/{asset.image_path}" '
                    f'alt="{html.escape(alt)}" />'
                )
            else:
                parts.append(
                    "<p><em>(image could not be extracted from the PDF)</em></p>"
                )
            caption = asset.caption_html or html.escape(asset.caption)
            parts.append(
                f'<figcaption><span class="asset-label">'
                f"{html.escape(asset.label)}.</span> {caption}</figcaption>"
            )
            parts.append("</figure>")
            parts.append(self._render_backlinks(asset))
            parts.append("</section>")
        return "\n".join(parts)

    def _render_backlinks(self, asset: Asset) -> str:
        if not self.options.include_backlinks or self.linker is None:
            return ""
        mentions = self.linker.mentions_for(asset.key)
        if not mentions:
            home = self._home_href(asset)
            if not home:
                return ""
            return (
                f'<p class="backlinks"><a class="backlink" href="{home}">'
                "↩ back to text</a></p>"
            )
        if len(mentions) == 1:
            mention = mentions[0]
            return (
                f'<p class="backlinks"><a class="backlink" '
                f'href="{mention.file}#{mention.id}">↩ back to text</a></p>'
            )
        links = " ".join(
            f'<a class="backlink" href="{m.file}#{m.id}">{m.index}</a>'
            for m in mentions
        )
        return f'<p class="backlinks">↩ {links}</p>'

    def _home_href(self, asset: Asset) -> str | None:
        """Fallback target for assets nothing in the text refers to."""
        if not asset.home_block_id:
            return None
        for chapter in self.chapters:
            for block in chapter.blocks:
                if block.id == asset.home_block_id:
                    return f"{chapter.file_name}#{chapter.anchor}"
        return None

    def _render_references(self) -> str:
        parts = ["<h1>References</h1>", '<div class="references">']
        for reference in _ordered_references(self.doc.references):
            back = ""
            if self.linker:
                mentions = self.linker.mentions_for(reference.key)
                if len(mentions) == 1:
                    back = (
                        f' <a class="backlink" href="{mentions[0].file}'
                        f'#{mentions[0].id}">↩</a>'
                    )
                elif mentions:
                    inner = " ".join(
                        f'<a class="backlink" href="{m.file}#{m.id}">{m.index}</a>'
                        for m in mentions
                    )
                    back = f' <span class="backlinks">↩ {inner}</span>'
            doi = ""
            if reference.doi:
                doi = (
                    f' <a class="doi" href="https://doi.org/'
                    f'{html.escape(reference.doi)}">doi</a>'
                )
            # The number is printed rather than left to an <ol>: reading
            # order does not always follow the numbering, and a list that
            # counts 1, 2, 3 beside entries numbered 66, 108, 67 is worse
            # than useless when the text cites them by number.
            marker = (
                f'<span class="refnum">{html.escape(reference.number)}.</span> '
                if reference.number
                else ""
            )
            parts.append(
                f'<p class="reference" id="{html.escape(reference.key)}">'
                f"{marker}{html.escape(reference.raw)}{doi}{back}</p>"
            )
        parts.append("</div>")
        return "\n".join(parts)

    # -- helpers -----------------------------------------------------------
    def _make_page(
        self, file_name: str, title: str, body: str, style: epub.EpubItem
    ) -> epub.EpubHtml:
        lang = self.doc.metadata.language or "en"
        item = epub.EpubHtml(
            title=title, file_name=file_name, lang=lang,
            uid=file_name.replace(".", "-"),
        )
        item.content = (
            XHTML_HEAD.format(lang=html.escape(lang), title=html.escape(title))
            + body
            + "\n"
            + XHTML_TAIL
        )
        item.add_item(style)
        return item

    def _add_images(self, book: epub.EpubBook, image_dir: Path) -> None:
        for asset in self.doc.assets:
            if not asset.image_path:
                continue
            source = image_dir / asset.image_path
            if not source.exists():
                self.doc.add_warning(f"missing image file for {asset.key}")
                asset.image_path = None
                continue
            book.add_item(
                epub.EpubItem(
                    uid=f"img-{asset.key}",
                    file_name=f"{IMAGE_DIR}/{asset.image_path}",
                    media_type="image/png",
                    content=source.read_bytes(),
                )
            )

    def _set_metadata(self, book: epub.EpubBook) -> None:
        meta = self.doc.metadata
        identifier = (
            f"urn:doi:{meta.doi}" if meta.doi
            else f"urn:paper2ink:{Path(self.doc.source_path).stem}"
        )
        book.set_identifier(identifier)
        book.set_title(meta.title or Path(self.doc.source_path).stem)
        book.set_language(meta.language or "en")
        for author in meta.authors:
            book.add_author(author)
        if meta.year:
            book.add_metadata("DC", "date", meta.year)
        if meta.journal:
            book.add_metadata("DC", "source", meta.journal)
        if meta.doi:
            book.add_metadata("DC", "identifier", f"https://doi.org/{meta.doi}")
        # ebooklib writes the required dcterms:modified itself; adding a
        # second one makes EPUBCheck fail (RSC-005).


def _is_hollow(chapter: _Chapter) -> bool:
    """A chapter that is nothing but headings carries no text to read."""
    return all(block.type in ("heading", "caption") for block in chapter.blocks)


def _ordered_references(references: list) -> list:
    """Numeric bibliographies are shown in numeric order.

    Two-column reference lists are not always read back in numbering order
    (a page's last column can be picked up early), and a reader following a
    citation expects the numbers to run.  Author-year lists keep the order
    the paper printed them in.
    """
    if not references:
        return references
    if not all(reference.number.isdigit() for reference in references):
        return references
    return sorted(references, key=lambda reference: int(reference.number))


def _section_key(text: str) -> str | None:
    appendix = _APPENDIX_HEADING_RE.match(text)
    if appendix:
        return normalize_key("section", appendix.group(1).lower())
    numbered = _NUMBERED_HEADING_RE.match(text)
    if numbered:
        return normalize_key("section", numbered.group(1))
    return None
