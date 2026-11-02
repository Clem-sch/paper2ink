# paper2ink

Turn scientific papers in PDF form into reflowable **EPUB3** files that read
well on a Kindle.

The point of the exercise is not just reflowing text. Every mention of a
figure, table or equation in the running text becomes a **link to the
graphic**, and every graphic carries **backlinks to each place that mentions
it** — numbered like Wikipedia footnotes, because the Kindle's back button is
unreliable.

Send the EPUB to your device with *Send to Kindle*; Amazon converts it on
their side. No MOBI is produced, because Amazon no longer accepts it.

```
PDF → parse (layout, reading order, block types)
    → intermediate document model (JSON-serialisable)
    → clean up (headers, hyphenation, boilerplate)
    → crop figures / tables / equations
    → detect and link cross references
    → build EPUB3
    → validate
```

## Requirements

- Windows, macOS or Linux; CPU only, no GPU, no Docker.
- Python ≥ 3.11.
- Optional: **Java + EPUBCheck** to validate the result, **Calibre** for
  `--azw3`.

## Install

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -e .                   # core: PyMuPDF, ebooklib, Typer, Pillow
pip install -e ".[docling]"        # add the Docling parser (large download)
pip install -e ".[pix2text]"       # add the pix2text formula recognizer
pip install -e ".[dev]"            # pytest
```

The core install is small and fully functional via `--parser pymupdf`.
Docling pulls in PyTorch and friends (one to two gigabytes) and downloads its
layout models from Hugging Face **the first time it runs** — expect a few
minutes and a few hundred megabytes on that first conversion. The download is
logged; run with `-v` to watch it.

## Use

```bash
paper2ink paper.pdf                       # EPUB next to the PDF
paper2ink paper.pdf -o out.epub
paper2ink ./papers/ --batch               # every PDF in the folder
paper2ink ./papers/ --batch --recursive
paper2ink paper.pdf --azw3                # also AZW3, needs Calibre
paper2ink paper.pdf --dpi 300 --color --tables html
paper2ink paper.pdf --dump-json --keep-temp --verbose
paper2ink paper.pdf --debug-layout        # one PNG per page with block boxes
paper2ink paper.pdf --parser pymupdf      # skip the ML stack
```

The output file is named `<FirstAuthor>_<Year>_<ShortTitle>.epub` unless you
pass `-o`. In batch mode a failing PDF does not stop the rest; the closing
table lists every file with its section, figure, table, equation, link and
unresolved-reference counts.

### Options worth knowing

| Option | Default | Notes |
| --- | --- | --- |
| `--parser` | `auto` | `docling`, `pymupdf`, or `auto` (Docling if installed) |
| `--dpi` | `250` | resolution of figure and table crops |
| `--color` / `--grayscale` | grayscale | Kindle screens are grayscale |
| `--tables` | `image` | `html` gives reflowable tables instead of pictures |
| `--formulas` | `off` | `latex` adds recognised LaTeX as `alt` text (slow) |
| `--formula-backend` | `docling` | or `pix2text` |
| `--no-cover` | cover on | the cover is generated from the metadata |
| `--no-validate` | validate | skips EPUBCheck and the internal checks |
| `--config` | auto-discovered | see `paper2ink.example.toml` |

## How it works

### Parsing

Two backends sit behind one interface (`BaseParser`), so Marker, MinerU or
GROBID can be added later without touching anything downstream.

- **`docling`** (default when installed) runs layout, reading-order and
  table-structure models. Best results on awkward layouts. Its output needs
  a fair amount of post-processing, which paper2ink does for you: Docling
  returns captions as items that are *not* linked to their picture (and often
  splits the label "FIGURE 2" from the caption text), reports journal logos
  and badges as pictures, and flattens every heading to level 1. So captions
  and graphics are paired up by page and distance, uncaptioned graphics are
  dropped rather than numbered as figures, and heading depth is re-derived
  from the section numbering.
- **`pymupdf`** is heuristic and needs no models: it finds the column gutter
  by looking for the widest vertical band of whitespace that no text block
  crosses, orders blocks column by column between full-width elements, and
  classifies headings, captions, equations and reference entries from font
  size, position and caption regexes.

Both produce the same intermediate model (`model.py`), which `--dump-json`
writes out for debugging.

### Figures, tables, equations

Figures are **not** pulled out as embedded raster images. Most are vector art
or assembled from several sub-images, so paper2ink re-renders the page region
behind the bounding box with PyMuPDF (250 dpi by default), trims the white
margin, converts to grayscale and caps the width at 1600 px. The caption is
never cropped into the picture — it is set as text underneath.

Tables are rendered as images by default, which is robust and legible on
e-ink; `--tables html` produces reflowable HTML tables instead.

Displayed equations are **always** cropped from the PDF, because Kindle
renders MathML unreliably. `--formulas latex` additionally attaches the
recognised LaTeX as `alt` text for searchability; the picture is still what
gets displayed. Formula recognition is slow on a CPU and therefore off by
default.

### The bibliography

Reference lists survive PDF extraction worse than anything else, so
`bibliography.py` covers three shapes, tried in that order:

1. a `References` heading followed by entries — the precise case, and the
   detection stays on afterwards, because sections printed *after* the
   bibliography (author contributions, funding) must not switch it off;
2. a run of `[1] … [2] …` with no heading at all, as Wiley prints it, broken
   across pages into blocks that start mid-entry — these are stitched back
   together and re-split on the markers, wherever in the block the list
   begins;
3. a list the parser already split into items with the markers stripped,
   which is what Docling hands over — recognised as a run of list items that
   closes the document and mostly carries years, then numbered by order.

Author-year entries are recognised by the parenthesised year rather than by
the author list: surnames carry particles ("de Jong"), non-ASCII initials
("Štefl") and corporate names ("Blender Online Community"), none of which fit
a tidy pattern.

### Cross references

`refs.py` handles, among others:

```
Fig. 3   Figure 3   Fig 3   FIG. 3   Figs. 3 and 4   Figs. 2–4
Figures 2, 3   Fig. 3a   Fig. 3(a)   Fig. 3a,b   Fig. S2
Supplementary Fig. 2   Extended Data Fig. 1
Table 1   Tab. 2   Tables 1 and 2   Table S1   Scheme 1
Eq. 5   Eq. (5)   Eqs. (2)–(4)   Equation 5   (5) in the right context
Section 2.1   Sec. 3   Appendix A
[12]   [3, 5–7]   [1−5]   (Smith et al., 2020)
```

ACS and Nature set citations as superscript digits, which extract as numbers
glued to the preceding word ("method.41,42") and are then indistinguishable
from ordinary figures. The PyMuPDF backend spots the superscript runs and
rewrites them in bracket form, which reads better on an e-ink screen anyway;
on two chemistry papers that took the link count from 26 to 163 and from 22
to 74.

Each target gets a stable anchor (`fig-3`, `tab-s1`, `eq-5`, `sec-2.1`,
`bib-12`) and each mention a unique id (`ref-fig-3-1`, `ref-fig-3-2`, …).
A range such as "Figs. 2–4" is linked at both endpoints. References whose
target does not exist in the PDF — typically supplementary figures that live
in a separate file — are left as plain text and counted in the summary.
Captions never link to themselves.

All figures and tables are collected in a **"Figures & Tables"** chapter after
the body text and before the bibliography, ordered figures → tables →
supplementary. Displayed equations stay where they are in the text. Each one
starts on its own page so it is not cut in half.

### EPUB

EPUB3 via ebooklib, with `nav.xhtml` and an NCX for older readers, a
generated black-and-white cover (1600×2560), the abstract set off after the
title, and deliberately minimal CSS: no embedded fonts, no fixed pixel sizes,
no background colour, no JavaScript. The reader's own typography wins.

### Validation

After the build, paper2ink checks that every internal link has a target,
every referenced image exists and no chapter is empty. If EPUBCheck is
available it runs that too — *Send to Kindle* rejects malformed EPUBs without
a useful message, so this matters.

To enable it, download EPUBCheck from
<https://github.com/w3c/epubcheck/releases> and either put `epubcheck` on
`PATH` or point `EPUBCHECK_JAR` at `epubcheck.jar`.

## Configuration

Settings can live in `paper2ink.toml`, looked up next to the input, in the
working directory and in `%APPDATA%\paper2ink\` (or `$XDG_CONFIG_HOME`). It is
also where you add publisher boilerplate patterns. See
`paper2ink.example.toml`.

## Development

```bash
pip install -e ".[dev]"
pytest                                   # no network needed
python scripts/fetch_test_papers.py      # real papers for manual testing
paper2ink tests/fixtures/papers --batch --debug-layout
```

The test suite builds its own PDF with PyMuPDF, so nothing is downloaded and
no paper has to be committed. The Docling tests cover the adapter logic
(caption pairing, heading depth, bibliography detection) without running
Docling itself, and skip cleanly when the extra is not installed.

`--debug-layout` is the tool to reach for when a layout comes out wrong: it
writes one PNG per page with every block's box and type drawn on top, plus
the asset regions in magenta.

A useful sanity check is to run both backends over the same paper and compare
the summary line — the section, figure, link and reference counts should be
close. A large gap means one of them misread the layout:

```bash
paper2ink paper.pdf --parser pymupdf -o /tmp/a.epub
paper2ink paper.pdf --parser docling -o /tmp/b.epub
```

### Project layout

```
src/paper2ink/
  cli.py            # Typer CLI, progress, summary
  pipeline.py       # convert(); the entry point a GUI would use
  config.py         # paper2ink.toml
  model.py          # intermediate document model
  parsers/          # BaseParser, DoclingParser, PyMuPDFParser
  cleanup.py        # hyphenation, running heads, boilerplate, paragraphs
  bibliography.py   # reference-list extraction shared by the backends
  assets.py         # crops, grayscale, cover, debug overlay
  refs.py           # reference detection and linking
  formulas.py       # BaseFormulaRecognizer and backends
  epub_builder.py   # EPUB3 assembly
  validate.py       # internal checks and EPUBCheck
  templates/        # CSS
