"""FastAPI application behind the web UI.

Binds to localhost only and serves four things: the single-page app, the
job API, the unpacked EPUB (so the browser renders the finished book
itself) and rendered PDF pages for the review view.
"""

from __future__ import annotations

import logging
import tempfile
from contextlib import asynccontextmanager
import webbrowser
from pathlib import Path
from threading import Timer
from typing import Any

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..model import Document
from ..pipeline import ConversionSettings
from .jobs import PAGE_DPI, Job, JobStore

log = logging.getLogger("paper2ink.gui")

__all__ = ["create_app", "run"]

STATIC_DIR = Path(__file__).parent / "static"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

#: Media types the unpacked EPUB may contain.
_MEDIA_TYPES = {
    ".xhtml": "application/xhtml+xml",
    ".html": "text/html",
    ".css": "text/css",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ncx": "application/x-dtbncx+xml",
    ".opf": "application/oebps-package+xml",
}


class _FreshStatic(StaticFiles):
    """Serve the UI's own files without letting the browser cache them.

    This runs on localhost where the transfer is free, and a stale script
    or stylesheet after upgrading paper2ink is a confusing bug to chase.
    """

    def file_response(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    app.state.store.shutdown()


def create_app(store: JobStore | None = None) -> FastAPI:
    app = FastAPI(
        title="paper2ink", docs_url=None, redoc_url=None, lifespan=_lifespan
    )
    app.state.store = store or JobStore(
        Path(tempfile.mkdtemp(prefix="paper2ink-gui-"))
    )

    # -- the page ----------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    app.mount("/static", _FreshStatic(directory=STATIC_DIR), name="static")

    # -- jobs --------------------------------------------------------------
    @app.post("/api/jobs")
    async def create_job(
        file: UploadFile,
        parser: str = Form("pymupdf"),
        dpi: int = Form(250),
        color: bool = Form(False),
        tables: str = Form("image"),
        cover: bool = Form(True),
    ) -> JSONResponse:
        data = await file.read()
        if not data:
            raise HTTPException(400, "the uploaded file is empty")
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "the file is larger than 200 MB")
        if not data.startswith(b"%PDF"):
            raise HTTPException(400, "that does not look like a PDF")
        if parser not in ("auto", "pymupdf", "docling"):
            raise HTTPException(400, "unknown parser")
        if tables not in ("image", "html"):
            raise HTTPException(400, "unknown table mode")

        settings = ConversionSettings(
            parser=parser,
            dpi=max(72, min(600, dpi)),
            color=color,
            tables=tables,
            cover=cover,
        )
        job = _store(app).create(file.filename or "paper.pdf", data, settings)
        return JSONResponse(job.summary(), status_code=201)

    @app.get("/api/jobs")
    def list_jobs() -> dict[str, Any]:
        return {"jobs": [job.summary() for job in _store(app).list()]}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        return _job(app, job_id).summary()

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str) -> dict[str, bool]:
        return {"deleted": _store(app).delete(job_id)}

    # -- results -----------------------------------------------------------
    @app.get("/api/jobs/{job_id}/epub")
    def download_epub(job_id: str) -> FileResponse:
        job = _job(app, job_id)
        if job.epub_path is None:
            raise HTTPException(404, "no EPUB for this job yet")
        return FileResponse(
            job.epub_path,
            media_type="application/epub+zip",
            filename=job.epub_path.name,
        )

    @app.get("/api/jobs/{job_id}/review")
    def review(job_id: str) -> dict[str, Any]:
        job = _job(app, job_id)
        document = job.document
        if document is None:
            raise HTTPException(404, "this job has no parsed document")
        return _review_payload(job, document)

    @app.get("/api/jobs/{job_id}/page/{number}")
    def page_image(job_id: str, number: int) -> FileResponse:
        job = _job(app, job_id)
        path = _render_page(job, number)
        if path is None:
            raise HTTPException(404, "no such page")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/jobs/{job_id}/book/{path:path}")
    def book_file(job_id: str, path: str) -> FileResponse:
        job = _job(app, job_id)
        target = _resolve_inside(job.book_dir, path)
        if target is None or not target.is_file():
            raise HTTPException(404, "not in this book")
        media = _MEDIA_TYPES.get(target.suffix.lower(), "application/octet-stream")
        return FileResponse(target, media_type=media)

    @app.get("/api/jobs/{job_id}/reader")
    def reader_entry(job_id: str) -> dict[str, Any]:
        """Where the reader should start, and the chapter list."""
        job = _job(app, job_id)
        if not job.book_dir.exists():
            raise HTTPException(404, "no book for this job yet")
        return {"chapters": _chapters(job)}

    return app


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _store(app: FastAPI) -> JobStore:
    return app.state.store


