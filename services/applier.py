"""
Base applier framework.

Provides:
  - ApplicationResult       dataclass returned by every applier
  - CaptchaDetected         exception that triggers the circuit breaker
  - AuthExpired             exception for expired cookies
  - CircuitBreaker          per-platform pause state stored in data/circuit_breaker.json
  - BaseApplier             ABC all platform appliers inherit from
  - run_apply_queue(db)     main entry point: priority queue → dispatch → update DB
"""
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import structlog
from sqlalchemy.orm import Session

from core.config import settings
from core.enums import ApplicationStatus, JobStatus
from core.models import Application, CVProfile, Job, Platform
from services import notifier
from services.session_manager import SessionManager

logger = structlog.get_logger()

CIRCUIT_BREAKER_PATH = settings.data_dir / "circuit_breaker.json"
MAX_RETRIES = 2


# ── Exceptions ────────────────────────────────────────────────────────────────

class CaptchaDetected(Exception):
    pass


class AuthExpired(Exception):
    pass


# ── Result ────────────────────────────────────────────────────────────────────

@dataclass
class ApplicationResult:
    success: bool
    status: str                              # ApplicationStatus value
    error: Optional[str] = None
    screenshot_path: Optional[str] = None
    orphan_questions: Optional[list] = None
    last_step: Optional[dict] = None


# ── Circuit breaker ───────────────────────────────────────────────────────────

class CircuitBreaker:
    """
    Persists pause state in data/circuit_breaker.json.
    Format: {"platform_name": {"paused_until": <unix_ts>, "reason": "..."}}
    """

    def __init__(self):
        self._path = CIRCUIT_BREAKER_PATH
        self._state: dict = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text())
            except Exception:
                return {}
        return {}

    def _save(self):
        self._path.write_text(json.dumps(self._state, indent=2))

    def is_paused(self, platform: str) -> bool:
        entry = self._state.get(platform)
        if not entry:
            return False
        paused_until = entry.get("paused_until", 0)
        if datetime.utcnow().timestamp() < paused_until:
            logger.info(
                "circuit_breaker.paused",
                platform=platform,
                until=datetime.utcfromtimestamp(paused_until).isoformat(),
                reason=entry.get("reason"),
            )
            return True
        # Pause expired — clear it
        del self._state[platform]
        self._save()
        return False

    def pause(self, platform: str, reason: str, hours: int = 24):
        until = datetime.utcnow() + timedelta(hours=hours)
        self._state[platform] = {
            "paused_until": until.timestamp(),
            "reason": reason,
        }
        self._save()
        logger.warning("circuit_breaker.triggered", platform=platform, hours=hours, reason=reason)

    def reset(self, platform: str):
        self._state.pop(platform, None)
        self._save()


# ── Base applier ──────────────────────────────────────────────────────────────

