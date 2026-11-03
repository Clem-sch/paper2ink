"""The web UI's HTTP API, driven end to end against the sample PDF."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="the web UI is an optional extra")

from fastapi.testclient import TestClient  # noqa: E402

from paper2ink.gui.jobs import JobStore  # noqa: E402
from paper2ink.gui.server import create_app  # noqa: E402


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory):
    store = JobStore(tmp_path_factory.mktemp("gui"))
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
