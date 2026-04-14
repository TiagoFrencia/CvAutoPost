"""
Headless auto-login for platforms that support credential-based authentication.

Each login function navigates to the platform's login page, fills username/password,
submits, waits for the redirect that confirms success, and returns the resulting
cookies in Playwright dict format.

LinkedIn is deliberately NOT included — auto-login is too risky given LinkedIn's
anti-bot fingerprint detection. LinkedIn cookies typically last ~1 year and
must be renewed manually via login_helper.py.

Usage (from session_manager.auto_login):
    cookies = login_headless("computrabajo", "user@email.com", "pass")

Usage (to save credentials for the first time, run on Windows host):
    python -c "
    from services.session_manager import SessionManager
    sm = SessionManager()
    sm.save_credentials('computrabajo', 'your@email.com', 'yourpassword')
    "
"""
import os
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from playwright_stealth import stealth_sync

import structlog
from core.config import settings
from core.playwright_config import CHROMIUM_ARGS

logger = structlog.get_logger()

# Platform login configurations
_LOGIN_CONFIGS = {
    "computrabajo": {
        # Login redirects through secure.computrabajo.com (OpenID Connect).
        # The password field has a "remember password" overlay that blocks pointer events,
        # so we use JS to set values and submit the form.
        "url": "https://candidato.ar.computrabajo.com/acceso/",
        "email_selector": "input[name='Email'], input[name='email'], input[type='email']",
        "password_selector": "input[name='Password'], input[name='password'], input[type='password']",
        "submit_selector": "button:has-text('Continuar'), button[type='button'], button[type='submit']",
        "success_check": lambda page: "computrabajo.com" in page.url and "/Account/Login" not in page.url and "acceso" not in page.url,
        "js_submit": True,  # Use force fill + Enter to bypass overlay
    },
    "indeed": {
        "url": "https://ar.indeed.com/account/login",
        "email_selector": "#login-email-input, input[name='__email'], input[type='email']",
        "password_selector": "#login-password-input, input[name='__password'], input[type='password']",
        "submit_selector": "button[type='submit']",
        "success_check": lambda page: "login" not in page.url and "indeed.com" in page.url,
        "two_step": True,  # Indeed has a separate email → password step
    },
    "zonajobs": {
        "url": "https://www.zonajobs.com.ar/login",
        "email_selector": "input[name='user'], input[name='email'], input[type='email']",
        "password_selector": "input[name='password'], input[type='password']",
        "submit_selector": "button[type='submit']",
        "success_check": lambda page: "login" not in page.url and "zonajobs.com.ar" in page.url,
    },
    "bumeran": {
        "url": "https://www.bumeran.com.ar/login",
        "email_selector": "input[name='email'], input[type='email']",
        "password_selector": "input[name='password'], input[type='password']",
        "submit_selector": "button[type='submit']",
        "success_check": lambda page: "login" not in page.url and "bumeran.com.ar" in page.url,
    },
    "workana": {
        "url": "https://www.workana.com/login",
        "email_selector": "input[name='email'], input[type='email'], #email",
        "password_selector": "input[name='password'], input[type='password'], #password",
        "submit_selector": "button[type='submit'], input[type='submit']",
        "success_check": lambda page: "login" not in page.url and "workana.com" in page.url,
    },
}


