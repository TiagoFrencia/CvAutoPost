from scrapers.indeed import (
    IndeedScraper,
    _build_search_url,
    _normalize_modality,
    _to_slug,
)


def test_build_search_url():
    assert _build_search_url("python", "Argentina") == (
        "https://ar.indeed.com/jobs?q=python&l=Argentina"
    )


def test_to_slug_removes_accents():
    assert _to_slug("Río Cuarto") == "rio-cuarto"


def test_normalize_modality():
    assert _normalize_modality("Desde casa") == "remoto"
    assert _normalize_modality("Híbrido") == "hibrido"
    assert _normalize_modality("Tiempo completo") == "presencial"


def test_indeed_builds_remote_and_local_specs():
    scraper = IndeedScraper(None)

    remote_specs = [spec for spec in scraper.search_specs if spec["mode"] == "remote"]
    local_specs = [spec for spec in scraper.search_specs if spec["mode"] == "local"]

    assert remote_specs
    assert local_specs
    assert all("l=Argentina" in spec["url"] for spec in remote_specs)
    assert all("R%C3%ADo+Cuarto%2C+C%C3%B3rdoba" in spec["url"] for spec in local_specs)
