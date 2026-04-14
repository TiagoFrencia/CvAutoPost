"""
Tests for JobMatcher.
Gemini API calls are fully mocked — no real API calls happen.
"""
import json
import pytest
from unittest.mock import MagicMock, patch

from ai_engine.job_matcher import (
    JobMatcher,
    MatchResponse,
    _is_legally_blocked,
    _get_boost_hints,
    _parse_response,
    _build_job_prompt,
)
from core.models import Job, CVProfile


# ── Fixtures ─────────────────────────────────────────────────────────────────

def make_job(**kwargs) -> Job:
    defaults = dict(
        id=1,
        platform_id=1,
        external_id="test-001",
        title="Junior React Developer",
        company="TechCo",
        location="Remote",
        url="https://example.com/job/1",
        description="We are looking for a junior React developer. Remote, contractor welcome. LATAM ok.",
        modality="remoto",
        status="PENDING",
    )
    defaults.update(kwargs)
    job = Job(**{k: v for k, v in defaults.items() if k != "id"})
    job.id = defaults["id"]
    return job


def make_db_with_cv():
    db = MagicMock()
    cv_profile = MagicMock(spec=CVProfile)
    cv_profile.id = 1
    cv_profile.name = "remoto"
    db.query.return_value.filter_by.return_value.first.return_value = cv_profile
    db.query.return_value.filter_by.return_value.all.return_value = []
    return db


VALID_LLM_RESPONSE = json.dumps({
    "score": 85,
    "match_reason": "Good React match, remote contractor role from LATAM.",
    "auto_apply": True,
    "missing_skills": [],
    "risk_flags": [],
    "legal_viability": "viable",
})


# ── Pre-filter tests ──────────────────────────────────────────────────────────

def test_is_legally_blocked_us_citizen():
    assert _is_legally_blocked("Senior Dev - US Citizen Required", "") is True


def test_is_legally_blocked_w2():
    assert _is_legally_blocked("Developer", "Must be W-2 employee only") is True


def test_is_legally_blocked_green_card():
    assert _is_legally_blocked("Engineer - Green Card holders only", "") is True


def test_is_legally_blocked_clearance():
    assert _is_legally_blocked("Dev with Security Clearance", "") is True


def test_is_not_blocked_normal_remote():
    assert _is_legally_blocked("Junior React Developer - Remote", "LATAM ok, contractor") is False


def test_boost_hints_detects_latam():
    hints = _get_boost_hints("Junior Dev - LATAM welcome", "Argentina timezone ok")
    assert "latam" in hints
    assert "argentina" in hints


def test_boost_hints_detects_contractor():
    hints = _get_boost_hints("", "Open to contractor or B2B arrangements")
    assert "contractor" in hints
    assert "b2b" in hints


# ── Response parsing tests ────────────────────────────────────────────────────

def test_parse_response_valid():
    result = _parse_response(VALID_LLM_RESPONSE, job_id=1)
    assert result is not None
    assert result.score == 85
    assert result.auto_apply is True
    assert result.legal_viability == "viable"


def test_parse_response_with_markdown_wrapper():
    wrapped = f"```json\n{VALID_LLM_RESPONSE}\n```"
    result = _parse_response(wrapped, job_id=1)
    assert result is not None
    assert result.score == 85


def test_parse_response_invalid_json():
    result = _parse_response("Sorry, I cannot evaluate this.", job_id=1)
    assert result is None


def test_parse_response_score_out_of_range():
    bad = json.dumps({"score": 150, "match_reason": "x", "auto_apply": False})
    result = _parse_response(bad, job_id=1)
    assert result is None


def test_parse_response_missing_required_fields():
    incomplete = json.dumps({"score": 70})
    result = _parse_response(incomplete, job_id=1)
    assert result is None


# ── JobMatcher integration tests (Gemini mocked) ─────────────────────────────

@patch("ai_engine.job_matcher.get_cv", return_value={"meta": {}, "personal_info": {}})
@patch("ai_engine.job_matcher.maybe_cached_model")
def test_run_batch_scores_pending_jobs(mock_cache_ctx, mock_get_cv):
    db = make_db_with_cv()
    job = make_job()
    db.query.return_value.filter_by.return_value.all.return_value = [job]

    # Mock the context manager model
    mock_model = MagicMock()
    mock_model.generate_content.return_value.text = VALID_LLM_RESPONSE
    mock_cache_ctx.return_value.__enter__ = MagicMock(return_value=mock_model)
    mock_cache_ctx.return_value.__exit__ = MagicMock(return_value=False)

    matcher = JobMatcher(db, cv_profile_name="remoto")
    stats = matcher.run_batch()

    assert stats["scored"] == 1
    assert stats["auto_apply"] == 1


@patch("ai_engine.job_matcher.get_cv", return_value={"meta": {}, "personal_info": {}})
@patch("ai_engine.job_matcher.maybe_cached_model")
def test_run_batch_skips_legally_blocked(mock_cache_ctx, mock_get_cv):
    db = make_db_with_cv()
    job = make_job(title="Senior Engineer - US Citizen Required", description="W-2 only")
    db.query.return_value.filter_by.return_value.all.return_value = [job]

    mock_model = MagicMock()
    mock_cache_ctx.return_value.__enter__ = MagicMock(return_value=mock_model)
    mock_cache_ctx.return_value.__exit__ = MagicMock(return_value=False)

    matcher = JobMatcher(db, cv_profile_name="remoto")
    stats = matcher.run_batch()

    assert stats["skipped_prefilter"] == 1
    assert stats["scored"] == 0
    mock_model.generate_content.assert_not_called()
