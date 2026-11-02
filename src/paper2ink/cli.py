"""Command line interface.

The CLI is a thin layer over :mod:`paper2ink.pipeline`: it parses arguments,
shows progress and prints the summary.  All real work lives in the library so
a GUI can reuse it unchanged.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from . import __version__
from .config import load_config
from .formulas import FORMULA_BACKENDS
from .parsers import PARSER_NAMES
from .pipeline import ConversionResult, ConversionSettings, convert

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    add_completion=False,
    help="Convert scientific PDFs into reflowable, Kindle-friendly EPUB3 files.",
)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"paper2ink {__version__}")
        raise typer.Exit()


@app.command()
def main(
    source: Path = typer.Argument(
        ...,
        exists=True,
        readable=True,
        help="A PDF file, or a folder together with --batch.",
    ),
    output: Optional[Path] = typer.Option(
        None, "-o", "--output",
        help="Output EPUB path (single file only). Default: next to the PDF.",
    ),
    batch: bool = typer.Option(
        False, "--batch", help="Convert every PDF in the given folder."
    ),
    recursive: bool = typer.Option(
        False, "--recursive", help="With --batch, also search sub-folders."
    ),
    parser: str = typer.Option(
        "auto", "--parser",
        help=f"Parser backend: {', '.join(PARSER_NAMES)}.",
    ),
    dpi: int = typer.Option(250, "--dpi", min=72, max=600,
                            help="Resolution for figure and table crops."),
    color: bool = typer.Option(
        False, "--color/--grayscale",
        help="Keep colour instead of converting crops to grayscale.",
    ),
    tables: str = typer.Option(
        "image", "--tables", help="Table rendering: image or html."
    ),
    formulas: str = typer.Option(
        "off", "--formulas",
        help="Formula handling: off, or latex to attach recognised LaTeX.",
    ),
    formula_backend: str = typer.Option(
        "docling", "--formula-backend",
        help=f"Formula recognizer: {', '.join(FORMULA_BACKENDS)}.",
    ),
    cover: bool = typer.Option(True, "--cover/--no-cover",
                               help="Generate a plain black-and-white cover."),
    azw3: bool = typer.Option(
        False, "--azw3",
        help="Also write AZW3 via Calibre's ebook-convert, if installed.",
    ),
    no_validate: bool = typer.Option(
        False, "--no-validate", help="Skip EPUBCheck and the internal checks."
    ),
    dump_json: bool = typer.Option(
        False, "--dump-json", help="Write the intermediate document model as JSON."
    ),
    debug_layout: bool = typer.Option(
        False, "--debug-layout",
        help="Write one PNG per page with block boxes and types drawn on it.",
    ),
    keep_temp: bool = typer.Option(
        False, "--keep-temp", help="Keep the temporary working directory."
    ),
    config: Optional[Path] = typer.Option(
        None, "--config", help="paper2ink.toml with defaults and boilerplate regexes."
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Verbose logging."),
    quiet: bool = typer.Option(False, "-q", "--quiet", help="Only print errors."),
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """Convert PAPER.pdf (or a whole folder) into Kindle-ready EPUB3."""
    _setup_logging(verbose, quiet)

    if tables not in ("image", "html"):
        raise typer.BadParameter("--tables must be 'image' or 'html'")
    if formulas not in ("off", "latex"):
        raise typer.BadParameter("--formulas must be 'off' or 'latex'")
    if parser not in PARSER_NAMES:
        raise typer.BadParameter(f"--parser must be one of {', '.join(PARSER_NAMES)}")

    settings = ConversionSettings(
        parser=parser,
        dpi=dpi,
        color=color,
        tables=tables,
        formulas=formulas,
        formula_backend=formula_backend,
        cover=cover,
        azw3=azw3,
        dump_json=dump_json,
        keep_temp=keep_temp,
        debug_layout=debug_layout,
        validate=not no_validate,
    )
    settings = load_config(config, source, settings)

    pdfs = _collect_pdfs(source, batch, recursive)
    if not pdfs:
        err_console.print("[red]No PDF files found.[/red]")
        raise typer.Exit(code=2)
    if output and len(pdfs) > 1:
        raise typer.BadParameter("-o cannot be combined with multiple input files")

    if settings.formulas == "latex" and not quiet:
        console.print(
            "[yellow]Formula recognition is on: expect roughly one to three "
            "seconds per formula on a laptop CPU.[/yellow]"
        )

    results = _run(pdfs, output, settings, quiet)
    _print_summary(results, quiet)
    failed = [r for r in results if not r.ok]
    invalid = [
        r for r in results
        if r.ok and r.validation is not None and not r.validation.ok
    ]
    raise typer.Exit(code=1 if failed or invalid else 0)


# --------------------------------------------------------------------------


def _setup_logging(verbose: bool, quiet: bool) -> None:
    level = logging.DEBUG if verbose else logging.ERROR if quiet else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=err_console, rich_tracebacks=True,
                              show_path=verbose)],
    )


def _collect_pdfs(source: Path, batch: bool, recursive: bool) -> list[Path]:
    if source.is_file():
        return [source]
    if not batch:
        err_console.print(
            f"[red]{source} is a folder; pass --batch to convert all PDFs in it."
            "[/red]"
        )
        raise typer.Exit(code=2)
    pattern = "**/*.pdf" if recursive else "*.pdf"
    return sorted(p for p in source.glob(pattern) if p.is_file())


def _run(
    pdfs: list[Path],
    output: Path | None,
    settings: ConversionSettings,
    quiet: bool,
) -> list[ConversionResult]:
    results: list[ConversionResult] = []
    if quiet:
        for pdf in pdfs:
            results.append(convert(pdf, output, settings))
        return results

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("converting", total=len(pdfs))
        for pdf in pdfs:
            progress.update(task, description=f"{pdf.name[:48]}")

            def report(message: str, pdf=pdf) -> None:
                progress.update(task, description=f"{pdf.name[:32]} - {message}")

            results.append(convert(pdf, output, settings, progress=report))
            progress.advance(task)
    return results


def _print_summary(results: list[ConversionResult], quiet: bool) -> None:
    if quiet:
        for result in results:
            if not result.ok:
                err_console.print(f"FAILED {result.source.name}: {result.error}")
        return

    table = Table(title="paper2ink summary", show_lines=False)
    for column in ("paper", "sections", "figures", "tables", "eqs", "links",
                   "unresolved", "refs", "status"):
        table.add_column(column, overflow="fold")

    for result in results:
        if not result.ok:
            table.add_row(result.source.name[:40], "-", "-", "-", "-", "-", "-", "-",
                          f"[red]{result.error}[/red]")
            continue
        build = result.build
        status = "[green]ok[/green]"
        if result.validation is not None:
            if not result.validation.ok:
                status = f"[red]{len(result.validation.errors)} errors[/red]"
            elif result.validation.epubcheck_ran:
                status = "[green]ok (EPUBCheck)[/green]"
            else:
                status = "[yellow]ok (no EPUBCheck)[/yellow]"
        table.add_row(
            result.source.name[:40],
            str(build.chapters if build else 0),
            str(build.figures if build else 0),
            str(build.tables if build else 0),
            str(build.equations if build else 0),
            str(build.links if build else 0),
            str(build.unresolved if build else 0),
            str(build.references if build else 0),
            status,
        )
    console.print(table)

    for result in results:
        if result.epub_path and result.ok:
            console.print(f"  [bold]{result.epub_path}[/bold]")
        if result.azw3_path:
            console.print(f"  {result.azw3_path}")
        if result.json_path:
            console.print(f"  {result.json_path}")
        if result.debug_dir:
            console.print(f"  layout images: {result.debug_dir}")
        for warning in result.warnings[:10]:
            console.print(f"  [yellow]warning:[/yellow] {warning}")
        if result.validation:
            for warning in result.validation.warnings[:5]:
                console.print(f"  [yellow]warning:[/yellow] {warning}")
            for error in result.validation.errors[:10]:
                console.print(f"  [red]error:[/red] {error}")
            extra = len(result.validation.errors) - 10
            if extra > 0:
                console.print(f"  [red]... and {extra} more errors[/red]")


def run() -> None:  # pragma: no cover - console entry point
    try:
        app()
    except KeyboardInterrupt:
        err_console.print("interrupted")
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    run()
