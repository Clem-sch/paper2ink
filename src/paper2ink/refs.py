"""Detection and linking of cross references in running text.

This is the core feature of paper2ink: every mention of a figure, table,
equation, section or bibliography entry in the body text becomes a hyperlink
to the corresponding target, and every target gets backlinks to each of the
places that mention it.

The module is deliberately free of EPUB specifics.  :class:`Linker` turns
plain text into XHTML fragments and records every mention it emitted; the
EPUB builder decides what the hrefs actually look like.

Detection works in two stages instead of one monster regex:

1. a keyword regex finds ``Fig.``/``Table``/``Eq.``/``Section``/... ;
2. a small scanner walks forward from the keyword and consumes a list or
   range of number tokens (``3``, ``S2``, ``2.1``, ``3a``, ``(5)``).

That keeps the individual pieces readable and testable.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

__all__ = [
    "RefMatch",
    "LinkSpan",
    "Mention",
    "Linker",
    "find_refs",
    "normalize_key",
    "parse_caption_label",
]

# --------------------------------------------------------------------------
# key normalisation
# --------------------------------------------------------------------------

KIND_PREFIX = {
    "figure": "fig",
    "scheme": "sch",
    "table": "tab",
    "equation": "eq",
    "section": "sec",
    "citation": "bib",
}


def normalize_key(kind: str, number: str, supplementary: bool = False) -> str:
    """Build the anchor id for a reference target.

    >>> normalize_key("figure", "3")
    'fig-3'
    >>> normalize_key("figure", "2", supplementary=True)
    'fig-s2'
    >>> normalize_key("table", "S1")
    'tab-s1'
    """
    prefix = KIND_PREFIX[kind]
    num = number.strip().lower().replace(" ", "")
    num = num.rstrip(".")
    if supplementary and not num.startswith("s"):
        num = "s" + num
    return f"{prefix}-{num}"


# --------------------------------------------------------------------------
# keyword patterns
# --------------------------------------------------------------------------

# Prefixes that mark a supplementary/extended-data target.
_SUPP_PREFIX = (
    r"(?P<supp>"
    r"supp(?:l(?:ementary|\.)?|\.)?|supporting|extended\s+data|online\s+supp\w*"
    r")\s+"
)

_KEYWORDS: list[tuple[str, str]] = [
    # (kind, pattern for the keyword itself, without the numbers)
    ("figure", rf"(?:{_SUPP_PREFIX})?(?:figures?|figs?)\b\.?"),
    ("table", rf"(?:{_SUPP_PREFIX})?(?:tables?|tabs?)\b\.?"),
    ("scheme", rf"(?:{_SUPP_PREFIX})?(?:schemes?)\b\.?"),
    ("equation", r"(?:equations?|eqns?|eqs?)\b\.?"),
    ("section", r"(?:sections?|secs?)\b\.?"),
    ("section", r"(?:appendix|appendices)\b"),
]

# One compiled regex per kind keeps the group names unique.
_KEYWORD_RES: list[tuple[str, re.Pattern[str]]] = [
    (kind, re.compile(rf"\b{pat}", re.IGNORECASE)) for kind, pat in _KEYWORDS
]

# A single number token: optional opening paren, optional S/supplementary
# marker, digits (possibly dotted like 2.1 or roman-ish appendix letters),
# optional panel letters ("3a", "3(a)", "3a,b").
_NUMBER_RE = re.compile(
    r"""
    \(?\s*
    (?P<s>[Ss])?
    (?P<num>\d+(?:\.\d+)*)                      # 2 or 2.1
    (?P<panels>                                 # panel letters stick to the
        (?:\(?[a-h]\)?(?![A-Za-z]))             # number: 3a or 3(a), never
        (?:\s*[,–—/-]\s*\(?[a-h]\)?(?![A-Za-z]))*   # the "a" of "3 and 4"
    )?
    (?:\s*\))?                                  # never eat trailing space
    """,
    re.VERBOSE,
)

# Sections and appendices may be labelled with a letter ("Appendix A").
_SECTION_NUMBER_RE = re.compile(
    _NUMBER_RE.pattern.replace(
        r"(?P<num>\d+(?:\.\d+)*)", r"(?P<num>\d+(?:\.\d+)*|[A-Z](?![A-Za-z]))"
    ),
    re.VERBOSE,
)


def _number_re(kind: str) -> re.Pattern[str]:
    return _SECTION_NUMBER_RE if kind == "section" else _NUMBER_RE


# Separators that continue a list or range of numbers.
_SEP_RE = re.compile(
    r"\s*(?P<sep>,|;|&|–|—|−|--|-|\b(?:and|to|through|or)\b)\s*",
    re.IGNORECASE,
)

_RANGE_SEPS = {"–", "—", "−", "--", "-", "to", "through"}

# Bare "(5)" equation references are only accepted after one of these words.
_BARE_EQ_CONTEXT = re.compile(
    r"\b(?:in|from|into|to|by|see|via|using|use|with|of|per|follows?|"
    r"given\s+by|according\s+to|described\s+by|defined\s+(?:in|by)|"
    r"combining|inserting|substituting|solving)\s*$",
    re.IGNORECASE,
)

# Numeric citations: [12], [3, 5-7], [1]-[3]
# − is the true minus sign, which ACS uses for citation ranges.
_CITATION_RE = re.compile(
    r"\[(?P<body>\d+(?:\s*[,;−–—-]\s*\d+)*)\]"
)

# Author-year citations, best effort: (Smith et al., 2020), (Smith & Jones, 1999)
_AUTHOR_YEAR_RE = re.compile(
    r"(?P<author>[A-Z][\wÀ-ſ'’-]+)"
    r"(?P<rest>\s+(?:et\s+al\.?|and\s+[A-Z][\wÀ-ſ'’-]+|"
    r"&\s*[A-Z][\wÀ-ſ'’-]+))?"
    r"[,\s]+\(?(?P<year>(?:19|20)\d{2}[a-z]?)\)?"
)

_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


# --------------------------------------------------------------------------
# data types
# --------------------------------------------------------------------------


@dataclass
class LinkSpan:
    """A slice of the input text that becomes one ``<a>`` element."""

    start: int
    end: int
    key: str
    kind: str


@dataclass
class RefMatch:
    """A whole reference expression, e.g. ``Figs. 2-4``."""

    start: int
    end: int
    kind: str
    text: str
    spans: list[LinkSpan] = field(default_factory=list)

    @property
    def keys(self) -> list[str]:
        return [span.key for span in self.spans]


@dataclass
class Mention:
    """One emitted link, so the target can link back to it."""

    id: str
    key: str
    block_id: str
    file: str = ""
    index: int = 0
    label: str = ""


# --------------------------------------------------------------------------
# scanning
# --------------------------------------------------------------------------


def _scan_numbers(
    text: str, pos: int, kind: str, keyword_supplementary: bool
) -> list[tuple[int, int, str, bool]]:
    """Consume a list/range of number tokens starting at *pos*.

    Returns ``(start, end, key, is_range_continuation)`` per token.
    """
    tokens: list[tuple[int, int, str, bool]] = []
    number_re = _number_re(kind)
    cursor = pos
    is_range = False
    while True:
        # Allow whitespace (and a stray period after "Fig") before the number.
        probe = number_re.match(text, cursor) or number_re.match(
            text, _skip_space(text, cursor)
        )
        if not probe:
            break
        supp = keyword_supplementary or bool(probe.group("s"))
        number = probe.group("num")
        if kind == "section" and number.isalpha():
            # "Appendix A"
            key = normalize_key("section", number.lower(), False)
        else:
            key = normalize_key(kind, number, supp)
        tokens.append((probe.start(), probe.end(), key, is_range))

        sep = _SEP_RE.match(text, probe.end())
        if not sep:
            break
        sep_token = sep.group("sep").lower()
        # A separator only continues the list if a number really follows.
        if not number_re.match(text, sep.end()):
            break
        if sep_token == "," and not _looks_like_number_list(text, sep.end()):
            break
        is_range = sep_token in _RANGE_SEPS
        cursor = sep.end()
    return tokens


def _skip_space(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t ":
        pos += 1
    return pos


def _looks_like_number_list(text: str, pos: int) -> bool:
    """Guard against swallowing "Fig. 3, the sample was ..." style prose."""
    probe = _NUMBER_RE.match(text, pos)
    if not probe:
        return False
    tail = text[probe.end() : probe.end() + 12]
    # "Fig. 3, 4 nm" -> the 4 belongs to a unit, not to the figure list.
    return not re.match(r"\s*(?:nm|mm|cm|m|s|ms|h|K|%|°|mV|V|mA|A|Hz)\b", tail)


def _iter_keyword_matches(text: str) -> Iterator[tuple[str, re.Match[str]]]:
    seen: set[int] = set()
    for kind, pattern in _KEYWORD_RES:
        for match in pattern.finditer(text):
            if match.start() in seen:
                continue
            seen.add(match.start())
            yield kind, match


def find_refs(
    text: str,
    *,
    known_keys: Iterable[str] | None = None,
    allow_bare_equations: bool = False,
    citations: bool = True,
    author_year_index: dict[tuple[str, str], str] | None = None,
) -> list[RefMatch]:
    """Find every cross reference in *text*.

    ``known_keys`` restricts the result to targets that actually exist; spans
    pointing nowhere are dropped so the caller can log them as unresolved.
    """
    keys = set(known_keys) if known_keys is not None else None
    matches: list[RefMatch] = []

    for kind, kw in _iter_keyword_matches(text):
        supp = bool(kw.groupdict().get("supp"))
        tokens = _scan_numbers(text, kw.end(), kind, supp)
        if not tokens:
            continue
        spans: list[LinkSpan] = []
        for i, (tstart, tend, key, _is_range) in enumerate(tokens):
            span_start = kw.start() if i == 0 else tstart
            spans.append(LinkSpan(span_start, tend, key, kind))
        end = tokens[-1][1]
        matches.append(
            RefMatch(kw.start(), end, kind, text[kw.start() : end], spans)
        )

    if allow_bare_equations and keys:
        matches.extend(_find_bare_equations(text, keys))

    if citations:
        matches.extend(_find_numeric_citations(text))
        if author_year_index:
            matches.extend(_find_author_year(text, author_year_index))

    # Drop spans without a target, then matches without spans.
    if keys is not None:
        for match in matches:
            match.spans = [s for s in match.spans if s.key in keys]
        matches = [m for m in matches if m.spans]

    matches = _drop_overlaps(matches)
    matches.sort(key=lambda m: m.start)
    return matches


def _find_bare_equations(text: str, keys: set[str]) -> list[RefMatch]:
    out: list[RefMatch] = []
    for probe in re.finditer(r"\((\d{1,3})\)", text):
        key = normalize_key("equation", probe.group(1))
        if key not in keys:
            continue
        if not _BARE_EQ_CONTEXT.search(text[: probe.start()]):
            continue
        out.append(
            RefMatch(
                probe.start(),
                probe.end(),
                "equation",
                probe.group(0),
                [LinkSpan(probe.start(), probe.end(), key, "equation")],
            )
        )
    return out


def _find_numeric_citations(text: str) -> list[RefMatch]:
    out: list[RefMatch] = []
    for group in _CITATION_RE.finditer(text):
        body = group.group("body")
        offset = group.start("body")
        spans: list[LinkSpan] = []
        for number in re.finditer(r"\d+", body):
            spans.append(
                LinkSpan(
                    offset + number.start(),
                    offset + number.end(),
                    normalize_key("citation", number.group()),
                    "citation",
                )
            )
        if spans:
            out.append(
                RefMatch(
                    group.start(), group.end(), "citation", group.group(), spans
                )
            )
    return out


def _find_author_year(
    text: str, index: dict[tuple[str, str], str]
) -> list[RefMatch]:
    out: list[RefMatch] = []
    for probe in _AUTHOR_YEAR_RE.finditer(text):
        author = probe.group("author").lower()
        year = probe.group("year")[:4]
        key = index.get((author, year))
        if not key:
            continue
        out.append(
            RefMatch(
                probe.start(),
                probe.end(),
                "citation",
                probe.group(),
                [LinkSpan(probe.start(), probe.end(), key, "citation")],
            )
        )
    return out


def _drop_overlaps(matches: list[RefMatch]) -> list[RefMatch]:
    """Keep the longest match when two detectors claim the same text."""
    ordered = sorted(matches, key=lambda m: (m.start, -(m.end - m.start)))
    kept: list[RefMatch] = []
    last_end = -1
    for match in ordered:
        if match.start < last_end:
            continue
        kept.append(match)
        last_end = match.end
    return kept


# --------------------------------------------------------------------------
# caption labels
# --------------------------------------------------------------------------

_CAPTION_RE = re.compile(
    r"""^\s*
    (?P<supp>supp(?:lementary|orting|\.)?\s+|extended\s+data\s+)?
    (?P<kw>figures?|figs?|tables?|tabs?|schemes?|charts?|equations?|eqs?)\.?
    \s*
    (?P<s>S)?(?P<num>\d+(?:\.\d+)*)
    (?P<panel>[a-h])?
    \s*[.:–—)|\-]?\s*
    (?P<rest>.*)$
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)

