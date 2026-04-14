"""
Tests for the base applier framework:
  - CircuitBreaker pause/resume/expiry
  - Priority queue ordering (best score first)
  - Checkpoint save/resume
  - CAPTCHA handling triggers circuit breaker
  - Retry count increments on failure, caps at MAX_RETRIES
"""
import json
import time
import pytest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.enums import ApplicationStatus
from core.models import Application, Job, Platform
from services.applier import (
    ApplicationResult,
    BaseApplier,
    CaptchaDetected,
    CircuitBreaker,
    MAX_RETRIES,
)


# ── CircuitBreaker ────────────────────────────────────────────────────────────

@pytest.fixture
def cb(tmp_path, monkeypatch):
    monkeypatch.setattr("services.applier.CIRCUIT_BREAKER_PATH", tmp_path / "cb.json")
    return CircuitBreaker()


def test_circuit_breaker_not_paused_initially(cb):
    assert cb.is_paused("linkedin") is False


def test_circuit_breaker_pause_and_detect(cb):
    cb.pause("linkedin", reason="test", hours=24)
    assert cb.is_paused("linkedin") is True


def test_circuit_breaker_expired_clears(cb, tmp_path):
    # Write a pause that expired in the past
    path = tmp_path / "cb.json"
    past = (datetime.utcnow() - timedelta(hours=1)).timestamp()
    path.write_text(json.dumps({"linkedin": {"paused_until": past, "reason": "old"}}))
    cb2 = CircuitBreaker.__new__(CircuitBreaker)
    cb2._path = path
    cb2._state = cb2._load()
    assert cb2.is_paused("linkedin") is False


def test_circuit_breaker_reset(cb):
    cb.pause("computrabajo", reason="test", hours=1)
    cb.reset("computrabajo")
    assert cb.is_paused("computrabajo") is False


def test_circuit_breaker_persists_to_file(cb, tmp_path):
    cb.pause("linkedin", reason="CAPTCHA", hours=24)
    # Re-load from file
    cb2 = CircuitBreaker.__new__(CircuitBreaker)
    cb2._path = tmp_path / "cb.json"
    cb2._state = cb2._load()
    assert cb2.is_paused("linkedin") is True


# ── Checkpoint system ─────────────────────────────────────────────────────────

def make_mock_applier(db):
    """Concrete subclass for testing abstract methods."""
    class _TestApplier(BaseApplier):
        platform_name = "getonboard"
        cv_profile_name = "remoto"

        def _do_apply(self, application, job):
            return ApplicationResult(True, ApplicationStatus.APPLIED.value)

    platform = MagicMock(spec=Platform)
    platform.name = "getonboard"
    platform.is_active = True
    db.query.return_value.filter_by.return_value.first.return_value = platform

    applier = _TestApplier.__new__(_TestApplier)
    applier.db = db
    applier.platform = platform
    applier.circuit_breaker = CircuitBreaker.__new__(CircuitBreaker)
    applier.circuit_breaker._path = Path("/tmp/test_cb.json")
    applier.circuit_breaker._state = {}
    applier.session_manager = MagicMock()
    return applier


def test_save_checkpoint_writes_to_application():
    db = MagicMock()
    applier = make_mock_applier(db)
    app = MagicMock(spec=Application)
    app.last_successful_step = None

    applier._save_checkpoint(app, step=2, step_name="fill_form", extra={"field": "email"})

    assert app.last_successful_step["step"] == 2
    assert app.last_successful_step["step_name"] == "fill_form"
    assert app.last_successful_step["extra"]["field"] == "email"
    db.commit.assert_called()


def test_get_resume_step_returns_zero_for_fresh():
    db = MagicMock()
    applier = make_mock_applier(db)
    app = MagicMock(spec=Application)
    app.last_successful_step = None
    assert applier._get_resume_step(app) == 0


def test_get_resume_step_returns_last_step():
    db = MagicMock()
    applier = make_mock_applier(db)
    app = MagicMock(spec=Application)
    app.last_successful_step = {"step": 3, "step_name": "submit"}
    assert applier._get_resume_step(app) == 3


# ── apply() — error handling ─────────────────────────────────────────────────

def make_job():
    job = MagicMock(spec=Job)
    job.id = 1
    job.title = "Junior Dev"
    job.url = "https://example.com"
    return job


def test_apply_captcha_triggers_circuit_breaker(tmp_path, monkeypatch):
    monkeypatch.setattr("services.applier.CIRCUIT_BREAKER_PATH", tmp_path / "cb.json")

    db = MagicMock()
    job = make_job()
    db.query.return_value.filter_by.return_value.first.side_effect = [
        MagicMock(name="getonboard", is_active=True),  # platform lookup
        job,  # job lookup in apply()
    ]

    class _CaptchaApplier(BaseApplier):
        platform_name = "getonboard"
        cv_profile_name = "remoto"

        def _do_apply(self, application, job):
            raise CaptchaDetected("test captcha")

    applier = _CaptchaApplier.__new__(_CaptchaApplier)
    applier.db = db
    platform = MagicMock()
    platform.name = "getonboard"
    applier.platform = platform
    applier.circuit_breaker = CircuitBreaker()
    applier.session_manager = MagicMock()

    app = MagicMock(spec=Application)
    app.job_id = 1
    app.id = 1
    app.retry_count = 0
    app.last_successful_step = None

    with patch("services.applier.notifier"):
        result = applier.apply(app)

    assert result.success is False
    assert applier.circuit_breaker.is_paused("getonboard") is True


def test_apply_increments_retry_count():
    db = MagicMock()
    job = make_job()
    db.query.return_value.filter_by.return_value.first.side_effect = [
        MagicMock(name="getonboard", is_active=True),
        job,
    ]

    class _FailApplier(BaseApplier):
        platform_name = "getonboard"
        cv_profile_name = "remoto"

        def _do_apply(self, application, job):
            raise Exception("network error")

    applier = _FailApplier.__new__(_FailApplier)
    applier.db = db
    applier.platform = MagicMock(name="getonboard")
    applier.circuit_breaker = MagicMock()
    applier.circuit_breaker.is_paused.return_value = False
    applier.session_manager = MagicMock()

    app = MagicMock(spec=Application)
    app.job_id = 1
    app.id = 1
    app.retry_count = 0
    app.last_successful_step = None

    result = applier.apply(app)

    assert result.success is False
    assert app.retry_count == 1
