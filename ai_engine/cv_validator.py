"""
Validates CV JSON files against the expected schema at pipeline startup.
A malformed CV causes a fatal error before any scraping or API calls happen.
"""
import structlog
from ai_engine.cv_loader import CV_FILES, get_cv
from services import notifier

logger = structlog.get_logger()

REQUIRED_TOP_LEVEL_KEYS = {"meta", "personal_info", "professional_summary"}
REQUIRED_META_KEYS = {"profile_type", "pdf_path"}


def validate_all() -> None:
    """Validate all CV JSON files. Raises SystemExit on failure."""
    errors: list[str] = []

    for profile_name in CV_FILES:
        try:
            cv = get_cv(profile_name)
            profile_errors = _validate_cv(profile_name, cv)
            errors.extend(profile_errors)
        except Exception as e:
            errors.append(f"[{profile_name}] Failed to load: {e}")

    if errors:
        msg = "CV validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        notifier.alert(msg)
        logger.critical("cv_validator.failed", errors=errors)
        raise SystemExit(msg)

    logger.info("cv_validator.ok", profiles=list(CV_FILES.keys()))


def _validate_cv(profile_name: str, cv: dict) -> list[str]:
    errors: list[str] = []

    missing_top = REQUIRED_TOP_LEVEL_KEYS - set(cv.keys())
    for key in missing_top:
        errors.append(f"[{profile_name}] Missing top-level key: '{key}'")

    meta = cv.get("meta", {})
    missing_meta = REQUIRED_META_KEYS - set(meta.keys())
    for key in missing_meta:
        errors.append(f"[{profile_name}] Missing meta key: '{key}'")

    return errors
