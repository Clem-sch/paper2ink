# PDFtoINK – Forschungspaper (PDF) → Kindle-taugliches EPUB

## Ziel

Baue ein Python-CLI-Tool `paper2ink`, das wissenschaftliche Paper im klassischen PDF-Layout (ein- oder zweispaltig, arXiv, ACS, Elsevier, Nature, Springer usw.) in ein umfließendes EPUB3 umwandelt, das auf einem **Kindle** gut lesbar ist. Der Weg zum Kindle ist „Send to Kindle“ (Amazon akzeptiert EPUB und wandelt selbst um). MOBI wird **nicht** erzeugt, da Amazon es nicht mehr annimmt.

Kernfeature: Jeder Verweis im Fließtext auf eine Abbildung, Tabelle oder Gleichung (z. B. „Fig. 3b“, „Table 2“) wird zu einem Link auf die Grafik. Unter jeder Grafik gibt es Rücksprung-Links zur jeweiligen Textstelle.

## Rahmenbedingungen

- Läuft lokal auf einem **Windows-Laptop**, nur CPU, keine GPU und kein Docker voraussetzen.
- Python ≥ 3.11, Projekt mit `pyproject.toml`. Installation per `pip install -e .` oder `uv`.
- Zuerst nur CLI. Den Code aber so trennen (Kernbibliothek vs. CLI-Schicht), dass später eine GUI angebunden werden kann.
- Code, Bezeichner und Kommentare auf Englisch. CLI-Ausgaben auf Englisch.
- Pfade immer mit `pathlib`, keine Annahmen über `/`. Dateinamen mit Leerzeichen und Umlauten müssen funktionieren.

## Pipeline

```
PDF
 → 1. Parsing (Layout, Lesereihenfolge, Blocktypen)
 → 2. Zwischenformat (Document-Modell, als JSON serialisierbar)
 → 3. Aufräumen
 → 4. Abbildungen/Tabellen/Formeln ausschneiden und rendern
 → 5. Verweise erkennen und verlinken
 → 6. EPUB bauen
 → 7. Validierung
```

### 1. Parsing

- Primärer Parser: **Docling** (läuft auf CPU, erkennt Lesereihenfolge bei zwei Spalten, Überschriften, Bildunterschriften, Tabellen, Formeln, Fußnoten, Seiten-Kopf/-Fußzeilen).
- **PyMuPDF** (`fitz`) zusätzlich für Seiten-Rendering, Bounding-Box-Crops und Metadaten.
- Parser hinter einem Interface (`BaseParser` → `DoclingParser`) kapseln, damit später Marker, MinerU oder GROBID als Alternative eingesteckt werden können. Option `--parser`.
- Docling-Modelle werden beim ersten Lauf heruntergeladen. Das sauber loggen und im README erwähnen.

### 2. Zwischenformat

Eigenes, schlankes Datenmodell (dataclasses oder pydantic), unabhängig vom Parser:

- `Document`: metadata (title, authors, doi, journal, year, abstract), blocks, figures, tables, equations, references, footnotes
- `Block`: type (heading/paragraph/list/equation/caption/footnote …), level, text mit Inline-Spans, Seite, bbox
- `Figure` / `Table`: label („Figure 3“, „Table S1“), Nummer normalisiert (`fig-3`, `tab-s1`), caption, Seite(n), bbox, Bildpfad
- `Reference`: Nummer bzw. Schlüssel, Rohtext, optional DOI

Option `--dump-json`, um das Zwischenformat zum Debuggen auszugeben.

### 3. Aufräumen

- Kopf- und Fußzeilen, Seitenzahlen, laufende Titel und Zeilennummern (Preprints) entfernen.
- Silbentrennung am Zeilenende zurückbauen („transi- tion“ → „transition“). Echte Bindestrich-Komposita („state-of-the-art“) erhalten.
- Absätze, die über Spalten- oder Seitengrenzen gebrochen sind, wieder zusammenführen.
- Verlags-Boilerplate (Download-Hinweise, Copyright-Zeilen, „Downloaded from …“) per konfigurierbarer Regex-Liste entfernen.
- Ligaturen und Sonderzeichen normalisieren (ﬁ → fi, Unicode NFC).
- Fußnoten als EPUB-Fußnoten erhalten (`epub:type="noteref"` / `"footnote"`).

### 4. Abbildungen, Tabellen, Formeln

