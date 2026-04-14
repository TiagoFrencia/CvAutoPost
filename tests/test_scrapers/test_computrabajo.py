from scrapers.computrabajo import (
    ComputrabajoScraper,
    _build_local_search_url,
    _build_remote_search_url,
    _normalize_modality,
    _to_slug,
)


def test_build_remote_search_url():
    assert _build_remote_search_url("java") == "https://ar.computrabajo.com/empleos-de-java?l=Argentina"


def test_build_local_search_url():
    assert _build_local_search_url("cajero") == "https://ar.computrabajo.com/trabajo-de-cajero-en-rio-cuarto"


def test_to_slug_removes_accents():
    assert _to_slug("Río Cuarto") == "rio-cuarto"


def test_normalize_modality():
    assert _normalize_modality("Remoto") == "remoto"
    assert _normalize_modality("Presencial y remoto") == "hibrido"
    assert _normalize_modality("Presencial") == "presencial"


def test_computrabajo_builds_remote_and_local_specs():
    scraper = ComputrabajoScraper(None)

    remote_specs = [spec for spec in scraper.search_specs if spec["mode"] == "remote"]
    local_specs = [spec for spec in scraper.search_specs if spec["mode"] == "local"]

    assert remote_specs
    assert local_specs
    assert all("?l=Argentina" in spec["url"] for spec in remote_specs)
    assert all("rio-cuarto" in spec["url"] for spec in local_specs)
