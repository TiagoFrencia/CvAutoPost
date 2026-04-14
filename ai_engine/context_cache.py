"""
Ollama Model wrapper — replaces Gemini context caching.

Since Ollama runs locally (no API cost per call), context caching is unnecessary.
We expose the same context-manager interface so job_matcher.py stays clean.

Gemini is kept as optional fallback: if Ollama is unreachable and GEMINI_API_KEY
is set, the caller can catch the exception and use Gemini directly.
"""
from contextlib import contextmanager

import requests
import structlog

from core.config import settings

logger = structlog.get_logger()


class _Response:
    """Mimics Gemini's GenerateContentResponse so job_matcher needs no changes."""
    def __init__(self, text: str):
        self.text = text


class OllamaModel:
    """
    Thin wrapper around Ollama's /api/chat endpoint.
    Exposes .generate_content(prompt) to match the old Gemini interface.
    """

    def __init__(self, system_prompt: str):
        self._system_prompt = system_prompt
        self._url = f"{settings.ollama_url}/api/chat"
        self._model = settings.ollama_model

    def generate_content(self, prompt: str) -> _Response:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1},
        }
        resp = requests.post(self._url, json=payload, timeout=180)
        resp.raise_for_status()
        text = resp.json()["message"]["content"]
        return _Response(text)


@contextmanager
def maybe_cached_model(system_prompt_with_cv: str, job_count: int):
    """
    Context manager that yields an OllamaModel.
    job_count is kept for API compatibility with existing callers.
    """
    logger.info("ollama_model.ready", model=settings.ollama_model, job_count=job_count)
    yield OllamaModel(system_prompt_with_cv)