- **Abbildungen nicht als eingebettete Rasterbilder extrahieren**, sondern den Bereich der Seite anhand der Bounding-Box mit PyMuPDF rendern (Standard 250 dpi, `--dpi`). Grund: Viele Abbildungen sind Vektorgrafiken oder aus mehreren Teilbildern zusammengesetzt.
- Bildunterschrift nicht ins Bild croppen, sondern als Text darunter setzen.
- Etwas Rand (Padding) um die bbox, Weißraum anschließend automatisch trimmen.
- Ausgabe als PNG, für Kindle in **Graustufen** konvertieren (Option `--color` zum Abschalten), Breite auf max. ~1600 px begrenzen, sinnvoll komprimieren.
- **Tabellen**: Standard als gerendertes Bild (robust und auf E-Ink lesbar). Option `--tables html` für umfließende HTML-Tabellen.
- **Formeln**: Kindle stellt MathML unzuverlässig dar. Deshalb abgesetzte Formeln **immer** als Bild aus dem PDF croppen. Das ist schnell und originalgetreu. Inline-Formeln als Text belassen, wenn der Parser sie sinnvoll liefert, sonst ebenfalls als kleines Inline-Bild.
- **Formelerkennung (LaTeX) ist optional**, Option `--formulas {off,latex}`, Standard `off`. Mit `latex` wird das erkannte LaTeX als `alt`-Text an das Formelbild gehängt (Durchsuchbarkeit, Barrierefreiheit) und für Inline-Formeln genutzt. Die Darstellung bleibt das Bild.
- Formelerkennung hinter einem eigenen Interface (`BaseFormulaRecognizer`) kapseln, Option `--formula-backend`:
  - `docling` (Standard): Docling mit `do_formula_enrichment=True` (CodeFormula-Modell), da ohnehin im Stack.
  - `pix2text`: leichtgewichtiges, CPU-taugliches Formel-OCR als Alternative. Optionale Abhängigkeit (`pip install paper2ink[pix2text]`).
  - Weitere Backends (Marker, MinerU, Mathpix-API) später möglich, nicht jetzt umsetzen.
- Ist die Formelerkennung aktiv, die Laufzeit in der CLI ehrlich anzeigen (auf CPU deutlich langsamer).
- Abbildungen, die sich über zwei Seiten erstrecken, und Teilabbildungen (a, b, c) unter einem gemeinsamen Label zusammenfassen.

### 5. Verweise verlinken (Kernfeature)

**Platzierung:** Alle Abbildungen und Tabellen stehen **gesammelt am Ende des Textes** in einem eigenen Kapitel „Figures & Tables“ (nach dem Haupttext, vor dem Literaturverzeichnis), sortiert nach Reihenfolge des Auftretens: erst Abbildungen, dann Tabellen, dann Supplementary (S1, S2 …). Abgesetzte Gleichungen bleiben an ihrer Stelle im Text.

**Erkennung** (eigenes Modul `refs.py`, gut getestet). Mindestens abdecken:

- `Fig. 3`, `Figure 3`, `Fig 3`, `FIG. 3`, `Figs. 3 and 4`, `Figs. 2–4`, `Figures 2, 3`, `Fig. 3a`, `Fig. 3(a)`, `Fig. 3a,b`, `Fig. S2`, `Supplementary Fig. 2`, `Extended Data Fig. 1`
- `Table 1`, `Tab. 2`, `Tables 1 and 2`, `Table S1`
- `Eq. 5`, `Eq. (5)`, `Eqs. (2)–(4)`, `Equation 5`, `(5)` nur wenn eindeutig als Gleichungsverweis erkennbar
- `Section 2.1`, `Sec. 3`, `Appendix A`
- Literaturverweise: numerisch `[12]`, `[3, 5–7]`, hochgestellte Ziffern sowie Autor-Jahr `(Smith et al., 2020)` best-effort
- Bereiche („2–4“) werden in Einzellinks aufgelöst bzw. der Bereich verlinkt auf das erste Element.
- Verweise, deren Ziel nicht existiert, bleiben unverlinkt und werden als Warnung geloggt.
- Bildunterschriften selbst („Figure 3. …“) dürfen sich nicht selbst verlinken.

**Anker und Rücksprung:**

- Jede Grafik: `id="fig-3"`, `id="tab-1"`, `id="eq-5"`.
- Jede einzelne Erwähnung im Text bekommt eine eindeutige ID: `id="ref-fig-3-1"`, `ref-fig-3-2` …
- Unter jeder Grafik eine Rücksprungzeile: bei einer Erwähnung `↩ back to text`, bei mehreren `↩ 1 2 3` (jede Zahl springt zur jeweiligen Stelle, wie Wikipedia-Fußnoten). Grafiken ohne Erwähnung bekommen einen Link zum Anfang des Abschnitts, in dem sie im Original standen.
- Dasselbe Prinzip für Literatur: Zitat → Eintrag im Literaturverzeichnis, dort Rücksprung zur Zitatstelle. Einträge mit DOI erhalten zusätzlich einen `https://doi.org/…`-Link.
- Grund für die expliziten Rücksprünge: Der Zurück-Button auf Kindle ist unzuverlässig.
- Links visuell dezent halten (kein knalliges Blau, Kindle zeigt Links ohnehin unterstrichen).

### 6. EPUB bauen

