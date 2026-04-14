import pytest
import responses as resp_mock

from unittest.mock import MagicMock
from scrapers.getonboard import GetOnBoardScraper, BASE_URL


def make_db():
    db = MagicMock()
    platform = MagicMock()
    platform.name = "getonboard"
    platform.is_active = True
    platform.id = 2
    db.query.return_value.filter_by.return_value.first.return_value = platform
    return db


SAMPLE_JOB = {
    "id": 42,
    "title": "Junior Full Stack Developer",
    "company": {"name": "TechStartup"},
    "country": {"name": "Argentina"},
    "remote": True,
    "url": "https://www.getonboard.com/jobs/42",
    "description": "We need a junior dev...",
    "modality": "remote",
}

SAMPLE_RESPONSE = {"jobs": [SAMPLE_JOB]}


@resp_mock.activate
def test_parse_job_valid():
    db = make_db()
    scraper = GetOnBoardScraper(db)
    job = scraper.parse_job(SAMPLE_JOB)
    assert job is not None
    assert job.title == "Junior Full Stack Developer"
    assert job.company == "TechStartup"
    assert job.external_id == "42"
    assert job.modality == "remoto"
    assert job.location == "Argentina"


def test_parse_job_missing_required_fields():
    db = make_db()
    scraper = GetOnBoardScraper(db)
    assert scraper.parse_job({}) is None
    assert scraper.parse_job({"id": "1"}) is None           # no title
    assert scraper.parse_job({"id": "1", "title": "X"}) is None  # no url


@resp_mock.activate
def test_fetch_handles_api_error():
    resp_mock.add(resp_mock.GET, f"{BASE_URL}/jobs.json", status=429)
    db = make_db()
    scraper = GetOnBoardScraper(db)
    result = scraper._fetch_page("python")
    assert result == []


@resp_mock.activate
def test_fetch_deduplicates_across_keywords():
    # Same job returned for two different keywords → should appear only once
    for _ in range(len(scraper_keywords())):
        resp_mock.add(
            resp_mock.GET,
            f"{BASE_URL}/jobs.json",
            json={"jobs": [SAMPLE_JOB]},
            status=200,
        )
    db = make_db()
    scraper = GetOnBoardScraper(db)
    jobs = scraper.fetch_jobs()
    assert len(jobs) == 1  # deduplicated by id


def scraper_keywords():
    from scrapers.getonboard import SEARCH_KEYWORDS
    return SEARCH_KEYWORDS
