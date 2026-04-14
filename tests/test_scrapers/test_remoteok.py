import pytest
import responses as resp_mock

from unittest.mock import MagicMock
from scrapers.remoteok import RemoteOKScraper, FEED_URL, TARGET_TAGS


def make_db():
    db = MagicMock()
    platform = MagicMock()
    platform.name = "remoteok"
    platform.is_active = True
    platform.id = 1
    db.query.return_value.filter_by.return_value.first.return_value = platform
    return db


SAMPLE_RESPONSE = [
    {"legal": "RemoteOK metadata"},
    {
        "id": "123",
        "slug": "react-developer-acme",
        "company": "Acme Corp",
        "position": "React Developer",
        "tags": ["react", "javascript", "frontend"],
        "description": "We are looking for a React Developer...",
        "url": "https://remoteok.com/remote-jobs/123",
        "date": "2026-04-01T10:00:00Z",
        "salary": "$80k-$120k",
    },
    {
        "id": "456",
        "slug": "senior-java-bigcorp",
        "company": "BigCorp",
        "position": "Senior Java Engineer",
        "tags": ["java", "senior", "backend"],  # has "senior" → should be filtered out
        "description": "5+ years required...",
        "url": "https://remoteok.com/remote-jobs/456",
        "date": "2026-04-01T10:00:00Z",
    },
    {
        "id": "789",
        "slug": "marketing-manager-xyz",
        "company": "XYZ",
        "position": "Marketing Manager",
        "tags": ["marketing", "manager"],  # no target tags → should be filtered
        "description": "Marketing role...",
        "url": "https://remoteok.com/remote-jobs/789",
        "date": "2026-04-01T10:00:00Z",
    },
]


@resp_mock.activate
def test_fetch_returns_jobs_without_metadata():
    resp_mock.add(resp_mock.GET, FEED_URL, json=SAMPLE_RESPONSE, status=200)
    db = make_db()
    scraper = RemoteOKScraper(db)
    raw = scraper.fetch_jobs()
    # Metadata element filtered out
    assert all("legal" not in job for job in raw)
    assert len(raw) == 3  # all non-metadata items returned (filtering happens in parse_job)


@resp_mock.activate
def test_parse_job_valid():
    resp_mock.add(resp_mock.GET, FEED_URL, json=SAMPLE_RESPONSE, status=200)
    db = make_db()
    scraper = RemoteOKScraper(db)
    job = scraper.parse_job(SAMPLE_RESPONSE[1])
    assert job is not None
    assert job.title == "React Developer"
    assert job.company == "Acme Corp"
    assert job.modality == "remoto"
    assert job.external_id == "123"


@resp_mock.activate
def test_parse_job_filters_senior():
    resp_mock.add(resp_mock.GET, FEED_URL, json=SAMPLE_RESPONSE, status=200)
    db = make_db()
    scraper = RemoteOKScraper(db)
    job = scraper.parse_job(SAMPLE_RESPONSE[2])  # has "senior" tag
    assert job is None


@resp_mock.activate
def test_parse_job_filters_no_target_tags():
    resp_mock.add(resp_mock.GET, FEED_URL, json=SAMPLE_RESPONSE, status=200)
    db = make_db()
    scraper = RemoteOKScraper(db)
    job = scraper.parse_job(SAMPLE_RESPONSE[3])  # marketing, no tech tags
    assert job is None


@resp_mock.activate
def test_fetch_handles_api_error():
    resp_mock.add(resp_mock.GET, FEED_URL, status=500)
    db = make_db()
    scraper = RemoteOKScraper(db)
    result = scraper.fetch_jobs()
    assert result == []
