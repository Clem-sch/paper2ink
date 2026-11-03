"""User settings: what the GUI remembers between runs."""

from __future__ import annotations

import json
from pathlib import Path

from paper2ink.settings import Settings, load_settings, save_settings


def test_defaults_are_usable_without_a_file(tmp_path):
    settings = load_settings(tmp_path / "nothing.json")
    assert settings.kindle_email == ""
    assert settings.parser == "pymupdf"
    assert settings.output_path.is_absolute()


def test_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    save_settings(
        Settings(
            kindle_email="me_abc@kindle.com",
            output_dir=str(tmp_path / "out"),
            dpi=300,
            parser="docling",
        ),
        path,
    )
    loaded = load_settings(path)
    assert loaded.kindle_email == "me_abc@kindle.com"
    assert loaded.dpi == 300
    assert loaded.parser == "docling"
    assert loaded.output_path == tmp_path / "out"


def test_the_folder_is_created_on_save(tmp_path):
    path = tmp_path / "deep" / "nested" / "settings.json"
    save_settings(Settings(), path)
    assert path.is_file()


def test_unreadable_settings_fall_back_instead_of_crashing(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ this is not json", encoding="utf-8")
    assert load_settings(path).parser == "pymupdf"


def test_unknown_keys_are_kept_not_dropped(tmp_path):
    """A newer version's keys must survive a round trip through an older one."""
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"kindle_email": "a@kindle.com", "future_option": 42}),
        encoding="utf-8",
    )
    loaded = load_settings(path)
    assert loaded.extra == {"future_option": 42}
    save_settings(loaded, path)
    assert json.loads(path.read_text(encoding="utf-8"))["future_option"] == 42


# --------------------------------------------------------------------------
# the mail draft
# --------------------------------------------------------------------------


def test_mailto_is_none_without_an_address():
    assert Settings().mailto_url("Subject") is None


def test_mailto_carries_the_address_and_subject():
    settings = Settings(kindle_email="me_abc@kindle.com")
    url = settings.mailto_url("Vreeker 2025")
    assert url.startswith("mailto:me_abc%40kindle.com?")
    assert "subject=Vreeker%202025" in url


def test_mailto_escapes_the_body():
    url = Settings(kindle_email="a@b.com").mailto_url("S", "line one & two")
    assert "%26" in url          # the ampersand must not split the query