_CAPTION_KIND = {
    "figure": "figure",
    "fig": "figure",
    "chart": "figure",
    "scheme": "scheme",
    "table": "table",
    "tab": "table",
    "equation": "equation",
    "eq": "equation",
}


def parse_caption_label(text: str) -> tuple[str, str, bool, str, str] | None:
    """Split a caption into ``(kind, number, supplementary, label, rest)``.

    Returns ``None`` if *text* does not start with a caption label.
    """
    match = _CAPTION_RE.match(text)
    if not match:
        return None
    kw = match.group("kw").rstrip(".").lower()
    kind = _CAPTION_KIND.get(kw)
    if kind is None:
        return None
    supplementary = bool(match.group("supp")) or bool(match.group("s"))
    number = match.group("num")
    if match.group("s"):
        number = "S" + number
    elif supplementary:
        number = "S" + number
    pretty = {
        "figure": "Figure",
        "scheme": "Scheme",
        "table": "Table",
        "equation": "Equation",
    }[kind]
    label = f"{pretty} {number}"
    return kind, number, supplementary, label, match.group("rest").strip()


def author_year_index(references) -> dict[tuple[str, str], str]:
    """Build the ``(surname, year) -> anchor`` map for author-year citations."""
    index: dict[tuple[str, str], str] = {}
    for reference in references:
        surname = re.match(r"\s*([A-Z][\wÀ-ſ'’-]+)", reference.raw or "")
        if surname and reference.year:
            index.setdefault((surname.group(1).lower(), reference.year), reference.key)
    return index


