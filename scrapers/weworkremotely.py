"""
We Work Remotely scraper — RSS feed (no auth, no browser needed).
Jobs link to external company sites → no applier. They get scored by AI
but never auto-applied (run_apply_queue skips platforms with no registered applier).

RSS feeds by category:
  https://weworkremotely.com/categories/remote-programming-jobs.rss
  https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss
  https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss
"""
from typing import Optional
import xml.etree.ElementTree as ET

import requests
import structlog

from core.models import Job
from scrapers.base import BaseScraper

logger = structlog.get_logger()

RSS_FEEDS = [
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; auto-applier-bot/1.0)",
    "Accept": "application/rss+xml, application/xml, text/xml",
}

# Keywords to filter — must appear in title to be worth scoring
TARGET_KEYWORDS = {
    "junior", "entry", "mid", "react", "java", "spring", "python",
    "javascript", "typescript", "node", "backend", "frontend",
    "full stack", "fullstack", "developer", "engineer",
}

# Skip clearly senior/management roles
REJECT_KEYWORDS = {"senior", "staff", "principal", "lead", "manager", "director", "vp", "head of"}


class WeWorkRemotelyScraper(BaseScraper):
    platform_name = "weworkremotely"

    def fetch_jobs(self) -> list[dict]:
        all_jobs: list[dict] = []
        seen: set[str] = set()

        for feed_url in RSS_FEEDS:
            try:
                jobs = self._fetch_feed(feed_url)
                for job in jobs:
                    jid = job.get("external_id", "")
                    if jid and jid not in seen:
                        seen.add(jid)
                        all_jobs.append(job)
            except Exception as e:
                logger.error("weworkremotely.feed_error", url=feed_url, error=str(e))

        return all_jobs

    def _fetch_feed(self, feed_url: str) -> list[dict]:
        resp = requests.get(feed_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()

        root = ET.fromstring(resp.content)
        ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
        items = root.findall(".//item")

        jobs = []
        for item in items:
            data = self._parse_item(item, ns)
            if data:
                jobs.append(data)
        return jobs

    def _parse_item(self, item, ns: dict) -> Optional[dict]:
        title_el = item.find("title")
        link_el = item.find("link")
        guid_el = item.find("guid")
        desc_el = item.find("description")

        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        url = link_el.text.strip() if link_el is not None and link_el.text else ""
        guid = guid_el.text.strip() if guid_el is not None and guid_el.text else url
        description = desc_el.text.strip() if desc_el is not None and desc_el.text else ""

        if not title or not url:
            return None

        title_lower = title.lower()

        # Reject senior/management roles
        if any(kw in title_lower for kw in REJECT_KEYWORDS):
            return None

        # Must match at least one target keyword
        if not any(kw in title_lower for kw in TARGET_KEYWORDS):
            return None

        # Parse company from title (WWR format: "Company: Job Title")
        company = ""
        if ": " in title:
            parts = title.split(": ", 1)
            company = parts[0].strip()
            title = parts[1].strip()

        # Extract ID from GUID or URL
        import re
        id_match = re.search(r"/(\d+)-", guid) or re.search(r"/(\d+)-", url)
        external_id = id_match.group(1) if id_match else guid.split("/")[-1]

        return {
            "external_id": external_id,
            "title": title,
            "company": company,
            "location": "Remote (Worldwide)",
            "url": url,
            "description": description[:1000],
            "modality": "remoto",
        }

    def parse_job(self, raw: dict) -> Optional[Job]:
        if not raw.get("external_id") or not raw.get("title"):
            return None
        return Job(
            external_id=raw["external_id"],
            title=raw["title"],
            company=raw.get("company", ""),
            location=raw.get("location", "Remote (Worldwide)"),
            url=raw["url"],
            description=raw.get("description", ""),
            modality="remoto",
            status="PENDING",
        )
