from abc import ABC, abstractmethod
from typing import Optional
import structlog

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from core.models import Job, Platform

logger = structlog.get_logger()


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
        """Insert job if not already present (dedup by platform_id + external_id). Returns True if inserted."""
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
