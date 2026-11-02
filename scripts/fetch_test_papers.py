"""Download a handful of open-access papers for manual end-to-end testing.

The PDFs are not kept in the repository.  Run this once to populate
``tests/fixtures/papers`` with layouts that stress different parts of the
pipeline, then point paper2ink at that folder:

    python scripts/fetch_test_papers.py
    paper2ink tests/fixtures/papers --batch --debug-layout

Everything listed here is openly licensed (arXiv or open access).
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "papers"

PAPERS: list[tuple[str, str, str]] = [
    # (file name, url, why it is interesting)
    (
        "arxiv-1706.03762-attention.pdf",
        "https://arxiv.org/pdf/1706.03762",
        "single column, many tables and equations",
    ),
    (
        "arxiv-1512.03385-resnet.pdf",
        "https://arxiv.org/pdf/1512.03385",
        "two columns, numeric citations, many figures",
    ),
    (
        "arxiv-1412.6980-adam.pdf",
        "https://arxiv.org/pdf/1412.6980",
        "heavy on displayed equations and algorithms",
    ),
    (
        "arxiv-2203.02155-instructgpt.pdf",
        "https://arxiv.org/pdf/2203.02155",
        "long paper with a large supplementary appendix",
    ),
]


def fetch(target_dir: Path, force: bool = False) -> int:
    target_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    for name, url, why in PAPERS:
        destination = target_dir / name
        if destination.exists() and not force:
            print(f"skip   {name} (already there)")
            continue
        print(f"fetch  {name}  <- {url}\n       ({why})")
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "paper2ink-test-fetcher"}
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                destination.write_bytes(response.read())
        except Exception as exc:
            print(f"       failed: {exc}", file=sys.stderr)
            failures += 1
    print(f"\npapers in {target_dir}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR,
                        help=f"target folder (default: {DEFAULT_DIR})")
    parser.add_argument("--force", action="store_true",
                        help="re-download papers that are already present")
    args = parser.parse_args()
    return 1 if fetch(args.dir, args.force) else 0


if __name__ == "__main__":
    raise SystemExit(main())
