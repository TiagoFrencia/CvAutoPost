"""
Entry point. Usage:
  python main.py run       — run pipeline once immediately
  python main.py schedule  — start scheduler (runs 08:00 and 20:00 daily)
  python main.py scrape    — run scrapers only (no AI matching, no apply)
  python main.py seed      — insert initial platforms and CV profiles into DB
"""
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import structlog
import logging

# ── Logging setup ───────────────────────────────────────────────────────────
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.BoundLogger,
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)
logging.basicConfig(level=logging.WARNING)

logger = structlog.get_logger()


# ── Health server ─────────────────────────────────────────────────────────────

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass  # silence HTTP access logs


def _start_health_server(port: int = 8081) -> None:
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True, name="health-server")
    t.start()
    logger.info("health_server.started", port=port)


def cmd_seed():
    from core.database import SessionLocal, engine
    from core.models import Base, Platform, CVProfile
    from ai_engine.cv_loader import get_cv

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    # ── Platforms ────────────────────────────────────────────────────────────
    platforms = [
        # Perfil A — Remoto
        dict(name="getonboard",   base_url="https://www.getonboard.com", auth_method="api",     daily_limit=50, is_active=False),  # API dead as of 2026
        dict(name="remoteok",     base_url="https://remoteok.com",       auth_method="api",     daily_limit=50),
        dict(name="linkedin",     base_url="https://www.linkedin.com",   auth_method="cookies", daily_limit=5),
        dict(name="workana",        base_url="https://www.workana.com",        auth_method="cookies", daily_limit=15, is_active=False),  # cuenta sin verificar
        dict(name="weworkremotely", base_url="https://weworkremotely.com",    auth_method="none",    daily_limit=50, is_active=True),
        # Perfil B — Local
        dict(name="computrabajo", base_url="https://www.computrabajo.com.ar", auth_method="cookies", daily_limit=15, is_active=True),
        dict(name="indeed",       base_url="https://ar.indeed.com",           auth_method="cookies", daily_limit=15, is_active=True),
        dict(name="zonajobs",     base_url="https://www.zonajobs.com.ar",     auth_method="cookies", daily_limit=15, is_active=True),
        dict(name="bumeran",      base_url="https://www.bumeran.com.ar",      auth_method="cookies", daily_limit=15, is_active=True),
    ]

    for p in platforms:
        existing = db.query(Platform).filter_by(name=p["name"]).first()
        if not existing:
            db.add(Platform(**p))
            logger.info("seed.platform_added", name=p["name"])
        else:
            logger.info("seed.platform_exists", name=p["name"])

    # ── CV Profiles ──────────────────────────────────────────────────────────
    cv_configs = [
        dict(
            name="remoto",
            json_path="data/cvs/cv_remoto.json",
            pdf_path="data/cvs/cv_programacion.pdf",
        ),
        dict(
            name="local",
            json_path="data/cvs/cv_local.json",
            pdf_path="data/cvs/cv_local.pdf",
        ),
    ]

    for cfg in cv_configs:
        existing = db.query(CVProfile).filter_by(name=cfg["name"]).first()
        if not existing:
            cv_data = get_cv(cfg["name"])
            profile = CVProfile(
                name=cfg["name"],
                json_path=cfg["json_path"],
                pdf_path=cfg["pdf_path"],
                structured_data=cv_data,
                target_keywords=cv_data.get("target_role", {}).get("titles", []),
                filters=cv_data.get("target_role", {}),
            )
            db.add(profile)
            logger.info("seed.cv_profile_added", name=cfg["name"])
        else:
            logger.info("seed.cv_profile_exists", name=cfg["name"])

    db.commit()
    db.close()
    logger.info("seed.done")


def cmd_scrape():
    from ai_engine.cv_validator import validate_all
    from core.database import SessionLocal
    from scrapers.getonboard import GetOnBoardScraper
    from scrapers.remoteok import RemoteOKScraper

    validate_all()
    db = SessionLocal()
    try:
        total = 0
        for ScraperClass in [GetOnBoardScraper, RemoteOKScraper]:
            try:
                count = ScraperClass(db).run()
                total += count
            except Exception as e:
                logger.error("scrape.error", scraper=ScraperClass.__name__, error=str(e))
        logger.info("scrape.total_new_jobs", count=total)
    finally:
        db.close()


def cmd_run():
    from ai_engine.cv_validator import validate_all
    validate_all()
    _start_health_server()
    from services.telegram_bot import start_polling_thread
    start_polling_thread()
    from orchestrator.pipeline import run_pipeline
    run_pipeline()


def cmd_schedule():
    from ai_engine.cv_validator import validate_all
    validate_all()
    _start_health_server()
    from services.telegram_bot import start_polling_thread
    start_polling_thread()
    from orchestrator.scheduler import start_scheduler
    start_scheduler()


COMMANDS = {
    "seed": cmd_seed,
    "scrape": cmd_scrape,
    "run": cmd_run,
    "schedule": cmd_schedule,
}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "schedule"
    if cmd not in COMMANDS:
        print(f"Unknown command: {cmd}")
        print(f"Available: {', '.join(COMMANDS)}")
        sys.exit(1)
    COMMANDS[cmd]()
