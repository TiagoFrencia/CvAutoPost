import pytest
from unittest.mock import patch, MagicMock
from scrapers.zonajobs import ZonaJobsScraper, SEARCH_QUERIES, SEARCH_URL, BASE_URL


def _make_db():
    db = MagicMock()
    platform = MagicMock()
    platform.name = "zonajobs"
    db.query.return_value.filter_by.return_value.first.return_value = platform
    return db


def _raw_job(**kwargs):
    defaults = {
        "id": 654321,
        "titulo": "Analista de Sistemas",
        "empresa": {"nombre": "TechCorp SRL"},
        "localizacion": "Córdoba",
        "modalidadTrabajo": "Remoto",
        "detalle": "Buscamos analista con experiencia en sistemas.",
        "portal": "zonajobs",
    }
    defaults.update(kwargs)
    return defaults


def test_parse_job_remote_modality():
    scraper = ZonaJobsScraper(_make_db())
    job = scraper.parse_job(_raw_job(modalidadTrabajo="Remoto"))
    assert job is not None
    assert job.modality == "remoto"
    assert job.external_id == "654321"
    assert job.title == "Analista de Sistemas"
    assert job.company == "TechCorp SRL"
    assert job.url == f"{BASE_URL}/empleos/654321"


def test_parse_job_hybrid_modality():
    scraper = ZonaJobsScraper(_make_db())
    job = scraper.parse_job(_raw_job(modalidadTrabajo="Híbrido"))
    assert job is not None
    assert job.modality == "hibrido"


def test_parse_job_presencial_modality():
    scraper = ZonaJobsScraper(_make_db())
    job = scraper.parse_job(_raw_job(modalidadTrabajo="Presencial"))
    assert job is not None
    assert job.modality == "presencial"


def test_parse_job_skips_crossbrand():
    """Jobs from Bumeran that appear in ZonaJobs results should be skipped."""
    scraper = ZonaJobsScraper(_make_db())
    job = scraper.parse_job(_raw_job(portal="bumeran"))
    assert job is None


def test_parse_job_missing_title_returns_none():
    scraper = ZonaJobsScraper(_make_db())
    job = scraper.parse_job(_raw_job(titulo=""))
    assert job is None


def test_search_queries_cover_tech_keywords():
    keywords = [q[0] for q in SEARCH_QUERIES]
    assert any("python" in k.lower() for k in keywords)
    assert any("desarrollador" in k.lower() or "programador" in k.lower() for k in keywords)


def test_fetch_jobs_deduplicates(mocker):
    """Same job id returned by two queries should appear only once."""
    scraper = ZonaJobsScraper(_make_db())
    duplicate_job = _raw_job(id=999)
    mocker.patch.object(scraper, "_fetch_query", return_value=[duplicate_job])
    results = scraper.fetch_jobs()
    assert results.count(duplicate_job) == 1


def test_fetch_jobs_skips_failed_query(mocker):
    """A query that raises should not abort the whole fetch."""
    scraper = ZonaJobsScraper(_make_db())
    calls = {"count": 0}

    def side_effect(keyword, area):
        calls["count"] += 1
        if calls["count"] == 1:
            raise Exception("timeout")
        return [_raw_job(id=calls["count"])]

    mocker.patch.object(scraper, "_fetch_query", side_effect=side_effect)
    results = scraper.fetch_jobs()
    assert len(results) == len(SEARCH_QUERIES) - 1
