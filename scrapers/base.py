import hashlib
import re
import time
import unicodedata
from abc import ABC, abstractmethod
from typing import Optional

import structlog
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from core.models import Job, Platform

logger = structlog.get_logger()


# ── User-Agent rotation ───────────────────────────────────────────────────────

def random_user_agent() -> str:
    """Return a random Chrome user agent. Falls back to a static string on error."""
    try:
        from fake_useragent import UserAgent
        return UserAgent().chrome
    except Exception:
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )


# ── Cross-platform deduplication fingerprint ─────────────────────────────────

def job_fingerprint(title: str, company: str, location: str = "") -> Optional[str]:
    """
    MD5 fingerprint of normalised title+company+location.
    Returns None when company is blank (no meaningful cross-platform signal).
    """
    if not title or not company:
        return None

    def _norm(s: str) -> str:
        s = unicodedata.normalize("NFKD", (s or "").lower())
        s = "".join(c for c in s if not unicodedata.combining(c))
        return re.sub(r"\W+", " ", s).strip()

    text = f"{_norm(title)} {_norm(company)} {_norm(location)}"
    return hashlib.md5(text.encode()).hexdigest()


# ── Playwright goto with retry ────────────────────────────────────────────────

def goto_with_retry(page, url: str, attempts: int = 3, timeout: int = 30_000) -> None:
    """Navigate to url; retries up to `attempts` times on PWTimeout with exponential backoff."""
    from playwright.sync_api import TimeoutError as PWTimeout

    for i in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            return
        except PWTimeout:
            if i == attempts - 1:
                raise
            wait = 2 ** i
            logger.warning("scraper.goto_retry", url=url, attempt=i + 1, wait_sec=wait)
            time.sleep(wait)


# ── Base scraper ──────────────────────────────────────────────────────────────

class BaseScraper(ABC):
    platform_name: str

    def __init__(self, db: Session):
        self.db = db
        self.platform = self._get_platform()

    def _get_platform(self) -> Platform:
        platform = self.db.query(Platform).filter_by(name=self.platform_name).first()
        if not platform:
            raise ValueError(f"Platform '{self.platform_name}' not found in DB. Run: python main.py seed")
        if not platform.is_active:
            raise ValueError(f"Platform '{self.platform_name}' is disabled.")
        return platform

    @abstractmethod
    def fetch_jobs(self) -> list[dict]:
        """Fetch raw job data from the platform. Returns list of raw dicts."""

    @abstractmethod
    def parse_job(self, raw: dict) -> Optional[Job]:
        """Parse a raw dict into a Job ORM instance (not yet saved). Return None to skip."""

    def run(self) -> int:
        """Full scrape cycle: fetch → parse → deduplicate → save. Returns count of new jobs."""
        log = logger.bind(platform=self.platform_name)
        log.info("scraper.start")

        raw_jobs = self.fetch_jobs()
        log.info("scraper.fetched", count=len(raw_jobs))

        new_count = 0
        for raw in raw_jobs:
            job = self.parse_job(raw)
            if job is None:
                continue
            job.platform_id = self.platform.id
            saved = self._save_job(job)
            if saved:
                new_count += 1

        log.info("scraper.done", new_jobs=new_count)
        return new_count

    def _save_job(self, job: Job) -> bool:
        """
        Insert job if not already present.
        Deduplicates by:
          1. Cross-platform fingerprint (title + company + location)
          2. Same-platform external_id
        Returns True if inserted.
        """
        # Cross-platform deduplication
        fp = job_fingerprint(job.title, job.company or "", job.location or "")
        if fp:
            job.fingerprint = fp
            if self.db.query(Job).filter(Job.fingerprint == fp).first():
                return False

        # Same-platform deduplication
        existing = (
            self.db.query(Job)
            .filter_by(platform_id=job.platform_id, external_id=job.external_id)
            .first()
        )
        if existing:
            return False

        try:
            self.db.add(job)
            self.db.commit()
            return True
        except IntegrityError:
            self.db.rollback()
            return False
