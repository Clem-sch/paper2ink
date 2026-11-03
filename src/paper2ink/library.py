"""A record of every paper that has been converted.

One SQLite file, one row per conversion: where the PDF came from, where the
EPUB went, what was extracted from it, and whether it has been sent to the
reader yet.  SQLite is in the standard library, survives restarts, and
handles the sorting and filtering the shelf view needs without any of it
having to live in the browser.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("paper2ink.library")

__all__ = ["Entry", "Library", "SORT_FIELDS"]

#: Columns the shelf may be sorted by, mapped to their SQL expression.
#: Anything not in here is rejected, so the sort key can never be injected.
SORT_FIELDS = {
    "created": "created",
    "title": "title COLLATE NOCASE",
    "author": "first_author COLLATE NOCASE",
    "year": "year",
    "journal": "journal COLLATE NOCASE",
    "pages": "pages",
    "figures": "figures",
    "references": "references_count",
    "sent": "sent_at",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created       TEXT    NOT NULL,
    source_name   TEXT    NOT NULL,
    source_path   TEXT,
    epub_path     TEXT,
    title         TEXT    DEFAULT '',
    first_author  TEXT    DEFAULT '',
    authors       TEXT    DEFAULT '',
    journal       TEXT    DEFAULT '',
    year          TEXT    DEFAULT '',
    doi           TEXT    DEFAULT '',
    pages         INTEGER DEFAULT 0,
    parser        TEXT    DEFAULT '',
    figures       INTEGER DEFAULT 0,
    tables        INTEGER DEFAULT 0,
    links         INTEGER DEFAULT 0,
    references_count INTEGER DEFAULT 0,
    unresolved    INTEGER DEFAULT 0,
    suspicious    INTEGER DEFAULT 0,
    valid         INTEGER,            -- 1 ok, 0 errors, NULL not checked
    sent_at       TEXT,
    starred       INTEGER DEFAULT 0,
    note          TEXT    DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_created ON conversions(created);
CREATE INDEX IF NOT EXISTS idx_sent ON conversions(sent_at);
"""


@dataclass
class Entry:
    """One converted paper."""

    id: int = 0
    created: str = ""
    source_name: str = ""
    source_path: str = ""
    epub_path: str = ""
    title: str = ""
    first_author: str = ""
    authors: str = ""
    journal: str = ""
    year: str = ""
    doi: str = ""
    pages: int = 0
    parser: str = ""
    figures: int = 0
    tables: int = 0
    links: int = 0
    references_count: int = 0
    unresolved: int = 0
    suspicious: int = 0
    valid: int | None = None
    sent_at: str | None = None
    starred: int = 0
    note: str = ""

    @property
    def is_sent(self) -> bool:
        return bool(self.sent_at)

    @property
    def epub_exists(self) -> bool:
        return bool(self.epub_path) and Path(self.epub_path).is_file()

    def to_dict(self) -> dict[str, Any]:
        data = {field_name: getattr(self, field_name) for field_name in _FIELDS}
        data["is_sent"] = self.is_sent
        data["epub_exists"] = self.epub_exists
        return data


_FIELDS = [
    "id", "created", "source_name", "source_path", "epub_path", "title",
    "first_author", "authors", "journal", "year", "doi", "pages", "parser",
    "figures", "tables", "links", "references_count", "unresolved",
    "suspicious", "valid", "sent_at", "starred", "note",
]


