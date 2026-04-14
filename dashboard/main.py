"""
Dashboard API — serves the frontend and exposes read-only endpoints
that the browser uses to render stats and application history.

Endpoints:
  GET /                        → index.html
  GET /api/health              → DB connectivity check
  GET /api/stats/today         → today's aggregated numbers
  GET /api/stats/history       → last 14 days of DailyReport rows
  GET /api/applications        → recent applications (newest first, limit 100)
  GET /api/platforms           → platform list with status + circuit breaker state
"""
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, text

# ── Bootstrap: set up sys.path so core/ imports work from Docker WORKDIR=/app
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import SessionLocal
from core.models import Application, DailyReport, Job, Platform

app = FastAPI(title="Auto Applier Dashboard", docs_url=None, redoc_url=None)

_STATIC = Path(__file__).parent / "static"
_CIRCUIT_BREAKER_PATH = Path("data/circuit_breaker.json")


# ── Response models ───────────────────────────────────────────────────────────

class StatsToday(BaseModel):
    date: str
    jobs_scraped: int
    jobs_matched: int
    jobs_applied: int
    jobs_failed: int
    match_rate: float       # matched / scraped * 100
    success_rate: float     # applied / (applied + failed) * 100


class HistoryPoint(BaseModel):
    date: str
    applied: int
    scraped: int
    matched: int
    failed: int


class ApplicationRow(BaseModel):
    id: int
    title: str
    company: Optional[str]
    platform: str
    score: Optional[int]
    status: str
    applied_at: Optional[str]
    url: str


class PlatformStatus(BaseModel):
    name: str
    is_active: bool
    daily_limit: int
    applied_today: int
    is_paused: bool
    paused_until: Optional[str]
    pause_reason: Optional[str]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(_STATIC / "index.html")


@app.get("/api/health")
def health():
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        return {"status": "ok", "db": "connected", "timestamp": datetime.utcnow().isoformat()}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DB unreachable: {e}")


@app.get("/api/stats/today", response_model=StatsToday)
def stats_today():
    db = SessionLocal()
    try:
        today = date.today()
        row = db.query(DailyReport).filter_by(report_date=today).first()

        scraped = row.jobs_scraped if row else 0
        matched = row.jobs_matched if row else 0
        applied = row.jobs_applied if row else 0
        failed  = row.jobs_failed  if row else 0

        match_rate   = round(matched / scraped * 100, 1) if scraped else 0.0
        success_rate = round(applied / (applied + failed) * 100, 1) if (applied + failed) else 0.0

        return StatsToday(
            date=today.isoformat(),
            jobs_scraped=scraped,
            jobs_matched=matched,
            jobs_applied=applied,
            jobs_failed=failed,
            match_rate=match_rate,
            success_rate=success_rate,
        )
    finally:
        db.close()


@app.get("/api/stats/history", response_model=list[HistoryPoint])
def stats_history():
    db = SessionLocal()
    try:
        cutoff = date.today() - timedelta(days=13)
        rows = (
            db.query(DailyReport)
            .filter(DailyReport.report_date >= cutoff)
            .order_by(DailyReport.report_date.asc())
            .all()
        )
        # Fill missing days with zeros so the chart always shows 14 bars
        existing = {r.report_date: r for r in rows}
        result = []
        for i in range(14):
            d = cutoff + timedelta(days=i)
            r = existing.get(d)
            result.append(HistoryPoint(
                date=d.isoformat(),
                applied=r.jobs_applied if r else 0,
                scraped=r.jobs_scraped if r else 0,
                matched=r.jobs_matched if r else 0,
                failed=r.jobs_failed  if r else 0,
            ))
        return result
    finally:
        db.close()


@app.get("/api/applications", response_model=list[ApplicationRow])
def applications(limit: int = 100):
    db = SessionLocal()
    try:
        rows = (
            db.query(Application, Job, Platform)
            .join(Job, Application.job_id == Job.id)
            .join(Platform, Job.platform_id == Platform.id)
            .order_by(Application.id.desc())
            .limit(min(limit, 200))
            .all()
        )

        # Pull scores from MatchResult — one query for all relevant job_ids
        job_ids = [job.id for _, job, _ in rows]
        scores: dict[int, int] = {}
        if job_ids:
            from core.models import MatchResult
            score_rows = (
                db.query(MatchResult.job_id, MatchResult.score)
                .filter(MatchResult.job_id.in_(job_ids))
                .order_by(MatchResult.evaluated_at.desc())
                .all()
            )
            for job_id, score in score_rows:
                if job_id not in scores:
                    scores[job_id] = score

        result = []
        for app, job, platform in rows:
            result.append(ApplicationRow(
                id=app.id,
                title=job.title,
                company=job.company,
                platform=platform.name,
                score=scores.get(job.id),
                status=app.status,
                applied_at=app.applied_at.isoformat() if app.applied_at else None,
                url=job.url,
            ))
        return result
    finally:
        db.close()


@app.get("/api/platforms", response_model=list[PlatformStatus])
def platforms():
    db = SessionLocal()
    try:
        platform_rows = db.query(Platform).order_by(Platform.name).all()

        # Applied today per platform
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        applied_today: dict[int, int] = {}
        counts = (
            db.query(Job.platform_id, func.count(Application.id))
            .join(Application, Application.job_id == Job.id)
            .filter(Application.status == "APPLIED")
            .filter(Application.applied_at >= today_start)
            .group_by(Job.platform_id)
            .all()
        )
        for pid, cnt in counts:
            applied_today[pid] = cnt

        # Circuit breaker state
        cb_state: dict = {}
        if _CIRCUIT_BREAKER_PATH.exists():
            try:
                cb_state = json.loads(_CIRCUIT_BREAKER_PATH.read_text())
            except Exception:
                pass

        now_ts = datetime.utcnow().timestamp()
        result = []
        for p in platform_rows:
            cb = cb_state.get(p.name, {})
            paused_until_ts = cb.get("paused_until", 0)
            is_paused = paused_until_ts > now_ts

            result.append(PlatformStatus(
                name=p.name,
                is_active=p.is_active,
                daily_limit=p.daily_limit,
                applied_today=applied_today.get(p.id, 0),
                is_paused=is_paused,
                paused_until=(
                    datetime.utcfromtimestamp(paused_until_ts).isoformat()
                    if is_paused else None
                ),
                pause_reason=cb.get("reason") if is_paused else None,
            ))
        return result
    finally:
        db.close()
