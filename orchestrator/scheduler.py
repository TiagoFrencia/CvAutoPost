"""
APScheduler wrapper.

Schedules:
  08:00 / 20:00  — full pipeline (scrape → match → apply → notify)
  Every 2 hours  — email monitor (check Gmail for job replies)
"""
import structlog
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from orchestrator.pipeline import run_pipeline
from services import notifier

logger = structlog.get_logger()

TIMEZONE = "America/Argentina/Cordoba"


def _run_email_check() -> None:
    """Wrapper so import errors (no credentials) don't crash the scheduler."""
    try:
        from services.email_monitor import run_email_check
        run_email_check()
    except Exception as e:
        logger.error("scheduler.email_check_error", error=str(e))


def start_scheduler() -> None:
    scheduler = BlockingScheduler(timezone=TIMEZONE)

    # Heartbeat — 5 min before each pipeline run (dead man's switch)
    scheduler.add_job(
        notifier.heartbeat,
        CronTrigger(hour=7, minute=55, timezone=TIMEZONE),
        id="morning_heartbeat",
        name="Morning heartbeat",
        misfire_grace_time=300,
    )
    scheduler.add_job(
        notifier.heartbeat,
        CronTrigger(hour=19, minute=55, timezone=TIMEZONE),
        id="evening_heartbeat",
        name="Evening heartbeat",
        misfire_grace_time=300,
    )

    # Pipeline — twice daily
    scheduler.add_job(
        run_pipeline,
        CronTrigger(hour=8, minute=0, timezone=TIMEZONE),
        id="morning_run",
        name="Morning pipeline",
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        run_pipeline,
        CronTrigger(hour=20, minute=0, timezone=TIMEZONE),
        id="evening_run",
        name="Evening pipeline",
        misfire_grace_time=3600,
    )

    # Email monitor — every 2 hours
    scheduler.add_job(
        _run_email_check,
        IntervalTrigger(hours=2),
        id="email_monitor",
        name="Email monitor",
        misfire_grace_time=300,
    )

    logger.info(
        "scheduler.start",
        timezone=TIMEZONE,
        jobs=["pipeline:08:00", "pipeline:20:00", "email:every_2h"],
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("scheduler.stop")
