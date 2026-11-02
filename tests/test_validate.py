"""Validation layer and configuration file handling."""

from __future__ import annotations

import zipfile
from pathlib import Path

from paper2ink.config import find_config, load_config
from paper2ink.pipeline import ConversionSettings
from paper2ink.validate import validate_epub


def make_epub(path: Path, pages: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        for name, body in pages.items():
            archive.writestr(
                name,
                '<?xml version="1.0" encoding="utf-8"?>'
                '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                f"{body}</body></html>",
            )
    return path


def test_valid_epub_passes(tmp_path):
    make_epub(
        tmp_path / "ok.epub",
        {
            "EPUB/a.xhtml": '<p id="x">text</p><a href="b.xhtml#y">link</a>',
            "EPUB/b.xhtml": '<p id="y">more text</p>',
        },
    )
    report = validate_epub(tmp_path / "ok.epub", run_external=False)
    assert report.ok, report.errors


def test_missing_anchor_is_an_error(tmp_path):
    make_epub(
        tmp_path / "bad.epub",
        {"EPUB/a.xhtml": '<p>t</p><a href="a.xhtml#nope">link</a>'},
    )
    report = validate_epub(tmp_path / "bad.epub", run_external=False)
    assert not report.ok
    assert any("missing anchor" in e for e in report.errors)


def test_missing_file_is_an_error(tmp_path):
    make_epub(
        tmp_path / "bad.epub",
        {"EPUB/a.xhtml": '<p>t</p><a href="gone.xhtml">link</a>'},
    )
    report = validate_epub(tmp_path / "bad.epub", run_external=False)
    assert any("missing file" in e for e in report.errors)


def test_missing_image_is_an_error(tmp_path):
    make_epub(
        tmp_path / "bad.epub", {"EPUB/a.xhtml": '<p>t</p><img src="images/x.png" />'}
    )
    report = validate_epub(tmp_path / "bad.epub", run_external=False)
    assert any("missing image" in e for e in report.errors)


def test_external_links_are_left_alone(tmp_path):
    make_epub(
        tmp_path / "ok.epub",
        {"EPUB/a.xhtml": '<p>t</p><a href="https://doi.org/10.1/x">doi</a>'},
    )
    assert validate_epub(tmp_path / "ok.epub", run_external=False).ok


def test_empty_page_is_only_a_warning(tmp_path):
    make_epub(tmp_path / "e.epub", {"EPUB/a.xhtml": "<div></div>"})
    report = validate_epub(tmp_path / "e.epub", run_external=False)
    assert report.ok
    assert any("no visible content" in w for w in report.warnings)


def test_image_only_page_is_not_empty(tmp_path):
    make_epub(tmp_path / "c.epub", {"EPUB/cover.xhtml": '<img src="x.png" />',
                                    "EPUB/x.png": ""})
    report = validate_epub(tmp_path / "c.epub", run_external=False)
    assert not report.warnings


def test_missing_epub_is_an_error(tmp_path):
    report = validate_epub(tmp_path / "nothing.epub", run_external=False)
    assert not report.ok


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


def test_config_is_applied(tmp_path):
    (tmp_path / "paper2ink.toml").write_text(
        '[paper2ink]\ndpi = 123\ncolor = true\ntables = "html"\n', encoding="utf-8"
    )
    settings = load_config(None, tmp_path, ConversionSettings())
    assert settings.dpi == 123
    assert settings.color is True
    assert settings.tables == "html"


def test_cli_flags_win_over_config(tmp_path):
    (tmp_path / "paper2ink.toml").write_text(
        "[paper2ink]\ndpi = 123\n", encoding="utf-8"
    )
    settings = load_config(
        None, tmp_path, ConversionSettings(dpi=300), overrides={"dpi"}
    )
    assert settings.dpi == 300


def test_boilerplate_extends_the_defaults(tmp_path):
    (tmp_path / "paper2ink.toml").write_text(
        "[paper2ink]\nboilerplate = ['^SECRET.*$']\n", encoding="utf-8"
    )
    settings = load_config(None, tmp_path, ConversionSettings())
    assert "^SECRET.*$" in settings.boilerplate
    assert len(settings.boilerplate) > 1


def test_boilerplate_can_replace_the_defaults(tmp_path):
    (tmp_path / "paper2ink.toml").write_text(
        "[paper2ink]\nboilerplate_extends_defaults = false\n"
        "boilerplate = ['^SECRET.*$']\n",
        encoding="utf-8",
    )
    settings = load_config(None, tmp_path, ConversionSettings())
    assert settings.boilerplate == ["^SECRET.*$"]


def test_broken_config_is_ignored(tmp_path):
    (tmp_path / "paper2ink.toml").write_text("this is not toml {", encoding="utf-8")
    settings = load_config(None, tmp_path, ConversionSettings(dpi=250))
    assert settings.dpi == 250


def test_explicit_config_path_that_does_not_exist(tmp_path):
    assert find_config(tmp_path / "nowhere.toml", tmp_path) is None


def test_explicit_config_path_wins(tmp_path):
    custom = tmp_path / "custom.toml"
    custom.write_text("[paper2ink]\ndpi = 99\n", encoding="utf-8")
    (tmp_path / "paper2ink.toml").write_text(
        "[paper2ink]\ndpi = 111\n", encoding="utf-8"
    )
    assert load_config(custom, tmp_path, ConversionSettings()).dpi == 99
