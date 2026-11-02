"""Rendering of figures, tables, equations, the cover and debug overlays.

Figures are *not* taken from the PDF's embedded raster images: most are
vector art or built from several sub-images, so the only faithful way is to
render the page region behind the bounding box.  Captions are never cropped
into the image, they are set as text underneath.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .model import Asset, BBox, Document

log = logging.getLogger("paper2ink.assets")

__all__ = ["RenderOptions", "AssetRenderer", "render_cover", "render_debug_layout"]

COVER_SIZE = (1600, 2560)


@dataclass
class RenderOptions:
    dpi: int = 250
    color: bool = False
    max_width: int = 1600
    padding: float = 4.0        # PDF points added around the bbox
    trim: bool = True
    trim_threshold: int = 247   # pixels brighter than this count as white


class AssetRenderer:
    """Crops asset regions out of the PDF and writes them as PNG."""

    def __init__(self, pdf_path: Path, options: RenderOptions | None = None) -> None:
        self.pdf_path = pdf_path
        self.options = options or RenderOptions()
        self._pdf = pymupdf.open(pdf_path)

    def close(self) -> None:
        self._pdf.close()

    def __enter__(self) -> "AssetRenderer":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- public API --------------------------------------------------------
    def render_document(self, doc: Document, out_dir: Path) -> int:
        """Render every asset of *doc* into *out_dir*; returns the count."""
        out_dir.mkdir(parents=True, exist_ok=True)
        rendered = 0
        for asset in doc.assets:
            if asset.kind == "table" and asset.html_table:
                continue  # reflowable table, no image needed
            try:
                image = self.render_asset(asset)
            except Exception as exc:  # pragma: no cover - defensive
                doc.add_warning(f"could not render {asset.label or asset.key}: {exc}")
                continue
            if image is None:
                doc.add_warning(f"empty image region for {asset.label or asset.key}")
                continue
            name = f"{asset.key}.png"
            image.save(out_dir / name, format="PNG", optimize=True)
            asset.image_path = name
            rendered += 1
        return rendered

    def render_asset(self, asset: Asset) -> Image.Image | None:
        """Render one asset, stacking the parts of a multi-page figure."""
        parts = [self.render_bbox(bbox) for bbox in asset.bboxes]
        parts = [p for p in parts if p is not None]
        if not parts:
            return None
        image = parts[0] if len(parts) == 1 else _stack_vertically(parts)
        return self._postprocess(image)

    def render_bbox(self, bbox: BBox) -> Image.Image | None:
        page = self._pdf[bbox.page]
        pad = self.options.padding
        clip = pymupdf.Rect(
            max(page.rect.x0, bbox.x0 - pad),
            max(page.rect.y0, bbox.y0 - pad),
            min(page.rect.x1, bbox.x1 + pad),
            min(page.rect.y1, bbox.y1 + pad),
        )
        if clip.is_empty or clip.width < 4 or clip.height < 4:
            return None
        zoom = self.options.dpi / 72.0
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False
        )
        return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)

    # -- image post-processing --------------------------------------------
    def _postprocess(self, image: Image.Image) -> Image.Image:
        if self.options.trim:
            image = trim_whitespace(image, self.options.trim_threshold)
        if not self.options.color:
            image = ImageOps.grayscale(image)
        if image.width > self.options.max_width:
            ratio = self.options.max_width / image.width
            size = (self.options.max_width, max(1, round(image.height * ratio)))
            image = image.resize(size, Image.LANCZOS)
        return image


def trim_whitespace(image: Image.Image, threshold: int = 247) -> Image.Image:
    """Crop the white margin around the content, keeping a small border."""
    grey = image.convert("L")
    mask = grey.point(lambda value: 255 if value < threshold else 0)
    box = mask.getbbox()
    if box is None:
        return image
    margin = 4
    left = max(0, box[0] - margin)
    top = max(0, box[1] - margin)
    right = min(image.width, box[2] + margin)
    bottom = min(image.height, box[3] + margin)
    if right - left < 8 or bottom - top < 8:
        return image
    return image.crop((left, top, right, bottom))


def _stack_vertically(parts: list[Image.Image], gap: int = 24) -> Image.Image:
    width = max(part.width for part in parts)
    height = sum(part.height for part in parts) + gap * (len(parts) - 1)
    canvas = Image.new("RGB", (width, height), "white")
    y = 0
    for part in parts:
        canvas.paste(part, ((width - part.width) // 2, y))
        y += part.height + gap
    return canvas


# --------------------------------------------------------------------------
# cover
# --------------------------------------------------------------------------


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        "DejaVuSerif-Bold.ttf" if bold else "DejaVuSerif.ttf",
        "georgiab.ttf" if bold else "georgia.ttf",
        "timesbd.ttf" if bold else "times.ttf",
        "arialbd.ttf" if bold else "arial.ttf",
    ]
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> float:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _wrap_to_width(
    draw: ImageDraw.ImageDraw, text: str, font, max_width: float
) -> list[str]:
    """Greedy word wrap measured in pixels, not characters.

    textwrap counts characters, which overflows badly with a proportional
    font and long chemical names.
    """
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and _text_width(draw, candidate, font) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _fit_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: float,
    max_lines: int,
    size: int,
    bold: bool = False,
    min_size: int = 40,
) -> tuple[list[str], object, int]:
    """Shrink the font until the text fits into *max_lines*."""
    while size > min_size:
        font = _load_font(size, bold=bold)
        lines = _wrap_to_width(draw, text, font, max_width)
        if len(lines) <= max_lines:
            return lines, font, size
        size -= 6
    font = _load_font(min_size, bold=bold)
    return _wrap_to_width(draw, text, font, max_width)[:max_lines], font, min_size


def render_cover(doc: Document, path: Path) -> Path:
    """Draw a plain black-and-white cover from the paper's metadata."""
    width, height = COVER_SIZE
    canvas = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(canvas)
    margin = 150

    draw.rectangle(
        [margin // 2, margin // 2, width - margin // 2, height - margin // 2],
        outline=0,
        width=6,
    )

    meta = doc.metadata
    text_width = width - 2 * margin
    y = 420

    title_lines, title_font, title_size = _fit_lines(
        draw, meta.title or "Untitled", text_width, max_lines=8, size=96, bold=True
    )
    for line in title_lines:
        draw.text((margin, y), line, font=title_font, fill=0)
        y += int(title_size * 1.25)

    y += 80
    draw.line([margin, y, width - margin, y], fill=0, width=4)
    y += 70

    authors = ", ".join(meta.authors[:6])
    if len(meta.authors) > 6:
        authors += " et al."
    author_lines, author_font, author_size = _fit_lines(
        draw, authors, text_width, max_lines=6, size=62, min_size=34
    )
    for line in author_lines:
        draw.text((margin, y), line, font=author_font, fill=0)
        y += int(author_size * 1.3)

    footer_font = _load_font(54)
    footer = " · ".join(p for p in (meta.journal, meta.year) if p)
    if footer:
        draw.text((margin, height - margin - 220), footer, font=footer_font, fill=0)
    if meta.doi:
        draw.text(
            (margin, height - margin - 140),
            f"doi:{meta.doi}",
            font=_load_font(40),
            fill=0,
        )
    draw.text(
        (margin, height - margin - 70),
        "prepared for e-ink reading with paper2ink",
        font=_load_font(36),
        fill=0,
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path, format="PNG", optimize=True)
    return path


# --------------------------------------------------------------------------
# debug overlay
# --------------------------------------------------------------------------

_DEBUG_COLORS = {
    "heading": (200, 0, 0),
    "paragraph": (0, 90, 200),
    "caption": (0, 150, 0),
    "equation": (200, 120, 0),
    "footnote": (130, 0, 180),
    "reference_entry": (120, 120, 120),
}


def render_debug_layout(doc: Document, pdf_path: Path, out_dir: Path, dpi: int = 110) -> int:
    """Write one PNG per page with block boxes and types drawn on top."""
    out_dir.mkdir(parents=True, exist_ok=True)
    zoom = dpi / 72.0
    written = 0
    with pymupdf.open(pdf_path) as pdf:
        for number in range(pdf.page_count):
            page = pdf[number]
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            image = Image.frombytes(
                "RGB", (pixmap.width, pixmap.height), pixmap.samples
            ).convert("RGB")
            draw = ImageDraw.Draw(image)
            font = _load_font(13, bold=True)

            for block in doc.blocks:
                if block.page != number or block.bbox is None:
                    continue
                color = _DEBUG_COLORS.get(block.type, (0, 0, 0))
                box = [coordinate * zoom for coordinate in block.bbox.as_tuple()]
                draw.rectangle(box, outline=color, width=2)
                label = block.type if not block.level else f"{block.type}{block.level}"
                draw.text((box[0] + 3, max(0, box[1] - 15)), label, fill=color, font=font)

            for asset in doc.assets:
                for bbox in asset.bboxes:
                    if bbox.page != number:
                        continue
                    box = [coordinate * zoom for coordinate in bbox.as_tuple()]
                    draw.rectangle(box, outline=(255, 0, 255), width=3)
                    draw.text(
                        (box[0] + 3, box[1] + 3),
                        asset.key,
                        fill=(255, 0, 255),
                        font=font,
                    )

            image.save(out_dir / f"page-{number + 1:03d}.png", format="PNG")
            written += 1
    return written
