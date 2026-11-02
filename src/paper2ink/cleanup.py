"""Text clean-up: the part that turns PDF line soup back into prose.

Everything here operates on the intermediate model, so it is shared by all
parser backends.  The expensive-to-get-right pieces are:

* joining lines while undoing end-of-line hyphenation but keeping real
  compounds ("state-of-the-art");
* dropping running heads, page numbers and preprint line numbers;
* stitching paragraphs back together across column and page breaks.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from .model import Block, Document

__all__ = [
    "DEFAULT_BOILERPLATE",
    "CleanupConfig",
    "normalize_text",
    "join_lines",
    "strip_boilerplate",
    "detect_running_heads",
    "head_signature",
    "heading_key",
    "FRONT_MATTER_RE",
    "FRONT_MATTER_LABEL_RE",
    "merge_paragraphs",
    "clean_document",
]

# Ligatures and look-alike characters that Kindle renders badly or that break
# word search.
_LIGATURES = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "ﬅ": "st",
    "ﬆ": "st",
    "‐": "-",
    "‑": "-",
    "­": "",      # soft hyphen
    "​": "",      # zero width space
    "﻿": "",
    " ": " ",
}

_LIGATURE_RE = re.compile("|".join(map(re.escape, _LIGATURES)))

# Publisher boilerplate, matched per line.  Extendable via paper2ink.toml.
DEFAULT_BOILERPLATE = [
    r"^\s*downloaded\s+from\b.*$",
    r"^.{0,60}?\bdownloaded\s+from\s+https?://.*$",
    r"^.*\bterms\s+and\s+conditions\s*\(https?://.*$",
    r"^.*\bwiley\s+online\s+library\s+(?:for\s+rules|on\s+\[?\d).*$",
    r"^\s*this\s+article\s+is\s+protected\s+by\s+copyright\b.*$",
    r"^\s*©\s*\d{4}\b.*$",
    r"^\s*copyright\s+©?\s*\d{4}\b.*$",
    r"^\s*all\s+rights\s+reserved\.?\s*$",
    r"^\s*see\s+discussions,\s+stats,\s+and\s+author\s+profiles\b.*$",
    r"^\s*(?:unauthorized|unauthorised)\s+reproduction\b.*$",
    r"^\s*for\s+personal\s+use\s+only\b.*$",
    r"^\s*licen[cs]ed?\s+under\s+(?:a\s+)?creative\s+commons\b.*$",
    r"^\s*(?:https?://)?(?:dx\.)?doi\.org/\S+\s*$",
    r"^\s*(?:www\.)?(?:nature|science|sciencedirect|pubs\.acs|onlinelibrary"
    r"|frontiersin)\.(?:com|org)\S*\s*$",
    r"^\s*page\s+\d+\s+of\s+\d+\s*$",
    r"^\s*\d+\s*$",  # bare page number
    r"^\s*this\s+is\s+an\s+open\s+access\s+article\b.*$",
]

# Compounds whose hyphen must survive a line break.  Checked on the joined
# word, lower-cased.
_KEEP_HYPHEN_PREFIXES = {
    "non", "self", "semi", "quasi", "pseudo", "anti", "co", "pre", "post",
    "sub", "super", "ultra", "micro", "nano", "multi", "cross", "high",
    "low", "long", "short", "well", "state", "in", "ex", "re", "intra",
    "inter", "trans", "cis", "para", "meta", "ortho", "mono", "di", "tri",
    "tetra", "poly", "bi", "two", "three", "one", "all", "real", "time",
}

_SENTENCE_END_RE = re.compile(r"[.!?][\"'”’)\]]*\s*$")
_LINE_NUMBER_RE = re.compile(r"^\s*\d{1,3}\s*$")

# Journal front matter that carries no content for the reader.  Matched
# against the start of a block on the first page only.  Shared by the
# parser backends, which both have to deal with it.
FRONT_MATTER_RE = re.compile(
    r"^\s*(?:open\s+access|edited\s+by|reviewed\s+by|\*?correspondence|"
    r"specialty\s+section|received[\s:]+\d|accepted[\s:]+\d|published[\s:]+\d|"
    r"citation\s|copyright\s*©|type\s+\w+|keywords?\b|"
    r"the\s+orcid\s+identification\s+number|"
    r"article\s+history|supporting\s+information\s+is\s+available|"
    r"doi[:\s]\s*10\.|https?://|www\.\S+\s*$|"
    r"[\w.+-]+@[\w.-]+\.\w+\s*$|"                      # bare e-mail line
    r"\d[A-Z][a-z]+\s|"                                # "1Molecular Dynamics"
    r"[A-Z]\.\s*[A-Z][\wÀ-ſ-]+,\s*[A-Z]\.|"  # "E. Vreeker, F. Gr..."
    r"(?:research\s+article|review(?:\s+article)?|perspective|"
    r"original\s+(?:research|article)|brief\s+report|mini[\s-]?review|"
    r"letter|communication|editorial|full\s+paper)\s*$)",
    re.IGNORECASE,
)

# Front-matter labels that stand alone; the block after them is their value.
FRONT_MATTER_LABEL_RE = re.compile(
    r"^\s*(?:keywords?|open\s+access|\*?correspondence|graphical\s+abstract|"
    r"citation|copyright|specialty\s+section|edited\s+by|reviewed\s+by)"
    r"\s*:?\s*$",
    re.IGNORECASE,
)


@dataclass
class CleanupConfig:
    boilerplate: list[str] | None = None
    dehyphenate: bool = True
    merge_paragraphs: bool = True
    drop_running_heads: bool = True

    def patterns(self) -> list[re.Pattern[str]]:
        raw = self.boilerplate if self.boilerplate is not None else DEFAULT_BOILERPLATE
        return [re.compile(p, re.IGNORECASE) for p in raw]


# --------------------------------------------------------------------------
# character level
# --------------------------------------------------------------------------


def normalize_text(text: str) -> str:
    """NFC-normalise, expand ligatures and collapse runs of whitespace."""
    text = _LIGATURE_RE.sub(lambda m: _LIGATURES[m.group()], text)
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


# --------------------------------------------------------------------------
# line joining and hyphenation
# --------------------------------------------------------------------------


def _keep_hyphen(left: str, right: str) -> bool:
    """Decide whether a line-final hyphen is part of the word."""
    if not left or not right:
        return True
    if right[0].isupper() or right[0].isdigit():
        return True            # "Na-K", "T-cell", "UV-Vis"
    if len(left) <= 2 and not left.isalpha():
        return True
    if left.lower() in _KEEP_HYPHEN_PREFIXES:
        return True
    if "-" in left:
        return True            # already a compound: "state-of-the-"
    if not left[-1].isalpha():
        return True
    return False


def join_lines(lines: list[str], *, dehyphenate: bool = True) -> str:
    """Join physical lines into one paragraph, undoing hyphenation."""
    out: list[str] = []
    for raw in lines:
        line = raw.rstrip()
        if not line:
            continue
        if not out:
            out.append(line)
            continue
        previous = out[-1]
        if dehyphenate and previous.endswith("-"):
            left = previous[:-1].rsplit(" ", 1)[-1]
            right = line.split(" ", 1)[0]
            if _keep_hyphen(left, right):
                out[-1] = previous + line.lstrip()
            else:
                out[-1] = previous[:-1] + line.lstrip()
        else:
            out[-1] = previous + " " + line.lstrip()
    return normalize_text(out[0] if out else "")


# --------------------------------------------------------------------------
# boilerplate and running heads
# --------------------------------------------------------------------------


def strip_boilerplate(text: str, patterns: list[re.Pattern[str]]) -> str:
    """Drop whole lines that match one of the boilerplate patterns."""
    kept = [
        line
        for line in text.splitlines()
        if not any(p.match(line) for p in patterns)
    ]
    return "\n".join(kept)


def is_line_number(text: str) -> bool:
    return bool(_LINE_NUMBER_RE.match(text))


#: Decorative bullets publishers put in front of section headings, most
#: visibly ACS ("■REFERENCES", with no space after the square).
_HEADING_BULLETS = "■□▪▫●○•‣◆◇❖–—-∙*#"


def heading_key(text: str) -> str:
    """Normalise a heading for comparison against known section names.

    Strips decorative bullets, numbering and trailing punctuation, so
    "■REFERENCES", "4. References" and "References:" all come out as
    "references".
    """
    stripped = text.strip().lstrip(_HEADING_BULLETS).strip()
    stripped = re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", stripped)
    return stripped.lower().strip(" .:–—-")


def head_signature(text: str) -> str:
    """Normalise a header candidate so page numbers do not make it unique."""
    signature = re.sub(r"\d+", "#", text.lower())
    signature = re.sub(r"\s+", " ", signature).strip()
    return signature


def detect_running_heads(
    page_texts: dict[int, list[str]], *, min_pages: int = 3, ratio: float = 0.5
) -> set[str]:
    """Return the signatures of lines that repeat as running heads/feet.

    *page_texts* maps a page number to the candidate lines of that page
    (typically the top-most and bottom-most blocks).
    """
    pages = len(page_texts)
    if pages < min_pages:
        return set()
    counts: Counter[str] = Counter()
    for lines in page_texts.values():
        for signature in {head_signature(line) for line in lines if line.strip()}:
            counts[signature] += 1
    threshold = max(2, int(pages * ratio))
    return {sig for sig, n in counts.items() if n >= threshold and len(sig) > 3}


# --------------------------------------------------------------------------
# paragraph stitching
# --------------------------------------------------------------------------


def _continues_previous(previous: Block, current: Block) -> bool:
    if previous.type != "paragraph" or current.type != "paragraph":
        return False
    if not previous.text or not current.text:
        return False
    if _SENTENCE_END_RE.search(previous.text):
        return False
    first = current.text.lstrip()[:1]
    if not first:
        return False
    if first.isupper() and not previous.text.rstrip().endswith(("-", ",", ";", ":")):
        # A new sentence starting with a capital is usually a real new block,
        # unless the previous one was clearly cut mid-clause.
        return False
    return True


def merge_paragraphs(blocks: list[Block], *, dehyphenate: bool = True) -> list[Block]:
    """Re-join paragraphs that the layout broke across columns or pages."""
    merged: list[Block] = []
    for block in blocks:
        if merged and _continues_previous(merged[-1], block):
            previous = merged[-1]
            joined = join_lines(
                [previous.text, block.text], dehyphenate=dehyphenate
            )
            previous.text = joined
            continue
        merged.append(block)
    return merged


# --------------------------------------------------------------------------
# document level entry point
# --------------------------------------------------------------------------


def clean_document(doc: Document, config: CleanupConfig | None = None) -> Document:
    """Run every clean-up stage over an already parsed document."""
    config = config or CleanupConfig()
    patterns = config.patterns()

    cleaned: list[Block] = []
    for block in doc.blocks:
        text = normalize_text(strip_boilerplate(block.text, patterns))
        if not text:
            continue
        if block.type == "paragraph" and is_line_number(text):
            continue
        block.text = text
        cleaned.append(block)

    if config.merge_paragraphs:
        cleaned = merge_paragraphs(cleaned, dehyphenate=config.dehyphenate)

    doc.blocks = cleaned
    for asset in doc.assets:
        asset.caption = normalize_text(strip_boilerplate(asset.caption, patterns))
    doc.metadata.abstract = normalize_text(doc.metadata.abstract)
    doc.metadata.title = normalize_text(doc.metadata.title)
    return doc
