"""
Shared Playwright launch configuration.
Import CHROMIUM_ARGS and launch_options() in every scraper/applier that launches Chromium.
"""
from core.config import settings

# Flags that reduce Chromium RAM usage inside Docker.
# --disable-dev-shm-usage: use /tmp instead of /dev/shm (Docker's default is 64 MB)
# --no-sandbox: required when running as root inside a container
# --disable-gpu: no GPU in a headless server, avoids GPU process overhead
CHROMIUM_ARGS = [
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-extensions",
    "--mute-audio",
    "--no-first-run",
]

def get_args() -> list:
    if settings.playwright_headless:
        return CHROMIUM_ARGS
    return []


def headless() -> bool:
    """Returns False when PLAYWRIGHT_HEADLESS=false is set in .env (local debugging)."""
    return settings.playwright_headless
