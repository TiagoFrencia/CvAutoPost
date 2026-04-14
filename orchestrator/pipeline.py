"""
Main pipeline: scrape → match (AI) → apply → notify.
"""
from datetime import date

import structlog

from core.database import SessionLocal
from core.models import Application, DailyReport, Platform, Job
from core.enums import ApplicationStatus, JobStatus
from orchestrator.lock_manager import pipeline_lock
from scrapers.getonboard import GetOnBoardScraper
from scrapers.remoteok import RemoteOKScraper
from services import notifier

logger = structlog.get_logger()

# Playwright-based scrapers for local platforms (only run if platform is active)
LOCAL_SCRAPERS = [
    ("computrabajo", "scrapers.computrabajo", "ComputrabajoScraper"),
    ("indeed",       "scrapers.indeed",       "IndeedScraper"),
    ("zonajobs",     "scrapers.zonajobs",     "ZonaJobsScraper"),
    ("bumeran",      "scrapers.bumeran",      "BumeranScraper"),
]

# Scrapers for remote platforms (no cookies needed)
REMOTE_SCRAPERS = [
    ("weworkremotely", "scrapers.weworkremotely", "WeWorkRemotelyScraper"),
    ("workana",        "scrapers.workana",        "WorkanaScraper"),
]


def run_pipeline() -> None:
    with pipeline_lock():
        logger.info("pipeline.start")
        db = SessionLocal()
        try:
            stats = _run_scrape_phase(db)
            match_stats = _run_match_phase(db)
            stats.update(match_stats)
            apply_stats = _run_apply_phase(db)
            stats.update(apply_stats)
            _save_daily_report(db, stats)
            notifier.daily_report(stats)
            logger.info("pipeline.done", **stats)
        finally:
            db.close()


# ── Scrape phase ──────────────────────────────────────────────────────────────

def _run_scrape_phase(db) -> dict:
    stats = {"jobs_scraped": 0, "jobs_matched": 0, "jobs_applied": 0, "jobs_failed": 0, "api_cost_usd": 0.0}

    # API/RSS scrapers — always run
    for ScraperClass in [GetOnBoardScraper, RemoteOKScraper]:
        stats["jobs_scraped"] += _run_scraper(ScraperClass, db)

    # Playwright remote scrapers — run if platform is active
    for platform_name, module_path, class_name in REMOTE_SCRAPERS:
        if _is_platform_active(db, platform_name):
            ScraperClass = _import_scraper(module_path, class_name)
            if ScraperClass:
                stats["jobs_scraped"] += _run_scraper(ScraperClass, db)

    # Playwright local scrapers — run if platform is active
    for platform_name, module_path, class_name in LOCAL_SCRAPERS:
        if _is_platform_active(db, platform_name):
            ScraperClass = _import_scraper(module_path, class_name)
            if ScraperClass:
                stats["jobs_scraped"] += _run_scraper(ScraperClass, db)

    # LinkedIn — only if cookies are present and valid
    if _is_platform_active(db, "linkedin"):
        try:
            from scrapers.linkedin import LinkedInScraper
            from services.session_manager import SessionManager
            sm = SessionManager()
            if sm.has_cookies("linkedin"):
                is_valid, _ = sm.check_expiry("linkedin")
                if is_valid:
                    stats["jobs_scraped"] += LinkedInScraper(db).run()
        except Exception as e:
            logger.error("pipeline.linkedin_scrape_error", error=str(e))

    return stats


def _run_scraper(ScraperClass, db) -> int:
    try:
        return ScraperClass(db).run()
    except Exception as e:
        logger.error("pipeline.scraper_error", scraper=ScraperClass.__name__, error=str(e))
        return 0


def _is_platform_active(db, platform_name: str) -> bool:
    p = db.query(Platform).filter_by(name=platform_name).first()
    return bool(p and p.is_active)


