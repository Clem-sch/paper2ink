"""Optional ``paper2ink.toml`` with defaults and boilerplate patterns.

Looked up in this order: the path passed to ``--config``, then
``paper2ink.toml`` next to the input, in the current directory, and in the
user's config directory.  Command line options always win.
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path

from .pipeline import ConversionSettings

log = logging.getLogger("paper2ink.config")

__all__ = ["find_config", "load_config", "CONFIG_NAME"]

CONFIG_NAME = "paper2ink.toml"

# Keys that may be set in the [paper2ink] table.
_SIMPLE_KEYS = {
    "parser": str,
    "dpi": int,
    "color": bool,
    "tables": str,
    "formulas": str,
    "formula_backend": str,
    "cover": bool,
    "azw3": bool,
    "validate": bool,
}


def find_config(explicit: Path | None, source: Path | None) -> Path | None:
    if explicit:
        return explicit if explicit.is_file() else None
    candidates: list[Path] = []
    if source:
        base = source if source.is_dir() else source.parent
        candidates.append(base / CONFIG_NAME)
    candidates.append(Path.cwd() / CONFIG_NAME)
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.environ.get("APPDATA")
    if config_home:
        candidates.append(Path(config_home) / "paper2ink" / CONFIG_NAME)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load_config(
    explicit: Path | None,
    source: Path | None,
    settings: ConversionSettings,
    *,
    overrides: set[str] | None = None,
) -> ConversionSettings:
    """Apply a config file to *settings* without touching explicit CLI flags."""
    path = find_config(explicit, source)
    if path is None:
        return settings
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("ignoring %s: %s", path, exc)
        return settings

    table = data.get("paper2ink", data)
    overrides = overrides or set()
    for key, caster in _SIMPLE_KEYS.items():
        if key in table and key not in overrides:
            try:
                setattr(settings, key, caster(table[key]))
            except (TypeError, ValueError):
                log.warning("ignoring %s.%s in %s", "paper2ink", key, path)

    patterns = table.get("boilerplate")
    if isinstance(patterns, list) and patterns:
        extend = bool(table.get("boilerplate_extends_defaults", True))
        if extend:
            from .cleanup import DEFAULT_BOILERPLATE

            settings.boilerplate = [*DEFAULT_BOILERPLATE, *map(str, patterns)]
        else:
            settings.boilerplate = [str(p) for p in patterns]

    log.info("loaded configuration from %s", path)
    return settings
