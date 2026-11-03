"""The web UI's HTTP API, driven end to end against the sample PDF."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="the web UI is an optional extra")

from fastapi.testclient import TestClient  # noqa: E402

from paper2ink.gui.jobs import JobStore  # noqa: E402
from paper2ink.gui.server import create_app  # noqa: E402
from paper2ink.library import Library  # noqa: E402
from paper2ink.settings import Settings  # noqa: E402


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> JobStore:
    """A store that writes everything under the test's own directory.

    The output folder has to be set explicitly: the default is a "papers"
    folder beside the working directory, which during a test run is the
    real one.
    """
    root = tmp_path_factory.mktemp("gui")
    return JobStore(
        root,
        library=Library(root / "library.sqlite3"),
        settings=Settings(output_dir=str(root / "out")),
    )


@pytest.fixture(scope="module")
def client(store: JobStore):
    with TestClient(create_app(store)) as test_client:
        yield test_client
    store.shutdown()


@pytest.fixture(scope="module")
def job(client, sample_pdf: Path) -> dict:
    """Upload the sample PDF and wait for the conversion to finish."""
    with sample_pdf.open("rb") as handle:
        response = client.post(
            "/api/jobs",
            files={"file": (sample_pdf.name, handle, "application/pdf")},
            data={"parser": "pymupdf", "dpi": "110", "tables": "image"},
        )
    assert response.status_code == 201, response.text
    job_id = response.json()["id"]

    for _ in range(600):
        status = client.get(f"/api/jobs/{job_id}").json()
        if status["status"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert status["status"] == "done", status.get("error")
    return status


# --------------------------------------------------------------------------
# the page itself
# --------------------------------------------------------------------------


def test_index_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "paper2ink" in response.text


def test_static_files_are_served(client):
    for path in ("/static/app.js", "/static/style.css"):
        assert client.get(path).status_code == 200


# --------------------------------------------------------------------------
# uploads
# --------------------------------------------------------------------------


def test_non_pdf_upload_is_rejected(client):
    response = client.post(
        "/api/jobs",
        files={"file": ("notes.txt", b"just text", "text/plain")},
    )
    assert response.status_code == 400
    assert "PDF" in response.json()["detail"]


def test_empty_upload_is_rejected(client):
    response = client.post(
        "/api/jobs", files={"file": ("empty.pdf", b"", "application/pdf")}
    )
    assert response.status_code == 400


def test_unknown_parser_is_rejected(client, sample_pdf):
    with sample_pdf.open("rb") as handle:
        response = client.post(
            "/api/jobs",
            files={"file": (sample_pdf.name, handle, "application/pdf")},
            data={"parser": "magic"},
        )
    assert response.status_code == 400


def test_unknown_job_is_a_404(client):
    assert client.get("/api/jobs/doesnotexist").status_code == 404


# --------------------------------------------------------------------------
# a finished conversion
# --------------------------------------------------------------------------


def test_conversion_reports_its_summary(job):
    assert job["build"]["references"] == 3
    assert job["build"]["figures"] >= 1
    assert job["build"]["links"] >= 4
    assert job["pages"] >= 3


def test_job_appears_in_the_listing(client, job):
    listing = client.get("/api/jobs").json()["jobs"]
    assert any(entry["id"] == job["id"] for entry in listing)


def test_epub_can_be_downloaded(client, job):
    response = client.get(f"/api/jobs/{job['id']}/epub")
    assert response.status_code == 200
    assert response.content[:2] == b"PK"          # a zip, i.e. an EPUB
    assert "epub" in response.headers["content-type"]


# --------------------------------------------------------------------------
# the review view
# --------------------------------------------------------------------------


def test_review_groups_content_by_page(client, job):
    data = client.get(f"/api/jobs/{job['id']}/review").json()
    assert data["page_count"] >= 3
    assert len(data["page_sizes"]) == data["page_count"]
    assert data["metadata"]["title"].startswith("A Minimal Paper")

    first = next(page for page in data["pages"] if page["number"] == 0)
    assert first["kept"], "page one should have extracted text"
    for block in first["kept"]:
        assert len(block["bbox"]) == 4


def test_review_reports_the_figure_region(client, job):
    data = client.get(f"/api/jobs/{job['id']}/review").json()
    assets = [asset for page in data["pages"] for asset in page["assets"]]
    assert any(asset["key"] == "fig-1" for asset in assets)


def test_review_lists_dropped_text_with_reasons(client, job):
    data = client.get(f"/api/jobs/{job['id']}/review").json()
    drops = [drop for page in data["pages"] for drop in page["dropped"]]
    for drop in drops:
        assert drop["reason"]
        assert "suspicious" in drop


def test_page_images_are_rendered(client, job):
    response = client.get(f"/api/jobs/{job['id']}/page/0")
    assert response.status_code == 200
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_page_image_out_of_range(client, job):
    assert client.get(f"/api/jobs/{job['id']}/page/999").status_code == 404


# --------------------------------------------------------------------------
# the reader
# --------------------------------------------------------------------------


def test_reader_lists_chapters_in_spine_order(client, job):
    chapters = client.get(f"/api/jobs/{job['id']}/reader").json()["chapters"]
    assert chapters, "the book should have readable documents"
    assert all(chapter["href"].endswith(".xhtml") for chapter in chapters)
    titles = [chapter["title"] for chapter in chapters]
    assert any("Figures" in title for title in titles)


def test_book_files_are_served_for_the_iframe(client, job):
    chapters = client.get(f"/api/jobs/{job['id']}/reader").json()["chapters"]
    response = client.get(f"/api/jobs/{job['id']}/book/{chapters[0]['href']}")
    assert response.status_code == 200
    assert "xhtml" in response.headers["content-type"]
    assert "<body" in response.text


@pytest.mark.parametrize(
    "path",
    ["../../../etc/passwd", "..%2f..%2fsecret", "nope.xhtml"],
)
def test_book_route_refuses_paths_outside_the_book(client, job, path):
    response = client.get(f"/api/jobs/{job['id']}/book/{path}")
    assert response.status_code == 404


# --------------------------------------------------------------------------
# removal
# --------------------------------------------------------------------------


def test_job_can_be_removed(client, sample_pdf):
    with sample_pdf.open("rb") as handle:
        created = client.post(
            "/api/jobs",
            files={"file": (sample_pdf.name, handle, "application/pdf")},
            data={"parser": "pymupdf", "dpi": "72"},
        ).json()
    assert client.delete(f"/api/jobs/{created['id']}").json()["deleted"] is True
    assert client.get(f"/api/jobs/{created['id']}").status_code == 404


def test_structural_pages_get_readable_names(client, job):
    """Cover and title page carry the book title, which reads as a duplicate."""
    chapters = client.get(f"/api/jobs/{job['id']}/reader").json()["chapters"]
    titles = [chapter["title"] for chapter in chapters]
    assert "Cover" in titles
    assert "Title page" in titles
    assert len(titles) == len(set(titles)), f"duplicate chapter names: {titles}"


def test_static_files_are_not_cached(client):
    """A stale script after an upgrade is a confusing bug to chase."""
    response = client.get("/static/app.js")
    assert "no-store" in response.headers.get("cache-control", "")


def test_review_reports_unresolved_targets(client, job):
    data = client.get(f"/api/jobs/{job['id']}/review").json()
    assert isinstance(data["unresolved"], dict)


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------


def test_settings_start_from_the_defaults(client):
    data = client.get("/api/settings").json()
    assert data["parser"] in ("pymupdf", "docling", "auto")
    assert data["output_dir"]
    assert "path" in data


def test_settings_round_trip(client, tmp_path):
    before = client.get("/api/settings").json()
    target = tmp_path / "shelf"
    response = client.put(
        "/api/settings",
        json={"kindle_email": "me_abc@kindle.com", "output_dir": str(target),
              "dpi": 300, "tables": "html"},
    )
    assert response.status_code == 200
    assert client.get("/api/settings").json()["kindle_email"] == "me_abc@kindle.com"
    assert target.is_dir(), "the output folder should be created"
    # Put everything back: this module shares one store, and the shelf
    # tests check where the already converted book was written.
    client.put(
        "/api/settings",
        json={"dpi": 250, "tables": "image", "output_dir": before["output_dir"]},
    )


@pytest.mark.parametrize("bad", ["not-an-address", "@nowhere", "a b@c.de"])
def test_a_bad_kindle_address_is_rejected(client, bad):
    assert client.put("/api/settings", json={"kindle_email": bad}).status_code == 400


def test_the_address_can_be_cleared(client):
    client.put("/api/settings", json={"kindle_email": "me@kindle.com"})
    assert client.put("/api/settings", json={"kindle_email": ""}).status_code == 200
    assert client.get("/api/settings").json()["kindle_email"] == ""


def test_unknown_parser_in_settings_is_rejected(client):
    assert client.put("/api/settings", json={"parser": "magic"}).status_code == 400


def test_dpi_is_clamped(client):
    assert client.put("/api/settings", json={"dpi": 9999}).json()["dpi"] == 600
    client.put("/api/settings", json={"dpi": 250})


# --------------------------------------------------------------------------
# the shelf
# --------------------------------------------------------------------------


def test_a_finished_conversion_lands_in_the_library(client, job):
    data = client.get("/api/library").json()
    assert data["stats"]["total"] >= 1
    entry = next(e for e in data["entries"] if e["id"] == job["entry_id"])
    assert entry["title"].startswith("A Minimal Paper")
    assert entry["references_count"] == 3
    assert entry["is_sent"] is False
    assert entry["epub_exists"] is True


def test_the_epub_is_written_to_the_configured_folder(client, job, store):
    entry = client.get("/api/library").json()["entries"][0]
    assert Path(entry["epub_path"]).parent == store.settings.output_path


def test_library_filters_and_sorting_are_offered(client, job):
    data = client.get("/api/library").json()
    assert "created" in data["sort_fields"] and "year" in data["sort_fields"]
    assert "pymupdf" in data["parsers"]


def test_searching_the_shelf(client, job):
    found = client.get("/api/library", params={"search": "Minimal"}).json()
    assert found["entries"]
    missing = client.get("/api/library", params={"search": "zzzznothing"}).json()
    assert missing["entries"] == []


def test_marking_an_entry_as_sent_and_back(client, job):
    entry_id = job["entry_id"]
    assert client.post(f"/api/library/{entry_id}/sent").json()["is_sent"] is True
    unsent = client.get("/api/library", params={"sent": "no"}).json()["entries"]
    assert all(e["id"] != entry_id for e in unsent)

    client.post(f"/api/library/{entry_id}/sent", json={"sent": False})
    assert client.get("/api/library", params={"sent": "no"}).json()["entries"]


def test_starring_an_entry(client, job):
    entry_id = job["entry_id"]
    client.post(f"/api/library/{entry_id}/starred", json={"starred": True})
    starred = client.get("/api/library", params={"starred": "yes"}).json()
    assert any(e["id"] == entry_id for e in starred["entries"])
    client.post(f"/api/library/{entry_id}/starred", json={"starred": False})


def test_downloading_from_the_shelf(client, job):
    response = client.get(f"/api/library/{job['entry_id']}/epub")
    assert response.status_code == 200
    assert response.content[:2] == b"PK"


def test_unknown_entry_is_a_404(client):
    assert client.post("/api/library/999999/sent").status_code == 404
    assert client.get("/api/library/999999/epub").status_code == 404


# --------------------------------------------------------------------------
# sending
# --------------------------------------------------------------------------


def test_sending_without_an_address_explains_itself(client, job):
    client.put("/api/settings", json={"kindle_email": ""})
    response = client.post(f"/api/library/{job['entry_id']}/send")
    assert response.status_code == 400
    assert "Settings" in response.json()["detail"]


def test_sending_returns_a_prepared_draft(client, job, monkeypatch):
    import paper2ink.gui.server as server

    revealed: list[Path] = []
    monkeypatch.setattr(server, "_reveal", lambda path: revealed.append(path) or True)

    client.put("/api/settings", json={"kindle_email": "me_abc@kindle.com"})
    data = client.post(f"/api/library/{job['entry_id']}/send").json()

    assert data["mailto"].startswith("mailto:me_abc%40kindle.com")
    assert data["file"].endswith(".epub")
    assert revealed, "the folder holding the book should be revealed"


def test_sending_does_not_mark_as_sent_by_itself(client, job, monkeypatch):
    """The mail is only drafted, so the user confirms that it went out."""
    import paper2ink.gui.server as server

    monkeypatch.setattr(server, "_reveal", lambda path: True)
    client.put("/api/settings", json={"kindle_email": "me_abc@kindle.com"})
    client.post(f"/api/library/{job['entry_id']}/sent", json={"sent": False})
    client.post(f"/api/library/{job['entry_id']}/send")

    entry = next(
        e for e in client.get("/api/library").json()["entries"]
        if e["id"] == job["entry_id"]
    )
    assert entry["is_sent"] is False