```

The library knows nothing about the CLI, so a GUI can call
`paper2ink.pipeline.convert()` directly and pass a progress callback.

## Known limitations

- The `pymupdf` backend is heuristic. Journals with unusual layouts can come
  out with jumbled reading order; use `--parser docling` or
  `--debug-layout` to see what happened. Rather than piling on special cases,
  known failures are documented here.
- Supplementary figures referenced in the text but published in a separate SI
  file cannot be linked. They are reported as unresolved references, and on a
  typical chemistry paper they are most of that count.
- When a caption is found but its figure region is not, the asset is kept
  with the caption alone so the cross references still lead somewhere; the
  summary warns about it.
- "Extended Data Fig. 1" and "Supplementary Fig. 1" currently map to the same
  anchor (`fig-s1`); papers that use both schemes at once may mislink.
- Footnotes are preserved with `epub:type="footnote"` at the end of their
  chapter, but footnote *markers* in the body text are not linked yet.
- Inline formulas are kept as text as the parser delivered them; they are not
  cropped into small inline images. Displayed equations always are.
- Table footnotes printed underneath a table stay in the body text instead of
  travelling with the table into the appendix.
- Author and journal detection is best effort and falls back to the PDF
  metadata and then the file name.

## Later

Not built yet, but nothing in the architecture is in the way: a GUI
(drag-and-drop), sending straight to a Send-to-Kindle address, further parser
backends (GROBID, Marker, MinerU), and optionally repeating each figure after
the paragraph that first mentions it.

## Licence

MIT.
