from typing import Optional
from urllib.parse import urlencode
import re
import unicodedata

import structlog
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from playwright_stealth import stealth_sync
from sqlalchemy.orm import Session

from ai_engine.cv_loader import get_cv
from core.models import Job
from core.playwright_config import CHROMIUM_ARGS, headless
from scrapers.base import BaseScraper

logger = structlog.get_logger()

BASE_URL = "https://ar.indeed.com"
REMOTE_LOCATION = "Argentina"
LOCAL_CITY = "Río Cuarto, Córdoba"
DEFAULT_REMOTE_KEYWORDS = [
    "python",
    "java",
    "software",
    "fullstack",
    "react",
    "backend",
    "frontend",
]
DEFAULT_LOCAL_KEYWORDS = [
    "repositor",
    "cajero",
    "vendedor",
    "atencion al cliente",
    "administrativo",
]


class IndeedScraper(BaseScraper):
    platform_name = "indeed"

    def __init__(self, db: Optional[Session]):
        self.db = db
        self.platform = self._get_platform()
        self.remote_cv = get_cv("remoto")
        self.local_cv = get_cv("local")
        self.search_specs = self._build_search_specs()

    def _get_platform(self):
        if not self.db:
            class DummyPlatform:
                id = 1
                is_active = True

            return DummyPlatform()
        return super()._get_platform()

    def fetch_jobs(self) -> list[dict]:
        all_jobs: list[dict] = []
        seen: set[str] = set()

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless(), args=CHROMIUM_ARGS)
            context = browser.new_context(
                locale="es-AR",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            for spec in self.search_specs:
                page = context.new_page()
                stealth_sync(page)
                try:
                    jobs = self._fetch_query(page, spec)
                    for job in jobs:
                        jk = job.get("external_id", "")
                        if jk and jk not in seen:
                            seen.add(jk)
                            all_jobs.append(job)
                except Exception as exc:
                    logger.error(
                        "indeed.query_error",
                        keyword=spec["keyword"],
                        mode=spec["mode"],
                        url=spec["url"],
                        error=str(exc),
                    )
                finally:
                    page.close()

            browser.close()

        return all_jobs

    def _build_search_specs(self) -> list[dict]:
        remote_titles = _dedupe_non_empty(
            DEFAULT_REMOTE_KEYWORDS + (self.remote_cv.get("target_role", {}).get("titles") or [])
        )
        local_titles = _dedupe_non_empty(
            (self.local_cv.get("target_role", {}).get("titles") or []) + DEFAULT_LOCAL_KEYWORDS
        )

        specs = []
        for keyword in remote_titles[:8]:
            specs.append(
                {
                    "mode": "remote",
                    "keyword": keyword,
                    "city": "",
                    "url": _build_search_url(keyword, REMOTE_LOCATION),
                }
            )
        for keyword in local_titles[:8]:
            specs.append(
                {
                    "mode": "local",
                    "keyword": keyword,
                    "city": LOCAL_CITY,
                    "url": _build_search_url(keyword, LOCAL_CITY),
                }
            )
        logger.info("indeed.search_specs_built", count=len(specs))
        return specs

    def _fetch_query(self, page, spec: dict) -> list[dict]:
        page.goto(spec["url"], wait_until="domcontentloaded", timeout=30_000)

        try:
            page.wait_for_selector("a[data-jk]", timeout=10_000)
        except PWTimeout:
            logger.warning("indeed.no_results", keyword=spec["keyword"], mode=spec["mode"], url=spec["url"])
            return []

        raw_cards = page.evaluate(
            """
            () => Array.from(document.querySelectorAll('a[data-jk]')).map((anchor) => {
              const container = anchor.closest('tr') || anchor.closest('[data-testid="slider_item"]') || anchor.parentElement;
              return {
                external_id: anchor.getAttribute('data-jk') || '',
                href: anchor.href || '',
                title: (anchor.innerText || '').trim(),
                text: (container?.innerText || '').trim()
              };
            })
            """
        )

        jobs = []
        for raw in raw_cards[:30]:
            parsed = self._parse_card(raw, spec)
            if parsed:
                jobs.append(parsed)

        logger.info(
            "indeed.items_found",
            keyword=spec["keyword"],
            mode=spec["mode"],
            count=len(jobs),
            url=spec["url"],
        )
        return jobs

    def _parse_card(self, raw: dict, spec: dict) -> Optional[dict]:
        jk = (raw.get("external_id") or "").strip()
        title = (raw.get("title") or "").strip()
        href = (raw.get("href") or "").strip()
        card_text = (raw.get("text") or "").strip()
        if not jk or not title or not href:
            return None

        lines = [line.strip() for line in card_text.splitlines() if line.strip()]
        company = lines[1] if len(lines) > 1 else ""
        location = _extract_location(lines)
        modality = _normalize_modality(card_text)

        if spec["mode"] == "remote":
            if modality != "remoto":
                return None
        else:
            if modality == "remoto":
                return None
            if "rio-cuarto" not in _to_slug(location):
                return None

        # Use canonical viewjob URL — rc/clk tracking URLs get blocked by Cloudflare WAF
        canonical_url = f"{BASE_URL}/viewjob?jk={jk}"
        return {
            "external_id": jk,
            "title": title,
            "company": company,
            "location": location,
            "url": canonical_url,
            "description": card_text[:500],
            "modality": modality,
        }

    def parse_job(self, raw: dict) -> Optional[Job]:
        if not raw.get("external_id") or not raw.get("title"):
            return None
        return Job(
            external_id=raw["external_id"],
            title=raw["title"],
            company=raw.get("company", ""),
            location=raw.get("location", ""),
            url=raw["url"],
            description=raw.get("description", ""),
            modality=raw.get("modality", ""),
            status="PENDING",
        )


def _build_search_url(keyword: str, location: str) -> str:
    params = urlencode({"q": keyword, "l": location})
    return f"{BASE_URL}/jobs?{params}"


def _extract_location(lines: list[str]) -> str:
    for line in lines[1:8]:
        slug = _to_slug(line)
        if any(token in slug for token in ["rio-cuarto", "cordoba", "capital-federal", "buenos-aires", "desde-casa", "argentina"]):
            return line
    return ""


def _normalize_modality(text: str) -> str:
    normalized = _strip_accents(text).lower()
    if "desde casa" in normalized or "remoto" in normalized or "remote" in normalized or "teletrabajo" in normalized:
        return "remoto"
    if "hibrido" in normalized or "hybrid" in normalized:
        return "hibrido"
    return "presencial"


def _dedupe_non_empty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = " ".join(str(value or "").split()).strip()
        if not cleaned:
            continue
        key = _to_slug(cleaned)
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def _strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _to_slug(text: str) -> str:
    ascii_text = _strip_accents(text).lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