def _import_scraper(module_path: str, class_name: str):
    try:
        import importlib
        module = importlib.import_module(module_path)
        return getattr(module, class_name)
    except Exception as e:
        logger.error("pipeline.scraper_import_error", module=module_path, error=str(e))
        return None


# ── Match phase ───────────────────────────────────────────────────────────────

def _run_match_phase(db) -> dict:
    from ai_engine.job_matcher import JobMatcher

    match_stats = {"jobs_matched": 0}
    platform_by_name = {p.name: p for p in db.query(Platform).all()}

    # Platforms matched with a single fixed CV profile
    SINGLE_PROFILE = {
        "getonboard":     "remoto",
        "remoteok":       "remoto",
        "weworkremotely": "remoto",
        "workana":        "remoto",
    }

    # Platforms split by modality: remoto → cv_remoto, presencial/híbrido → cv_local
    DUAL_PROFILE = ["linkedin", "zonajobs", "bumeran", "computrabajo", "indeed"]

    for name, cv_profile in SINGLE_PROFILE.items():
        p = platform_by_name.get(name)
        if not p:
            continue
        try:
            result = JobMatcher(db, cv_profile_name=cv_profile).run_batch(platform_ids=[p.id])
            match_stats["jobs_matched"] += result.get("scored", 0)
        except Exception as e:
            logger.error("pipeline.match_error", platform=name, cv_profile=cv_profile, error=str(e))

    for name in DUAL_PROFILE:
        p = platform_by_name.get(name)
        if not p:
            continue
        for cv_profile, modalities, include_null in [
            ("remoto", ["remoto"],                    False),
            ("local",  ["presencial", "hibrido", ""], True),
        ]:
            try:
                result = JobMatcher(db, cv_profile_name=cv_profile).run_batch(
                    platform_ids=[p.id],
                    modalities=modalities,
                    include_null_modality=include_null,
                )
                match_stats["jobs_matched"] += result.get("scored", 0)
            except Exception as e:
                logger.error("pipeline.match_error", platform=name, cv_profile=cv_profile, error=str(e))

    return match_stats


# ── Apply phase ───────────────────────────────────────────────────────────────

def _run_apply_phase(db) -> dict:
    from services.applier import run_apply_queue
    try:
        return run_apply_queue(db)
    except Exception as e:
        logger.error("pipeline.apply_error", error=str(e))
        return {"applied": 0, "failed": 0, "review_form": 0}


# ── Report ────────────────────────────────────────────────────────────────────

def _build_platform_breakdown(db) -> dict:
    """Build per-platform application counts for today's report."""
    from sqlalchemy import func
    rows = (
        db.query(Platform.name, Application.status, func.count(Application.id))
        .join(Job, Job.id == Application.job_id)
        .join(Platform, Platform.id == Job.platform_id)
        .group_by(Platform.name, Application.status)
        .all()
    )
    breakdown: dict = {}
    for platform_name, status, count in rows:
        if platform_name not in breakdown:
            breakdown[platform_name] = {}
        breakdown[platform_name][status] = count
    return breakdown


def _save_daily_report(db, stats: dict) -> None:
    today = date.today()
    breakdown = _build_platform_breakdown(db)
    report = db.query(DailyReport).filter_by(report_date=today).first()
    if report:
        report.jobs_scraped += stats.get("jobs_scraped", 0)
        report.jobs_matched += stats.get("jobs_matched", 0)
        report.jobs_applied += stats.get("applied", 0)
        report.jobs_failed += stats.get("failed", 0)
        report.platform_breakdown = breakdown
    else:
        report = DailyReport(
            report_date=today,
            jobs_scraped=stats.get("jobs_scraped", 0),
            jobs_matched=stats.get("jobs_matched", 0),
            jobs_applied=stats.get("applied", 0),
            jobs_failed=stats.get("failed", 0),
            platform_breakdown=breakdown,
        )
        db.add(report)
    db.commit()