def _job(app: FastAPI, job_id: str) -> Job:
    job = _store(app).get(job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    return job


def _resolve_inside(root: Path, relative: str) -> Path | None:
    """Resolve *relative* under *root*, refusing anything that escapes it."""
    try:
        target = (root / relative).resolve()
    except (OSError, ValueError):
        return None
    if not str(target).startswith(str(root.resolve())):
        return None
    return target


def _review_payload(job: Job, document: Document) -> dict[str, Any]:
    """Everything the review view needs, grouped by page."""
    pages: dict[int, dict[str, Any]] = {}

    def page(number: int) -> dict[str, Any]:
        return pages.setdefault(
            number,
            {"number": number, "kept": [], "dropped": [], "assets": []},
        )

    for block in document.blocks:
        if block.bbox is None:
            continue
        page(block.page)["kept"].append(
            {
                "type": block.type,
                "text": block.text,
                "bbox": _bbox(block.bbox),
                "level": block.level,
            }
        )
    for drop in document.dropped:
        page(drop.page)["dropped"].append(
            {
                "reason": drop.reason,
                "text": drop.text,
                "bbox": _bbox(drop.bbox) if drop.bbox else None,
                "suspicious": drop.is_suspicious,
            }
        )
    for asset in document.assets:
        for bbox in asset.bboxes:
            page(bbox.page)["assets"].append(
                {"key": asset.key, "label": asset.label, "bbox": _bbox(bbox)}
            )

    return {
        "id": job.id,
        "name": job.name,
        "page_count": document.page_count,
        "page_sizes": _page_sizes(job, document.page_count),
        "pages": [pages[number] for number in sorted(pages)],
        "suspicious": [
            {
                "reason": drop.reason,
                "text": drop.text,
                "page": drop.page,
            }
            for drop in document.suspicious_drops
        ],
        "warnings": document.warnings,
        "metadata": {
            "title": document.metadata.title,
            "authors": document.metadata.authors,
            "journal": document.metadata.journal,
            "year": document.metadata.year,
            "doi": document.metadata.doi,
            "abstract": document.metadata.abstract,
        },
        "unresolved": dict(
            sorted(document.unresolved.items(), key=lambda item: -item[1])
        ),
    }


def _bbox(bbox) -> list[float]:
    return [round(value, 2) for value in bbox.as_tuple()]


def _page_sizes(job: Job, count: int) -> list[list[float]]:
    import pymupdf

    sizes: list[list[float]] = []
    try:
        with pymupdf.open(job.pdf_path) as pdf:
            for index in range(min(count or pdf.page_count, pdf.page_count)):
                rect = pdf[index].rect
                sizes.append([round(rect.width, 2), round(rect.height, 2)])
    except Exception:  # pragma: no cover - defensive
        log.debug("could not read page sizes for %s", job.id)
    return sizes


def _render_page(job: Job, number: int) -> Path | None:
    """Render one PDF page to PNG, caching it in the job directory."""
    import pymupdf

    job.page_dir.mkdir(parents=True, exist_ok=True)
    target = job.page_dir / f"{number:04d}.png"
    if target.exists():
        return target
    try:
        with pymupdf.open(job.pdf_path) as pdf:
            if not 0 <= number < pdf.page_count:
                return None
            zoom = PAGE_DPI / 72.0
            pixmap = pdf[number].get_pixmap(
                matrix=pymupdf.Matrix(zoom, zoom), alpha=False
            )
            pixmap.save(target)
    except Exception:  # pragma: no cover - defensive
        log.exception("could not render page %s of job %s", number, job.id)
        return None
    return target


def _chapters(job: Job) -> list[dict[str, str]]:
    """List the book's documents in spine order, with readable titles."""
    import re

    opf = next(job.book_dir.rglob("*.opf"), None)
    if opf is None:
        return []
    text = opf.read_text(encoding="utf-8", errors="replace")
    # Attribute order inside <item> varies, so collect both spellings.
    manifest: dict[str, str] = {}
    for item in re.findall(r"<item\s[^>]*/?>", text):
        item_id = re.search(r'\bid="([^"]+)"', item)
        href = re.search(r'\bhref="([^"]+)"', item)
        if item_id and href:
            manifest[item_id.group(1)] = href.group(1)

    out: list[dict[str, str]] = []
    base = opf.parent.relative_to(job.book_dir)
    for item_id in re.findall(r'<itemref\s[^>]*idref="([^"]+)"', text):
        href = manifest.get(item_id)
        if not href or not href.endswith((".xhtml", ".html")):
            continue
        path = (base / href).as_posix()
        name = Path(href).name
        structural = _STRUCTURAL_TITLES.get(name)
        out.append(
            {
                "href": path,
                "title": structural or _title_of(job.book_dir / path, href),
            }
        )
    return out


#: Pages whose <title> is the book's title and would otherwise show up
#: twice in the chapter list.
_STRUCTURAL_TITLES = {
    "cover.xhtml": "Cover",
    "title.xhtml": "Title page",
    "nav.xhtml": "Contents",
}


def _title_of(path: Path, fallback: str) -> str:
    import html as html_module
    import re

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return fallback
    match = re.search(r"<title[^>]*>(.*?)</title>", content, re.DOTALL | re.IGNORECASE)
    if match:
        title = html_module.unescape(re.sub(r"\s+", " ", match.group(1))).strip()
        if title:
            return title
    return fallback


def run(
    host: str = "127.0.0.1",
    port: int = 8731,
    open_browser: bool = True,
) -> None:
    """Start the server and point the default browser at it."""
    import uvicorn

    app = create_app()
    url = f"http://{host}:{port}/"
    if open_browser:
        Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"paper2ink GUI on {url}  (Ctrl+C to stop)")
    uvicorn.run(app, host=host, port=port, log_level="warning")
