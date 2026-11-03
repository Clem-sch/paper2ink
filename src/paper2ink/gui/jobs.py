"""Conversion jobs for the web UI.

Conversions are long (minutes with the Docling parser), so the HTTP layer
never waits for one: a job is queued, a single worker thread runs it, and
the browser polls for progress.  One worker on purpose - two parsers
fighting over the same CPU make both slower and the progress meaningless.

A job reports *done* as soon as the EPUB exists.  EPUBCheck then runs as a
separate step and updates the record when it finishes, so a slow external
validator never stands between the user and a finished book.
"""

from __future__ import annotations

import logging
import shutil
import threading
import uuid
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..library import Library, entry_from_result
from ..model import Document
from ..pipeline import ConversionResult, ConversionSettings, convert
from ..settings import Settings
from ..validate import validate_epub

log = logging.getLogger("paper2ink.gui")

__all__ = ["Job", "JobStore", "PAGE_DPI"]

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
    entry_id: int | None = None
    #: none | running | done - EPUBCheck runs after the job reports done.
    validation_state: str = "none"
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
            "entry_id": self.entry_id,
            "validation_state": self.validation_state,
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
        if self.epub_path:
            data["epub"] = self.epub_path.name
            data["epub_dir"] = str(self.epub_path.parent)
        document = self.document
        if document is not None:
            data["suspicious"] = len(document.suspicious_drops)
            data["dropped"] = len(document.dropped)
            data["pages"] = document.page_count
            data["title"] = document.metadata.title
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

    def __init__(
        self,
        root: Path,
        library: Library | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.library = library or Library(root / "library.sqlite3")
        self.settings = settings or Settings()
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="convert")

    # -- lifecycle ---------------------------------------------------------
    def create(self, name: str, data: bytes, settings: ConversionSettings) -> Job:
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
            # Validation is a separate step below, so the book is ready to
            # use the moment it is built.
            settings = replace(job.settings, validate=False)
            result = convert(job.pdf_path, None, settings, progress=progress)
            job.result = result
            if not result.ok:
                job.status = "failed"
                job.error = result.error or "conversion failed"
                job.message = "failed"
                return

            result.epub_path = self._publish(result.epub_path)
            _unpack(result.epub_path, job.book_dir)
            job.entry_id = self.library.add(
                entry_from_result(result, job.settings.parser)
            ).id
            job.status = "done"
            job.message = "finished"
        except Exception as exc:  # pragma: no cover - the worker must not die
            log.exception("job %s crashed", job.id)
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.message = "failed"
            return

        if job.settings.validate:
            self._validate(job)

    def _publish(self, epub_path: Path | None) -> Path | None:
        """Move the finished book out of the work directory."""
        if epub_path is None or not epub_path.exists():
            return epub_path
        target_dir = self.settings.output_path
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            target = _free_name(target_dir / epub_path.name)
            shutil.move(str(epub_path), str(target))
            return target
        except OSError as exc:
            log.warning("could not write to %s: %s", target_dir, exc)
            return epub_path

    def _validate(self, job: Job) -> None:
        """Run the checks after the job already reports done."""
        job.validation_state = "running"
        try:
            report = validate_epub(job.epub_path) if job.epub_path else None
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("validation of job %s crashed", job.id)
            job.validation_state = "done"
            if job.result:
                job.result.warnings.append(f"validation failed: {exc}")
            return
        if report is not None and job.result is not None:
            job.result.validation = report
            if job.entry_id:
                self.library.update(job.entry_id, valid=1 if report.ok else 0)
        job.validation_state = "done"


def _safe_name(name: str) -> str:
    """Keep the user's file name but strip anything path-like."""
    cleaned = Path(name).name
    cleaned = "".join(ch for ch in cleaned if ch not in '<>:"/\\|?*')
    return cleaned[:150] or "paper.pdf"


def _free_name(path: Path) -> Path:
    """A path that does not overwrite an existing book."""
    if not path.exists():
        return path
    for index in range(2, 100):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path


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
