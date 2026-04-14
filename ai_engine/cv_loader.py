"""
Loads and caches CV JSON files. Single source of truth for the AI engine.
PDFs are used only for file attachment in applications — never parsed here.
"""
import json
from functools import lru_cache
from pathlib import Path

import structlog

from core.config import settings

logger = structlog.get_logger()

CV_FILES = {
    "remoto": settings.cvs_dir / "cv_remoto.json",
    "local": settings.cvs_dir / "cv_local.json",
}


@lru_cache(maxsize=None)
def get_cv(profile_name: str) -> dict:
    """Load and cache a CV by profile name ('remoto' | 'local')."""
    if profile_name not in CV_FILES:
        raise ValueError(f"Unknown CV profile: '{profile_name}'. Valid: {list(CV_FILES.keys())}")

    path: Path = CV_FILES[profile_name]
    if not path.exists():
        raise FileNotFoundError(f"CV file not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    logger.info("cv_loader.loaded", profile=profile_name, path=str(path))
    return data


def get_pdf_path(profile_name: str) -> Path:
    cv = get_cv(profile_name)
    pdf_path = Path(cv["meta"]["pdf_path"])
    if not pdf_path.exists():
        raise FileNotFoundError(f"CV PDF not found: {pdf_path}")
    return pdf_path


def reload_all() -> None:
    """Clear cache and reload all CVs. Useful after editing JSON files."""
    get_cv.cache_clear()
    for name in CV_FILES:
        get_cv(name)
    logger.info("cv_loader.reloaded_all")