def extract_doi(text: str) -> str | None:
    match = _DOI_RE.search(text)
    if not match:
        return None
    return match.group().rstrip(".,;)")


# --------------------------------------------------------------------------
# linking
# --------------------------------------------------------------------------

HrefResolver = Callable[[str], str | None]


class Linker:
    """Turn plain text into XHTML and record every link it emits."""

    def __init__(
        self,
        known_keys: Iterable[str],
        href_for: HrefResolver,
        *,
        allow_bare_equations: bool = True,
        author_year_index: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self.known_keys = set(known_keys)
        self.href_for = href_for
        self.allow_bare_equations = allow_bare_equations
        self.author_year_index = author_year_index or {}
        self.mentions: dict[str, list[Mention]] = {}
        self.unresolved: dict[str, int] = {}
        self.current_file: str = ""
        self._counter: dict[str, int] = {}

    # -- public API --------------------------------------------------------
    def linkify(
        self,
        text: str,
        block_id: str,
        *,
        self_key: str | None = None,
        citations: bool = True,
    ) -> str:
        """Return XHTML for *text* with all resolvable references linked."""
        matches = find_refs(
            text,
            known_keys=self.known_keys,
            allow_bare_equations=self.allow_bare_equations,
            citations=citations,
            author_year_index=self.author_year_index,
        )
        self._record_unresolved(text, citations=citations)

        out: list[str] = []
        cursor = 0
        for match in matches:
            for span in match.spans:
                if self_key and span.key == self_key:
                    continue  # a caption must not link to itself
                href = self.href_for(span.key)
                if href is None:
                    continue
                if span.start < cursor:
                    continue
                out.append(html.escape(text[cursor : span.start]))
                mention = self._new_mention(span.key, block_id, text[span.start : span.end])
                out.append(
                    f'<a class="xref" id="{mention.id}" href="{href}">'
                    f"{html.escape(text[span.start : span.end])}</a>"
                )
                cursor = span.end
        out.append(html.escape(text[cursor:]))
        return "".join(out)

    def mentions_for(self, key: str) -> list[Mention]:
        return self.mentions.get(key, [])

    @property
    def link_count(self) -> int:
        return sum(len(v) for v in self.mentions.values())

    @property
    def unresolved_count(self) -> int:
        return sum(self.unresolved.values())

    # -- internals ---------------------------------------------------------
    def _new_mention(self, key: str, block_id: str, label: str) -> Mention:
        index = self._counter.get(key, 0) + 1
        self._counter[key] = index
        mention = Mention(
            id=f"ref-{key}-{index}",
            key=key,
            block_id=block_id,
            file=self.current_file,
            index=index,
            label=label,
        )
        self.mentions.setdefault(key, []).append(mention)
        return mention

    def _record_unresolved(self, text: str, *, citations: bool) -> None:
        """Count references whose target does not exist, for the summary."""
        everything = find_refs(
            text,
            known_keys=None,
            allow_bare_equations=False,
            citations=citations,
            author_year_index=self.author_year_index,
        )
        for match in everything:
            for span in match.spans:
                if span.key not in self.known_keys:
                    self.unresolved[span.key] = self.unresolved.get(span.key, 0) + 1
