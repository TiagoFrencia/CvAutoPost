"""
Tests for FormFiller.
LLM calls are mocked. YAML loading uses a temp file.
"""
import json
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path

from ai_engine.form_filler import (
    FormFiller,
    OrphanQuestion,
    _yaml_exact_match,
    _yaml_fuzzy_match,
    _parse_fill_response,
)

SAMPLE_ANSWERS = {
    "salary_expectation_usd": "1000",
    "full_name": "Tiago F. Frencia",
    "availability_start": "Inmediata",
    "english_level": "Técnico — Lectura de documentación.",
}


# ── YAML matching tests ───────────────────────────────────────────────────────

def test_yaml_exact_match():
    assert _yaml_exact_match(SAMPLE_ANSWERS, "full_name") == "Tiago F. Frencia"


def test_yaml_exact_match_normalised():
    assert _yaml_exact_match(SAMPLE_ANSWERS, "salary expectation usd") == "1000"


def test_yaml_fuzzy_match_partial():
    result = _yaml_fuzzy_match(SAMPLE_ANSWERS, "¿Cuál es tu salary expectation usd mensual?")
    assert result == "1000"


def test_yaml_no_match():
    assert _yaml_exact_match(SAMPLE_ANSWERS, "stripe_experience") is None
    assert _yaml_fuzzy_match(SAMPLE_ANSWERS, "stripe_experience") is None


# ── Response parsing tests ────────────────────────────────────────────────────

def test_parse_fill_response_normal():
    raw = json.dumps({"answer": "1000 USD mensuales"})
    result = _parse_fill_response(raw, "salary")
    assert result == "1000 USD mensuales"


def test_parse_fill_response_null_answer_returns_none():
    # When LLM returns null answer, we get None (never raises OrphanQuestion)
    raw = json.dumps({"answer": None})
    result = _parse_fill_response(raw, "unknown field")
    assert result is None


def test_parse_fill_response_missing_json_returns_none():
    result = _parse_fill_response("No JSON here", "some field")
    assert result is None


def test_parse_fill_response_empty_answer_returns_none():
    raw = json.dumps({"answer": "   "})
    result = _parse_fill_response(raw, "blank field")
    assert result is None


def test_parse_fill_response_strips_markdown():
    raw = "```json\n" + json.dumps({"answer": "Sí"}) + "\n```"
    result = _parse_fill_response(raw, "disponibilidad")
    assert result == "Sí"


# ── OrphanQuestion backward compatibility ─────────────────────────────────────

def test_orphan_question_exists_for_backward_compat():
    """OrphanQuestion class must remain importable — it may be caught elsewhere."""
    exc = OrphanQuestion("some question")
    assert exc.question_text == "some question"
    assert "some question" in str(exc)


# ── FormFiller integration tests ──────────────────────────────────────────────

@patch("ai_engine.form_filler.get_cv", return_value={"meta": {}, "personal_info": {}})
def test_form_filler_yaml_hit_no_llm(mock_get_cv, tmp_path, monkeypatch):
    answers_file = tmp_path / "answers.yaml"
    answers_file.write_text("full_name: 'Tiago F. Frencia'\n")
    monkeypatch.setattr("ai_engine.form_filler.ANSWERS_YAML_PATH", answers_file)
    monkeypatch.setattr("ai_engine.form_filler.PROFILE_CONTEXT_PATH", tmp_path / "missing.yaml")

    filler = FormFiller("remoto")
    result = filler.fill("full_name")
    assert result == "Tiago F. Frencia"


@patch("ai_engine.form_filler.get_cv", return_value={"meta": {}, "personal_info": {}})
@patch("ai_engine.form_filler.requests.post")
def test_form_filler_ollama_fallback(mock_post, mock_get_cv, tmp_path, monkeypatch):
    answers_file = tmp_path / "answers.yaml"
    answers_file.write_text("{}\n")
    monkeypatch.setattr("ai_engine.form_filler.ANSWERS_YAML_PATH", answers_file)
    monkeypatch.setattr("ai_engine.form_filler.PROFILE_CONTEXT_PATH", tmp_path / "missing.yaml")

    mock_post.return_value.json.return_value = {
        "message": {"content": json.dumps({"answer": "Sí"})}
    }
    mock_post.return_value.raise_for_status = MagicMock()

    filler = FormFiller("remoto")
    result = filler.fill("¿Tenés disponibilidad para viajar?")
    assert result == "Sí"


@patch("ai_engine.form_filler.get_cv", return_value={"meta": {}, "personal_info": {}})
@patch("ai_engine.form_filler.requests.post", side_effect=ConnectionError("Ollama down"))
def test_form_filler_gemini_fallback_when_ollama_fails(mock_post, mock_get_cv, tmp_path, monkeypatch):
    """When Ollama is unreachable, FormFiller falls back to Gemini."""
    answers_file = tmp_path / "answers.yaml"
    answers_file.write_text("{}\n")
    monkeypatch.setattr("ai_engine.form_filler.ANSWERS_YAML_PATH", answers_file)
    monkeypatch.setattr("ai_engine.form_filler.PROFILE_CONTEXT_PATH", tmp_path / "missing.yaml")

    mock_gemini_response = MagicMock()
    mock_gemini_response.text = json.dumps({"answer": "Disponibilidad inmediata"})

    mock_gemini_model = MagicMock()
    mock_gemini_model.generate_content.return_value = mock_gemini_response

    with patch("google.generativeai.configure"), \
         patch("google.generativeai.GenerativeModel", return_value=mock_gemini_model):
        filler = FormFiller("remoto")
        result = filler.fill("¿Cuándo podés empezar?")

    assert result == "Disponibilidad inmediata"


