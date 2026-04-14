import pytest
from unittest.mock import patch, MagicMock
from scrapers.bumeran import BumeranScraper, SEARCH_QUERIES, SEARCH_URL, BASE_URL


def _make_db():
    db = MagicMock()
    platform = MagicMock()
    platform.name = "bumeran"
    db.query.return_value.filter_by.return_value.first.return_value = platform
    return db


def _raw_job(**kwargs):
    defaults = {
        "id": 123456,
        "titulo": "Desarrollador Python",
        "empresa": {"nombre": "Acme SA"},
        "localizacion": "Buenos Aires",
        "modalidadTrabajo": "Remoto",
        "detalle": "Buscamos desarrollador Python con 2 años de experiencia.",
        "portal": "bumeran",
    }
    defaults.update(kwargs)
    return defaults


def test_parse_job_remote_modality():
    scraper = BumeranScraper(_make_db())
    job = scraper.parse_job(_raw_job(modalidadTrabajo="Remoto"))
    assert job is not None
    assert job.modality == "remoto"
    assert job.external_id == "123456"
    assert job.title == "Desarrollador Python"
    assert job.company == "Acme SA"
    assert job.url == f"{BASE_URL}/empleos/123456"


def test_parse_job_hybrid_modality():
    scraper = BumeranScraper(_make_db())
    job = scraper.parse_job(_raw_job(modalidadTrabajo="Híbrido"))
    assert job is not None
    assert job.modality == "hibrido"


def test_parse_job_presencial_modality():
    scraper = BumeranScraper(_make_db())
    job = scraper.parse_job(_raw_job(modalidadTrabajo="Presencial"))
    assert job is not None
    assert job.modality == "presencial"


def test_parse_job_skips_crossbrand():
    """Jobs from ZonaJobs that appear in Bumeran results should be skipped."""
    scraper = BumeranScraper(_make_db())
    job = scraper.parse_job(_raw_job(portal="zonajobs"))
    assert job is None


def test_parse_job_missing_title_returns_none():
    scraper = BumeranScraper(_make_db())
    job = scraper.parse_job(_raw_job(titulo=""))
    assert job is None


def test_search_queries_cover_tech_keywords():
    keywords = [q[0] for q in SEARCH_QUERIES]
    assert any("python" in k.lower() for k in keywords)
    assert any("desarrollador" in k.lower() or "programador" in k.lower() for k in keywords)


def test_fetch_jobs_deduplicates(mocker):
    """Same job id returned by two queries should appear only once."""
    scraper = BumeranScraper(_make_db())
    duplicate_job = _raw_job(id=999)
    mocker.patch.object(scraper, "_fetch_query", return_value=[duplicate_job])
    results = scraper.fetch_jobs()
    assert results.count(duplicate_job) == 1


def test_fetch_jobs_skips_failed_query(mocker):
    """A query that raises should not abort the whole fetch."""
    scraper = BumeranScraper(_make_db())
    calls = {"count": 0}

    def side_effect(keyword, area):
        calls["count"] += 1
        if calls["count"] == 1:
            raise Exception("timeout")
        return [_raw_job(id=calls["count"])]

    mocker.patch.object(scraper, "_fetch_query", side_effect=side_effect)
    results = scraper.fetch_jobs()
    # First query failed, remaining queries should still return results
    assert len(results) == len(SEARCH_QUERIES) - 1
