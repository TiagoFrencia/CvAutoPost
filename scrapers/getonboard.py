"""
GetOnBoard scraper — uses their public REST API.
Docs: https://www.getonboard.com/developers
API base: https://www.getonboard.com/api/v1
"""
from typing import Optional
import requests
import structlog

from sqlalchemy.orm import Session

from core.models import Job
from scrapers.base import BaseScraper

logger = structlog.get_logger()

BASE_URL = "https://www.getonboard.com/api/v1"
DEFAULT_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; auto-applier-bot/1.0)",
}

# Keywords that target the remoto CV profile
SEARCH_KEYWORDS = [
    "java",
    "spring boot",
    "react",
    "python",
    "full stack",
    "backend",
    "frontend",
    "javascript",
]


class GetOnBoardScraper(BaseScraper):
    platform_name = "getonboard"

    def fetch_jobs(self) -> list[dict]:
        all_jobs: list[dict] = []

        for keyword in SEARCH_KEYWORDS:
            jobs = self._fetch_page(keyword)
            all_jobs.extend(jobs)
            logger.debug("getonboard.keyword_done", keyword=keyword, count=len(jobs))

        # Deduplicate by external id before returning
        seen: set[str] = set()
        unique: list[dict] = []
        for job in all_jobs:
            jid = str(job.get("id", ""))
            if jid and jid not in seen:
                seen.add(jid)
                unique.append(job)

        return unique

    def _fetch_page(self, keyword: str, page: int = 1) -> list[dict]:
        params = {
            "kws": keyword,
            "remote": "true",
            "per_page": 50,
            "page": page,
        }
        try:
            resp = requests.get(
                f"{BASE_URL}/jobs.json",
                params=params,
                headers=DEFAULT_HEADERS,
                timeout=15,
                verify=False,
            )
            resp.raise_for_status()
            data = resp.json()

            # The API wraps jobs under a "jobs" key
            # Adjust key name if the API returns a different structure
            return data.get("jobs", data if isinstance(data, list) else [])

        except requests.RequestException as e:
            logger.error("getonboard.fetch_error", keyword=keyword, error=str(e))
            return []

    def parse_job(self, raw: dict) -> Optional[Job]:
        external_id = str(raw.get("id", ""))
        title = raw.get("title") or raw.get("position", "")
        url = raw.get("url") or raw.get("application_url", "")

        if not external_id or not title or not url:
            return None

        # Company can be a nested object or a string depending on API version
        company_raw = raw.get("company", {})
        if isinstance(company_raw, dict):
            company = company_raw.get("name", "")
        else:
            company = str(company_raw)

        # Location
        country_raw = raw.get("country", {})
        if isinstance(country_raw, dict):
            location = country_raw.get("name", "")
        else:
            location = str(country_raw) if country_raw else ""

        # Modality
        is_remote = raw.get("remote", False)
        modality = "remoto" if is_remote else raw.get("modality", "")

        description = raw.get("description") or raw.get("body", "")

        return Job(
            external_id=external_id,
            title=title,
            company=company,
            location=location,
            url=url,
            description=description,
            modality=modality,
            status="PENDING",
        )