@patch("ai_engine.form_filler.get_cv", return_value={"meta": {}, "personal_info": {}})
def test_form_filler_never_raises_orphan(mock_get_cv, tmp_path, monkeypatch):
    """fill() must never raise OrphanQuestion, even for unknown fields."""
    answers_file = tmp_path / "answers.yaml"
    answers_file.write_text("{}\n")
    monkeypatch.setattr("ai_engine.form_filler.ANSWERS_YAML_PATH", answers_file)
    monkeypatch.setattr("ai_engine.form_filler.PROFILE_CONTEXT_PATH", tmp_path / "missing.yaml")

    filler = FormFiller("remoto")
    # Both Ollama and Gemini fail → should return None, not raise
    with patch.object(filler, "_ask_ollama", return_value=None), \
         patch.object(filler, "_ask_gemini", return_value=None):
        result = filler.fill("¿Cuántos años de experiencia con Kubernetes en producción?")
    assert result is None  # None is acceptable; OrphanQuestion is not


@patch("ai_engine.form_filler.get_cv", return_value={"meta": {}, "personal_info": {}})
def test_form_filler_profile_context_loaded(mock_get_cv, tmp_path, monkeypatch):
    """profile_context.yaml is loaded and scoped to the active CV profile."""
    answers_file = tmp_path / "answers.yaml"
    answers_file.write_text("{}\n")
    context_file = tmp_path / "profile_context.yaml"
    context_file.write_text(
        "remoto:\n  motivacion:\n    objetivo: 'Ser senior developer'\n"
        "general:\n  datos: 'test'\n"
    )
    monkeypatch.setattr("ai_engine.form_filler.ANSWERS_YAML_PATH", answers_file)
    monkeypatch.setattr("ai_engine.form_filler.PROFILE_CONTEXT_PATH", context_file)

    filler = FormFiller("remoto")
    ctx = filler.profile_context
    assert "objetivo" in ctx
    assert "Ser senior developer" in ctx
    assert "test" in ctx  # general section included


@patch("ai_engine.form_filler.get_cv", return_value={"meta": {}, "personal_info": {}})
def test_form_filler_profile_context_local_excludes_remoto(mock_get_cv, tmp_path, monkeypatch):
    """Local profile does not include the remoto section."""
    answers_file = tmp_path / "answers.yaml"
    answers_file.write_text("{}\n")
    context_file = tmp_path / "profile_context.yaml"
    context_file.write_text(
        "remoto:\n  motivacion:\n    objetivo: 'Remote dev goal'\n"
        "local:\n  motivacion:\n    objetivo: 'Local job goal'\n"
        "general:\n  datos: 'common'\n"
    )
    monkeypatch.setattr("ai_engine.form_filler.ANSWERS_YAML_PATH", answers_file)
    monkeypatch.setattr("ai_engine.form_filler.PROFILE_CONTEXT_PATH", context_file)

    filler = FormFiller("local")
    ctx = filler.profile_context
    assert "Local job goal" in ctx
    assert "Remote dev goal" not in ctx
    assert "common" in ctx  # general section always included


@patch("ai_engine.form_filler.get_cv", return_value={"meta": {}, "personal_info": {}})
def test_auto_save_short_answer(mock_get_cv, tmp_path, monkeypatch):
    """Short LLM answers are auto-saved to answers.yaml."""
    answers_file = tmp_path / "answers.yaml"
    answers_file.write_text("{}\n")
    monkeypatch.setattr("ai_engine.form_filler.ANSWERS_YAML_PATH", answers_file)
    monkeypatch.setattr("ai_engine.form_filler.PROFILE_CONTEXT_PATH", tmp_path / "missing.yaml")

    filler = FormFiller("remoto")
    with patch.object(filler, "_ask_ollama", return_value="Sí, disponible"), \
         patch.object(filler, "_ask_gemini", return_value=None):
        filler.fill("disponibilidad_turno_noche")

    saved = answers_file.read_text(encoding="utf-8")
    assert "disponibilidad_turno_noche" in saved
    assert "disponible" in saved


@patch("ai_engine.form_filler.get_cv", return_value={"meta": {}, "personal_info": {}})
def test_auto_save_skips_long_answer(mock_get_cv, tmp_path, monkeypatch):
    """Long answers (>150 chars) are NOT auto-saved (cover letters, essays)."""
    answers_file = tmp_path / "answers.yaml"
    answers_file.write_text("{}\n")
    monkeypatch.setattr("ai_engine.form_filler.ANSWERS_YAML_PATH", answers_file)
    monkeypatch.setattr("ai_engine.form_filler.PROFILE_CONTEXT_PATH", tmp_path / "missing.yaml")

    long_answer = "x" * 200
    filler = FormFiller("remoto")
    with patch.object(filler, "_ask_ollama", return_value=long_answer), \
         patch.object(filler, "_ask_gemini", return_value=None):
        result = filler.fill("carta de presentacion larga")

    assert result == long_answer
    saved = answers_file.read_text(encoding="utf-8")
    assert "carta_de_presentacion_larga" not in saved