class Library:
    """The conversion history, backed by one SQLite file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        # check_same_thread=False: the GUI's worker thread writes entries
        # while the request thread reads them, each with its own call.
        connection = sqlite3.connect(
            self.path, check_same_thread=False, timeout=10
        )
        connection.row_factory = sqlite3.Row
        return connection

    # -- writing -----------------------------------------------------------
    def add(self, entry: Entry) -> Entry:
        entry.created = entry.created or _now()
        columns = [f for f in _FIELDS if f != "id"]
        placeholders = ", ".join("?" for _ in columns)
        values = [getattr(entry, column) for column in columns]
        with self._connect() as connection:
            cursor = connection.execute(
                f"INSERT INTO conversions ({', '.join(columns)}) "
                f"VALUES ({placeholders})",
                values,
            )
            entry.id = int(cursor.lastrowid or 0)
        return entry

    def update(self, entry_id: int, **fields: Any) -> bool:
        allowed = {k: v for k, v in fields.items() if k in _FIELDS and k != "id"}
        if not allowed:
            return False
        assignments = ", ".join(f"{name} = ?" for name in allowed)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE conversions SET {assignments} WHERE id = ?",
                [*allowed.values(), entry_id],
            )
            return cursor.rowcount > 0

    def mark_sent(self, entry_id: int, sent: bool = True) -> bool:
        return self.update(entry_id, sent_at=_now() if sent else None)

    def set_starred(self, entry_id: int, starred: bool) -> bool:
        return self.update(entry_id, starred=1 if starred else 0)

    def delete(self, entry_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM conversions WHERE id = ?", (entry_id,)
            )
            return cursor.rowcount > 0

    # -- reading -----------------------------------------------------------
    def get(self, entry_id: int) -> Entry | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversions WHERE id = ?", (entry_id,)
            ).fetchone()
        return _entry(row) if row else None

    def list(
        self,
        *,
        search: str = "",
        sent: bool | None = None,
        starred: bool | None = None,
        parser: str = "",
        year: str = "",
        sort: str = "created",
        descending: bool = True,
        limit: int = 500,
    ) -> list[Entry]:
        """The shelf, filtered and sorted by the parameters the UI offers."""
        clauses: list[str] = []
        values: list[Any] = []

        if search:
            clauses.append(
                "(title LIKE ? OR authors LIKE ? OR source_name LIKE ? "
                "OR journal LIKE ? OR doi LIKE ?)"
            )
            values.extend([f"%{search}%"] * 5)
        if sent is True:
            clauses.append("sent_at IS NOT NULL")
        elif sent is False:
            clauses.append("sent_at IS NULL")
        if starred is True:
            clauses.append("starred = 1")
        if parser:
            clauses.append("parser = ?")
            values.append(parser)
        if year:
            clauses.append("year = ?")
            values.append(year)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = SORT_FIELDS.get(sort, SORT_FIELDS["created"])
        direction = "DESC" if descending else "ASC"
        query = (
            f"SELECT * FROM conversions {where} "
            f"ORDER BY {order} {direction}, id DESC LIMIT ?"
        )
        values.append(max(1, min(limit, 5000)))
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [_entry(row) for row in rows]

    def stats(self) -> dict[str, int]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN sent_at IS NOT NULL THEN 1 ELSE 0 END) AS sent, "
                "SUM(CASE WHEN starred = 1 THEN 1 ELSE 0 END) AS starred "
                "FROM conversions"
            ).fetchone()
        return {
            "total": row["total"] or 0,
            "sent": row["sent"] or 0,
            "unsent": (row["total"] or 0) - (row["sent"] or 0),
            "starred": row["starred"] or 0,
        }

    def known_values(self, column: str) -> list[str]:
        """Distinct values of a column, for the filter dropdowns."""
        if column not in ("parser", "year", "journal"):
            return []
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT DISTINCT {column} AS value FROM conversions "
                f"WHERE {column} != '' ORDER BY {column}"
            ).fetchall()
        return [row["value"] for row in rows]

    def forget_missing(self) -> int:
        """Drop rows whose EPUB has been deleted from disk."""
        removed = 0
        for entry in self.list(limit=5000):
            if entry.epub_path and not entry.epub_exists:
                self.delete(entry.id)
                removed += 1
        return removed


def _entry(row: sqlite3.Row) -> Entry:
    return Entry(**{key: row[key] for key in row.keys() if key in _FIELDS})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def entry_from_result(result, parser: str) -> Entry:
    """Build a shelf entry from a finished :class:`ConversionResult`."""
    document = result.document
    build = result.build
    metadata = document.metadata if document else None
    validation = result.validation

    entry = Entry(
        source_name=Path(result.source).name,
        source_path=str(result.source),
        epub_path=str(result.epub_path) if result.epub_path else "",
        parser=parser,
    )
    if metadata is not None:
        entry.title = metadata.title or ""
        entry.authors = ", ".join(metadata.authors)
        entry.first_author = metadata.first_author_surname
        entry.journal = metadata.journal or ""
        entry.year = metadata.year or ""
        entry.doi = metadata.doi or ""
    if document is not None:
        entry.pages = document.page_count
        entry.suspicious = len(document.suspicious_drops)
    if build is not None:
        entry.figures = build.figures
        entry.tables = build.tables
        entry.links = build.links
        entry.references_count = build.references
        entry.unresolved = build.unresolved
    if validation is not None:
        entry.valid = 1 if validation.ok else 0
    return entry
