"""
GetOnBoard scraper — uses their public REST API.
Docs: https://www.getonboard.com/developers
API base: https://www.getonboard.com/api/v1
"""
from typing import Optional
import requests
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from sqlalchemy.orm import Session

from core.models import Job
from scrapers.base import BaseScraper, random_user_agent

logger = structlog.get_logger()

BASE_URL = "https://www.getonboard.com/api/v1"

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
        try:
            return self._http_get(keyword, page)
        except Exception as e:
            logger.error("getonboard.fetch_error", keyword=keyword, error=str(e))
            return []

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def _http_get(self, keyword: str, page: int = 1) -> list[dict]:
        params = {
            "kws": keyword,
            "remote": "true",
            "per_page": 50,
            "page": page,
        }
        resp = requests.get(
            f"{BASE_URL}/jobs.json",
            params=params,
            headers={"Accept": "application/json", "User-Agent": random_user_agent()},
            timeout=15,
            verify=False,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("jobs", data if isinstance(data, list) else [])

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
