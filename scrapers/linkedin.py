import re
import unicodedata
from typing import Optional
from urllib.parse import urlencode

import structlog
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from playwright_stealth import stealth_sync
from sqlalchemy.orm import Session

from ai_engine.cv_loader import get_cv
from core.models import Job, Platform
from core.playwright_config import CHROMIUM_ARGS, headless

logger = structlog.get_logger()

BASE_SEARCH_URL = "https://www.linkedin.com/jobs/search/"
REMOTE_LOCATION = "Argentina"
DEFAULT_REMOTE_KEYWORDS = [
    "python",
    "java",
    "software",
    "full stack",
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


class LinkedInScraper:
    """
    Search-only LinkedIn scraper using public job result pages.
    Applying still relies on the logged-in Nodriver flow in services/appliers/linkedin.py.
    """
    platform_name = "linkedin"

    def __init__(self, db: Session):
        self.db = db
        self.platform = self._get_platform()
        self.remote_cv = get_cv("remoto")
        self.local_cv = get_cv("local")
        self.search_specs = _build_search_specs(self.remote_cv, self.local_cv)

    def _get_platform(self) -> Platform:
        p = self.db.query(Platform).filter_by(name=self.platform_name).first()
        if not p:
            raise ValueError("Platform 'linkedin' not in DB. Run: python main.py seed")
        if not p.is_active:
            raise ValueError("LinkedIn platform is disabled.")
        return p

    def run(self) -> int:
        logger.info("linkedin.scraper_start", queries=len(self.search_specs))
        raw_jobs = self.fetch_jobs()
        new_count = 0
        for raw in raw_jobs:
            job = self._parse_job(raw)
            if job and self._save_job(job):
                new_count += 1
        logger.info("linkedin.scraper_done", new_jobs=new_count)
        return new_count

    def fetch_jobs(self) -> list[dict]:
        all_jobs: list[dict] = []
        seen: set[str] = set()

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless(), args=CHROMIUM_ARGS)
            context = browser.new_context(locale="es-AR")

            for spec in self.search_specs:
                page = context.new_page()
                stealth_sync(page)
                try:
                    jobs = self._fetch_query(page, spec)
                    for job in jobs:
                        external_id = job.get("external_id", "")
                        if external_id and external_id not in seen:
                            seen.add(external_id)
                            all_jobs.append(job)
                except Exception as exc:
                    logger.error(
                        "linkedin.query_error",
                        keyword=spec["keyword"],
                        mode=spec["mode"],
                        location=spec["location"],
                        error=str(exc),
                    )
                finally:
                    page.close()

            browser.close()

        return all_jobs

    def _fetch_query(self, page, spec: dict) -> list[dict]:
        url = _build_search_url(spec)
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        try:
            page.wait_for_selector("ul.jobs-search__results-list li, .base-search-card", timeout=10_000)
        except PWTimeout:
            logger.warning("linkedin.no_results", keyword=spec["keyword"], mode=spec["mode"], url=url)
            return []

        raw_cards = page.evaluate(
            """
            () => Array.from(document.querySelectorAll('ul.jobs-search__results-list li, .base-search-card'))
              .map((card) => {
                const link = card.querySelector('a[href*="/jobs/view/"]');
                return {
                  href: link ? link.href : '',
                  text: (card.innerText || '').trim()
                };
              })
              .filter(card => card.href && card.text)
            """
        )

        jobs = []
        for raw in raw_cards[:25]:
            parsed = _parse_card(raw, spec)
            if parsed:
                jobs.append(parsed)

        logger.info(
            "linkedin.items_found",
            keyword=spec["keyword"],
            mode=spec["mode"],
            count=len(jobs),
            url=url,
        )
        return jobs

    def _parse_job(self, raw: dict) -> Optional[Job]:
        external_id = raw.get("external_id", "")
        title = raw.get("title", "")
        url = raw.get("url", "")
        if not external_id or not title or not url:
            return None
        return Job(
            platform_id=self.platform.id,
            external_id=external_id,
            title=title,
            company=raw.get("company", ""),
            location=raw.get("location", ""),
            url=url,
            description=raw.get("description", ""),
            modality=raw.get("modality", ""),
            status="PENDING",
        )

    def _save_job(self, job: Job) -> bool:
        from sqlalchemy.exc import IntegrityError

        existing = self.db.query(Job).filter_by(
            platform_id=job.platform_id, external_id=job.external_id
        ).first()
        if existing:
            return False
        try:
            self.db.add(job)
            self.db.commit()
            return True
        except IntegrityError:
            self.db.rollback()
            return False


