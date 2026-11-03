"""FastAPI application behind the web UI.

Binds to localhost only and serves four things: the single-page app, the
job API, the unpacked EPUB (so the browser renders the finished book
itself) and rendered PDF pages for the review view.
"""

from __future__ import annotations

import logging
import re
import tempfile
from contextlib import asynccontextmanager
import webbrowser
from pathlib import Path
from threading import Timer
from typing import Any

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..library import SORT_FIELDS, Library
from ..model import Document
from ..pipeline import ConversionSettings
from ..settings import (
    Settings,
    config_dir,
    load_settings,
    save_settings,
    settings_path,
)
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
    if store is None:
        user_settings = load_settings()
        store = JobStore(
            Path(tempfile.mkdtemp(prefix="paper2ink-gui-")),
            library=Library(config_dir() / "library.sqlite3"),
            settings=user_settings,
        )
    app.state.store = store

    # -- the page ----------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    app.mount("/static", _FreshStatic(directory=STATIC_DIR), name="static")

    # -- jobs --------------------------------------------------------------
    @app.post("/api/jobs")
    async def create_job(
        file: UploadFile,
        parser: str = Form(""),
        dpi: int = Form(0),
        color: bool = Form(False),
        tables: str = Form(""),
        cover: bool = Form(True),
    ) -> JSONResponse:
        defaults = _store(app).settings
        parser = parser or defaults.parser
        dpi = dpi or defaults.dpi
        tables = tables or defaults.tables
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
            validate=defaults.validate,
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

    # -- settings ----------------------------------------------------------
    @app.get("/api/settings")
    def read_settings() -> dict[str, Any]:
        settings = _store(app).settings
        return {
            **settings.to_dict(),
            "path": str(settings_path()),
            "output_exists": settings.output_path.is_dir(),
        }

    @app.put("/api/settings")
    def write_settings(payload: dict[str, Any]) -> dict[str, Any]:
        store = _store(app)
        settings = store.settings
        email = str(payload.get("kindle_email", settings.kindle_email)).strip()
        if email and not _looks_like_email(email):
            raise HTTPException(400, "that does not look like an e-mail address")
        settings.kindle_email = email

        output = str(payload.get("output_dir", settings.output_dir)).strip()
        if output:
            try:
                target = Path(output).expanduser()
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise HTTPException(400, f"cannot use that folder: {exc}") from exc
            settings.output_dir = str(target)

        for key, allowed in (
            ("parser", ("auto", "pymupdf", "docling")),
            ("tables", ("image", "html")),
        ):
            if key in payload:
                value = str(payload[key])
                if value not in allowed:
                    raise HTTPException(400, f"unknown {key}")
                setattr(settings, key, value)
        if "dpi" in payload:
            settings.dpi = max(72, min(600, int(payload["dpi"])))
        for flag in ("color", "cover", "validate"):
            if flag in payload:
                setattr(settings, flag, bool(payload[flag]))

        save_settings(settings)
        return {**settings.to_dict(), "path": str(settings_path())}

    # -- the shelf ---------------------------------------------------------
    @app.get("/api/library")
    def library_list(
        search: str = "",
        sent: str = "",
        starred: str = "",
        parser: str = "",
        year: str = "",
        sort: str = "created",
        order: str = "desc",
        limit: int = 500,
    ) -> dict[str, Any]:
        store = _store(app)
        entries = store.library.list(
            search=search,
            sent=_tristate(sent),
            starred=True if starred == "yes" else None,
            parser=parser,
            year=year,
            sort=sort,
            descending=order != "asc",
            limit=limit,
        )
        return {
            "entries": [entry.to_dict() for entry in entries],
            "stats": store.library.stats(),
            "sort_fields": list(SORT_FIELDS),
            "parsers": store.library.known_values("parser"),
            "years": store.library.known_values("year"),
        }

    @app.post("/api/library/{entry_id}/sent")
    def set_sent(entry_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        sent = True if payload is None else bool(payload.get("sent", True))
        if not _store(app).library.mark_sent(entry_id, sent):
            raise HTTPException(404, "no such entry")
        return _entry_or_404(app, entry_id)

    @app.post("/api/library/{entry_id}/starred")
    def set_starred(entry_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        if not _store(app).library.set_starred(entry_id, bool(payload.get("starred"))):
            raise HTTPException(404, "no such entry")
        return _entry_or_404(app, entry_id)

    @app.delete("/api/library/{entry_id}")
    def delete_entry(entry_id: int, remove_file: bool = False) -> dict[str, bool]:
        store = _store(app)
        entry = store.library.get(entry_id)
        if entry is None:
            raise HTTPException(404, "no such entry")
        if remove_file and entry.epub_exists:
            try:
                Path(entry.epub_path).unlink()
            except OSError as exc:
                raise HTTPException(500, f"could not delete the file: {exc}") from exc
        return {"deleted": store.library.delete(entry_id)}

    @app.post("/api/library/forget-missing")
    def forget_missing() -> dict[str, int]:
        return {"removed": _store(app).library.forget_missing()}

    @app.get("/api/library/{entry_id}/epub")
    def download_entry(entry_id: int) -> FileResponse:
        entry = _store(app).library.get(entry_id)
        if entry is None or not entry.epub_exists:
            raise HTTPException(404, "that book is no longer on disk")
        return FileResponse(
            entry.epub_path,
            media_type="application/epub+zip",
            filename=Path(entry.epub_path).name,
        )

    # -- sending -----------------------------------------------------------
    @app.post("/api/library/{entry_id}/send")
    def send_entry(entry_id: int) -> dict[str, Any]:
        """Prepare a Send-to-Kindle mail and show the user the file.

        A ``mailto:`` link cannot carry an attachment - mail clients refuse
        that - so the draft is opened pre-addressed and the folder holding
        the EPUB is revealed, leaving one drag to the user.
        """
        store = _store(app)
        entry = store.library.get(entry_id)
        if entry is None:
            raise HTTPException(404, "no such entry")
        if not entry.epub_exists:
            raise HTTPException(404, "that book is no longer on disk")
        if not store.settings.kindle_email:
            raise HTTPException(
                400, "no Send-to-Kindle address is set; add one in Settings"
            )

        subject = entry.title or Path(entry.epub_path).stem
        url = store.settings.mailto_url(subject[:120])
        revealed = _reveal(Path(entry.epub_path))
        return {
            "mailto": url,
            "file": entry.epub_path,
            "folder": str(Path(entry.epub_path).parent),
            "revealed": revealed,
        }

    @app.post("/api/library/{entry_id}/reveal")
    def reveal_entry(entry_id: int) -> dict[str, Any]:
        entry = _store(app).library.get(entry_id)
        if entry is None or not entry.epub_exists:
            raise HTTPException(404, "that book is no longer on disk")
        return {"revealed": _reveal(Path(entry.epub_path))}

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


def _entry_or_404(app: FastAPI, entry_id: int) -> dict[str, Any]:
    entry = _store(app).library.get(entry_id)
    if entry is None:
        raise HTTPException(404, "no such entry")
    return entry.to_dict()


def _tristate(value: str) -> bool | None:
    """Turn a "yes"/"no"/"" query parameter into True/False/None."""
    return {"yes": True, "no": False}.get(value)


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _looks_like_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value))


def _reveal(path: Path) -> bool:
    """Open the folder holding *path* in the system file manager.

    This runs on the user's own machine, which is the only reason it can
    be done at all: a browser cannot open Explorer.
    """
    import subprocess
    import sys

    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
        return True
    except (OSError, ValueError) as exc:  # pragma: no cover - platform specific
        log.warning("could not reveal %s: %s", path, exc)
        return False


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
