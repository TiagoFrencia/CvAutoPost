"""
RemoteOK scraper — uses their public JSON feed.
Feed: https://remoteok.com/api
Returns a JSON array. First element is metadata, the rest are job objects.
"""
from typing import Optional
import time
import requests
import structlog

from sqlalchemy.orm import Session

from core.models import Job
from scrapers.base import BaseScraper

logger = structlog.get_logger()

FEED_URL = "https://remoteok.com/api"
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; auto-applier-bot/1.0)",
    "Accept": "application/json",
}

# Tags to filter — only keep jobs that have at least one of these
TARGET_TAGS = {
    "java", "spring", "react", "javascript", "typescript",
    "python", "node", "backend", "frontend", "fullstack",
    "full-stack", "junior", "entry-level",
}

# Hard-reject tags — skip jobs with any of these
REJECT_TAGS = {
    "senior", "staff", "principal", "architect", "manager",
    "lead", "director", "vp", "c-level",
}


class RemoteOKScraper(BaseScraper):
    platform_name = "remoteok"

    def fetch_jobs(self) -> list[dict]:
        try:
            # RemoteOK requires a small delay between requests to avoid 429
            time.sleep(1)
            resp = requests.get(FEED_URL, headers=DEFAULT_HEADERS, timeout=20)
            resp.raise_for_status()
            data = resp.json()

            # First element is always metadata (has "legal" key), skip it
            jobs = [item for item in data if isinstance(item, dict) and "legal" not in item]
            return jobs

        except requests.RequestException as e:
            logger.error("remoteok.fetch_error", error=str(e))
            return []

    def parse_job(self, raw: dict) -> Optional[Job]:
        external_id = str(raw.get("id", raw.get("slug", "")))
        title = raw.get("position", "")
        url = raw.get("url", "")

        if not external_id or not title or not url:
            return None

        tags: list[str] = [t.lower() for t in raw.get("tags", [])]

        # Filter: must match at least one target tag
        if not any(tag in TARGET_TAGS for tag in tags):
            return None

        # Filter: skip if any reject tag present
        if any(tag in REJECT_TAGS for tag in tags):
            return None

        company = raw.get("company", "")
        description = raw.get("description", "")
        salary_range = raw.get("salary", "")

        return Job(
            external_id=external_id,
            title=title,
            company=company,
            location="Remote",
            url=url,
            description=description,
            salary_range=salary_range if salary_range else None,
            modality="remoto",
            status="PENDING",
        )