def _start_xvfb() -> subprocess.Popen | None:
    """Start Xvfb virtual display on Linux if not already running."""
    if sys.platform != "linux":
        return None
    os.environ.setdefault("DISPLAY", ":99")
    proc = subprocess.Popen(
        ["Xvfb", ":99", "-screen", "0", "1280x800x24"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1)
    return proc


def _launch_browser(p):
    """
    Launch Chrome/Chromium for auto-login.
    Prefers the real Chrome executable (bypasses Cloudflare WAF).
    Falls back to Playwright Chromium if Chrome is not found.
    """
    chrome_path = settings.chrome_executable_path
    if chrome_path and __import__("pathlib").Path(chrome_path).exists():
        logger.info("auto_login.using_real_chrome", path=chrome_path)
        return p.chromium.launch(
            executable_path=chrome_path,
            headless=False,  # Xvfb provides the display on Linux
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
    logger.info("auto_login.using_playwright_chromium")
    return p.chromium.launch(headless=True, args=CHROMIUM_ARGS)


def login_headless(platform: str, username: str, password: str) -> list[dict] | None:
    """
    Attempt headless login for the given platform using real Chrome (Cloudflare bypass).
    Returns list of cookies on success, None on failure.
    """
    cfg = _LOGIN_CONFIGS.get(platform)
    if not cfg:
        logger.error("auto_login.no_config", platform=platform)
        return None

    logger.info("auto_login.attempt", platform=platform)
    xvfb_proc = _start_xvfb()

    with sync_playwright() as p:
        browser = _launch_browser(p)
        context = browser.new_context(locale="es-AR", viewport={"width": 1280, "height": 800})
        page = context.new_page()
        stealth_sync(page)

        try:
            page.goto(cfg["url"], wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(1000)  # short settle

            if cfg.get("js_submit"):
                # Platforms with overlays blocking pointer events: fill with force + Enter key
                try:
                    page.wait_for_selector(cfg["email_selector"], timeout=10_000)
                    page.locator(cfg["email_selector"]).fill(username, force=True)
                    page.wait_for_timeout(300)
                    page.locator(cfg["password_selector"]).fill(password, force=True)
                    page.wait_for_timeout(500)
                    page.locator(cfg["password_selector"]).press("Enter")
                    # OIDC redirect chain — wait for final destination
                    page.wait_for_load_state("domcontentloaded", timeout=25_000)
                    page.wait_for_timeout(3000)
                except Exception as e:
                    logger.warning("auto_login.js_submit_error", platform=platform, error=str(e))
            else:
                # Fill email
                try:
                    page.locator(cfg["email_selector"]).first.fill(username, timeout=10_000)
                except PWTimeout:
                    logger.warning("auto_login.email_field_not_found", platform=platform)
                    browser.close()
                    if xvfb_proc:
                        xvfb_proc.terminate()
                    return None

                # For two-step flows (e.g. Indeed: submit email, then fill password)
                if cfg.get("two_step"):
                    try:
                        page.locator(cfg["submit_selector"]).first.click(timeout=5_000)
                        page.wait_for_load_state("domcontentloaded", timeout=15_000)
                        page.wait_for_timeout(1000)
                    except PWTimeout:
                        pass  # Some platforms load the password field inline

                # Fill password
                try:
                    page.locator(cfg["password_selector"]).first.fill(password, timeout=10_000)
                except PWTimeout:
                    logger.warning("auto_login.password_field_not_found", platform=platform)
                    browser.close()
                    if xvfb_proc:
                        xvfb_proc.terminate()
                    return None

                # Submit
                try:
                    page.locator(cfg["submit_selector"]).first.click(timeout=8_000)
                    page.wait_for_load_state("domcontentloaded", timeout=25_000)
                    page.wait_for_timeout(2000)  # let redirects settle
                except PWTimeout:
                    logger.warning("auto_login.submit_timeout", platform=platform)

            # Verify success
            success_check = cfg["success_check"]
            if not success_check(page):
                logger.warning(
                    "auto_login.login_failed_url_check",
                    platform=platform,
                    url=page.url,
                )
                browser.close()
                if xvfb_proc:
                    xvfb_proc.terminate()
                return None

            cookies = context.cookies()
            logger.info("auto_login.success", platform=platform, cookies=len(cookies))
            browser.close()
            if xvfb_proc:
                xvfb_proc.terminate()
            return cookies

        except Exception as e:
            logger.error("auto_login.error", platform=platform, error=str(e))
            try:
                browser.close()
            except Exception:
                pass
            if xvfb_proc:
                xvfb_proc.terminate()
            return None
