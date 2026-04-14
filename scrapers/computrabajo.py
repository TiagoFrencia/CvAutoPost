"""
Computrabajo Argentina scraper — Playwright (JS-rendered content).

Search policy:
  - Remote jobs: nationwide (`l=Argentina`) and keep only remote jobs
  - Local jobs: specific Río Cuarto routes and keep only presencial/híbrido jobs in Río Cuarto
"""
from typing import Optional
from urllib.parse import quote_plus
import re
import unicodedata

import structlog
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from playwright_stealth import stealth_sync
from sqlalchemy.orm import Session

from ai_engine.cv_loader import get_cv
from core.models import Job
from core.playwright_config import CHROMIUM_ARGS, headless
from scrapers.base import BaseScraper, goto_with_retry, random_user_agent

logger = structlog.get_logger()

BASE_URL = "https://www.computrabajo.com.ar"
REMOTE_LOCATION = "Argentina"
LOCAL_CITY = "Río Cuarto"
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


class ComputrabajoScraper(BaseScraper):
    platform_name = "computrabajo"

    def __init__(self, db: Session):
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
                user_agent=random_user_agent(),
            )
            page = context.new_page()
            stealth_sync(page)

            for spec in self.search_specs:
                try:
                    jobs = self._fetch_page(page, spec)
                    for job in jobs:
                        job_id = job.get("external_id", "")
                        if job_id and job_id not in seen:
                            seen.add(job_id)
                            all_jobs.append(job)
                except Exception as exc:
                    logger.error(
                        "computrabajo.query_error",
                        keyword=spec["keyword"],
                        mode=spec["mode"],
                        url=spec["url"],
                        error=str(exc),
                    )

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
                    "url": _build_remote_search_url(keyword),
                }
            )
        for keyword in local_titles[:8]:
            specs.append(
                {
                    "mode": "local",
                    "keyword": keyword,
                    "city": LOCAL_CITY,
                    "url": _build_local_search_url(keyword),
                }
            )

        logger.info("computrabajo.search_specs_built", count=len(specs))
        return specs

    def _fetch_page(self, page, spec: dict) -> list[dict]:
        goto_with_retry(page, spec["url"])

        try:
            page.wait_for_selector("article.box_offer", timeout=10_000)
        except PWTimeout:
            logger.warning("computrabajo.no_results", keyword=spec["keyword"], mode=spec["mode"], url=spec["url"])
            return []

        cards = page.locator("article.box_offer").all()
        jobs = []
        for card in cards[:30]:
            try:
                data = self._parse_card(card, spec)
                if data:
                    jobs.append(data)
            except Exception as exc:
                logger.debug("computrabajo.card_error", error=str(exc))

        logger.info(
            "computrabajo.items_found",
            keyword=spec["keyword"],
            mode=spec["mode"],
            count=len(jobs),
            url=spec["url"],
        )
        return jobs

    def _parse_card(self, card, spec: dict) -> Optional[dict]:
        external_id = card.get_attribute("data-id") or ""
        if not external_id:
            return None

        try:
            title_el = card.locator("h2 a.js-o-link").first
            title = title_el.inner_text().strip()
            href = title_el.get_attribute("href") or ""
        except Exception:
            return None

        if not title:
            return None

        url = href if href.startswith("http") else "https://ar.computrabajo.com" + href

        try:
            company = card.locator("a[offer-grid-article-company-url]").first.inner_text().strip()
        except Exception:
            company = ""

        location = _extract_location(card)
        card_text = card.inner_text()
        modality = _extract_modality(card, card_text)

        if spec["mode"] == "remote":
            if modality != "remoto":
                return None
        else:
            if modality == "remoto":
                return None
            if _to_slug(spec["city"]) not in _to_slug(location):
                return None

        return {
            "external_id": external_id,
            "title": title,
            "company": company,
            "location": location,
            "url": url,
            "description": card_text[:500],
            "modality": modality,
        }

    def parse_job(self, raw: dict) -> Optional[Job]:
        if not raw.get("external_id") or not raw.get("title") or not raw.get("url"):
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


def _build_remote_search_url(keyword: str) -> str:
    return f"https://ar.computrabajo.com/empleos-de-{_to_slug(keyword)}?l={quote_plus(REMOTE_LOCATION)}"


def _build_local_search_url(keyword: str) -> str:
    return f"https://ar.computrabajo.com/trabajo-de-{_to_slug(keyword)}-en-{_to_slug(LOCAL_CITY)}"


def _normalize_modality(text: str) -> str:
    normalized = _strip_accents(text).lower()
    if "presencial y remoto" in normalized or "hibrido" in normalized:
        return "hibrido"
    if " remoto" in f" {normalized}" or "teletrabajo" in normalized:
        return "remoto"
    return "presencial"


def _extract_location(card) -> str:
    try:
        blocks = [text.strip() for text in card.locator("p.fs16.fc_base.mt5").all_inner_texts() if text.strip()]
        if len(blocks) >= 2:
            return blocks[1]
    except Exception:
        pass
    try:
        spans = [text.strip() for text in card.locator("span.mr10").all_inner_texts() if text.strip()]
        for text in spans:
            slug = _to_slug(text)
            if any(token in slug for token in ["rio-cuarto", "capital-federal", "buenos-aires", "cordoba", "mendoza", "retiro", "recoleta"]):
                return text
    except Exception:
        pass
    return ""


def _extract_modality(card, fallback_text: str) -> str:
    try:
        spans = [text.strip() for text in card.locator("span.mr10").all_inner_texts() if text.strip()]
        for text in spans:
            normalized = _strip_accents(text).lower()
            if "remoto" in normalized or "hibrido" in normalized or "presencial" in normalized:
                return _normalize_modality(text)
    except Exception:
        pass
    return _normalize_modality(fallback_text)


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
