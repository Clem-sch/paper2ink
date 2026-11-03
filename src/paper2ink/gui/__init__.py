"""Local web UI for paper2ink.

Deliberately a browser app rather than a desktop toolkit: an EPUB *is*
XHTML plus CSS, so the browser renders the finished book exactly as the
builder wrote it, with no second rendering engine to install and keep in
sync.  It serves on 127.0.0.1 only and needs no network access.
"""

from __future__ import annotations

from .server import create_app, run

__all__ = ["create_app", "run"]
