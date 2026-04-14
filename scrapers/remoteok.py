"""
RemoteOK scraper — uses their public JSON feed.
Feed: https://remoteok.com/api
Returns a JSON array. First element is metadata, the rest are job objects.
"""
from typing import Optional
import time
import requests
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from sqlalchemy.orm import Session

from core.models import Job
from scrapers.base import BaseScraper, random_user_agent

logger = structlog.get_logger()

FEED_URL = "https://remoteok.com/api"

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
            return self._http_fetch()
        except Exception as e:
            logger.error("remoteok.fetch_error", error=str(e))
            return []

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def _http_fetch(self) -> list[dict]:
        # RemoteOK requires a small delay between requests to avoid 429
        time.sleep(1)
        headers = {
            "User-Agent": random_user_agent(),
            "Accept": "application/json",
        }
        resp = requests.get(FEED_URL, headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        # First element is always metadata (has "legal" key), skip it
        return [item for item in data if isinstance(item, dict) and "legal" not in item]

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
