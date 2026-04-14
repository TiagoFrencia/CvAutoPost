"""
Captures Playwright page screenshots for debugging failed applications.
"""
from datetime import datetime
from pathlib import Path

import structlog

from core.config import settings

logger = structlog.get_logger()


def capture(page, label: str) -> str:
    """
    Save a screenshot of the current Playwright page.
    Returns the file path as string, or empty string on failure.
    """
    settings.screenshots_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{label}.png"
    path = settings.screenshots_dir / filename

    try:
        page.screenshot(path=str(path), full_page=True)
        logger.info("screenshot.saved", path=str(path))
        return str(path)
    except Exception as e:
        logger.error("screenshot.failed", label=label, error=str(e))
        return ""
