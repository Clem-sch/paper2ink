"""Conversion jobs for the web UI.

Conversions are long (minutes with the Docling parser), so the HTTP layer
never waits for one: a job is queued, a single worker thread runs it, and
the browser polls for progress.  One worker on purpose — two parsers
fighting over the same CPU make both slower and the progress meaningless.
"""

from __future__ import annotations

import logging
import shutil
import threading
import uuid
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..model import Document
from ..pipeline import ConversionResult, ConversionSettings, convert

log = logging.getLogger("paper2ink.gui")

__all__ = ["Job", "JobStore"]

#: Page images are rendered on demand at this resolution; enough to read
#: the small print of a two-column paper when zoomed.
PAGE_DPI = 110


@dataclass
class Job:
    """One PDF on its way to an EPUB."""

    id: str
    name: str
    pdf_path: Path
    work_dir: Path
    settings: ConversionSettings
    status: str = "queued"          # queued | running | done | failed
    message: str = "waiting"
    created: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    result: ConversionResult | None = None
    error: str | None = None
    _future: Future | None = field(default=None, repr=False)

    # -- derived paths -----------------------------------------------------
    @property
    def epub_path(self) -> Path | None:
        if self.result and self.result.epub_path and self.result.epub_path.exists():
            return self.result.epub_path
        return None

    @property
    def document(self) -> Document | None:
        return self.result.document if self.result else None

    @property
    def book_dir(self) -> Path:
        """Where the EPUB is unpacked so the browser can render it."""
        return self.work_dir / "book"

    @property
    def page_dir(self) -> Path:
        return self.work_dir / "pages"

    # -- serialisation for the browser -------------------------------------
    def summary(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "created": self.created,
            "error": self.error,
            "parser": self.settings.parser,
        }
        build = self.result.build if self.result else None
        if build:
            data["build"] = {
                "chapters": build.chapters,
                "figures": build.figures,
                "tables": build.tables,
                "equations": build.equations,
                "links": build.links,
                "unresolved": build.unresolved,
                "references": build.references,
            }
            data["epub"] = self.result.epub_path.name if self.result.epub_path else None
        document = self.document
        if document is not None:
            data["suspicious"] = len(document.suspicious_drops)
            data["dropped"] = len(document.dropped)
            data["pages"] = document.page_count
        if self.result:
            data["warnings"] = self.result.warnings[:20]
            validation = self.result.validation
            if validation:
                data["validation"] = {
                    "ok": validation.ok,
                    "epubcheck": validation.epubcheck_ok,
                    "errors": validation.errors[:20],
                    "warnings": validation.warnings[:10],
                }
        return data


class JobStore:
    """Keeps the jobs of one GUI session and runs them one at a time."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="convert")

    # -- lifecycle ---------------------------------------------------------
    def create(
        self, name: str, data: bytes, settings: ConversionSettings
    ) -> Job:
        job_id = uuid.uuid4().hex[:12]
        work_dir = self.root / job_id
        work_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = work_dir / _safe_name(name)
        pdf_path.write_bytes(data)

        job = Job(
            id=job_id,
            name=name,
            pdf_path=pdf_path,
            work_dir=work_dir,
            settings=settings,
        )
        with self._lock:
            self._jobs[job_id] = job
        job._future = self._pool.submit(self._run, job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        shutil.rmtree(job.work_dir, ignore_errors=True)
        return True

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # -- the worker --------------------------------------------------------
    def _run(self, job: Job) -> None:
        job.status = "running"
        job.message = "starting"

        def progress(message: str) -> None:
            job.message = message

        try:
            out_path = job.work_dir / "out.epub"
            result = convert(job.pdf_path, out_path, job.settings, progress=progress)
            job.result = result
            if result.ok:
                _unpack(result.epub_path, job.book_dir)
                job.status = "done"
                job.message = "finished"
            else:
                job.status = "failed"
                job.error = result.error or "conversion failed"
                job.message = "failed"
        except Exception as exc:  # pragma: no cover - the worker must not die
            log.exception("job %s crashed", job.id)
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.message = "failed"


def _safe_name(name: str) -> str:
    """Keep the user's file name but strip anything path-like."""
    cleaned = Path(name).name
    cleaned = "".join(ch for ch in cleaned if ch not in '<>:"/\\|?*')
    return cleaned[:150] or "paper.pdf"


def _unpack(epub_path: Path | None, target: Path) -> None:
    """Extract the EPUB so its pages can be served to an iframe."""
    if epub_path is None or not epub_path.exists():
        return
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(epub_path) as archive:
        for member in archive.namelist():
            # Never let a crafted archive escape the target directory.
            destination = (target / member).resolve()
            if not str(destination).startswith(str(target.resolve())):
                continue
            if member.endswith("/"):
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(member))
