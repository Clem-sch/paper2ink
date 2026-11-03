"""User settings the GUI can write back.

Kept separate from :mod:`paper2ink.config`, which reads ``paper2ink.toml``
for command line defaults: that file belongs to the user and may carry
comments, so a program must not rewrite it.  These settings are machine
written, so they live in a small JSON file instead.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("paper2ink.settings")

__all__ = ["Settings", "settings_path", "load_settings", "save_settings"]

SETTINGS_NAME = "settings.json"


def config_dir() -> Path:
    """Where paper2ink keeps its own files."""
    base = os.environ.get("PAPER2INK_HOME")
    if base:
        return Path(base)
    roaming = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME")
    if roaming:
        return Path(roaming) / "paper2ink"
    return Path.home() / ".paper2ink"


def settings_path() -> Path:
    return config_dir() / SETTINGS_NAME


def default_output_dir() -> Path:
    """Where finished EPUBs go unless the user says otherwise.

    A ``papers`` folder beside the working directory if there is one, which
    is where the PDFs usually live anyway; otherwise one in the home
    directory, so the tool never writes to a surprising place.
    """
    candidate = Path.cwd() / "papers"
    if candidate.is_dir():
        return candidate
    return Path.home() / "paper2ink"


@dataclass
class Settings:
    """Everything the GUI remembers between runs."""

    #: Send-to-Kindle address, e.g. "you_abc123@kindle.com".
    kindle_email: str = ""
    #: Folder the finished EPUBs are written to.
    output_dir: str = ""
    #: Defaults the conversion form starts with.
    parser: str = "pymupdf"
    dpi: int = 250
    color: bool = False
    tables: str = "image"
    cover: bool = True
    #: Run EPUBCheck after building.  It is a separate step in the GUI, so
    #: it never holds up the finished book.
    validate: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    # -- derived -----------------------------------------------------------
    @property
    def output_path(self) -> Path:
        return Path(self.output_dir) if self.output_dir else default_output_dir()

    def mailto_url(self, subject: str, body: str = "") -> str | None:
        """A pre-addressed draft for the Send-to-Kindle address.

        ``mailto:`` cannot carry an attachment - every mail client refuses
        that - so the caller also has to show the user where the file is.
        """
        from urllib.parse import quote

        if not self.kindle_email:
            return None
        url = f"mailto:{quote(self.kindle_email)}?subject={quote(subject)}"
        if body:
            url += f"&body={quote(body)}"
        return url

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["output_dir"] = str(self.output_path)
        return data


def load_settings(path: Path | None = None) -> Settings:
    """Read the settings, falling back to the defaults on any problem."""
    path = path or settings_path()
    if not path.is_file():
        return Settings(output_dir=str(default_output_dir()))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("ignoring unreadable settings at %s: %s", path, exc)
        return Settings(output_dir=str(default_output_dir()))

    known = {f for f in Settings.__dataclass_fields__ if f != "extra"}
    settings = Settings(**{k: v for k, v in raw.items() if k in known})
    settings.extra = {k: v for k, v in raw.items() if k not in known}
    if not settings.output_dir:
        settings.output_dir = str(default_output_dir())
    return settings


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    """Write the settings, creating the folder if needed."""
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**settings.extra, **asdict(settings)}
    payload.pop("extra", None)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path
