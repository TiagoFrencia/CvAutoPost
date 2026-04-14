"""
Tests for services/telegram_bot.py — review-score approval/rejection flow.

No real Telegram bot token needed — all external calls are mocked.
"""
import pytest
from unittest.mock import MagicMock, patch

from core.enums import ApplicationStatus, JobStatus
from core.models import Application, CVProfile, Job, MatchResult


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_job(status=JobStatus.REVIEW_SCORE.value, platform_id=1):
    job = MagicMock(spec=Job)
    job.id = 42
    job.title = "Python Developer"
    job.company = "Acme"
    job.location = "Remote"
    job.url = "https://example.com/job/42"
    job.status = status
    job.platform_id = platform_id
    return job


def _make_match_result(score=72, cv_profile_id=1):
    mr = MagicMock(spec=MatchResult)
    mr.score = score
    mr.match_reason = "Good Python skills match"
    mr.missing_skills = ["Docker"]
    mr.risk_flags = []
    mr.cv_profile_id = cv_profile_id
    return mr


def _make_cv_profile(name="remoto", id_=1):
    cvp = MagicMock(spec=CVProfile)
    cvp.id = id_
    cvp.name = name
    return cvp


# ── _build_review_message ─────────────────────────────────────────────────────

def test_build_review_message_contains_score():
    from services.telegram_bot import _build_review_message
    job = _make_job()
    mr = _make_match_result(score=72)
    msg = _build_review_message(job, mr)
    assert "72" in msg
    assert "Python Developer" in msg
    assert "Acme" in msg


def test_build_review_message_missing_skills():
    from services.telegram_bot import _build_review_message
    job = _make_job()
    mr = _make_match_result()
    mr.missing_skills = ["Kubernetes", "Go"]
    msg = _build_review_message(job, mr)
    assert "Kubernetes" in msg
    assert "Go" in msg


def test_build_review_message_handles_none_match_result():
    from services.telegram_bot import _build_review_message
    job = _make_job()
    msg = _build_review_message(job, None)
    assert "Python Developer" in msg


# ── _approve_job ──────────────────────────────────────────────────────────────

def test_approve_job_creates_application():
    from services.telegram_bot import _approve_job
    import services.telegram_bot as tgbot

    db = MagicMock()
    job = _make_job()
    cv_profile = _make_cv_profile(name="remoto", id_=1)
    match_result = _make_match_result(score=72, cv_profile_id=1)

    # DB query returns: CVProfile, MatchResult, no existing Application
    def query_side_effect(model):
        mock_q = MagicMock()
        if model is tgbot.CVProfile:
            mock_q.filter_by.return_value.first.return_value = cv_profile
        elif model is tgbot.MatchResult:
            mock_q.filter_by.return_value.order_by.return_value.first.return_value = match_result
        elif model is tgbot.ApplicationModel:
            mock_q.filter_by.return_value.first.return_value = None  # no existing
        else:
            mock_q.filter_by.return_value.first.return_value = None
        return mock_q

    db.query.side_effect = query_side_effect

    _approve_job(db, job, "remoto")

    assert job.status == JobStatus.AUTO_APPLY.value
    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert added.status == ApplicationStatus.QUEUED.value
    assert added.priority_score == 72
    db.commit.assert_called_once()


def test_approve_job_skips_duplicate_application():
    from services.telegram_bot import _approve_job
    import services.telegram_bot as tgbot

    db = MagicMock()
    job = _make_job()
    cv_profile = _make_cv_profile()
    match_result = _make_match_result()
    existing_app = MagicMock()

    def query_side_effect(model):
        mock_q = MagicMock()
        if model is tgbot.CVProfile:
            mock_q.filter_by.return_value.first.return_value = cv_profile
        elif model is tgbot.MatchResult:
            mock_q.filter_by.return_value.order_by.return_value.first.return_value = match_result
        elif model is tgbot.ApplicationModel:
            mock_q.filter_by.return_value.first.return_value = existing_app
        return mock_q

    db.query.side_effect = query_side_effect

    _approve_job(db, job, "remoto")

    db.add.assert_not_called()  # no duplicate
    db.commit.assert_called_once()


def test_approve_job_handles_missing_cv_profile():
    from services.telegram_bot import _approve_job
    import services.telegram_bot as tgbot

    db = MagicMock()
    job = _make_job()

    def query_side_effect(model):
        mock_q = MagicMock()
        if model is tgbot.CVProfile:
            mock_q.filter_by.return_value.first.return_value = None  # profile not found
        return mock_q

    db.query.side_effect = query_side_effect

    _approve_job(db, job, "nonexistent")

    # Should not crash — job stays at AUTO_APPLY but no Application is created
    assert job.status == JobStatus.AUTO_APPLY.value
    db.add.assert_not_called()
    db.commit.assert_called_once()


# ── notify_review_score (no real Telegram) ───────────────────────────────────

def test_notify_review_score_noop_when_disabled():
    """When Telegram is not configured, notify is a no-op."""
    from services.telegram_bot import notify_review_score

    job = _make_job()
    mr = _make_match_result()

    with patch("services.telegram_bot.settings") as mock_settings:
        mock_settings.telegram_bot_token = ""
        mock_settings.telegram_chat_id = ""
        # Should not raise even though no bot token
        notify_review_score(job, mr, "remoto")


def test_notify_review_score_calls_send_when_enabled():
    """When Telegram is configured, it attempts to send a message."""
    import sys
    # Inject a fake 'telegram' module so the lazy import inside notify_review_score works
    fake_telegram = MagicMock()
    fake_telegram.Bot = MagicMock()
    fake_telegram.InlineKeyboardButton = MagicMock(return_value=MagicMock())
    fake_telegram.InlineKeyboardMarkup = MagicMock(return_value=MagicMock())

    with patch.dict(sys.modules, {"telegram": fake_telegram,
                                   "telegram.ext": MagicMock()}):
        from services.telegram_bot import notify_review_score

        job = _make_job()
        mr = _make_match_result()

        with patch("services.telegram_bot.settings") as mock_settings, \
             patch("services.telegram_bot.asyncio") as mock_asyncio:
            mock_settings.telegram_bot_token = "fake-token"
            mock_settings.telegram_chat_id = "12345"
            mock_asyncio.get_running_loop.side_effect = RuntimeError("no loop")
            mock_asyncio.run = MagicMock()

            notify_review_score(job, mr, "remoto")

            mock_asyncio.run.assert_called_once()
