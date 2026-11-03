"""Extracting the bibliography, shared by all parser backends.

Reference lists are the part of a paper that survives PDF extraction worst:
set in two narrow columns and a smaller font, they come back as dozens of
layout blocks that start and end mid-entry, and several publishers print no
"References" heading at all.  Both backends hit the same problem, so the
handling lives here.
"""

from __future__ import annotations

import re

from .cleanup import join_lines
from .model import Document, Reference
from .refs import extract_doi, normalize_key

__all__ = [
    "BRACKET_MARKER_RE",
    "ENTRY_MARKER_RE",
    "AUTHOR_YEAR_ENTRY_RE",
    "make_reference",
    "split_bracketed_references",
    "split_marked_references",
    "harvest_numeric_bibliography",
    "harvest_list_bibliography",
    "is_bracketed_reference_run",
]

#: "[12]" markers inside a run of concatenated entries.
BRACKET_MARKER_RE = re.compile(r"\[(\d{1,3})\]\s*")

#: Publishers number entries either in square brackets (Wiley, Elsevier) or
#: in parentheses (ACS).  Both pack several entries into one layout block.
_MARKER_STYLES: dict[str, tuple[re.Pattern[str], re.Pattern[str]]] = {
    "bracket": (BRACKET_MARKER_RE, re.compile(r"\[1\]\s")),
    "paren": (re.compile(r"\((\d{1,3})\)\s"), re.compile(r"\(1\)\s")),
    # RSC numbers entries with a bare digit: "4 L. Fenno, Annu. Rev. ...".
    # Bare numbers are everywhere in a reference list ("2011, 34, 389"), so
    # the marker only counts after a full stop and before a capital, which
    # is where the previous entry ended and the next author begins.
    "bare": (
        re.compile(r"(?:^|(?<=\.))\s*(\d{1,3})\s+(?=[A-Z])"),
        re.compile(r"(?:^|(?<=\.))\s*1\s+(?=[A-Z])"),
    ),
}

#: References are long; an "entry" far shorter than this is an equation
#: number or a cross reference that happened to look like a marker.
_MIN_MEAN_ENTRY_LENGTH = 20

#: The leading number of one entry: "[12] ", "(12) " or "12. ".
ENTRY_MARKER_RE = re.compile(r"^\s*(?:\[(\d{1,3})\]|\((\d{1,3})\)|(\d{1,3})[.)])\s+")

#: An author-year entry, recognised by the parenthesised year near the front.
#: Matching the author list instead is a trap: surnames carry particles
#: ("de Jong"), non-ASCII initials ("Stefl") and corporate names ("Blender
#: Online Community"), none of which fit a tidy pattern.
AUTHOR_YEAR_ENTRY_RE = re.compile(r"^.{0,250}?\((?:19|20)\d{2}[a-z]?\)")

#: Share of the entries that must carry a four-digit year.
_YEAR_SHARE = 0.7

_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")

#: A bibliography must have at least this many entries to be believed.
_MIN_ENTRIES = 5


def make_reference(number: str, raw: str) -> Reference:
    """Build one :class:`Reference`, pulling out the DOI and the year."""
    year = re.search(r"\b(19|20)\d{2}\b", raw)
    return Reference(
        key=normalize_key("citation", number),
        number=number,
        raw=raw.strip(),
        doi=extract_doi(raw),
        authors=raw.split(".")[0][:120],
        year=year.group() if year else "",
    )


def split_marked_references(text: str, style: str = "bracket") -> list[tuple[str, str]]:
    """Split ``"[1] A ... [2] B ..."`` into ``(number, text)`` pairs."""
    pattern = _MARKER_STYLES[style][0]
    markers = list(pattern.finditer(text))
    out: list[tuple[str, str]] = []
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        entry = text[marker.end() : end].strip()
        if entry:
            out.append((marker.group(1), entry))
    return out


def split_bracketed_references(text: str) -> list[tuple[str, str]]:
    """Split a square-bracket run; see :func:`split_marked_references`."""
    return split_marked_references(text, "bracket")


def is_bracketed_reference_run(text: str) -> bool:
    """Whether a single block holds several ``[n]`` entries in a row."""
    return text.lstrip().startswith("[") and len(BRACKET_MARKER_RE.findall(text)) >= 3


