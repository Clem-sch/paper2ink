"""Post-build validation.

Send to Kindle rejects malformed EPUBs without saying why, so two layers of
checking run after every build: paper2ink's own structural checks (every
internal link has a target, every image exists, no empty chapter) and, when
Java and EPUBCheck are available, EPUBCheck itself.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("paper2ink.validate")

__all__ = ["ValidationReport", "validate_epub", "run_epubcheck", "find_epubcheck"]

_ID_RE = re.compile(r'\bid="([^"]+)"')
_HREF_RE = re.compile(r'\bhref="([^"]+)"')
_SRC_RE = re.compile(r'\bsrc="([^"]+)"')
_BODY_RE = re.compile(r"<body[^>]*>(.*)</body>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")

EPUBCHECK_HINT = (
    "EPUBCheck was not found. Install it from "
    "https://github.com/w3c/epubcheck/releases, then either put 'epubcheck' on "
    "PATH or set EPUBCHECK_JAR to the epubcheck.jar path."
)


@dataclass
class ValidationReport:
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    epubcheck_ran: bool = False
    epubcheck_ok: bool | None = None

    def add_error(self, message: str) -> None:
        self.errors.append(message)
        self.ok = False

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)


# --------------------------------------------------------------------------
# own checks
# --------------------------------------------------------------------------


def validate_epub(path: Path, *, run_external: bool = True) -> ValidationReport:
    """Check an EPUB for broken internal links, missing images, empty pages."""
    report = ValidationReport()
    if not path.exists():
        report.add_error(f"EPUB not written: {path}")
        return report

    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        documents = {
            name: archive.read(name).decode("utf-8", "replace")
            for name in names
            if name.lower().endswith((".xhtml", ".html"))
        }

        ids: dict[str, set[str]] = {
            name: set(_ID_RE.findall(content)) for name, content in documents.items()
        }

        for name, content in documents.items():
            base = Path(name).parent
            for href in _HREF_RE.findall(content):
                if href.startswith(("http://", "https://", "mailto:", "data:")):
                    continue
                target, _, anchor = href.partition("#")
                if target:
                    resolved = _normalise(base / target)
                    if resolved not in names:
                        report.add_error(f"{name}: link to missing file {href}")
                        continue
                else:
                    resolved = name
                if anchor and anchor not in ids.get(resolved, set()):
                    report.add_error(f"{name}: link to missing anchor {href}")

            for src in _SRC_RE.findall(content):
                if src.startswith(("http://", "https://", "data:")):
                    continue
                resolved = _normalise(base / src)
                if resolved not in names:
                    report.add_error(f"{name}: missing image {src}")

            if _is_empty(content) and "nav" not in name.lower():
                report.add_warning(f"{name}: page has no visible content")

    if run_external:
        _append_epubcheck(path, report)
    return report


def _normalise(path: Path) -> str:
    return Path(os.path.normpath(str(path))).as_posix()


def _is_empty(content: str) -> bool:
    body = _BODY_RE.search(content)
    if not body:
        return True
    inner = body.group(1)
    if re.search(r"<(?:img|image|svg)\b", inner, re.IGNORECASE):
        return False  # a cover page is a single image and that is fine
    return not _TAG_RE.sub(" ", inner).strip()


# --------------------------------------------------------------------------
# EPUBCheck
# --------------------------------------------------------------------------


def find_epubcheck() -> list[str] | None:
    """Return the command that runs EPUBCheck, or ``None``."""
    jar = os.environ.get("EPUBCHECK_JAR")
    if jar and Path(jar).exists():
        java = shutil.which("java")
        if java:
            return [java, "-jar", jar]
    executable = shutil.which("epubcheck")
    if executable:
        return [executable]
    java = shutil.which("java")
    if java:
        for candidate in (
            Path("epubcheck/epubcheck.jar"),
            Path.home() / "epubcheck" / "epubcheck.jar",
        ):
            if candidate.exists():
                return [java, "-jar", str(candidate)]
    return None


def run_epubcheck(path: Path) -> tuple[bool, str] | None:
    """Run EPUBCheck; ``None`` means it is not installed."""
    command = find_epubcheck()
    if command is None:
        return None
    try:
        completed = subprocess.run(
            [*command, str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        return False, f"could not run EPUBCheck: {exc}"
    output = (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode == 0, output


def _append_epubcheck(path: Path, report: ValidationReport) -> None:
    result = run_epubcheck(path)
    if result is None:
        report.add_warning(EPUBCHECK_HINT)
        return
    report.epubcheck_ran = True
    ok, output = result
    report.epubcheck_ok = ok
    if ok:
        return
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("ERROR") or stripped.startswith("FATAL"):
            report.add_error(f"EPUBCheck: {stripped}")
        elif stripped.startswith("WARNING"):
            report.add_warning(f"EPUBCheck: {stripped}")
    if not report.errors:
        report.add_error("EPUBCheck reported a problem:\n" + output.strip()[:2000])
