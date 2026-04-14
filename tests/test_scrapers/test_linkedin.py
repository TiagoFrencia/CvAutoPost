from scrapers.linkedin import (
    _build_search_specs,
    _build_search_url,
    _normalize_modality,
    _to_slug,
)


def test_to_slug_removes_accents():
    assert _to_slug("Río Cuarto") == "rio-cuarto"


def test_normalize_modality():
    assert _normalize_modality("Remote") == "remoto"
    assert _normalize_modality("Híbrido") == "hibrido"
    assert _normalize_modality("On-site") == "presencial"


def test_build_search_url_for_remote():
    url = _build_search_url(
        {"keyword": "python", "location": "Argentina", "work_type": "2"}
    )
    assert "keywords=python" in url
    assert "location=Argentina" in url
    assert "f_WT=2" in url
    assert "f_AL=true" in url


def test_build_search_specs_remote_and_local():
    remote_cv = {
        "target_role": {
            "titles": ["Python Developer", "Java Developer"],
        }
    }
    local_cv = {
        "target_role": {
            "titles": ["Cajero", "Vendedor"],
        },
        "personal_info": {
            "location": {
                "city": "Río Cuarto",
                "province": "Córdoba",
                "country": "Argentina",
            }
        },
    }

    specs = _build_search_specs(remote_cv, local_cv)
    remote_specs = [spec for spec in specs if spec["mode"] == "remote"]
    local_specs = [spec for spec in specs if spec["mode"] == "local"]

    assert remote_specs
    assert local_specs
    assert all(spec["location"] == "Argentina" for spec in remote_specs)
    assert all(spec.get("city") == "Río Cuarto" for spec in local_specs)