class BaseApplier(ABC):
    platform_name: str
    cv_profile_name: str   # 'remoto' | 'local' — determines which PDF to attach

    def __init__(self, db: Session):
        self.db = db
        self.platform = self._get_platform()
        self.circuit_breaker = CircuitBreaker()
        self.session_manager = SessionManager()

    def _get_platform(self) -> Platform:
        p = self.db.query(Platform).filter_by(name=self.platform_name).first()
        if not p:
            raise ValueError(f"Platform '{self.platform_name}' not in DB. Run: python main.py seed")
        return p

    def apply(self, application: Application) -> ApplicationResult:
        """Public entry point. Handles circuit breaker, retries, and DB updates."""
        job = self.db.query(Job).filter_by(id=application.job_id).first()
        log = logger.bind(platform=self.platform_name, job_id=job.id, app_id=application.id)

        if self.circuit_breaker.is_paused(self.platform_name):
            log.info("applier.skipped_circuit_open")
            return ApplicationResult(False, ApplicationStatus.FAILED.value, "Platform paused")

        log.info("applier.start", title=job.title, retry=application.retry_count)

        try:
            result = self._do_apply(application, job)
        except CaptchaDetected as e:
            self.circuit_breaker.pause(self.platform_name, reason="CAPTCHA detected", hours=24)
            notifier.alert(f"CAPTCHA en {self.platform_name} — plataforma pausada 24h.\nOferta: {job.title}")
            screenshot = _take_error_screenshot(None, f"captcha_{self.platform_name}")
            result = ApplicationResult(
                False, ApplicationStatus.FAILED.value,
                str(e), screenshot_path=screenshot,
            )
        except AuthExpired:
            log.warning("applier.auth_expired_attempting_auto_login")
            renewed = self.session_manager.auto_login(self.platform_name)
            if renewed:
                log.info("applier.auth_renewed_retrying")
                try:
                    result = self._do_apply(application, job)
                except Exception as retry_exc:
                    log.error("applier.retry_after_renewal_failed", error=str(retry_exc))
                    result = ApplicationResult(False, ApplicationStatus.FAILED.value, str(retry_exc))
            else:
                notifier.alert(
                    f"Cookies expiradas en {self.platform_name} y no se pudo renovar automáticamente. "
                    f"Ejecutá: python login_helper.py --platform {self.platform_name}"
                )
                self.platform.is_active = False
                self.db.commit()
                result = ApplicationResult(False, ApplicationStatus.FAILED.value, "Auth expired")
        except Exception as e:
            log.error("applier.unexpected_error", error=str(e))
            result = ApplicationResult(False, ApplicationStatus.FAILED.value, str(e))

        self._persist_result(application, result)
        log.info("applier.done", success=result.success, status=result.status)
        return result

    @abstractmethod
    def _do_apply(self, application: Application, job: Job) -> ApplicationResult:
        """Platform-specific application logic. Raises CaptchaDetected / AuthExpired when needed."""

    def _save_checkpoint(self, application: Application, step: int, step_name: str, extra: dict = None):
        application.last_successful_step = {
            "step": step,
            "step_name": step_name,
            "extra": extra or {},
            "timestamp": datetime.utcnow().isoformat(),
        }
        self.db.commit()

    def _get_resume_step(self, application: Application) -> int:
        """Return the step number to resume from (0 = start from beginning)."""
        checkpoint = application.last_successful_step
        if checkpoint and isinstance(checkpoint, dict):
            return checkpoint.get("step", 0)
        return 0

    def _persist_result(self, application: Application, result: ApplicationResult):
        application.status = result.status
        if result.error:
            application.error_log = result.error
        if result.screenshot_path:
            application.screenshot_path = result.screenshot_path
        if result.orphan_questions:
            application.orphan_questions = result.orphan_questions
        if result.last_step:
            application.last_successful_step = result.last_step
        if not result.success and result.status == ApplicationStatus.FAILED.value:
            application.retry_count = (application.retry_count or 0) + 1
            application.last_successful_step = None  # reset checkpoint so next retry starts fresh
        if result.success:
            application.applied_at = datetime.utcnow()
        self.db.commit()

    def _get_application_cv_profile_name(self, application: Application) -> str:
        cv_profile = self.db.query(CVProfile).filter_by(id=application.cv_profile_id).first()
        if cv_profile and cv_profile.name:
            return cv_profile.name
        return self.cv_profile_name


# ── Priority queue runner ─────────────────────────────────────────────────────

# Maps platform_name → applier class (populated by each applier module via register())
_APPLIER_REGISTRY: dict[str, type[BaseApplier]] = {}


def register(cls: type[BaseApplier]) -> type[BaseApplier]:
    """Decorator: register an applier class so run_apply_queue can find it."""
    _APPLIER_REGISTRY[cls.platform_name] = cls
    return cls


def run_apply_queue(db: Session) -> dict:
    """
    Process QUEUED applications across all active platforms.
    Per platform: takes top N by priority_score (N = platform.daily_limit),
    leaving any excess in QUEUED for tomorrow.
    """
    # Import applier modules so their @register decorators fire
    from services.appliers import (  # noqa: F401
        getonboard, computrabajo, linkedin,
        indeed, zonajobs, bumeran, workana,
    )

    stats = {"applied": 0, "failed": 0, "review_form": 0, "skipped": 0}

    active_platforms = db.query(Platform).filter_by(is_active=True).all()

    for platform in active_platforms:
        if platform.name not in _APPLIER_REGISTRY:
            continue

        ApplierClass = _APPLIER_REGISTRY[platform.name]
        applier = ApplierClass(db)

        # Priority queue: best scored first, capped at daily_limit
        queued = (
            db.query(Application)
            .join(Job, Application.job_id == Job.id)
            .filter(Application.status == ApplicationStatus.QUEUED.value)
            .filter(Job.platform_id == platform.id)
            .filter(Application.retry_count < MAX_RETRIES)
            .order_by(Application.priority_score.desc())
            .limit(platform.daily_limit)
            .all()
        )

        if not queued:
            continue

        logger.info("queue.platform_start", platform=platform.name, count=len(queued))

        for application in queued:
            result = applier.apply(application)
            if result.success:
                stats["applied"] += 1
            elif result.status == ApplicationStatus.REVIEW_FORM.value:
                stats["review_form"] += 1
                _notify_orphan(application, db)
            else:
                stats["failed"] += 1

    logger.info("queue.done", **stats)
    return stats


def _notify_orphan(application: Application, db: Session):
    job = db.query(Job).filter_by(id=application.job_id).first()
    questions = application.orphan_questions or []
    notifier.alert(
        f"REVIEW_FORM — {job.title} @ {job.company}\n"
        f"Preguntas sin respuesta:\n"
        + "\n".join(f"  • {q.get('field', q)}" for q in questions)
        + f"\nAgregá las respuestas a data/answers.yaml y el bot reintentará mañana."
    )


def _take_error_screenshot(page, label: str) -> Optional[str]:
    if page is None:
        return None
    from services.screenshot import capture
    return capture(page, label)
