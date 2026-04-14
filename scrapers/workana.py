"""
Workana scraper — Playwright. Freelance/contract platform.
No login needed for browsing job listings.
Targets: IT/programming projects in Spanish (LATAM market).
"""
from typing import Optional
from urllib.parse import urlencode
import re

import structlog
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from playwright_stealth import stealth_sync

from core.models import Job
from core.playwright_config import CHROMIUM_ARGS, headless
from scrapers.base import BaseScraper

logger = structlog.get_logger()

BASE_URL = "https://www.workana.com"

# Category pages for IT/programming
SEARCH_URLS = [
    f"{BASE_URL}/jobs?language=es&category=it-programming&subcategory=web-mobile-development",
    f"{BASE_URL}/jobs?language=es&category=it-programming&subcategory=software-programming",
    f"{BASE_URL}/jobs?language=es&category=it-programming&subcategory=web-design",
]

TARGET_KEYWORDS = {
    "react", "java", "spring", "python", "javascript", "typescript",
    "node", "backend", "frontend", "full stack", "fullstack",
    "desarrollador", "programador", "developer",
}


class WorkanaScraper(BaseScraper):
    platform_name = "workana"

    def fetch_jobs(self) -> list[dict]:
        all_jobs: list[dict] = []
        seen: set[str] = set()

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless(), args=CHROMIUM_ARGS)
            context = browser.new_context(locale="es-AR")
            page = context.new_page()
            stealth_sync(page)

            for url in SEARCH_URLS:
                try:
                    jobs = self._fetch_page(page, url)
                    for job in jobs:
                        jid = job.get("external_id", "")
                        if jid and jid not in seen:
                            seen.add(jid)
                            all_jobs.append(job)
                except Exception as e:
                    logger.error("workana.page_error", url=url, error=str(e))

            browser.close()

        return all_jobs

    def _fetch_page(self, page, url: str) -> list[dict]:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        try:
            page.wait_for_selector(".project-item, [class*='project'], article.job", timeout=10_000)
        except PWTimeout:
            logger.warning("workana.no_results", url=url)
            return []

        cards = page.locator(".project-item, [class*='project-item'], article.job").all()
        jobs = []
        for card in cards[:25]:
            try:
                data = self._parse_card(card)
                if data:
                    jobs.append(data)
            except Exception as e:
                logger.debug("workana.card_error", error=str(e))
        return jobs

    def _parse_card(self, card) -> Optional[dict]:
        try:
            link = card.locator("h2 a, h3 a, a.title, a[href*='/job/']").first
            title = link.inner_text().strip()
            href = link.get_attribute("href") or ""
        except Exception:
            return None

        if not title or not href:
            return None

        # Filter by target keywords
        title_lower = title.lower()
        if not any(kw in title_lower for kw in TARGET_KEYWORDS):
            return None

        url = href if href.startswith("http") else BASE_URL + href

        id_match = re.search(r"/job/(\d+)", url)
        external_id = id_match.group(1) if id_match else url.split("-")[-1]

        try:
            budget = card.locator(".budget, .price, [class*='budget']").first.inner_text().strip()
        except Exception:
            budget = ""

        try:
            description = card.locator(".project-description, p.description, .excerpt").first.inner_text().strip()
        except Exception:
            description = ""

        return {
            "external_id": external_id,
            "title": title,
            "company": "",  # Workana shows client name only after login
            "location": "Remoto (LATAM)",
            "url": url,
            "description": description[:500],
            "salary_range": budget,
            "modality": "remoto",
        }

    def parse_job(self, raw: dict) -> Optional[Job]:
        if not raw.get("external_id") or not raw.get("title"):
            return None
        return Job(
            external_id=raw["external_id"],
            title=raw["title"],
            company=raw.get("company", ""),
            location=raw.get("location", "Remoto (LATAM)"),
            url=raw["url"],
            description=raw.get("description", ""),
            salary_range=raw.get("salary_range"),
            modality="remoto",
            status="PENDING",
        )