def _build_search_specs(remote_cv: dict, local_cv: dict) -> list[dict]:
    remote_titles = _dedupe_non_empty(
        DEFAULT_REMOTE_KEYWORDS + (remote_cv.get("target_role", {}).get("titles") or [])
    )
    local_titles = _dedupe_non_empty(
        (local_cv.get("target_role", {}).get("titles") or []) + DEFAULT_LOCAL_KEYWORDS
    )
    city = local_cv.get("personal_info", {}).get("location", {}).get("city", "Río Cuarto")
    province = local_cv.get("personal_info", {}).get("location", {}).get("province", "Córdoba")
    country = local_cv.get("personal_info", {}).get("location", {}).get("country", "Argentina")
    local_location = ", ".join(part for part in [city, province, country] if part)

    specs = []
    for keyword in remote_titles[:8]:
        specs.append(
            {
                "mode": "remote",
                "keyword": keyword,
                "location": REMOTE_LOCATION,
                "work_type": "2",
            }
        )
    for keyword in local_titles[:8]:
        specs.append(
            {
                "mode": "local",
                "keyword": keyword,
                "location": local_location,
                "city": city,
                "work_type": None,
            }
        )
    return specs


def _build_search_url(spec: dict) -> str:
    params = {
        "keywords": spec["keyword"],
        "location": spec["location"],
        "f_AL": "true",
        "sortBy": "DD",
    }
    if spec.get("work_type"):
        params["f_WT"] = spec["work_type"]
    return BASE_SEARCH_URL + "?" + urlencode(params)


def _parse_card(raw: dict, spec: dict) -> Optional[dict]:
    href = (raw.get("href") or "").strip()
    text = (raw.get("text") or "").strip()
    if not href or not text:
        return None

    # Extract numeric LinkedIn job ID from URL path (e.g. .../jobs/view/title-slug-3987654321/)
    path_segment = href.split("?")[0].rstrip("/").split("/")[-1]
    id_match = re.search(r"(\d{7,})", path_segment)
    if not id_match:
        return None
    external_id = id_match.group(1)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    title = lines[0] if lines else ""
    company = lines[2][:255] if len(lines) > 2 else ""
    location = _extract_location(lines)
    modality = _normalize_modality(text)

    if not title:
        return None

    if spec["mode"] == "remote":
        if modality != "remoto":
            return None
    else:
        if modality == "remoto":
            return None
        if _to_slug(spec.get("city", "")) not in _to_slug(location):
            return None

    # Use canonical URL without tracking params — cleaner and avoids WAF blocks
    canonical_url = f"https://www.linkedin.com/jobs/view/{external_id}/"
    return {
        "external_id": external_id,
        "title": title,
        "company": company,
        "location": location,
        "url": canonical_url,
        "description": text[:500],
        "modality": modality,
    }


def _extract_location(lines: list[str]) -> str:
    for line in lines[1:8]:
        slug = _to_slug(line)
        if any(token in slug for token in ["rio-cuarto", "cordoba", "argentina", "buenos-aires", "remote", "remoto"]):
            return line
    return ""


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


def _normalize_modality(text: str) -> str:
    normalized = _strip_accents(text).lower()
    if "remote" in normalized or "remoto" in normalized:
        return "remoto"
    if "hybrid" in normalized or "hibrido" in normalized:
        return "hibrido"
    return "presencial"