def harvest_numeric_bibliography(doc: Document) -> list[Reference]:
    """Collect a ``[1] ... [2] ...`` bibliography that spans many blocks.

    Several publishers (Wiley among them) print no "References" heading and
    break the list across pages, so the bracketed markers are the only
    reliable handle. Everything from the first ``[1]`` in the latter part of
    the paper is taken as bibliography and re-split on those markers.

    Returns the entries and removes the consumed blocks from *doc*; an empty
    list means no such bibliography was found and nothing was changed.
    """
    found = _find_start(doc)
    if found is None:
        return []
    start, offset, style = found

    texts = [doc.blocks[start].text[offset:]]
    texts.extend(block.text for block in doc.blocks[start + 1 :])
    pairs = split_marked_references(join_lines(texts), style)
    if len(pairs) < _MIN_ENTRIES:
        return []
    mean_length = sum(len(raw) for _, raw in pairs) / len(pairs)
    if mean_length < _MIN_MEAN_ENTRY_LENGTH:
        # Numbered equations, not a bibliography.
        return []
    with_year = sum(1 for _, raw in pairs if _YEAR_RE.search(raw))
    if with_year < len(pairs) * _YEAR_SHARE:
        # Sources carry years; a run of numbered list items does not.
        return []

    # The bibliography may start part-way into a block; keep what came before
    # it as ordinary text.
    head = doc.blocks[start].text[:offset].strip()
    del doc.blocks[start:]
    if head:
        from .model import Block

        doc.blocks.append(Block(id=f"{start:04d}-tail", type="paragraph", text=head))
    seen: set[str] = set()
    entries: list[Reference] = []
    for number, raw in pairs:
        reference = make_reference(number, raw)
        if reference.key in seen:
            continue
        seen.add(reference.key)
        entries.append(reference)
    return entries


#: How many blocks from the candidate start are inspected to decide whether
#: a "[1]" really opens the bibliography.
_START_WINDOW = 3


#: Page furniture between two reference list items (a stray running head)
#: may be this short without breaking the run.
_NOISE_LENGTH = 80
#: Blocks allowed after the last list item before the run stops counting as
#: the closing bibliography.
_TRAILING_SLACK = 3


def harvest_list_bibliography(doc: Document) -> list[Reference]:
    """Collect a bibliography that the parser already split into list items.

    Docling recognises numbered reference lists and hands them over as
    ``list_item`` blocks with the ``[n]`` markers stripped, so there is no
    marker left to split on. The handle is instead a run of list items that
    closes the document and mostly carries years; they are numbered by their
    order, which is the order the paper printed them in.

    Returns the entries and removes the consumed blocks from *doc*; an empty
    list means nothing matched and nothing was changed.
    """
    blocks = doc.blocks
    last = next(
        (i for i in range(len(blocks) - 1, -1, -1) if blocks[i].type == "list_item"),
        None,
    )
    if last is None or len(blocks) - 1 - last > _TRAILING_SLACK:
        return []

    start = last
    while start > 0:
        previous = blocks[start - 1]
        if previous.type == "list_item":
            start -= 1
            continue
        # A running head that survived the clean-up may sit *between* two
        # entries, but it must not pull the run back into the body text.
        if (
            previous.type != "heading"
            and len(previous.text) < _NOISE_LENGTH
            and start >= 2
            and blocks[start - 2].type == "list_item"
        ):
            start -= 2
            continue
        break

    items = [b for b in blocks[start : last + 1] if b.type == "list_item"]
    if len(items) < _MIN_ENTRIES or start < len(blocks) * 0.3:
        return []
    with_year = sum(1 for b in items if _YEAR_RE.search(b.text))
    if with_year < len(items) * _YEAR_SHARE:
        return []               # a real list in the body, not a bibliography

    entries = [
        make_reference(str(number), block.text)
        for number, block in enumerate(items, start=1)
    ]
    del doc.blocks[start : last + 1]
    return entries


def _find_start(doc: Document) -> tuple[int, int, str] | None:
    """Where the bibliography begins: ``(block index, offset, marker style)``.

    The list does not always start a block of its own — a layout block can
    end a paragraph and run straight into "[1] ...", so the marker is looked
    for inside the block too. It is only believed when the markers keep
    coming right after it: a lone ``[1]`` in body text is a citation.
    """
    threshold = len(doc.blocks) * 0.4
    for index, block in enumerate(doc.blocks):
        if index < threshold:
            continue           # "[1]" early on is a citation, not the list
        for style, (marker_re, first_re) in _MARKER_STYLES.items():
            first = first_re.search(block.text)
            if first is None:
                continue
            window = " ".join(
                [block.text[first.start() :]]
                + [b.text for b in doc.blocks[index + 1 : index + _START_WINDOW]]
            )
            if len(marker_re.findall(window)) >= 3:
                return index, first.start(), style
    return None