- EPUB3 mit **ebooklib** (oder eigenes Zippen mit sauberem XHTML, falls ebooklib Probleme macht).
- Kapitelstruktur aus den Überschriften, Inhaltsverzeichnis (`nav.xhtml` + NCX für Kompatibilität).
- Metadaten: Titel, Autoren, Sprache, DOI als Identifier, Datum. Fallback: PDF-Metadaten, dann Dateiname.
- **Automatisch generiertes Cover** (Pillow): Titel, Autoren, Journal/Jahr, schlicht schwarz-weiß, 1600×2560.
- Abstract direkt nach dem Titel, optisch abgesetzt.
- Schlankes CSS für E-Ink: Serifenschrift des Readers verwenden (keine Fonts einbetten), keine festen Schriftgrößen in px, Abbildungen `max-width: 100%`, Bildunterschriften etwas kleiner, kein Seitenhintergrund, kein JavaScript.
- Jede Abbildung im Anhang auf eigener „Seite“ (`page-break-before`), damit sie nicht zerschnitten wird.
- Ausgabe-Dateiname: `<Erstautor>_<Jahr>_<Kurztitel>.epub`, Option `-o`.

### 7. Validierung

- Nach dem Bauen **EPUBCheck** ausführen, falls installiert (Java). Send to Kindle lehnt fehlerhafte EPUBs ohne klare Fehlermeldung ab, daher ist das wichtig. Falls nicht installiert: Warnung mit Installationshinweis.
- Eigene Checks: alle internen Links haben ein Ziel, alle Bilder existieren, keine leeren Kapitel.
- Am Ende eine kurze Zusammenfassung ausgeben: Anzahl Abschnitte, Abbildungen, Tabellen, Gleichungen, verlinkte Verweise, nicht auflösbare Verweise.

## CLI

```
paper2ink paper.pdf                      # → EPUB neben dem PDF
paper2ink paper.pdf -o out.epub
paper2ink ./papers/ --batch              # alle PDFs im Ordner
paper2ink paper.pdf --azw3               # zusätzlich AZW3 via Calibre ebook-convert, falls installiert
paper2ink paper.pdf --dpi 300 --color --tables html
paper2ink paper.pdf --dump-json --keep-temp --verbose
```

- Mit **Typer** (oder argparse) und **rich** für Fortschrittsanzeige.
- Batch-Modus: Fehler bei einem PDF bricht den Rest nicht ab, am Ende Übersicht.
- Konfigurationsdatei optional (`paper2ink.toml`) für Standardwerte und Boilerplate-Regexe.

## Projektstruktur (Vorschlag)

```
paper2ink/
  pyproject.toml
  README.md
  src/paper2ink/
    cli.py
    pipeline.py
    model.py          # Zwischenformat
    parsers/
      base.py
      docling_parser.py
    cleanup.py
    assets.py         # Crops, Rendering, Graustufen, Cover
    refs.py           # Verweiserkennung und Verlinkung
    epub_builder.py
    validate.py
    templates/        # XHTML-Templates, CSS
  tests/
    test_refs.py      # viele Regex-Fälle
    test_cleanup.py
    test_pipeline.py  # End-to-end mit Beispiel-PDFs
    fixtures/
```

## Testen

- `tests/test_refs.py` mit umfangreicher Tabelle aus Eingabetext → erwartete Links (alle oben genannten Varianten plus Negativbeispiele wie „Figure 3.“ in einer Bildunterschrift oder „Table“ als normales Wort).
- Für End-to-end-Tests einige frei verfügbare Paper verwenden (z. B. arXiv-Preprints in ein- und zweispaltigem Layout, ein Paper mit vielen Formeln, eines mit Supplementary-Abbildungen). Download-Skript statt PDFs ins Repo legen.
- Ein Debug-Modus, der für jede Seite ein PNG mit eingezeichneten Bounding-Boxen und Blocktypen erzeugt (`--debug-layout`). Das hilft enorm beim Tunen.

## Vorgehen

Bitte schrittweise arbeiten und nach jedem Schritt lauffähigen Stand haben:

1. Projektgerüst, Datenmodell, CLI-Skelett, Docling-Parser → `--dump-json` funktioniert.
2. Minimales EPUB nur mit Text und Überschriften, das EPUBCheck besteht und per Send to Kindle ankommt.
3. Abbildungen und Tabellen croppen, Anhang „Figures & Tables“.
4. Verweiserkennung und Links mit Rücksprung (inkl. Tests).
5. Aufräumen (Kopf-/Fußzeilen, Silbentrennung, Boilerplate).
6. Formeln, Literaturverzeichnis-Links, Fußnoten.
7. Cover, Metadaten, Batch-Modus, AZW3-Option, README.

Vor größeren Architekturentscheidungen kurz Rückfrage halten. Wenn Docling bei einem Layout schlechte Ergebnisse liefert, das dokumentieren statt mit immer mehr Sonderfall-Heuristiken zu flicken.

## Später (nicht jetzt umsetzen, aber nicht verbauen)

- GUI (Drag-and-Drop, z. B. mit einem schlanken Web-UI oder PySide)
- Direktversand per Send-to-Kindle-Mail
- Alternative Parser (GROBID, Marker) für schwierige Layouts
- Option, Abbildungen zusätzlich nach dem ersten Erwähnungs-Absatz einzufügen
